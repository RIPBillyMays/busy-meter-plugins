"""What every bundled recipe has: a name, a plugin id, a settings schema, its signals, and a poll() that says which
signals are active now. The runner (busymeter.recipes.runner) does the rest: register, refresh, clear, re-register."""

from __future__ import annotations

import sys
import time
from typing import Any, Callable

from .settings import Schema


class Recipe:
    name = ""  # e.g. "focus_timer" (also the settings file name)
    plugin_id = ""  # e.g. "com.thebusymeter.focus"
    title = ""  # shown in the tray's Allow prompt
    summary = ""  # one line for list_recipes
    reads = ""  # what it reads (for list_recipes and the README)
    never_reads = ""  # what it never reads or sends
    schema = Schema({})
    ttl_seconds = 30  # signals expire this long after the last refresh (the runner refreshes at half of it)
    interval = 1.0  # seconds between polls

    def __init__(self, settings: dict[str, Any], *, clock: Callable[[], float] = time.monotonic,
                 say: Callable[[str], None] | None = None):
        self.settings = settings
        self.clock = clock
        self.say = say or (lambda message: print(message, file=sys.stderr))
        self.finished = False  # set by recipes that end on their own (a single focus block)
        self.settings_path: Any = None  # the settings file (set by the runner), for messages

    def signals(self) -> list[dict[str, Any]]:
        """Declared signals with suggested levels, from the current settings."""
        return []

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """New settings from the file (already validated)."""
        self.settings = settings

    def start(self) -> None:
        pass

    def connected(self) -> None:
        """Called once, when Busy Meter first accepted the recipe (after any Allow)."""

    def stop(self) -> None:
        pass

    def poll(self) -> list[str]:
        """The signals that are active now (names from signals())."""
        return []
