#!/usr/bin/env python3
"""A small HTTP endpoint on this PC that turns POSTs into Busy Meter levels.

    python webhook_bridge.py                  # listens on http://127.0.0.1:47821
    curl -X POST http://127.0.0.1:47821/level -H "Authorization: Bearer <secret>" \
         -H "Content-Type: application/json" -d "{\"level\": 5, \"for\": \"10m\"}"
    curl -X POST http://127.0.0.1:47821/clear -H "Authorization: Bearer <secret>"

- Bound to 127.0.0.1 only: other devices can't reach it. To trigger it from a phone or a web service you'd need your
  own tunnel or relay; that's on you, and this script isn't hardened for the internet.
- Every request needs the shared secret (``Authorization: Bearer <secret>`` or ``X-Busy-Meter-Secret: <secret>``).
  The secret is made on first run and kept with the plugin tokens (see busymeter.tokens), readable by you only;
  ``--show-secret`` prints it.
- Requests from web pages (with an Origin header) are refused, so a site you visit can't change your meter.
- Body: ``{"level": 0-6 | "flash", "for": "10m"}`` (``for`` defaults to 10m; longer than 10 minutes is refreshed
  while this script runs). ``POST /clear`` clears.

The first run asks you to click Allow in the Busy Meter tray (turn on Settings > Sources > External connections).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import busymeter
except ImportError:  # running from the repo without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
    import busymeter

from busymeter import BusyMeter, BusyMeterError, normalize_level, parse_duration
from busymeter.cli import safe_console
from busymeter.client import MAX_TTL_SECONDS
from busymeter.tokens import TokenStore, default_token_dir

PLUGIN_ID = "com.thebusymeter.webhook"
DEFAULT_PORT = 47821
MAX_BODY = 4096
REFRESH_TTL = 60


def secret_path() -> Path:
    return default_token_dir() / "webhook-secret.txt"


def load_or_create_secret(path: Path) -> tuple[str, bool]:
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value, False
    except OSError:
        pass
    TokenStore(path.parent).ensure_dir()  # the token folder: yours only (see busymeter.tokens)
    value = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.replace(tmp, path)
    return value, True


class Holder:
    """Keeps one level up, refreshing it when it's longer than the API's 10-minute TTL. Every change (and every
    refresh) happens under one lock, so overlapping requests leave exactly one refresher and the newest level wins."""

    def __init__(self, meter: Any):
        self.meter = meter
        self._stop: threading.Event | None = None
        self._lock = threading.Lock()

    def _call(self, fn: Any) -> Any:
        # A revoked token: register again (the user clicks Allow in the tray), then retry once.
        reconnecting = getattr(self.meter, "reconnecting", None)
        return reconnecting(fn) if reconnecting is not None else fn()

    def set(self, level: int | str, seconds: int) -> dict[str, Any]:
        with self._lock:
            self._cancel_locked()
            if seconds <= MAX_TTL_SECONDS:
                return self._call(lambda: self.meter.set_level(level, seconds))
            reply = self._call(lambda: self.meter.set_level(level, REFRESH_TTL))
            stop = threading.Event()
            self._stop = stop
            threading.Thread(target=self._refresh, args=(level, seconds, stop), daemon=True,
                             name="busymeter-webhook-hold").start()
            return reply

    def _refresh(self, level: int | str, seconds: int, stop: threading.Event) -> None:
        left = seconds - REFRESH_TTL / 2
        while left > 0 and not stop.wait(REFRESH_TTL / 2):
            with self._lock:
                if stop.is_set():
                    return
                try:
                    self._call(lambda: self.meter.set_level(level, max(1, min(REFRESH_TTL, int(left)))))
                except BusyMeterError as e:
                    print(f"Refresh failed: {e.message}", file=sys.stderr)
                    stop.set()
                    return
            left -= REFRESH_TTL / 2

    def _cancel_locked(self) -> None:
        stop, self._stop = self._stop, None
        if stop is not None:
            stop.set()

    def cancel(self) -> None:
        with self._lock:
            self._cancel_locked()

    def active(self) -> int:
        """Refreshers still running (for tests): at most one."""
        with self._lock:
            return 0 if self._stop is None or self._stop.is_set() else 1

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._cancel_locked()
            return self._call(self.meter.clear)


def make_handler(secret: str, holder: Holder, port: int) -> type[BaseHTTPRequestHandler]:
    expected_host = f"127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "busymeter-webhook"
        timeout = 10  # seconds per socket read: a client that stalls is dropped

        def log_message(self, fmt: str, *args: Any) -> None:  # one short line, never the secret
            sys.stderr.write(f"{self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}\n")

        def reply(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
            given = self.headers.get("X-Busy-Meter-Secret") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                given = auth[len("Bearer "):].strip()
            return hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))

        def do_GET(self) -> None:
            self.reply(405, {"error": "method_not_allowed", "message": "POST /level or POST /clear."})

        do_PUT = do_DELETE = do_OPTIONS = do_GET

        def do_POST(self) -> None:
            if self.headers.get("Origin") is not None:
                return self.reply(403, {"error": "forbidden_origin", "message": "Requests from web pages aren't accepted."})
            if self.headers.get("Host") not in (expected_host, f"localhost:{port}"):
                return self.reply(400, {"error": "bad_host", "message": "Use http://" + expected_host})
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized", "message": "Missing or wrong secret."})
            text = (self.headers.get("Content-Length") or "0").strip()
            if not text.isdigit():  # negative, signed or not a number
                return self.reply(400, {"error": "bad_request", "message": "Bad Content-Length."})
            length = int(text)
            if length > MAX_BODY:
                return self.reply(413, {"error": "too_large", "message": "At most 4 KB."})
            try:
                raw = self.rfile.read(length) if length else b""
            except TimeoutError:
                return self.reply(408, {"error": "timeout", "message": "The body didn't arrive in time."})
            path = self.path.split("?")[0].rstrip("/")
            try:
                if path == "/clear":
                    return self.reply(200, {"ok": True, **holder.clear()})
                if path != "/level":
                    return self.reply(404, {"error": "not_found", "message": "POST /level or POST /clear."})
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(body, dict):
                        raise ValueError("Send a JSON object.")
                    level = normalize_level(body.get("level"))
                    seconds = parse_duration(body.get("for", "10m"))
                except (ValueError, UnicodeDecodeError) as e:
                    return self.reply(400, {"error": "bad_request", "message": str(e)})
                return self.reply(200, {"ok": True, **holder.set(level, seconds)})
            except BusyMeterError as e:
                return self.reply(502, {"error": e.code, "message": e.message})

    return Handler


def main(argv: list[str] | None = None) -> int:
    safe_console()
    parser = argparse.ArgumentParser(description="Local webhook -> Busy Meter level bridge (127.0.0.1 only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument("--id", default=PLUGIN_ID, help=f"plugin id (default {PLUGIN_ID})")
    parser.add_argument("--show-secret", action="store_true", help="print the shared secret and exit")
    args = parser.parse_args(argv)

    secret, created = load_or_create_secret(secret_path())
    if args.show_secret or created:
        print(f"Shared secret (kept in {secret_path()}):\n{secret}")
        if args.show_secret:
            return 0

    meter = BusyMeter(args.id)
    try:
        meter.connect(name="Webhook bridge", wants=("signals",),
                      on_wait=lambda: print("Waiting for you to click Allow in the Busy Meter tray...", file=sys.stderr))
    except BusyMeterError as e:
        print(e.message, file=sys.stderr)
        return 1

    holder = Holder(meter)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(secret, holder, args.port))
    print(f"Listening on http://127.0.0.1:{args.port} (POST /level, POST /clear). Ctrl+C stops.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            holder.clear()
        except BusyMeterError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
