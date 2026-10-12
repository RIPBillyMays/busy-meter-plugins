"""A minimal WebSocket client (RFC 6455), standard library only, for loopback connections.

Just what the stream-status recipe needs to talk to OBS's built-in obs-websocket on 127.0.0.1: the opening handshake
(with the Sec-WebSocket-Accept check), masked text frames out, text/continuation/ping/pong/close frames in, a frame
size cap, and no extensions (no compression). Not for the internet: no TLS, no proxies, no redirects.

Why not a package: the recipe connects to one local program, and a ~150-line client keeps busymeter free of
third-party code (the client's rule: standard library only). It's tested against a fake server in the tests.
"""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 1024 * 1024
MAX_MESSAGE = 4 * 1024 * 1024


class WebSocketError(Exception):
    """The handshake failed, the server broke the protocol, or the connection closed (``code`` when it said)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def encode_frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    head = bytearray([0x80 | opcode])
    length = len(payload)
    bit = 0x80 if mask else 0
    if length < 126:
        head.append(bit | length)
    elif length < 65536:
        head.append(bit | 126)
        head += struct.pack("!H", length)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", length)
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    return bytes(head) + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def read_exact(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise WebSocketError("The connection closed.")
        data += chunk
    return bytes(data)


def read_frame(sock: socket.socket, max_frame: int = MAX_FRAME, from_client: bool = False) -> tuple[bool, int, bytes]:
    """(fin, opcode, payload). A server's frames must not be masked (RFC 6455 5.1); ``from_client`` reads the other
    way round (for a test server). Control frames must be whole and at most 125 bytes."""
    b0, b1 = read_exact(sock, 2)
    if b0 & 0x70:
        raise WebSocketError("Unexpected extension bits.")
    fin, opcode = bool(b0 & 0x80), b0 & 0x0F
    masked = bool(b1 & 0x80)
    if masked != from_client:
        raise WebSocketError("Masked frame from the server." if masked else "Unmasked frame from the client.")
    length = b1 & 0x7F
    if opcode >= 0x8 and (not fin or length > 125):
        raise WebSocketError("Bad control frame (fragmented or over 125 bytes).")
    if length == 126:
        (length,) = struct.unpack("!H", read_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", read_exact(sock, 8))
    if length > max_frame:
        raise WebSocketError("Frame too large.")
    key = read_exact(sock, 4) if masked else None
    payload = read_exact(sock, length) if length else b""
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class WebSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.closed = False

    @classmethod
    def connect(cls, port: int, path: str = "/", timeout: float = 3.0, host: str = "127.0.0.1",
                protocol: str | None = None) -> "WebSocket":
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            if protocol:
                lines.append(f"Sec-WebSocket-Protocol: {protocol}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            # Byte by byte (a short response on loopback), so the server's first frame, which may follow in the
            # same packet, stays in the socket for read_frame.
            response = bytearray()
            while not response.endswith(b"\r\n\r\n"):
                chunk = sock.recv(1)
                if not chunk:
                    raise WebSocketError("The server closed the connection during the handshake.")
                response += chunk
                if len(response) > 16384:
                    raise WebSocketError("Handshake response too long.")
            head = bytes(response[:-4])
            status, *header_lines = head.decode("latin-1").split("\r\n")
            if " 101 " not in status + " ":
                raise WebSocketError(f"The server refused the WebSocket upgrade ({status.strip()[:60]}).")
            headers = {}
            for line in header_lines:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            if headers.get("sec-websocket-accept") != accept_key(key):
                raise WebSocketError("Bad Sec-WebSocket-Accept from the server.")
            return cls(sock)
        except BaseException:
            sock.close()
            raise

    def settimeout(self, seconds: float | None) -> None:
        self.sock.settimeout(seconds)

    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(0x1, text.encode("utf-8")))

    def recv_text(self, wait: float | None = None) -> str | None:
        """The next text message, or None when nothing starts arriving within ``wait`` seconds (None = block).

        Answers pings; raises WebSocketError on close. Once a frame starts, the rest must come within the socket's
        timeout (a stalled frame is an error, never a half-read stream)."""
        parts: list[bytes] = []
        total = 0
        while True:
            if wait is not None and not parts:
                readable, _, _ = select.select([self.sock], [], [], max(0.0, wait))
                if not readable:
                    return None
            fin, opcode, payload = read_frame(self.sock)
            if opcode == 0x9:  # ping
                self.sock.sendall(encode_frame(0xA, payload))
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                self.closed = True
                try:
                    self.sock.sendall(encode_frame(0x8, payload[:2]))
                except OSError:
                    pass
                reason = payload[2:].decode("utf-8", "replace")[:120]
                raise WebSocketError(f"Closed by the server ({code}{': ' + reason if reason else ''}).", code)
            if opcode not in (0x0, 0x1, 0x2):
                raise WebSocketError(f"Unknown opcode {opcode}.")
            if opcode != 0x0 and parts:
                raise WebSocketError("A new message started inside a fragmented one.")
            parts.append(payload)
            total += len(payload)
            if total > MAX_MESSAGE:
                raise WebSocketError("Message too large.")
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.sendall(encode_frame(0x8, struct.pack("!H", 1000)))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
