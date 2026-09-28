"""Test package. Puts scripts/ on sys.path so `import jobhunter` works from the repo root:

    python3 -m unittest discover -s tests -p 'test_*.py' -t .
"""
from __future__ import annotations

import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
