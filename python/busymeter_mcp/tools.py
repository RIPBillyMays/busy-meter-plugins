"""The busymeter-mcp tools as plain functions (no MCP import), so they can be tested without a network or the SDK.

Every tool returns a dict with ``ok`` and a ``message`` meant to be relayed to the user. The agent can ask to
register; only the user can approve, by clicking Allow in the Busy Meter tray. Nothing here approves anything.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from busymeter.client import (
    MAX_TTL_SECONDS,
    TURN_ON_MESSAGE,
    ApprovalTimeoutError,
    BusyMeter,
    BusyMeterError,
    DeniedError,
    NotListeningError,
    normalize_level,
    parse_duration,
)
from busymeter.recipes.manage import Manager

DEFAULT_PLUGIN_ID = "com.thebusymeter.mcp"
DEFAULT_NAME = "AI assistant (busymeter-mcp)"
WAITING_MESSAGE = (
    "Waiting for you to click Allow in the Busy Meter tray. The prompt names this plugin; it is denied after "
    "2 minutes without an answer."
)
SOURCE_KINDS = ("camera", "microphone", "teams", "slack", "calendar", "plugin", "agents", "manual", "none")
LEVEL_SHORTCUTS = ("level-0", "level-1", "level-2", "level-3", "level-4", "level-5", "level-6", "level-flash")
MAX_WAIT_SECONDS = 110  # keep a tool call well under typical client timeouts; call again to keep waiting
REFRESH_TTL = 60


class Tools:
    """State shared by the tools: the plugin id, a client factory, and a refresher for holds over 10 minutes."""

    def __init__(self, plugin_id: str | None = None, client_factory: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic, recipes: Manager | None = None):
        self.plugin_id = plugin_id or os.environ.get("BUSYMETER_PLUGIN_ID") or DEFAULT_PLUGIN_ID
        self._factory = client_factory or (lambda pid: BusyMeter(pid))
        self.recipes = recipes or Manager(client_factory=client_factory)
        self._client: Any = None
        self._clock = clock
        self._hold: _Hold | None = None
        self._lock = threading.Lock()

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._factory(self.plugin_id)
        return self._client

    # ----- tools -----

    def get_status(self) -> dict[str, Any]:
        """Current level, the winning source's kind, and whether External connections is on."""
        meter = self.client
        if not meter.is_listening():
            return _fail("not_listening", TURN_ON_MESSAGE, externalConnections="off or app not running")
        if not meter.token:
            return {"ok": True, "externalConnections": "on", "registered": False,
                    "message": "External connections is on, but this assistant isn't registered yet. Call "
                               "register_plugin; you'll click Allow in the Busy Meter tray."}
        try:
            status = meter.status()
        except BusyMeterError as e:
            return _from_error(e, externalConnections="on", registered=e.code != "unauthorized")
        return {"ok": True, "externalConnections": "on", "registered": True, **status,
                "message": _describe(status)}

    def list_sources(self) -> dict[str, Any]:
        """What can be known about sources through the plugin API (it has no endpoint that lists them)."""
        meter = self.client
        result: dict[str, Any] = {
            "ok": True,
            "complete": False,
            "message": "The plugin API doesn't list the app's sources or level mappings (on purpose: plugins see "
                       "only the level and the winning source's kind). The full list and every mapping are on "
                       "Settings → Levels in the Busy Meter app.",
            "sourceKinds": list(SOURCE_KINDS),
            "levels": "0-6 stars (0 = lip accent only) and \"flash\" (the red/blue look)",
            "thisPlugin": {"id": self.plugin_id, "registered": bool(meter.token),
                           "levelShortcuts": list(LEVEL_SHORTCUTS)},
        }
        saved = meter.store.load(self.plugin_id) or {}
        if saved.get("signals"):
            result["thisPlugin"]["signals"] = saved["signals"]
        if saved.get("wants"):
            result["thisPlugin"]["wants"] = saved["wants"]
        if not meter.is_listening():
            result["externalConnections"] = "off or app not running"
            result["message"] += " " + TURN_ON_MESSAGE
            return result
        result["externalConnections"] = "on"
        if meter.token:
            try:
                status = meter.status()
                result["currentWinner"] = status.get("source")
                result["currentLevel"] = status.get("level")
            except BusyMeterError as e:
                result["statusError"] = e.message
        return result

    def register_plugin(self, name: str = DEFAULT_NAME, signals: list[dict[str, Any]] | None = None,
                        wants: list[str] | None = None, wait_seconds: int = 60, force: bool = False) -> dict[str, Any]:
        """Ask the user to allow this assistant; waits up to wait_seconds for the click, then returns."""
        meter = self.client
        wants = wants or ["signals", "status"]
        wait = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
        try:
            # A saved token counts only if Busy Meter still knows it (a revoked one is deleted by the check).
            if not force and meter.token and meter.verify_token():
                return {"ok": True, "state": "approved", "pluginId": self.plugin_id,
                        "message": "Already allowed in Busy Meter. Pass force=true to ask again with new signals."}
            # With force, the working token stays saved until the new one arrives (a Deny keeps the old one).
            request_id = meter.pending_request()
            if request_id is None:
                request_id = meter.register(name=name, signals=signals or [], wants=wants)
            if wait == 0:
                return {"ok": True, "state": "pending", "requestId": request_id, "message": WAITING_MESSAGE}
            meter.wait_for_approval(request_id, timeout=wait)
            return {"ok": True, "state": "approved", "pluginId": self.plugin_id,
                    "message": "Allowed. The token is saved for this user; set_level and set_signals work now."}
        except ApprovalTimeoutError:
            return {"ok": True, "state": "pending", "message": WAITING_MESSAGE +
                    " Call register_plugin again to keep waiting."}
        except DeniedError as e:
            return _from_error(e, state="denied")
        except BusyMeterError as e:
            return _from_error(e)
        except ValueError as e:
            return _fail("bad_request", str(e))

    def set_signals(self, active: list[str], ttl_seconds: int = 60) -> dict[str, Any]:
        """Replace this assistant's active signals (declared at registration) for ttl_seconds (1-600)."""
        with self._lock:
            self._stop_hold_locked()
            try:
                reply = self.client.set_signals(active, int(ttl_seconds))
            except BusyMeterError as e:
                return _from_error(e)
            except ValueError as e:
                return _fail("bad_request", str(e))
        return {"ok": True, **reply, "message": f"Active for {reply.get('expiresInSeconds')} s: "
                                                f"{', '.join(reply.get('active') or []) or 'nothing'}."}

    def set_level(self, level: int | str, ttl: int | str = 600) -> dict[str, Any]:
        """Show a level (0-6 stars or "flash") for ttl (seconds or text like 10m, 2h)."""
        try:
            value = normalize_level(level)
            seconds = parse_duration(ttl)
        except ValueError as e:
            return _fail("bad_request", str(e))
        # Stop the old hold, send, and start the new one under one lock, so overlapping calls leave one refresher.
        with self._lock:
            self._stop_hold_locked()
            try:
                if seconds <= MAX_TTL_SECONDS:
                    reply = self.client.set_level(value, seconds)
                    return {"ok": True, **reply, "message": f"Level {value} for {seconds} s (through the user's level "
                                                            "map; other sources may still win)."}
                reply = self.client.set_level(value, REFRESH_TTL)
            except BusyMeterError as e:
                return _from_error(e)
            self._hold = _Hold(self, value, self._clock() + seconds)
            self._hold.start()
        return {"ok": True, **reply, "message": f"Level {value} for {seconds} s. Longer than 10 minutes, so this "
                                                "server refreshes it while it runs; clear stops it."}

    def clear(self) -> dict[str, Any]:
        """Clear this assistant's signals and level now."""
        with self._lock:
            self._stop_hold_locked()
            try:
                reply = self.client.clear()
            except BusyMeterError as e:
                return _from_error(e)
        return {"ok": True, **reply, "message": "Cleared."}

    # ----- bundled recipes (no arbitrary code: names from busymeter.recipes.RECIPES only) -----

    def list_recipes(self) -> dict[str, Any]:
        """Bundled recipes, whether each is installed (has a settings file) and running."""
        return self.recipes.list_recipes()

    def install_recipe(self, name: str) -> dict[str, Any]:
        """Write a bundled recipe's default settings and start it in the background; the user clicks Allow."""
        return self.recipes.install_recipe(name)

    def stop_recipe(self, name: str) -> dict[str, Any]:
        """Stop a bundled recipe running in the background (it clears its signals)."""
        return self.recipes.stop_recipe(name)

    def uninstall_recipe(self, name: str) -> dict[str, Any]:
        """Stop it, clear its signals, delete its settings, saved token and sign-in entry."""
        return self.recipes.uninstall_recipe(name)

    def set_recipe_autostart(self, name: str, on: bool) -> dict[str, Any]:
        """Start a bundled recipe at every Windows sign-in (on) or stop doing that (off): one per-user Run entry."""
        return self.recipes.set_recipe_autostart(name, on)

    def get_recipe_settings(self, name: str) -> dict[str, Any]:
        """A recipe's settings (secrets hidden) and their schema."""
        return self.recipes.get_recipe_settings(name)

    def set_recipe_settings(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Change settings, checked against the schema (no unknown keys, ranges enforced, never secrets)."""
        return self.recipes.set_recipe_settings(name, changes)

    # ----- holds over 10 minutes -----

    def _stop_hold(self) -> None:
        with self._lock:
            self._stop_hold_locked()

    def _stop_hold_locked(self) -> None:
        hold, self._hold = self._hold, None
        if hold is not None:
            hold.stop()

    def active_holds(self) -> int:
        """Refreshers still running (for tests): at most one."""
        return sum(1 for t in threading.enumerate() if isinstance(t, _Hold) and t.tools is self and not t.stopped)


class _Hold(threading.Thread):
    def __init__(self, tools: Tools, level: int | str, until: float):
        super().__init__(daemon=True, name="busymeter-hold")
        self.tools, self.level, self.until = tools, level, until
        self._stopped = threading.Event()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:
        while not self._stopped.wait(REFRESH_TTL / 2):
            # Refresh under the tools' lock and only while still current, so a newer level or clear always wins.
            with self.tools._lock:
                if self._stopped.is_set():
                    return
                left = self.until - self.tools._clock()
                try:
                    if left <= 0:
                        self.tools.client.clear()
                        self._stopped.set()
                        if self.tools._hold is self:
                            self.tools._hold = None
                        return
                    self.tools.client.set_level(self.level, max(1, min(REFRESH_TTL, int(left + 0.999))))
                except BusyMeterError:
                    self._stopped.set()
                    return


def _describe(status: dict[str, Any]) -> str:
    level, source = status.get("level"), status.get("source")
    if status.get("paused"):
        return "Syncing is paused in Busy Meter; the meter is off."
    if level == "off":
        return "The meter is off."
    shown = "Flash (red/blue)" if level == "flash" else f"{level} star{'s' if level != 1 else ''}"
    return f"The meter shows {shown}; winning source: {source}."


def _fail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, **extra}


def _from_error(e: BusyMeterError, **extra: Any) -> dict[str, Any]:
    out = _fail(e.code, e.message, **extra)
    if isinstance(e, NotListeningError):
        out["externalConnections"] = "off or app not running"
    if e.retry_after is not None:
        out["retryAfterSeconds"] = e.retry_after
    return out
