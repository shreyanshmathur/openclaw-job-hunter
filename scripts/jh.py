#!/usr/bin/env python3
"""openclaw-job-hunter command line entry point (design 3).

    <PY> $REPO/scripts/jh.py <group> [<command>] [args]

Adds scripts/ to sys.path and runs jobhunter.cli.main(), which auto-discovers the command modules.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jobhunter import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main())
