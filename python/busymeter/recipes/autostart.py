"""Start a bundled recipe at sign-in: ``busymeter autostart <recipe> on|off``, or the MCP tool
``set_recipe_autostart``.

Windows only, off by default, per recipe. "On" writes one per-user value, ``Busy Meter recipe <recipe>``, under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``; "off" removes it. Nothing else in the Run key is read or
changed. The value runs a small fixed launcher, ``sign-in.pyw`` in the recipes folder (user-only), with this
environment's ``pythonw.exe`` (no console window)::

    "C:\\...\\pythonw.exe" -E -P "C:\\Users\\...\\busymeter-plugins\\recipes\\sign-in.pyw" vibe_meter

The launcher exists because Windows limits a Run value to 260 characters, too short for the path setup that
``control.start_background`` passes inline. It moves to the recipes folder, refuses a package folder outside the
user's profile, does the same path setup (no current or launcher folder on sys.path, the package folder before
site-packages), logs a failed import to ``<recipe>.log``, and calls ``busymeter run <recipe> --at-sign-in``. That takes
only bundled names, starts nothing if the recipe is already running or its last request was answered with Deny, and
otherwise waits for the app like any recipe (``waiting_for_app``) and asks for Allow in the tray the first time.

Only a package installed for this user (under %USERPROFILE%: ``pip install --user``, a per-user Python, or a folder
of the user's own) can be set to start at sign-in, so no other account can change the code that runs at this user's
sign-in. Environment overrides set only in a shell (``BUSYMETER_RECIPES_DIR``, ``BUSYMETER_TOKEN_DIR``) aren't seen at
sign-in: a sign-in start uses the default per-user folders unless those variables are set in the user's Windows
environment. The launcher is removed when no recipe starts at sign-in any more.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

from . import RECIPES
from .control import RunFiles, package_root, python_for_background
from .settings import recipes_dir
from ..tokens import ensure_private_dir

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_PREFIX = "Busy Meter recipe "
LAUNCHER_NAME = "sign-in.pyw"
MAX_COMMAND = 260  # Windows' documented limit for a Run value's command line

NOT_SUPPORTED = "Starting recipes at sign-in works on Windows only; nothing was changed."
NOT_USER_INSTALL = ("Starting at sign-in needs the busymeter package installed for your user, but it is in {root}, "
                    "outside your user folder ({profile}). Install it with `pip install --user busymeter` first "
                    "(\"busymeter[mcp]\" for the assistant tools), then turn this on again. Nothing was changed.")

# The launcher's fixed code. Only the package folder (a repr()'d string) is filled in; the recipe name comes from the
# command line and is checked by the CLI (bundled names only). Nothing is imported before the path clean-up but os and
# sys, which Python has loaded already.
LAUNCHER = '''\
# Busy Meter: starts a bundled recipe at sign-in. Written by `busymeter autostart`; removed when no recipe uses it.
import os, sys
ROOT = {root}
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if p not in ("", ".") and os.path.normcase(os.path.abspath(p)) != os.path.normcase(_here)]
_name = sys.argv[1] if len(sys.argv) > 1 else ""
_safe = 0 < len(_name) <= 40 and _name.replace("_", "").isalpha() and _name.isascii() and _name.islower()
_log = os.path.join(_here, (_name if _safe else "sign-in") + ".log")


def _say(text):
    try:
        with open(_log, "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\\n")
    except OSError:
        pass


os.chdir(_here)
_profile = os.path.normcase(os.path.abspath(os.environ.get("USERPROFILE") or os.path.expanduser("~")))
_root = os.path.normcase(os.path.abspath(ROOT))
try:
    _inside = os.path.commonpath([_profile, _root]) == _profile
except ValueError:
    _inside = False
if not _inside:
    _say("Not started at sign-in: the busymeter package (" + ROOT + ") is outside your user folder. Install it with "
         "pip install --user busymeter, then run: busymeter autostart " + (_name if _safe else "<recipe>") + " on")
    sys.exit(6)
sys.path.insert(next((i for i, p in enumerate(sys.path) if "site-packages" in p.lower()), len(sys.path)), ROOT)
try:
    from busymeter.cli import main
except Exception:
    import traceback
    _say("Not started at sign-in: the busymeter package didn't load (reinstall it with pip install --user busymeter, "
         "then turn autostart on again).\\n" + traceback.format_exc())
    sys.exit(7)
sys.exit(main(['run', _name, '--at-sign-in']))
'''


class Registry(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> bool: ...
    def names(self) -> list[str]: ...


class WindowsRegistry:
    """String values under one HKEY_CURRENT_USER key (the Run key unless a test passes a throwaway one)."""

    def __init__(self, path: str = RUN_KEY):
        import winreg

        self._winreg = winreg
        self.path = path

    def _open(self, write: bool) -> Any:
        w = self._winreg
        if write:
            return w.CreateKeyEx(w.HKEY_CURRENT_USER, self.path, 0, w.KEY_SET_VALUE | w.KEY_QUERY_VALUE)
        return w.OpenKey(w.HKEY_CURRENT_USER, self.path, 0, w.KEY_QUERY_VALUE)

    def get(self, name: str) -> str | None:
        try:
            with self._open(False) as key:
                value, _kind = self._winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> None:
        with self._open(True) as key:
            self._winreg.SetValueEx(key, name, 0, self._winreg.REG_SZ, value)

    def delete(self, name: str) -> bool:
        try:
            with self._open(True) as key:
                self._winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return False
        return True

    def names(self) -> list[str]:
        out = []
        try:
            with self._open(False) as key:
                i = 0
                while True:
                    try:
                        out.append(self._winreg.EnumValue(key, i)[0])
                    except OSError:
                        break
                    i += 1
        except FileNotFoundError:
            pass
        return out


class FakeRegistry:
    """In-memory stand-in for tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> bool:
        return self.values.pop(name, None) is not None

    def names(self) -> list[str]:
        return list(self.values)


def _check(name: Any) -> str:
    if not isinstance(name, str) or name not in RECIPES:
        raise KeyError(name)
    return name


def inside(child: Path | str, parent: Path | str) -> bool:
    """True when ``child`` is ``parent`` or below it (whole path parts: C:\\Users\\Al2 isn't inside C:\\Users\\Al)."""
    c = os.path.normcase(os.path.abspath(str(child)))
    p = os.path.normcase(os.path.abspath(str(parent)))
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


class Autostart:
    def __init__(self, directory: Path | str | None = None, *, registry: Registry | None = None,
                 platform: str | None = None, python: str | None = None, restrict: bool = True,
                 profile: str | None = None, root: Path | str | None = None):
        self._directory = Path(directory) if directory is not None else None
        self._registry = registry
        self.platform = platform or sys.platform
        self._python = python
        self.restrict = restrict
        self._profile = profile
        self._root = Path(root) if root is not None else None

    @property
    def supported(self) -> bool:
        return self.platform == "win32"

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = WindowsRegistry()  # opened only when first used
        return self._registry

    @property
    def directory(self) -> Path:
        return self._directory if self._directory is not None else recipes_dir()

    @property
    def launcher_path(self) -> Path:
        return self.directory / LAUNCHER_NAME

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else package_root()

    @property
    def profile(self) -> str:
        return self._profile or os.environ.get("USERPROFILE") or str(Path.home())

    @staticmethod
    def value_name(name: str) -> str:
        return VALUE_PREFIX + _check(name)

    def command(self, name: str) -> str:
        """The exact Run value: pythonw (or python) -E [-P] "<launcher>" <recipe>, quoted for spaces."""
        _check(name)
        python = self._python or python_for_background()
        flags = ["-E"] + (["-P"] if sys.version_info >= (3, 11) else [])
        return subprocess.list2cmdline([python, *flags, str(self.launcher_path), name])

    def _exists(self, name: str) -> bool:
        """Whether the entry exists; OSError when the registry can't be read (callers decide what that means)."""
        return self.registry.get(self.value_name(name)) is not None

    def is_on(self, name: str) -> bool:
        _check(name)
        if not self.supported:
            return False
        try:
            return self._exists(name)
        except OSError:
            return False

    def skipped(self, name: str) -> bool:
        """On, but its last request was answered with Deny: sign-in starts skip it until it's turned on again."""
        return self.is_on(name) and RunFiles(name, self.directory).denied()

    def states(self) -> dict[str, bool]:
        return {name: self.is_on(name) for name in RECIPES}

    def user_install_problem(self) -> str | None:
        if inside(self.root, self.profile):
            return None
        return NOT_USER_INSTALL.format(root=self.root, profile=self.profile)

    def enable(self, name: str) -> dict[str, Any]:
        _check(name)
        if not self.supported:
            return {"ok": False, "error": "not_supported", "message": NOT_SUPPORTED}
        problem = self.user_install_problem()
        if problem:
            return {"ok": False, "error": "not_user_install", "message": problem}
        command = self.command(name)
        if len(command) > MAX_COMMAND:
            return {"ok": False, "error": "too_long",
                    "message": f"The start command would be {len(command)} characters; Windows allows "
                               f"{MAX_COMMAND} for a sign-in entry. Nothing was changed."}
        had_launcher = self.launcher_path.exists()
        try:
            self._write_launcher()
            self.registry.set(self.value_name(name), command)
        except OSError as e:
            if not had_launcher:
                try:
                    self.launcher_path.unlink()
                except OSError:
                    pass
            return {"ok": False, "error": "registry", "message": f"Couldn't save the sign-in entry: {e}"}
        RunFiles(name, self.directory).clear_denied()  # turning it on again is the answer to an earlier Deny
        return {"ok": True, "on": True, "command": command, "valueName": self.value_name(name)}

    def disable(self, name: str) -> dict[str, Any]:
        _check(name)
        if not self.supported:
            return {"ok": False, "error": "not_supported", "message": NOT_SUPPORTED}
        try:
            was = self.registry.delete(self.value_name(name))
        except OSError as e:
            return {"ok": False, "error": "registry", "message": f"Couldn't remove the sign-in entry: {e}"}
        try:
            remaining = any(self._exists(other) for other in RECIPES)
        except OSError:
            remaining = True  # can't tell: keep the launcher (a missing one would break a remaining entry)
        if not remaining:
            try:
                self.launcher_path.unlink()
            except OSError:
                pass
        return {"ok": True, "on": False, "wasOn": was, "valueName": self.value_name(name)}

    def _write_launcher(self) -> None:
        ensure_private_dir(self.directory, restrict=self.restrict)
        text = LAUNCHER.format(root=repr(str(self.root)))
        path = self.launcher_path
        tmp = path.with_name(f".tmp-{LAUNCHER_NAME}")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
