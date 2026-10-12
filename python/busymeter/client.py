"""Thin client for the Busy Meter plugin API (spec: ../../spec/openapi.yaml). Standard library only.

The Busy Meter Windows app listens on 127.0.0.1 only, and only while the user has turned on
Settings > Sources > External connections. A program registers, the user clicks Allow in the
Busy Meter tray, and the program gets a token, which this client keeps in a per-user file
(see busymeter.tokens). Nothing here can approve a request: only the user can.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .tokens import TokenStore

DEFAULT_PORT = 47820
API_VERSION = 1
LEVELS = (0, 1, 2, 3, 4, 5, 6, "flash")
WANTS = ("signals", "status", "taps", "effects")
EFFECTS = ("lip-pulse", "star-sweep", "flash-short")
MAX_TTL_SECONDS = 600
CONNECT_TIMEOUT = 1.0

NOT_RUNNING_MESSAGE = (
    "Busy Meter isn't running, or External connections is off (there's no %LOCALAPPDATA%\\BusyMeter\\"
    "plugin-api.json, which the app writes while it listens). Turn on Settings \u2192 Sources \u2192 External "
    "connections in the Busy Meter app."
)
NOT_RESPONDING_MESSAGE = (
    "Busy Meter didn't answer in time (the app may be busy or stuck). Try again; if it keeps happening, restart the "
    "Busy Meter app."
)
TURN_ON_MESSAGE = (
    "Busy Meter isn't accepting connections. Turn on Settings → Sources → External connections "
    "in the Busy Meter app (and make sure the app is running)."
)

_ID = re.compile(r"^[a-z0-9.-]{3,64}$")
_SIGNAL = re.compile(r"^[a-z0-9-]{1,48}$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)?\s*$", re.I)


# ----- errors -----


class BusyMeterError(Exception):
    """An error from the API (or the connection). ``code`` is the API's error code, ``status`` the HTTP status."""

    def __init__(self, message: str, code: str = "error", status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retry_after = retry_after


class NotListeningError(BusyMeterError):
    """Nothing answers on the port: the app isn't running or External connections is off."""


class AppNotRespondingError(BusyMeterError):
    """Something accepted the connection but didn't answer in time, or the connection broke mid-request."""


class UnauthorizedError(BusyMeterError):
    """401: no token, or Busy Meter doesn't know it (revoked or removed)."""


class ForbiddenError(BusyMeterError):
    """403: switched off, not allowed by the approval, or (never from this client) an Origin header."""


class ConflictError(BusyMeterError):
    """409: a request is already waiting, too many plugins, or an alert was ignored."""


class RateLimitedError(BusyMeterError):
    """429: too fast. ``retry_after`` is in seconds when the app said."""


class DeniedError(BusyMeterError):
    """The user clicked Deny, or didn't answer within 2 minutes."""


class CancelledError(BusyMeterError):
    """Stopped waiting for approval because the caller's ``cancel`` event was set. A request already shown in the
    tray stays there until the user answers or it expires (2 minutes); its id stays saved, so the next connect()
    waits on it instead of asking again."""


class ApprovalTimeoutError(BusyMeterError):
    """Still waiting for the user after the time this client was told to wait."""


_FRIENDLY = {
    "unauthorized": "Busy Meter doesn't know this plugin's token (it was revoked, or never approved). "
    "Register again; you'll be asked to click Allow in the Busy Meter tray.",
    "plugin_off": "This plugin is switched off in Busy Meter. Turn it on under Settings → Sources → "
    "External connections.",
    "not_allowed": "Busy Meter didn't allow this plugin to do that. Register again asking for it in 'wants' "
    "(the user approves the new request in the tray).",
    "already_pending": "A request for this plugin id is already waiting for you in the Busy Meter tray. "
    "Click Allow (or Deny) there.",
    "too_many_plugins": "Busy Meter already has the most plugins it takes (32). Revoke one under Settings → "
    "Sources → External connections.",
    "cooldown": "This plugin was denied in the last 10 minutes; Busy Meter won't ask again until then.",
    "withdraw_pause": "This plugin just withdrew a request; Busy Meter takes a new one after 5 seconds.",
    "too_many_pending": "Too many requests are waiting in the Busy Meter tray; answer those first.",
    "forbidden_origin": "Busy Meter refused the request because it carried an Origin header (web pages can't "
    "use the API).",
    "unknown_signal": "That signal wasn't declared when the plugin registered. Use a declared signal or a level.",
}


# ----- discovery and small helpers -----


@dataclass(frozen=True)
class Endpoint:
    port: int
    from_file: bool
    explicit: bool = False

    @property
    def trusted(self) -> bool:
        """True when the port came from the app's discovery file (or the caller named it). Tokens go only there."""
        return self.from_file or self.explicit

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/"


def discovery_path() -> Path:
    """``%LOCALAPPDATA%\\BusyMeter\\plugin-api.json`` (written by the app only while it listens)."""
    override = os.environ.get("BUSYMETER_DISCOVERY_FILE")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "BusyMeter" / "plugin-api.json"


def discover(path: Path | str | None = None) -> Endpoint:
    """The port from the discovery file, or 47820 when there is none (the app may be off).

    Only the port is taken from the file; the host is always 127.0.0.1.
    """
    file = Path(path) if path is not None else discovery_path()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        port = data.get("port") if isinstance(data, dict) else None
        if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
            return Endpoint(port, True)
    except (OSError, ValueError):
        pass
    return Endpoint(DEFAULT_PORT, False)


def parse_duration(value: int | float | str) -> int:
    """Seconds from 30, "30", "30s", "10m", "1.5h". At least 1 second."""
    if isinstance(value, bool):
        raise ValueError("A duration is a number of seconds or text like 30s, 10m, 1h.")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = _DURATION.match(str(value))
        if not match:
            raise ValueError(f"Can't read the duration {value!r}; use e.g. 30s, 10m or 1h.")
        number, unit = float(match.group(1)), (match.group(2) or "s").lower()
        seconds = number * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    if seconds < 1:
        raise ValueError("A duration must be at least 1 second.")
    return int(round(seconds))


def normalize_level(level: int | str) -> int | str:
    """0-6 or "flash" (also accepts "0".."6" as text)."""
    if isinstance(level, bool):
        raise ValueError("level must be 0-6 or \"flash\".")
    if isinstance(level, str):
        text = level.strip().lower()
        if text == "flash":
            return "flash"
        if text.isdigit():
            level = int(text)
    if isinstance(level, int) and 0 <= level <= 6:
        return level
    raise ValueError("level must be 0-6 (stars; 0 = lip accent only) or \"flash\".")


def check_ttl(ttl_seconds: int) -> int:
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError("ttl_seconds must be 1-600 (refresh before it runs out to keep a level longer).")
    return ttl_seconds


def is_valid_plugin_id(plugin_id: str) -> bool:
    return isinstance(plugin_id, str) and bool(_ID.match(plugin_id))


def signal_declarations(signals: Iterable[Any]) -> list[dict[str, Any]]:
    """Signals as the API wants them, from dicts or (name, label, suggest) tuples or plain names."""
    out: list[dict[str, Any]] = []
    for item in signals or ():
        if isinstance(item, str):
            entry: dict[str, Any] = {"name": item}
        elif isinstance(item, Mapping):
            entry = {k: item[k] for k in ("name", "label", "suggest") if k in item and item[k] is not None}
        else:
            parts = list(item)
            entry = {"name": parts[0]}
            if len(parts) > 1 and parts[1] is not None:
                entry["label"] = parts[1]
            if len(parts) > 2 and parts[2] is not None:
                entry["suggest"] = parts[2]
        name = entry.get("name")
        if not isinstance(name, str) or not _SIGNAL.match(name) or name.startswith("level-"):
            raise ValueError(f"Signal name {name!r}: use 1-48 of a-z, 0-9 and '-', not starting with 'level-'.")
        if "suggest" in entry:
            entry["suggest"] = normalize_level(entry["suggest"])
        out.append(entry)
    if len(out) > 32:
        raise ValueError("At most 32 signals.")
    return out


# ----- the client -----


class BusyMeter:
    """One plugin's connection to the Busy Meter app.

    >>> meter = BusyMeter("com.example.cli")
    >>> meter.connect(name="My script")      # registers and waits for Allow the first time
    >>> meter.set_level(5, ttl_seconds=600)
    """

    def __init__(
        self,
        plugin_id: str,
        *,
        port: int | None = None,
        token: str | None = None,
        store: TokenStore | None = None,
        timeout: float = 10.0,
        discovery_file: Path | str | None = None,
    ):
        if not is_valid_plugin_id(plugin_id):
            raise ValueError("plugin_id must be 3-64 of a-z, 0-9, '.' and '-' (e.g. com.example.heartrate).")
        self.plugin_id = plugin_id
        self._port = port
        self._discovery_file = discovery_file
        self.store = store if store is not None else TokenStore()
        self._token = token
        self.timeout = timeout
        self._connect_args: dict[str, Any] | None = None

    # --- connection ---

    @property
    def endpoint(self) -> Endpoint:
        if self._port is not None:
            return Endpoint(self._port, False, explicit=True)
        return discover(self._discovery_file)

    @property
    def token(self) -> str | None:
        if self._token is None:
            saved = self.store.load(self.plugin_id)
            if saved and isinstance(saved.get("token"), str):
                self._token = saved["token"]
        return self._token

    def is_listening(self) -> bool:
        """True when the API answers (External connections is on), whether or not this plugin has a token."""
        try:
            self._request("GET", "/v1/status", auth=False, raise_for_status=False)
            return True
        except (NotListeningError, AppNotRespondingError):
            return False

    def verify_token(self) -> bool:
        """True when Busy Meter still knows this plugin's token (a 401 also deletes the saved token)."""
        if not self.token:
            return False
        try:
            self._request("GET", "/v1/status")
            return True
        except UnauthorizedError:
            return False
        except ForbiddenError:
            return True  # known token: switched off, or no "status" in the approval

    def reconnecting(self, call: Callable[[], Any]) -> Any:
        """Run ``call``; if Busy Meter no longer knows the token (revoked), register again with the arguments of the
        last connect() (the user clicks Allow again), then run it once more."""
        try:
            return call()
        except UnauthorizedError:
            if self._connect_args is None:
                raise
            self.connect(**self._connect_args)
            return call()

    def _request(self, method: str, path: str, body: Any = None, *, auth: bool = True, raise_for_status: bool = True,
                 timeout: float | None = None) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}  # never an Origin header: the app refuses those
        if auth:
            token = self.token
            if not token:
                raise UnauthorizedError(
                    f"No token for {self.plugin_id} yet. Register first; you'll be asked to click Allow in the "
                    "Busy Meter tray.", "unauthorized", 401)
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        endpoint = self.endpoint
        if auth and not endpoint.trusted:
            # No discovery file: the app isn't listening. Whatever holds the default port isn't getting a token.
            raise NotListeningError(NOT_RUNNING_MESSAGE, "not_listening")
        port = endpoint.port
        read_timeout = timeout or self.timeout
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=read_timeout)
        try:
            try:
                # Loopback connects are instant when something listens; don't sit through Windows' SYN retries.
                conn.sock = socket.create_connection(("127.0.0.1", port), timeout=min(CONNECT_TIMEOUT, read_timeout))
            except OSError as e:
                raise NotListeningError(TURN_ON_MESSAGE, "not_listening") from e
            try:
                conn.sock.settimeout(read_timeout)
                conn.request(method, path, body=data, headers=headers)
                response = conn.getresponse()
                raw = response.read(64 * 1024)
                status = response.status
                retry_after = response.getheader("Retry-After")
            except (socket.timeout, TimeoutError) as e:
                raise AppNotRespondingError(NOT_RESPONDING_MESSAGE, "not_responding") from e
            except (http.client.HTTPException, OSError) as e:
                raise AppNotRespondingError(
                    "The connection to Busy Meter broke before it answered. Try again; if it keeps happening, "
                    "restart the Busy Meter app.", "not_responding") from e
        finally:
            conn.close()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if auth and status == 401:
            self.forget_token()  # revoked or unknown: the next connect() registers again
        if raise_for_status and status >= 400:
            raise _error(status, payload, retry_after)
        return status, payload

    # --- registration ---

    def register(self, name: str | None = None, signals: Iterable[Any] = (), wants: Sequence[str] = ("signals", "status"),
                 version: str | None = None) -> str:
        """Ask the user to allow this plugin. Returns the request id (also kept in the token file)."""
        body: dict[str, Any] = {"id": self.plugin_id, "signals": signal_declarations(signals), "wants": list(wants)}
        if name:
            body["name"] = name
        if version:
            body["version"] = version
        _, reply = self._request("POST", "/v1/register", body, auth=False)
        request_id = reply.get("requestId")
        if not isinstance(request_id, str):
            raise BusyMeterError("Busy Meter answered the registration without a request id.", "bad_reply")
        self.store.save_pending(self.plugin_id, request_id, body["signals"], body["wants"])
        return request_id

    def poll(self, request_id: str) -> dict[str, Any]:
        """The raw answer: {"state": "pending" | "denied" | "approved", ...}. Saves the token when approved."""
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", request_id or ""):
            raise ValueError("Not a request id.")
        _, reply = self._request("GET", "/v1/register/" + request_id, auth=False)
        if reply.get("state") == "approved" and isinstance(reply.get("token"), str):
            self._token = reply["token"]
            self.store.save_token(self.plugin_id, reply["token"])
        elif reply.get("state") == "denied":
            self.store.clear_pending(self.plugin_id)
        return reply

    def withdraw(self, request_id: str | None = None) -> dict[str, Any]:
        """Take back a registration that still waits for the user (its prompt in the tray closes). Defaults to the
        saved waiting request. Returns {"state": "withdrawn"}; when the user already answered, the poll's answer (a
        token is saved as usual); {"state": "not_found"} when Busy Meter no longer knows it; {"state": "none"} when
        nothing is waiting. The same id can't register again for 5 seconds afterwards."""
        request_id = request_id or self.pending_request()
        if not request_id:
            return {"state": "none"}
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", request_id):
            raise ValueError("Not a request id.")
        status, reply = self._request("DELETE", "/v1/register/" + request_id, auth=False, raise_for_status=False)
        if status == 200:
            if self.pending_request() == request_id:
                self.store.clear_pending(self.plugin_id)
            return {"state": "withdrawn"}
        if status == 409 and reply.get("error") == "already_answered":
            try:
                return self.poll(request_id)  # collects the token once, or clears a denied request
            except BusyMeterError as e:
                if e.status != 404:
                    raise
        elif status != 404:
            raise _error(status, reply, None)
        if self.pending_request() == request_id:
            self.store.clear_pending(self.plugin_id)
        return {"state": "not_found"}

    def pending_request(self) -> str | None:
        saved = self.store.load(self.plugin_id) or {}
        pending = saved.get("pendingRequestId")
        return pending if isinstance(pending, str) else None

    def wait_for_approval(self, request_id: str | None = None, timeout: float = 125.0, interval: float = 1.0,
                          on_wait: Callable[[], None] | None = None, cancel: Any = None) -> str:
        """Poll until the user answers. Returns the token; raises DeniedError or ApprovalTimeoutError, or
        CancelledError as soon as ``cancel`` (a threading.Event) is set."""
        request_id = request_id or self.pending_request()
        if not request_id:
            raise BusyMeterError("Nothing to wait for: register first.", "no_request")
        deadline = time.monotonic() + timeout
        told = False
        while True:
            try:
                reply = self.poll(request_id)
            except BusyMeterError as e:
                if e.status == 404:
                    self.store.clear_pending(self.plugin_id)
                    raise BusyMeterError("Busy Meter no longer knows this request (answered over 10 minutes ago, "
                                         "or its token was already collected). Register again.", "not_found", 404) from e
                raise
            state = reply.get("state")
            if state == "approved":
                return self._token  # type: ignore[return-value]
            if state == "denied":
                reason = reply.get("reason", "denied")
                raise DeniedError(
                    "The request expired: nobody clicked Allow in the Busy Meter tray within 2 minutes. Try again."
                    if reason == "expired" else "The user clicked Deny in the Busy Meter tray.", reason)
            if not told and on_wait is not None:
                on_wait()
                told = True
            if time.monotonic() >= deadline:
                raise ApprovalTimeoutError("Still waiting for you to click Allow in the Busy Meter tray.", "pending")
            if cancel is not None:
                if cancel.wait(interval):
                    raise CancelledError("Stopped waiting for Allow.", "cancelled")
            else:
                time.sleep(interval)

    def connect(self, name: str | None = None, signals: Iterable[Any] = (), wants: Sequence[str] = ("signals", "status"),
                version: str | None = None, timeout: float = 125.0, on_wait: Callable[[], None] | None = None,
                cancel: Any = None) -> str:
        """A token for this plugin: the saved one if Busy Meter still knows it, else wait on a saved request, else
        register and wait. A revoked token is deleted and the plugin registers again. ``cancel`` (a
        threading.Event) stops the wait (CancelledError), and no new request is made once it is set."""
        self.remember(name=name, signals=signals, wants=wants, version=version, timeout=timeout, on_wait=on_wait,
                      cancel=cancel)
        if self.token and self.verify_token():
            return self.token  # type: ignore[return-value]
        if self.pending_request():
            try:
                return self.wait_for_approval(timeout=timeout, on_wait=on_wait, cancel=cancel)
            except BusyMeterError as e:
                if e.code not in ("not_found", "expired"):
                    raise
        if cancel is not None and cancel.is_set():
            raise CancelledError("Stopped before asking for Allow.", "cancelled")
        request_id = self.register(name=name, signals=signals, wants=wants, version=version)
        return self.wait_for_approval(request_id, timeout=timeout, on_wait=on_wait, cancel=cancel)

    def remember(self, name: str | None = None, signals: Iterable[Any] = (), wants: Sequence[str] = ("signals", "status"),
                 version: str | None = None, timeout: float = 125.0, on_wait: Callable[[], None] | None = None,
                 cancel: Any = None) -> None:
        """Set what reconnecting() registers with after a revoke. connect() sets it; call it after a new
        registration (e.g. new signals) was approved through register() and poll()."""
        self._connect_args = {"name": name, "signals": list(signals or ()), "wants": tuple(wants), "version": version,
                              "timeout": timeout, "on_wait": on_wait, "cancel": cancel}

    def forget_token(self) -> None:
        """Drop the token (memory and file) but keep a waiting request."""
        self._token = None
        self.store.drop_token(self.plugin_id)

    def forget(self) -> None:
        """Delete this plugin's saved token (the user can also Revoke it in the app)."""
        self._token = None
        self.store.delete(self.plugin_id)

    # --- signals, status, effects ---

    def set_signals(self, active: Iterable[str], ttl_seconds: int) -> dict[str, Any]:
        """Replace this plugin's active signals for ttl_seconds (1-600). An empty list clears."""
        names = list(active)
        _, reply = self._request("PUT", "/v1/signals", {"active": names, "ttlSeconds": check_ttl(ttl_seconds)})
        return reply

    def set_level(self, level: int | str, ttl_seconds: int) -> dict[str, Any]:
        """Show a level (0-6 stars or "flash") for ttl_seconds (1-600), through the user's level map."""
        _, reply = self._request("PUT", "/v1/signals", {"level": normalize_level(level), "ttlSeconds": check_ttl(ttl_seconds)})
        return reply

    def clear(self) -> dict[str, Any]:
        _, reply = self._request("DELETE", "/v1/signals")
        return reply

    def status(self) -> dict[str, Any]:
        """{"level": 0-6 | "flash" | "off", "paused": bool, "source": kind, ["plugin": own id]}. Needs "status"."""
        _, reply = self._request("GET", "/v1/status")
        return reply

    def alert(self, effect: str, color: str | None = None, duration_ms: int | None = None) -> dict[str, Any]:
        """One short effect (lip-pulse, star-sweep, flash-short). Needs "effects"; one every 10 s."""
        if effect not in EFFECTS:
            raise ValueError("effect must be lip-pulse, star-sweep or flash-short.")
        body: dict[str, Any] = {"effect": effect}
        if color is not None:
            body["color"] = color
        if duration_ms is not None:
            body["durationMs"] = duration_ms
        _, reply = self._request("POST", "/v1/alert", body, timeout=max(self.timeout, 10.0))
        return reply


def _error(status: int, payload: Mapping[str, Any], retry_after: str | None) -> BusyMeterError:
    code = str(payload.get("error") or "http_" + str(status))
    server_message = str(payload.get("message") or "")
    message = _FRIENDLY.get(code, server_message or f"Busy Meter answered {status}.")
    if code == "ignored":
        message = f"Busy Meter ignored the alert ({server_message or 'busy'})."
    elif code == "rate_limited":
        message = "Too fast: " + (server_message or "slow down.")
    elif code not in _FRIENDLY and server_message and message != server_message:
        message = server_message
    try:
        retry = float(retry_after) if retry_after is not None else None
    except ValueError:
        retry = None
    cls = {401: UnauthorizedError, 403: ForbiddenError, 409: ConflictError, 429: RateLimitedError}.get(status, BusyMeterError)
    return cls(message, code, status, retry)
