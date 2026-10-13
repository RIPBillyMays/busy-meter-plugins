"""Where plugin tokens are kept: one small JSON file per plugin id, readable by the current user only.

Default folder (override with the BUSYMETER_TOKEN_DIR environment variable):

- Windows: ``%LOCALAPPDATA%\\busymeter-plugins\\tokens``. Before every write the folder (also one named by
  BUSYMETER_TOKEN_DIR) loses its inherited permissions and grants only the current user, by SID from
  ``whoami /user`` (``icacls``); entries the folder already had explicitly (SYSTEM, Administrators) stay. If that fails a warning says so and the file is still written.
- macOS/Linux: ``$XDG_CONFIG_HOME/busymeter-plugins/tokens`` (else ``~/.config/...``); the folder is set to 0700
  (also when it already existed with looser modes), files 0600.

A file looks like ``{"pluginId": "...", "token": "...", "signals": [...], "wants": [...]}``, plus
``pendingRequestId`` while a registration waits for the user. The token is a bearer secret: anyone who reads it can
drive this plugin's signals until the user revokes it under Settings > Sources > External connections.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any

_ID = re.compile(r"^[a-z0-9.-]{3,64}$")


def default_token_dir() -> Path:
    override = os.environ.get("BUSYMETER_TOKEN_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "busymeter-plugins" / "tokens"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "busymeter-plugins" / "tokens"


class PermissionWarning(UserWarning):
    """The token folder couldn't be narrowed to the current user."""


def _system32(exe: str) -> str:
    """Full path, so a same-named program earlier on PATH (e.g. Git's whoami) isn't run instead."""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", exe)


def _no_window() -> dict[str, int]:
    """Under pythonw.exe (background and sign-in starts) a console program would open a window: never show one."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def current_user_sid() -> str | None:
    """The current user's SID from ``whoami /user`` (Windows), or None."""
    try:
        out = subprocess.run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"], check=False, capture_output=True,
                             text=True, timeout=15, **_no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    match = re.search(r'"(S-1-[0-9-]+)"', out.stdout)
    return match.group(1) if match else None


def restrict_windows_dir(path: Path) -> bool:
    """Remove inherited ACEs and grant only the current user (by SID) full control. False (and a warning) on failure."""
    sid = current_user_sid()
    if sid is None:
        warnings.warn(f"Couldn't find the current user's SID; {path} keeps its inherited permissions.", PermissionWarning,
                      stacklevel=3)
        return False
    # The folder: no inherited entries, only the user (inherited by files and folders created in it). Files already
    # there are reset so they inherit exactly that (never /T with the folder's grant: on files it leaves no access).
    commands = [[_system32("icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "/Q"]]
    if any(path.iterdir()):
        commands.append([_system32("icacls.exe"), str(path / "*"), "/reset", "/C", "/Q"])
    for command in commands:
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30, **_no_window())
        except (OSError, subprocess.SubprocessError) as e:
            warnings.warn(f"Couldn't narrow the permissions of {path}: {e}", PermissionWarning, stacklevel=3)
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            warnings.warn(f"Couldn't narrow the permissions of {path} (icacls {result.returncode}): {detail}",
                          PermissionWarning, stacklevel=3)
            return False
    return True


def ensure_private_dir(directory: Path, restrict: bool = True) -> None:
    """Create ``directory`` if needed and narrow it to the current user (also the recipes folder uses this)."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not restrict:
        return
    if sys.platform == "win32":
        restrict_windows_dir(directory)
    else:
        mode = directory.stat().st_mode & 0o777
        if mode != 0o700:
            os.chmod(directory, 0o700)


class TokenStore:
    """Per-user token files. ``directory`` defaults to :func:`default_token_dir`."""

    def __init__(self, directory: Path | str | None = None, restrict: bool = True):
        self.directory = Path(directory) if directory is not None else default_token_dir()
        self._restrict = restrict

    def path(self, plugin_id: str) -> Path:
        if not _ID.match(plugin_id or ""):
            raise ValueError("Not a plugin id.")
        return self.directory / f"{plugin_id}.json"

    def load(self, plugin_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path(plugin_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("pluginId") == plugin_id else None

    def save_pending(self, plugin_id: str, request_id: str, signals: list[dict[str, Any]], wants: list[str]) -> None:
        data = self.load(plugin_id) or {"pluginId": plugin_id}
        data.update({"pendingRequestId": request_id, "requestedSignals": signals, "requestedWants": wants,
                     "requestedAt": int(time.time())})
        self._write(plugin_id, data)

    def clear_pending(self, plugin_id: str) -> None:
        data = self.load(plugin_id)
        if data is None:
            return
        for key in ("pendingRequestId", "requestedSignals", "requestedWants", "requestedAt"):
            data.pop(key, None)
        if data.get("token"):
            self._write(plugin_id, data)
        else:
            self.delete(plugin_id)

    def save_token(self, plugin_id: str, token: str) -> None:
        data = self.load(plugin_id) or {"pluginId": plugin_id}
        if "requestedSignals" in data:
            data["signals"] = data.pop("requestedSignals")
        if "requestedWants" in data:
            data["wants"] = data.pop("requestedWants")
        data.pop("pendingRequestId", None)
        data.pop("requestedAt", None)
        data["token"] = token
        data["savedAt"] = int(time.time())
        self._write(plugin_id, data)

    def drop_token(self, plugin_id: str) -> None:
        """Remove the token but keep a waiting request (and what it asked for)."""
        data = self.load(plugin_id)
        if data is None or "token" not in data:
            return
        data.pop("token", None)
        data.pop("savedAt", None)
        if data.get("pendingRequestId"):
            self._write(plugin_id, data)
        else:
            self.delete(plugin_id)

    def delete(self, plugin_id: str) -> None:
        try:
            self.path(plugin_id).unlink()
        except FileNotFoundError:
            pass

    def plugin_ids(self) -> list[str]:
        try:
            return sorted(p.stem for p in self.directory.glob("*.json") if _ID.match(p.stem))
        except OSError:
            return []

    def ensure_dir(self) -> None:
        """Create the folder if needed and narrow it to the current user, every time (it may have been loosened)."""
        ensure_private_dir(self.directory, self._restrict)

    def _write(self, plugin_id: str, data: dict[str, Any]) -> None:
        self.ensure_dir()
        target = self.path(plugin_id)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.directory)  # created 0600 on POSIX
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            if sys.platform != "win32":
                os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
