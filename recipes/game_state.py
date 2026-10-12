#!/usr/bin/env python3
"""Game state receiver: a 127.0.0.1 endpoint for a game's official state push; rules -> signals.

    python game_state.py            (python game_state.py --show-token prints the token)

Same as ``busymeter run game_state``; the code is in ../python/busymeter/recipes/game_state.py and the settings file is
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
    sys.exit(main(["run", "game_state", *sys.argv[1:]]))
