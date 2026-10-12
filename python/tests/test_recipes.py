"""The recipes' own logic (webhook bridge rules, loudness math), without a meter or a microphone."""

import http.client
import importlib.util
import json
import math
import struct
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

RECIPES = Path(__file__).resolve().parents[2] / "recipes"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, RECIPES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


webhook = load("webhook_bridge")
loudness = load("mic_loudness")
cli_recipe = load("level_from_cli")


class FakeMeter:
    def __init__(self):
        self.calls = []

    def set_level(self, level, ttl):
        self.calls.append(("level", level, ttl))
        return {"active": [f"level-{level}"], "expiresInSeconds": ttl}

    def clear(self):
        self.calls.append(("clear",))
        return {"active": [], "expiresInSeconds": 0}


class ReconnectingMeter(FakeMeter):
    """The first call fails as revoked; reconnecting() registers again and retries, like busymeter.BusyMeter."""

    def __init__(self):
        super().__init__()
        self.reconnects = 0
        self.revoked = True

    def set_level(self, level, ttl):
        if self.revoked:
            from busymeter import UnauthorizedError
            raise UnauthorizedError("revoked", "unauthorized", 401)
        return super().set_level(level, ttl)

    def reconnecting(self, fn):
        from busymeter import UnauthorizedError
        try:
            return fn()
        except UnauthorizedError:
            self.reconnects += 1
            self.revoked = False
            return fn()


class WebhookTests(unittest.TestCase):
    SECRET = "s" * 43

    def setUp(self):
        self.meter = FakeMeter()
        self.holder = webhook.Holder(self.meter)
        self.addCleanup(self.holder.cancel)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webhook.make_handler(self.SECRET, self.holder, 0))
        self.port = self.server.server_address[1]
        handler = webhook.make_handler(self.SECRET, self.holder, self.port)
        handler.log_message = lambda *args: None
        self.server.RequestHandlerClass = handler
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body=None, secret=SECRET, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json", **(headers or {})}
        if secret:
            h["Authorization"] = "Bearer " + secret
        conn.request("POST", path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, data

    def test_listens_on_loopback_only(self):
        self.assertEqual("127.0.0.1", self.server.server_address[0])

    def test_level_and_clear(self):
        self.assertEqual(200, self.post("/level", {"level": 5, "for": "10m"})[0])
        self.assertEqual(200, self.post("/level", {"level": "flash", "for": 30})[0])
        self.assertEqual(200, self.post("/clear")[0])
        self.assertEqual([("level", 5, 600), ("level", "flash", 30), ("clear",)], self.meter.calls)

    def test_secret_is_required(self):
        self.assertEqual(401, self.post("/level", {"level": 5}, secret=None)[0])
        self.assertEqual(401, self.post("/level", {"level": 5}, secret="wrong")[0])
        self.assertEqual(200, self.post("/level", {"level": 5}, secret=None, headers={"X-Busy-Meter-Secret": self.SECRET})[0])

    def test_web_pages_and_other_hosts_are_refused(self):
        self.assertEqual(403, self.post("/level", {"level": 5}, headers={"Origin": "https://example.com"})[0])
        self.assertEqual(400, self.post("/level", {"level": 5}, headers={"Host": "evil.example"})[0])
        self.assertEqual([], self.meter.calls)

    def test_bad_requests(self):
        self.assertEqual(400, self.post("/level", {"level": 9})[0])
        self.assertEqual(400, self.post("/level", {"level": 3, "for": "forever"})[0])
        self.assertEqual(404, self.post("/nope", {})[0])
        self.assertEqual(413, self.post("/level", {"level": 3, "pad": "x" * 5000})[0])

    def test_long_holds_are_refreshed_with_short_ttls(self):
        self.post("/level", {"level": 4, "for": "2h"})
        self.assertEqual([("level", 4, 60)], self.meter.calls)
        self.assertIsNotNone(self.holder._stop)
        self.post("/clear")
        self.assertIsNone(self.holder._stop)

    def raw(self, request: bytes) -> int:
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(request)
            return int(sock.recv(4096).split(b" ")[1])

    def test_bad_content_length_is_400(self):
        for value in (b"-5", b"abc", b"+3", b"1e3"):
            request = (b"POST /level HTTP/1.1\r\nHost: 127.0.0.1:" + str(self.port).encode() +
                       b"\r\nAuthorization: Bearer " + self.SECRET.encode() + b"\r\nContent-Type: application/json\r\n"
                       b"Content-Length: " + value + b"\r\n\r\n")
            self.assertEqual(400, self.raw(request), value)
        self.assertEqual([], self.meter.calls)

    def test_handler_has_a_socket_timeout(self):
        self.assertGreater(self.server.RequestHandlerClass.timeout or 0, 0)
        self.assertLessEqual(self.server.RequestHandlerClass.timeout, 30)

    def test_overlapping_long_holds_leave_one_refresher(self):
        threads = [threading.Thread(target=self.holder.set, args=(i % 7, 7200)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, self.holder.active())
        import time
        deadline = time.monotonic() + 5
        while sum(1 for t in threading.enumerate() if t.name == "busymeter-webhook-hold" and t.is_alive()) > 1:
            self.assertLess(time.monotonic(), deadline, "stopped refreshers didn't exit")
            time.sleep(0.01)
        self.holder.clear()
        self.assertEqual(0, self.holder.active())

    def test_a_revoked_token_reconnects(self):
        meter = ReconnectingMeter()
        holder = webhook.Holder(meter)
        self.assertEqual(["level-4"], holder.set(4, 60)["active"])
        self.assertEqual(1, meter.reconnects)

    def test_secret_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens" / "webhook-secret.txt"
            first, created = webhook.load_or_create_secret(path)
            again, created_again = webhook.load_or_create_secret(path)
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first, again)
            self.assertGreaterEqual(len(first), 40)


class LoudnessTests(unittest.TestCase):
    def test_parse_levels(self):
        self.assertEqual([(-25.0, 6), (-35.0, 4), (-45.0, 2)], loudness.parse_levels("-45:2,-35:4,-25:6"))
        self.assertEqual([(-15.0, "flash")], loudness.parse_levels("-15:flash"))
        with self.assertRaises(ValueError):
            loudness.parse_levels("-15:9")

    def test_rms(self):
        self.assertEqual(-90.0, loudness.rms_dbfs(b"\x00\x00" * 100))
        full = struct.pack("<100h", *([32767, -32767] * 50))
        self.assertAlmostEqual(0.0, loudness.rms_dbfs(full), places=2)
        half = struct.pack("<100h", *([16384, -16384] * 50))
        self.assertAlmostEqual(20 * math.log10(0.5), loudness.rms_dbfs(half), places=1)

    def test_level_for(self):
        levels = loudness.parse_levels("-45:2,-35:4,-25:6")
        self.assertIsNone(loudness.level_for(-60, levels))
        self.assertEqual(2, loudness.level_for(-40, levels))
        self.assertEqual(6, loudness.level_for(-3, levels))

    def test_only_a_number_is_kept(self):
        meter = loudness.Loudness()
        meter.add(struct.pack("<4h", 1000, -1000, 1000, -1000))
        self.assertEqual({"db", "attack", "release", "lock"}, set(vars(meter)))


class CliRecipeTests(unittest.TestCase):
    def test_it_is_the_package_cli(self):
        from busymeter.cli import main
        self.assertIs(main, cli_recipe.main)


if __name__ == "__main__":
    unittest.main()
