"""Offline tests for CARD-07: Run Manifest & Audit Trail.

Covers: 17-field manifest completeness, append-only event ordering, digest
stability, interrupted marking, replay reject (modified input), replay
deterministic full-match, sampled-layer drift, and append-only tamper-proofing.
"""

from __future__ import annotations

from pathlib import Path

from src.cn.runs import (
    RunConfig,
    RunInputDoc,
    RunManifest,
    RunManifestError,
    RunRecordError,
    Stage,
    digest_of,
    load_events,
    load_manifest,
    replay,
    validate_manifest_fields,
)
from src.cn.runs.recorder import RunRecorder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config(tmp_path: Path, doc_path: Path) -> RunConfig:
    return RunConfig(
        input_documents=[RunInputDoc(sha256=digest_of(doc_path.read_bytes()), path=str(doc_path))],
        parser_name="pdf_text",
        parser_version="1.4.0",
        execution_mode="offline",
        snapshot_id="600519.SH_20260810",
        tool_versions={"tushare": "1.2.3"},
        random_seed=42,
    )


def _deterministic_pipeline(rec: RunRecorder, calc_value: float = 0.61) -> None:
    """Emit one event per pipeline stage (deterministic)."""
    rec.emit(Stage.PARSE, "parsed", input_={"pages": 120}, output={"facts": 88}, status="ok")
    rec.emit(Stage.NORMALIZE, "normalized", input_={"raw": "1,234"}, output={"value": 1234.0}, status="ok")
    rec.emit(Stage.RETRIEVAL, "retrieved", input_={"query": "CFO"}, output={"hits": 3}, status="ok")
    rec.emit(Stage.TOOL_CALL, "get_cn_financials", input_={"symbol": "600519.SH"},
             output={"rows": 12}, status="ok")
    rec.emit(Stage.CALCULATION, "cfoni", input_={"cfo": 100, "ni": 164},
             output={"ratio": calc_value}, status="ok", calculation_id="CAL12")
    rec.emit(Stage.CLAIM, "claim_c18", input_={"ratio": calc_value},
             output={"text": "盈利质量稳定"}, status="ok")
    rec.emit(Stage.VALIDATION, "validated", input_={"claim": "claim_c18"},
             output={"status": "supported"}, status="ok")
    rec.emit(Stage.REPORT, "report_written", input_={"claim": "claim_c18"},
             output={"chars": 4200}, status="ok")


def _rerun_events(store_dir: Path, config: RunConfig, *, calc_value: float = 0.61) -> list[dict]:
    """Faithful re-execution of the pipeline via a fresh recorder."""
    rec = RunRecorder(store_dir)
    run_id = rec.start(config)
    _deterministic_pipeline(rec, calc_value=calc_value)
    rec.finish("ok")
    return [e.to_dict() for e in load_events(store_dir, run_id)]


# ---------------------------------------------------------------------------
# §11 / §15 unit tests
# ---------------------------------------------------------------------------
def test_manifest_17_fields_complete(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    rec = RunRecorder(tmp_path)
    run_id = rec.start(_make_config(tmp_path, doc))
    m = load_manifest(tmp_path, run_id)

    assert len(m.to_dict()) == 17
    validate_manifest_fields(m.to_dict())  # must not raise
    # offline mode: model series may be None but MUST exist as keys
    assert m.model is None and m.model_provider is None and m.model_version is None
    assert m.execution_mode == "offline"
    assert m.input_document_sha256 == [digest_of(doc.read_bytes())]


def test_manifest_rejects_wrong_field_count():
    good = {k: (None if k != "run_id" else "x") for k in [
        "run_id", "started_at", "finished_at", "input_document_sha256", "snapshot_id",
        "model", "model_provider", "model_version", "temperature", "prompt_hash",
        "skill_hash", "parser_name", "parser_version", "git_commit", "tool_versions",
        "random_seed", "execution_mode"]}
    validate_manifest_fields(good)  # 17 keys ok
    for dropped in ("model", "git_commit"):
        bad = {k: v for k, v in good.items() if k != dropped}
        try:
            validate_manifest_fields(bad)
        except RunManifestError:
            pass
        else:
            raise AssertionError("expected RunManifestError for missing field")


def test_event_append_order_monotonic_ts(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"data")
    rec = RunRecorder(tmp_path)
    run_id = rec.start(_make_config(tmp_path, doc))
    _deterministic_pipeline(rec)
    rec.finish("ok")

    events = load_events(tmp_path, run_id)
    stages = [e.stage for e in events]
    assert stages[0] == "file_load"  # auto-emitted
    assert stages[-1] == "report" and events[-1].event_type == "run_end"
    # monotonic timestamps
    ts = [e.ts for e in events]
    assert ts == sorted(ts)


def test_digest_stability(tmp_path):
    assert digest_of({"a": 1, "b": [1, 2]}) == digest_of({"b": [1, 2], "a": 1})
    assert digest_of(None) == digest_of(None)
    assert digest_of("x") != digest_of("y")
    # time-independent: same input -> same digest, always
    assert digest_of({"k": "v"}) == digest_of({"k": "v"})


def test_interrupted_mark(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"data")
    rec = RunRecorder(tmp_path)
    run_id = rec.start(_make_config(tmp_path, doc))
    # never call finish() -> interrupted
    m = load_manifest(tmp_path, run_id)
    assert m.status == "interrupted"
    assert m.finished_at == ""

    plan = rec.verify_replayable(run_id)
    assert not plan.replayable
    assert any("interrupted" in r for r in plan.reasons)


def test_calculation_event_carries_calculation_id(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"data")
    rec = RunRecorder(tmp_path)
    run_id = rec.start(_make_config(tmp_path, doc))
    rec.emit(Stage.CALCULATION, "cfoni", input_={"cfo": 100}, output={"ratio": 0.61},
             status="ok", calculation_id="CAL12")
    rec.finish("ok")
    calc_event = next(e for e in load_events(tmp_path, run_id) if e.stage == "calculation")
    assert calc_event.calculation_id == "CAL12"
    assert calc_event.to_dict()["calculation_id"] == "CAL12"


def test_file_load_event_carries_sha256(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"hello-world")
    rec = RunRecorder(tmp_path)
    run_id = rec.start(_make_config(tmp_path, doc))
    rec.finish("ok")
    fl = next(e for e in load_events(tmp_path, run_id) if e.stage == "file_load")
    assert fl.sha256 == digest_of(doc.read_bytes())
    assert fl.path == str(doc)


def test_replay_reject_modified_file(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"original-bytes")
    rec = RunRecorder(tmp_path)
    run_id = rec.start(_make_config(tmp_path, doc))
    _deterministic_pipeline(rec)
    rec.finish("ok")

    # Mutate the input document after the run.
    doc.write_bytes(b"tampered-bytes")

    plan = rec.verify_replayable(run_id)
    assert not plan.replayable
    assert any("modified" in r for r in plan.reasons)

    config = _make_config(tmp_path, doc)
    try:
        replay(run_id, executor=lambda m: _rerun_events(tmp_path, config), store_dir=tmp_path)
    except Exception as exc:
        assert type(exc).__name__ == "ReplayRejected"
    else:
        raise AssertionError("expected ReplayRejected for modified input")


def test_replay_deterministic_full_match(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"data")
    config = _make_config(tmp_path, doc)
    rec = RunRecorder(tmp_path)
    run_id = rec.start(config)
    _deterministic_pipeline(rec)
    rec.finish("ok")

    result = replay(run_id, executor=lambda m: _rerun_events(tmp_path, config), store_dir=tmp_path)
    assert result.replayable
    assert result.status == "ok"
    assert result.deterministic_full_match
    assert result.deterministic_events > 0
    assert result.llm_sampled_diff == []


def test_replay_sampled_drift_reported(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"data")
    config = _make_config(tmp_path, doc)
    rec = RunRecorder(tmp_path)
    run_id = rec.start(config)
    _deterministic_pipeline(rec)
    # a sampled (LLM) claim event
    rec.emit(Stage.CLAIM, "opinion_claim", input_={"thesis": "X"},
             output={"text": "看多"}, status="ok", sampled=True)
    rec.finish("ok")

    original = load_events(tmp_path, run_id)

    def executor(manifest):
        # re-run: deterministic events identical, but sampled output drifts
        rerun = [e.to_dict() for e in original if not e.sampled]
        rerun.append({
            "ts": "2026-01-01T00:00:00+00:00", "run_id": "rerun", "stage": "claim",
            "event_type": "opinion_claim", "input_digest": digest_of({"thesis": "X"}),
            "output_digest": digest_of({"text": "看空"}), "status": "ok", "detail": {},
            "sampled": True,
        })
        return rerun

    result = replay(run_id, executor=executor, store_dir=tmp_path)
    assert result.deterministic_full_match
    assert result.status == "replay_drift"
    assert len(result.llm_sampled_diff) == 1


def test_replay_deterministic_divergence_detected(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"data")
    config = _make_config(tmp_path, doc)
    rec = RunRecorder(tmp_path)
    run_id = rec.start(config)
    _deterministic_pipeline(rec, calc_value=0.61)
    rec.finish("ok")

    # executor returns a divergent deterministic calculation output
    def executor(manifest):
        return _rerun_events(tmp_path, config, calc_value=0.99)

    result = replay(run_id, executor=executor, store_dir=tmp_path)
    assert not result.deterministic_full_match
    assert result.status == "replay_drift"
    assert result.deterministic_mismatches


def test_append_only_no_rewrite(tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"data")
    rec = RunRecorder(tmp_path)
    run_id = rec.start(_make_config(tmp_path, doc))
    _deterministic_pipeline(rec)
    rec.finish("ok")

    events_path = tmp_path / run_id / "events.jsonl"
    before = events_path.read_bytes()

    # Read-only operations must NOT alter the audit trail.
    load_events(tmp_path, run_id)
    rec.verify_replayable(run_id)
    load_manifest(tmp_path, run_id)
    after = events_path.read_bytes()

    assert before == after
    # re-emit must append, not overwrite
    rec.emit(Stage.VALIDATION, "recheck", input_=None, output={"ok": True}, status="ok")
    assert len(load_events(tmp_path, run_id)) == len(before.decode().splitlines()) + 1


def test_collect_tool_versions_offline(tmp_path):
    from src.cn.runs import collect_tool_versions

    versions = collect_tool_versions()
    assert isinstance(versions, dict)
    # tushare is a tracked dependency; value is a version string or "unknown"
    assert "tushare" in versions


def test_git_commit_detected_in_repo(tmp_path):
    from src.cn.runs import detect_git_commit
    from src.cn.runs.recorder import _REPO_ROOT

    commit = detect_git_commit(_REPO_ROOT)
    # Either a real 40-char sha or the offline fail-open token.
    assert commit == "unknown" or (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit))


# ---------------------------------------------------------------------------
# §12 integration test: one full research pipeline -> replay deterministic match
# ---------------------------------------------------------------------------
def test_integration_full_pipeline_artifacts_and_replay(tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF annual report bytes")
    config = _make_config(tmp_path, doc)
    rec = RunRecorder(tmp_path)
    run_id = rec.start(config)
    _deterministic_pipeline(rec)  # file_load(auto) + 8 stages
    rec.finish("ok")

    run_dir = tmp_path / run_id
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "events.jsonl").exists()

    # All nine stages present (file_load + 8 manual).
    stages = {e.stage for e in load_events(tmp_path, run_id)}
    assert stages == {
        "file_load", "parse", "normalize", "retrieval", "tool_call",
        "calculation", "claim", "validation", "report",
    }

    result = replay(run_id, executor=lambda m: _rerun_events(tmp_path, config), store_dir=tmp_path)
    assert result.deterministic_full_match
    assert result.status == "ok"
