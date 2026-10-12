"""Focus timer: a focus block (or Pomodoro cycles) shows as Focus on the meter; breaks clear it.

    busymeter focus 25m              # one 25-minute focus block, then clears and exits
    busymeter focus --pomodoro       # focus / short break cycles, a long break every 4th, from the settings file
    busymeter run focus_timer        # the same cycles as a recipe (also what install_recipe starts)

Reads nothing but the clock. Signal: ``focus`` (suggested level 2, ``focus_level``). Ctrl+C (or the stop file) clears.
"""

from __future__ import annotations

import sys
from typing import Any

from .base import Recipe
from .settings import Field, Schema

SCHEMA = Schema({
    "focus_minutes": Field("float", 25, "Length of a focus block, minutes.", min=1, max=240),
    "short_break_minutes": Field("float", 5, "Short break, minutes (0 = none).", min=0, max=60),
    "long_break_minutes": Field("float", 15, "Long break, minutes (0 = none).", min=0, max=120),
    "long_break_every": Field("int", 4, "A long break after every Nth focus block.", min=1, max=12),
    "cycles": Field("int", 0, "Focus blocks to run, then stop (0 = keep going until stopped).", min=0, max=100),
    "focus_level": Field("level", 2, "Suggested level for the focus signal (0-6 or \"flash\"; remap in the app).", ),
})


class FocusTimer(Recipe):
    name = "focus_timer"
    plugin_id = "com.thebusymeter.focus"
    title = "Focus timer"
    summary = "Focus blocks or Pomodoro cycles: Focus while a block runs, cleared on breaks."
    reads = "Only the clock."
    never_reads = "Anything else: no files, apps or network besides the local plugin API."
    schema = SCHEMA
    ttl_seconds = 30
    interval = 1.0

    def __init__(self, settings: dict[str, Any], **kwargs: Any):
        super().__init__(settings, **kwargs)
        self.block = 0  # focus blocks started
        self.phase = "idle"  # focus | short_break | long_break
        self.phase_end = 0.0
        self.live = bool(getattr(sys.stderr, "isatty", lambda: False)())
        self._last_minute: int | None = None

    def signals(self) -> list[dict[str, Any]]:
        return [{"name": "focus", "label": "Focus block", "suggest": self.settings["focus_level"]}]

    def connected(self) -> None:
        # The block starts once the meter can show it (after the Allow in the tray), not while waiting for it.
        if self.phase == "idle":
            self._start_focus(self.clock())

    def _start_focus(self, at: float) -> None:
        self.block += 1
        self.phase = "focus"
        self.phase_end = at + self.settings["focus_minutes"] * 60
        cycles = self.settings["cycles"]
        of = f"/{cycles}" if cycles else ""
        self._announce(f"Focus {self.block}{of}: {_mmss(self.settings['focus_minutes'] * 60)}.")

    def _start_break(self, at: float) -> None:
        long = self.block % self.settings["long_break_every"] == 0
        minutes = self.settings["long_break_minutes" if long else "short_break_minutes"]
        if minutes <= 0:
            self._start_focus(at)
            return
        self.phase = "long_break" if long else "short_break"
        self.phase_end = at + minutes * 60
        self._announce(f"{'Long' if long else 'Short'} break: {_mmss(minutes * 60)}.")

    def _advance(self, now: float) -> None:
        # Loop so a long sleep (laptop lid) skips whole phases instead of replaying them.
        while not self.finished and now >= self.phase_end:
            at = self.phase_end
            if self.phase == "focus":
                cycles = self.settings["cycles"]
                if cycles and self.block >= cycles:
                    self.finished = True
                    self._announce("Done.")
                    return
                self._start_break(at)
            else:
                self._start_focus(at)

    def poll(self) -> list[str]:
        now = self.clock()
        if self.phase == "idle":
            return []
        self._advance(now)
        if self.finished:
            return []
        left = max(0.0, self.phase_end - now)
        label = "Focus" if self.phase == "focus" else "Break"
        if self.live:
            sys.stderr.write(f"\r{label}: {_mmss(left)} left ")
            sys.stderr.flush()
        else:
            minute = int(left // 60)
            if minute != self._last_minute:
                self._last_minute = minute
                if left >= 60:
                    self.say(f"{label}: {_mmss(left)} left.")
        return ["focus"] if self.phase == "focus" else []

    def _announce(self, message: str) -> None:
        if self.live:
            sys.stderr.write("\n")
        self._last_minute = None
        self.say(message)


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
