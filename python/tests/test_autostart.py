"""Start at sign-in: one HKCU Run value per bundled recipe, behind a fake registry (never the real Run key)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from busymeter.recipes import autostart, control
from busymeter.recipes import cli as recipes_cli
from busymeter.recipes.autostart import Autostart, FakeRegistry
from busymeter.recipes.control import RunFiles
from busymeter.recipes.manage import Manager
from busymeter.recipes.runner import EXIT_ALREADY, EXIT_DENIED
from busymeter_mcp.tools import Tools

from .test_mcp_recipes import Base


PROFILE = str(control.package_root().parent)  # stands in for %USERPROFILE%, so this checkout counts as per-user


def make(tmp, platform="win32", registry=None, python=r"C:\Users\Some One\Python 3\pythonw.exe", **kwargs):
    kwargs.setdefault("profile", PROFILE)
    return Autostart(Path(tmp) / "recipes", registry=registry or FakeRegistry(), platform=platform,
                     python=python, restrict=False, **kwargs)


def run_launcher(auto, name, profile=PROFILE):
    flags = ["-E"] + (["-P"] if sys.version_info >= (3, 11) else [])
    env = {k: v for k, v in os.environ.items() if not k.startswith("BUSYMETER_")}
    env["USERPROFILE"] = profile
    return subprocess.run([sys.executable, *flags, str(auto.launcher_path), name], capture_output=True, text=True,
                          timeout=60, cwd=str(Path.home()), env=env)


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = FakeRegistry()
        self.auto = make(self.tmp.name, registry=self.registry)

    def test_off_by_default(self):
        self.assertEqual({"stream_status": False, "game_state": False, "focus_timer": False, "vibe_meter": False},
                         self.auto.states())
        self.assertEqual({}, self.registry.values)

    def test_on_writes_one_value_for_that_recipe_and_off_removes_it(self):
        result = self.auto.enable("vibe_meter")
        self.assertTrue(result["ok"], result)
        self.assertEqual(["Busy Meter recipe vibe_meter"], list(self.registry.values))
        self.assertTrue(self.auto.is_on("vibe_meter"))
        self.assertFalse(self.auto.is_on("focus_timer"))
        self.auto.enable("focus_timer")
        self.assertEqual(2, len(self.registry.values))
        off = self.auto.disable("vibe_meter")
        self.assertTrue(off["ok"] and off["wasOn"])
        self.assertEqual(["Busy Meter recipe focus_timer"], list(self.registry.values))
        again = self.auto.disable("vibe_meter")
        self.assertTrue(again["ok"])
        self.assertFalse(again["wasOn"])

    def test_the_command_starts_the_launcher_hidden_with_quoted_paths(self):
        self.auto.enable("vibe_meter")
        command = self.registry.values["Busy Meter recipe vibe_meter"]
        launcher = self.auto.launcher_path
        flags = "-E -P" if sys.version_info >= (3, 11) else "-E"
        self.assertEqual(f'"C:\\Users\\Some One\\Python 3\\pythonw.exe" {flags} "{launcher}" vibe_meter'
                         if " " in str(launcher) else
                         f'"C:\\Users\\Some One\\Python 3\\pythonw.exe" {flags} {launcher} vibe_meter', command)
        self.assertLessEqual(len(command), autostart.MAX_COMMAND)
        self.assertEqual(self.auto.command("vibe_meter"), command)

    def test_paths_with_spaces_are_quoted(self):
        auto = Autostart(Path(self.tmp.name) / "my recipes", registry=self.registry, platform="win32",
                         python=r"C:\Users\Some One\pythonw.exe", restrict=False)
        command = auto.command("focus_timer")
        self.assertTrue(command.startswith('"C:\\Users\\Some One\\pythonw.exe" -E'))
        self.assertIn(f'"{auto.launcher_path}"', command)
        self.assertTrue(command.endswith(" focus_timer"))

    def test_the_launcher_is_fixed_code_that_runs_only_the_recipe_cli(self):
        self.auto.enable("game_state")
        text = self.auto.launcher_path.read_text(encoding="utf-8")
        self.assertIn(repr(str(control.package_root())), text)
        self.assertIn("main(['run', _name, '--at-sign-in'])", text)
        self.assertNotIn("game_state", text)  # one launcher for all recipes; the name comes from the command
        compile(text, str(self.auto.launcher_path), "exec")

    def test_the_launcher_finds_the_package_and_refuses_other_names(self):
        # Runs the written launcher for real, but with a name the CLI refuses, so no recipe starts.
        self.auto.enable("vibe_meter")
        planted = self.auto.launcher_path.parent / "json.py"
        planted.write_text("raise SystemExit('planted module imported')\n", encoding="utf-8")
        done = run_launcher(self.auto, "../evil")
        self.assertEqual(2, done.returncode, done.stderr)
        self.assertIn("invalid choice", done.stderr)
        self.assertNotIn("planted", done.stderr)

    def test_the_launcher_logs_a_broken_install(self):
        root = Path(self.tmp.name) / "broken"
        (root / "busymeter").mkdir(parents=True)
        (root / "busymeter" / "__init__.py").write_text("raise ImportError('broken install for the test')\n",
                                                       encoding="utf-8")
        auto = make(self.tmp.name, registry=self.registry, root=root, profile=self.tmp.name)
        self.assertTrue(auto.enable("vibe_meter")["ok"])
        done = run_launcher(auto, "vibe_meter", profile=self.tmp.name)
        self.assertNotEqual(0, done.returncode)
        log = (auto.directory / "vibe_meter.log").read_text(encoding="utf-8")
        self.assertIn("broken install for the test", log)

    def test_the_launcher_refuses_a_package_outside_the_user_profile(self):
        self.auto.enable("vibe_meter")
        elsewhere = Path(self.tmp.name) / "other-user"
        elsewhere.mkdir()
        done = run_launcher(self.auto, "vibe_meter", profile=str(elsewhere))
        self.assertNotEqual(0, done.returncode)
        log = (self.auto.directory / "vibe_meter.log").read_text(encoding="utf-8")
        self.assertIn("pip install --user", log)

    def test_enable_refuses_a_package_outside_the_user_profile(self):
        auto = make(self.tmp.name, registry=self.registry, root=Path(r"D:\shared\python"),
                    profile=r"C:\Users\Some One")
        result = auto.enable("vibe_meter")
        self.assertFalse(result["ok"])
        self.assertEqual("not_user_install", result["error"])
        self.assertIn("pip install --user", result["message"])
        self.assertEqual({}, self.registry.values)
        self.assertFalse(auto.launcher_path.exists())

    def test_a_profile_prefix_is_not_enough(self):
        auto = make(self.tmp.name, registry=self.registry, root=Path(r"C:\Users\Some One2\py"),
                    profile=r"C:\Users\Some One")
        self.assertEqual("not_user_install", auto.enable("vibe_meter")["error"])

    def test_enable_clears_an_earlier_deny(self):
        files = RunFiles("vibe_meter", self.auto.directory)
        files.mark_denied()
        self.assertTrue(self.auto.skipped("vibe_meter") is False)  # off: nothing to skip
        self.auto.enable("vibe_meter")
        self.assertFalse(files.denied())
        files.mark_denied()
        self.assertTrue(self.auto.skipped("vibe_meter"))

    def test_disable_keeps_the_launcher_when_the_registry_cant_be_read(self):
        self.auto.enable("vibe_meter")

        class Unreadable(FakeRegistry):
            def get(self, name):
                raise OSError("read failed")

        broken = Unreadable()
        broken.values = self.registry.values
        auto = make(self.tmp.name, registry=broken)
        auto.disable("vibe_meter")
        self.assertTrue(auto.launcher_path.exists())

    def test_a_failed_registry_write_removes_the_launcher_it_wrote(self):
        class Broken(FakeRegistry):
            def set(self, name, value):
                raise PermissionError("denied")
        auto = make(self.tmp.name, registry=Broken())
        self.assertFalse(auto.enable("vibe_meter")["ok"])
        self.assertFalse(auto.launcher_path.exists())

    def test_only_bundled_names(self):
        for bad in ("../x", "vibe_meter; calc", "C:\\x.py", "", None, 5, "VIBE_METER", "os"):
            with self.assertRaises(KeyError):
                self.auto.enable(bad)
            with self.assertRaises(KeyError):
                self.auto.disable(bad)
        self.assertEqual({}, self.registry.values)

    def test_not_windows_changes_nothing(self):
        auto = make(self.tmp.name, platform="linux", registry=self.registry)
        for result in (auto.enable("vibe_meter"), auto.disable("vibe_meter")):
            self.assertFalse(result["ok"])
            self.assertEqual("not_supported", result["error"])
            self.assertIn("Windows", result["message"])
        self.assertEqual({}, self.registry.values)
        self.assertFalse(auto.launcher_path.exists())
        self.assertFalse(any(auto.states().values()))

    def test_a_too_long_command_is_refused(self):
        auto = make(self.tmp.name, registry=self.registry, python="C:\\" + "p" * 300 + "\\pythonw.exe")
        result = auto.enable("vibe_meter")
        self.assertFalse(result["ok"])
        self.assertEqual("too_long", result["error"])
        self.assertEqual({}, self.registry.values)

    def test_other_values_in_the_run_key_are_left_alone(self):
        self.registry.values["OneDrive"] = "onedrive.exe /background"
        self.registry.values["BusyMeter"] = '"BusyMeter.exe" --autostart'  # the tray app's own entry
        self.auto.enable("vibe_meter")
        self.auto.disable("vibe_meter")
        self.assertEqual({"OneDrive": "onedrive.exe /background", "BusyMeter": '"BusyMeter.exe" --autostart'},
                         self.registry.values)

    def test_the_launcher_goes_when_no_recipe_uses_it(self):
        self.auto.enable("vibe_meter")
        self.auto.enable("focus_timer")
        self.auto.disable("vibe_meter")
        self.assertTrue(self.auto.launcher_path.exists())
        self.auto.disable("focus_timer")
        self.assertFalse(self.auto.launcher_path.exists())

    def test_registry_errors_are_reported(self):
        class Broken(FakeRegistry):
            def set(self, name, value):
                raise PermissionError("denied")
        auto = make(self.tmp.name, registry=Broken())
        result = auto.enable("vibe_meter")
        self.assertFalse(result["ok"])
        self.assertEqual("registry", result["error"])

    def test_without_pythonw_it_uses_python_and_the_cli_hides_itself(self):
        auto = make(self.tmp.name, registry=self.registry, python=r"C:\Py\python.exe")
        self.assertTrue(auto.command("vibe_meter").startswith(r"C:\Py\python.exe -E"))


class SignInStartTests(unittest.TestCase):
    """``busymeter run <recipe> --at-sign-in`` (what the launcher runs)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_running_copy_means_no_second_one_and_its_log_is_kept(self):
        files = RunFiles("vibe_meter", self.dir)
        files.write("running", "running")  # this process: alive
        files.log_path.write_text("earlier\n", encoding="utf-8")
        started, ran = [], []
        code = recipes_cli.sign_in_start("vibe_meter", directory=self.dir, executable=r"C:\Py\pythonw.exe",
                                         starter=lambda n, d=None: started.append(n),
                                         runner=lambda n: ran.append(n) or 0)
        self.assertEqual(EXIT_ALREADY, code)
        self.assertEqual([], started)
        self.assertEqual([], ran)
        self.assertEqual("earlier\n", files.log_path.read_text(encoding="utf-8"))

    def test_under_pythonw_it_runs_in_place_with_a_fresh_log(self):
        files = RunFiles("vibe_meter", self.dir)
        files.log_path.write_text("old run\n", encoding="utf-8")
        ran = []
        code = recipes_cli.sign_in_start("vibe_meter", directory=self.dir, executable=r"C:\Py\pythonw.exe",
                                         starter=lambda n, d=None: self.fail("no second process"),
                                         runner=lambda n: ran.append(n) or 0)
        self.assertEqual(0, code)
        self.assertEqual(["vibe_meter"], ran)
        self.assertEqual("", files.log_path.read_text(encoding="utf-8"))

    def test_under_console_python_it_hands_off_to_a_hidden_process(self):
        started = []
        code = recipes_cli.sign_in_start("vibe_meter", directory=self.dir, executable=r"C:\Py\python.exe",
                                         starter=lambda n, d=None: started.append(n) or 4242,
                                         runner=lambda n: self.fail("not in the console"))
        self.assertEqual(0, code)
        self.assertEqual(["vibe_meter"], started)

    def test_after_a_deny_it_skips_and_says_why(self):
        files = RunFiles("vibe_meter", self.dir)
        files.mark_denied()
        code = recipes_cli.sign_in_start("vibe_meter", directory=self.dir, executable=r"C:\Py\pythonw.exe",
                                         starter=lambda n, d=None: self.fail("no start after a Deny"),
                                         runner=lambda n: self.fail("no run after a Deny"))
        self.assertEqual(EXIT_DENIED, code)
        log = files.log_path.read_text(encoding="utf-8")
        self.assertIn("Deny", log)
        self.assertIn("busymeter autostart vibe_meter on", log)

    def test_the_flag_is_hidden_and_takes_only_bundled_names(self):
        from busymeter.cli import main
        with mock.patch.object(recipes_cli, "sign_in_start", return_value=0) as start:
            self.assertEqual(0, main(["run", "focus_timer", "--at-sign-in"]))
        start.assert_called_once_with("focus_timer")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            main(["run", "../x", "--at-sign-in"])


class ManagerAutostartTests(Base):
    def setUp(self):
        super().setUp()
        self.registry = FakeRegistry()
        self.manager.autostart = Autostart(self.dir, registry=self.registry, platform="win32",
                                           python=r"C:\Py\pythonw.exe", restrict=False, profile=PROFILE)

    def test_a_deny_shows_as_skipped_until_turned_on_again(self):
        self.tools.set_recipe_autostart("vibe_meter", True)
        RunFiles("vibe_meter", self.dir).mark_denied()
        listed = {r["name"]: r for r in self.tools.list_recipes()["recipes"]}
        self.assertTrue(listed["vibe_meter"]["startsAtSignIn"])
        self.assertTrue(listed["vibe_meter"]["signInSkipped"])
        self.assertFalse(listed["focus_timer"]["signInSkipped"])
        result = self.tools.set_recipe_autostart("vibe_meter", True)
        self.assertIn("if you click Deny it won't start at sign-in again until you turn it on again",
                      result["message"])
        listed = {r["name"]: r for r in self.tools.list_recipes()["recipes"]}
        self.assertFalse(listed["vibe_meter"]["signInSkipped"])

    def test_the_cli_list_shows_a_skip(self):
        import io
        from contextlib import redirect_stdout
        self.manager.autostart.enable("game_state")
        RunFiles("game_state", self.dir).mark_denied()
        out = io.StringIO()
        with redirect_stdout(out):
            recipes_cli.list_autostart(self.manager.autostart)
        line = [l for l in out.getvalue().splitlines() if l.startswith("game_state")][0]
        self.assertIn("Deny", line)

    def test_list_shows_starts_at_sign_in(self):
        listed = self.tools.list_recipes()
        self.assertTrue(all(r["startsAtSignIn"] is False for r in listed["recipes"]))
        self.assertNotIn("don't start at sign-in", listed["message"])
        self.assertIn("set_recipe_autostart", listed["message"])
        self.tools.set_recipe_autostart("vibe_meter", True)
        listed = {r["name"]: r for r in self.tools.list_recipes()["recipes"]}
        self.assertTrue(listed["vibe_meter"]["startsAtSignIn"])
        self.assertFalse(listed["focus_timer"]["startsAtSignIn"])

    def test_on_says_every_sign_in_and_the_first_allow(self):
        result = self.tools.set_recipe_autostart("vibe_meter", True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["startsAtSignIn"])
        self.assertIn("It will start every time you sign in to Windows", result["message"])
        self.assertIn("Allow", result["message"])
        self.assertEqual([], self.launched)  # turning it on doesn't start it now
        self.assertTrue((self.dir / "vibe_meter.json").exists())  # its settings are written (installed)

    def test_off_says_it_wont_any_more(self):
        self.tools.set_recipe_autostart("focus_timer", True)
        result = self.tools.set_recipe_autostart("focus_timer", False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["startsAtSignIn"])
        self.assertIn("won't start at sign-in any more", result["message"])
        self.assertEqual({}, self.registry.values)

    def test_off_when_it_was_off(self):
        result = self.tools.set_recipe_autostart("focus_timer", False)
        self.assertTrue(result["ok"])
        self.assertIn("doesn't start at sign-in", result["message"])

    def test_only_bundled_names(self):
        for bad in ("../x", "C:\\evil.py", "import os", None, 3):
            result = self.tools.set_recipe_autostart(bad, True)
            self.assertFalse(result["ok"])
            self.assertEqual("unknown_recipe", result["error"])
        self.assertEqual({}, self.registry.values)

    def test_not_windows(self):
        self.manager.autostart = Autostart(self.dir, registry=self.registry, platform="darwin", restrict=False)
        result = self.tools.set_recipe_autostart("vibe_meter", True)
        self.assertFalse(result["ok"])
        self.assertEqual("not_supported", result["error"])
        self.assertEqual({}, self.registry.values)

    def test_uninstall_removes_the_entry(self):
        self.tools.install_recipe("vibe_meter")
        self.tools.set_recipe_autostart("vibe_meter", True)
        self.tools.set_recipe_autostart("focus_timer", True)
        result = self.tools.uninstall_recipe("vibe_meter")
        self.assertTrue(result["ok"])
        self.assertIn("sign-in", result["message"])
        self.assertEqual(["Busy Meter recipe focus_timer"], list(self.registry.values))

    def test_install_says_whether_it_starts_at_sign_in(self):
        result = self.tools.install_recipe("vibe_meter")
        self.assertIn("set_recipe_autostart", result["message"])
        self.tools.stop_recipe("vibe_meter")
        self.tools.set_recipe_autostart("vibe_meter", True)
        result = self.tools.install_recipe("vibe_meter")
        self.assertIn("every time you sign in", result["message"])


class DefaultsTests(unittest.TestCase):
    def test_the_default_registry_is_only_opened_when_used(self):
        with mock.patch.object(autostart, "WindowsRegistry") as cls:
            auto = Autostart()
            Manager(autostart=auto)
            Tools(client_factory=lambda pid: None)
        cls.assert_not_called()

    def test_the_real_key_is_the_per_user_run_key(self):
        self.assertEqual(r"Software\Microsoft\Windows\CurrentVersion\Run", autostart.RUN_KEY)

    def test_the_suite_guard_refuses_the_real_run_key(self):
        # tests/__init__.py: any test that reaches the default registry fails instead of reading or writing HKCU Run.
        with self.assertRaises(AssertionError):
            Autostart(platform="win32").registry
        with self.assertRaises(AssertionError):
            Autostart(platform="win32").is_on("vibe_meter")


class NoConsoleWindowTests(unittest.TestCase):
    """At sign-in nothing may flash a console: every helper process is started with CREATE_NO_WINDOW on Windows."""

    def test_whoami_and_icacls_have_no_window(self):
        from busymeter import tokens

        calls = []

        def fake_run(command, **kwargs):
            calls.append(kwargs)
            out = 'x,"S-1-5-21-1-2-3-1001"' if "whoami" in command[0].lower() else ""
            return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tokens, "sys") as fake_sys, \
                mock.patch.object(tokens.subprocess, "run", side_effect=fake_run):
            fake_sys.platform = "win32"
            (Path(tmp) / "f.json").write_text("{}", encoding="utf-8")
            tokens.restrict_windows_dir(Path(tmp))
        self.assertEqual(3, len(calls))  # whoami, icacls folder, icacls files
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        for kwargs in calls:
            self.assertEqual(no_window, kwargs.get("creationflags", 0) & no_window, kwargs)


@unittest.skipUnless(sys.platform == "win32", "Windows registry")
class WindowsRegistryTests(unittest.TestCase):
    """A throwaway HKCU\\Software\\BusyMeterTest-<random> key, deleted afterwards (never the Run key)."""

    def test_set_get_delete_names(self):
        import winreg

        path = f"Software\\BusyMeterTest-{uuid.uuid4().hex}"
        self.addCleanup(lambda: _delete_key(winreg, path))
        reg = autostart.WindowsRegistry(path)
        self.assertEqual([], reg.names())
        self.assertIsNone(reg.get("Busy Meter recipe vibe_meter"))
        reg.set("Busy Meter recipe vibe_meter", '"C:\\a b\\pythonw.exe" -E x vibe_meter')
        self.assertEqual('"C:\\a b\\pythonw.exe" -E x vibe_meter', reg.get("Busy Meter recipe vibe_meter"))
        self.assertEqual(["Busy Meter recipe vibe_meter"], reg.names())
        self.assertTrue(reg.delete("Busy Meter recipe vibe_meter"))
        self.assertFalse(reg.delete("Busy Meter recipe vibe_meter"))
        self.assertIsNone(reg.get("Busy Meter recipe vibe_meter"))


def _delete_key(winreg, path):
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()
