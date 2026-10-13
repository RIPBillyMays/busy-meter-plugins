"""The MCP recipe tools (list/install/stop/uninstall/get/set settings) with a fake launcher and temp folders."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from busymeter import TokenStore
from busymeter.recipes import control
from busymeter.recipes.autostart import Autostart, FakeRegistry
from busymeter.recipes.control import RunFiles
from busymeter.recipes.manage import Manager
from busymeter_mcp.tools import Tools


class FakeMeter:
    def __init__(self, store, plugin_id):
        self.store, self.plugin_id = store, plugin_id
        self.calls = []

    @property
    def token(self):
        return (self.store.load(self.plugin_id) or {}).get("token")

    def clear(self):
        self.calls.append("clear")
        return {"active": []}

    def forget(self):
        self.calls.append("forget")
        self.store.delete(self.plugin_id)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "recipes"
        self.store = TokenStore(Path(self.tmp.name) / "tokens", restrict=False)
        self.launched = []
        self.meters = {}
        self.auto = Autostart(self.dir, registry=FakeRegistry(), restrict=False)  # never the real Run key

        def launcher(name):
            self.launched.append(name)
            files = RunFiles(name, self.dir)
            files.write("waiting_for_allow", "Waiting for you to click Allow in the Busy Meter tray...")
            return os.getpid()  # a live process, so the run file counts as running

        def factory(pid):
            self.meters.setdefault(pid, FakeMeter(self.store, pid))
            return self.meters[pid]

        self.stops = []

        def stopper(name, directory):
            self.stops.append(name)
            files = RunFiles(name, directory)
            was = files.status()["running"]
            if was:
                files.write_stopped_for(os.getpid(), "Stopped; signals cleared.")
            return {"stopped": was, "wasRunning": was, "message": f"Stopped {name}." if was else f"{name} isn't running."}

        self.manager = Manager(self.dir, launcher=launcher, client_factory=factory, restrict=False, stopper=stopper,
                               settle=0.3, autostart=self.auto)
        self.tools = Tools(client_factory=lambda pid: None, recipes=self.manager)


class InstallTests(Base):
    def test_list_shows_the_bundled_recipes(self):
        listed = self.tools.list_recipes()
        names = [r["name"] for r in listed["recipes"]]
        self.assertEqual(["stream_status", "game_state", "focus_timer", "vibe_meter"], names)
        self.assertTrue(all(not r["installed"] and not r["running"] for r in listed["recipes"]))
        self.assertTrue(all(r["reads"] and r["neverReads"] for r in listed["recipes"]))
        self.assertIn("set_recipe_autostart", listed["message"])
        self.assertTrue(all(r["startsAtSignIn"] is False for r in listed["recipes"]))

    def test_install_writes_defaults_starts_it_and_says_click_allow(self):
        result = self.tools.install_recipe("vibe_meter")
        self.assertTrue(result["ok"], result)
        self.assertEqual(["vibe_meter"], self.launched)
        self.assertIn("click Allow in the Busy Meter tray", result["message"])
        self.assertIn("stop_recipe", result["message"])
        self.assertIn("sign-in", result["message"])
        settings = json.loads((self.dir / "vibe_meter.json").read_text(encoding="utf-8"))
        self.assertEqual(4, settings["burst_sessions"])
        listed = {r["name"]: r for r in self.tools.list_recipes()["recipes"]}
        self.assertTrue(listed["vibe_meter"]["installed"])
        self.assertTrue(listed["vibe_meter"]["running"])
        self.assertEqual("waiting_for_allow", listed["vibe_meter"]["state"])

    def test_install_with_a_saved_approval_says_running(self):
        def launcher(name):
            RunFiles(name, self.dir).write("running", "Focus timer: running.")
            return os.getpid()

        manager = Manager(self.dir, launcher=launcher, restrict=False, autostart=self.auto, settle=1.0)
        result = manager.install_recipe("focus_timer")
        self.assertTrue(result["ok"])
        self.assertEqual("running", result["state"])
        self.assertNotIn("click Allow", result["message"])
        self.assertIn("already allowed", result["message"])

    def test_install_reports_a_recipe_that_stopped_at_once(self):
        def launcher(name):
            RunFiles(name, self.dir).write_stopped_for(os.getpid(), "The user clicked Deny in the Busy Meter tray.")
            return os.getpid()

        manager = Manager(self.dir, launcher=launcher, restrict=False, autostart=self.auto, settle=1.0)
        result = manager.install_recipe("focus_timer")
        self.assertFalse(result["ok"])
        self.assertIn("Deny", result["message"])

    def test_an_old_stopped_note_is_not_read_as_this_run(self):
        RunFiles("focus_timer", self.dir).directory.mkdir(parents=True, exist_ok=True)
        RunFiles("focus_timer", self.dir).write_stopped_for(1, "old run")
        result = self.tools.install_recipe("focus_timer")
        self.assertTrue(result["ok"], result)
        self.assertEqual("waiting_for_allow", result["state"])

    def test_install_reports_a_process_that_died_before_starting(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=30)

        def launcher(name):
            return dead.pid  # exited without writing a run file

        manager = Manager(self.dir, launcher=launcher, restrict=False, autostart=self.auto, settle=3.0)
        started = time.monotonic()
        result = manager.install_recipe("focus_timer")
        self.assertLess(time.monotonic() - started, 2.5)  # noticed, not waited out
        self.assertFalse(result["ok"])
        self.assertEqual("start_failed", result["error"])
        self.assertIn(str(RunFiles("focus_timer", self.dir).log_path), result["message"])

    def test_install_twice_does_not_start_a_second_copy(self):
        self.tools.install_recipe("focus_timer")
        again = self.tools.install_recipe("focus_timer")
        self.assertTrue(again["alreadyRunning"])
        self.assertEqual(["focus_timer"], self.launched)

    def test_only_bundled_names_never_paths_or_code(self):
        for bad in ("../../evil", "C:\\Windows\\system32\\cmd.exe", "vibe_meter.py", "os.system", "", "VIBE_METER",
                    "level_from_cli", "https://example.com/x.py"):
            with self.subTest(name=bad):
                for call in (self.tools.install_recipe, self.tools.stop_recipe, self.tools.uninstall_recipe,
                             self.tools.get_recipe_settings):
                    result = call(bad)
                    self.assertFalse(result["ok"])
                    self.assertEqual("unknown_recipe", result["error"])
                self.assertFalse(self.tools.set_recipe_settings(bad, {"x": 1})["ok"])
        self.assertEqual([], self.launched)
        self.assertFalse(self.dir.exists())

    def test_game_state_and_stream_status_say_where_secrets_go(self):
        game = self.tools.install_recipe("game_state")
        self.assertIn("--show-token", game["message"])
        token = json.loads((self.dir / "game_state.json").read_text(encoding="utf-8"))["token"]
        self.assertNotIn(token, json.dumps(game))
        obs = self.tools.install_recipe("stream_status")
        self.assertIn("password", obs["message"])

    def test_stop_and_uninstall(self):
        self.tools.install_recipe("focus_timer")
        self.store.save_token("com.thebusymeter.focus", "t" * 43)
        stopped = self.tools.stop_recipe("focus_timer")
        self.assertTrue(stopped["stopped"])
        self.assertFalse(self.tools.list_recipes()["recipes"][2]["running"])
        result = self.tools.uninstall_recipe("focus_timer")
        self.assertTrue(result["ok"])
        self.assertIn("Revoke", result["message"])
        self.assertFalse((self.dir / "focus_timer.json").exists())
        self.assertFalse(RunFiles("focus_timer", self.dir).run_path.exists())
        self.assertEqual(["clear", "forget"], self.meters["com.thebusymeter.focus"].calls)
        self.assertIsNone(self.store.load("com.thebusymeter.focus"))

    def test_launch_failure_is_reported(self):
        def broken(name):
            raise OSError("no python")

        manager = Manager(self.dir, launcher=broken, restrict=False, autostart=self.auto, settle=0.1)
        result = manager.install_recipe("focus_timer")
        self.assertFalse(result["ok"])
        self.assertEqual("start_failed", result["error"])


class AppCommandTests(Base):
    """The commands the Busy Meter app's Recipes rows run: recipes --json, install, uninstall."""

    def run_cli(self, fn, *args):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from busymeter.recipes import cli as recipes_cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = getattr(recipes_cli, fn)(*args, manager=self.manager)
        return code, out.getvalue(), err.getvalue()

    def test_json_list_has_states_and_no_paths_or_messages(self):
        self.tools.install_recipe("focus_timer")
        self.manager.autostart.enable("focus_timer")
        code, out, _ = self.run_cli("list_recipes_json")
        self.assertEqual(0, code)
        data = json.loads(out)
        self.assertEqual(1, data["version"])
        rows = {r["name"]: r for r in data["recipes"]}
        self.assertEqual(["stream_status", "game_state", "focus_timer", "vibe_meter"], list(rows))
        focus = rows["focus_timer"]
        self.assertEqual({"name", "title", "pluginId", "installed", "running", "state", "startsAtSignIn", "signInSkipped"},
                         set(focus))
        self.assertEqual("com.thebusymeter.focus", focus["pluginId"])
        self.assertTrue(focus["installed"] and focus["running"])
        self.assertEqual("waiting_for_allow", focus["state"])
        self.assertFalse(rows["vibe_meter"]["installed"])
        self.assertNotIn(str(self.dir), out)

    def test_install_and_uninstall_commands_use_the_manager(self):
        code, out, _ = self.run_cli("install", "stream_status")
        self.assertEqual(0, code)
        self.assertIn("click Allow", out)
        self.assertEqual(["stream_status"], self.launched)
        code, out, _ = self.run_cli("uninstall", "stream_status")
        self.assertEqual(0, code)
        self.assertFalse((self.dir / "stream_status.json").exists())

    def test_stop_exits_1_when_nothing_was_running(self):
        import io
        from contextlib import redirect_stdout
        from busymeter.recipes import cli as recipes_cli
        stopper = lambda name: self.stopper_result(name)
        self.tools.install_recipe("focus_timer")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0, recipes_cli.stop_command("focus_timer", stopper=stopper))
        self.assertIn("Stopped", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, recipes_cli.stop_command("focus_timer", stopper=stopper))
        self.assertIn("isn't running", out.getvalue())

    def stopper_result(self, name):
        return self.manager.stop_recipe(name)

    def test_a_failed_install_exits_1(self):
        def broken(name):
            raise OSError("no python")

        self.manager = Manager(self.dir, launcher=broken, restrict=False, autostart=self.auto, settle=0.1)
        code, out, err = self.run_cli("install", "focus_timer")
        self.assertEqual(1, code)
        self.assertIn("Couldn't start", err)

    def test_the_parser_takes_bundled_names_only(self):
        import io
        from contextlib import redirect_stderr
        from busymeter.cli import main
        for command in ("install", "uninstall"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main([command, "../evil"])
                self.assertEqual(2, raised.exception.code)


class SettingsTests(Base):
    def test_get_shows_values_and_schema_but_never_secrets(self):
        self.tools.install_recipe("stream_status")
        path = self.dir / "stream_status.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["password"] = "hunter2-obs"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = self.tools.get_recipe_settings("stream_status")
        self.assertTrue(result["ok"])
        self.assertNotIn("hunter2-obs", json.dumps(result))
        self.assertEqual("(set in the settings file)", result["values"]["password"])
        self.assertEqual({"type": "level", "description": result["schema"]["live_level"]["description"],
                          "default": 5}, result["schema"]["live_level"])
        self.assertEqual(str(path), result["settingsFile"])

    def test_get_before_install_shows_defaults_without_writing(self):
        result = self.tools.get_recipe_settings("vibe_meter")
        self.assertFalse(result["installed"])
        self.assertEqual("sessions", result["values"]["driver"])
        self.assertFalse(self.dir.exists())

    def test_set_validates_and_saves(self):
        self.tools.install_recipe("vibe_meter")
        result = self.tools.set_recipe_settings("vibe_meter", {"burst_sessions": 6, "alert_level": 6})
        self.assertTrue(result["ok"], result)
        self.assertIn("click Allow", result["message"])  # running and the suggested level changed
        saved = json.loads((self.dir / "vibe_meter.json").read_text(encoding="utf-8"))
        self.assertEqual(6, saved["burst_sessions"])
        same_signals = self.tools.set_recipe_settings("vibe_meter", {"burst_sessions": 3})
        self.assertNotIn("click Allow", same_signals["message"])

    def test_set_refuses_unknown_out_of_range_and_secret_keys(self):
        self.tools.install_recipe("stream_status")
        path = self.dir / "stream_status.json"
        before = path.read_text(encoding="utf-8")
        for changes, text in [({"nope": 1}, "not a setting"), ({"port": 80}, "at least 1024"),
                              ({"live_level": 7}, "0-6"), ({"password": "x"}, "settings file"),
                              ({"live_level": 5, "password": "x"}, "settings file"), ({}, "Pass the changes")]:
            with self.subTest(changes=changes):
                result = self.tools.set_recipe_settings("stream_status", changes)
                self.assertFalse(result["ok"])
                self.assertIn(text, result["message"])
                self.assertIn("Nothing was changed", result["message"])
        self.assertEqual(before, path.read_text(encoding="utf-8"))
        secret = self.tools.set_recipe_settings("stream_status", {"password": "x"})
        self.assertIn(str(path), secret["message"])
        token = self.tools.set_recipe_settings("game_state", {"token": "y" * 40})
        self.assertFalse(token["ok"])
        paths = self.tools.set_recipe_settings("vibe_meter", {"projects_dir": "C:/"})
        self.assertFalse(paths["ok"])

    def test_set_game_rules_are_checked(self):
        bad = self.tools.set_recipe_settings("game_state", {"rules": [{"path": "a", "signal": "x"}]})
        self.assertFalse(bad["ok"])
        good = self.tools.set_recipe_settings("game_state", {"rules": [
            {"path": "match.state", "equals": "on", "signal": "playing", "suggest": 5}]})
        self.assertTrue(good["ok"], good)
        self.assertNotIn("token", json.dumps(good["values"]).replace('"token": "(set in the settings file)"', ""))


class ControlTests(unittest.TestCase):
    def test_background_command_is_fixed_and_detached(self):
        calls = []

        class P:
            pid = 4242

        def popen(command, **kwargs):
            calls.append((command, kwargs))
            return P()

        with tempfile.TemporaryDirectory() as tmp, mock.patch("busymeter.tokens.restrict_windows_dir"):
            pid = control.start_background("vibe_meter", tmp, popen=popen)
        self.assertEqual(4242, pid)
        command, kwargs = calls[0]
        self.assertEqual("-E", command[1])
        if sys.version_info >= (3, 11):
            self.assertEqual("-P", command[2])
        self.assertEqual(["-c", control.BOOTSTRAP, str(control.package_root()), "vibe_meter"], command[-4:])
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
        self.assertEqual(str(Path(tmp) / "vibe_meter.log"), kwargs["stderr"].name)  # early crashes land in the log
        self.assertTrue(kwargs["stderr"].closed)  # the parent's copy is closed after the start
        self.assertEqual(str(Path(tmp)), kwargs["cwd"])  # the recipes folder, never the home folder
        self.assertNotEqual(str(Path.home()), kwargs["cwd"])
        if sys.platform == "win32":
            self.assertTrue(command[0].lower().endswith(("pythonw.exe", "python.exe")))
            self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW or
                            kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
        with self.assertRaises(KeyError):
            control.background_command("../x")

    def test_a_real_background_run_registers_and_stops_cleanly(self):
        from .fake_api import FakeApi

        with tempfile.TemporaryDirectory() as tmp, FakeApi() as api:
            api.answer = True
            discovery = Path(tmp) / "plugin-api.json"
            discovery.write_text(json.dumps({"port": api.port}), encoding="utf-8")
            recipes = Path(tmp) / "recipes"
            recipes.mkdir()
            marker = Path(tmp) / "planted-ran.txt"
            (recipes / "json.py").write_text(f"open(r'{marker}', 'a').write('json')\n", encoding="utf-8")
            env = {"BUSYMETER_DISCOVERY_FILE": str(discovery), "BUSYMETER_TOKEN_DIR": str(Path(tmp) / "tokens")}
            with mock.patch.dict(os.environ, env):
                pid = control.start_background("focus_timer", recipes)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not api.active.get("com.thebusymeter.focus"):
                    time.sleep(0.1)
                self.assertEqual(["focus"], api.active.get("com.thebusymeter.focus"))
                self.assertEqual(pid, RunFiles("focus_timer", recipes).status()["pid"])
                result = control.stop("focus_timer", recipes, wait=15)
                self.assertIn("cleared", result["message"])
                self.assertEqual([], api.active["com.thebusymeter.focus"])
                log = (recipes / "focus_timer.log").read_text(encoding="utf-8")
                self.assertIn("Focus 1", log)
                self.assertFalse(marker.exists())  # the json.py planted in its working folder never ran
            finally:
                if control.pid_alive(pid):
                    os.kill(pid, 15)

    def test_an_early_crash_lands_in_the_log(self):
        def crashing(command, **kwargs):
            # Same detached start (pythonw, flags, the log as stderr), but a child that fails before the runner starts.
            command = [command[0], "-c", "import sys; sys.stderr.write('early crash here'); sys.exit(1)"]
            return subprocess.Popen(command, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("busymeter.tokens.restrict_windows_dir"):
            pid = control.start_background("focus_timer", tmp, popen=crashing)
            log = Path(tmp) / "focus_timer.log"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and (control.pid_alive(pid) or not log.exists()):
                time.sleep(0.1)
            time.sleep(0.2)
            self.assertIn("early crash here", log.read_text(encoding="utf-8", errors="replace"))

    def test_the_package_folder_never_shadows_the_standard_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pkgroot"
            root.mkdir()
            marker = Path(tmp) / "shadowed.txt"
            (root / "json.py").write_text(f"open(r'{marker}', 'a').write('json')\n", encoding="utf-8")
            probe = (control.PATH_SETUP + "; import json, os; "
                     "print(json.__file__); print(sys.path.index(sys.argv[1]))")
            result = subprocess.run([sys.executable, "-E", "-c", probe, str(root)], cwd=tmp, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(marker.exists())
            json_file, index = result.stdout.splitlines()
            self.assertNotIn("pkgroot", json_file)
            # before site-packages (a checkout wins over an older installed copy), after the standard library
            sys_path = subprocess.run([sys.executable, "-E", "-c", control.PATH_SETUP + "; print(repr(sys.path))",
                                       str(root)], cwd=tmp, capture_output=True, text=True, timeout=60).stdout
            entries = eval(sys_path)  # noqa: S307 - our own repr of a list of str
            site = [i for i, p in enumerate(entries) if "site-packages" in p.lower()]
            if site:
                self.assertLess(entries.index(str(root)), min(site))
            self.assertGreater(entries.index(str(root)), 0)

    def test_planted_modules_in_the_working_folder_are_never_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            marker = folder / "planted-ran.txt"
            for module in ("json", "busymeter", "argparse"):
                (folder / f"{module}.py").write_text(
                    f"open(r'{marker}', 'a').write('{module}')\n", encoding="utf-8")
            # The bootstrap without -P too (Python 3.10 has no -P): it must drop the folder from sys.path itself.
            for flags in (["-E"], ["-E", "-P"] if sys.version_info >= (3, 11) else ["-E"]):
                result = subprocess.run([sys.executable, *flags, "-c", control.BOOTSTRAP, str(control.package_root()),
                                         "not_a_recipe"], cwd=tmp, capture_output=True, text=True, timeout=60)
                self.assertEqual(2, result.returncode, result.stderr)  # argparse refused the name: it really ran
                self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

    def test_stale_or_dead_run_files_are_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = RunFiles("vibe_meter", tmp)
            files.write("running")
            self.assertTrue(files.status()["running"])
            self.assertFalse(files.status(now=time.time() + 60)["running"])
            data = json.loads(files.run_path.read_text())
            data["pid"] = 2_000_000_000  # no such process
            files.run_path.write_text(json.dumps(data))
            self.assertFalse(files.status()["running"])

    def test_stop_asks_then_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = RunFiles("focus_timer", tmp)
            files.write("running")

            def recipe_sees_the_stop_file():
                while not files.stop_requested():
                    time.sleep(0.02)
                files.write_stopped_for(os.getpid(), "Stopped; signals cleared.")

            threading.Thread(target=recipe_sees_the_stop_file, daemon=True).start()
            result = control.stop("focus_timer", tmp, wait=5)
            self.assertTrue(result["stopped"])
            self.assertIn("cleared", result["message"])
            self.assertFalse(files.stop_path.exists())
            self.assertFalse(control.stop("focus_timer", tmp)["wasRunning"])

    def test_stop_ends_a_process_that_does_not_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                files = RunFiles("focus_timer", tmp)
                files.write("running")
                data = json.loads(files.run_path.read_text())
                data["pid"] = child.pid
                files.run_path.write_text(json.dumps(data))
                result = control.stop("focus_timer", tmp, wait=0.3)
                self.assertTrue(result["stopped"])
                self.assertIn("expire", result["message"])
                child.wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()


if __name__ == "__main__":
    unittest.main()
