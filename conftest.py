"""Pytest root configuration for FinTrace-CN.

Ensures the project root is importable so ``import src...`` resolves in offline
test runs, regardless of how pytest computes its rootdir.

Also gives every test a private CARD-14 prompt-usage log: the registry's usage
log is process-global by design (one run, one audit trail), so leaving it to
accumulate across tests would make any assertion on it order-dependent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _isolate_prompt_usage_log():
    from src.agents.prompts import reset_usage

    reset_usage()
    yield
    reset_usage()
