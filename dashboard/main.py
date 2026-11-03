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

Threading: the UI tick only does cheap /proc and /sys reads (collector.py)
and publishes each snapshot by replacing latest["snap"]; a snapshot is never
mutated after that, so web threads read it without a lock. The process list
is only sampled while someone looks at it (TOP page or an open web page).
"""

import errno
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from appbase import LEFT, RIGHT, app

import slide
from collector import History, Prober, ProcTop, Sampler, tailscale_ip
from details import Details, Tools
from screen import PAGES, SETTINGS_PAGE, TOP_PAGE, draw, settings_rows
from settings import OPTIONS, PAGING, TIMEOUTS, Settings

APP_DIR = Path(__file__).resolve().parent
WEB_PATH = "/apps/%s/" % APP_DIR.name
SPARK_POINTS = 120  # history handed to the panel's sparkline
SAMPLE = 1.0
PROC_EVERY = 2.0
VIEWER_TTL = 10.0  # a web page counts as open this long after its last poll
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


def is_json(headers):
    ctype = headers.get("Content-Type", "")
    return ctype.split(";")[0].strip().lower() == "application/json"


def start_web(port, latest, history, boot_id, viewers, tools, settings):
    """Serve the page and API in daemon threads; returns an error or ""."""
    try:
        page = (APP_DIR / "assets" / "index.html").read_bytes()
    except OSError:
        return "NO INDEX.HTML"

    hostname = socket.gethostname()

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
                viewers["seen"] = time.monotonic()
                try:
                    since = int(parse_qs(url.query).get("since", ["0"])[0])
                except ValueError:
                    since = 0
                now, cols = history.since(since)
                self.reply_json({"boot_id": boot_id, "now_seq": now,
                                 "snap": latest["snap"], "history": cols})
            elif url.path == WEB_PATH + "api/history":
                now, cols = history.since(0)
                self.reply_json({"boot_id": boot_id, "now_seq": now,
                                 "history": cols})
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
    prober = Prober(values["probe_host"], values["dns_name"])
    settings.listen(lambda v: prober.configure(v["probe_host"], v["dns_name"]))
    # a new auto-off setting counts from now, not from the last touch
    changed_at = [None]  # set by any thread; a float store is atomic
    settings.listen(lambda _v: changed_at.__setitem__(0, time.monotonic()))
    prober.start()
    top = ProcTop()
    history = History()
    latest = {}
    viewers = {"seen": -VIEWER_TTL}

    def web_open():
        return time.monotonic() - viewers["seen"] < VIEWER_TTL

    details = Details(web_open)
    details.start()
    st = {"page": 0, "since": 0.0, "proc_age": PROC_EVERY, "procs": None,
          "max_ms": 0.0, "report": 0.0,
          "awake": True, "touched": time.monotonic()}
    boot_id = os.urandom(4).hex()

    def publish():
        snap = sampler.sample()
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
        history.append(snap)
        latest["snap"] = snap  # published: never mutated after this line

    publish()
    if port is not None:
        web_error = start_web(port, latest, history, boot_id, viewers, Tools(),
                              settings)
    if web_error:  # also shown on the WEB page
        print("dashboard: " + web_error, file=sys.stderr, flush=True)

    info = {"port": port, "web_error": web_error, "path": WEB_PATH}
    animate = slide.can(ctx.fb)

    def redraw():  # ctx.run flushes after every frame
        if not st["awake"]:
            return
        _, spark = history.since(history.seq - SPARK_POINTS)
        draw(ctx.fb, latest["snap"], st["page"], info, spark, settings.values)

    def page(step):
        old = bytes(ctx.fb._buf) if animate else None
        st["page"] = (st["page"] + step) % len(PAGES)
        redraw()
        if animate:
            slide.play(ctx.fb, old, bytes(ctx.fb._buf), step > 0)

    def touched():
        """Note a touch; True if it only woke the panel and is used up."""
        st["touched"] = time.monotonic()
        if st["awake"]:
            return False
        st["awake"] = True
        redraw()
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

    def screen_timer():
        at, changed_at[0] = changed_at[0], None
        if at is not None:
            st["touched"] = max(st["touched"], at)
        v = settings.values
        if st["awake"]:
            if v["screen_off"] and \
                    time.monotonic() - st["touched"] >= v["screen_off_s"]:
                st["awake"] = False
                ctx.fb.clear(0)  # stays black: redraw() is skipped while dark
        elif not v["screen_off"]:  # auto off was disabled on the web page
            st["awake"] = True
            st["touched"] = time.monotonic()
            redraw()

    def tick(dt):
        screen_timer()
        st["since"] += dt
        if st["since"] < SAMPLE:
            return
        st["since"] = 0.0
        t0 = time.perf_counter()
        publish()
        redraw()
        if DEBUG:
            st["max_ms"] = max(st["max_ms"], (time.perf_counter() - t0) * 1000)
            st["report"] += SAMPLE
            if st["report"] >= 10:
                st["report"] = 0.0
                print("dashboard: max tick %.1f ms" % st["max_ms"],
                      file=sys.stderr, flush=True)

    redraw()
    ctx.run(tick, fps=10, on_tap=on_tap, on_swipe=on_swipe)


if __name__ == "__main__":
    main()
