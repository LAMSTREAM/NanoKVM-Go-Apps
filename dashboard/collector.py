"""Device status sampling for the Dashboard App (stdlib only, no appbase).

Parsers take file text so they can be tested on any OS; readers take a root
directory so tests can point them at a fake /proc and /sys. Anything slow
(network probes, DNS, the tailscale CLI) lives in Prober's background thread,
so Sampler.sample() stays cheap enough to run on the UI tick.
"""

import errno
import glob
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

HISTORY_LEN = 600
PROBE_PERIOD = 5.0
PROBE_STALE = 3 * PROBE_PERIOD  # idle longer than this: results start over
DNS_TIMEOUT = 3.0
TAILSCALE_REFRESH = 30.0
STREAMER_URL = "https://127.0.0.1/api/streamer/local"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
# virtual / overlay links are left out of the RX/TX totals to avoid counting
# the same bytes twice
VIRTUAL_PREFIXES = ("lo", "tailscale", "docker", "br", "veth")
SIOCGIFADDR = 0x8915
RTF_UP = 0x1
RTF_GATEWAY = 0x2


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


# -- parsers ----------------------------------------------------------------

def parse_cpu(text):
    """Map "cpu" (aggregate) and "cpuN" to (busy, total) jiffies."""
    times = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or not fields[0].startswith("cpu"):
            continue
        try:
            vals = [int(v) for v in fields[1:9]]  # guest is inside user/nice
        except ValueError:
            continue
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        times[fields[0]] = (sum(vals) - idle, sum(vals))
    return times


def cpu_percent(prev, cur):
    if prev is None or cur is None or cur[1] <= prev[1]:
        return None
    return round(100.0 * (cur[0] - prev[0]) / (cur[1] - prev[1]), 1)


def parse_meminfo(text):
    """Return {used, total, swap_used, swap_total} bytes, or None."""
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        try:
            info[key.strip()] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            continue
    total = info.get("MemTotal")
    if not total:
        return None
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    swap = info.get("SwapTotal", 0)
    return {"used": total - avail, "total": total,
            "swap_used": swap - info.get("SwapFree", swap), "swap_total": swap}


def parse_loadavg(text):
    try:
        return [float(v) for v in text.split()[:3]]
    except ValueError:
        return None


def parse_uptime(text):
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def parse_default_route(text):
    """Return (iface, gateway_ip or None) of the lowest-metric default route.

    /proc/net/route stores addresses as little-endian hex; a default route
    without RTF_GATEWAY (point-to-point links) has no gateway to probe.
    """
    best = None
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7 or parts[1] != "00000000":
            continue
        try:
            flags, gw, metric = int(parts[3], 16), int(parts[2], 16), int(parts[6])
        except ValueError:
            continue
        if not flags & RTF_UP:
            continue
        if best is None or metric < best[0]:
            ip = None
            if flags & RTF_GATEWAY and gw:
                ip = socket.inet_ntoa(struct.pack("<L", gw))
            best = (metric, parts[0], ip)
    return (best[1], best[2]) if best else ("", None)


def parse_wireless(text):
    """Map interface -> signal level in dBm from /proc/net/wireless."""
    levels = {}
    for line in text.splitlines()[2:]:
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        try:
            level = int(float(rest.split()[2].rstrip(".")))
        except (IndexError, ValueError):
            continue
        # some drivers report the level as an unsigned byte
        levels[name.strip()] = level - 256 if level > 0 else level
    return levels


def parse_iw_link(text):
    """{ssid, freq_mhz, signal_dbm, rx_mbps, tx_mbps} from `iw dev X link`.

    Returns None when the interface is not associated.
    """
    if not text.startswith("Connected"):
        return None

    def field(pattern, cast):
        m = re.search(pattern, text, re.M)
        return cast(m.group(1)) if m else None

    return {"ssid": field(r"^\s*SSID: (.*)$", str),
            "freq_mhz": field(r"freq: ([\d.]+)", lambda v: int(float(v))),
            "signal_dbm": field(r"signal: (-?\d+) dBm", int),
            "rx_mbps": field(r"rx bitrate: ([\d.]+) MBit/s", float),
            "tx_mbps": field(r"tx bitrate: ([\d.]+) MBit/s", float)}


def parse_streamer(obj):
    """Video-in state from NanoKVM-Server's /api/streamer/local JSON."""
    try:
        s = obj["result"]["streamer"]
    except (KeyError, TypeError):
        return None
    source = s.get("source") or {}
    res = source.get("resolution") or {}
    h264 = s.get("h264") or {}
    sinks = s.get("sinks") or {}
    width, height = res.get("width") or 0, res.get("height") or 0
    # "online" only means the capture pipeline is running, which happens
    # while someone watches the KVM page; a valid resolution means signal
    return {"signal": bool(width and height),
            "online": bool(source.get("online")),
            "resolution": "%dx%d" % (width, height) if width and height else None,
            "fps": source.get("captured_fps"),
            "h264_online": bool(h264.get("online")),
            "h264_fps": h264.get("fps"),
            "bitrate_kbps": h264.get("real_bitrate"),
            "watched": any((v or {}).get("has_clients") for v in sinks.values())}


def parse_ping_ms(text):
    m = re.search(r"time[=<]\s*([\d.]+)\s*ms", text)
    return round(float(m.group(1)), 1) if m else None


# -- readers ----------------------------------------------------------------

def millidegrees(path):
    try:
        return round(int(read(path)) / 1000.0, 1)
    except ValueError:
        return None


def temperatures(root="/"):
    """[{name, temp}] from thermal zones plus hwmon sensors not already seen.

    Thermal zones are often mirrored as hwmon devices of the same name, so
    names are compared with '-' and '_' treated alike.
    """
    sensors, seen = [], set()

    def add(name, temp):
        key = name.replace("-", "_").lower()
        if temp is not None and key not in seen:
            seen.add(key)
            sensors.append({"name": name, "temp": temp})

    for zone in sorted(glob.glob(os.path.join(root, "sys/class/thermal/thermal_zone*"))):
        add(read(os.path.join(zone, "type")) or os.path.basename(zone),
            millidegrees(os.path.join(zone, "temp")))
    for hw in sorted(glob.glob(os.path.join(root, "sys/class/hwmon/hwmon*"))):
        chip = read(os.path.join(hw, "name")) or os.path.basename(hw)
        inputs = sorted(glob.glob(os.path.join(hw, "temp*_input")))
        for path in inputs:
            label = read(path[:-len("input")] + "label")
            name = chip if len(inputs) == 1 and not label else \
                "%s %s" % (chip, label or os.path.basename(path)[:-6])
            add(name, millidegrees(path))
    return sensors


def cpu_temperature(sensors):
    """The CPU/SoC sensor if one is named so, else the hottest one."""
    for s in sensors:
        if re.search(r"cpu|soc", s["name"], re.I):
            return s["temp"]
    return max((s["temp"] for s in sensors), default=None)


def ipv4(name):
    try:
        import fcntl  # Linux only; keeps this module importable elsewhere
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            res = fcntl.ioctl(s.fileno(), SIOCGIFADDR,
                              struct.pack("256s", name[:15].encode()))
        return socket.inet_ntoa(res[20:24])
    except (ImportError, OSError):
        return ""


def in_tailscale_net(ip):
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_NET
    except ValueError:
        return False


def tailscale_ip(ifaces):
    """Tailscale address from a tailscale* interface, or None."""
    for i in ifaces:
        if i["name"].startswith("tailscale") and in_tailscale_net(i["ip"]):
            return i["ip"]
    return None


GADGET_FUNCTIONS = (("hid", "HID"), ("ncm", "Network"), ("ecm", "Network"),
                    ("rndis", "Network"), ("mass_storage", "Storage"),
                    ("uac", "Audio"), ("acm", "Serial"))


def usb_link(root="/"):
    """USB device-mode link to the target host: {state, speed, functions}."""
    udcs = sorted(glob.glob(os.path.join(root, "sys/class/udc/*")))
    if not udcs:
        return None
    functions = []
    for path in sorted(glob.glob(os.path.join(
            root, "sys/kernel/config/usb_gadget/*/functions/*"))):
        kind = os.path.basename(path).split(".")[0]
        functions.append(next((label for prefix, label in GADGET_FUNCTIONS
                               if kind.startswith(prefix)), kind))
    speed = read(os.path.join(udcs[0], "current_speed"))
    return {"state": read(os.path.join(udcs[0], "state"), "unknown"),
            "speed": None if speed in ("", "UNKNOWN") else speed,
            "functions": functions}


def list_ifaces(root="/"):
    try:
        return sorted(n for n in os.listdir(os.path.join(root, "sys/class/net"))
                      if n != "lo")
    except OSError:
        return []


def counter(path):
    try:
        return int(read(path, "0") or 0)
    except ValueError:
        return 0


class Sampler:
    """Turns successive counter reads into a JSON-ready snapshot."""

    def __init__(self, root="/"):
        self.root = root
        self.prev_cpu = {}
        self.prev_net = {}
        self.prev_t = time.monotonic()
        self._cpu()  # prime the delta so the first sample has a baseline

    def path(self, rel):
        return os.path.join(self.root, rel)

    def _cpu(self):
        """(total %, [per-core %]) since the previous call."""
        cur = parse_cpu(read(self.path("proc/stat")))
        prev, self.prev_cpu = self.prev_cpu, cur
        cores = sorted((k for k in cur if k != "cpu"), key=lambda k: int(k[3:]))
        return (cpu_percent(prev.get("cpu"), cur.get("cpu")),
                [cpu_percent(prev.get(k), cur[k]) for k in cores])

    def _freq_mhz(self):
        try:
            return int(read(self.path(
                "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"))) // 1000
        except ValueError:
            return None

    def _disk(self):
        try:
            du = shutil.disk_usage(self.root)
        except OSError:
            return None
        return {"used": du.used, "total": du.total}

    def sample(self):
        now = time.monotonic()
        dt = max(1e-3, now - self.prev_t)
        self.prev_t = now

        mem = parse_meminfo(read(self.path("proc/meminfo")))
        route_iface, gateway = parse_default_route(
            read(self.path("proc/net/route")))
        wifi = parse_wireless(read(self.path("proc/net/wireless")))

        ifaces = []
        seen = set()
        rx_total = tx_total = 0.0
        for name in list_ifaces(self.root):
            base = self.path("sys/class/net/%s/" % name)
            rx = counter(base + "statistics/rx_bytes")
            tx = counter(base + "statistics/tx_bytes")
            prx, ptx = self.prev_net.get(name, (rx, tx))
            self.prev_net[name] = (rx, tx)
            seen.add(name)
            rx_rate = max(0, rx - prx) / dt
            tx_rate = max(0, tx - ptx) / dt
            if not name.startswith(VIRTUAL_PREFIXES):
                rx_total += rx_rate
                tx_total += tx_rate
            ifaces.append({
                "name": name,
                "state": read(base + "operstate", "unknown"),
                "ip": ipv4(name),
                "mac": read(base + "address"),
                "rx_rate": round(rx_rate, 1),
                "tx_rate": round(tx_rate, 1),
                "wifi_dbm": wifi.get(name),
                "default": name == route_iface,
            })
        for gone in set(self.prev_net) - seen:
            del self.prev_net[gone]
        ifaces.sort(key=lambda i: not i["default"])  # default route first

        primary = next((i["ip"] for i in ifaces if i["default"] and i["ip"]),
                       next((i["ip"] for i in ifaces if i["ip"]
                             and not i["name"].startswith("tailscale")), ""))
        cpu, cores = self._cpu()
        temps = temperatures(self.root)
        return {
            "hostname": socket.gethostname(),
            "ip": primary,
            "gateway": gateway,
            "uptime": parse_uptime(read(self.path("proc/uptime"))),
            "load": parse_loadavg(read(self.path("proc/loadavg"))),
            "cpu": cpu,
            "cores": cores,
            "cpu_mhz": self._freq_mhz(),
            "mem": mem,
            "disk": self._disk(),
            "temp": cpu_temperature(temps),
            "temps": temps,
            "ifaces": ifaces,
            "usb": usb_link(self.root),
            "rx_rate": round(rx_total, 1),
            "tx_rate": round(tx_total, 1),
        }


# -- processes --------------------------------------------------------------

def parse_proc_stat(text):
    """(name, state, utime+stime ticks, rss pages) from /proc/<pid>/stat."""
    head, sep, rest = text.rpartition(")")  # comm may contain spaces and ')'
    if not sep:
        return None
    fields = rest.split()
    try:
        return (head.partition("(")[2], fields[0],
                int(fields[11]) + int(fields[12]), int(fields[21]))
    except (IndexError, ValueError):
        return None


try:
    PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):  # not available on Windows
    PAGE_SIZE = 4096


class ProcTop:
    """Top processes by CPU share since the previous sample, like `top`.

    Only the short process name is reported, never the command line, which
    can carry secrets and the web page has no password.
    """

    def __init__(self, root="/", limit=10):
        self.root = root
        self.limit = limit
        self.prev = {}
        self.prev_total = None

    def sample(self):
        total = parse_cpu(read(os.path.join(self.root, "proc/stat"))).get("cpu")
        total = total[1] if total else None
        span = total - self.prev_total if total and self.prev_total else None
        self.prev_total = total
        procs, ticks = [], {}
        try:
            pids = [p for p in os.listdir(os.path.join(self.root, "proc"))
                    if p.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            st = parse_proc_stat(read(os.path.join(self.root, "proc", pid, "stat")))
            if st is None:  # exited between listdir and read
                continue
            name, state, used, rss = st
            ticks[pid] = used
            cpu = None
            if span and pid in self.prev:
                cpu = round(100.0 * max(0, used - self.prev[pid]) / span, 1)
            procs.append({"pid": int(pid), "name": name, "state": state,
                          "cpu": cpu, "rss": rss * PAGE_SIZE})
        self.prev = ticks
        procs.sort(key=lambda p: (p["cpu"] or 0, p["rss"]), reverse=True)
        return procs[:self.limit]


# -- probes -----------------------------------------------------------------

def tcp_probe(host, port, timeout):
    """Return (outcome, ms): outcome is ok, refused, unreachable or timeout.

    A refused connection still proves the host answered.
    """
    t0 = time.perf_counter()
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        outcome = "ok"
    except ConnectionRefusedError:
        outcome = "refused"
    except socket.timeout:
        return "timeout", None
    except OSError as e:
        if e.errno in (errno.ECONNREFUSED, errno.ECONNRESET):
            outcome = "refused"
        else:
            return "unreachable", None
    return outcome, round((time.perf_counter() - t0) * 1000, 1)


def result(target, ok, ms=None, method="none"):
    return {"target": target, "ok": ok, "ms": ms, "method": method,
            "at": time.monotonic()}


class Prober:
    """Background connectivity checks; publishes by swapping self.result.

    Ownership: self.result is replaced under self._lock, by the prober thread
    after each round and by configure() when the settings change targets.
    self._dns is written under the lock by the DNS worker and configure().
    A round that started before a target change never publishes a result
    for the old target. Readers get a fresh dict from view(), never a live
    reference.
    """

    def __init__(self, probe_host, dns_name, root="/", active=lambda: True):
        self.active = active  # callable: is anyone looking? if not, sleep
        self._kick = threading.Event()  # start a round now
        self.probe_host = probe_host
        self.dns_name = dns_name
        self.root = root
        self.ping = shutil.which("ping")
        self.iw = shutil.which("iw")
        self.tailscale_cmd = shutil.which("tailscale")
        self.tailscale_cli = None
        self.wifi = {}  # iface -> parse_iw_link() result, replaced as a whole
        self.video = None  # parse_streamer() result, replaced as a whole
        self._ts_checked = -TAILSCALE_REFRESH
        # built once: create_default_context() would load the CA store on
        # every round, and the loopback server's certificate is self-signed
        self._ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE
        self._lock = threading.Lock()
        self._dns = result(dns_name or None, None, method="dns")
        self._dns_started = None  # monotonic start of the in-flight lookup
        self._dns_gen = 0  # bumped when the DNS name changes
        self.result = {
            "gateway": result(None, None),
            "internet": result(probe_host, None, method="tcp"),
            "dns": dict(self._dns),
        }

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def configure(self, probe_host, dns_name):
        """Switch targets (settings changed); their results start over as unknown."""
        with self._lock:
            res = dict(self.result)
            if probe_host != self.probe_host:
                self.probe_host = probe_host
                res["internet"] = result(probe_host or None, None, method="tcp")
            if dns_name != self.dns_name:
                self.dns_name = dns_name
                self._dns_gen += 1  # a lookup still running for the old name is ignored
                self._dns_started = None
                self._dns = result(dns_name or None, None, method="dns")
                res["dns"] = dict(self._dns)
            self.result = res

    def view(self):
        """Copy of the latest results with age_s instead of timestamps."""
        now = time.monotonic()
        out = {}
        for key, r in self.result.items():
            item = {k: v for k, v in r.items() if k != "at"}
            item["age_s"] = None if r["ok"] is None else round(now - r["at"], 1)
            out[key] = item
        return out

    def kick(self):
        """Run a round now instead of at the end of the current wait."""
        self._kick.set()

    def forget(self):
        """Drop every result: they are from before an idle spell."""
        with self._lock:
            self._dns = result(self.dns_name or None, None, method="dns")
            self.result = {
                "gateway": result(None, None),
                "internet": result(self.probe_host or None, None, method="tcp"),
                "dns": dict(self._dns),
            }
        self.video = None

    def _loop(self):
        idle_since = None
        while True:
            if not self.active():  # nobody looking: no probes at all
                if idle_since is None:
                    idle_since = time.monotonic()
                self._kick.wait(1.0)
                self._kick.clear()
                continue
            if idle_since is not None:
                if time.monotonic() - idle_since > PROBE_STALE:
                    self.forget()  # unknown until this round, not old news
                idle_since = None
            t0 = time.monotonic()
            try:
                self.run_once()
            except Exception as e:  # keep probing; one bad round must not end it
                print("dashboard: probe error %r" % e, file=sys.stderr,
                      flush=True)
            self._kick.wait(max(0.0, PROBE_PERIOD - (time.monotonic() - t0)))
            self._kick.clear()

    def run_once(self):
        _, gw = parse_default_route(
            read(os.path.join(self.root, "proc/net/route")))
        gateway = self.probe_gateway(gw)
        internet = self.probe_internet()
        self.refresh_tailscale()
        self.refresh_wifi()
        self.refresh_video()
        self.kick_dns()
        with self._lock:
            if internet["target"] != (self.probe_host or None):
                internet = self.result["internet"]  # target changed meanwhile
            self.result = {"gateway": gateway, "internet": internet,
                           "dns": dict(self._dns)}

    def probe_gateway(self, gw):
        if not gw:
            return result(None, None)
        outcome, ms = tcp_probe(gw, 80, 1.0)
        if outcome in ("ok", "refused"):
            return result(gw, True, ms, "tcp")
        if outcome == "unreachable":  # ARP failed: nobody answers there
            return result(gw, False, None, "tcp")
        if not self.ping:  # silently dropped TCP proves nothing
            return result(gw, None)
        try:
            p = subprocess.run([self.ping, "-c", "1", "-W", "1", gw],
                               capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return result(gw, None)
        ok = p.returncode == 0
        return result(gw, ok, parse_ping_ms(p.stdout) if ok else None, "ping")

    def probe_internet(self):
        host = self.probe_host  # read once: configure() may change it meanwhile
        if not host:
            return result(None, None)
        outcome, ms = tcp_probe(host, 443, 2.0)
        return result(host, outcome in ("ok", "refused"), ms, "tcp")

    def refresh_tailscale(self):
        # the interface is read on every sample; the CLI is only a fallback
        # for userspace-networking setups without a tailscale* interface
        if not self.tailscale_cmd or any(
                n.startswith("tailscale") for n in list_ifaces(self.root)):
            self.tailscale_cli = None
            return
        now = time.monotonic()
        if now - self._ts_checked < TAILSCALE_REFRESH:
            return
        self._ts_checked = now
        try:
            out = subprocess.run([self.tailscale_cmd, "ip", "-4"],
                                 capture_output=True, text=True,
                                 timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        self.tailscale_cli = next(
            (ln.strip() for ln in out.splitlines()
             if in_tailscale_net(ln.strip())), None)

    def refresh_wifi(self):
        # link details (and the signal on cfg80211-only kernels, which have
        # no /proc/net/wireless) come from `iw`
        if not self.iw:
            return
        links = {}
        for name in list_ifaces(self.root):
            if not os.path.isdir(os.path.join(self.root, "sys/class/net", name,
                                              "wireless")):
                continue
            try:
                out = subprocess.run([self.iw, "dev", name, "link"],
                                     capture_output=True, text=True,
                                     timeout=2).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            links[name] = parse_iw_link(out)
        self.wifi = links

    def refresh_video(self):
        """HDMI capture state from the local NanoKVM-Server (no login needed)."""
        try:
            with urllib.request.urlopen(STREAMER_URL, timeout=2,
                                        context=self._ssl) as r:
                self.video = parse_streamer(json.loads(r.read().decode()))
        except (OSError, ValueError):
            self.video = None

    def kick_dns(self):
        """Start a lookup unless one is in flight (single-flight)."""
        now = time.monotonic()
        with self._lock:
            name, gen = self.dns_name, self._dns_gen
            if not name:
                return
            if self._dns_started is not None:
                if now - self._dns_started > DNS_TIMEOUT \
                        and self._dns["ok"] is not False:
                    self._dns = result(name, False, method="dns")
                return
            self._dns_started = now
        threading.Thread(target=self._resolve, args=(now, name, gen),
                         daemon=True).start()

    def _resolve(self, started, name, gen):
        try:
            socket.getaddrinfo(name, None, socket.AF_INET)
            ok = True
        except (OSError, UnicodeError):
            ok = False
        elapsed = time.monotonic() - started
        with self._lock:
            if gen != self._dns_gen:  # name changed while this lookup ran
                return
            self._dns_started = None
            if elapsed > DNS_TIMEOUT:  # too slow counts as failed, even if it resolved
                ok = False
            self._dns = result(name, ok,
                               round(elapsed * 1000, 1) if ok else None, "dns")


# -- history ----------------------------------------------------------------

HISTORY_FIELDS = ("cpu", "mem", "temp", "rx", "tx")


def _round(v):
    return None if v is None else round(v, 1)


class History:
    """Ring buffer of one point per sample, indexed by a sequence number."""

    def __init__(self, maxlen=HISTORY_LEN):
        self._points = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.seq = 0

    def append(self, snap):
        mem = snap.get("mem")
        mem_pct = 100.0 * mem["used"] / mem["total"] if mem else None
        with self._lock:
            self.seq += 1
            self._points.append((self.seq, _round(snap.get("cpu")),
                                 _round(mem_pct), _round(snap.get("temp")),
                                 _round(snap.get("rx_rate")),
                                 _round(snap.get("tx_rate"))))
            return self.seq

    def gap(self, n):
        """n empty points for seconds that were not sampled (nobody looked),
        so charts show a break instead of joining across it."""
        with self._lock:
            for _ in range(min(n, self._points.maxlen)):
                self.seq += 1
                self._points.append((self.seq,) + (None,) * len(HISTORY_FIELDS))

    def since(self, seq, upto=None):
        """Columns of the points newer than seq (and not newer than upto)."""
        with self._lock:
            now = self.seq if upto is None else min(upto, self.seq)
            pts = [p for p in self._points if seq < p[0] <= now]
        cols = {"seq": [p[0] for p in pts]}
        for i, name in enumerate(HISTORY_FIELDS, 1):
            cols[name] = [p[i] for p in pts]
        return now, cols
