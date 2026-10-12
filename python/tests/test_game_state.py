"""The game-state receiver: rules, the loopback endpoint's checks, the shipped example config, and expiry."""

import http.client
import json
import socket
import unittest
from pathlib import Path

from busymeter.recipes.game_state import EXAMPLE_RULES, SCHEMA, GameState, check_rules, evaluate, lookup
from busymeter.recipes.settings import SettingsError

EXAMPLE = Path(__file__).resolve().parents[2] / "recipes" / "examples" / "game_state.example.json"
TOKEN = "t" * 32


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class RuleTests(unittest.TestCase):
    def test_lookup(self):
        data = {"a": {"b": [{"c": 1}, {"c": 2}]}, "x": None}
        self.assertEqual(2, lookup(data, "a.b.1.c"))
        self.assertIsNone(lookup(data, "x"))
        self.assertIsNot(None, lookup(data, "a.b.5.c"))
        self.assertIsNot(None, lookup(data, "a.z"))

    def test_conditions(self):
        rules = check_rules("rules", [
            {"path": "round.phase", "equals": "live", "signal": "in-match", "suggest": 4},
            {"path": "player.health", "lt": 30, "signal": "low-health", "suggest": "flash"},
            {"path": "player.score", "gt": 10, "signal": "winning"},
            {"path": "player.spectating", "exists": True, "signal": "spectating"},
            {"path": "player.flag", "equals": True, "signal": "flag"},
            {"path": "round.phase", "equals": "overtime", "signal": "in-match"},
        ])
        self.assertEqual(["in-match", "low-health"], evaluate(rules, {"round": {"phase": "live"},
                                                                         "player": {"health": 12}}))
        self.assertEqual(["in-match", "spectating", "winning"],
                         sorted(evaluate(rules, {"round": {"phase": "overtime"},
                                          "player": {"health": 30, "score": 11, "spectating": None}})))
        self.assertEqual([], evaluate(rules, {"player": {"health": "12", "score": True, "flag": 1}}))
        self.assertEqual(["flag"], evaluate(rules, {"player": {"flag": True}}))
        self.assertEqual([], evaluate(rules, "not an object"))
        self.assertEqual([], evaluate(rules, {"round": {"phase": 1}}))

    def test_bad_rules_name_the_rule(self):
        bad = [
            ([], "a list of 1-64"),
            ([{"path": "a", "signal": "x"}], "exactly one of"),
            ([{"path": "a", "equals": 1, "lt": 2, "signal": "x"}], "exactly one of"),
            ([{"path": "a", "lt": "5", "signal": "x"}], "lt needs a number"),
            ([{"path": "a", "exists": "yes", "signal": "x"}], "exists is true or false"),
            ([{"path": "a", "equals": [1], "signal": "x"}], "equals compares"),
            ([{"path": "a..b", "equals": 1, "signal": "x"}], "path must be"),
            ([{"path": "a", "equals": 1, "signal": "Level-UP"}], "signal must be"),
            ([{"path": "a", "equals": 1, "signal": "level-3"}], "signal must be"),
            ([{"path": "a", "equals": 1, "signal": "x", "suggest": 9}], "suggest is 0-6"),
            ([{"path": "a", "equals": 1, "signal": "x", "run": "cmd"}], "unknown field run"),
            ([{"path": "a", "equals": 1, "signal": f"s{i}"} for i in range(33)], "at most 32"),
        ]
        for rules, text in bad:
            with self.subTest(text=text):
                with self.assertRaises(SettingsError) as ctx:
                    check_rules("rules", rules)
                self.assertIn(text, str(ctx.exception))

    def test_the_shipped_example_is_valid_and_neutral(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        values = SCHEMA.validate(data)
        self.assertEqual(EXAMPLE_RULES, values["rules"])
        recipe = GameState(values)
        self.assertEqual([{"name": "in-match", "label": "In a match", "suggest": 4},
                          {"name": "low-health", "label": "Low health", "suggest": "flash"}], recipe.signals())

    def test_token_is_made_on_first_run_and_is_secret(self):
        a, b = SCHEMA.defaults()["token"], SCHEMA.defaults()["token"]
        self.assertGreaterEqual(len(a), 32)
        self.assertNotEqual(a, b)
        with self.assertRaises(SettingsError):
            SCHEMA.check_agent_changes({"token": "x" * 40})
        with self.assertRaises(SettingsError):
            SCHEMA.validate({"token": "short"})


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.messages = []
        values = SCHEMA.validate({"port": 47822, "token": TOKEN, "max_body_kb": 1, "stale_seconds": 30})
        values["port"] = 0  # any free port (tests only; the schema keeps 1024-65535)
        self.recipe = GameState(values, clock=self.clock, say=self.messages.append)
        self.recipe.start()
        self.addCleanup(self.recipe.stop)
        self.port = self.recipe.port

    def post(self, body, headers=None, host=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else json.dumps(body).encode()
        hdrs = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request("POST", "/", body=data, headers=hdrs)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, json.loads(payload or b"{}")

    def test_listens_on_loopback_only(self):
        self.assertEqual("127.0.0.1", self.recipe.server.server_address[0])

    def test_token_in_the_body_or_a_header(self):
        state = {"round": {"phase": "live"}, "player": {"state": {"health": 80}}}
        self.assertEqual(200, self.post({**state, "auth": {"token": TOKEN}})[0])
        self.assertEqual(["in-match"], self.recipe.poll())
        self.assertEqual(200, self.post({"player": {"state": {"health": 5}}}, {"Authorization": "Bearer " + TOKEN})[0])
        self.assertEqual(["low-health"], self.recipe.poll())
        self.assertEqual(200, self.post({}, {"X-Busy-Meter-Token": TOKEN})[0])
        self.assertEqual([], self.recipe.poll())

    def test_missing_or_wrong_token_is_401_and_changes_nothing(self):
        self.post({"round": {"phase": "live"}, "auth": {"token": TOKEN}})
        for headers, body in [({}, {"round": {"phase": "x"}}), ({"Authorization": "Bearer nope"}, {}),
                              ({}, {"auth": {"token": TOKEN[:-1]}}), ({}, {"auth": {"token": 5}})]:
            with self.subTest(headers=headers, body=body):
                status, reply = self.post(body, headers)
                self.assertEqual(401, status)
                self.assertNotIn(TOKEN, json.dumps(reply))
        self.assertEqual(["in-match"], self.recipe.poll())

    def test_web_pages_and_other_hosts_are_refused(self):
        body = {"round": {"phase": "live"}, "auth": {"token": TOKEN}}
        self.assertEqual(403, self.post(body, {"Origin": "https://example.com"})[0])
        self.assertEqual(403, self.post(body, {"Origin": "null"})[0])
        self.assertEqual(400, self.post(body, host="evil.example:80")[0])
        self.assertEqual(400, self.post(body, host=f"127.0.0.1:{self.port + 1}")[0])
        self.assertEqual(200, self.post(body, host=f"localhost:{self.port}")[0])

    def test_size_cap_and_bad_bodies(self):
        big = {"auth": {"token": TOKEN}, "pad": "x" * 2000}
        try:
            status = self.post(big)[0]
        except ConnectionError:
            status = 413  # the body is refused unread; Windows may reset before the 413 arrives
        self.assertEqual(413, status)
        self.assertEqual(401, self.post(None, raw=b"{not json")[0])  # no token can be read: 401 first
        self.assertEqual(400, self.post(None, raw=b"{not json", headers={"X-Busy-Meter-Token": TOKEN})[0])
        self.assertEqual(200, self.post(None, raw=b"[1, 2]", headers={"X-Busy-Meter-Token": TOKEN})[0])
        self.assertEqual([], self.recipe.poll())  # not an object: no rule matches

    def test_deep_json_does_not_crash(self):
        self.recipe.settings["max_body_kb"] = 256  # room for a deeply nested body
        status, _ = self.post(None, raw=b"[" * 100000 + b"]" * 100000, headers={"X-Busy-Meter-Token": TOKEN})
        self.assertEqual(400, status)  # RecursionError is a bad body, not a crash
        self.assertEqual(200, self.post({"auth": {"token": TOKEN}})[0])  # still answers

    def test_get_and_chunked_are_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(405, conn.getresponse().status)
        conn.close()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nTransfer-Encoding: chunked\r\n\r\n"
                     "5\r\nhello\r\n0\r\n\r\n".encode())
        self.assertIn(b" 411 ", sock.recv(1024))
        sock.close()

    def test_signals_expire_when_the_game_stops_posting(self):
        self.post({"round": {"phase": "live"}, "auth": {"token": TOKEN}})
        self.clock.now += 29
        self.assertEqual(["in-match"], self.recipe.poll())
        self.clock.now += 2
        self.assertEqual([], self.recipe.poll())

    def test_new_rules_wait_for_the_next_post(self):
        self.post({"round": {"phase": "live"}, "auth": {"token": TOKEN}})
        values = dict(self.recipe.settings)
        values["rules"] = check_rules("rules", [{"path": "round.phase", "equals": "live", "signal": "playing"}])
        self.recipe.apply_settings(values)
        self.assertEqual([{"name": "playing"}], self.recipe.signals())
        self.assertEqual([], self.recipe.poll())
        self.post({"round": {"phase": "live"}, "auth": {"token": TOKEN}})
        self.assertEqual(["playing"], self.recipe.poll())

    def test_a_busy_port_keeps_the_old_listener(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        old_port = self.port
        values = dict(self.recipe.settings)
        values["port"] = taken.getsockname()[1]
        self.recipe.apply_settings(values)  # must not raise
        self.assertEqual(old_port, self.recipe.port)
        self.assertEqual(self.recipe.settings["port"], 0)  # the old setting is kept
        self.assertTrue(any("Can't listen on port" in m for m in self.messages))
        self.assertEqual(200, self.post({"auth": {"token": TOKEN}})[0])  # still serving

    def test_a_free_new_port_moves_the_listener(self):
        old_port = self.port
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        new_port = probe.getsockname()[1]
        probe.close()
        values = dict(self.recipe.settings)
        values["port"] = new_port
        self.recipe.apply_settings(values)
        self.port = self.recipe.port
        self.assertEqual(new_port, self.port)
        self.assertEqual(200, self.post({"auth": {"token": TOKEN}})[0])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", old_port), timeout=1).close()

    def test_malformed_tokens_are_401_never_an_error(self):
        for given in ("\ud800", "\udfff" * 40, 5, None, ["x"], {"t": 1}, "", TOKEN + "\ud800"):
            with self.subTest(given=repr(given)):
                self.assertFalse(self.recipe.token_ok(given))
        status, _ = self.post(None, raw=b'{"auth":{"token":"\\ud800"}}')
        self.assertEqual(401, status)
        status, _ = self.post(None, raw=b'{"auth":{"token":"%s\\udc80"}}' % TOKEN.encode())
        self.assertEqual(401, status)
        self.assertEqual(200, self.post({"auth": {"token": TOKEN}})[0])

    def test_at_most_eight_connections_at_once(self):
        from busymeter.recipes.game_state import MAX_CONNECTIONS

        idle = [socket.create_connection(("127.0.0.1", self.port), timeout=5) for _ in range(MAX_CONNECTIONS)]
        try:
            import time as _time

            _time.sleep(0.3)
            extra = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            self.assertIn(b" 503 ", extra.recv(1024))
            extra.close()
        finally:
            for s in idle:
                s.close()
        import time as _time

        deadline = _time.monotonic() + 5
        status = None
        while _time.monotonic() < deadline:
            try:
                status = self.post({"auth": {"token": TOKEN}})[0]
            except (OSError, http.client.HTTPException):
                status = None
            if status == 200:
                break
            _time.sleep(0.1)
        self.assertEqual(200, status)  # slots come back once the idle connections close

    def test_the_token_is_never_printed(self):
        self.post({"auth": {"token": TOKEN}})
        self.assertFalse(any(TOKEN in m for m in self.messages))


if __name__ == "__main__":
    unittest.main()
