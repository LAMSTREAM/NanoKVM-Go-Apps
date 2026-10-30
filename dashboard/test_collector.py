import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collector  # noqa: E402
import details  # noqa: E402
import qr  # noqa: E402

ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
eth0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0
eth0\t0000000A\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   54.  -56.  -256        0      0      0      0      0        0
 wlan1: 0000   40.  190.  0        0      0      0      0      0        0
"""


def make_root(tmp):
    root = Path(tmp)
    files = {
        "proc/stat": "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3 4\n",
        "proc/meminfo": "MemTotal:  1000 kB\nMemAvailable: 400 kB\nbroken\n",
        "proc/uptime": "3725.5 100.0\n",
        "proc/loadavg": "0.10 0.20 0.30 1/80 123\n",
        "proc/net/route": ROUTE,
        "sys/class/net/eth0/operstate": "up\n",
        "sys/class/net/eth0/address": "aa:bb:cc:dd:ee:ff\n",
        "sys/class/net/eth0/statistics/rx_bytes": "1000\n",
        "sys/class/net/eth0/statistics/tx_bytes": "500\n",
        "sys/class/net/lo/operstate": "unknown\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class ParserTest(unittest.TestCase):
    def test_cpu_aggregate_and_cores(self):
        text = "cpu 10 0 10 70 10 0 0 0\ncpu0 5 0 5 35 5\ncpu1 5 0 5 35 5\nintr 1 2 3\n"
        times = collector.parse_cpu(text)
        self.assertEqual(times["cpu"], (20, 100))
        self.assertEqual(sorted(times), ["cpu", "cpu0", "cpu1"])
        self.assertEqual(collector.parse_cpu("intr 1 2 3"), {})
        self.assertEqual(collector.cpu_percent((20, 100), (70, 200)), 50.0)
        self.assertIsNone(collector.cpu_percent(None, (70, 200)))

    def test_meminfo_skips_malformed_lines(self):
        self.assertEqual(
            collector.parse_meminfo("MemTotal: 1000 kB\nbad\nMemAvailable: 250 kB\n"
                                    "SwapTotal: 100 kB\nSwapFree: 40 kB\n"),
            {"used": 750 * 1024, "total": 1000 * 1024,
             "swap_used": 60 * 1024, "swap_total": 100 * 1024})
        self.assertIsNone(collector.parse_meminfo("garbage"))

    def test_proc_stat(self):
        line = "123 (kvm (ui) x) S 1 1 1 0 -1 4194560 100 0 0 0 250 50 0 0 20 0 3 0 99 1000 77 4294967295"
        self.assertEqual(collector.parse_proc_stat(line), ("kvm (ui) x", "S", 300, 77))
        self.assertIsNone(collector.parse_proc_stat("garbage"))

    def test_default_route_picks_lowest_metric_and_decodes_gateway(self):
        self.assertEqual(collector.parse_default_route(ROUTE), ("eth0", "10.0.0.1"))

    def test_default_route_without_gateway(self):
        text = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\n" \
               "ppp0\t00000000\t00000000\t0001\t0\t0\t0\n"
        self.assertEqual(collector.parse_default_route(text), ("ppp0", None))
        self.assertEqual(collector.parse_default_route(""), ("", None))

    def test_wireless_signed_and_unsigned_levels(self):
        self.assertEqual(collector.parse_wireless(WIRELESS), {"wlan0": -56, "wlan1": -66})
        self.assertEqual(collector.parse_wireless(""), {})

    def test_iw_link(self):
        out = ("Connected to 78:45:58:f8:68:c9 (on wlan0)\n\tSSID: lab net\n"
               "\tfreq: 5200.0\n\tsignal: -58 dBm\n"
               "\trx bitrate: 172.0 MBit/s 40MHz HE-MCS 7\n"
               "\ttx bitrate: 206.5 MBit/s 40MHz HE-MCS 8\n")
        self.assertEqual(collector.parse_iw_link(out),
                         {"ssid": "lab net", "freq_mhz": 5200, "signal_dbm": -58,
                          "rx_mbps": 172.0, "tx_mbps": 206.5})
        self.assertIsNone(collector.parse_iw_link("Not connected."))

    def test_streamer(self):
        obj = {"ok": True, "result": {"streamer": {
            "source": {"online": True, "captured_fps": 30,
                       "resolution": {"width": 1920, "height": 1080}},
            "h264": {"online": True, "fps": 30, "real_bitrate": 8000},
            "sinks": {"h264": {"has_clients": True}, "jpeg": {"has_clients": False}}}}}
        self.assertEqual(collector.parse_streamer(obj),
                         {"signal": True, "online": True, "resolution": "1920x1080", "fps": 30,
                          "h264_online": True, "h264_fps": 30, "bitrate_kbps": 8000,
                          "watched": True})
        idle = {"result": {"streamer": {"source": {"online": False,
                                                    "resolution": {"width": 0}}}}}
        self.assertEqual(collector.parse_streamer(idle)["resolution"], None)
        self.assertFalse(collector.parse_streamer(idle)["signal"])
        # idle pipeline with a monitor attached: signal without capture
        idle_signal = {"result": {"streamer": {"source": {
            "online": False, "captured_fps": 60,
            "resolution": {"width": 3440, "height": 1440}}}}}
        v = collector.parse_streamer(idle_signal)
        self.assertEqual((v["signal"], v["online"], v["resolution"], v["fps"]),
                         (True, False, "3440x1440", 60))
        self.assertIsNone(collector.parse_streamer({"error": "x"}))

    def test_ping_ms(self):
        self.assertEqual(collector.parse_ping_ms("64 bytes: seq=0 ttl=64 time=1.234 ms"), 1.2)
        self.assertIsNone(collector.parse_ping_ms("no reply"))

    def test_tailscale_only_from_tailscale_interfaces(self):
        ifaces = [{"name": "eth0", "ip": "100.70.0.5"},
                  {"name": "tailscale0", "ip": "100.101.2.3"}]
        self.assertEqual(collector.tailscale_ip(ifaces), "100.101.2.3")
        self.assertIsNone(collector.tailscale_ip(ifaces[:1]))


class SamplerTest(unittest.TestCase):
    def test_missing_sources_are_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(collector.temperatures(tmp), [])
            snap = collector.Sampler(tmp).sample()
            self.assertEqual(collector.ProcTop(tmp).sample(), [])
        self.assertIsNone(snap["temp"])
        self.assertEqual(snap["cores"], [])
        self.assertIsNone(snap["mem"])
        self.assertIsNone(snap["cpu"])
        self.assertIsNone(snap["uptime"])
        self.assertEqual(snap["ifaces"], [])
        json.dumps(snap)

    def test_first_and_second_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            sampler = collector.Sampler(str(root))
            first = sampler.sample()
            (root / "proc/stat").write_text("cpu  150 0 50 850 50 0 0 0\n")
            (root / "sys/class/net/eth0/statistics/rx_bytes").write_text("3000\n")
            second = sampler.sample()
        self.assertEqual(first["mem"]["used"], 600 * 1024)
        self.assertEqual(first["mem"]["swap_total"], 0)
        self.assertEqual(first["load"], [0.1, 0.2, 0.3])
        self.assertEqual(first["uptime"], 3725.5)
        self.assertEqual(first["gateway"], "10.0.0.1")
        self.assertEqual([i["name"] for i in first["ifaces"]], ["eth0"])
        self.assertTrue(first["ifaces"][0]["default"])
        self.assertEqual(first["ifaces"][0]["rx_rate"], 0)
        self.assertEqual(second["cpu"], 50.0)
        self.assertGreater(second["ifaces"][0]["rx_rate"], 0)
        json.dumps(second)


class TemperatureTest(unittest.TestCase):
    def test_zones_and_hwmon_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "sys/class/thermal/thermal_zone0/type": "soc-thermal\n",
                "sys/class/thermal/thermal_zone0/temp": "41250\n",
                "sys/class/thermal/thermal_zone1/type": "ddr-thermal\n",
                "sys/class/thermal/thermal_zone1/temp": "45000\n",
                "sys/class/hwmon/hwmon0/name": "soc_thermal\n",  # mirror of zone0
                "sys/class/hwmon/hwmon0/temp1_input": "41250\n",
                "sys/class/hwmon/hwmon1/name": "wifi\n",
                "sys/class/hwmon/hwmon1/temp1_input": "52000\n",
            }
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            temps = collector.temperatures(tmp)
        self.assertEqual(temps, [{"name": "soc-thermal", "temp": 41.2},
                                 {"name": "ddr-thermal", "temp": 45.0},
                                 {"name": "wifi", "temp": 52.0}])
        self.assertEqual(collector.cpu_temperature(temps), 41.2)
        self.assertEqual(collector.cpu_temperature(temps[1:]), 52.0)
        self.assertIsNone(collector.cpu_temperature([]))


class ProcTopTest(unittest.TestCase):
    def test_cpu_share_between_samples(self):
        def stat(ticks):
            return "1 (busy) R 0 0 0 0 0 0 0 0 0 0 %d 0 0 0 0 0 1 0 0 0 10 0" % ticks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proc/1").mkdir(parents=True)
            (root / "proc/2").mkdir(parents=True)
            (root / "proc/stat").write_text("cpu 0 0 0 1000 0 0 0 0\n")
            (root / "proc/1/stat").write_text(stat(100))
            (root / "proc/2/stat").write_text("2 (idle) S" + " 0" * 25)
            top = collector.ProcTop(tmp)
            first = top.sample()
            (root / "proc/stat").write_text("cpu 0 0 0 1200 0 0 0 0\n")
            (root / "proc/1/stat").write_text(stat(150))
            second = top.sample()
        self.assertIsNone(first[0]["cpu"])
        self.assertEqual(second[0]["name"], "busy")
        self.assertEqual(second[0]["cpu"], 25.0)
        self.assertEqual(second[0]["rss"], 10 * collector.PAGE_SIZE)


class HistoryTest(unittest.TestCase):
    def snap(self, i):
        return {"cpu": i % 100 + 0.123, "mem": {"used": 3, "total": 4},
                "temp": 51.26, "rx_rate": 123456.78, "tx_rate": None}

    def test_capacity_and_delta(self):
        h = collector.History()
        for i in range(650):
            h.append(self.snap(i))
        now, cols = h.since(0)
        self.assertEqual(now, 650)
        self.assertEqual(len(cols["seq"]), 600)
        self.assertEqual(cols["seq"][0], 51)
        self.assertEqual(cols["mem"][0], 75.0)
        self.assertEqual(cols["cpu"][-1], 49.1)
        self.assertIsNone(cols["tx"][0])
        _, delta = h.since(648)
        self.assertEqual(delta["seq"], [649, 650])
        self.assertEqual(h.since(650)[1]["seq"], [])

    def test_full_history_json_under_100kb(self):
        h = collector.History()
        for i in range(600):
            h.append(self.snap(i))
        self.assertLess(len(json.dumps(h.since(0)[1])), 100 * 1024)


class ProberTest(unittest.TestCase):
    def test_dns_single_flight_discards_late_result(self):
        release = threading.Event()
        calls = []

        def slow_lookup(*_args):
            calls.append(1)
            release.wait(5)
            return []

        prober = collector.Prober("", "example.com")
        with patch.object(collector.socket, "getaddrinfo", slow_lookup), \
                patch.object(collector, "DNS_TIMEOUT", 0.05):
            prober.kick_dns()
            prober.kick_dns()  # still in flight: must not start another
            time.sleep(0.1)
            prober.kick_dns()  # past the timeout: marked failed
            self.assertEqual(len(calls), 1)
            self.assertIs(prober._dns["ok"], False)
            release.set()
            for _ in range(50):
                if prober._dns_started is None:
                    break
                time.sleep(0.01)
        self.assertIsNone(prober._dns_started)
        self.assertIs(prober._dns["ok"], False)  # late success still counts as failed

    def test_slow_dns_without_overlap_counts_as_failed(self):
        def slow_lookup(*_args):
            time.sleep(0.1)
            return []

        prober = collector.Prober("", "example.com")
        with patch.object(collector.socket, "getaddrinfo", slow_lookup), \
                patch.object(collector, "DNS_TIMEOUT", 0.05):
            prober.kick_dns()
            for _ in range(50):
                if prober._dns_started is None:
                    break
                time.sleep(0.01)
        self.assertIs(prober._dns["ok"], False)

    def test_fast_dns_succeeds(self):
        prober = collector.Prober("", "example.com")
        with patch.object(collector.socket, "getaddrinfo", lambda *_a: []):
            prober.kick_dns()
            for _ in range(50):
                if prober._dns_started is None:
                    break
                time.sleep(0.01)
        self.assertIs(prober._dns["ok"], True)
        self.assertIsNotNone(prober._dns["ms"])

    def test_gateway_unknown_without_ping(self):
        prober = collector.Prober("", "")
        prober.ping = None
        with patch.object(collector, "tcp_probe", return_value=("timeout", None)):
            r = prober.probe_gateway("192.0.2.1")
        self.assertIsNone(r["ok"])
        with patch.object(collector, "tcp_probe", return_value=("refused", 0.4)):
            r = prober.probe_gateway("192.0.2.1")
        self.assertEqual((r["ok"], r["ms"], r["method"]), (True, 0.4, "tcp"))

    def test_view_is_a_copy(self):
        prober = collector.Prober("192.0.2.1", "")
        view = prober.view()
        view["internet"]["ok"] = True
        self.assertIsNone(prober.result["internet"]["ok"])
        self.assertNotIn("at", view["internet"])


class UsbTest(unittest.TestCase):
    def test_gadget_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sys/class/udc/x.dwc3").mkdir(parents=True)
            (root / "sys/class/udc/x.dwc3/state").write_text("configured\n")
            (root / "sys/class/udc/x.dwc3/current_speed").write_text("high-speed\n")
            for fn in ("hid.GS0", "hid.GS1", "ncm.usb0"):
                (root / "sys/kernel/config/usb_gadget/g0/functions" / fn).mkdir(parents=True)
            self.assertEqual(collector.usb_link(tmp),
                             {"state": "configured", "speed": "high-speed",
                              "functions": ["HID", "HID", "Network"]})
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(collector.usb_link(tmp))


class DetailsTest(unittest.TestCase):
    def test_services(self):
        text = ("Id=nanokvm-go.service\nLoadState=loaded\nActiveState=active\n"
                "SubState=running\nNRestarts=2\n\n"
                "Id=chrony.service\nLoadState=not-found\nActiveState=inactive\n\n"
                "Id=tailscaled.service\nLoadState=loaded\nActiveState=failed\n"
                "SubState=failed\nNRestarts=\n")
        self.assertEqual(details.services(text), [
            {"unit": "nanokvm-go.service", "label": "NanoKVM Server",
             "active": "active", "sub": "running", "restarts": 2},
            {"unit": "tailscaled.service", "label": "Tailscale",
             "active": "failed", "sub": "failed", "restarts": None}])

    def test_tailscale_is_reduced_to_counts(self):
        obj = {"BackendState": "Running",
               "Self": {"TailscaleIPs": ["100.1.2.3", "fd7a::1"], "HostName": "kvm"},
               "Peer": {"a": {"Online": True, "HostName": "laptop"},
                        "b": {"Online": False, "HostName": "phone"}}}
        self.assertEqual(details.parse_tailscale(obj),
                         {"state": "Running", "ips": ["100.1.2.3"], "peers": 2,
                          "online": 1, "exit_node": False})
        self.assertNotIn("laptop", json.dumps(details.parse_tailscale(obj)))

    def test_ping_summary(self):
        busybox = ("3 packets transmitted, 2 packets received, 33% packet loss\n"
                   "round-trip min/avg/max = 1.1/2.25/3.4 ms\n")
        iputils = ("3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
                   "rtt min/avg/max/mdev = 0.4/0.5/0.6/0.1 ms\n")
        self.assertEqual(details.parse_ping_summary(busybox),
                         {"sent": 3, "received": 2, "loss": 33, "avg_ms": 2.2})
        self.assertEqual(details.parse_ping_summary(iputils)["avg_ms"], 0.5)
        self.assertIsNone(details.parse_ping_summary("ping: bad address"))

    def test_target_validation(self):
        for good in ("1.1.1.1", "fd7a:115c::53", "example.com", "kvm-01.local."):
            self.assertEqual(details.valid_target(good), good)
        for bad in ("", "-c 99", "--help", "a b", "x;rm -rf", "a" * 300, "1.1.1.1 -f"):
            self.assertIsNone(details.valid_target(bad), bad)

    def test_tools_single_flight_and_rate_limit(self):
        tools = details.Tools()
        with patch.object(details.socket, "getaddrinfo",
                          lambda *_a: [(0, 0, 0, "", ("93.184.216.34", 0))]):
            first = tools.run("dns", "example.com")
            second = tools.run("dns", "example.com")
        self.assertEqual(first["dns"]["addresses"], ["93.184.216.34"])
        self.assertIn("wait", second["error"])
        self.assertIn("error", tools.run("rm", "example.com"))
        self.assertIn("error", tools.run("ping", "-oops"))
        tools._lock.acquire()
        try:
            self.assertIn("still running", tools.run("dns", "example.com")["error"])
        finally:
            tools._lock.release()


class QrTest(unittest.TestCase):
    # zbar decoded every version 1-6 / mask combination during development;
    # these lock the structure so regressions show up without a decoder

    def test_codewords_hello(self):
        version, words = qr._codewords(b"HELLO")
        self.assertEqual(version, 1)
        self.assertEqual(words[:9], [64, 84, 132, 84, 196, 196, 240, 236, 17])
        self.assertEqual(len(words), 26)  # 19 data + 7 EC

    def test_version_by_length(self):
        url = "http://192.168.100.200:8080/apps/dashboard/"
        self.assertEqual(len(qr.encode(url)), 29)  # version 3
        self.assertEqual(len(qr.encode("x" * 134)), 41)  # version 6, two blocks
        with self.assertRaises(ValueError):
            qr.encode("x" * 135)

    def test_finder_patterns_and_timing(self):
        m = qr.encode("HELLO")
        n = len(m)
        for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):
            ring = [m[y0][x0 + i] for i in range(7)]
            self.assertEqual(ring, [True] * 7)
            self.assertFalse(m[y0 + 1][x0 + 1])
            self.assertTrue(m[y0 + 3][x0 + 3])
        self.assertEqual([m[6][x] for x in range(8, n - 8)],
                         [x % 2 == 0 for x in range(8, n - 8)])


if __name__ == "__main__":
    unittest.main()
