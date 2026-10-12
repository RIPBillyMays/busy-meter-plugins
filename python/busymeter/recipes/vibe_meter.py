"""Vibe meter: how hard you're vibing with AI coding, from Claude Code's local transcript files.

Counts active Claude Code sessions (a transcript file under ``~/.claude/projects/<project>/<session>.jsonl`` that
changed in the last ``active_minutes``, default 3) and their token rate, and shows it as stars: by default 1 active
session = 1 star ... 6 or more = 6 stars (``driver`` = "sessions"), or by tokens per minute (``driver`` = "tokens",
with ``token_thresholds``). ``vibe-alert`` (suggested "flash", shown as "Vibe alert" in the Gamer names) fires for
``alert_seconds`` (default 20) when parallel sessions reach ``burst_sessions`` (default 4) or the token rate reaches
``burst_tokens_per_minute``, at most once per ``alert_cooldown_seconds``.

What it reads, and only this:
- File metadata (names, sizes, modification times) of ``<projects>/*/*.jsonl``.
- From lines appended to active files: only the top-level assistant message's ``usage`` token counts (input,
  output, cache creation, cache read) and its id (to count a message once), found by walking the line's top-level
  keys. Message text, tool calls, file contents and prompts are skipped by position, never decoded, kept, printed
  or sent; each line is dropped once its numbers are taken.
It opens the files read-only, reads only what was appended since the last look (files present at start are read from
their end), never writes to them, and sends nothing anywhere except signal names to the Busy Meter app on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from .base import Recipe
from .settings import Field, Schema, ascending_numbers

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
MAX_LINE = 2 * 1024 * 1024  # longer lines are skipped (their usage too)
MAX_READ_PER_TICK = 8 * 1024 * 1024  # per file; the rest is read on the next ticks
MAX_FILES = 512
WINDOW_SECONDS = 60.0

_WS = re.compile(rb"[ \t\r\n]*")
_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"', re.S)
_NEXT = re.compile(rb'["{}\[\]]')
_SCALAR = re.compile(rb"-?[0-9][0-9eE.+-]*|true|false|null")

SCHEMA = Schema({
    "driver": Field("choice", "sessions", "What sets the stars: \"sessions\" (active sessions) or \"tokens\" "
                                          "(tokens per minute).", choices=("sessions", "tokens")),
    "active_minutes": Field("float", 3, "A session is active if its transcript changed in the last N minutes.",
                            min=0.5, max=60),
    "session_thresholds": Field("list", [1, 2, 3, 4, 5, 6], "Sessions needed for 1, 2, ... stars (lowest first, "
                                "up to 6 entries).", check=ascending_numbers(6, 1, 1000)),
    "token_thresholds": Field("list", [2000, 10000, 25000, 50000, 100000, 200000], "Tokens per minute needed for "
                              "1, 2, ... stars (lowest first, up to 6 entries).", check=ascending_numbers(6, 1, 1e9)),
    "count_cache_reads": Field("bool", False, "Count cache-read tokens in the token rate (they're large and cheap; "
                                              "off by default)."),
    "burst_sessions": Field("int", 4, "Vibe alert at this many parallel sessions (0 = never).", min=0, max=100),
    "burst_tokens_per_minute": Field("int", 150000, "Vibe alert at this token rate (0 = never).", min=0,
                                     max=1_000_000_000),
    "alert_seconds": Field("int", 20, "How long a vibe alert shows.", min=3, max=300),
    "alert_cooldown_seconds": Field("int", 300, "Quiet time after an alert before the next one.", min=0, max=86400),
    "star_levels": Field("bool", True, "Suggest 1-6 stars for vibe-1 ... vibe-6 (remap in the app)."),
    "alert_level": Field("level", "flash", "Suggested level for vibe-alert (remap in the app)."),
    "scan_seconds": Field("float", 5, "How often to look at the files.", min=1, max=60),
    "projects_dir": Field("str", "", "Transcript folder (empty = ~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects). "
                                     "Set it in this file only.", agent=False, max_length=1024),
})


def default_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


class _Malformed(Exception):
    pass


def _ws(data: bytes, i: int) -> int:
    return _WS.match(data, i).end()  # type: ignore[union-attr]


def _skip_value(data: bytes, i: int) -> int:
    """Index just past the JSON value at data[i] (after white space). Strings are skipped by a regex, never decoded."""
    i = _ws(data, i)
    if i >= len(data):
        raise _Malformed
    c = data[i]
    if c == 0x22:  # "
        m = _STRING.match(data, i)
        if not m:
            raise _Malformed
        return m.end()
    if c in (0x7B, 0x5B):  # { [
        depth = 0
        while True:
            m = _NEXT.search(data, i)
            if not m:
                raise _Malformed
            ch = data[m.start()]
            if ch == 0x22:
                sm = _STRING.match(data, m.start())
                if not sm:
                    raise _Malformed
                i = sm.end()
                continue
            depth += 1 if ch in (0x7B, 0x5B) else -1
            i = m.start() + 1
            if depth == 0:
                return i
    m = _SCALAR.match(data, i)
    if not m:
        raise _Malformed
    return m.end()


def _members(data: bytes, i: int):
    """(raw key, value start, value end) for each member of the object at data[i]; values aren't decoded."""
    if data[i] != 0x7B:
        raise _Malformed
    i = _ws(data, i + 1)
    if i < len(data) and data[i] == 0x7D:
        return
    while True:
        m = _STRING.match(data, i)
        if not m:
            raise _Malformed
        key = data[m.start() + 1:m.end() - 1]
        i = _ws(data, m.end())
        if i >= len(data) or data[i] != 0x3A:  # :
            raise _Malformed
        start = _ws(data, i + 1)
        end = _skip_value(data, start)
        yield key, start, end
        i = _ws(data, end)
        if i < len(data) and data[i] == 0x2C:  # ,
            i = _ws(data, i + 1)
            continue
        if i < len(data) and data[i] == 0x7D:
            return
        raise _Malformed


def usage_from_line(line: bytes) -> tuple[str | None, tuple[int, int, int, int]] | None:
    """(message id, (input, output, cache creation, cache read)) from an assistant line, else None.

    Walks only the line's top-level keys and the top-level ``message`` object's keys; every other value (message
    text, tool calls and their inputs, tool results) is skipped by position without being decoded. Counts only when
    the top-level ``type`` is "assistant", and only ``message.usage`` (never a "usage" or "type" inside content).
    """
    if b'"assistant"' not in line or b'"usage"' not in line:
        return None  # cheap filter: most lines aren't assistant lines with usage
    try:
        start = _ws(line, 0)
        if start >= len(line):
            return None
        kind = message = None
        for key, vs, ve in _members(line, start):
            if key == b"type":
                kind = line[vs:ve]
            elif key == b"message":
                message = vs
        if kind != b'"assistant"' or message is None or line[message] != 0x7B:
            return None
        usage = msg_id = None
        for key, vs, ve in _members(line, message):
            if key == b"usage" and line[vs] == 0x7B and ve - vs <= 65536:
                usage = json.loads(line[vs:ve])
            elif key == b"id" and line[vs] == 0x22 and ve - vs <= 130:
                value = json.loads(line[vs:ve])
                msg_id = value if isinstance(value, str) else None
    except (_Malformed, ValueError, RecursionError, IndexError):
        return None
    if not isinstance(usage, dict) or not isinstance(usage.get("output_tokens"), int):
        return None
    numbers = []
    for key in USAGE_KEYS:
        value = usage.get(key, 0)
        numbers.append(value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0)
    return msg_id, tuple(numbers)  # type: ignore[return-value]


class _Tracked:
    __slots__ = ("offset", "key", "partial", "skipping", "seen")

    def __init__(self, offset: int, key: Any):
        self.offset = offset
        self.key = key
        self.partial = b""
        self.skipping = False  # inside an over-long line: drop bytes until its end
        self.seen: OrderedDict[str, tuple[int, int, int, int]] = OrderedDict()


class TranscriptWatcher:
    """Active sessions and their new token counts, read incrementally."""

    def __init__(self, root: Path, clock_wall: Any = None):
        import time as _time

        self.root = root
        self.wall = clock_wall or _time.time
        self.files: dict[str, _Tracked] = {}
        self.sizes: dict[str, int] = {}  # every transcript's size at the last scan (active or not)
        self.first_scan = True
        self.bytes_read = 0  # for tests

    def scan(self, active_seconds: float) -> tuple[int, list[tuple[int, int, int, int]]]:
        """(active sessions, token deltas read since the last scan)."""
        now = self.wall()
        try:
            candidates = list(self.root.glob("*/*.jsonl"))
        except OSError:
            candidates = []
        active: list[tuple[Path, os.stat_result]] = []
        sizes: dict[str, int] = {}
        for path in candidates:
            try:
                st = path.stat()
            except OSError:
                continue
            sizes[str(path)] = st.st_size
            if now - st.st_mtime <= active_seconds:
                active.append((path, st))
        active.sort(key=lambda item: item[1].st_mtime, reverse=True)
        active = active[:MAX_FILES]
        deltas: list[tuple[int, int, int, int]] = []
        keep: set[str] = set()
        for path, st in active:
            name = str(path)
            keep.add(name)
            key = (getattr(st, "st_ino", 0), getattr(st, "st_dev", 0))
            tracked = self.files.get(name)
            if tracked is None:
                # Present at the first look: start at the end (history isn't counted). Seen before while idle: from
                # its size then. Never seen: a new session, from the start.
                if self.first_scan:
                    start = st.st_size
                else:
                    start = min(self.sizes.get(name, 0), st.st_size)
                tracked = _Tracked(start, key)
                self.files[name] = tracked
            elif tracked.key != key or st.st_size < tracked.offset:
                # Replaced or cut short: skip what's there now rather than count it again.
                tracked.offset, tracked.key, tracked.partial, tracked.skipping = st.st_size, key, b"", False
            if st.st_size > tracked.offset:
                deltas.extend(self._read(path, tracked, st.st_size))
        for name in list(self.files):
            if name not in keep:
                del self.files[name]  # inactive: forget (picked up from its end if it wakes again)
        self.sizes = sizes
        self.first_scan = False
        return len(active), deltas

    def _read(self, path: Path, tracked: _Tracked, size: int) -> list[tuple[int, int, int, int]]:
        out: list[tuple[int, int, int, int]] = []
        want = min(size - tracked.offset, MAX_READ_PER_TICK)
        try:
            with open(path, "rb") as f:  # read-only
                f.seek(tracked.offset)
                chunk = f.read(want)
        except OSError:
            return out
        tracked.offset += len(chunk)
        self.bytes_read += len(chunk)
        data = tracked.partial + chunk
        lines = data.split(b"\n")
        tracked.partial = lines.pop()  # incomplete last line (or b"")
        for line in lines:
            if tracked.skipping:
                tracked.skipping = False  # this piece ends the over-long line
                continue
            if len(line) > MAX_LINE:
                continue
            found = usage_from_line(line)
            if found is None:
                continue
            msg_id, numbers = found
            if msg_id is None:
                out.append(numbers)
                continue
            before = tracked.seen.get(msg_id)
            if before is not None:
                numbers_delta = tuple(max(0, n - b) for n, b in zip(numbers, before))
                tracked.seen[msg_id] = tuple(max(n, b) for n, b in zip(numbers, before))  # type: ignore[assignment]
                tracked.seen.move_to_end(msg_id)
                if any(numbers_delta):
                    out.append(numbers_delta)  # type: ignore[arg-type]
                continue
            tracked.seen[msg_id] = numbers
            if len(tracked.seen) > 512:
                tracked.seen.popitem(last=False)
            out.append(numbers)
        if len(tracked.partial) > MAX_LINE:
            tracked.partial = b""
            tracked.skipping = True
        return out


class VibeMeter(Recipe):
    name = "vibe_meter"
    plugin_id = "com.thebusymeter.vibe"
    title = "Vibe meter (Claude Code)"
    summary = "Active Claude Code sessions (or their token rate) as stars; Vibe alert on a burst."
    reads = ("File names, sizes and times of ~/.claude/projects/*/*.jsonl, and only the token counts and message id "
             "of new assistant lines.")
    never_reads = "Message text, prompts, tool calls or file contents; it never writes those files or sends them."
    schema = SCHEMA
    ttl_seconds = 30
    interval = 1.0

    def __init__(self, settings: dict[str, Any], *, wall_clock: Any = None, **kwargs: Any):
        super().__init__(settings, **kwargs)
        self.wall_clock = wall_clock
        self.watcher = TranscriptWatcher(self._root(), wall_clock)
        self.window: deque[tuple[float, int]] = deque()
        self.sessions = 0
        self.rate = 0
        self.stars = 0
        self.alert_until = 0.0
        self.last_alert: float | None = None
        self.bursting = False
        self.next_scan = 0.0
        self._last_report: tuple[int, int, int] | None = None

    def _root(self) -> Path:
        text = self.settings.get("projects_dir") or ""
        return Path(os.path.expandvars(os.path.expanduser(text))) if text else default_projects_dir()

    def signals(self) -> list[dict[str, Any]]:
        levels = self.settings["star_levels"]
        out: list[dict[str, Any]] = []
        for n in range(1, 7):
            entry: dict[str, Any] = {"name": f"vibe-{n}", "label": f"Vibe {n}"}
            if levels:
                entry["suggest"] = n
            out.append(entry)
        out.append({"name": "vibe-alert", "label": "Vibe alert (burst)", "suggest": self.settings["alert_level"]})
        return out

    def apply_settings(self, settings: dict[str, Any]) -> None:
        old_root = self._root()
        super().apply_settings(settings)
        if self._root() != old_root:
            self.watcher = TranscriptWatcher(self._root(), self.wall_clock)
        self.next_scan = 0.0

    def start(self) -> None:
        root = self._root()
        if not root.is_dir():
            self.say(f"No Claude Code transcripts folder at {root} yet; watching for it.")

    def _tokens(self, numbers: tuple[int, int, int, int]) -> int:
        total = numbers[0] + numbers[1] + numbers[2]
        if self.settings["count_cache_reads"]:
            total += numbers[3]
        return total

    def update(self, now: float) -> None:
        sessions, deltas = self.watcher.scan(self.settings["active_minutes"] * 60)
        added = sum(self._tokens(d) for d in deltas)
        if added:
            self.window.append((now, added))
        while self.window and now - self.window[0][0] > WINDOW_SECONDS:
            self.window.popleft()
        self.sessions = sessions
        self.rate = sum(n for _, n in self.window)  # tokens in the last 60 s = tokens per minute
        if self.settings["driver"] == "tokens":
            value, thresholds = self.rate, self.settings["token_thresholds"]
        else:
            value, thresholds = sessions, self.settings["session_thresholds"]
        self.stars = min(6, sum(1 for t in thresholds if value >= t))
        burst_s, burst_t = self.settings["burst_sessions"], self.settings["burst_tokens_per_minute"]
        bursting = bool((burst_s and sessions >= burst_s) or (burst_t and self.rate >= burst_t))
        cooled = self.last_alert is None or now - self.last_alert >= self.settings["alert_cooldown_seconds"]
        if bursting and not self.bursting and cooled:
            self.alert_until = now + self.settings["alert_seconds"]
            self.last_alert = now
            self.say(f"Vibe alert: {sessions} sessions, {self.rate} tokens/min.")
        self.bursting = bursting
        report = (sessions, self.stars, self.rate // 1000)
        if report != self._last_report:
            self._last_report = report
            self.say(f"{sessions} active session{'s' if sessions != 1 else ''}, ~{self.rate} tokens/min -> "
                     f"{self.stars} star{'s' if self.stars != 1 else ''}.")

    def poll(self) -> list[str]:
        now = self.clock()
        if now >= self.next_scan:
            self.next_scan = now + self.settings["scan_seconds"]
            self.update(now)
        out = [f"vibe-{self.stars}"] if self.stars else []
        if now < self.alert_until:
            out.append("vibe-alert")
        return out
