"""Command-line entry points for recipes (wired into the ``busymeter`` command):

    busymeter run <recipe>          # stream_status, game_state, focus_timer, vibe_meter; Ctrl+C clears and stops
    busymeter focus 25m             # one focus block; --pomodoro for cycles
    busymeter recipes [--json]      # bundled recipes, running or not, and where their settings are
    busymeter install <recipe>      # write its default settings and start it in the background
    busymeter stop <recipe>         # stop one running in the background (it clears its signals; exit 1: not running)
    busymeter uninstall <recipe>    # stop it, delete its settings, saved token and sign-in start
    busymeter autostart [<recipe> on|off]   # Windows: start a recipe at every sign-in (off by default)

The Busy Meter app's Settings > Sources > Recipes rows run ``recipes --json``, ``install``, ``stop``, ``uninstall`` and
``autostart`` (bundled names only) and read only the JSON and the exit codes.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .. import recipes as registry
from ..client import parse_duration
from . import control
from .autostart import Autostart
from .control import RunFiles, stop
from .runner import EXIT_ALREADY, EXIT_DENIED, EXIT_SETTINGS, Runner, install_stop_handlers
from .settings import SettingsError, SettingsFile
from ..tokens import ensure_private_dir


def add_commands(sub: Any) -> None:
    run = sub.add_parser("run", help="run a bundled recipe (Ctrl+C clears and stops)")
    run.add_argument("recipe", choices=registry.names())
    run.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--at-sign-in", dest="at_sign_in", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--show-token", action="store_true",
                     help="game_state: print the token the game must send, then exit")
    focus = sub.add_parser("focus", help="focus timer: busymeter focus 25m, or --pomodoro")
    focus.add_argument("duration", nargs="?", default=None, help="focus length, e.g. 25m (default: settings, 25m)")
    focus.add_argument("--pomodoro", action="store_true", help="repeat focus/break cycles (long break every 4th)")
    focus.add_argument("--cycles", type=int, default=None, help="with --pomodoro: focus blocks to run (0 = until stopped)")
    focus.add_argument("--break", dest="short_break", default=None, help="short break length, e.g. 5m")
    focus.add_argument("--long-break", dest="long_break", default=None, help="long break length, e.g. 15m")
    listing = sub.add_parser("recipes", help="list the bundled recipes and whether they're running")
    listing.add_argument("--json", dest="as_json", action="store_true", help="print the list as JSON (for the app)")
    install_cmd = sub.add_parser("install", help="write a recipe's default settings and start it in the background")
    install_cmd.add_argument("recipe", choices=registry.names())
    stop_cmd = sub.add_parser("stop", help="stop a recipe running in the background")
    stop_cmd.add_argument("recipe", choices=registry.names())
    uninstall_cmd = sub.add_parser("uninstall", help="stop a recipe and delete its settings, token and sign-in start")
    uninstall_cmd.add_argument("recipe", choices=registry.names())
    auto = sub.add_parser("autostart", help="Windows: start a recipe every time you sign in (no arguments: list)")
    auto.add_argument("recipe", nargs="?", choices=registry.names())
    auto.add_argument("state", nargs="?", choices=("on", "off"))


def handles(command: str) -> bool:
    return command in ("run", "focus", "recipes", "install", "stop", "uninstall", "autostart")


def dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "recipes":
        return list_recipes_json() if args.as_json else list_recipes()
    if args.command == "install":
        return install(args.recipe)
    if args.command == "uninstall":
        return uninstall(args.recipe)
    if args.command == "stop":
        return stop_command(args.recipe)
    if args.command == "autostart":
        if args.recipe is None:
            return list_autostart()
        if args.state is None:
            parser.error("autostart: say on or off, e.g. busymeter autostart vibe_meter on")
        return set_autostart(args.recipe, args.state == "on")
    if args.command == "focus":
        overrides: dict[str, Any] = {}
        try:
            if args.duration is not None:
                overrides["focus_minutes"] = parse_duration(args.duration) / 60
            if args.short_break is not None:
                overrides["short_break_minutes"] = parse_duration(args.short_break) / 60
            if args.long_break is not None:
                overrides["long_break_minutes"] = parse_duration(args.long_break) / 60
        except ValueError as e:
            parser.error(str(e))
        if args.pomodoro:
            if args.cycles is not None:
                overrides["cycles"] = args.cycles
        else:
            overrides["cycles"] = 1
        return run_recipe("focus_timer", overrides=overrides)
    if args.show_token:
        return show_token(args.recipe)
    if args.at_sign_in:
        return sign_in_start(args.recipe)
    return run_recipe(args.recipe, background=args.background)


def run_recipe(name: str, *, overrides: dict[str, Any] | None = None, background: bool = False) -> int:
    cls = registry.get(name)
    if background:
        _log_to_file(name)
    stop_event = threading.Event()
    install_stop_handlers(stop_event)
    try:
        runner = Runner(cls, overrides=overrides, stop_event=stop_event)
        return runner.run()
    except SettingsError as e:
        print(str(e), file=sys.stderr)
        return EXIT_SETTINGS
    except KeyboardInterrupt:
        return 130


def sign_in_start(name: str, *, directory: Any = None, executable: str | None = None,
                  starter: Any = None, runner: Any = None) -> int:
    """What the sign-in launcher runs. Starts nothing when the recipe already runs (and leaves its log alone); under
    a console python.exe (no pythonw.exe in this environment) it hands off to a hidden background process and exits;
    under pythonw.exe it runs here with a fresh log, waiting for the app like any recipe. After a Deny it starts
    nothing (and says so in the log) until autostart is turned on again, so a Deny isn't asked again every sign-in."""
    registry.get(name)  # bundled names only
    files = RunFiles(name, directory)
    if files.status()["running"]:
        return EXIT_ALREADY
    if files.denied():
        _append_log(files, f"{time.strftime('%Y-%m-%d %H:%M:%S')} Not started at sign-in: you clicked Deny in the "
                           f"Busy Meter tray last time. To start it at sign-in again, run: busymeter autostart {name} on")
        return EXIT_DENIED
    exe = Path(executable or sys.executable).name.lower()
    if exe != "pythonw.exe":
        (starter or control.start_background)(name, directory)
        return 0
    try:
        ensure_private_dir(files.directory, restrict=not files.directory.exists())  # narrowed when it was made
        files.log_path.write_text("", encoding="utf-8")  # fresh each sign-in, like a background start
    except OSError:
        pass
    return (runner or (lambda n: run_recipe(n, background=True)))(name)


def _append_log(files: RunFiles, text: str) -> None:
    try:
        with open(files.log_path, "a", encoding="utf-8") as log:
            log.write(text + "\n")
    except OSError:
        pass


def list_autostart(auto: Autostart | None = None) -> int:
    auto = auto or Autostart()
    if not auto.supported:
        print("Starting recipes at sign-in works on Windows only.")
        return 0
    for name, on in auto.states().items():
        if on and auto.skipped(name):
            state = f"on, but skipped: you clicked Deny (busymeter autostart {name} on to start it again)"
        else:
            state = "starts at sign-in" if on else "off"
        print(f"{name:14} {state}")
    return 0


def set_autostart(name: str, on: bool, auto: Autostart | None = None) -> int:
    auto = auto or Autostart()
    result = auto.enable(name) if on else auto.disable(name)
    if not result["ok"]:
        print(result["message"], file=sys.stderr)
        return 1
    if on:
        print(f"{name} will start every time you sign in to Windows (in the background; the first time, click "
              f"Allow in the Busy Meter tray; if you click Deny it won't start at sign-in again until you turn it on "
              f"again). Turn it off with: busymeter autostart {name} off")
    else:
        print(f"{name} won't start at sign-in any more." if result.get("wasOn") else
              f"{name} doesn't start at sign-in (it was already off).")
    return 0


def show_token(name: str) -> int:
    if name != "game_state":
        print("Only game_state has a token.", file=sys.stderr)
        return 2
    settings = SettingsFile(name, registry.get(name).schema)
    try:
        values = settings.ensure()
    except SettingsError as e:
        print(str(e), file=sys.stderr)
        return EXIT_SETTINGS
    print(f"Token (kept in {settings.path}):\n{values['token']}")
    return 0


def list_recipes() -> int:
    for name in registry.names():
        cls = registry.get(name)
        status = RunFiles(name).status()
        settings = SettingsFile(name, cls.schema)
        state = status["state"] if status["running"] else ("installed" if settings.exists() else "not installed")
        print(f"{name:14} {state:18} {cls.summary}")
        if settings.exists():
            print(f"{'':14} settings: {settings.path}")
    return 0


# What ``recipes --json`` prints for each recipe: names, flags and states only (no paths, messages or settings), so the
# app has nothing to show raw. "version" changes only if a field's meaning does.
JSON_FIELDS = ("name", "title", "pluginId", "installed", "running", "state", "startsAtSignIn", "signInSkipped")


def list_recipes_json(manager: Any = None) -> int:
    listed = (manager or _manager()).list_recipes()["recipes"]
    print(json.dumps({"version": 1, "recipes": [{k: r[k] for k in JSON_FIELDS} for r in listed]}))
    return 0


def stop_command(name: str, stopper: Any = None) -> int:
    """busymeter stop: exits 1 when nothing was running, so a caller (the app's Stop button) can tell."""
    result = (stopper or stop)(name)
    print(result["message"])
    return 0 if result.get("wasRunning") else 1


def install(name: str, manager: Any = None) -> int:
    """Same as the MCP tool install_recipe: default settings, then a background start (the user clicks Allow)."""
    result = (manager or _manager()).install_recipe(name)
    print(result["message"], file=sys.stdout if result["ok"] else sys.stderr)
    return 0 if result["ok"] else 1


def uninstall(name: str, manager: Any = None) -> int:
    """Same as the MCP tool uninstall_recipe: stop, clear, delete settings, token and sign-in start."""
    result = (manager or _manager()).uninstall_recipe(name)
    print(result["message"], file=sys.stdout if result["ok"] else sys.stderr)
    return 0 if result["ok"] else 1


def _manager() -> Any:
    from .manage import Manager  # here, so `busymeter level` doesn't load it

    return Manager()


def _log_to_file(name: str) -> None:
    """Background runs have no console: send what they print to <recipes>/<name>.log (started fresh by the
    launcher, which also points the process's own stderr at it; appended to here)."""
    files = RunFiles(name)
    try:
        ensure_private_dir(files.directory)
        log = open(files.log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        log = io.StringIO()
    sys.stdout = sys.stderr = log
