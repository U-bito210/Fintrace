"""Execution-status guard for the real-model competition CLI."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_llm_report_check.py"
SPEC = importlib.util.spec_from_file_location("competition_llm_run", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_all_unextracted_model_output_is_blocked_not_ok():
    status, reason = MODULE._run_status({"summary": {"claims_checked": 0, "sentences_unextracted": 8}})
    assert status == "blocked"
    assert "未执行实质核查" in reason


def test_partial_extraction_stays_visible():
    status, reason = MODULE._run_status({"summary": {"claims_checked": 3, "sentences_unextracted": 2}})
    assert status == "partial"
    assert "2" in reason


def test_complete_extraction_is_ok():
    assert MODULE._run_status({"summary": {"claims_checked": 3, "sentences_unextracted": 0}}) == ("ok", None)


def test_parser_defaults_to_a_small_llm_batch():
    assert MODULE.parser().parse_args([
        "--model", "deepseek-v4-flash", "--draft", "draft.pdf", "--annual", "annual.pdf",
    ]).llm_batch_size == 3
