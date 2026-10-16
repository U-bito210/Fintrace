"""Pytest root configuration for FinTrace-CN.

Ensures the project root is importable and gives every test a private CARD-14
prompt-usage log.  It also assigns a unique ignored temporary leaf to each
ordinary Windows test run, so a stale ACL cannot prevent fixture setup.
"""

from __future__ import annotations

import tempfile
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


def pytest_configure(config) -> None:
    """Give each default Windows run a disposable, isolated base directory.

    Pytest clears ``basetemp`` before creating its fixtures.  Reusing one
    repository directory therefore makes a later run depend on ACLs left by
    an earlier local process.  A fresh parent lets pytest create an empty leaf
    it owns, while keeping all artefacts in the ignored ``tmp/`` tree.
    """
    if config.option.basetemp is not None:
        return

    temp_root = Path(__file__).resolve().parent / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="pytest-", dir=temp_root))
    config.option.basetemp = run_root / "fixtures"
