"""A tiny stand-in for the Busy Meter plugin API (stdlib http.server on 127.0.0.1, random port), for client tests.

It applies the same transport rules as the app (Origin -> 403, Host must be 127.0.0.1:<port>, no query strings,
JSON bodies only) and records every request so tests can check what the client sent.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.registrations: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.answer: bool | None = None  # None = leave pending; True/False = answer on the next poll
        self.expire = False  # answer "denied/expired" on the next poll
        self.forced: dict[str, tuple[int, str, str]] = {}  # "METHOD /path" -> (status, code, message), once
        self.status_body: dict[str, Any] = {"level": 4, "paused": False, "source": "teams"}
        self.active: dict[str, list[str]] = {}
        self.withdrawn: list[str] = []
        self.lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:
                api.handle(self, "GET")

            def do_POST(self) -> None:
                api.handle(self, "POST")

            def do_PUT(self) -> None:
                api.handle(self, "PUT")

            def do_DELETE(self) -> None:
                api.handle(self, "DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeApi":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def approve_token(self, plugin_id: str, wants: list[str] | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self.tokens[token] = {"id": plugin_id, "wants": wants or ["signals", "status"], "enabled": True}
        return token

    # ----- request handling -----

    def handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        record = {"method": method, "path": h.path, "headers": dict(h.headers.items()), "body": raw}
        with self.lock:
            self.requests.append(record)
        path = h.path
        key = f"{method} {path.split('?')[0]}"
        if h.headers.get("Origin") is not None:
            return self.send(h, 403, error("forbidden_origin", "Requests from web pages aren't accepted."))
        if h.headers.get("Host") != f"127.0.0.1:{self.port}":
            return self.send(h, 400, error("bad_host", "Host must be 127.0.0.1:port."))
        if "?" in path:
            return self.send(h, 400, error("bad_request", "No query strings."))
        if key in self.forced:
            status, code, message = self.forced.pop(key)
            return self.send(h, status, error(code, message), retry_after=status == 429)
        body: Any = None
        if method in ("POST", "PUT"):
            if (h.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self.send(h, 415, error("unsupported_media_type", "Content-Type must be application/json."))
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                return self.send(h, 400, error("bad_json", "Not valid JSON."))

        if path == "/v1/register" and method == "POST":
            if any(r["state"] == "pending" and r["id"] == body.get("id") for r in self.registrations.values()):
                return self.send(h, 409, error("already_pending", "A request for this plugin id is already waiting."))
            request_id = secrets.token_urlsafe(16)
            self.registrations[request_id] = {"id": body.get("id"), "state": "pending", "body": body}
            return self.send(h, 202, {"requestId": request_id, "state": "pending", "expiresInSeconds": 120})
        if path.startswith("/v1/register/") and method == "DELETE":
            request_id = path[len("/v1/register/"):]
            request = self.registrations.get(request_id)
            if request is None:
                return self.send(h, 404, error("not_found", "No such request."))
            if request["state"] == "pending" and self.answer is None and not self.expire:
                del self.registrations[request_id]
                self.withdrawn.append(request_id)
                return self.send(h, 200, {"state": "withdrawn"})
            if request["state"] == "pending":  # the user's answer is already in
                request["state"] = "approved" if self.answer else "denied"
                request["reason"] = "expired" if self.expire else "denied"
            body = error("already_answered", "The user already answered this request.")
            body["state"] = "approved" if request["state"] == "approved" else "denied"
            return self.send(h, 409, body)
        if path.startswith("/v1/register/") and method == "GET":
            request = self.registrations.get(path[len("/v1/register/"):])
            if request is None:
                return self.send(h, 404, error("not_found", "No such request."))
            if request["state"] == "pending" and self.expire:
                request["state"], request["reason"] = "denied", "expired"
            elif request["state"] == "pending" and self.answer is not None:
                request["state"] = "approved" if self.answer else "denied"
                request["reason"] = "denied"
            if request["state"] == "approved":
                del self.registrations[path[len("/v1/register/"):]]
                token = self.approve_token(request["id"], request["body"].get("wants"))
                return self.send(h, 200, {"state": "approved", "pluginId": request["id"], "token": token})
            if request["state"] == "denied":
                return self.send(h, 200, {"state": "denied", "reason": request["reason"]})
            return self.send(h, 200, {"state": "pending"})

        if path not in ("/v1/signals", "/v1/status", "/v1/alert"):
            return self.send(h, 404, error("not_found", "No such endpoint."))
        auth = h.headers.get("Authorization") or ""
        plugin = self.tokens.get(auth[len("Bearer "):]) if auth.startswith("Bearer ") else None
        if plugin is None:
            return self.send(h, 401, error("unauthorized", "Missing or unknown token."))
        if not plugin["enabled"]:
            return self.send(h, 403, error("plugin_off", "This plugin is switched off in Busy Meter."))
        if path == "/v1/status" and method == "GET":
            return self.send(h, 200, dict(self.status_body))
        if path == "/v1/signals" and method == "PUT":
            ttl = body.get("ttlSeconds")
            if not isinstance(ttl, int) or not 1 <= ttl <= 600:
                return self.send(h, 400, error("bad_ttl", "ttlSeconds must be 1-600."))
            active = [f"level-{body['level']}"] if "level" in body else list(body.get("active") or [])
            self.active[plugin["id"]] = active
            return self.send(h, 200, {"active": active, "expiresInSeconds": ttl})
        if path == "/v1/signals" and method == "DELETE":
            self.active[plugin["id"]] = []
            return self.send(h, 200, {"active": [], "expiresInSeconds": 0})
        if path == "/v1/alert" and method == "POST":
            return self.send(h, 200, {"shown": True})
        return self.send(h, 405, error("method_not_allowed", "Method not allowed."))

    @staticmethod
    def send(h: BaseHTTPRequestHandler, status: int, body: dict[str, Any], retry_after: bool = False) -> None:
        data = json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        if status == 429 or retry_after:
            h.send_header("Retry-After", "1")
        h.end_headers()
        h.wfile.write(data)


def error(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}
