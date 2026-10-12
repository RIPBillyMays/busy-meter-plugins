"""Game state receiver: a loopback endpoint for games that offer an official "game state" HTTP push feature.

Some games can post their state as JSON to a local URL you configure in the game's own config. Point it at
``http://127.0.0.1:47822/`` and put this recipe's token in it (most such features send it as ``auth.token`` in the
JSON body; an ``Authorization: Bearer <token>`` or ``X-Busy-Meter-Token`` header works too). Rules in the settings
file map JSON paths and simple conditions to signals with suggested levels::

    {"path": "round.phase", "equals": "live", "signal": "in-match", "suggest": 4}
    {"path": "player.state.health", "lt": 30, "signal": "low-health", "suggest": "flash"}

Conditions: ``equals`` (text, number, true/false, null), ``lt`` / ``gt`` (numbers), ``exists`` (true/false). Paths
are dotted keys; a number picks a list item (``players.0.state``). Several rules may raise the same signal (any of
them). When the game stops posting for ``stale_seconds`` (default 30) the signals clear.

Security: bound to 127.0.0.1 only; the Host header must be 127.0.0.1:<port> or localhost:<port>; any request with an
``Origin`` header (a web page) is refused; POST only; bodies over ``max_body_kb`` are refused unread; the token is
compared in constant time and never printed or shown through MCP. Reads only what the game posts; keeps nothing but
the active signal names and the time of the last post.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..client import normalize_level
from .base import Recipe
from .settings import Field, Schema, SettingsError

_SIGNAL = re.compile(r"^[a-z0-9-]{1,48}$")
CONDITIONS = ("equals", "lt", "gt", "exists")
MAX_RULES = 64
MAX_SIGNALS = 32
_MISSING = object()

EXAMPLE_RULES: list[dict[str, Any]] = [
    {"path": "round.phase", "equals": "live", "signal": "in-match", "label": "In a match", "suggest": 4},
    {"path": "player.state.health", "lt": 30, "signal": "low-health", "label": "Low health", "suggest": "flash"},
]


def check_rules(key: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_RULES:
        raise SettingsError(f"{key}: a list of 1-{MAX_RULES} rules.")
    out: list[dict[str, Any]] = []
    for i, rule in enumerate(value, 1):
        where = f"{key}[{i}]"
        if not isinstance(rule, dict):
            raise SettingsError(f"{where}: each rule is an object with path, a condition and signal.")
        unknown = set(rule) - {"path", "signal", "suggest", "label", *CONDITIONS}
        if unknown:
            raise SettingsError(f"{where}: unknown field {sorted(unknown)[0]} (use path, equals/lt/gt/exists, "
                                "signal, suggest, label).")
        path = rule.get("path")
        if not isinstance(path, str) or not 1 <= len(path) <= 200 or any(not p for p in path.split(".")) \
                or len(path.split(".")) > 16:
            raise SettingsError(f"{where}: path must be dotted keys like round.phase (up to 16 parts).")
        conditions = [c for c in CONDITIONS if c in rule]
        if len(conditions) != 1:
            raise SettingsError(f"{where}: exactly one of equals, lt, gt, exists.")
        condition = conditions[0]
        expected = rule[condition]
        if condition in ("lt", "gt") and (isinstance(expected, bool) or not isinstance(expected, (int, float))):
            raise SettingsError(f"{where}: {condition} needs a number.")
        if condition == "exists" and not isinstance(expected, bool):
            raise SettingsError(f"{where}: exists is true or false.")
        if condition == "equals" and not (expected is None or isinstance(expected, (str, int, float, bool))):
            raise SettingsError(f"{where}: equals compares with text, a number, true/false or null.")
        if isinstance(expected, str) and len(expected) > 200:
            raise SettingsError(f"{where}: {condition} text is at most 200 characters.")
        signal = rule.get("signal")
        if not isinstance(signal, str) or not _SIGNAL.match(signal) or signal.startswith("level-"):
            raise SettingsError(f"{where}: signal must be 1-48 of a-z, 0-9 and '-', not starting with 'level-'.")
        cleaned: dict[str, Any] = {"path": path, condition: expected, "signal": signal}
        if "suggest" in rule:
            try:
                cleaned["suggest"] = normalize_level(rule["suggest"])
            except ValueError:
                raise SettingsError(f"{where}: suggest is 0-6 or \"flash\".") from None
        if "label" in rule:
            if not isinstance(rule["label"], str) or len(rule["label"]) > 64:
                raise SettingsError(f"{where}: label is text, at most 64 characters.")
            cleaned["label"] = rule["label"]
        out.append(cleaned)
    if len({r["signal"] for r in out}) > MAX_SIGNALS:
        raise SettingsError(f"{key}: at most {MAX_SIGNALS} different signals.")
    return out


SCHEMA = Schema({
    "port": Field("int", 47822, "Port on 127.0.0.1 the game posts to.", min=1024, max=65535),
    "token": Field("str", None, "Shared token the game sends (auth.token in the body, or a Bearer header). Made on "
                                "first run; set it in this file only.", secret=True, agent=False, min_length=16,
                   max_length=256, default_factory=lambda: secrets.token_urlsafe(32)),
    "max_body_kb": Field("int", 64, "Largest post accepted, KB.", min=1, max=1024),
    "stale_seconds": Field("int", 30, "Signals clear when the game hasn't posted for this long.", min=5, max=600),
    "rules": Field("list", EXAMPLE_RULES, "Rules: {path, equals|lt|gt|exists, signal, suggest, label}.",
                   check=check_rules),
})


def lookup(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return _MISSING
    return data


def matches(rule: dict[str, Any], data: Any) -> bool:
    value = lookup(data, rule["path"])
    if "exists" in rule:
        return (value is not _MISSING) == rule["exists"]
    if value is _MISSING:
        return False
    if "equals" in rule:
        expected = rule["equals"]
        if isinstance(expected, bool) or isinstance(value, bool):
            return isinstance(expected, bool) and isinstance(value, bool) and expected == value
        if expected is None or value is None:
            return expected is None and value is None
        if isinstance(expected, str) or isinstance(value, str):
            return isinstance(expected, str) and isinstance(value, str) and expected == value
        return isinstance(value, (int, float)) and value == expected
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value < rule["lt"] if "lt" in rule else value > rule["gt"]


def evaluate(rules: list[dict[str, Any]], data: Any) -> list[str]:
    out: list[str] = []
    for rule in rules:
        if rule["signal"] not in out and matches(rule, data):
            out.append(rule["signal"])
    return out


MAX_CONNECTIONS = 8


class _Server(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS handled at once (each with the handler's 10 s read timeout); more are closed at once.
    Never shares the port: a taken port is an error (on Windows, SO_REUSEADDR would let two listeners bind it)."""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class GameState(Recipe):
    name = "game_state"
    plugin_id = "com.thebusymeter.gamestate"
    title = "Game state receiver"
    summary = "A local endpoint for games with an official game-state push feature; rules map fields to signals."
    reads = "Only the JSON a game posts to 127.0.0.1:<port> with the token."
    never_reads = "Game files, memory or the screen; nothing from other devices or web pages."
    schema = SCHEMA
    ttl_seconds = 30
    interval = 1.0

    def __init__(self, settings: dict[str, Any], **kwargs: Any):
        super().__init__(settings, **kwargs)
        self.server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()
        self.active: list[str] = []
        self.last_post: float | None = None
        self.posts = 0

    def signals(self) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for rule in self.settings["rules"]:
            entry = out.setdefault(rule["signal"], {"name": rule["signal"]})
            if "label" in rule and "label" not in entry:
                entry["label"] = rule["label"]
            if "suggest" in rule and "suggest" not in entry:
                entry["suggest"] = rule["suggest"]
        return list(out.values())

    def start(self) -> None:
        self.server = self._serve(self.settings["port"])

    def _serve(self, port: int) -> _Server:
        server = _Server(("127.0.0.1", port), make_handler(self))
        threading.Thread(target=server.serve_forever, daemon=True, name="busymeter-game-state").start()
        self.say(f"Listening on http://127.0.0.1:{server.server_address[1]}/ for game state posts (token in the "
                 "settings file).")
        return server

    @property
    def port(self) -> int:
        return self.server.server_address[1] if self.server is not None else self.settings["port"]

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def apply_settings(self, settings: dict[str, Any]) -> None:
        settings = dict(settings)
        if settings["port"] != self.settings["port"]:
            try:
                new_server = self._serve(settings["port"])  # bind the new port before letting go of the old one
            except OSError as e:
                self.say(f"Can't listen on port {settings['port']} ({e.strerror or e}); still listening on "
                         f"{self.port}. Pick another port in the settings file.")
                settings["port"] = self.settings["port"]
            else:
                self.stop()
                self.server = new_server
        super().apply_settings(settings)
        with self.lock:
            self.active, self.last_post = [], None  # new rules: wait for the next post

    def receive(self, data: Any) -> list[str]:
        active = evaluate(self.settings["rules"], data)
        with self.lock:
            self.active, self.last_post = active, self.clock()
            self.posts += 1
        return active

    def token_ok(self, given: Any) -> bool:
        """Constant-time check. Anything that isn't a well-formed token (not text, lone surrogates...) is False."""
        expected = self.settings.get("token") or ""
        if not isinstance(given, str) or not given or not isinstance(expected, str) or not expected:
            return False
        try:
            given_bytes = given.encode("utf-8")
            expected_bytes = expected.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(given_bytes, expected_bytes)

    def poll(self) -> list[str]:
        with self.lock:
            if self.last_post is None or self.clock() - self.last_post > self.settings["stale_seconds"]:
                return []
            return list(self.active)


def make_handler(recipe: GameState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "busymeter-game-state"
        timeout = 10  # a client that stalls is dropped

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: never the body or the token
            pass

        def reply(self, status: int, code: str = "", message: str = "") -> None:
            body = json.dumps({"ok": status == 200} if not code else {"error": code, "message": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status >= 400:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def refuse(self) -> None:
            self.reply(405, "method_not_allowed", "POST only.")

        do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = refuse  # type: ignore[assignment]

        def do_POST(self) -> None:
            if self.headers.get("Origin") is not None:
                return self.reply(403, "forbidden_origin", "Requests from web pages aren't accepted.")
            port = recipe.port
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self.reply(400, "bad_host", f"Use http://127.0.0.1:{port}/")
            if self.headers.get("Transfer-Encoding"):
                return self.reply(411, "length_required", "Send a Content-Length (no chunked bodies).")
            text = (self.headers.get("Content-Length") or "").strip()
            if not text.isdigit():
                return self.reply(411, "length_required", "Send a Content-Length.")
            length = int(text)
            if length > recipe.settings["max_body_kb"] * 1024:
                return self.reply(413, "too_large", f"At most {recipe.settings['max_body_kb']} KB.")
            try:
                raw = self.rfile.read(length) if length else b""
            except (TimeoutError, OSError):
                self.close_connection = True
                return None
            if len(raw) < length:
                return self.reply(400, "bad_request", "The body was cut short.")
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                data = None
            header_token = self.headers.get("X-Busy-Meter-Token")
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                header_token = auth[len("Bearer "):].strip()
            body_token = lookup(data, "auth.token") if isinstance(data, dict) else _MISSING
            given = header_token if header_token else (body_token if isinstance(body_token, str) else None)
            if not recipe.token_ok(given):
                return self.reply(401, "unauthorized", "Missing or wrong token.")
            if data is None:
                return self.reply(400, "bad_json", "Send a JSON object.")
            recipe.receive(data)
            return self.reply(200)

    return Handler
