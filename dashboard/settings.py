"""User settings for the Dashboard App, kept in settings.json beside main.py.

Both the panel (main thread) and the web page (server threads) change them.
Readers use Settings.values, a dict that is replaced on every change and never
mutated, so it needs no lock. Writers go through update(), which validates,
saves atomically and only then publishes the new dict.
"""

import ipaddress
import json
import os
import sys
import threading

from details import HOST_RE

PAGING = ("swipe", "tap")
TIMEOUTS = (30, 60, 300, 600)  # seconds before the panel turns off
DEFAULTS = {
    "paging": "swipe",
    "screen_off": True,
    "screen_off_s": 60,
    "probe_host": "1.1.1.1",  # IP only, so the internet check does not need DNS
    "dns_name": "www.cloudflare.com",
}
OPTIONS = {"paging": list(PAGING), "screen_off_s": list(TIMEOUTS)}


def _check(key, value):
    """Return (clean value, None) or (None, error message)."""
    if key == "paging":
        if value in PAGING:
            return value, None
        return None, "must be one of: " + ", ".join(PAGING)
    if key == "screen_off":
        if isinstance(value, bool):
            return value, None
        return None, "must be true or false"
    if key == "screen_off_s":
        if type(value) is int and value in TIMEOUTS:
            return value, None
        return None, "must be one of: " + ", ".join(map(str, TIMEOUTS))
    if not isinstance(value, str):
        return None, "must be text"
    value = value.strip()
    if not value:  # empty turns the check off
        return "", None
    if key == "probe_host":
        try:
            return str(ipaddress.ip_address(value)), None
        except ValueError:
            return None, "must be an IP address (or empty to turn the check off)"
    if key == "dns_name":
        try:
            ipaddress.ip_address(value)
            is_ip = True  # resolves without asking DNS, so it proves nothing
        except ValueError:
            is_ip = False
        if not is_ip and HOST_RE.match(value) and not value.startswith("-"):
            return value, None
        return None, "must be a host name (or empty to turn the check off)"
    return None, "unknown setting"


def validate(changes):
    """Return (clean changes, {key: error}) for a partial update."""
    if not isinstance(changes, dict):
        return {}, {"": "expected a JSON object"}
    clean, errors = {}, {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            errors[key] = "unknown setting"
            continue
        value, err = _check(key, value)
        if err:
            errors[key] = err
        else:
            clean[key] = value
    return clean, errors


def load(path):
    """Settings from path; missing or bad entries fall back to the defaults."""
    values = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except FileNotFoundError:
        return values
    except (OSError, ValueError) as e:
        log("cannot read %s (%s), using defaults" % (path, e))
        return values
    if not isinstance(stored, dict):
        log("%s is not a JSON object, using defaults" % path)
        return values
    known = {k: v for k, v in stored.items() if k in DEFAULTS}
    clean, errors = validate(known)
    for key, err in errors.items():
        log("setting %s in %s %s, using the default" % (key, path, err))
    values.update(clean)
    return values


def log(msg):
    print("dashboard: " + msg, file=sys.stderr, flush=True)


class Settings:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._listeners = []
        self.values = load(path)  # replaced on every change, never mutated

    def listen(self, fn):
        """Call fn(values) after every change, in the changing thread; keep it cheap."""
        self._listeners.append(fn)

    def update(self, changes):
        """Validate, save and publish; returns {key: error}, empty on success.

        Nothing is applied when any field is invalid or saving fails.
        """
        clean, errors = validate(changes)
        if errors:
            return errors
        with self._lock:
            values = dict(self.values, **clean)
            if values == self.values:
                return {}
            try:
                self._save(values)
            except OSError as e:
                log("cannot save %s: %s" % (self.path, e))
                return {"": "could not save the settings on the device"}
            self.values = values
            # under the lock, so listeners see changes in the order they happen
            for fn in self._listeners:
                try:
                    fn(values)
                except Exception as e:  # saved already; never fail the caller
                    log("settings listener failed: %r" % e)
        return {}

    def _save(self, values):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(values, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # atomic: a crash never leaves half a file
        try:  # make the rename itself survive a power cut (POSIX only)
            fd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
