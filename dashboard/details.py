"""Slow-changing details and diagnostic tools for the web dashboard.

Details are refreshed in a background thread only while the web page is open,
since they spawn processes (systemctl, tailscale). Nothing that could expose
logs or other tailnet devices is collected: Tailscale is reduced to counts.

Tools (ping, DNS lookup) run on request from the web page; they validate the
target, run one at a time and are rate limited, because the page has no
password.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

REFRESH = 15.0
SERVICES = (("nanokvm-go.service", "NanoKVM Server"),
            ("kvmcomm.service", "KVM Comm (UI, video)"),
            ("tailscaled.service", "Tailscale"),
            ("ssh.service", "SSH"),
            ("systemd-resolved.service", "DNS resolver"),
            ("chrony.service", "Time sync"),
            ("avahi-daemon.service", "mDNS"))
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})"
                     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*\.?$")
TOOL_GAP = 2.0  # seconds between tool runs


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


def run(args, timeout):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# -- parsers ----------------------------------------------------------------

def parse_systemctl_show(text):
    """Blocks of KEY=VALUE lines (one per unit) -> {Id: {key: value}}."""
    units = {}
    for block in text.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            units[props["Id"]] = props
    return units


def services(show_text):
    units = parse_systemctl_show(show_text)
    out = []
    for unit, label in SERVICES:
        p = units.get(unit)
        if p is None or p.get("LoadState") == "not-found":
            continue
        try:
            restarts = int(p.get("NRestarts", ""))
        except ValueError:
            restarts = None
        out.append({"unit": unit, "label": label,
                    "active": p.get("ActiveState", "unknown"),
                    "sub": p.get("SubState", ""), "restarts": restarts})
    return out


def parse_tailscale(obj):
    """Backend state and peer counts only (no names or addresses)."""
    if not isinstance(obj, dict):
        return None
    peers = list((obj.get("Peer") or {}).values())
    self_node = obj.get("Self") or {}
    return {"state": obj.get("BackendState"),
            "ips": [ip for ip in self_node.get("TailscaleIPs") or [] if ":" not in ip],
            "peers": len(peers),
            "online": sum(1 for p in peers if p.get("Online")),
            "exit_node": any(p.get("ExitNode") for p in peers)}


def parse_resolv(text):
    return [line.split()[1] for line in text.splitlines()
            if line.startswith("nameserver") and len(line.split()) > 1]


def parse_ping_summary(text):
    """{sent, received, loss, avg_ms} from ping (busybox or iputils)."""
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    if not m:
        return None
    sent, got = int(m.group(1)), int(m.group(2))
    avg = re.search(r"= [\d.]+/([\d.]+)/", text)
    return {"sent": sent, "received": got,
            "loss": round(100.0 * (sent - got) / sent) if sent else None,
            "avg_ms": round(float(avg.group(1)), 1) if avg else None}


def valid_target(target):
    """IPv4/IPv6 literal or a host name; never something that looks like an option."""
    target = (target or "").strip()
    if not target or target.startswith("-"):
        return None
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in target else socket.AF_INET, target)
        return target
    except OSError:
        return target if HOST_RE.match(target) else None


# -- collectors -------------------------------------------------------------

class Details:
    """Web-only details; self.result is replaced as a whole on each refresh."""

    def __init__(self, watched, root="/"):
        self.watched = watched  # callable: is the web page open?
        self.root = root
        self.systemctl = shutil.which("systemctl")
        self.tailscale = shutil.which("tailscale")
        self.result = None

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            if self.watched():
                try:
                    self.refresh()
                except Exception as e:  # keep going; one bad round must not end it
                    print("dashboard: details error %r" % e, file=sys.stderr,
                          flush=True)
                time.sleep(REFRESH)
            else:
                time.sleep(1.0)

    def refresh(self):
        svc = []
        if self.systemctl:
            svc = services(run([self.systemctl, "show", "--no-pager",
                                "--property=Id,LoadState,ActiveState,SubState,NRestarts"]
                               + [u for u, _ in SERVICES], 5))
        ts = None
        if self.tailscale:
            try:
                ts = parse_tailscale(json.loads(run([self.tailscale, "status", "--json"], 5)))
            except ValueError:
                ts = None
        self.result = {
            "services": svc,
            "tailscale": ts,
            "dns_servers": parse_resolv(read(os.path.join(self.root, "etc/resolv.conf"))),
            "kernel": read(os.path.join(self.root, "proc/sys/kernel/osrelease")),
            "at": time.time(),
        }


class Tools:
    """ping / DNS lookup on demand: one at a time, at most one per TOOL_GAP."""

    def __init__(self):
        self.ping = shutil.which("ping")
        self._lock = threading.Lock()
        self._last = 0.0

    def run(self, kind, target):
        target = valid_target(target)
        if target is None:
            return {"error": "Enter an IP address or host name."}
        if kind not in ("ping", "dns"):
            return {"error": "Unknown tool."}
        if not self._lock.acquire(blocking=False):
            return {"error": "Another check is still running."}
        try:
            if time.monotonic() - self._last < TOOL_GAP:
                return {"error": "Please wait a moment and try again."}
            result = self._ping(target) if kind == "ping" else self._dns(target)
            result["target"] = target
            self._last = time.monotonic()
            return result
        finally:
            self._lock.release()

    def _ping(self, target):
        if not self.ping:
            return {"error": "ping is not available on this device."}
        summary = parse_ping_summary(
            run([self.ping, "-c", "3", "-W", "2", target], 10))
        if summary is None:
            return {"error": "Could not reach or resolve %s." % target}
        return {"ping": summary}

    def _dns(self, target):
        box = {}

        def lookup():
            t0 = time.monotonic()
            try:
                infos = socket.getaddrinfo(target, None)
                box["addresses"] = sorted({i[4][0] for i in infos})
            except (OSError, UnicodeError) as e:
                box["error"] = "Lookup failed: %s" % (e.strerror or e)
            box["ms"] = round((time.monotonic() - t0) * 1000, 1)

        worker = threading.Thread(target=lookup, daemon=True)
        worker.start()
        worker.join(5)
        if worker.is_alive():
            return {"error": "DNS lookup timed out after 5 s."}
        if "error" in box:
            return {"error": box["error"]}
        return {"dns": {"addresses": box["addresses"], "ms": box["ms"]}}
