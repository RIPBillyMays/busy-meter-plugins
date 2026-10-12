"""The common recipe runner against the fake plugin API: register with suggested levels, refresh, clear on exit,
re-register after a revoke or when the declared signals change, stop file, settings re-read."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from busymeter import BusyMeter, TokenStore
from busymeter.recipes.base import Recipe
from busymeter.recipes.control import RunFiles
from busymeter.recipes.runner import EXIT_DENIED, EXIT_OK, EXIT_SETTINGS, Runner
from busymeter.recipes.settings import Field, Schema

from .fake_api import FakeApi

HERE = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Scripted(Recipe):
    """Plays a list of active-signal lists, one per poll, advancing the fake clock by ``step`` each time."""

    name = "scripted"
    plugin_id = "com.example.scripted"
    title = "Scripted test recipe"
    schema = Schema({"level": Field("level", 3), "second": Field("bool", False)})
    ttl_seconds = 10
    interval = 0.005
    script: list = []
    step = 1.0
    hook = None

    def __init__(self, settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.polls = 0

    def signals(self):
        out = [{"name": "busy", "label": "Busy", "suggest": self.settings["level"]}]
        if self.settings["second"]:
            out.append({"name": "extra", "suggest": "flash"})
        return out

    def poll(self):
        if self.hook:
            self.hook(self)
        if self.polls >= len(self.script):
            self.finished = True
            return []
        active = self.script[self.polls]
        self.polls += 1
        self.clock.now += self.step
        return active


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = FakeApi().__enter__()
        self.addCleanup(self.api.__exit__)
        self.api.answer = True
        self.store = TokenStore(Path(self.tmp.name) / "tokens", restrict=False)
        self.dir = Path(self.tmp.name) / "recipes"
        self.meter = BusyMeter(Scripted.plugin_id, port=self.api.port, store=self.store, timeout=5)
        self.clock = Clock()
        self.messages = []

    def runner(self, cls=Scripted, **kwargs):
        return Runner(cls, meter=self.meter, directory=self.dir, restrict=False, clock=self.clock,
                      say=self.messages.append, heartbeat=0.05, **kwargs)

    def calls(self, method=None):
        out = []
        for r in self.api.requests:
            if r["path"].startswith("/v1/register"):
                kind = {"POST": "register", "DELETE": "withdraw"}.get(r["method"], "poll")
            else:
                kind = r["method"]
            if method is None or kind == method:
                body = json.loads(r["body"]) if r["body"] else None
                out.append((kind, body))
        return out


def script(cls_name, steps, **attrs):
    if "hook" in attrs:
        attrs["hook"] = staticmethod(attrs["hook"])
    return type(cls_name, (Scripted,), {"script": steps, **attrs})


class RunnerTests(RunnerBase):
    def test_registers_with_suggested_levels_sends_refreshes_and_clears(self):
        cls = script("A", [["busy"]] * 7 + [[]] + [["busy"]], step=1.0)
        code = self.runner(cls).run()
        self.assertEqual(EXIT_OK, code)
        register = self.calls("register")[0][1]
        self.assertEqual("com.example.scripted", register["id"])
        self.assertEqual([{"name": "busy", "label": "Busy", "suggest": 3}], register["signals"])
        self.assertEqual(["signals"], register["wants"])
        puts = [b for k, b in self.calls("PUT")]
        self.assertTrue(all(p == {"active": ["busy"], "ttlSeconds": 10} for p in puts))
        # sent at t=1, refreshed once the TTL is half gone (t=6), then cleared, then sent again
        self.assertEqual(3, len(puts))
        kinds = [k for k, _ in self.calls() if k in ("PUT", "DELETE")]
        self.assertEqual(["PUT", "PUT", "DELETE", "PUT", "DELETE"], kinds)  # the last DELETE: clear on exit
        self.assertTrue((self.dir / "scripted.json").is_file())
        self.assertEqual("stopped", RunFiles("scripted", self.dir).read()["state"])

    def test_nothing_sent_and_nothing_cleared_when_never_active(self):
        self.runner(script("B", [[], [], []])).run()
        self.assertEqual([], [k for k, _ in self.calls() if k in ("PUT", "DELETE")])

    def test_undeclared_names_are_never_sent(self):
        self.runner(script("C", [["busy", "nope"]])).run()
        self.assertEqual({"active": ["busy"], "ttlSeconds": 10}, self.calls("PUT")[0][1])

    def test_a_revoked_token_registers_again_and_retries(self):
        api = self.api

        def revoke(recipe):
            if recipe.polls == 2:
                api.tokens.clear()  # the user clicked Revoke in the app

        cls = script("D", [["busy"]] * 4, step=6.0, hook=revoke)
        self.assertEqual(EXIT_OK, self.runner(cls).run())
        self.assertEqual(2, len(self.calls("register")))
        kinds = [k for k, _ in self.calls()]
        second = len(kinds) - 1 - kinds[::-1].index("register")
        self.assertIn("PUT", kinds[second:])  # the refused update was sent again with the new token

    def test_denied_stops_with_a_message(self):
        self.api.answer = False
        code = self.runner(script("E", [["busy"]])).run()
        self.assertEqual(EXIT_DENIED, code)
        self.assertTrue(any("Deny" in m for m in self.messages))
        self.assertEqual("stopped", RunFiles("scripted", self.dir).read()["state"])

    def test_a_deny_is_remembered_and_an_allow_forgets_it(self):
        files = RunFiles("scripted", self.dir)
        self.api.answer = False
        self.runner(script("D", [["busy"]])).run()
        self.assertTrue(files.denied())  # so a sign-in start doesn't ask again
        self.api.answer = True
        self.store.delete(Scripted.plugin_id)
        self.runner(script("Denied2", [["busy"]])).run()
        self.assertFalse(files.denied())

    def test_the_last_stopped_is_never_overwritten_by_the_heartbeat(self):
        files = RunFiles("scripted", self.dir)
        for i in range(15):
            self.runner(script(f"S{i}", [["busy"]] * 3)).run()
            time.sleep(0.07)  # longer than the 0.05 s heartbeat
            self.assertEqual("stopped", files.read()["state"])
        self.assertFalse([t for t in threading.enumerate() if t.name == "busymeter-heartbeat"])

    def test_changed_signals_ask_again_without_stopping(self):
        settings_dir = self.dir

        def edit(recipe):
            if recipe.polls == 2:
                path = settings_dir / "scripted.json"
                path.write_text(json.dumps({"level": 5, "second": True}), encoding="utf-8")
                os.utime(path, ns=(time.time_ns() + 10**9,) * 2)

        steps = [["busy", "extra"]] * 12
        cls = script("F", steps, hook=edit, step=0.5)
        self.assertEqual(EXIT_OK, self.runner(cls).run())
        registers = [b for k, b in self.calls("register")]
        self.assertEqual(2, len(registers))
        self.assertEqual([{"name": "busy", "label": "Busy", "suggest": 5}, {"name": "extra", "suggest": "flash"}],
                         registers[1]["signals"])
        puts = [b["active"] for k, b in self.calls("PUT")]
        self.assertEqual(["busy"], puts[0])  # "extra" isn't sent before the new registration is allowed
        self.assertEqual(["busy", "extra"], puts[-1])
        saved = self.store.load("com.example.scripted")
        self.assertEqual(2, len(saved["signals"]))

    def test_a_broken_settings_edit_keeps_the_old_settings(self):
        settings_dir = self.dir

        def edit(recipe):
            if recipe.polls == 1:
                path = settings_dir / "scripted.json"
                path.write_text('{"level": 9}', encoding="utf-8")
                os.utime(path, ns=(time.time_ns() + 10**9,) * 2)

        self.runner(script("G", [["busy"]] * 4, hook=edit)).run()
        self.assertEqual(1, len(self.calls("register")))
        self.assertEqual(1, sum("Settings not applied" in m for m in self.messages))

    def test_a_broken_settings_file_at_start_is_exit_2(self):
        self.dir.mkdir(parents=True)
        (self.dir / "scripted.json").write_text('{"level": "loud"}', encoding="utf-8")
        self.assertEqual(EXIT_SETTINGS, self.runner(script("H", [["busy"]])).run())
        self.assertTrue(any("level" in m for m in self.messages))
        self.assertEqual([], self.api.requests)

    def test_the_stop_file_stops_it_and_clears(self):
        stop_dir = self.dir

        def ask_stop(recipe):
            if recipe.polls == 2:
                RunFiles("scripted", stop_dir).request_stop()

        cls = script("I", [["busy"]] * 100000, hook=ask_stop, step=0.0, interval=0.01)
        self.assertEqual(EXIT_OK, self.runner(cls).run())
        self.assertEqual("DELETE", [k for k, _ in self.calls()][-1])
        self.assertFalse(RunFiles("scripted", self.dir).stop_path.exists())

    def test_waits_for_the_app_when_external_connections_is_off(self):
        self.api.__exit__()
        stop = threading.Event()
        runner = Runner(script("J", [["busy"]]), meter=BusyMeter("com.example.scripted", port=self.api.port,
                                                                 store=self.store, timeout=1),
                        directory=self.dir, restrict=False, say=self.messages.append, stop_event=stop, heartbeat=0.05)
        threading.Timer(0.5, stop.set).start()
        self.assertEqual(EXIT_OK, runner.run())
        self.assertTrue(any("External connections" in m for m in self.messages))
        self.assertEqual("stopped", RunFiles("scripted", self.dir).read()["state"])

    def test_a_recipe_that_cannot_start_stops_with_a_message(self):
        import socket

        from busymeter.recipes.game_state import GameState
        from busymeter.recipes.runner import EXIT_START

        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)
        self.dir.mkdir(parents=True)
        (self.dir / "game_state.json").write_text(json.dumps({"port": taken.getsockname()[1], "token": "k" * 32}),
                                                  encoding="utf-8")
        runner = Runner(GameState, meter=self.meter, directory=self.dir, restrict=False, say=self.messages.append,
                        heartbeat=0.05)
        self.assertEqual(EXIT_START, runner.run())
        self.assertTrue(any("Couldn't start" in m for m in self.messages))
        self.assertEqual([], self.api.requests)

    def test_stop_while_waiting_for_allow_returns_quickly(self):
        self.api.answer = None  # nobody answers the tray prompt
        stop = threading.Event()
        runner = self.runner(script("L", [["busy"]] * 3), stop_event=stop)
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        self.assertEqual(EXIT_OK, runner.run())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(1, len(self.calls("register")))
        self.assertEqual([], [k for k, _ in self.calls() if k in ("PUT", "DELETE")])
        # the waiting prompt is withdrawn, so the tray doesn't keep asking for a recipe that stopped
        self.assertEqual(1, len(self.calls("withdraw")))
        self.assertEqual(1, len(self.api.withdrawn))
        self.assertIsNone(self.store.load("com.example.scripted"))
        self.api.answer = True
        self.assertEqual(EXIT_OK, self.runner(script("L2", [["busy"]])).run())
        self.assertEqual(2, len(self.calls("register")))

    def test_a_withdraw_pause_is_a_short_wait_not_an_app_problem(self):
        self.api.forced["POST /v1/register"] = (429, "withdraw_pause", "just withdrew")
        states = []
        runner = self.runner(script("P", [["busy"]]))
        original = runner.set_state
        runner.set_state = lambda state, message="": (states.append(state), original(state, message))[1]
        started = time.monotonic()
        self.assertEqual(EXIT_OK, runner.run())
        self.assertLess(time.monotonic() - started, 5)  # Retry-After 1 from the fake
        self.assertNotIn("waiting_for_app", states)
        self.assertEqual(2, len([r for r in self.api.requests if r["method"] == "POST" and r["path"] == "/v1/register"]))

    def test_a_deny_cooldown_is_shown_and_not_retried_fast(self):
        self.api.forced["POST /v1/register"] = (429, "cooldown", "denied recently")
        states = []
        stop = threading.Event()
        runner = self.runner(script("C", [["busy"]]), stop_event=stop)
        original = runner.set_state
        runner.set_state = lambda state, message="": (states.append((state, message)), original(state, message))[1]
        threading.Timer(1.5, stop.set).start()
        runner.run()
        self.assertIn("waiting_for_app", [s for s, _ in states])
        self.assertTrue(any("denied" in m for _, m in states))
        self.assertEqual(1, len([r for r in self.api.requests if r["method"] == "POST" and r["path"] == "/v1/register"]))

    def test_stop_while_a_re_registration_waits_after_a_revoke(self):
        api = self.api

        def revoke(recipe):
            if recipe.polls == 1:
                api.tokens.clear()
                api.answer = None

        stop = threading.Event()
        cls = script("M", [["busy"]] * 1000, hook=revoke, step=6.0, interval=0.01)
        runner = self.runner(cls, stop_event=stop)
        threading.Timer(1.0, stop.set).start()
        started = time.monotonic()
        self.assertEqual(EXIT_OK, runner.run())
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(2, len(self.calls("register")))

    def test_connected_is_called_once_after_the_allow(self):
        events = []
        api = self.api

        class Hooked(Scripted):
            script = [["busy"]] * 3

            def connected(self):
                events.append(("connected", any(r["path"].startswith("/v1/register/") for r in api.requests)))

        self.runner(Hooked).run()
        self.assertEqual([("connected", True)], events)

    def test_heartbeat_marks_it_running(self):
        seen = []
        files = RunFiles("scripted", self.dir)

        def look(recipe):
            if recipe.polls == 3:
                time.sleep(0.15)
                seen.append(files.status())

        self.runner(script("K", [["busy"]] * 5, hook=look)).run()
        self.assertTrue(seen[0]["running"])
        self.assertEqual("running", seen[0]["state"])
        self.assertEqual(os.getpid(), seen[0]["pid"])
        self.assertFalse(files.status()["running"])


class ProcessTests(unittest.TestCase):
    """``python -m busymeter run focus_timer`` as a real process: registers, shows focus, stops on the stop file."""

    def test_runs_and_stops_cleanly_from_the_stop_file(self):
        with tempfile.TemporaryDirectory() as tmp, FakeApi() as api:
            api.answer = True
            discovery = Path(tmp) / "plugin-api.json"
            discovery.write_text(json.dumps({"port": api.port}), encoding="utf-8")
            recipes = Path(tmp) / "recipes"
            env = dict(os.environ, BUSYMETER_DISCOVERY_FILE=str(discovery), BUSYMETER_TOKEN_DIR=str(Path(tmp) / "t"),
                       BUSYMETER_RECIPES_DIR=str(recipes), PYTHONPATH=str(HERE))
            process = subprocess.Popen([sys.executable, "-m", "busymeter", "run", "focus_timer"], env=env, cwd=tmp,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not api.active.get("com.thebusymeter.focus"):
                    time.sleep(0.1)
                self.assertEqual(["focus"], api.active.get("com.thebusymeter.focus"))
                self.assertTrue(RunFiles("focus_timer", recipes).status()["running"])
                RunFiles("focus_timer", recipes).request_stop()
                out, _ = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
            self.assertEqual(0, process.returncode, out)
            self.assertEqual([], api.active["com.thebusymeter.focus"])
            self.assertFalse(RunFiles("focus_timer", recipes).status()["running"])
            register = next(json.loads(r["body"]) for r in api.requests if r["path"] == "/v1/register")
            self.assertEqual([{"name": "focus", "label": "Focus block", "suggest": 2}], register["signals"])


if __name__ == "__main__":
    unittest.main()
