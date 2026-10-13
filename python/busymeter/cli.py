"""``busymeter`` command: set a level from the command line.

    busymeter level 5 --for 10m     # 5 stars for 10 minutes, then back to whatever the app shows
    busymeter level flash --for 30s
    busymeter level 4 --for 2h      # longer than 10 minutes: stays running and refreshes; Ctrl+C clears
    busymeter clear
    busymeter status
    busymeter run vibe_meter        # a bundled recipe: stream_status, game_state, focus_timer, vibe_meter
    busymeter focus 25m             # a focus block (--pomodoro for cycles)
    busymeter recipes               # which recipes are installed or running
    busymeter install focus_timer   # write its default settings and start it in the background
    busymeter stop vibe_meter       # stop one running in the background
    busymeter uninstall vibe_meter  # stop it and delete its settings, token and sign-in start

The first run asks you to click Allow in the Busy Meter tray (needs Settings > Sources > External connections on).
"""

from __future__ import annotations

import argparse
import sys
import time

from .client import MAX_TTL_SECONDS, BusyMeter, BusyMeterError, normalize_level, parse_duration

DEFAULT_ID = "com.thebusymeter.cli"
REFRESH_TTL = 60


def safe_console() -> None:
    """Messages use arrows (Settings → Sources); don't crash on a console code page without them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def call(meter, fn):
    """Run fn; if the token was revoked, register again (the user clicks Allow in the tray) and retry once."""
    reconnecting = getattr(meter, "reconnecting", None)
    return reconnecting(fn) if reconnecting is not None else fn()


def _connect(meter: BusyMeter) -> None:
    meter.connect(
        name="Busy Meter command line",
        wants=("signals", "status"),
        on_wait=lambda: print("Waiting for you to click Allow in the Busy Meter tray...", file=sys.stderr),
    )


def hold_level(meter: BusyMeter, level: int | str, seconds: int, *, sleep=time.sleep, clock=time.monotonic) -> None:
    """Show ``level`` for ``seconds``. Up to 10 minutes is one call; longer refreshes every 30 s, then clears."""
    if seconds <= MAX_TTL_SECONDS:
        call(meter, lambda: meter.set_level(level, seconds))
        return
    end = clock() + seconds
    try:
        while True:
            left = end - clock()
            if left <= 0:
                break
            ttl = max(1, min(REFRESH_TTL, int(left + 0.999)))
            call(meter, lambda: meter.set_level(level, ttl))
            sleep(min(REFRESH_TTL / 2, max(left, 0)))
    finally:
        try:
            meter.clear()
        except BusyMeterError:
            pass


def main(argv: list[str] | None = None) -> int:
    safe_console()
    parser = argparse.ArgumentParser(prog="busymeter", description="Set the Busy Meter's level from the command line.")
    parser.add_argument("--id", default=DEFAULT_ID, help=f"plugin id to register as (default {DEFAULT_ID})")
    sub = parser.add_subparsers(dest="command", required=True)
    level = sub.add_parser("level", help="show a level for a while")
    level.add_argument("level", help="0-6 (stars; 0 = lip accent only) or flash")
    level.add_argument("--for", dest="duration", default="10m", help="how long, e.g. 30s, 10m, 2h (default 10m)")
    sub.add_parser("clear", help="clear this command's level now")
    sub.add_parser("status", help="print what the meter shows")
    from .recipes import cli as recipes_cli

    recipes_cli.add_commands(sub)
    args = parser.parse_args(argv)
    if recipes_cli.handles(args.command):
        return recipes_cli.dispatch(args, parser)

    meter = BusyMeter(args.id)
    try:
        if args.command == "level":
            try:
                value = normalize_level(args.level)
                seconds = parse_duration(args.duration)
            except ValueError as e:
                parser.error(str(e))
            _connect(meter)
            if seconds > MAX_TTL_SECONDS:
                print(f"Holding {value} for {seconds} s; keep this running (Ctrl+C clears).", file=sys.stderr)
            hold_level(meter, value, seconds)
            if seconds <= MAX_TTL_SECONDS:
                print(f"Level {value} for {seconds} s.")
        elif args.command == "clear":
            _connect(meter)
            call(meter, meter.clear)
            print("Cleared.")
        else:
            _connect(meter)
            s = call(meter, meter.status)
            print(f"level={s.get('level')} paused={str(s.get('paused')).lower()} source={s.get('source')}")
    except KeyboardInterrupt:
        return 130
    except BusyMeterError as e:
        print(e.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
