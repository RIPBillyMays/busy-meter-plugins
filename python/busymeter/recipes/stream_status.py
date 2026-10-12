"""Stream status: live or recording in OBS Studio shows on the meter.

Talks to OBS's built-in obs-websocket server (protocol v5; OBS 28 and later, Tools > WebSocket Server Settings) on
127.0.0.1 only. Streaming -> ``live`` (suggested 5 stars), recording only -> ``recording`` (suggested 4; a paused
recording counts as not recording), OBS closed or the server off -> nothing (the meter shows the app's other sources).

Reads: whether OBS is streaming or recording (GetStreamStatus, GetRecordStatus and the output events). Never reads
scenes, sources, audio, video, chat or stream keys, and never changes anything in OBS.

The obs-websocket password goes in the settings file only (``password``); it is never printed, logged, or shown or
accepted through the MCP server. Authentication is obs-websocket v5's: base64(sha256(base64(sha256(password + salt))
+ challenge)), so the password itself never goes over the socket.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
from typing import Any

from ._ws import WebSocket, WebSocketError
from .base import Recipe
from .settings import Field, Schema

SCHEMA = Schema({
    "port": Field("int", 4455, "obs-websocket port on 127.0.0.1 (OBS: Tools > WebSocket Server Settings).",
                  min=1024, max=65535),
    "password": Field("str", "", "obs-websocket password (empty if authentication is off in OBS). Set it in this "
                                 "file only.", secret=True, max_length=256),
    "live_level": Field("level", 5, "Suggested level while streaming (remap in the app)."),
    "recording_level": Field("level", 4, "Suggested level while recording only (remap in the app)."),
    "show_recording": Field("bool", True, "Report recording at all (false = streaming only)."),
})

OUTPUTS_EVENTS = 1 << 6  # obs-websocket EventSubscription.Outputs
AUTH_FAILED = 4009
RETRY_SECONDS = 5.0
AUTH_RETRY_SECONDS = 60.0
QUERY_SECONDS = 5.0
MAX_MESSAGES_PER_POLL = 50


def auth_string(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 authentication string."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


class ObsAuthError(Exception):
    pass


class StreamStatus(Recipe):
    name = "stream_status"
    plugin_id = "com.thebusymeter.stream"
    title = "Stream status (OBS)"
    summary = "Live in OBS -> 'live' (5 stars), recording only -> 'recording' (4 stars)."
    reads = "Whether OBS is streaming or recording, from OBS's own obs-websocket server on 127.0.0.1."
    never_reads = "Scenes, sources, audio, video, chat or stream keys; it never changes anything in OBS."
    schema = SCHEMA
    ttl_seconds = 30
    interval = 1.0

    def __init__(self, settings: dict[str, Any], **kwargs: Any):
        super().__init__(settings, **kwargs)
        self.ws: WebSocket | None = None
        self.next_try = 0.0
        self.next_query = 0.0
        self.streaming = False
        self.recording = False
        self.record_paused = False
        self._request = 0
        self._told: str | None = None

    def signals(self) -> list[dict[str, Any]]:
        out = [{"name": "live", "label": "Streaming (OBS)", "suggest": self.settings["live_level"]}]
        if self.settings["show_recording"]:
            out.append({"name": "recording", "label": "Recording (OBS)", "suggest": self.settings["recording_level"]})
        return out

    def apply_settings(self, settings: dict[str, Any]) -> None:
        reconnect = (settings["port"], settings["password"]) != (self.settings["port"], self.settings["password"])
        super().apply_settings(settings)
        if reconnect:
            self._drop()
            self.next_try = 0.0

    def stop(self) -> None:
        self._drop()

    # ----- connection -----

    def _tell(self, key: str, message: str) -> None:
        if self._told != key:
            self._told = key
            self.say(message)

    def _drop(self) -> None:
        if self.ws is not None:
            self.ws.close()
        self.ws = None
        self.streaming = self.recording = self.record_paused = False

    def _connect(self) -> None:
        ws = WebSocket.connect(self.settings["port"], "/", timeout=3.0, protocol="obswebsocket.json")
        try:
            hello = self._expect(ws, 0)
            data: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": OUTPUTS_EVENTS}
            auth = hello.get("authentication")
            if isinstance(auth, dict):
                if not self.settings["password"]:
                    raise ObsAuthError("OBS asks for a password: put it in \"password\" in the stream_status settings "
                                       f"file ({self.settings_path or 'see busymeter recipes'}).")
                data["authentication"] = auth_string(self.settings["password"], str(auth.get("salt", "")),
                                                     str(auth.get("challenge", "")))
            ws.send_text(json.dumps({"op": 1, "d": data}))
            try:
                self._expect(ws, 2)
            except WebSocketError as e:
                if e.code == AUTH_FAILED:
                    raise ObsAuthError("OBS refused the password. Check \"password\" in the stream_status settings "
                                       f"file ({self.settings_path or 'see busymeter recipes'}).") from None
                raise
        except BaseException:
            ws.close()
            raise
        self.ws = ws
        self.next_query = 0.0

    @staticmethod
    def _expect(ws: WebSocket, op: int) -> dict[str, Any]:
        for _ in range(20):
            text = ws.recv_text(3.0)
            if text is None:
                raise WebSocketError("OBS didn't answer.")
            message = _parse(text)
            if message.get("op") == op:
                d = message.get("d")
                return d if isinstance(d, dict) else {}
        raise WebSocketError("OBS didn't answer as expected.")

    def _query(self) -> None:
        assert self.ws is not None
        for request in ("GetStreamStatus", "GetRecordStatus"):
            self._request += 1
            self.ws.send_text(json.dumps({"op": 6, "d": {"requestType": request, "requestId": f"bm-{self._request}"}}))

    def _handle(self, message: dict[str, Any]) -> None:
        d = message.get("d")
        if not isinstance(d, dict):
            return
        op = message.get("op")
        if op == 7:  # RequestResponse
            data = d.get("responseData")
            if not isinstance(data, dict):
                return
            if d.get("requestType") == "GetStreamStatus":
                self.streaming = data.get("outputActive") is True
            elif d.get("requestType") == "GetRecordStatus":
                self.recording = data.get("outputActive") is True
                self.record_paused = data.get("outputPaused") is True
        elif op == 5:  # Event
            data = d.get("eventData")
            if not isinstance(data, dict):
                return
            kind = d.get("eventType")
            if kind == "StreamStateChanged":
                self.streaming = data.get("outputActive") is True
            elif kind == "RecordStateChanged":
                self.recording = data.get("outputActive") is True
                state = data.get("outputState")
                if state == "OBS_WEBSOCKET_OUTPUT_PAUSED":
                    self.record_paused = True
                elif state in ("OBS_WEBSOCKET_OUTPUT_RESUMED", "OBS_WEBSOCKET_OUTPUT_STARTED",
                               "OBS_WEBSOCKET_OUTPUT_STOPPED"):
                    self.record_paused = False

    # ----- poll -----

    def poll(self) -> list[str]:
        now = self.clock()
        if self.ws is None:
            if now < self.next_try:
                return []
            try:
                self._connect()
                self._tell("connected", "Connected to OBS.")
            except ObsAuthError as e:
                self.next_try = now + AUTH_RETRY_SECONDS
                self._tell("auth", str(e))
                return []
            except (OSError, WebSocketError):
                self.next_try = now + RETRY_SECONDS
                self._tell("closed", "OBS isn't running, or its WebSocket server is off (Tools > WebSocket Server "
                                     f"Settings, port {self.settings['port']}). Trying again every few seconds.")
                return []
        try:
            if now >= self.next_query:
                self._query()
                self.next_query = now + QUERY_SECONDS
            wait = 0.3
            for _ in range(MAX_MESSAGES_PER_POLL):  # bounded: a chatty server can't hold the loop
                text = self.ws.recv_text(wait)
                if text is None:
                    break
                self._handle(_parse(text))
                wait = 0.0
        except (OSError, WebSocketError, socket.timeout):
            self._drop()
            self.next_try = now + RETRY_SECONDS
            self._tell("closed", "Lost the connection to OBS. Trying again every few seconds.")
            return []
        if self.streaming:
            return ["live"]
        if self.recording and not self.record_paused and self.settings["show_recording"]:
            return ["recording"]
        return []


def _parse(text: str) -> dict[str, Any]:
    try:
        message = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return message if isinstance(message, dict) else {}
