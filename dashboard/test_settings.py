import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collector  # noqa: E402
import settings  # noqa: E402
import slide  # noqa: E402


class ValidateTest(unittest.TestCase):
    def test_accepts_known_values(self):
        clean, errors = settings.validate({
            "paging": "tap", "screen_off": False, "screen_off_s": 300,
            "probe_host": " 9.9.9.9 ", "dns_name": "example.com"})
        self.assertEqual(errors, {})
        self.assertEqual(clean["probe_host"], "9.9.9.9")

    def test_empty_probe_targets_turn_checks_off(self):
        clean, errors = settings.validate({"probe_host": "", "dns_name": " "})
        self.assertEqual(errors, {})
        self.assertEqual(clean, {"probe_host": "", "dns_name": ""})

    def test_rejects_bad_values(self):
        _, errors = settings.validate({
            "paging": "drag", "screen_off": "yes", "screen_off_s": 45,
            "probe_host": "one.one.one.one", "dns_name": "-rf",
            "colour": "red"})
        self.assertEqual(set(errors), {"paging", "screen_off", "screen_off_s",
                                       "probe_host", "dns_name", "colour"})

    def test_bool_is_not_a_timeout(self):
        _, errors = settings.validate({"screen_off_s": True})
        self.assertIn("screen_off_s", errors)

    def test_rejects_non_object(self):
        _, errors = settings.validate(["paging"])
        self.assertTrue(errors)
        _, errors = settings.validate(None)
        self.assertTrue(errors)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "settings.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults_without_a_file(self):
        self.assertEqual(settings.Settings(self.path).values, settings.DEFAULTS)
        self.assertFalse(os.path.exists(self.path))

    def test_update_saves_and_reloads(self):
        s = settings.Settings(self.path)
        seen = []
        s.listen(seen.append)
        self.assertEqual(s.update({"paging": "tap", "dns_name": "example.org"}), {})
        self.assertEqual(seen[-1]["paging"], "tap")
        again = settings.Settings(self.path)
        self.assertEqual(again.values["paging"], "tap")
        self.assertEqual(again.values["dns_name"], "example.org")
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_invalid_update_changes_nothing(self):
        s = settings.Settings(self.path)
        before = s.values
        errors = s.update({"paging": "tap", "probe_host": "not-an-ip"})
        self.assertIn("probe_host", errors)
        self.assertIs(s.values, before)
        self.assertFalse(os.path.exists(self.path))

    def test_values_are_replaced_not_mutated(self):
        s = settings.Settings(self.path)
        before = s.values
        s.update({"screen_off": False})
        self.assertTrue(before["screen_off"])
        self.assertFalse(s.values["screen_off"])

    def test_corrupt_or_partly_bad_file_falls_back(self):
        Path(self.path).write_text("{not json", encoding="utf-8")
        self.assertEqual(settings.Settings(self.path).values, settings.DEFAULTS)
        Path(self.path).write_text(json.dumps(
            {"paging": "tap", "screen_off_s": 7, "extra": 1}), encoding="utf-8")
        values = settings.Settings(self.path).values
        self.assertEqual(values["paging"], "tap")
        self.assertEqual(values["screen_off_s"], settings.DEFAULTS["screen_off_s"])
        self.assertNotIn("extra", values)

    def test_save_failure_is_reported_and_not_applied(self):
        s = settings.Settings(os.path.join(self.dir.name, "missing", "s.json"))
        errors = s.update({"paging": "tap"})
        self.assertIn("", errors)
        self.assertEqual(s.values["paging"], "swipe")


class ProberConfigureTest(unittest.TestCase):
    def test_new_targets_start_unknown(self):
        p = collector.Prober("1.1.1.1", "example.com")
        p.result["internet"] = collector.result("1.1.1.1", True, 5.0, "tcp")
        p.configure("9.9.9.9", "")
        view = p.view()
        self.assertEqual(view["internet"]["target"], "9.9.9.9")
        self.assertIsNone(view["internet"]["ok"])
        self.assertIsNone(view["dns"]["target"])
        self.assertEqual((p.probe_host, p.dns_name), ("9.9.9.9", ""))

    def test_lookup_for_an_old_name_is_dropped(self):
        p = collector.Prober("", "old.example")
        p._dns_started = 0.0
        gen = p._dns_gen
        p.configure("", "new.example")
        self.assertIsNone(p._dns_started)  # a hung old lookup does not block the new name
        p._dns_started = 5.0  # pretend the new lookup is in flight
        p._resolve(0.0, "old.example", gen)
        self.assertEqual(p._dns["target"], "new.example")
        self.assertIsNone(p._dns["ok"])
        self.assertEqual(p._dns_started, 5.0)  # the new flight is untouched

    def test_round_for_an_old_internet_target_is_not_published(self):
        p = collector.Prober("1.1.1.1", "")
        p.probe_gateway = lambda gw: collector.result(None, None)
        for name in ("refresh_tailscale", "refresh_wifi", "refresh_video"):
            setattr(p, name, lambda: None)

        def probe():
            out = collector.result("1.1.1.1", True, 3.0, "tcp")
            p.configure("10.9.9.9", "")  # settings change during the round
            return out
        p.probe_internet = probe
        p.run_once()
        self.assertEqual(p.result["internet"]["target"], "10.9.9.9")
        self.assertIsNone(p.result["internet"]["ok"])

    def test_dns_name_must_not_be_an_ip(self):
        _, errors = settings.validate({"dns_name": "1.2.3.4"})
        self.assertIn("dns_name", errors)

    def test_failing_listener_does_not_fail_the_update(self):
        with tempfile.TemporaryDirectory() as d:
            s = settings.Settings(os.path.join(d, "s.json"))
            s.listen(lambda v: 1 / 0)
            self.assertEqual(s.update({"paging": "tap"}), {})
            self.assertEqual(s.values["paging"], "tap")


class FakeFB:
    """Just the appbase.FrameBuffer parts slide.py uses, plus its mapping."""

    def __init__(self, phys_w, phys_h, rotate, pad=4):
        self.phys_w, self.phys_h, self.rotate = phys_w, phys_h, rotate
        self.stride = (phys_w + pad) * 2  # padded rows, like some fbdev drivers
        self._buf = bytearray(self.stride * phys_h)
        self.width, self.height = ((phys_h, phys_w) if rotate in (90, 270)
                                   else (phys_w, phys_h))
        self.flushes = 0

    def flush(self):
        self.flushes += 1

    def _phys(self, x, y):  # copied from appbase.FrameBuffer
        r = self.rotate
        if r == 90:
            return self.phys_w - 1 - y, x
        if r == 180:
            return self.phys_w - 1 - x, self.phys_h - 1 - y
        if r == 270:
            return y, self.phys_h - 1 - x
        return x, y

    def frame(self, color):
        """Buffer where each visual pixel holds color(x, y) (16 bits)."""
        buf = bytearray(len(self._buf))
        for y in range(self.height):
            for x in range(self.width):
                px, py = self._phys(x, y)
                off = py * self.stride + px * 2
                buf[off:off + 2] = (color(x, y) & 0xFFFF).to_bytes(2, "little")
        return buf

    def pixel(self, buf, x, y):
        px, py = self._phys(x, y)
        off = py * self.stride + px * 2
        return int.from_bytes(buf[off:off + 2], "little")


class SlideTest(unittest.TestCase):
    def check(self, rotate):
        fb = FakeFB(12, 8, rotate)
        old = fb.frame(lambda x, y: x * 16 + y)
        new = fb.frame(lambda x, y: 0x8000 | (x * 16 + y))
        w = fb.width
        for forward in (True, False):
            for shift in (0, 3, w):
                out = bytearray(len(old))
                slide.compose(fb, out, old, new, shift, forward)
                for y in range(fb.height):
                    for x in range(w):
                        if forward:
                            want = (fb.pixel(old, x + shift, y) if x < w - shift
                                    else fb.pixel(new, x - (w - shift), y))
                        else:
                            want = (fb.pixel(new, x + w - shift, y) if x < shift
                                    else fb.pixel(old, x - shift, y))
                        self.assertEqual(fb.pixel(out, x, y), want,
                                         (rotate, forward, shift, x, y))

    def test_compose_every_rotation(self):
        for rotate in (0, 90, 180, 270):
            self.check(rotate)

    def test_play_ends_on_the_new_frame(self):
        fb = FakeFB(12, 8, 90)
        old = fb.frame(lambda x, y: 1)
        new = fb.frame(lambda x, y: 2)
        slide.play(fb, old, new, True)
        self.assertEqual(fb._buf, new)
        self.assertEqual(fb.flushes, slide.FRAMES - 1)

    def test_can(self):
        self.assertTrue(slide.can(FakeFB(4, 4, 0)))
        self.assertFalse(slide.can(object()))


def import_main():
    """main.py needs appbase, which only exists on the device; stub it."""
    import types
    try:
        import appbase
        real = hasattr(appbase, "app")  # not a stray namespace package
    except ImportError:
        real = False
    if not real:
        stub = types.ModuleType("appbase")
        stub.LEFT, stub.RIGHT, stub.TAP, stub.WHITE = "left", "right", "tap", 0xFFFF
        stub.rgb565 = lambda r, g, b: 0
        stub.Rect = object
        stub.app = lambda: (lambda f: f)
        sys.modules["appbase"] = stub
    import main
    return main


class HostTest(unittest.TestCase):
    def test_device_names_and_addresses(self):
        ok = import_main().host_allowed
        for host in ("192.168.1.50:8080", "192.168.1.50", "[fe80::1]:8080",
                     "fe80::1", "localhost:8080", "NanoKVM-Go:8080",
                     "nanokvm-go.local", "nanokvm-go.local.",
                     "nanokvm-go.tail1234.ts.net:8080"):
            self.assertTrue(ok(host, "nanokvm-go"), host)

    def test_other_names_are_refused(self):
        ok = import_main().host_allowed
        for host in ("", None, "evil.example", "nanokvm-go.evil.example",
                     "rebind.attacker.ts.net", "nanokvm-go.a.b.ts.net",
                     "localhost.evil.example", "[::1"):
            self.assertFalse(ok(host, "nanokvm-go"), host)
        self.assertFalse(ok("anything.local", ""))

    def test_json_content_type(self):
        is_json = import_main().is_json
        self.assertTrue(is_json({"Content-Type": "application/json; charset=utf-8"}))
        self.assertFalse(is_json({"Content-Type": "text/plain"}))
        self.assertFalse(is_json({}))


def sample_snap(**over):
    snap = {
        "hostname": "kvm", "ip": "192.168.1.5", "tailscale_ip": None,
        "cpu": 10.0, "mem": {"used": 1, "total": 4}, "temp": 40.0,
        "rx_rate": 0.0, "tx_rate": 0.0, "uptime": 100,
        "ifaces": [{"name": "eth0", "state": "up", "ip": "192.168.1.5",
                    "wifi_dbm": None, "default": True}],
        "probes": {k: {"target": "x", "ok": True, "ms": 1.2}
                   for k in ("gateway", "internet", "dns")},
        "video": {"signal": True, "online": False, "resolution": "1920x1080",
                  "fps": 60, "watched": False},
        "usb": {"state": "configured", "speed": "high-speed"},
        "procs": None,
    }
    snap.update(over)
    return snap


class HistoryTest(unittest.TestCase):
    def test_gap_adds_empty_points(self):
        h = collector.History(maxlen=10)
        h.append(sample_snap())
        h.gap(3)
        h.append(sample_snap())
        now, cols = h.since(0)
        self.assertEqual(now, 5)
        self.assertEqual(cols["cpu"], [10.0, None, None, None, 10.0])

    def test_gap_is_capped_by_the_buffer(self):
        h = collector.History(maxlen=4)
        h.gap(1000)
        self.assertEqual(h.seq, 4)

    def test_since_upto(self):
        h = collector.History()
        for _ in range(5):
            h.append(sample_snap())
        now, cols = h.since(1, upto=3)
        self.assertEqual((now, cols["seq"]), (3, [2, 3]))


class StatusCacheTest(unittest.TestCase):
    def test_one_encoding_per_sample_and_since(self):
        main = import_main()
        h = collector.History()
        latest = {"cur": (h.append(sample_snap()), sample_snap())}
        cache = main.StatusCache(h, latest, "b00t")
        first = cache.get(0)
        self.assertIs(cache.get(0), first)  # shared by every page in step
        body = json.loads(first + b"}")
        self.assertEqual(body["now_seq"], 1)
        self.assertEqual(body["snap"]["ip"], "192.168.1.5")
        hist = json.loads(cache.get(0, with_snap=False) + b"}")
        self.assertNotIn("snap", hist)
        latest["cur"] = (h.append(sample_snap(cpu=50.0)), sample_snap(cpu=50.0))
        again = json.loads(cache.get(1) + b"}")
        self.assertEqual(again["history"]["cpu"], [50.0])
        self.assertEqual(again["snap"]["cpu"], 50.0)

    def test_history_never_runs_past_the_published_snapshot(self):
        main = import_main()
        h = collector.History()
        latest = {"cur": (h.append(sample_snap()), sample_snap())}
        h.append(sample_snap())  # appended, not yet published
        body = json.loads(main.StatusCache(h, latest, "b").get(0) + b"}")
        self.assertEqual(body["now_seq"], 1)

    def test_entries_are_bounded(self):
        main = import_main()
        h = collector.History()
        latest = {"cur": (h.append(sample_snap()), sample_snap())}
        cache = main.StatusCache(h, latest, "b")
        for since in range(100):
            cache.get(since)
        self.assertLessEqual(len(cache._entries), cache.MAX)


class WakerTest(unittest.TestCase):
    def test_wake_ends_the_wait(self):
        waker = import_main().Waker()
        self.addCleanup(waker._r.close)
        self.addCleanup(waker._w.close)
        waker.wake()
        t = time.monotonic()
        waker.wait([], 5.0)
        self.assertLess(time.monotonic() - t, 1.0)
        t = time.monotonic()
        waker.wait([], 0.05)  # drained: the next wait runs to its timeout
        self.assertGreaterEqual(time.monotonic() - t, 0.04)


class PageKeyTest(unittest.TestCase):
    def setUp(self):
        import_main()
        import screen
        self.screen = screen
        self.info = {"port": 8080, "web_error": "", "path": "/"}

    def key(self, name, snap, values=settings.DEFAULTS):
        return self.screen.page_key(snap, self.screen.PAGES.index(name),
                                    self.info, values)

    def test_unshown_fields_keep_the_key(self):
        base = sample_snap()
        busy = sample_snap(cpu=99.0, temp=80.0, uptime=999,
                           mem={"used": 3, "total": 4})
        for name in ("HOME", "HOST", "WEB", "NETWORK", "SETTINGS"):
            self.assertEqual(self.key(name, base), self.key(name, busy), name)

    def test_shown_fields_change_the_key(self):
        base = sample_snap()
        changes = {
            "HOME": sample_snap(tailscale_ip="100.64.0.1"),
            "HOST": sample_snap(video=dict(base["video"], fps=30)),
            "WEB": sample_snap(ip="10.0.0.2"),
            "NETWORK": sample_snap(probes=dict(base["probes"], dns={
                "target": "x", "ok": True, "ms": 9.0})),
            "TOP": sample_snap(procs=[{"name": "a", "cpu": 1.0}]),
        }
        for name, snap in changes.items():
            self.assertNotEqual(self.key(name, base), self.key(name, snap), name)
        self.assertNotEqual(
            self.key("SETTINGS", base),
            self.key("SETTINGS", base, dict(settings.DEFAULTS, paging="tap")))

    def test_pages_differ_and_health_always_redraws(self):
        snap = sample_snap()
        keys = [self.key(n, snap) for n in self.screen.PAGES if n != "HEALTH"]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertIsNone(self.key("HEALTH", snap))


class StaleProbeTest(unittest.TestCase):
    def test_slow_but_current_results_stay(self):
        p = collector.Prober("1.1.1.1", "")
        r = collector.result("1.1.1.1", False, None, "tcp")
        r["at"] -= collector.PROBE_STALE + 1  # e.g. a lookup hanging for 20 s
        p.result["internet"] = r
        self.assertIs(p.view()["internet"]["ok"], False)

    def test_long_idle_forgets_results(self):
        looking = [False]
        p = collector.Prober("1.1.1.1", "x.example", active=lambda: looking[0])
        p.result["internet"] = collector.result("1.1.1.1", True, 3.0, "tcp")
        seen = []
        p.run_once = lambda: seen.append(p.view()["internet"]["ok"])
        with patch.object(collector, "PROBE_STALE", 0.1):
            p.start()
            time.sleep(1.3)  # idle longer than PROBE_STALE
            looking[0] = True
            p.kick()
            time.sleep(0.3)
        self.assertEqual(seen[:1], [None])
        self.assertEqual(p.view()["internet"]["target"], "1.1.1.1")

    def test_idle_prober_does_not_probe(self):
        rounds = []
        p = collector.Prober("", "", active=lambda: False)
        p.run_once = lambda: rounds.append(1)
        p.start()
        time.sleep(0.3)
        self.assertEqual(rounds, [])


if __name__ == "__main__":
    unittest.main()
