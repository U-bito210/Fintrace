"""CARD-12 Evidence Pack & Audit Replay — offline tests.

The card's whole point is *faithfulness*, so the tests are mostly about what
happens when things are broken: a missing artefact, a corrupt event line, a
blocked claim. A pack that looks complete when it is not, or a replay that
smooths over a failure, is the bug this module exists to prevent.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.cn.evidence_pack import (
    PACK_FILE_NAMES,
    PACK_STATUS_FILE,
    REPLAY_TIMELINE,
    STEP_IDS,
    ZIP_NAME,
    EvidencePackError,
    diff_run_manifests,
    export_evidence_pack,
    load_events_tolerant,
    render_pack_markdown,
    replay_timeline,
    validate_pack,
    zip_path_for,
)
from src.cn.failure_injection import SCENARIOS, render_injection_markdown, run_all, run_scenario
from src.cn.runs.models import RunConfig, RunInputDoc, Stage, digest_of
from src.cn.runs.recorder import RunRecorder


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _config(doc: Path) -> RunConfig:
    return RunConfig(
        input_documents=[RunInputDoc(sha256=digest_of(doc.read_bytes()), path=str(doc))],
        parser_name="pdf_text",
        parser_version="1.4.0",
        execution_mode="offline",
        snapshot_id="SYN-600519",
        tool_versions={"tushare": "1.2.3"},
        random_seed=42,
    )


def _pipeline(rec: RunRecorder, *, calc_value: float = 0.61, fail_at: str = "") -> None:
    """One event per stage; ``fail_at`` makes one stage fail on purpose."""
    steps = [
        (Stage.PARSE, "parsed"),
        (Stage.NORMALIZE, "normalized"),
        (Stage.RETRIEVAL, "retrieved"),
        (Stage.TOOL_CALL, "get_cn_financials"),
        (Stage.CALCULATION, "cfoni"),
        (Stage.CLAIM, "claim_c18"),
        (Stage.VALIDATION, "validated"),
        (Stage.REPORT, "report_written"),
    ]
    for stage, event_type in steps:
        status = "failed" if fail_at and stage.value == fail_at else "ok"
        rec.emit(
            stage,
            event_type,
            input_={"stage": stage.value},
            output={"value": calc_value},
            status=status,
            calculation_id="CAL12" if stage is Stage.CALCULATION else None,
        )


def _make_run(tmp_path: Path, *, fail_at: str = "") -> str:
    doc = tmp_path / "a.pdf"
    if not doc.exists():
        doc.write_bytes(b"%PDF-1.4 fake filing")
    rec = RunRecorder(tmp_path / "runs")
    run_id = rec.start(_config(doc))
    _pipeline(rec, fail_at=fail_at)
    rec.finish("failed" if fail_at else "ok")
    return run_id


def _artefacts(run_id: str, tmp_path: Path) -> dict:
    charts = tmp_path / "charts_src"
    charts.mkdir(exist_ok=True)
    (charts / "cfo_trend.svg").write_text("<svg/>", encoding="utf-8")
    citations = tmp_path / "citations_src"
    citations.mkdir(exist_ok=True)
    (citations / "c11_page12.json").write_text('{"page": 12}', encoding="utf-8")
    return {
        "report.md": "# 报告\n\n正文\n",
        "report.json": {"run_id": run_id, "sections": []},
        "financials.xlsx": b"PK\x03\x04fake-xlsx",
        "valuation.xlsx": b"PK\x03\x04fake-xlsx",
        "corrections.xlsx": b"PK\x03\x04fake-xlsx",
        "claims.json": {"claims": [{"claim_id": "C11", "claim_type": "fact", "evidence_ids": ["E1"]}]},
        "evidence.json": {"evidence": [{"evidence_id": "E1", "page": 12}]},
        "calculations.json": {"calculations": [{"calculation_id": "CAL12"}]},
        "validation.json": {"valid": True},
        "charts": charts,
        "citations": citations,
    }


# --------------------------------------------------------------------------- #
# §15 acceptance 1 — 13 artefacts and completeness validation
# --------------------------------------------------------------------------- #
def test_pack_lists_exactly_the_cards_thirteen_artefacts():
    assert len(PACK_FILE_NAMES) == 13
    assert set(("manifest.json", "events.jsonl", "report.md", "report.json")) <= set(PACK_FILE_NAMES)
    assert len(set(PACK_FILE_NAMES)) == 13


def test_export_writes_the_pack_the_zip_and_a_status_sheet(tmp_path: Path):
    run_id = _make_run(tmp_path)
    pack_dir = export_evidence_pack(run_id, store_dir=tmp_path / "runs", artefacts=_artefacts(run_id, tmp_path))

    for name in PACK_FILE_NAMES:
        assert (pack_dir / name).exists(), name
    assert (pack_dir / PACK_STATUS_FILE).exists()

    zip_path = zip_path_for(pack_dir)
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "manifest.json" in names and "report.md" in names
    # The zip must be unpackable and self-describing.
    status = json.loads((pack_dir / PACK_STATUS_FILE).read_text(encoding="utf-8"))
    assert status["status"] == "complete"


def test_a_pack_missing_artefacts_is_marked_incomplete_with_the_list(tmp_path: Path):
    run_id = _make_run(tmp_path)
    partial = {"report.md": "# 报告\n", "claims.json": {"claims": []}}
    pack_dir = export_evidence_pack(run_id, store_dir=tmp_path / "runs", artefacts=partial)

    validation = validate_pack(pack_dir, expected_run_id=run_id)
    assert not validation.complete
    assert validation.status == "incomplete"
    assert "report.json" in validation.missing_completeness
    assert "financials.xlsx" in validation.missing_completeness
    # Optional gaps do not make a pack incomplete, but they are still listed.
    assert "charts" in validation.missing_optional
    assert validation.valid  # incomplete is not the same as broken

    status = json.loads((pack_dir / PACK_STATUS_FILE).read_text(encoding="utf-8"))
    assert status["status"] == "incomplete"
    assert "financials.xlsx" in status["missing_files"]

    markdown = render_pack_markdown(validation)
    assert "INCOMPLETE" in markdown
    assert "financials.xlsx" in markdown


def test_an_optional_directory_supplied_as_a_file_is_reported_not_faked(tmp_path: Path):
    run_id = _make_run(tmp_path)
    artefacts = _artefacts(run_id, tmp_path)
    artefacts["charts"] = b"not a directory"
    pack_dir = export_evidence_pack(run_id, store_dir=tmp_path / "runs", artefacts=artefacts)
    validation = validate_pack(pack_dir)
    charts = next(item for item in validation.entries if item.name == "charts")
    assert charts.present is False
    assert "目录" in charts.note
    assert "charts" in validation.missing_optional


def test_require_complete_refuses_to_ship_a_partial_pack(tmp_path: Path):
    run_id = _make_run(tmp_path)
    with pytest.raises(EvidencePackError, match="残包"):
        export_evidence_pack(
            run_id, store_dir=tmp_path / "runs", artefacts={"report.md": "x"}, require_complete=True
        )


def test_export_refuses_an_unknown_run(tmp_path: Path):
    with pytest.raises(EvidencePackError, match="manifest"):
        export_evidence_pack("nope", store_dir=tmp_path / "runs")


def test_pack_without_a_manifest_is_invalid_not_silently_incomplete(tmp_path: Path):
    empty = tmp_path / "empty_pack"
    empty.mkdir()
    validation = validate_pack(empty)
    assert not validation.valid
    assert "manifest.json" in validation.missing_required
    assert any("manifest" in item for item in validation.errors)


def test_a_caller_cannot_override_the_runs_own_manifest_or_events(tmp_path: Path):
    run_id = _make_run(tmp_path)
    pack_dir = export_evidence_pack(
        run_id,
        store_dir=tmp_path / "runs",
        artefacts={**(_artefacts(run_id, tmp_path)), "manifest.json": {"run_id": "FORGED"}, "events.jsonl": ""},
    )
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == run_id
    assert (pack_dir / "events.jsonl").read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# §10 validator — run_id consistency and reference resolvability
# --------------------------------------------------------------------------- #
def test_validate_pack_flags_a_run_id_mismatch(tmp_path: Path):
    run_id = _make_run(tmp_path)
    pack_dir = export_evidence_pack(run_id, store_dir=tmp_path / "runs", artefacts=_artefacts(run_id, tmp_path))
    validation = validate_pack(pack_dir, expected_run_id="some-other-run")
    assert not validation.valid
    assert any("不一致" in item for item in validation.errors)


def test_validate_pack_flags_an_unresolvable_reference(tmp_path: Path):
    run_id = _make_run(tmp_path)
    artefacts = _artefacts(run_id, tmp_path)
    artefacts["claims.json"] = {
        "claims": [{"claim_id": "C11", "evidence_ids": ["E404"], "calculation_ids": ["CAL404"]}]
    }
    pack_dir = export_evidence_pack(run_id, store_dir=tmp_path / "runs", artefacts=artefacts)
    validation = validate_pack(pack_dir)
    assert not validation.valid
    assert "evidence_ids:E404" in validation.unresolved_references
    assert "calculation_ids:CAL404" in validation.unresolved_references


def test_validate_pack_resolves_a_consistent_pack(tmp_path: Path):
    run_id = _make_run(tmp_path)
    pack_dir = export_evidence_pack(run_id, store_dir=tmp_path / "runs", artefacts=_artefacts(run_id, tmp_path))
    validation = validate_pack(pack_dir, expected_run_id=run_id)
    assert validation.valid and validation.complete
    assert not validation.unresolved_references


# --------------------------------------------------------------------------- #
# §11 — corrupt events are tolerated *and reported*
# --------------------------------------------------------------------------- #
def test_load_events_tolerant_skips_and_reports_a_broken_line(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    good = json.dumps(
        {
            "ts": "2026-09-19T00:00:00+00:00",
            "run_id": "R",
            "stage": "parse",
            "event_type": "parsed",
            "input_digest": "a",
            "output_digest": "b",
            "status": "ok",
        }
    )
    path.write_text(f"{good}\n{{not json at all\n\n{good}\n", encoding="utf-8")
    events, broken = load_events_tolerant(path)
    assert len(events) == 2
    assert len(broken) == 1
    assert broken[0].startswith("line2:")


def test_a_corrupt_events_line_does_not_stop_the_replay_but_is_named(tmp_path: Path):
    run_id = _make_run(tmp_path)
    pack_dir = export_evidence_pack(run_id, store_dir=tmp_path / "runs", artefacts=_artefacts(run_id, tmp_path))
    target = pack_dir / "events.jsonl"
    target.write_text(target.read_text(encoding="utf-8") + "{oops\n", encoding="utf-8")

    validation = validate_pack(pack_dir)
    assert any("损坏行" in item for item in validation.warnings)
    events, broken = load_events_tolerant(target)
    assert events and broken
    timeline = replay_timeline(events)
    assert timeline["event_count"] == len(events)


# --------------------------------------------------------------------------- #
# §15 acceptance 2 — eleven-step timeline with the five elements
# --------------------------------------------------------------------------- #
def test_timeline_has_exactly_the_eleven_card_steps_in_order():
    assert STEP_IDS == (
        "file_load", "hash", "parse", "extract", "normalize",
        "retrieval", "tool_call", "calculation", "claim", "validation", "report",
    )
    assert [spec.step_id for spec in REPLAY_TIMELINE] == list(STEP_IDS)


def test_timeline_renders_every_step_and_carries_five_elements(tmp_path: Path):
    run_id = _make_run(tmp_path)
    from src.cn.runs.recorder import load_events

    timeline = replay_timeline(load_events(tmp_path / "runs", run_id))
    assert len(timeline["steps"]) == 11
    for step in timeline["steps"]:
        assert set(step) >= {"step_id", "label", "status", "started_at", "ended_at", "inputs", "outputs", "sources"}
    recorded = [step for step in timeline["steps"] if step["event_count"]]
    assert recorded
    for step in recorded:
        event = step["events"][0]
        assert set(event) >= {"ts", "stage", "event_type", "status", "input", "output", "source"}
        assert event["input"]["digest"] and event["output"]["digest"]


def test_unrecorded_steps_are_shown_as_not_recorded_not_omitted(tmp_path: Path):
    run_id = _make_run(tmp_path)
    from src.cn.runs.recorder import load_events

    timeline = replay_timeline(load_events(tmp_path / "runs", run_id))
    # The fixture pipeline never emits a financial-extraction event.
    assert "extract" in timeline["not_recorded_steps"]
    assert len(timeline["steps"]) == 11
    missing = next(step for step in timeline["steps"] if step["step_id"] == "extract")
    assert missing["status"] == "not_recorded"
    assert missing["started_at"] is None


def test_a_failed_stage_is_surfaced_not_smoothed_over(tmp_path: Path):
    run_id = _make_run(tmp_path, fail_at="calculation")
    from src.cn.runs.recorder import load_events

    timeline = replay_timeline(load_events(tmp_path / "runs", run_id))
    assert "calculation" in timeline["failed_steps"]
    step = next(item for item in timeline["steps"] if item["step_id"] == "calculation")
    assert step["status"] == "failed"
    assert timeline["failure_event_count"] >= 1
    assert "failed" in timeline["statuses_seen"]


def test_timeline_pages_a_large_run_without_dropping_events(tmp_path: Path):
    run_id = _make_run(tmp_path)
    rec = RunRecorder(tmp_path / "runs")
    extra = rec.start(_config(tmp_path / "a.pdf"))
    for index in range(50):
        rec.emit(Stage.TOOL_CALL, f"call_{index}", input_=index, output=index * 2, status="ok")
    rec.finish("ok")
    from src.cn.runs.recorder import load_events

    events = load_events(tmp_path / "runs", extra)
    assert len(events) >= 50
    paged = replay_timeline(events, page=1, page_size=10)
    assert paged["truncated_events"] > 0
    assert paged["event_count"] == len(events)
    # Nothing is lost: the declared count plus the window equals the total.
    windowed = sum(step["event_count"] for step in paged["steps"])
    assert windowed + paged["truncated_events"] == len(events)
    assert run_id  # the original run is untouched


# --------------------------------------------------------------------------- #
# §8 edge 4 — multi-run diff
# --------------------------------------------------------------------------- #
def test_manifest_diff_flags_a_real_difference(tmp_path: Path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF-1.4 fake filing")
    rec = RunRecorder(tmp_path / "runs")
    run_a = rec.start(_config(doc))
    _pipeline(rec)
    rec.finish("ok")

    changed = RunConfig(
        input_documents=[RunInputDoc(sha256=digest_of(doc.read_bytes()), path=str(doc))],
        parser_name="pdf_text",
        parser_version="1.4.0",
        execution_mode="offline",
        snapshot_id="SYN-600519",
        random_seed=7,          # the deliberate difference
    )
    rec2 = RunRecorder(tmp_path / "runs")
    run_b = rec2.start(changed)
    _pipeline(rec2)
    rec2.finish("ok")

    diff = diff_run_manifests(run_a, run_b, store_dir=tmp_path / "runs")
    assert "random_seed" in diff["different_fields"]
    assert diff["same_inputs"] is True
    assert diff["comparable"] is True
    row = next(item for item in diff["rows"] if item["field"] == "random_seed")
    assert row["run_a"] == 42 and row["run_b"] == 7 and row["same"] is False


def test_manifest_diff_warns_when_inputs_differ(tmp_path: Path):
    rec = RunRecorder(tmp_path / "runs")
    first = tmp_path / "a.pdf"
    first.write_bytes(b"one")
    run_a = rec.start(_config(first))
    _pipeline(rec)
    rec.finish("ok")

    second = tmp_path / "b.pdf"
    second.write_bytes(b"two")
    run_b = rec.start(_config(second))
    _pipeline(rec)
    rec.finish("ok")

    diff = diff_run_manifests(run_a, run_b, store_dir=tmp_path / "runs")
    assert diff["same_inputs"] is False
    assert diff["comparable"] is False
    assert "不可直接归因" in diff["note"]


# --------------------------------------------------------------------------- #
# §15 acceptance 3 — the seven failure-injection scenarios
# --------------------------------------------------------------------------- #
def test_all_seven_scenarios_are_registered():
    assert [spec.scenario_id for spec in SCENARIOS] == [1, 2, 3, 4, 5, 6, 7]
    assert all(spec.expected_behavior for spec in SCENARIOS)


def test_implemented_scenarios_reproduce_offline():
    results = {item.scenario_id: item for item in run_all()}
    for scenario_id in (1, 2, 3, 4, 6, 7):
        result = results[scenario_id]
        assert result.ok, f"#{scenario_id} {result.slug}: {result.observed}"
        assert result.observed
    # The card's headline demo is #6.
    assert results[6].ok


def test_scenario_five_is_implemented_and_takes_the_corrected_value():
    """CARD-13 landed, so #5 must now run for real rather than report deferred."""
    result = run_scenario(5)
    assert result.ok, result.observed
    assert "deferred_to" not in result.evidence
    assert result.evidence["rule_id"] == "rule.regulatory_correction_supersedes_prior"
    assert "900,000" in result.observed and "1,000,000" in result.observed
    # The superseded source is named, so the audit link survives the swap.
    assert result.evidence["conflict"]["superseded_source"]


def test_no_scenario_is_silently_skipped():
    """Every one of the seven must produce a real verdict, none a placeholder."""
    for result in run_all():
        assert result.evidence.get("status") != "deferred", result.slug
        assert result.observed


def test_injection_markdown_reports_all_seven_as_run():
    markdown = render_injection_markdown(run_all())
    assert "失败注入演示（7 场景）" in markdown
    assert "未实现（如实列出，不掩盖）" not in markdown
    assert "通过：7/7" in markdown


def test_unknown_scenario_id_is_rejected():
    with pytest.raises(KeyError):
        run_scenario(99)


# --------------------------------------------------------------------------- #
# §12 integration — a run with injection → pack → replay shows the failure
# --------------------------------------------------------------------------- #
def test_a_failing_run_packs_and_replays_with_the_failure_visible(tmp_path: Path):
    run_id = _make_run(tmp_path, fail_at="validation")
    pack_dir = export_evidence_pack(
        run_id, store_dir=tmp_path / "runs", artefacts=_artefacts(run_id, tmp_path)
    )
    validation = validate_pack(pack_dir, expected_run_id=run_id)
    assert validation.valid

    events, broken = load_events_tolerant(pack_dir / "events.jsonl")
    assert not broken
    timeline = replay_timeline(events)
    assert "validation" in timeline["failed_steps"]
    # The pack still describes itself honestly.
    status = json.loads((pack_dir / PACK_STATUS_FILE).read_text(encoding="utf-8"))
    assert status["status"] in {"complete", "incomplete"}
    assert (pack_dir / "report.md").exists()


def test_incomplete_pack_is_exported_and_labelled_when_a_run_has_no_report(tmp_path: Path):
    """§8 edge case 1: an interrupted run still exports — labelled, not faked."""
    run_id = _make_run(tmp_path)
    pack_dir = export_evidence_pack(
        run_id, store_dir=tmp_path / "runs", artefacts={"claims.json": {"claims": []}}
    )
    validation = validate_pack(pack_dir, expected_run_id=run_id)
    assert validation.status == "incomplete"
    assert "report.md" in validation.missing_completeness
    markdown = render_pack_markdown(validation)
    assert "本节" not in markdown  # no invented content
    assert "report.md" in markdown
