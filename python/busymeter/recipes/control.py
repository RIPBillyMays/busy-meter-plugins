"""Running recipes in the background, finding them, and stopping them.

Each running recipe keeps three small files next to its settings in the recipes folder (user-only, see settings.py):

- ``<recipe>.run.json``: pid, state (starting, waiting_for_app, waiting_for_allow, running, switched_off, stopped), a
  short message, and a heartbeat the runner rewrites every 2 s. A recipe counts as running while the state isn't
  "stopped", the heartbeat is under 15 s old and the process is still there.
- ``<recipe>.stop``: written to ask it to stop. The runner sees it within ~2 s, clears its signals and exits.
- ``<recipe>.log``: what it printed (background runs only; started fresh each run; never holds a password or token).

So a background recipe is stopped with ``busymeter stop <recipe>``, the MCP tool ``stop_recipe``, by creating the stop
file by hand, or by ending its ``pythonw.exe`` in Task Manager (then its signals expire within 30 s on their own).
Starting at sign-in (opt-in, per recipe) is in autostart.py.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import RECIPES
from .settings import _replace, recipes_dir
from ..tokens import ensure_private_dir

HEARTBEAT_SECONDS = 2.0
STALE_SECONDS = 15.0


class RunFiles:
    def __init__(self, name: str, directory: Path | str | None = None):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{1,40}", name):  # a file name, never a path
            raise KeyError(name)
        self.name = name
        self.directory = Path(directory) if directory is not None else recipes_dir()

    @property
    def run_path(self) -> Path:
        return self.directory / f"{self.name}.run.json"

    @property
    def stop_path(self) -> Path:
        return self.directory / f"{self.name}.stop"

    @property
    def log_path(self) -> Path:
        return self.directory / f"{self.name}.log"

    def read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.run_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, state: str, message: str = "", started: float | None = None) -> None:
        ensure_private_dir(self.directory, restrict=False)  # already narrowed when the settings file was written
        data = {"recipe": self.name, "pid": os.getpid(), "state": state, "message": message,
                "startedAt": started or time.time(), "heartbeat": time.time()}
        self._write_data(data)

    def _write_data(self, data: dict[str, Any]) -> None:
        # A unique temp name per write: the heartbeat thread and the main loop both write, and a shared name let one
        # writer replace the other's half-written file (the flaky "running after stopped" seen in the suite).
        tmp = self.run_path.with_name(f".tmp-{self.name}-{uuid.uuid4().hex}.run")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            _replace(str(tmp), self.run_path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    @property
    def denied_path(self) -> Path:
        return self.directory / f"{self.name}.denied"

    def denied(self) -> bool:
        """True when this recipe's last request was answered with Deny (a sign-in start then skips it)."""
        return self.denied_path.exists()

    def mark_denied(self) -> None:
        try:
            ensure_private_dir(self.directory, restrict=False)
            self.denied_path.write_text("denied\n", encoding="utf-8")
        except OSError:
            pass

    def clear_denied(self) -> None:
        try:
            self.denied_path.unlink()
        except OSError:
            pass

    def status(self, now: float | None = None) -> dict[str, Any]:
        """{"running": bool, "state": ..., "message": ..., "pid": ...} from the run file."""
        data = self.read()
        if data is None:
            return {"running": False, "state": "not_running"}
        now = time.time() if now is None else now
        fresh = isinstance(data.get("heartbeat"), (int, float)) and now - data["heartbeat"] < STALE_SECONDS
        pid = data.get("pid")
        running = data.get("state") != "stopped" and fresh and isinstance(pid, int) and pid_alive(pid)
        out = {"running": running, "state": data.get("state") if running or data.get("state") == "stopped" else "not_running",
               "message": data.get("message") or "", "pid": pid if running else None}
        return out

    def write_stopped_for(self, pid: Any, message: str) -> None:
        data = {"recipe": self.name, "pid": pid, "state": "stopped", "message": message, "heartbeat": time.time()}
        self._write_data(data)

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def request_stop(self) -> None:
        ensure_private_dir(self.directory, restrict=False)
        self.stop_path.write_text("stop\n", encoding="utf-8")

    def clear_stop(self) -> None:
        try:
            self.stop_path.unlink()
        except FileNotFoundError:
            pass

    def delete_all(self) -> None:
        for path in (self.run_path, self.stop_path, self.log_path, self.denied_path):
            try:
                path.unlink()
            except (FileNotFoundError, PermissionError):
                pass


def pid_alive(pid: int) -> bool:
    """True when a process with this id exists (best effort; never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists (another user's)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def package_root() -> Path:
    """The folder that holds the busymeter package (put on the child's PYTHONPATH for runs from a checkout)."""
    return Path(__file__).resolve().parent.parent.parent


def python_for_background() -> str:
    """pythonw.exe next to this Python on Windows (no console window), else this Python."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        candidate = exe.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


# Run by the background Python before anything else is imported: drops the current folder from sys.path (also on
# 3.10, which has no -P) and adds the busymeter package's folder (sys.argv[1]) after the standard library but before
# the first site-packages folder, so nothing in it can shadow a standard module while a checkout still wins over an
# older installed copy.
PATH_SETUP = ("import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
              "sys.path.insert(next((i for i, p in enumerate(sys.path) if 'site-packages' in p.lower()), "
              "len(sys.path)), sys.argv[1])")
BOOTSTRAP = PATH_SETUP + "; from busymeter.cli import main; sys.exit(main(['run', sys.argv[2], '--background']))"


def background_command(name: str) -> list[str]:
    """``pythonw -E [-P] -c BOOTSTRAP <package folder> <recipe>``: -E ignores PYTHON* variables (PYTHONPATH,
    PYTHONSTARTUP...), -P (3.11+) and the bootstrap keep the current folder off sys.path, so no planted module
    (e.g. a json.py next to it) can be imported in place of the real one."""
    if name not in RECIPES:
        raise KeyError(name)
    flags = ["-E"] + (["-P"] if sys.version_info >= (3, 11) else [])
    return [python_for_background(), *flags, "-c", BOOTSTRAP, str(package_root()), name]


def start_background(name: str, directory: Path | str | None = None,
                     popen: Callable[..., Any] = subprocess.Popen) -> int:
    """Start a bundled recipe as a detached background process for this user. Returns its pid.

    Its working folder is the recipes folder (user-only), never the home folder or wherever the caller was."""
    command = background_command(name)
    folder = Path(directory) if directory is not None else recipes_dir()
    ensure_private_dir(folder)
    env = dict(os.environ)
    env["BUSYMETER_RECIPES_DIR"] = str(folder)
    # The log starts fresh here and takes the child's stderr, so a crash before the runner starts (an import error,
    # a broken install) is in <recipe>.log too; the runner appends to it.
    log = open(RunFiles(name, folder).log_path, "wb")
    try:
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": log,
                                  "close_fds": True, "env": env, "cwd": str(folder)}
        if sys.platform == "win32":
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
            breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: keep running when the MCP client's job ends
            try:
                process = popen(command, creationflags=flags | breakaway, **kwargs)
            except OSError:
                process = popen(command, creationflags=flags, **kwargs)  # the job doesn't allow breaking away
        else:
            process = popen(command, start_new_session=True, **kwargs)
    finally:
        log.close()
    return process.pid


def stop(name: str, directory: Path | str | None = None, wait: float = 8.0,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Ask a running recipe to stop (it clears its signals); end the process if it doesn't within ``wait`` s."""
    files = RunFiles(name, directory)
    status = files.status()
    if not status["running"]:
        files.clear_stop()
        return {"stopped": False, "wasRunning": False, "message": f"{name} isn't running."}
    files.request_stop()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not files.status()["running"]:
            files.clear_stop()
            return {"stopped": True, "wasRunning": True, "message": f"Stopped {name}; it cleared its signals."}
        sleep(0.2)
    pid = status.get("pid")
    current = files.read() or {}
    if isinstance(pid, int) and current.get("pid") == pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess
        except OSError:
            pass
    files.clear_stop()
    try:
        files.write_stopped_for(pid, "Ended (it didn't stop in time); its signals expire within 30 s.")
    except OSError:
        pass
    return {"stopped": True, "wasRunning": True,
            "message": f"Ended {name} (it didn't stop in time); its signals expire within 30 s."}

