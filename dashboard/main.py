#!/usr/bin/env python3
"""Device status: addresses, health, connectivity and processes.

Five pages on the panel (screen.py): tap the right half or swipe left/up for
the next page, the left half or swipe right/down for the previous one. While
the App is open it also serves a live page under WEB_PATH on DASHBOARD_WEB_PORT, backed by
"<WEB_PATH>api/status?since=<seq>" and "<WEB_PATH>api/history". Everything
stops with the App. Exit with the host's edge gesture.

Threading: the UI tick only does cheap /proc and /sys reads (collector.py)
and publishes each snapshot by replacing latest["snap"]; a snapshot is never
mutated after that, so web threads read it without a lock. The process list
is only sampled while someone looks at it (TOP page or an open web page).
"""

import errno
import ipaddress
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from appbase import DOWN, LEFT, RIGHT, UP, app

from collector import History, Prober, ProcTop, Sampler, tailscale_ip
from details import Details, Tools
from screen import PAGES, TOP_PAGE, draw

APP_DIR = Path(__file__).resolve().parent
WEB_PATH = "/apps/%s/" % APP_DIR.name
SPARK_POINTS = 120  # history handed to the panel's sparkline
SAMPLE = 1.0
PROC_EVERY = 2.0
VIEWER_TTL = 10.0  # a web page counts as open this long after its last poll
DEBUG = os.environ.get("DASHBOARD_DEBUG") == "1"


# -- web --------------------------------------------------------------------

def start_web(port, latest, history, boot_id, viewers, tools):
    """Serve the page and API in daemon threads; returns an error or ""."""
    try:
        page = (APP_DIR / "assets" / "index.html").read_bytes()
    except OSError:
        return "NO INDEX.HTML"

    class Handler(BaseHTTPRequestHandler):
        timeout = 5  # a stalled client must not pin a thread forever

        def do_GET(self):
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
            else:
                self.send_error(404)

        def do_POST(self):
            # diagnostic tools: {"tool": "ping"|"dns", "target": "..."}
            if urlsplit(self.path).path != WEB_PATH + "api/tool":
                self.send_error(404)
                return
            try:
                length = min(int(self.headers.get("Content-Length", 0)), 1024)
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                req = {}
            if not isinstance(req, dict):
                req = {}
            self.reply_json(tools.run(str(req.get("tool", "")),
                                      str(req.get("target", ""))))

        def redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def reply_json(self, obj):
            self.reply("application/json",
                       json.dumps(obj, separators=(",", ":")).encode())

        def reply(self, ctype, body):
            self.send_response(200)
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
    """Return (port or None, probe_host, dns_name, web_error, other_errors).

    Missing variables fall back to the app.json defaults; an empty probe host
    or DNS name disables that check.
    """
    web_error, errors = "", []
    try:
        port = int(env.get("DASHBOARD_WEB_PORT", "8080"))
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        port, web_error = None, "BAD WEB PORT"
    host = env.get("DASHBOARD_PROBE_HOST", "1.1.1.1").strip()
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            errors.append("BAD PROBE HOST %s: NOT AN IP" % host)
            host = ""
    dns = env.get("DASHBOARD_DNS_NAME", "www.sipeed.com").strip()
    return port, host, dns, web_error, errors


@app()
def main(ctx):
    port, probe_host, dns_name, web_error, errors = read_config(ctx.env)
    sampler = Sampler()
    prober = Prober(probe_host, dns_name)
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
          "max_ms": 0.0, "report": 0.0}
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
        watched = st["page"] == TOP_PAGE or web_open()
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
        web_error = start_web(port, latest, history, boot_id, viewers, Tools())
    # only web errors fit on the panel; everything is logged
    for err in [web_error] + errors:
        if err:
            print("dashboard: " + err, file=sys.stderr, flush=True)

    info = {"port": port, "web_error": web_error, "path": WEB_PATH}

    def redraw():  # ctx.run flushes after every frame
        _, spark = history.since(history.seq - SPARK_POINTS)
        draw(ctx.fb, latest["snap"], st["page"], info, spark)

    def page(step):
        st["page"] = (st["page"] + step) % len(PAGES)
        redraw()

    def on_tap(x, _y):
        page(-1 if x < ctx.width // 2 else 1)

    def on_swipe(kind, _x, _y):
        # content follows the finger: swiping left brings in the next page;
        # swipes from the side edges belong to the host's exit gesture
        if kind in (LEFT, UP):
            page(1)
        elif kind in (RIGHT, DOWN):
            page(-1)

    def tick(dt):
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
