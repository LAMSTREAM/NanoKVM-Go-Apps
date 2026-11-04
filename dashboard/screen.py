"""Panel pages for the Dashboard App.

Pure drawing on an appbase FrameBuffer (no I/O, no flush): rounded cards,
status chips, a sparkline, and a seven-segment face for big numbers, which
reads much better on this panel than the built-in 8x8 font scaled up.
"""

from appbase import WHITE, Rect, rgb565

import qr

BG = rgb565(8, 10, 18)
CARD = rgb565(22, 27, 42)
GRID = rgb565(44, 50, 70)
DIM = rgb565(130, 138, 160)
ACCENT = rgb565(80, 160, 255)
OK = rgb565(40, 210, 120)
WARN = rgb565(255, 184, 48)
BAD = rgb565(255, 85, 85)
OK_BG = rgb565(14, 60, 38)
BAD_BG = rgb565(72, 20, 26)
QR_LIGHT = rgb565(240, 242, 246)

PAGES = ["HOME", "HOST", "WEB", "HEALTH", "NETWORK", "TOP", "SETTINGS"]
TOP_PAGE = PAGES.index("TOP")
SETTINGS_PAGE = PAGES.index("SETTINGS")

GLYPHS = set(" $%-./0123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def screen_text(s):
    """Uppercase s and replace characters the 8x8 font lacks with '-'."""
    return "".join(c if c in GLYPHS else "-" for c in s.upper())


def visible_rect(fb):
    if fb.rotate == 90:
        return Rect(0, 0, fb.width, fb.height - 14)
    if fb.rotate == 270:
        return Rect(0, 14, fb.width, fb.height - 14)
    if fb.rotate == 180:
        return Rect(0, 0, fb.width - 14, fb.height)
    return Rect(14, 0, fb.width - 14, fb.height)


# -- formatting -------------------------------------------------------------

def human(n, suffix=""):
    for unit in ("", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return ("%d%s%s" if unit == "" else "%.1f%s%s") % (n, unit, suffix)
        n /= 1024.0
    return ""


def uptime_text(sec):
    if sec is None:
        return "--"
    d, sec = divmod(int(sec), 86400)
    h, sec = divmod(sec, 3600)
    if d:
        return "%dD %02d:%02d" % (d, h, sec // 60)
    return "%02d:%02d" % (h, sec // 60)


def level(value, warn, bad):
    if value is None:
        return DIM
    return BAD if value >= bad else WARN if value >= warn else OK


def usage_pct(stat):
    return 100.0 * stat["used"] / stat["total"] if stat else None


# -- seven-segment numbers --------------------------------------------------

class Face:
    def __init__(self, w, h, t, gap):
        self.w, self.h, self.t, self.gap = w, h, t, gap


SMALL = Face(7, 11, 2, 2)
MID = Face(9, 15, 2, 3)
LARGE = Face(13, 23, 3, 3)

SEGMENTS = {"0": "abcdef", "2": "abged", "3": "abgcd",
            "4": "fgbc", "5": "afgcd", "6": "afgedc", "7": "abc",
            "8": "abcdefg", "9": "abcdfg", "-": "g"}


def num_width(text, face):
    w = 0
    for ch in text:
        w += (face.t if ch in ".:1" else face.w) + face.gap
    return max(0, w - face.gap)


def draw_num(fb, x, y, text, color, face):
    """Draw digits, '.', ':' and '-'; returns the x after the text."""
    w, h, t = face.w, face.h, face.t
    mid = y + (h - t) // 2
    bottom = y + h - t
    for ch in text:
        if ch == ".":
            fb.fill_rect(x, bottom, t, t, color)
            x += t + face.gap
            continue
        if ch == ":":
            fb.fill_rect(x, y + h // 3 - t // 2, t, t, color)
            fb.fill_rect(x, y + 2 * h // 3 - t // 2, t, t, color)
            x += t + face.gap
            continue
        if ch == "1":  # narrow cell, otherwise "31" reads as "3 1"
            fb.fill_rect(x, y + t, t, mid - y - t, color)
            fb.fill_rect(x, mid + t, t, bottom - mid - t, color)
            x += t + face.gap
            continue
        segs = SEGMENTS.get(ch, "")
        # corners stay empty, which gives the segments bevelled ends
        rects = {"a": (x + t, y, w - 2 * t, t),
                 "g": (x + t, mid, w - 2 * t, t),
                 "d": (x + t, bottom, w - 2 * t, t),
                 "f": (x, y + t, t, mid - y - t),
                 "b": (x + w - t, y + t, t, mid - y - t),
                 "e": (x, mid + t, t, bottom - mid - t),
                 "c": (x + w - t, mid + t, t, bottom - mid - t)}
        for s in segs:
            fb.fill_rect(*rects[s], color)
        x += w + face.gap
    return x - face.gap


def value(fb, x, y, number, unit, color, face):
    """Big number with its unit on the baseline (2x for the large face)."""
    end = draw_num(fb, x, y, number, color, face)
    if unit:
        scale = 2 if face is LARGE else 1
        fb.draw_text(end + 3, y + face.h - 8 * scale, unit, DIM, scale)


# -- primitives -------------------------------------------------------------

def card(fb, x, y, w, h, color=CARD):
    fb.fill_rect(x, y, w, h, color)
    for cx, cy, dx in ((x, y, 1), (x + w - 1, y, -1),
                       (x, y + h - 1, 1), (x + w - 1, y + h - 1, -1)):
        dy = 1 if cy == y else -1  # cut 3 pixels per corner
        fb.fill_rect(cx, cy, 1, 1, BG)
        fb.fill_rect(cx + dx, cy, 1, 1, BG)
        fb.fill_rect(cx, cy + dy, 1, 1, BG)


def bar(fb, x, y, w, pct, color, h=5, track=GRID):
    fb.fill_rect(x, y, w, h, track)
    if pct is not None and pct > 0:
        fb.fill_rect(x, y, max(2, int(w * min(100.0, pct) / 100)), h, color)


def text(fb, x, y, s, color, scale=2):
    fb.draw_text(x, y, s, color, scale)


def text_right(fb, right, y, s, color, scale=1):
    fb.draw_text(right - fb.text_width(s, scale), y, s, color, scale)


def fit(fb, s, max_w, scale=2):
    """Largest scale <= scale at which s fits max_w."""
    while scale > 1 and fb.text_width(s, scale) > max_w:
        scale -= 1
    return scale


def chip(fb, x, y, w, label, ok):
    fg, bg = (DIM, GRID) if ok is None else (OK, OK_BG) if ok else (BAD, BAD_BG)
    card(fb, x, y, w, 26, bg)
    scale = fit(fb, label, w - 18)
    tw = 10 + fb.text_width(label, scale)
    lx = x + (w - tw) // 2
    fb.fill_rect(lx, y + 10, 6, 6, fg)
    fb.draw_text(lx + 10, y + 13 - 4 * scale, label, fg, scale)


def sparkline(fb, x, y, w, h, values, top, color):
    """Last w//2 values as 2px columns, scaled to top."""
    values = values[-(w // 2):]
    fb.fill_rect(x, y + h - 1, w, 1, GRID)
    cx = x + w - 2 * len(values)
    for v in values:
        if v is not None:
            vh = max(1, min(h, int(round(v / top * h))))
            fb.fill_rect(cx, y + h - vh, 2, vh, GRID)
            fb.fill_rect(cx, y + h - vh, 2, 2, color)
        cx += 2


def probe_color(p):
    return DIM if p["ok"] is None else OK if p["ok"] else BAD


# -- pages ------------------------------------------------------------------
# Main information is drawn at 2x or with the seven-segment face; the 1x
# font is only for secondary detail.

class Box:
    """Content area of a page, between the title and the page indicator."""

    def __init__(self, area):
        self.x = area.x + 8
        self.w = area.w - 16
        self.top = area.y + 32
        self.bottom = area.y + area.h - 18
        self.compact = area.h < 260  # landscape: tighter, no footer lines


def address_card(fb, b, y, label, ip, color, note=""):
    card(fb, b.x, y, b.w, 58)
    text(fb, b.x + 8, y + 7, label, DIM)
    if note:
        text_right(fb, b.x + b.w - 8, y + 11, note, DIM)
    if ip:
        face = LARGE if num_width(ip, LARGE) <= b.w - 16 else MID
        draw_num(fb, b.x + 8, y + 29, ip, color, face)
    else:
        text(fb, b.x + 8, y + 32, "OFFLINE", DIM)


def page_home(fb, b, snap):
    default = next((i for i in snap["ifaces"] if i["default"]), None)
    note = ""
    if default:
        note = screen_text(default["name"])
        if default["wifi_dbm"] is not None:
            note += " %dDBM" % default["wifi_dbm"]
    y = b.top
    address_card(fb, b, y, "LAN", snap["ip"], WHITE, note)
    y += 64
    address_card(fb, b, y, "TAILSCALE", snap["tailscale_ip"], ACCENT)
    y += 64
    cw = (b.w - 12) // 3
    for i, (key, label) in enumerate((("gateway", "GW"), ("internet", "NET"),
                                      ("dns", "DNS"))):
        chip(fb, b.x + i * (cw + 6), y, cw, label, snap["probes"][key]["ok"])
    y += 32
    # target host: HDMI input and the USB link that carries keyboard/mouse
    video, usb = snap.get("video"), snap.get("usb")
    hw = (b.w - 6) // 2
    chip(fb, b.x, y, hw, "HDMI", video["signal"] if video else None)
    chip(fb, b.x + hw + 6, y, hw, "USB",
         usb["state"] == "configured" if usb else None)
    y += 34
    if not b.compact:
        host = screen_text(snap["hostname"])
        text(fb, b.x + 2, y, host, WHITE, fit(fb, host, b.w - 4))


def page_host(fb, b, snap):
    """Target host: HDMI input mode and the USB link to it."""
    video, usb = snap.get("video"), snap.get("usb")
    y = b.top
    card(fb, b.x, y, b.w, 104)
    text(fb, b.x + 8, y + 7, "HDMI", DIM)
    if not video or not video["signal"]:
        text_right(fb, b.x + b.w - 8, y + 11, "NO SIGNAL" if video else "UNKNOWN",
                   BAD if video else DIM)
        text(fb, b.x + 8, y + 44, "NO INPUT" if video else "--", DIM)
    else:
        live = video["online"]
        text_right(fb, b.x + b.w - 8, y + 11, "LIVE" if live else "IDLE",
                   OK if live else DIM)
        w, h = video["resolution"].split("x")
        x = draw_num(fb, b.x + 8, y + 32, w, WHITE, LARGE)
        text(fb, x + 5, y + 39, "X", DIM)
        draw_num(fb, x + 26, y + 32, h, WHITE, LARGE)
        if video["fps"]:
            value(fb, b.x + 8, y + 70, "%d" % video["fps"], "FPS", ACCENT, MID)
        if video["watched"]:
            text_right(fb, b.x + b.w - 8, y + 76, "VIEWER ON", WARN)
    y += 110

    card(fb, b.x, y, b.w, 62)
    text(fb, b.x + 8, y + 7, "USB", DIM)
    if usb:
        ok = usb["state"] == "configured"
        if usb["speed"]:
            text_right(fb, b.x + b.w - 8, y + 11, screen_text(usb["speed"]), DIM)
        state = "CONNECTED" if ok else screen_text(usb["state"])
        text(fb, b.x + 8, y + 32, state, OK if ok else BAD,
             fit(fb, state, b.w - 16))
    else:
        text(fb, b.x + 8, y + 32, "--", DIM)


_qr_cache = {}


def page_web(fb, b, snap, info):
    if info["web_error"] or not snap["ip"]:
        card(fb, b.x, b.top, b.w, 70)
        text(fb, b.x + 8, b.top + 10,
             "WEB OFF" if info["web_error"] else "NO NETWORK", BAD)
        detail = info["web_error"] or "CONNECT TO SCAN"
        fb.draw_text(b.x + 8, b.top + 40, detail[:26], DIM, 1)
        return
    host = "%s:%d" % (snap["ip"], info["port"])
    url = "http://%s%s" % (host, info["path"])
    if url not in _qr_cache:
        _qr_cache.clear()
        _qr_cache[url] = qr.encode(url)
    matrix = _qr_cache[url]

    n = len(matrix) + 4  # 2-module quiet zone on each side
    module = max(2, min(b.w, b.bottom - b.top - 24) // n)
    size = n * module
    qx = b.x + (b.w - size) // 2
    qy = b.top
    fb.fill_rect(qx, qy, size, size, QR_LIGHT)
    for r, row in enumerate(matrix):
        c = 0
        while c < len(row):  # one rect per run of dark modules
            if row[c]:
                start = c
                while c < len(row) and row[c]:
                    c += 1
                fb.fill_rect(qx + (start + 2) * module, qy + (r + 2) * module,
                             (c - start) * module, module, BG)
            else:
                c += 1
    face = MID if num_width(host, MID) <= b.w else SMALL
    draw_num(fb, b.x + (b.w - num_width(host, face)) // 2, qy + size + 7,
             host, WHITE, face)


def page_health(fb, b, snap, history):
    y = b.top
    small = 54 if b.compact else 58
    card(fb, b.x, y, b.w, 70)
    text(fb, b.x + 8, y + 7, "CPU", DIM)
    detail = []
    if len(snap["cores"]) > 1:
        detail.append("%dX" % len(snap["cores"]))
    if snap["cpu_mhz"]:
        detail.append("%dMHZ" % snap["cpu_mhz"])
    text_right(fb, b.x + b.w - 8, y + 11, " ".join(detail), DIM)
    cpu = snap["cpu"]
    if cpu is None:
        text(fb, b.x + 8, y + 36, "--", DIM)
    else:
        value(fb, b.x + 8, y + 34, "%d" % round(cpu), "%", WHITE, LARGE)
    sw = b.w // 2
    sparkline(fb, b.x + b.w - 8 - sw, y + 28, sw, 34, history.get("cpu", []),
              100.0, level(cpu, 70, 90) if cpu is not None else ACCENT)
    y += 76

    mem = snap["mem"]
    pct = usage_pct(mem)
    card(fb, b.x, y, b.w, small)
    text(fb, b.x + 8, y + 7, "MEM", DIM)
    if mem:
        text_right(fb, b.x + b.w - 8, y + 11,
                   "%s/%s" % (human(mem["used"]), human(mem["total"])), DIM)
    if pct is not None:
        value(fb, b.x + 8, y + small - 23, "%d" % round(pct), "%", WHITE, MID)
    bar(fb, b.x + 78, y + small - 15, b.w - 86, pct, level(pct, 75, 90))
    y += small + 6

    cw = (b.w - 6) // 2
    disk = usage_pct(snap["disk"])
    temp = snap["temp"]
    tiles = (("DISK", disk, "%d", "%", level(disk, 80, 95)),
             ("TEMP", temp, "%.1f", "C", level(temp, 70, 85)))  # bar: 0-100 C
    for i, (label, v, fmt, unit, color) in enumerate(tiles):
        tx = b.x + i * (cw + 6)
        card(fb, tx, y, cw, small)
        text(fb, tx + 8, y + 7, label, DIM)
        if v is None:
            text(fb, tx + 8, y + small - 25, "--", DIM)
        else:
            value(fb, tx + 8, y + small - 26, fmt % v, unit, WHITE, MID)
        bar(fb, tx + 8, y + small - 8, cw - 16, v, color, h=3)
    y += small + 6

    if not b.compact:
        text(fb, b.x + 2, y + 2, "UP " + uptime_text(snap["uptime"]), DIM)


def page_network(fb, b, snap):
    y = b.top
    for key, name in (("gateway", "GW"), ("internet", "NET"), ("dns", "DNS")):
        p = snap["probes"][key]
        color = probe_color(p)
        card(fb, b.x, y, b.w, 38)
        fb.fill_rect(b.x, y + 5, 3, 28, color)
        text(fb, b.x + 10, y + 5, name, WHITE)
        fb.draw_text(b.x + 10, y + 25, screen_text(p["target"] or "OFF")[:15], DIM, 1)
        if p["ok"] and p["ms"] is not None:
            ms = "%d" % round(p["ms"])
            right = b.x + b.w - 8 - fb.text_width("MS", 1) - 3
            draw_num(fb, right - num_width(ms, MID), y + 12, ms, color, MID)
            text_right(fb, b.x + b.w - 8, y + 19, "MS", DIM)
        else:
            status = "--" if p["ok"] is None else "OK" if p["ok"] else "FAIL"
            text_right(fb, b.x + b.w - 8, y + 11, status, color, 2)
        y += 42

    shown = [i for i in snap["ifaces"] if i["ip"] or i["state"] == "up"]
    for n, i in enumerate(shown):
        if y + 42 + (12 if n + 1 < len(shown) else 0) > b.bottom:
            fb.draw_text(b.x + 2, y + 2,
                         "%d MORE ON THE WEB PAGE" % (len(shown) - n), DIM, 1)
            return
        up = i["state"] == "up" or (i["state"] == "unknown" and i["ip"])
        fb.fill_rect(b.x + 2, y + 5, 7, 7, OK if up else DIM)
        text(fb, b.x + 14, y + 1, screen_text(i["name"])[:9],
             ACCENT if i["default"] else WHITE)
        if i["wifi_dbm"] is not None:
            text_right(fb, b.x + b.w - 2, y + 5, "%dDBM" % i["wifi_dbm"],
                       level(-i["wifi_dbm"], 67, 75))
        if i["ip"]:
            draw_num(fb, b.x + 14, y + 21, i["ip"], WHITE, MID)
        else:
            fb.draw_text(b.x + 14, y + 24, "NO IPV4", DIM, 1)
        fb.fill_rect(b.x + 2, y + 39, b.w - 4, 1, GRID)
        y += 42
    if not shown:
        text(fb, b.x + 2, y + 2, "NO LINKS", DIM)


def page_top(fb, b, snap):
    y = b.top
    procs = snap["procs"]
    if not procs or procs[0]["cpu"] is None:
        card(fb, b.x, y, b.w, 50)
        text(fb, b.x + 8, y + 17, "MEASURING", DIM)
        return
    scale = max(5.0, procs[0]["cpu"])  # bars relative to the busiest process
    for p in procs:
        if y + 24 > b.bottom:
            break
        pct = "%.1f%%" % p["cpu"] if p["cpu"] < 10 else "%d%%" % round(p["cpu"])
        color = level(p["cpu"], 30, 60)
        name_w = b.w - 4 - fb.text_width(pct, 2) - 12
        text(fb, b.x + 2, y, screen_text(p["name"])[:name_w // 16], WHITE)
        text_right(fb, b.x + b.w - 2, y, pct, color, 2)
        bar(fb, b.x + 2, y + 18, b.w - 4, 100.0 * p["cpu"] / scale, color, h=3)
        y += 26


# Panel settings: one row per setting; tapping a row steps to the next value.
# Probe targets need typing, so they are only on the web page.

def timeout_text(sec):
    return "%dS" % sec if sec < 60 else "%d MIN" % (sec // 60)


def settings_rows(fb):
    """[(key, Rect)] of the rows on the SETTINGS page, for drawing and taps."""
    b = Box(visible_rect(fb))
    h = 44 if b.compact else 52
    return [(key, Rect(b.x, b.top + i * (h + 6), b.w, h))
            for i, key in enumerate(("paging", "screen_off", "screen_off_s"))]


def page_settings(fb, b, values):
    rows = settings_rows(fb)
    shown = {
        "paging": ("PAGING", values["paging"].upper(), True),
        "screen_off": ("AUTO OFF", "ON" if values["screen_off"] else "OFF",
                       True),
        "screen_off_s": ("AFTER", timeout_text(values["screen_off_s"]),
                         values["screen_off"]),
    }
    for key, r in rows:
        label, value, active = shown[key]
        card(fb, r.x, r.y, r.w, r.h)
        ty = r.y + (r.h - 16) // 2
        text(fb, r.x + 10, ty, label, DIM if active else GRID)
        text_right(fb, r.x + r.w - 10, ty, value,
                   ACCENT if active else GRID, 2)
    y = rows[-1][1].y + rows[-1][1].h + 10
    if y + 20 <= b.bottom:
        fb.draw_text(b.x + 2, y, "TAP A ROW TO CHANGE IT", DIM, 1)
        fb.draw_text(b.x + 2, y + 12, "PROBE TARGETS: WEB PAGE", DIM, 1)


def page_key(snap, page, info, values):
    """Everything a page shows, or None if it changes on every sample.

    Equal keys draw equal pixels, so the panel can skip the redraw. Keep each
    entry in step with what its page_* function reads.
    """
    name = PAGES[page]
    probes = snap["probes"]
    if name == "HOME":
        default = next((i for i in snap["ifaces"] if i["default"]), None)
        video, usb = snap.get("video"), snap.get("usb")
        return (name, snap["ip"], snap["tailscale_ip"], snap["hostname"],
                default and (default["name"], default["wifi_dbm"]),
                tuple(probes[k]["ok"] for k in ("gateway", "internet", "dns")),
                video and video["signal"], usb and usb["state"])
    if name == "HOST":
        video, usb = snap.get("video"), snap.get("usb")
        return (name,
                video and (video["signal"], video["online"],
                           video["resolution"], video["fps"], video["watched"]),
                usb and (usb["state"], usb["speed"]))
    if name == "WEB":
        return (name, snap["ip"], info["port"], info["web_error"])
    if name == "NETWORK":
        return (name,
                tuple((p["target"], p["ok"],
                       None if p["ms"] is None else round(p["ms"]))
                      for p in (probes[k] for k in ("gateway", "internet", "dns"))),
                tuple((i["name"], i["state"], i["ip"], i["wifi_dbm"], i["default"])
                      for i in snap["ifaces"]))
    if name == "TOP":
        procs = snap["procs"]
        return (name, procs and tuple((p["name"], p["cpu"]) for p in procs))
    if name == "SETTINGS":
        return (name, values["paging"], values["screen_off"],
                values["screen_off_s"])
    return None  # HEALTH: CPU and its sparkline move every second


def draw(fb, snap, page, info, history, values):
    """Render one page of snap into the back buffer."""
    area = visible_rect(fb)
    b = Box(area)
    fb.clear(BG)
    # centred title: the panel's rounded corners clip anything near them
    fb.text_center(area.cx, area.y + 9, PAGES[page], WHITE, 2)
    name = PAGES[page]
    if name == "HOME":
        page_home(fb, b, snap)
    elif name == "HOST":
        page_host(fb, b, snap)
    elif name == "WEB":
        page_web(fb, b, snap, info)
    elif name == "HEALTH":
        page_health(fb, b, snap, history)
    elif name == "NETWORK":
        page_network(fb, b, snap)
    elif name == "TOP":
        page_top(fb, b, snap)
    else:
        page_settings(fb, b, values)

    # page indicator: active page is a wider accent pill
    widths = [12 if i == page else 4 for i in range(len(PAGES))]
    x = area.cx - (sum(widths) + 5 * (len(PAGES) - 1)) // 2
    for i, w in enumerate(widths):
        fb.fill_rect(x, area.y + area.h - 10, w, 4, ACCENT if i == page else GRID)
        x += w + 5
