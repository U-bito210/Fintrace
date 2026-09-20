"""Pytest root configuration for FinTrace-CN.

Ensures the project root is importable so ``import src...`` resolves in offline
test runs, regardless of how pytest computes its rootdir.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
