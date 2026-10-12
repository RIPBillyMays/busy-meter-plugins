#!/usr/bin/env python3
"""Vibe meter: active Claude Code sessions (or their token rate) as stars; Vibe alert on a burst.

    python vibe_meter.py

Same as ``busymeter run vibe_meter``; the code is in ../python/busymeter/recipes/vibe_meter.py and the settings file is
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
    sys.exit(main(["run", "vibe_meter", *sys.argv[1:]]))
