#!/usr/bin/env python3
"""Stream status: OBS live -> 'live' (5 stars), recording only -> 'recording' (4 stars).

    python stream_status.py

Same as ``busymeter run stream_status``; the code is in ../python/busymeter/recipes/stream_status.py and the settings file is
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
    sys.exit(main(["run", "stream_status", *sys.argv[1:]]))
