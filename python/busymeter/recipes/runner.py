"""The common recipe runner: ``busymeter run <recipe>`` (or ``python -m busymeter run <recipe>``).

It loads and checks the recipe's settings file (writing the defaults the first time), registers the recipe as a plugin
with its signals and suggested levels (the user clicks Allow in the Busy Meter tray and can remap them on Settings >
Levels), polls the recipe, sends its active signals with a TTL and refreshes them at half the TTL, re-reads the
settings file when it changes, asks again when the declared signals change (new levels or rules), registers again
after a revoke (the client's ``reconnecting``), and clears its signals on exit (Ctrl+C, the stop file, or the end of
a focus block).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Any, Callable

from ..client import (
    ApprovalTimeoutError,
    AppNotRespondingError,
    BusyMeter,
    BusyMeterError,
    CancelledError,
    ConflictError,
    DeniedError,
    ForbiddenError,
    NotListeningError,
    RateLimitedError,
    UnauthorizedError,
    signal_declarations,
)
from .base import Recipe
from .control import HEARTBEAT_SECONDS, RunFiles
from .settings import SettingsError, SettingsFile

APPROVAL_TIMEOUT = 130.0  # a little over the app's 2 minutes, so its "expired" answer arrives
RETRY_SECONDS = 5.0

EXIT_OK, EXIT_SETTINGS, EXIT_DENIED, EXIT_ALREADY, EXIT_START = 0, 2, 3, 4, 5


class Runner:
    def __init__(self, recipe_cls: type[Recipe], *, meter: Any = None, directory: Any = None,
                 overrides: dict[str, Any] | None = None, restrict: bool = True,
                 clock: Callable[[], float] = time.monotonic, say: Callable[[str], None] | None = None,
                 stop_event: threading.Event | None = None, heartbeat: float = HEARTBEAT_SECONDS):
        self.recipe_cls = recipe_cls
        self.name = recipe_cls.name
        self.meter = meter if meter is not None else BusyMeter(recipe_cls.plugin_id)
        self.settings_file = SettingsFile(self.name, recipe_cls.schema, directory, restrict=restrict)
        self.files = RunFiles(self.name, self.settings_file.directory)
        self.overrides = dict(overrides or {})
        self.clock = clock
        self._say = say or (lambda message: print(message, file=sys.stderr, flush=True))
        self.stop_event = stop_event or threading.Event()
        self.heartbeat = heartbeat
        self.state = "starting"
        self.message = ""
        self.started = time.time()
        self.recipe: Recipe | None = None
        self.approved: list[dict[str, Any]] = []  # the signals Busy Meter has (from the approved registration)
        self.pending: tuple[str, list[dict[str, Any]]] | None = None  # a re-registration waiting for Allow
        self.refused: list[dict[str, Any]] | None = None  # declarations the user denied (not asked again)
        self.last_sent: list[str] | None = None  # None = unknown (send whatever comes next)
        self.last_time = 0.0
        self.sent_anything = False
        self.exit_code = EXIT_OK
        self._state_lock = threading.Lock()

    # ----- messages and state -----

    def say(self, message: str) -> None:
        self._say(time.strftime("%H:%M:%S ") + message)

    def set_state(self, state: str, message: str = "", *, announce: bool = True) -> None:
        with self._state_lock:
            changed = (state, message) != (self.state, self.message)
            self.state, self.message = state, message
        if changed:
            if announce and message:
                self.say(message)
            self._write_run()

    def _write_run(self) -> None:
        # Under the state lock, so the heartbeat thread can't write an older state over a newer one.
        with self._state_lock:
            try:
                self.files.write(self.state, self.message, self.started)
            except OSError:
                pass

    def _denied(self, e: BusyMeterError) -> None:
        self.files.mark_denied()  # a sign-in start skips it until autostart is turned on again
        self.set_state("stopped", e.message + " Stopping.")
        self.exit_code = EXIT_DENIED

    def _heartbeat_loop(self, done: threading.Event) -> None:
        while not done.wait(self.heartbeat):
            self._write_run()
            if self.files.stop_requested():
                self.stop_event.set()

    # ----- settings -----

    def _load_settings(self) -> dict[str, Any]:
        values = self.settings_file.ensure()
        if self.overrides:
            values = self.recipe_cls.schema.validate({**values, **self.overrides})
        return values

    def _reload_settings(self) -> None:
        if not self.settings_file.changed():
            return
        try:
            values = self._load_settings()
        except SettingsError as e:
            self.say(f"Settings not applied, keeping the previous ones: {e}")
            self.settings_file.mark_seen()  # don't repeat the message every tick
            return
        assert self.recipe is not None
        self.recipe.apply_settings(values)
        self.say("Settings re-read.")

    # ----- registration -----

    def _declarations(self) -> list[dict[str, Any]]:
        assert self.recipe is not None
        return signal_declarations(self.recipe.signals())

    def _remember(self, decls: list[dict[str, Any]]) -> None:
        remember = getattr(self.meter, "remember", None)
        if remember is not None:
            remember(name=self.recipe_cls.title, signals=decls, wants=("signals",), version=None,
                     timeout=APPROVAL_TIMEOUT, on_wait=self._on_wait, cancel=self.stop_event)

    def _on_wait(self) -> None:
        self.set_state("waiting_for_allow", "Waiting for you to click Allow in the Busy Meter tray...")

    def _connect(self) -> bool:
        """Connect until approved. False when told to stop or the user said no."""
        decls = self._declarations()
        while not self.stop_event.is_set():
            try:
                self.meter.connect(name=self.recipe_cls.title, signals=decls, wants=("signals",),
                                   timeout=APPROVAL_TIMEOUT, on_wait=self._on_wait, cancel=self.stop_event)
            except (NotListeningError, AppNotRespondingError) as e:
                self.set_state("waiting_for_app", e.message + " Trying again every few seconds.")
                self.stop_event.wait(RETRY_SECONDS)
                continue
            except DeniedError as e:
                self._denied(e)
                return False
            except CancelledError:
                return False
            except ApprovalTimeoutError:
                continue
            except RateLimitedError as e:
                if e.code not in ("withdraw_pause", "rate_limited"):
                    # cooldown (denied recently) or too_many_pending: the user should see why, and no fast retries
                    self.set_state("waiting_for_app", e.message)
                    self.stop_event.wait(RETRY_SECONDS * 2)
                    continue
                # withdraw_pause right after a stop and restart, or a short burst: a short wait, nothing for the user to do
                self.stop_event.wait(min(max(e.retry_after or RETRY_SECONDS, 1), 60))
                continue
            except BusyMeterError as e:
                self.set_state("waiting_for_app", e.message)
                self.stop_event.wait(RETRY_SECONDS * 2)
                continue
            saved = (getattr(self.meter, "store", None) and self.meter.store.load(self.meter.plugin_id)) or {}
            self.approved = saved.get("signals") if isinstance(saved.get("signals"), list) else decls
            self._remember(self.approved)
            self.files.clear_denied()  # allowed now (or a saved approval): an earlier Deny no longer counts
            self.set_state("running", f"{self.recipe_cls.title}: running.")
            return True
        return False

    def _check_declarations(self) -> None:
        """Ask again when the settings changed the declared signals (new levels or rules)."""
        decls = self._declarations()
        if self.pending is not None:
            request_id, asked = self.pending
            try:
                reply = self.meter.poll(request_id)
            except BusyMeterError as e:
                if e.status == 404:
                    # Gone: answered elsewhere (e.g. a reconnect after a revoke waited on it) or too old.
                    self.pending = None
                    saved = self.meter.store.load(self.meter.plugin_id) or {}
                    if saved.get("token") and saved.get("signals") == asked:
                        self.approved = asked
                        self._remember(asked)
                return
            state = reply.get("state")
            if state == "approved":
                self.pending = None
                self.approved = asked
                self._remember(asked)
                self.last_sent = None
                self.set_state("running", "Allowed: the new signals and levels apply.")
            elif state == "denied":
                self.pending = None
                self.refused = asked
                self.set_state("running", "Not allowed; keeping the signals Busy Meter already has.")
            return
        if decls == self.approved or decls == self.refused:
            return
        try:
            request_id = self.meter.register(name=self.recipe_cls.title, signals=decls, wants=("signals",))
        except ConflictError:
            return  # one is already waiting in the tray
        except (NotListeningError, AppNotRespondingError, RateLimitedError):
            return
        except BusyMeterError as e:
            self.say(e.message)
            self.refused = decls
            return
        self.pending = (request_id, decls)
        self.set_state("waiting_for_allow", "The settings changed this recipe's signals or levels: click Allow in "
                                            "the Busy Meter tray to apply them.")

    # ----- sending -----

    def _send(self, active: list[str]) -> None:
        recipe = self.recipe
        assert recipe is not None
        known = {d.get("name") for d in self.approved}
        active = sorted(dict.fromkeys(a for a in active if a in known))
        now = self.clock()
        ttl = recipe.ttl_seconds
        due = active != self.last_sent or (active and now - self.last_time >= ttl / 2)
        if not due:
            return
        try:
            if active:
                self.meter.reconnecting(lambda: self.meter.set_signals(active, ttl))
                self.sent_anything = True
            elif self.last_sent is None and not self.sent_anything:
                self.last_sent, self.last_time = active, now
                return
            else:
                self.meter.reconnecting(self.meter.clear)
            self.last_sent, self.last_time = active, now
            if self.state in ("waiting_for_app", "switched_off", "waiting_for_allow") and self.pending is None:
                self.set_state("running", f"{self.recipe_cls.title}: running.")
        except (NotListeningError, AppNotRespondingError) as e:
            self.last_sent = None
            self.set_state("waiting_for_app", e.message)
        except RateLimitedError:
            pass
        except ForbiddenError as e:
            self.last_sent = None
            self.last_time = now
            self.set_state("switched_off", e.message)
        except DeniedError as e:
            self._denied(e)
            self.stop_event.set()
        except CancelledError:
            self.stop_event.set()
        except ApprovalTimeoutError:
            self.last_sent = None
        except UnauthorizedError as e:
            self.last_sent = None
            self.set_state("waiting_for_allow", e.message)
        except BusyMeterError as e:
            self.last_sent = None
            self.say(e.message)

    def _clear(self) -> None:
        if not self.sent_anything:
            return
        try:
            self.meter.clear()
        except BusyMeterError:
            pass

    def _withdraw(self) -> None:
        """A stopped recipe takes back an Allow prompt still waiting in the tray, its own or a re-ask's."""
        ids = [self.pending[0]] if self.pending is not None else []
        try:
            saved = self.meter.pending_request()
        except (AttributeError, OSError):
            saved = None
        if saved and saved not in ids:
            ids.append(saved)
        for request_id in ids:
            try:
                self.meter.withdraw(request_id)
            except (BusyMeterError, ValueError, AttributeError):
                pass
        self.pending = None

    # ----- the loop -----

    def run(self) -> int:
        current = self.files.status()
        if current["running"] and current.get("pid") != os.getpid():
            self.say(f"{self.name} is already running (process {current['pid']}). Stop it first: "
                     f"busymeter stop {self.name}")
            return EXIT_ALREADY
        try:
            settings = self._load_settings()
        except SettingsError as e:
            self.say(str(e))
            return EXIT_SETTINGS
        self.files.clear_stop()
        self.say(f"Settings: {self.settings_file.path}")
        self._write_run()
        done = threading.Event()
        beat = threading.Thread(target=self._heartbeat_loop, args=(done,), daemon=True, name="busymeter-heartbeat")
        beat.start()
        self.recipe = self.recipe_cls(settings, clock=self.clock, say=self.say)
        self.recipe.settings_path = self.settings_file.path
        try:
            try:
                self.recipe.start()
            except OSError as e:  # e.g. the game-state port is taken
                self.set_state("stopped", f"Couldn't start: {e.strerror or e}. Stopping.")
                self.exit_code = EXIT_START
                return self.exit_code
            if self._connect():
                self.recipe.connected()  # e.g. the focus timer starts its block now, not before the Allow
                while not self.stop_event.is_set():
                    self._reload_settings()
                    self._check_declarations()
                    active = self.recipe.poll()
                    if self.recipe.finished:
                        break
                    self._send(active)
                    self.stop_event.wait(self.recipe.interval)
        except KeyboardInterrupt:
            pass
        finally:
            done.set()
            try:
                self.recipe.stop()
            finally:
                self._clear()
                self._withdraw()
                beat.join(timeout=self.heartbeat + 5)  # done is set: no heartbeat write can follow the last one
                with self._state_lock:
                    if self.state != "stopped":
                        self.state, self.message = "stopped", "Stopped; signals cleared."
                self._write_run()
                self.files.clear_stop()
        return self.exit_code


def install_stop_handlers(stop_event: threading.Event) -> None:
    """SIGTERM / SIGBREAK (Windows Ctrl+Break) stop the loop cleanly; Ctrl+C raises KeyboardInterrupt as usual."""
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: stop_event.set())
        except (ValueError, OSError):
            pass
