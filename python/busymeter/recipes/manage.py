"""Install, stop, uninstall and configure bundled recipes (used by busymeter-mcp; no MCP import here).

Only names in busymeter.recipes.RECIPES are accepted: no paths, no code, no downloads. Installing writes the recipe's
default settings file and starts it as a background process for this user; it still has to be allowed by the user
in the Busy Meter tray, like any plugin. Nothing here can approve it. Starting at sign-in is opt-in per recipe
(set_recipe_autostart): one per-user Run value each (see autostart.py), removed again by uninstall_recipe.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .. import recipes as registry
from ..client import BusyMeter, BusyMeterError, signal_declarations
from . import control
from .autostart import Autostart
from .settings import SettingsError, SettingsFile, recipes_dir

ALLOW_MESSAGE = "Waiting for you to click Allow in the Busy Meter tray (the prompt names \"{title}\")."
REVOKE_NOTE = ("Busy Meter still lists it under Settings → Sources → External connections until you Revoke it "
               "there.")


def _unknown(name: Any) -> dict[str, Any]:
    return {"ok": False, "error": "unknown_recipe",
            "message": f"No bundled recipe named {str(name)[:40]!r}. Bundled: {', '.join(registry.names())}."}


class Manager:
    def __init__(self, directory: Path | str | None = None, *, launcher: Callable[[str], int] | None = None,
                 client_factory: Callable[[str], Any] | None = None, restrict: bool = True,
                 stopper: Callable[..., dict[str, Any]] | None = None, settle: float = 5.0,
                 autostart: Autostart | None = None):
        self._directory = Path(directory) if directory is not None else None
        self._launcher = launcher
        self._client_factory = client_factory or (lambda pid: BusyMeter(pid))
        self.restrict = restrict
        self._stopper = stopper or control.stop
        self.settle = settle
        self._alive = control.pid_alive
        self.autostart = autostart or Autostart(self._directory, restrict=restrict)

    @property
    def directory(self) -> Path:
        return self._directory if self._directory is not None else recipes_dir()

    def _settings(self, name: str) -> SettingsFile:
        return SettingsFile(name, registry.get(name).schema, self.directory, restrict=self.restrict)

    def _launch(self, name: str) -> int:
        if self._launcher is not None:
            return self._launcher(name)
        return control.start_background(name, self._directory)

    # ----- tools -----

    def list_recipes(self) -> dict[str, Any]:
        out = []
        for name in registry.names():
            cls = registry.get(name)
            settings = self._settings(name)
            status = control.RunFiles(name, self.directory).status()
            out.append({
                "name": name, "title": cls.title, "summary": cls.summary, "reads": cls.reads,
                "neverReads": cls.never_reads, "pluginId": cls.plugin_id,
                "installed": settings.exists(), "running": status["running"], "state": status["state"],
                "startsAtSignIn": self.autostart.is_on(name), "signInSkipped": self.autostart.skipped(name),
                "message": status.get("message") or "",
            })
        return {"ok": True, "recipes": out,
                "message": "Bundled recipes only. install_recipe starts one in the background; the user clicks Allow "
                           "in the Busy Meter tray. startsAtSignIn: whether it starts every time the user signs in to "
                           "Windows (off unless turned on with set_recipe_autostart or `busymeter autostart`); "
                           "signInSkipped: on, but the user clicked Deny, so sign-in starts skip it until it's turned "
                           "on again."}

    def install_recipe(self, name: str) -> dict[str, Any]:
        if name not in registry.RECIPES:
            return _unknown(name)
        cls = registry.get(name)
        settings = self._settings(name)
        try:
            settings.ensure()
        except SettingsError as e:
            return {"ok": False, "error": "bad_settings", "message": f"{e} Fix or delete the file, then try again.",
                    "settingsFile": str(settings.path)}
        files = control.RunFiles(name, self.directory)
        status = files.status()
        if status["running"]:
            return {"ok": True, "state": status["state"], "alreadyRunning": True, "settingsFile": str(settings.path),
                    "message": f"{name} is already running ({status['state']}). {status.get('message') or ''}".strip()}
        try:
            files.run_path.unlink()  # a "stopped" note from an earlier run mustn't be read as this one's
        except OSError:
            pass
        try:
            pid = self._launch(name)
        except OSError as e:
            return {"ok": False, "error": "start_failed", "message": f"Couldn't start {name}: {e}"}
        deadline = time.monotonic() + self.settle
        current: dict[str, Any] = {"running": False, "state": "starting", "message": ""}
        while time.monotonic() < deadline:
            current = files.status()
            if current["state"] not in ("starting", "not_running"):
                break  # waiting_for_allow, running (a saved token), waiting_for_app, or stopped
            if current["state"] == "not_running" and not self._alive(pid):
                break  # gone before it wrote its run file
            time.sleep(0.1)
        if current["state"] == "not_running" and not self._alive(pid):
            return {"ok": False, "error": "start_failed", "settingsFile": str(settings.path),
                    "message": f"{name} exited before it started (process {pid}). What it printed is in "
                               f"{files.log_path}."}
        state = current["state"] if current["state"] != "not_running" else "starting"
        settings_note = f"Settings: {settings.path}."
        lifetime = (f"It runs in the background (process {pid}) until stopped with stop_recipe or "
                    f"`busymeter stop {name}`; " +
                    ("it also starts every time you sign in to Windows (set_recipe_autostart off stops that)."
                     if self.autostart.is_on(name) else
                     "it doesn't start again at sign-in unless that's turned on (set_recipe_autostart)."))
        if state == "stopped":
            return {"ok": False, "error": "stopped", "state": state, "settingsFile": str(settings.path),
                    "message": f"{name} stopped right away: {current.get('message') or 'see its log'}. "
                               f"Log: {files.log_path}."}
        if state == "running":
            notes = [f"Running: Busy Meter already allowed {cls.title} (saved approval), so there's nothing to click.",
                     lifetime, settings_note]
        elif state == "waiting_for_app":
            notes = ["Busy Meter isn't accepting connections yet: turn on Settings → Sources → External connections "
                     "in the app. The recipe keeps trying, then asks for Allow in the tray.", lifetime, settings_note]
        else:
            state = "waiting_for_allow"
            notes = [ALLOW_MESSAGE.format(title=cls.title), lifetime, settings_note]
        if name == "stream_status":
            notes.append("If OBS's WebSocket server has a password, the user puts it in \"password\" in that file "
                         "(it can't be set through the assistant).")
        if name == "game_state":
            notes.append("The game must post to http://127.0.0.1:<port>/ with the token from that file (shown with "
                         "`busymeter run game_state --show-token`; the assistant never sees it).")
        return {"ok": True, "state": state, "pid": pid, "settingsFile": str(settings.path), "message": " ".join(notes)}

    def stop_recipe(self, name: str) -> dict[str, Any]:
        if name not in registry.RECIPES:
            return _unknown(name)
        result = self._stopper(name, self.directory)
        return {"ok": True, **result}

    def uninstall_recipe(self, name: str) -> dict[str, Any]:
        if name not in registry.RECIPES:
            return _unknown(name)
        cls = registry.get(name)
        stopped = self._stopper(name, self.directory)
        cleared = False
        meter = self._client_factory(cls.plugin_id)
        try:
            if getattr(meter, "token", None):
                meter.clear()
                cleared = True
        except BusyMeterError:
            pass
        try:
            meter.forget()
        except (AttributeError, OSError):
            pass
        deleted = self._settings(name).delete()
        control.RunFiles(name, self.directory).delete_all()
        signin = self.autostart.disable(name) if self.autostart.supported else {}
        parts = [stopped.get("message", ""), "Its signals are cleared." if cleared or stopped.get("wasRunning") else "",
                 "Settings deleted." if deleted else "There was no settings file.",
                 "Its saved token is deleted.",
                 "It no longer starts at sign-in." if signin.get("wasOn") else
                 (signin.get("message", "") if signin.get("ok") is False else ""), REVOKE_NOTE]
        return {"ok": True, "uninstalled": name, "message": " ".join(p for p in parts if p)}

    def set_recipe_autostart(self, name: str, on: Any) -> dict[str, Any]:
        if not isinstance(name, str) or name not in registry.RECIPES:
            return _unknown(name)
        if not isinstance(on, bool):
            return {"ok": False, "error": "bad_request", "message": "on must be true or false."}
        cls = registry.get(name)
        if not on:
            result = self.autostart.disable(name)
            if not result["ok"]:
                return result
            message = (f"{name} won't start at sign-in any more." if result.get("wasOn") else
                       f"{name} doesn't start at sign-in (it was already off).")
            if control.RunFiles(name, self.directory).status()["running"]:
                message += " It keeps running now until stop_recipe."
            return {"ok": True, "recipe": name, "startsAtSignIn": False, "message": message}
        if not self.autostart.supported:
            return self.autostart.enable(name)  # "Windows only", before anything is written
        settings = self._settings(name)
        try:
            settings.ensure()  # the settings it will start with (also marks it installed)
        except SettingsError as e:
            return {"ok": False, "error": "bad_settings", "message": f"{e} Fix or delete the file, then try again.",
                    "settingsFile": str(settings.path)}
        result = self.autostart.enable(name)
        if not result["ok"]:
            return result
        running = control.RunFiles(name, self.directory).status()["running"]
        message = (f"It will start every time you sign in to Windows: {name} then runs in the background (no window) "
                   f"and waits for the Busy Meter app if it isn't up yet. If {cls.title} was never allowed, the first "
                   "run still needs you to click Allow in the Busy Meter tray; if you click Deny it won't start at "
                   "sign-in again until you turn it on again. "
                   + ("It's running now too." if running else
                      "It isn't running now; install_recipe starts it without waiting for the next sign-in.")
                   + f" Turn it off with set_recipe_autostart(on=false) or `busymeter autostart {name} off`.")
        return {"ok": True, "recipe": name, "startsAtSignIn": True, "settingsFile": str(settings.path),
                "message": message}

    def get_recipe_settings(self, name: str) -> dict[str, Any]:
        if name not in registry.RECIPES:
            return _unknown(name)
        cls = registry.get(name)
        settings = self._settings(name)
        installed = settings.exists()
        try:
            values = settings.load() if installed else cls.schema.defaults()
        except SettingsError as e:
            return {"ok": False, "error": "bad_settings", "message": str(e), "settingsFile": str(settings.path)}
        return {"ok": True, "recipe": name, "installed": installed, "settingsFile": str(settings.path),
                "values": cls.schema.public_view(values), "schema": cls.schema.describe(),
                "message": ("Current settings." if installed else "Not installed: these are the defaults.") +
                           " Secret fields are never shown; the user sets them in the settings file."}

    def set_recipe_settings(self, name: str, changes: Any) -> dict[str, Any]:
        if name not in registry.RECIPES:
            return _unknown(name)
        cls = registry.get(name)
        settings = self._settings(name)
        try:
            checked = cls.schema.check_agent_changes(changes)
        except SettingsError as e:
            return {"ok": False, "error": "bad_settings", "settingsFile": str(settings.path),
                    "message": f"{e} Nothing was changed." + (
                        f" Settings file: {settings.path}" if "settings file" in str(e) else "")}
        try:
            before = settings.load() if settings.exists() else cls.schema.defaults()
            after = settings.update(checked)
        except SettingsError as e:
            return {"ok": False, "error": "bad_settings", "settingsFile": str(settings.path),
                    "message": f"{e} Nothing was changed."}
        running = control.RunFiles(name, self.directory).status()["running"]
        message = "Saved."
        if running:
            message += " The running recipe re-reads it within a few seconds."
            if _declarations(cls, before) != _declarations(cls, after):
                message += (" Its signals or suggested levels changed, so it asks again in the Busy Meter tray: "
                            "click Allow there for the new levels to apply.")
        return {"ok": True, "recipe": name, "settingsFile": str(settings.path),
                "values": cls.schema.public_view(after), "message": message}


def _declarations(cls: Any, values: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return signal_declarations(cls(values).signals())
    except (ValueError, KeyError):
        return []
