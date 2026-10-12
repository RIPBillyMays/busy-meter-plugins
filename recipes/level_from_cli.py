#!/usr/bin/env python3
"""Set the Busy Meter's level from the command line.

    python level_from_cli.py level 5 --for 10m
    python level_from_cli.py level flash --for 30s
    python level_from_cli.py clear
    python level_from_cli.py status

Same as the ``busymeter`` command installed with ``pip install busymeter``. The first run asks you to click Allow in
the Busy Meter tray (turn on Settings > Sources > External connections first).
"""

import sys
from pathlib import Path

try:
    from busymeter.cli import main
except ImportError:  # running from the repo without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
    from busymeter.cli import main

if __name__ == "__main__":
    sys.exit(main())
