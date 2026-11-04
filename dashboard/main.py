#!/usr/bin/env python3
"""Device status: addresses, health, connectivity and processes.

Seven pages on the panel (screen.py). With "swipe" paging (the default) a
left/right swipe changes the page; with "tap" paging the right half of the
screen is next and the left half previous, and swipes still work. Pages slide
in (slide.py). The panel goes dark after a while without touch, if enabled;
the next touch only wakes it. Settings (settings.py) are changed on the
SETTINGS page and on the web page.

While the App is open it also serves a live page under WEB_PATH on
DASHBOARD_WEB_PORT, backed by "<WEB_PATH>api/status?since=<seq>",
"<WEB_PATH>api/history" and "<WEB_PATH>api/settings". Everything stops with
the App. Exit with the host's edge gesture.

Work only happens while someone looks: the panel is lit, or a web page polled
in the last VIEWER_TTL seconds. Otherwise nothing is sampled or probed and
the main thread sleeps until a touch or a web request wakes it; history gets
a gap for the missed seconds. The panel is only redrawn when what the page
shows changed (screen.page_key) and only flushed when a frame was drawn.

Threading: the main thread samples cheap /proc and /sys files (collector.py)
and publishes each snapshot by replacing latest["cur"] with (seq, snap); a
snapshot is never mutated after that, so web threads read it without a lock.
StatusCache encodes each sample once for all open pages. The process list is
only sampled while someone looks at it (TOP page or an open web page).
"""

import errno
import ipaddress
import json
import os
import select
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from appbase import LEFT, RIGHT, TAP, app

import slide
from collector import History, Prober, ProcTop, Sampler, tailscale_ip
from details import Details, Tools
from screen import PAGES, SETTINGS_PAGE, TOP_PAGE, draw, page_key, settings_rows
from settings import OPTIONS, PAGING, TIMEOUTS, Settings

APP_DIR = Path(__file__).resolve().parent
WEB_PATH = "/apps/%s/" % APP_DIR.name
SPARK_POINTS = 120  # history handed to the panel's sparkline
SAMPLE = 1.0
PROC_EVERY = 2.0
VIEWER_TTL = 10.0  # a web page counts as open this long after its last poll
IDLE_WAIT = 5.0  # longest sleep while nobody looks (touch and web wake it sooner)
STALE = 2 * SAMPLE  # a web poll waits for a new sample if the last is older
DEBUG = os.environ.get("DASHBOARD_DEBUG") == "1"


# -- web --------------------------------------------------------------------

def host_allowed(header, hostname):
    """Is a request's Host header a name this device is reached by?

    The page has no password, so a page on some other site must not read it
    or change settings by pointing its own domain at this device (DNS
    rebinding). Accepted: IP addresses, localhost, the device's host name,
    <name>.local (mDNS) and <name>.<tailnet>.ts.net (Tailscale MagicDNS).
    """
    host = (header or "").strip().lower()
    if host.startswith("["):  # [IPv6]:port
        host = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":")[0]
    host = host.rstrip(".")
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    name = (hostname or "").strip().lower().split(".")[0]
    if host in ("localhost", name, name + ".local"):
        return bool(host)
    labels = host.split(".")
    return bool(name) and len(labels) == 4 and labels[0] == name \
        and labels[2:] == ["ts", "net"]


class Waker:
    """Lets any thread end the main thread's wait early (a socket pair, so
    select() can watch it together with the touch device)."""

    def __init__(self):
        self._r, self._w = socket.socketpair()
        self._r.setblocking(False)
        self._w.setblocking(False)

    def wake(self):
        try:
            self._w.send(b"x")
        except OSError:  # buffer full: a wake-up is already pending
            pass

    def wait(self, fds, timeout):
        """Until one of fds is readable, wake() is called, or timeout.

        Returns the readable fds (not counting the wake-up socket).
        """
        try:
            ready = select.select([self._r] + fds, [], [], max(0.0, timeout))[0]
        except (OSError, ValueError):
            time.sleep(min(max(0.0, timeout), 0.1))
            ready = []
        try:
            while self._r.recv(64):
                pass
        except OSError:
            pass
        return [fd for fd in ready if fd is not self._r]


class StatusCache:
    """api/status and api/history bodies, encoded once per sample.

    Every open page polls every 2 s; pages in step ask for the same `since`,
    so they share one encoding instead of each serialising the snapshot and
    history again. Entries are dropped when the next sample is published.
    """

    MAX = 32  # distinct `since` values kept per sample

    def __init__(self, history, latest, boot_id):
        self.history, self.latest, self.boot_id = history, latest, boot_id
        self._lock = threading.Lock()
        self._seq = None
        self._entries = {}

    def get(self, since, with_snap=True):
        """JSON object text without its closing brace, so fields can follow."""
        seq, snap = self.latest["cur"]
        key = (since, with_snap)
        with self._lock:
            if seq != self._seq:
                self._seq, self._entries = seq, {}
            body = self._entries.get(key)
            if body is None:
                now, cols = self.history.since(since, upto=seq)
                parts = ['{"boot_id":%s,"now_seq":%d' % (json.dumps(self.boot_id), now)]
                if with_snap:
                    parts.append(',"snap":' + json.dumps(snap, separators=(",", ":")))
                parts.append(',"history":' + json.dumps(cols, separators=(",", ":")))
                body = "".join(parts).encode()
                if len(self._entries) < self.MAX:
                    self._entries[key] = body
        return body


def is_json(headers):
    ctype = headers.get("Content-Type", "")
    return ctype.split(";")[0].strip().lower() == "application/json"


def start_web(port, latest, history, boot_id, viewers, tools, settings,
              waker, fresh):
    """Serve the page and API in daemon threads; returns an error or ""."""
    try:
        page = (APP_DIR / "assets" / "index.html").read_bytes()
    except OSError:
        return "NO INDEX.HTML"

    hostname = socket.gethostname()
    cache = StatusCache(history, latest, boot_id)

    def wait_fresh():
        """After an idle spell the last sample is old: wake the main thread
        and give it a moment to sample, so the page never shows stale data."""
        if time.monotonic() - latest["at"] <= STALE:
            return
        waker.wake()
        with fresh:
            fresh.wait_for(lambda: time.monotonic() - latest["at"] <= STALE, 1.5)

    class Handler(BaseHTTPRequestHandler):
        timeout = 5  # a stalled client must not pin a thread forever

        def allowed(self):
            if host_allowed(self.headers.get("Host"), hostname):
                return True
            self.send_error(403, "Open this page by the device's IP address "
                                 "or host name")
            return False

        def do_GET(self):
            if not self.allowed():
                return
            url = urlsplit(self.path)
            if url.path in ("/", WEB_PATH.rstrip("/")):
                self.redirect(WEB_PATH)
            elif url.path == WEB_PATH:
                self.reply("text/html; charset=utf-8", page)
            elif url.path == WEB_PATH + "api/status":
                viewers["seen"] = time.monotonic()  # someone looks: sample
                wait_fresh()
                try:
                    since = int(parse_qs(url.query).get("since", ["0"])[0])
                except ValueError:
                    since = 0
                # settings ride along, so the Settings tab needs no polling
                self.reply("application/json", cache.get(since) +
                           b',"settings":' + json.dumps(settings.values).encode()
                           + b"}")
            elif url.path == WEB_PATH + "api/history":
                self.reply("application/json",
                           cache.get(0, with_snap=False) + b"}")
            elif url.path == WEB_PATH + "api/settings":
                self.reply_json({"settings": settings.values,
                                 "options": OPTIONS})
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            # The page has no password. Requiring a JSON content type makes
            # a browser ask before another site's page can post here, and
            # this server never says yes (no CORS headers).
            if path in (WEB_PATH + "api/tool", WEB_PATH + "api/settings") \
                    and not is_json(self.headers):
                self.reply_json({"error": "expected application/json"}, 415)
                return
            if path == WEB_PATH + "api/tool":
                # diagnostic tools: {"tool": "ping"|"dns", "target": "..."}
                req = self.read_json()
                if not isinstance(req, dict):
                    req = {}
                self.reply_json(tools.run(str(req.get("tool", "")),
                                          str(req.get("target", ""))))
            elif path == WEB_PATH + "api/settings":
                errors = settings.update(self.read_json())
                if errors:
                    self.reply_json({"error": "invalid settings",
                                     "errors": errors}, 400)
                else:
                    self.reply_json({"settings": settings.values,
                                     "options": OPTIONS})
            else:
                self.send_error(404)

        def read_json(self):
            """Request body as JSON (at most 1 KiB), or None."""
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 1024:  # read(-1) would wait for EOF
                    return None
                return json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, RecursionError):  # RecursionError: deep nesting
                return None

        def redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def reply_json(self, obj, status=200):
            self.reply("application/json",
                       json.dumps(obj, separators=(",", ":")).encode(), status)

        def reply(self, ctype, body, status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    try:
        server = ThreadingHTTPServer(("", port), Handler)
    except OSError as e:
        return "PORT %d %s" % (port, "IN USE" if e.errno == errno.EADDRINUSE
                               else "ERROR %s" % e.errno)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return ""


def read_config(env):
    """Return (port or None, web_error) from DASHBOARD_WEB_PORT.

    Everything else is a setting (settings.py), changed from the panel or the
    web page.
    """
    try:
        port = int(env.get("DASHBOARD_WEB_PORT", "8080"))
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        return None, "BAD WEB PORT"
    return port, ""


def next_value(values, key):
    """The value a tap on the panel's settings row for key moves to."""
    if key == "paging":
        return PAGING[(PAGING.index(values["paging"]) + 1) % len(PAGING)]
    if key == "screen_off":
        return not values["screen_off"]
    return TIMEOUTS[(TIMEOUTS.index(values[key]) + 1) % len(TIMEOUTS)]


@app()
def main(ctx):
    port, web_error = read_config(ctx.env)
    settings = Settings(str(APP_DIR / "settings.json"))
    values = settings.values
    sampler = Sampler()
    viewers = {"seen": -VIEWER_TTL}
    st = {"page": 0, "proc_age": PROC_EVERY, "procs": None, "drawn": None,
          "dirty": False, "max_ms": 0.0, "report": 0.0,
          "awake": True, "touched": time.monotonic()}

    def web_open():
        return time.monotonic() - viewers["seen"] < VIEWER_TTL

    def active():
        """Is anyone looking? If not, nothing is sampled, probed or drawn."""
        return st["awake"] or web_open()

    waker = Waker()
    prober = Prober(values["probe_host"], values["dns_name"], active=active)
    settings.listen(lambda v: prober.configure(v["probe_host"], v["dns_name"]))
    # a new auto-off setting counts from now, not from the last touch
    changed_at = [None]  # set by any thread; a float store is atomic
    settings.listen(lambda _v: changed_at.__setitem__(0, time.monotonic()))
    settings.listen(lambda _v: waker.wake())  # show the change on the panel now
    prober.start()
    top = ProcTop()
    history = History()
    latest = {}
    fresh = threading.Condition()  # notified after every sample

    details = Details(web_open)
    details.start()
    boot_id = os.urandom(4).hex()

    def publish(blank=False):
        """Sample and publish. blank: the counters span an idle spell, so
        rates and CPU would be averages over it; show them as unknown."""
        snap = sampler.sample()
        if blank:
            snap["cpu"] = snap["rx_rate"] = snap["tx_rate"] = None
            snap["cores"] = [None] * len(snap["cores"])
            for i in snap["ifaces"]:
                i["rx_rate"] = i["tx_rate"] = None
        links = prober.wifi
        for i in snap["ifaces"]:
            link = links.get(i["name"])
            i["wifi"] = dict(link) if link else None
            if i["wifi_dbm"] is None and link:
                i["wifi_dbm"] = link["signal_dbm"]
        snap["probes"] = prober.view()
        snap["video"] = dict(prober.video) if prober.video else None
        snap["tailscale_ip"] = tailscale_ip(snap["ifaces"]) or \
            prober.tailscale_cli
        # the process scan is the costliest read, so only run it on demand
        watched = (st["awake"] and st["page"] == TOP_PAGE) or web_open()
        st["proc_age"] += SAMPLE
        if watched and st["proc_age"] >= PROC_EVERY:
            st["proc_age"] = 0.0
            st["procs"] = top.sample()
        snap["procs"] = st["procs"] if watched else None
        snap["details"] = details.result if web_open() else None
        seq = history.append(snap)
        latest["cur"] = (seq, snap)  # published: never mutated after this line
        latest["at"] = time.monotonic()
        with fresh:
            fresh.notify_all()

    publish()
    if port is not None:
        web_error = start_web(port, latest, history, boot_id, viewers, Tools(),
                              settings, waker, fresh)
    if web_error:  # also shown on the WEB page
        print("dashboard: " + web_error, file=sys.stderr, flush=True)

    info = {"port": port, "web_error": web_error, "path": WEB_PATH}
    animate = slide.can(ctx.fb)

    def redraw():
        """Draw the page if it is lit and shows something new."""
        if not st["awake"]:
            return  # dark: no drawing at all
        snap = latest["cur"][1]
        key = page_key(snap, st["page"], info, settings.values)
        if key is not None and key == st["drawn"]:
            return  # same pixels as on screen
        _, spark = history.since(history.seq - SPARK_POINTS)
        draw(ctx.fb, snap, st["page"], info, spark, settings.values)
        st["drawn"] = key
        st["dirty"] = True

    def page(step):
        old = bytes(ctx.fb._buf) if animate else None
        st["page"] = (st["page"] + step) % len(PAGES)
        redraw()
        if animate:
            slide.play(ctx.fb, old, bytes(ctx.fb._buf), step > 0)
            st["dirty"] = True  # play() leaves its final frame unflushed

    def sleep():
        st["awake"] = False
        st["drawn"] = None  # the next wake draws from scratch
        ctx.fb.clear(0)
        st["dirty"] = True  # one black frame, then nothing until a touch

    def wake():
        st["awake"] = True
        now = st["touched"] = time.monotonic()
        if now - latest["at"] > STALE:
            sample(now)  # nothing was sampled while dark; this also draws
        else:
            redraw()

    def touched():
        """Note a touch; True if it only woke the panel and is used up."""
        st["touched"] = time.monotonic()
        if st["awake"]:
            return False
        wake()
        return True

    def on_tap(x, y):
        if touched():
            return
        if st["page"] == SETTINGS_PAGE:
            for key, rect in settings_rows(ctx.fb):
                v = settings.values
                if key == "screen_off_s" and not v["screen_off"]:
                    continue  # greyed out: the tap falls through to paging
                if rect.contains(x, y):
                    errors = settings.update({key: next_value(v, key)})
                    if errors:
                        print("dashboard: settings: %s" % errors,
                              file=sys.stderr, flush=True)
                    redraw()
                    return
        if settings.values["paging"] == "tap":
            page(-1 if x < ctx.width // 2 else 1)

    def on_swipe(kind, _x, _y):
        if touched():
            return
        # content follows the finger: swiping left brings in the next page;
        # swipes from the side edges belong to the host's exit gesture.
        # Swipes work in both paging modes, so no page is ever a dead end.
        if kind == LEFT:
            page(1)
        elif kind == RIGHT:
            page(-1)

    def screen_timer(now):
        """Seconds until the panel should turn off, or None."""
        at, changed_at[0] = changed_at[0], None
        if at is not None:
            st["touched"] = max(st["touched"], at)
        v = settings.values
        if not st["awake"]:
            if not v["screen_off"]:  # auto off was disabled on the web page
                wake()
            return None
        if not v["screen_off"]:
            return None
        left = st["touched"] + v["screen_off_s"] - now
        if left <= 0:
            sleep()
            return None
        return left

    def sample(now):
        """Publish a sample; after an idle spell, mark the gap first."""
        idle = now - latest["at"]
        blank = idle > STALE
        if blank:
            history.gap(int(idle / SAMPLE) - 1)
            prober.kick()  # its results are as old as the gap
            st["proc_age"], st["procs"] = PROC_EVERY, None  # rescan now
            details.result = None  # loads again within a second
        t0 = time.perf_counter()
        publish(blank)
        redraw()
        if DEBUG:
            st["max_ms"] = max(st["max_ms"], (time.perf_counter() - t0) * 1000)
            st["report"] += SAMPLE
            if st["report"] >= 10:
                st["report"] = 0.0
                print("dashboard: max sample %.1f ms" % st["max_ms"],
                      file=sys.stderr, flush=True)

    touch_fd = getattr(ctx.touch, "_fd", -1)  # appbase internal; -1: no touch
    fds = [touch_fd] if isinstance(touch_fd, int) and touch_fd >= 0 else []
    # without the fd, touch can only be polled: keep that responsive
    poll_cap = None if fds or getattr(ctx.touch, "_fd", None) == -1 else 0.1
    empty = {"n": 0, "since": time.monotonic()}  # readable, no gesture

    def on_cont(_sig, _frame):
        # The host stops the App (SIGSTOP) during its exit gesture and may
        # draw on the framebuffer meanwhile; repaint everything on resume.
        st["repaint"] = True
        waker.wake()

    st["repaint"] = False
    if hasattr(signal, "SIGCONT"):
        signal.signal(signal.SIGCONT, on_cont)
    redraw()
    while True:
        now = time.monotonic()
        off_in = screen_timer(now)
        if active():
            if now - latest["at"] >= SAMPLE:
                sample(now)
            timeout = latest["at"] + SAMPLE - now
        else:
            timeout = IDLE_WAIT  # a touch or a web request ends it sooner
        if off_in is not None:
            timeout = min(timeout, off_in)
        if poll_cap is not None:
            timeout = min(timeout, poll_cap)
        if st["repaint"]:
            st["repaint"] = False
            st["drawn"] = None
            redraw()
            st["dirty"] = True  # also re-flushes a dark (black) panel
        if st["dirty"]:
            ctx.fb.flush()
            st["dirty"] = False
        ready = wait_input(waker, fds, timeout)
        gestures = ctx.poll()
        for kind, x, y in gestures:
            if kind == TAP:
                on_tap(x, y)
            else:
                on_swipe(kind, x, y)
        # A dead touch device stays "readable" forever. Finger moves also
        # read empty, but a panel reports at most a few hundred per second,
        # so far more than that in one second means stop watching it.
        if ready and not gestures:
            now = time.monotonic()
            if now - empty["since"] > 1.0:
                empty["n"], empty["since"] = 0, now
            empty["n"] += 1
            if empty["n"] > 500:
                print("dashboard: touch device keeps waking with no input; "
                      "polling it instead", file=sys.stderr, flush=True)
                fds, poll_cap = [], 0.1
        if st["dirty"]:
            ctx.fb.flush()
            st["dirty"] = False


def wait_input(waker, fds, timeout):
    """Sleep until a touch event, a wake-up from another thread or timeout;
    returns the readable fds."""
    return waker.wait(fds, timeout)


if __name__ == "__main__":
    main()
