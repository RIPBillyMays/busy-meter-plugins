#!/usr/bin/env python3
"""Focus timer: Focus while a block runs, cleared on breaks (Pomodoro cycles from the settings).

    python focus_timer.py           (or: busymeter focus 25m)

Same as ``busymeter run focus_timer``; the code is in ../python/busymeter/recipes/focus_timer.py and the settings file is
described in README.md. Ctrl+C clears the meter and stops.
"""

import sys
from pathlib import Path

try:
    import busymeter  # noqa: F401
except ImportError:  # running from the repo without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from busymeter.cli import main

if __name__ == "__main__":
    sys.exit(main(["run", "focus_timer", *sys.argv[1:]]))
