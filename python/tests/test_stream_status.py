"""The stream-status recipe and its small WebSocket client against a fake obs-websocket v5 server."""

import base64
import hashlib
import json
import socket
import struct
import threading
import unittest

from busymeter.recipes import _ws
from busymeter.recipes.stream_status import SCHEMA, StreamStatus, auth_string

PASSWORD = "correct horse"


class FakeObs:
    """obs-websocket v5, just enough: Hello (with an auth challenge when a password is set), Identify -> Identified or
    close 4009, GetStreamStatus / GetRecordStatus, and pushed output events."""

    def __init__(self, password=PASSWORD):
        self.password = password
        self.streaming = False
        self.recording = False
        self.paused = False
        self.identify = None
        self.requests = []
        self.connections = 0
        self.clients = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.closed = True
        self.sock.close()
        for c in self.clients:
            try:
                c.close()
            except OSError:
                pass

    def _accept(self):
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            self.clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def send(self, conn, message):
        conn.sendall(_ws.encode_frame(0x1, json.dumps(message).encode(), mask=False))

    def push(self, event_type, data):
        for c in list(self.clients):
            try:
                self.send(c, {"op": 5, "d": {"eventType": event_type, "eventIntent": 64, "eventData": data}})
            except OSError:
                pass

    def _serve(self, conn):
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(1024)
            headers = dict(line.split(": ", 1) for line in request.decode().split("\r\n")[1:] if ": " in line)
            assert headers["Host"] == f"127.0.0.1:{self.port}"
            assert "Origin" not in headers
            accept = _ws.accept_key(headers["Sec-WebSocket-Key"])
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
            hello = {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}
            salt, challenge = "c2FsdA==", "Y2hhbGxlbmdl"
            if self.password:
                hello["authentication"] = {"challenge": challenge, "salt": salt}
            self.send(conn, {"op": 0, "d": hello})
            while True:
                fin, opcode, payload = _ws.read_frame(conn, from_client=True)
                if opcode == 0x8:
                    return
                if opcode == 0xA:
                    self.pongs = getattr(self, "pongs", 0) + 1
                    continue
                assert opcode == 0x1
                message = json.loads(payload)
                if message["op"] == 1:
                    self.identify = message["d"]
                    if self.password:
                        secret = base64.b64encode(hashlib.sha256((self.password + salt).encode()).digest()).decode()
                        expected = base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()
                        if message["d"].get("authentication") != expected:
                            conn.sendall(_ws.encode_frame(0x8, struct.pack("!H", 4009) + b"Authentication failed.",
                                                          mask=False))
                            return
                    self.send(conn, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
                elif message["op"] == 6:
                    kind = message["d"]["requestType"]
                    self.requests.append(kind)
                    data = ({"outputActive": self.streaming} if kind == "GetStreamStatus"
                            else {"outputActive": self.recording, "outputPaused": self.paused})
                    conn.sendall(_ws.encode_frame(0x9, b"hi", mask=False))  # a ping in between
                    self.send(conn, {"op": 7, "d": {"requestType": kind, "requestId": message["d"]["requestId"],
                                                    "requestStatus": {"result": True, "code": 100},
                                                    "responseData": data}})
        except (OSError, AssertionError, _ws.WebSocketError, ValueError):
            pass
        finally:
            conn.close()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class StreamStatusTests(unittest.TestCase):
    def setUp(self):
        self.obs = FakeObs()
        self.addCleanup(self.obs.close)
        self.clock = Clock()
        self.messages = []

    def recipe(self, **settings):
        values = SCHEMA.validate({"port": self.obs.port, "password": PASSWORD, **settings})
        recipe = StreamStatus(values, clock=self.clock, say=self.messages.append)
        self.addCleanup(recipe.stop)
        return recipe

    def poll_until(self, recipe, want, tries=20):
        got = None
        for _ in range(tries):
            self.clock.now += 6  # past the query interval
            got = recipe.poll()
            if got == want:
                return got
        return got

    def test_auth_string_matches_the_protocol(self):
        secret = base64.b64encode(hashlib.sha256(b"pwsalt").digest()).decode()
        expected = base64.b64encode(hashlib.sha256((secret + "chal").encode()).digest()).decode()
        self.assertEqual(expected, auth_string("pw", "salt", "chal"))

    def test_streaming_recording_and_idle(self):
        recipe = self.recipe()
        self.assertEqual([], recipe.poll())
        self.assertEqual(64, self.obs.identify["eventSubscriptions"])
        self.assertEqual(1, self.obs.identify["rpcVersion"])
        self.obs.recording = True
        self.assertEqual(["recording"], self.poll_until(recipe, ["recording"]))
        self.obs.streaming = True
        self.assertEqual(["live"], self.poll_until(recipe, ["live"]))  # streaming wins over recording
        self.obs.streaming = False
        self.obs.paused = True
        self.assertEqual([], self.poll_until(recipe, []))  # a paused recording isn't "recording"
        self.assertEqual(1, self.obs.connections)
        self.assertIn("GetStreamStatus", self.obs.requests)

    def test_events_update_without_waiting_for_the_next_query(self):
        recipe = self.recipe()
        recipe.poll()
        self.obs.push("StreamStateChanged", {"outputActive": True, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"})
        got = []
        for _ in range(10):
            got = recipe.poll()  # the clock doesn't move: no new query
            if got:
                break
        self.assertEqual(["live"], got)

    def test_recording_can_be_left_out(self):
        recipe = self.recipe(show_recording=False)
        self.assertEqual(["live"], [s["name"] for s in recipe.signals()])
        self.obs.recording = True
        self.assertEqual([], self.poll_until(recipe, ["recording"], tries=3))

    def test_suggested_levels_come_from_the_settings(self):
        recipe = self.recipe(live_level=6, recording_level="flash")
        self.assertEqual([{"name": "live", "label": "Streaming (OBS)", "suggest": 6},
                          {"name": "recording", "label": "Recording (OBS)", "suggest": "flash"}], recipe.signals())

    def test_wrong_password_is_reported_without_the_password(self):
        recipe = self.recipe(password="wrong-password-123")
        recipe.settings_path = "C:/x/stream_status.json"
        self.assertEqual([], recipe.poll())
        self.assertTrue(any("refused the password" in m for m in self.messages), self.messages)
        self.assertFalse(any("wrong-password-123" in m or PASSWORD in m for m in self.messages))
        connections = self.obs.connections
        self.clock.now += 10
        recipe.poll()
        self.assertEqual(connections, self.obs.connections)  # waits a minute before trying again
        self.clock.now += 61
        recipe.poll()
        self.assertEqual(connections + 1, self.obs.connections)

    def test_missing_password_says_where_to_put_it(self):
        recipe = self.recipe(password="")
        recipe.poll()
        self.assertTrue(any("asks for a password" in m for m in self.messages))

    def test_no_password_needed_when_auth_is_off(self):
        self.obs.password = ""
        self.obs.streaming = True
        recipe = self.recipe(password="")
        self.assertEqual(["live"], self.poll_until(recipe, ["live"]))
        self.assertNotIn("authentication", self.obs.identify)

    def test_obs_closed_clears_and_retries(self):
        recipe = self.recipe()
        self.obs.streaming = True
        self.assertEqual(["live"], self.poll_until(recipe, ["live"]))
        self.obs.close()
        got = self.poll_until(recipe, [])
        self.assertEqual([], got)
        self.assertTrue(any("OBS" in m for m in self.messages))
        # comes back when OBS does
        self.obs = FakeObs()
        self.addCleanup(self.obs.close)
        self.obs.streaming = True
        recipe.apply_settings(SCHEMA.validate({"port": self.obs.port, "password": PASSWORD}))
        self.assertEqual(["live"], self.poll_until(recipe, ["live"]))

    def test_nothing_listening(self):
        port = self.obs.port
        self.obs.close()
        recipe = StreamStatus(SCHEMA.validate({"port": port}), clock=self.clock, say=self.messages.append)
        self.assertEqual([], recipe.poll())
        self.assertEqual(1, sum("isn't running" in m for m in self.messages))
        self.clock.now += 6
        recipe.poll()
        self.assertEqual(1, sum("isn't running" in m for m in self.messages))  # said once


class HardeningTests(unittest.TestCase):
    def test_masked_server_frames_and_bad_control_frames_are_refused(self):
        bad = [
            _ws.encode_frame(0x1, b"hi", mask=True),  # a server must not mask
            bytes([0x89, 126]) + struct.pack("!H", 200) + b"x" * 200,  # ping over 125 bytes
            bytes([0x09, 2]) + b"hi",  # fragmented ping
            bytes([0x08, 2]) + b"\x03\xe8",  # fragmented close
        ]
        for frame in bad:
            with self.subTest(frame=frame[:4]):
                a, b = socket.socketpair()
                a.sendall(frame)
                with self.assertRaises(_ws.WebSocketError):
                    _ws.WebSocket(b).recv_text(1.0)
                a.close()
                b.close()

    def test_one_poll_reads_a_bounded_number_of_messages(self):
        from busymeter.recipes import stream_status

        obs = FakeObs()
        self.addCleanup(obs.close)
        clock = Clock()
        recipe = StreamStatus(SCHEMA.validate({"port": obs.port, "password": PASSWORD}), clock=clock,
                              say=lambda m: None)
        self.addCleanup(recipe.stop)
        recipe.poll()
        handled = []
        original = recipe._handle
        recipe._handle = lambda message: (handled.append(1), original(message))
        for _ in range(stream_status.MAX_MESSAGES_PER_POLL + 30):
            obs.push("StreamStateChanged", {"outputActive": False})
        import time as _time

        _time.sleep(0.3)
        recipe.poll()
        self.assertLessEqual(len(handled), stream_status.MAX_MESSAGES_PER_POLL)
        recipe.poll()
        self.assertGreater(len(handled), stream_status.MAX_MESSAGES_PER_POLL)  # the rest come next time


class WebSocketTests(unittest.TestCase):
    def test_bad_accept_key_is_refused(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: nope\r\n\r\n")
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        with self.assertRaises(_ws.WebSocketError):
            _ws.WebSocket.connect(port)
        server.close()

    def test_frames_round_trip_and_size_cap(self):
        a, b = socket.socketpair()
        for size in (5, 300, 70000):
            a.sendall(_ws.encode_frame(0x1, b"x" * size))
            fin, opcode, payload = _ws.read_frame(b, from_client=True)
            self.assertEqual((True, 1, b"x" * size), (fin, opcode, payload))
        a.sendall(bytes([0x81, 0x80 | 127]) + struct.pack("!Q", 10**9))
        with self.assertRaises(_ws.WebSocketError):
            _ws.read_frame(b, from_client=True)
        a.close()
        b.close()

    def test_fragmented_messages_are_joined(self):
        a, b = socket.socketpair()
        ws = _ws.WebSocket(b)
        a.sendall(bytes([0x01, 3]) + b"abc" + bytes([0x80, 3]) + b"def")
        self.assertEqual("abcdef", ws.recv_text(1.0))
        self.assertIsNone(ws.recv_text(0.05))
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
