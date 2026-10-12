#!/usr/bin/env python3
"""Loudness meter: the louder the room, the more stars.

    pip install sounddevice==0.5.1         # optional package this recipe needs (PortAudio bindings)
    python mic_loudness.py                  # default thresholds
    python mic_loudness.py --levels=-45:2,-35:4,-25:6,-15:flash --dry-run

Privacy: this only measures how loud the microphone is (one number, RMS in dBFS, every ~0.1 s). Audio is never
recorded, stored, sent anywhere or analysed for content; each 0.1 s block is dropped as soon as its loudness is
computed. Only the level goes to the Busy Meter app on this PC. Windows shows the microphone-in-use icon while this
runs, and if the app's microphone source is on it may also count this as "mic in use".

Each threshold ``dB:level`` means "at or above dB, show level". Below the lowest threshold this plugin reports
nothing, so the meter shows whatever the app's other sources say. The level is reported with a 5 s TTL and
refreshed while it holds, so stopping the script clears it within seconds.

The first run asks you to click Allow in the Busy Meter tray (turn on Settings > Sources > External connections).
"""

from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from array import array
from pathlib import Path

try:
    import busymeter  # noqa: F401
except ImportError:  # running from the repo without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from busymeter import BusyMeter, BusyMeterError, normalize_level
from busymeter.cli import call, safe_console

PLUGIN_ID = "com.thebusymeter.loudness"
SAMPLE_RATE = 16000
BLOCK = 1600  # 0.1 s
TTL = 5
DEFAULT_LEVELS = "-45:2,-35:4,-25:6"


def parse_levels(text: str) -> list[tuple[float, int | str]]:
    """'-45:2,-35:4' -> [(-35.0, 4), (-45.0, 2)] (loudest first)."""
    out = []
    for part in text.split(","):
        db, _, level = part.partition(":")
        out.append((float(db), normalize_level(level)))
    if not out:
        raise ValueError("No thresholds.")
    return sorted(out, key=lambda t: t[0], reverse=True)


def rms_dbfs(block: bytes) -> float:
    """Loudness of one block of 16-bit mono samples, in dBFS (0 = full scale, -90 = silence)."""
    samples = array("h")
    samples.frombytes(block[: len(block) - len(block) % 2])
    if not samples:
        return -90.0
    mean_square = sum(s * s for s in samples) / len(samples)
    return max(-90.0, 10 * math.log10(mean_square / (32768.0 ** 2)) if mean_square > 0 else -90.0)


def level_for(db: float, levels: list[tuple[float, int | str]]) -> int | str | None:
    for threshold, level in levels:
        if db >= threshold:
            return level
    return None


class Loudness:
    """A smoothed loudness number. Holds no audio."""

    def __init__(self, attack: float = 0.5, release: float = 0.1):
        self.db = -90.0
        self.attack, self.release = attack, release
        self.lock = threading.Lock()

    def add(self, block: bytes) -> None:
        db = rms_dbfs(block)  # the block is not kept
        with self.lock:
            k = self.attack if db > self.db else self.release
            self.db += k * (db - self.db)

    def value(self) -> float:
        with self.lock:
            return self.db


def main(argv: list[str] | None = None) -> int:
    safe_console()
    parser = argparse.ArgumentParser(description="Show how loud the room is on the Busy Meter (loudness only).")
    parser.add_argument("--levels", default=DEFAULT_LEVELS, help=f"dB:level thresholds (default {DEFAULT_LEVELS})")
    parser.add_argument("--device", default=None, help="input device name or number (default: system default)")
    parser.add_argument("--dry-run", action="store_true", help="print loudness and level; don't touch the meter")
    parser.add_argument("--id", default=PLUGIN_ID, help=f"plugin id (default {PLUGIN_ID})")
    args = parser.parse_args(argv)
    try:
        levels = parse_levels(args.levels)
    except ValueError as e:
        parser.error(f"--levels: {e}")

    try:
        import sounddevice  # optional third-party package
    except ImportError:
        print("This recipe needs the optional 'sounddevice' package: pip install sounddevice==0.5.1", file=sys.stderr)
        return 2

    meter = None
    if not args.dry_run:
        meter = BusyMeter(args.id)
        try:
            meter.connect(name="Loudness meter", wants=("signals",),
                          on_wait=lambda: print("Waiting for you to click Allow in the Busy Meter tray...", file=sys.stderr))
        except BusyMeterError as e:
            print(e.message, file=sys.stderr)
            return 1

    loudness = Loudness()

    def on_audio(indata, frames, time_info, status) -> None:  # noqa: ARG001
        loudness.add(bytes(indata))

    shown: int | str | None = None
    last_sent = 0.0
    try:
        with sounddevice.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK, channels=1, dtype="int16",
                                        device=args.device, callback=on_audio):
            print("Listening for loudness (Ctrl+C stops).", file=sys.stderr)
            while True:
                time.sleep(1)
                db = loudness.value()
                level = level_for(db, levels)
                if args.dry_run:
                    print(f"{db:6.1f} dBFS -> {level if level is not None else '-'}")
                    continue
                now = time.monotonic()
                try:
                    if level != shown or (level is not None and now - last_sent >= TTL / 2):
                        if level is None:
                            call(meter, meter.clear)
                        else:
                            call(meter, lambda: meter.set_level(level, TTL))
                        shown, last_sent = level, now
                except BusyMeterError as e:
                    print(e.message, file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        if meter is not None and shown is not None:
            try:
                meter.clear()
            except BusyMeterError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
