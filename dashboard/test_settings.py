import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

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
    if "appbase" not in sys.modules:
        try:
            import appbase  # noqa: F401
        except ImportError:
            stub = types.ModuleType("appbase")
            stub.LEFT, stub.RIGHT, stub.WHITE = "left", "right", 0xFFFF
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


if __name__ == "__main__":
    unittest.main()
