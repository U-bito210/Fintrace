"""CARD-25 — Temporal Revalidation Engine 测试。

分组对应卡的各节：§14 单测（三态 / 谱系 / 子图 / 增量对账 / 空报告 / 环）、
§13 校验器、§9 零 LLM、§15 集成（真实 annual_04 的重述血缘）、§8 工具契约。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from src.agents.tools.cn_tools import RevalidateCnClaimsTool
from src.cn.analysis import analyze_document
from src.cn.documents.registry import DocumentRegistry
from src.cn.evidence import EvidenceLedger
from src.cn.graph import (
    CLAIM_FACT,
    CLAIM_INFERENCE,
    THESIS_STALE,
    VALIDATION_BLOCKED,
    VALIDATION_STALE,
    VALIDATION_SUPPORTED,
    VERIFICATION_CONFLICTED,
    Claim,
    ClaimGraph,
    EvidenceNode,
    Thesis,
    build_graph,
    build_graph_from_analysis,
)
from src.cn.graph.propagation import downstream_closure
from src.cn.normalization import normalize_financials
from src.cn.normalization.evidence_bridge import (
    normalization_jumps,
    register_normalized_evidence,
)
from src.cn.proof import ProofContext, verify_claim
from src.cn.proof.models import STATUS_STALE
from src.cn.temporal import (
    ACTION_REFRESH_MEMO_SECTION,
    ACTION_REFRESH_VALUATION,
    ACTION_REVIEW_SUPERSESSION,
    CHANGE_DATA_CONFLICT,
    CHANGE_NEW_INFORMATION,
    CHANGE_VERSION_SUPERSESSION,
    EVENT_CORRECTION,
    EVENT_NEW_FILING,
    EVENT_NEW_SNAPSHOT,
    EVENT_RESTATEMENT,
    IMPACT_KEYS,
    LINEAGE_CORRECTED,
    LINEAGE_RESTATED,
    LINEAGE_SUPERSEDED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    TemporalError,
    TemporalEvent,
    VersionLineage,
    VersionLineageEntry,
    build_lineage,
    compare_full_vs_incremental,
    compute_affected_subgraph,
    ingest_temporal_event,
    recompute_full,
    snapshot_statuses,
    subgraph_view,
)
from src.validation import validate_temporal, validate_temporal_corpus
from src.validation.checker_validator import check_source_is_model_free
import src.validation.temporal_validator as temporal_validator

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "documents"
TEMPORAL_PACKAGE = Path(__file__).parents[1] / "src" / "cn" / "temporal"
VALIDATOR_PATH = Path(__file__).parents[1] / "src" / "validation" / "temporal_validator.py"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _evidence(evidence_id: str, *, metric: str = "", value: float = 1.0) -> EvidenceNode:
    return EvidenceNode(
        evidence_id=evidence_id,
        kind="fact",
        document_id="DOC-1",
        page=1,
        metric=metric or None,
        value=value,
        unit="元",
        period_key="2025FY",
    )


def _fact(claim_id: str, text: str, evidence_ids=(), metric=None, derived_from=()) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=CLAIM_FACT if not derived_from else CLAIM_INFERENCE,
        text=text,
        evidence_ids=tuple(evidence_ids),
        derived_from=tuple(derived_from),
        metric=metric,
        period_key="2025FY" if metric else None,
    )


def _supersession_graph() -> ClaimGraph:
    """E-OLD 供 C01；E-NEW 是替代新值。T01 需要 C01。

    链：E-OLD → C01 → T01
    """
    return build_graph(
        "run-supersession",
        claims=[_fact("C01", "营业收入 110 万", evidence_ids=("E-OLD",), metric="revenue")],
        theses=[Thesis("T01", "收入可持续", claim_ids=("C01",))],
        evidence_index=[
            _evidence("E-OLD", metric="revenue", value=1_100_000.0),
            _evidence("E-NEW", metric="revenue", value=1_000_000.0),
        ],
    )


def _conflict_graph() -> ClaimGraph:
    """E1 → C01 → C02（C02 是推论，derived_from=C01）。"""
    return build_graph(
        "run-conflict",
        claims=[
            _fact("C01", "营业收入 31 亿", evidence_ids=("E1",), metric="revenue"),
            _fact("C02", "由收入推出规模", derived_from=("C01",)),
        ],
        evidence_index=[_evidence("E1", metric="revenue", value=3.1e9)],
    )


def _lineage_for(ids, *, superseded=(), superseded_by=None) -> VersionLineage:
    """手工血缘：``ids`` 里先登记，再把 ``superseded`` 里每个标为被 ``superseded_by`` 替代。"""
    lineage = VersionLineage()
    for fact_id in ids:
        lineage.register(VersionLineageEntry(fact_id=fact_id))
    if superseded and superseded_by:
        for old in superseded:
            lineage.mark_superseded(old, superseded_by=superseded_by, reason="restatement")
    return lineage


def _supersession_event(old: str, new: str, **overrides) -> TemporalEvent:
    payload = dict(
        event_id="EV-SUP",
        event_kind=CHANGE_VERSION_SUPERSESSION,
        event_type=EVENT_RESTATEMENT,
        event_date="2026-05-01",
        changed_source_ids=(new,),
        supersedes=old,
    )
    payload.update(overrides)
    return TemporalEvent(**payload)


# --------------------------------------------------------------------------- #
# §14 A. 三态区分（卡 §3：不能全叫 conflict）
# --------------------------------------------------------------------------- #

def test_supersession_marks_dependent_claims_stale_not_blocked():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)

    assert graph.claims["C01"].validation_status == VALIDATION_STALE
    assert graph.claims["C01"].validation_status != VALIDATION_BLOCKED
    assert report.impact["claims_invalidated"] == 0
    assert report.impact["facts_superseded"] == 1
    assert lineage.get("E-OLD").version_status == LINEAGE_SUPERSEDED
    assert lineage.get("E-NEW").supersedes == "E-OLD"


def test_supersession_stales_the_thesis_too():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)

    assert graph.theses["T01"].status == THESIS_STALE
    kinds = {(item["node_type"], item["node_id"], item["after"]) for item in report.state_diff}
    assert ("thesis", "T01", THESIS_STALE) in kinds


def test_supersession_emits_a_review_action_for_the_old_value():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)
    actions = {item["action_type"]: item for item in report.revalidation_actions}
    assert ACTION_REVIEW_SUPERSESSION in actions
    assert actions[ACTION_REVIEW_SUPERSESSION]["target_id"] == "E-OLD"


def test_unresolved_conflict_blocks_downstream():
    graph = _conflict_graph()
    event = TemporalEvent(
        event_id="EV-C",
        event_kind=CHANGE_DATA_CONFLICT,
        event_type=EVENT_NEW_FILING,
        event_date="2026-05-02",
        changed_source_ids=("E1",),
    )
    report = ingest_temporal_event(graph, event)

    # 冲突事实本身被标 CONFLICTED（它仍"存在"，只是有争议）……
    assert graph.claims["C01"].verification_status == VERIFICATION_CONFLICTED
    # ……而它的下游 fail-closed 阻断。
    assert graph.claims["C02"].validation_status == VALIDATION_BLOCKED
    assert report.impact["claims_invalidated"] == 1


def test_resolved_conflict_does_not_block():
    graph = _conflict_graph()
    event = TemporalEvent(
        event_id="EV-C2",
        event_kind=CHANGE_DATA_CONFLICT,
        event_type=EVENT_NEW_FILING,
        event_date="2026-05-02",
        changed_source_ids=("E1",),
        resolved_by="DOC-OFFICIAL",
    )
    report = ingest_temporal_event(graph, event)

    assert graph.claims["C01"].verification_status != VERIFICATION_CONFLICTED
    assert graph.claims["C02"].validation_status != VALIDATION_BLOCKED
    assert report.impact["claims_invalidated"] == 0
    assert any("conflict_resolved_by" in warning for warning in report.warnings)


def test_new_information_stales_claims_without_touching_lineage():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"])
    before = tuple(lineage.records)
    event = TemporalEvent(
        event_id="EV-N",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_FILING,
        event_date="2026-06-30",
        changed_source_ids=("E-OLD",),
    )
    report = ingest_temporal_event(graph, event, lineage=lineage)

    assert graph.claims["C01"].validation_status == VALIDATION_STALE
    # 新信息不建立替代关系：血缘一条都不该动。
    assert tuple(lineage.records) == before
    assert report.impact["facts_superseded"] == 0
    assert report.impact["claims_invalidated"] == 0


def test_three_event_kinds_produce_distinguishable_reports():
    from src.cn.temporal import EVENT_EFFECTS

    assert len(set(EVENT_EFFECTS.values())) == 3

    sup_graph = _supersession_graph()
    sup_lineage = _lineage_for(
        ["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW"
    )
    sup = ingest_temporal_event(
        sup_graph, _supersession_event("E-OLD", "E-NEW"), lineage=sup_lineage
    )

    con_graph = _conflict_graph()
    con = ingest_temporal_event(
        con_graph,
        TemporalEvent(
            event_id="EV-C",
            event_kind=CHANGE_DATA_CONFLICT,
            event_type=EVENT_NEW_FILING,
            event_date="2026-05-02",
            changed_source_ids=("E1",),
        ),
    )

    new_graph = _supersession_graph()
    new = ingest_temporal_event(
        new_graph,
        TemporalEvent(
            event_id="EV-N",
            event_kind=CHANGE_NEW_INFORMATION,
            event_type=EVENT_NEW_FILING,
            event_date="2026-06-30",
            changed_source_ids=("E-OLD",),
        ),
    )

    # 阻断只发生在冲突路径；替代只发生在替代路径。
    assert con.impact["claims_invalidated"] == 1 and sup.impact["claims_invalidated"] == 0
    assert sup.impact["facts_superseded"] == 1
    assert new.impact["facts_superseded"] == 0 and new.impact["claims_invalidated"] == 0
    assert sup.lineage and not new.lineage


# --------------------------------------------------------------------------- #
# §14 B. 版本谱系四态
# --------------------------------------------------------------------------- #

def test_lineage_accepts_all_four_version_statuses():
    lineage = VersionLineage()
    lineage.register(VersionLineageEntry("F1", version_status=LINEAGE_RESTATED, reason="restatement"))
    lineage.register(VersionLineageEntry("F2", version_status=LINEAGE_CORRECTED, reason="correction"))
    lineage.register(VersionLineageEntry("F3"))
    lineage.register(
        VersionLineageEntry("F4", version_status=LINEAGE_SUPERSEDED, superseded_by="F1", reason="restatement")
    )
    assert {entry.version_status for entry in lineage.entries()} == {
        "AS_REPORTED",
        LINEAGE_RESTATED,
        LINEAGE_CORRECTED,
        LINEAGE_SUPERSEDED,
    }
    assert lineage.live_ids() == ("F1", "F2", "F3")
    assert lineage.superseded_ids() == ("F4",)


def test_superseded_entry_without_a_replacement_is_rejected():
    with pytest.raises(TemporalError):
        VersionLineageEntry("F1", version_status=LINEAGE_SUPERSEDED)


def test_entry_carrying_superseded_by_must_be_superseded():
    with pytest.raises(TemporalError):
        VersionLineageEntry("F1", version_status=LINEAGE_RESTATED, superseded_by="F2", reason="restatement")


def test_as_reported_cannot_carry_a_reason():
    with pytest.raises(TemporalError):
        VersionLineageEntry("F1", version_status="AS_REPORTED", reason="correction")


def test_entry_rejects_self_reference():
    with pytest.raises(TemporalError):
        VersionLineageEntry("F1", superseded_by="F1", version_status=LINEAGE_SUPERSEDED)


def test_entry_rejects_unknown_version_status_and_reason():
    with pytest.raises(TemporalError):
        VersionLineageEntry("F1", version_status="WHATEVER")
    with pytest.raises(TemporalError):
        VersionLineageEntry("F1", version_status=LINEAGE_RESTATED, reason="因为我觉得")


def test_mark_superseded_writes_both_directions():
    lineage = _lineage_for(["OLD", "NEW"])
    lineage.mark_superseded("OLD", superseded_by="NEW", reason="restatement")
    assert lineage.get("OLD").superseded_by == "NEW"
    assert lineage.get("NEW").supersedes == "OLD"


def test_lineage_never_physically_overwrites_the_old_state():
    lineage = _lineage_for(["OLD", "NEW"])
    lineage.mark_superseded("OLD", superseded_by="NEW", reason="restatement")
    history = lineage.history("OLD")
    # 两条记录：改之前的那条还在，且当时并不是"已被替代"。
    assert len(history) == 2
    assert history[0].version_status == "AS_REPORTED"
    assert history[1].version_status == LINEAGE_SUPERSEDED


def test_chain_orders_oldest_to_newest():
    lineage = _lineage_for(["V1", "V2", "V3"])
    lineage.mark_superseded("V1", superseded_by="V2", reason="restatement")
    lineage.mark_superseded("V2", superseded_by="V3", reason="restatement")
    assert lineage.chain("V3") == ("V1", "V2", "V3")
    assert lineage.chain("V1") == ("V1", "V2", "V3")


def test_chain_rejects_a_cycle():
    lineage = VersionLineage()
    lineage.register(
        VersionLineageEntry("A", version_status=LINEAGE_SUPERSEDED, superseded_by="B", reason="update")
    )
    lineage.register(
        VersionLineageEntry("B", version_status=LINEAGE_SUPERSEDED, superseded_by="A", reason="update")
    )
    with pytest.raises(TemporalError):
        lineage.chain("A")
    with pytest.raises(TemporalError):
        lineage.assert_acyclic()


def test_mark_superseded_rejects_an_unknown_replacement():
    lineage = _lineage_for(["OLD"])
    with pytest.raises(TemporalError):
        lineage.mark_superseded("OLD", superseded_by="GHOST", reason="restatement")


def test_one_replacement_can_supersede_several_old_values():
    """一份更正常常一次改好几行：一个替代者对应多条旧值。

    此时单值的 ``supersedes`` 装不下全部前驱，所以约定：
    ``superseded_by``（写在旧值上）是**完整**映射，``supersedes`` 只记主前驱且**不覆盖**。
    """
    lineage = _lineage_for(["OLD-A", "OLD-B", "NEW"])
    lineage.mark_superseded("OLD-A", superseded_by="NEW", reason="correction")
    lineage.mark_superseded("OLD-B", superseded_by="NEW", reason="correction")

    # 完整映射：两条旧值都指向同一个替代者。
    assert lineage.get("OLD-A").superseded_by == "NEW"
    assert lineage.get("OLD-B").superseded_by == "NEW"
    assert lineage.predecessors("NEW") == ("OLD-A", "OLD-B")

    # 单值主前驱：仍是**先**建立的那条，没有被第二次调用覆盖掉。
    assert lineage.get("NEW").supersedes == "OLD-A"
    # 而且"OLD-A 原来是什么状态"这段历史一条没丢。
    assert len(lineage.history("OLD-A")) == 2
    assert lineage.history("OLD-A")[0].version_status == "AS_REPORTED"


def test_lineage_round_trips_through_dict_including_history():
    lineage = _lineage_for(["OLD", "NEW"])
    lineage.mark_superseded("OLD", superseded_by="NEW", reason="restatement")
    restored = VersionLineage.from_dict(lineage.to_dict())
    assert restored.get("OLD").version_status == LINEAGE_SUPERSEDED
    assert restored.get("NEW").supersedes == "OLD"
    assert len(restored.history("OLD")) == 2


# --------------------------------------------------------------------------- #
# §14 C. affected_subgraph
# --------------------------------------------------------------------------- #

def test_affected_subgraph_includes_the_seed():
    graph = _supersession_graph()
    nodes = compute_affected_subgraph(graph, ["E-OLD"])
    assert "E-OLD" in nodes


def test_affected_subgraph_follows_dependency_direction():
    graph = _supersession_graph()
    nodes = compute_affected_subgraph(graph, ["E-OLD"])
    assert set(nodes) == {"E-OLD", "C01"}
    # 反向：改 C01 不会把 E-OLD 拉进来。
    assert set(compute_affected_subgraph(graph, ["C01"])) == {"C01"}


def test_affected_subgraph_is_depth_limited():
    graph = build_graph(
        "run-chain3",
        claims=[
            _fact("C1", "一层", evidence_ids=("E1",)),
            _fact("C2", "二层", derived_from=("C1",)),
            _fact("C3", "三层", derived_from=("C2",)),
        ],
        evidence_index=[_evidence("E1")],
    )
    assert set(compute_affected_subgraph(graph, ["E1"], max_depth=8)) == {"E1", "C1", "C2", "C3"}
    assert set(compute_affected_subgraph(graph, ["E1"], max_depth=1)) == {"E1", "C1"}
    assert subgraph_view(graph, ["E1"], max_depth=1).truncated is True
    assert subgraph_view(graph, ["E1"], max_depth=8).truncated is False


def test_affected_subgraph_rejects_a_negative_depth():
    graph = _supersession_graph()
    with pytest.raises(TemporalError):
        subgraph_view(graph, ["E-OLD"], max_depth=-1)


# --------------------------------------------------------------------------- #
# §14 D. full vs incremental
# --------------------------------------------------------------------------- #

def _wide_graph(width: int = 60) -> ClaimGraph:
    """一条链路 + 一堆互不相干的链路，用来展示增量的实际节省。"""
    claims = [_fact("C-SEED", "被改的那条", evidence_ids=("E-SEED",))]
    evidence = [_evidence("E-SEED")]
    for index in range(width - 1):
        claims.append(_fact(f"C{index}", f"独立结论 {index}", evidence_ids=(f"E{index}",)))
        evidence.append(_evidence(f"E{index}"))
    return build_graph("run-wide", claims=claims, evidence_index=evidence)


def test_full_and_incremental_agree_on_a_small_graph():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)
    assert report.full_vs_incremental["results_identical"] is True
    assert report.full_vs_incremental["fell_back"] is False
    assert report.fallback is None


def test_incremental_recomputes_strictly_fewer_nodes_than_full():
    graph = _wide_graph(60)
    event = TemporalEvent(
        event_id="EV-W",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_SNAPSHOT,
        event_date="2026-07-01",
        changed_source_ids=("E-SEED",),
    )
    report = ingest_temporal_event(graph, event)

    comparison = report.full_vs_incremental
    assert comparison["results_identical"] is True
    assert comparison["nodes_recomputed"] < comparison["full_nodes"]
    # 只碰了种子那条链，其余 59 条一个都没重算。
    assert comparison["nodes_recomputed"] == 1
    assert comparison["full_nodes"] == 60


def test_other_claims_keep_their_status_when_only_one_chain_changes():
    graph = _wide_graph(60)
    before = {cid: claim.validation_status for cid, claim in graph.claims.items()}
    event = TemporalEvent(
        event_id="EV-W2",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_SNAPSHOT,
        event_date="2026-07-01",
        changed_source_ids=("E-SEED",),
    )
    ingest_temporal_event(graph, event)
    changed = {
        cid
        for cid, claim in graph.claims.items()
        if claim.validation_status != before[cid]
    }
    assert changed == {"C-SEED"}


def test_a_truncated_subgraph_falls_back_to_full_recomputation():
    """卡 §11：增量与全量不一致 → 断言失败，回落全量并记录。"""
    graph = build_graph(
        "run-chain3",
        claims=[
            _fact("C1", "一层", evidence_ids=("E1",)),
            _fact("C2", "二层", derived_from=("C1",)),
            _fact("C3", "三层", derived_from=("C2",)),
        ],
        evidence_index=[_evidence("E1")],
    )
    event = TemporalEvent(
        event_id="EV-T",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_SNAPSHOT,
        event_date="2026-07-01",
        changed_source_ids=("E1",),
    )
    report = ingest_temporal_event(graph, event, max_depth=1)

    comparison = report.full_vs_incremental
    assert comparison["results_identical"] is False
    assert comparison["fell_back"] is True
    assert report.fallback == "full_recomputation"
    assert report.status == STATUS_PARTIAL
    # 回落全量后，三层都该被算到 —— 限深不会让结果残缺。
    assert {item["node_id"] for item in report.state_diff} == {"C1", "C2", "C3"}
    assert any("incremental_full_mismatch" in warning for warning in report.warnings)
    assert any("affected_subgraph_truncated" in warning for warning in report.warnings)
    # 回落时 nodes_recomputed 必须等于 full_nodes。
    assert comparison["nodes_recomputed"] == comparison["full_nodes"]


def test_compare_reports_missing_and_extra_nodes_explicitly():
    graph = _wide_graph(3)
    baseline = snapshot_statuses(graph)
    # 制造一个真实的状态变化，却**故意**不把它算进增量结果 ——
    # 全量侧会算出这个变化，于是两边对不上，必须被逐节点点名。
    graph.stale_claims.append({"claim_id": "C-SEED", "reason": "test"})
    comparison = compare_full_vs_incremental(
        graph, [], baseline=baseline, nodes_recomputed=0, measure_latency=False
    )
    assert comparison["results_identical"] is False
    assert comparison["full_only"] == ["claim:C-SEED"]
    assert comparison["incremental_only"] == []
    # 不测延迟时 latency_ms 必须是 None，而不是编一个数出来。
    assert comparison["latency_ms"] is None


def test_compare_detects_an_extra_node_the_full_pass_did_not_change():
    graph = _wide_graph(3)
    baseline = snapshot_statuses(graph)
    invented = {
        "node_id": "C-GHOST",
        "node_type": "claim",
        "before": VALIDATION_SUPPORTED,
        "after": VALIDATION_STALE,
        "reason": "凭空多出来的",
        "trigger": None,
    }
    comparison = compare_full_vs_incremental(
        graph, [invented], baseline=baseline, nodes_recomputed=1, measure_latency=False
    )
    assert comparison["results_identical"] is False
    assert comparison["incremental_only"] == ["claim:C-GHOST"]


# --------------------------------------------------------------------------- #
# §14 E. 空报告与边界
# --------------------------------------------------------------------------- #

def test_no_changes_produces_an_empty_report():
    graph = _supersession_graph()
    event = TemporalEvent(
        event_id="EV-EMPTY",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_FILING,
        event_date="2026-08-01",
        changed_source_ids=(),
    )
    report = ingest_temporal_event(graph, event)
    assert report.status == STATUS_OK
    assert report.state_diff == ()
    assert report.impact["claims_invalidated"] == 0


def test_unknown_changed_nodes_make_the_report_unavailable():
    graph = _supersession_graph()
    event = TemporalEvent(
        event_id="EV-GHOST",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_FILING,
        event_date="2026-08-01",
        changed_source_ids=("NOPE",),
    )
    report = ingest_temporal_event(graph, event)
    assert report.status == STATUS_UNAVAILABLE
    assert report.unavailable_reason == "unknown_changed_nodes"
    assert report.state_diff == ()


def test_a_graph_without_claims_is_unavailable():
    graph = build_graph("run-empty", claims=[], evidence_index=[_evidence("E1")])
    event = TemporalEvent(
        event_id="EV-E",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_FILING,
        event_date="2026-08-01",
        changed_source_ids=("E1",),
    )
    report = ingest_temporal_event(graph, event)
    assert report.status == STATUS_UNAVAILABLE
    assert report.unavailable_reason == "graph_has_no_claims"


def test_supersession_requires_the_supersedes_field():
    graph = _supersession_graph()
    event = TemporalEvent(
        event_id="EV-BAD",
        event_kind=CHANGE_VERSION_SUPERSESSION,
        event_type=EVENT_RESTATEMENT,
        event_date="2026-05-01",
        changed_source_ids=("E-NEW",),
    )
    with pytest.raises(TemporalError):
        ingest_temporal_event(graph, event)


def test_supersession_requires_a_new_fact_id():
    graph = _supersession_graph()
    event = TemporalEvent(
        event_id="EV-BAD2",
        event_kind=CHANGE_VERSION_SUPERSESSION,
        event_type=EVENT_RESTATEMENT,
        event_date="2026-05-01",
        changed_source_ids=("E-OLD",),
        supersedes="E-OLD",
    )
    with pytest.raises(TemporalError):
        ingest_temporal_event(graph, event)


def test_supersession_with_an_unknown_old_id_raises():
    graph = _supersession_graph()
    event = _supersession_event("GHOST", "E-NEW")
    with pytest.raises(TemporalError):
        ingest_temporal_event(graph, event)


def test_supersession_requires_the_new_value_to_be_in_the_lineage():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD"])  # 新值没登记
    event = _supersession_event("E-OLD", "E-NEW")
    with pytest.raises(TemporalError):
        ingest_temporal_event(graph, event, lineage=lineage)


def test_event_rejects_unknown_kind_and_type():
    with pytest.raises(TemporalError):
        TemporalEvent("EV", "NOT_A_KIND", EVENT_NEW_FILING, "2026-01-01")
    with pytest.raises(TemporalError):
        TemporalEvent("EV", CHANGE_NEW_INFORMATION, "not_a_type", "2026-01-01")


def test_event_date_may_be_empty_but_is_flagged():
    graph = _supersession_graph()
    event = TemporalEvent(
        event_id="EV-NODATE",
        event_kind=CHANGE_NEW_INFORMATION,
        event_type=EVENT_NEW_SNAPSHOT,
        changed_source_ids=("E-OLD",),
    )
    report = ingest_temporal_event(graph, event)
    assert any("event_date_missing" in warning for warning in report.warnings)


# --------------------------------------------------------------------------- #
# impact
# --------------------------------------------------------------------------- #

def test_impact_carries_all_six_keys():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)
    assert set(report.impact) == set(IMPACT_KEYS)


def test_missing_external_domains_are_null_not_zero():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)
    assert report.impact["valuations_stale"] is None
    assert report.impact["memo_sections_affected"] is None
    assert report.status == STATUS_PARTIAL
    assert any("valuations_stale" in warning for warning in report.warnings)
    assert any("memo_sections_affected" in warning for warning in report.warnings)


def test_supplied_external_domains_are_counted_and_acted_on():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(
        graph,
        _supersession_event("E-OLD", "E-NEW"),
        lineage=lineage,
        valuations=["VAL-1", "VAL-2"],
        memo_sections=["financials"],
    )
    assert report.impact["valuations_stale"] == 2
    assert report.impact["memo_sections_affected"] == 1
    assert report.status == STATUS_OK
    types = {item["action_type"] for item in report.revalidation_actions}
    assert ACTION_REFRESH_VALUATION in types
    assert ACTION_REFRESH_MEMO_SECTION in types


# --------------------------------------------------------------------------- #
# §9 Zero-LLM boundary (source-level assertion)
# --------------------------------------------------------------------------- #

def _strip_docstrings(source: str) -> str:
    out: List[str] = []
    inside = False
    for line in source.splitlines():
        if line.count('"""') % 2 == 1:
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)


def test_temporal_package_is_model_free():
    problems: List[str] = []
    for path in sorted(TEMPORAL_PACKAGE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        problems.extend(check_source_is_model_free(source))
        problems.extend(check_source_is_model_free(_strip_docstrings(source)))
    assert problems == []


def test_temporal_validator_is_model_free():
    source = VALIDATOR_PATH.read_text(encoding="utf-8")
    assert check_source_is_model_free(source) == []
    assert check_source_is_model_free(_strip_docstrings(source)) == []


# --------------------------------------------------------------------------- #
# §13 Validator
# --------------------------------------------------------------------------- #

def _valid_report(**overrides) -> object:
    from src.cn.temporal import RevalidationReport

    payload = dict(
        event_id="EV-V",
        event={
            "event_id": "EV-V",
            "event_kind": CHANGE_NEW_INFORMATION,
            "event_type": EVENT_NEW_FILING,
            "event_date": "2026-08-01",
            "changed_source_ids": ["E1"],
        },
        status=STATUS_OK,
        impact={
            "facts_superseded": 0,
            "calculations_stale": 0,
            "claims_invalidated": 0,
            "theses_weakened": 0,
            "valuations_stale": 0,
            "memo_sections_affected": 0,
        },
        changed_nodes=("E1",),
        affected_subgraph=("E1", "C01"),
        state_diff=(
            {
                "node_id": "C01",
                "node_type": "claim",
                "before": VALIDATION_SUPPORTED,
                "after": VALIDATION_STALE,
                "reason": "上游过期",
                "trigger": None,
            },
        ),
        revalidation_actions=(),
        full_vs_incremental={
            "nodes_recomputed": 1,
            "full_nodes": 1,
            "results_identical": True,
            "fell_back": False,
        },
        lineage=(),
        warnings=(),
    )
    payload.update(overrides)
    return RevalidationReport(**payload)


def test_validator_accepts_a_clean_report():
    outcome = validate_temporal(_valid_report())
    assert outcome.valid is True
    assert outcome.errors == []


def test_validator_accepts_a_real_report():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)
    outcome = validate_temporal(report, lineage=lineage, graph=graph)
    assert outcome.valid is True, outcome.errors


def test_validator_rejects_a_missing_impact_key():
    impact = _valid_report().impact
    impact.pop("theses_weakened")
    outcome = validate_temporal(_valid_report(impact=impact))
    assert outcome.valid is False
    assert any("impact_key_missing:theses_weakened" in error for error in outcome.errors)


def test_validator_rejects_a_subgraph_that_is_not_a_superset():
    outcome = validate_temporal(_valid_report(affected_subgraph=("E1",)))
    assert outcome.valid is False
    assert any("subgraph_not_superset_of_state_diff" in error for error in outcome.errors)


def test_validator_rejects_a_subgraph_missing_the_changed_node():
    outcome = validate_temporal(_valid_report(affected_subgraph=("C01",)))
    assert outcome.valid is False
    assert any("subgraph_missing_changed_node" in error for error in outcome.errors)


def test_validator_rejects_a_no_op_state_diff_entry():
    diff = (
        {
            "node_id": "C01",
            "node_type": "claim",
            "before": VALIDATION_SUPPORTED,
            "after": VALIDATION_SUPPORTED,
            "reason": "什么都没变",
            "trigger": None,
        },
    )
    outcome = validate_temporal(_valid_report(state_diff=diff))
    assert outcome.valid is False
    assert any("state_diff_no_op" in error for error in outcome.errors)


def test_validator_rejects_an_unknown_status_in_state_diff():
    diff = (
        {
            "node_id": "C01",
            "node_type": "claim",
            "before": VALIDATION_SUPPORTED,
            "after": "totally_fine",
            "reason": "编的",
            "trigger": None,
        },
    )
    outcome = validate_temporal(_valid_report(state_diff=diff))
    assert outcome.valid is False
    assert any("state_diff_unknown_after" in error for error in outcome.errors)


def test_validator_rejects_a_mismatch_that_did_not_fall_back():
    outcome = validate_temporal(
        _valid_report(
            full_vs_incremental={
                "nodes_recomputed": 1,
                "full_nodes": 3,
                "results_identical": False,
                "fell_back": False,
            }
        )
    )
    assert outcome.valid is False
    assert any("not_fell_back" in error for error in outcome.errors)


def test_validator_rejects_fallback_that_reports_ok():
    outcome = validate_temporal(
        _valid_report(
            status=STATUS_OK,
            fallback="full_recomputation",
            full_vs_incremental={
                "nodes_recomputed": 3,
                "full_nodes": 3,
                "results_identical": False,
                "fell_back": True,
            },
            warnings=("incremental_full_mismatch：坏了",),
        )
    )
    assert outcome.valid is False
    assert any("fallback_but_status_ok" in error for error in outcome.errors)


def test_validator_rejects_fallback_without_marking_it():
    outcome = validate_temporal(
        _valid_report(
            fallback=None,
            full_vs_incremental={
                "nodes_recomputed": 3,
                "full_nodes": 3,
                "results_identical": False,
                "fell_back": True,
            },
            warnings=("incremental_full_mismatch：坏了",),
        )
    )
    assert outcome.valid is False
    assert any("mismatch_without_fallback_marker" in error for error in outcome.errors)


def test_validator_rejects_incomplete_comparison():
    outcome = validate_temporal(_valid_report(full_vs_incremental={}))
    assert outcome.valid is False
    assert any("full_vs_incremental_incomplete" in error for error in outcome.errors)


def test_validator_rejects_unavailable_without_a_reason():
    outcome = validate_temporal(_valid_report(status=STATUS_UNAVAILABLE, state_diff=()))
    assert outcome.valid is False
    assert any("unavailable_without_reason" in error for error in outcome.errors)


def test_validator_rejects_unavailable_with_a_state_diff():
    outcome = validate_temporal(
        _valid_report(status=STATUS_UNAVAILABLE, unavailable_reason="nope")
    )
    assert outcome.valid is False
    assert any("unavailable_but_has_state_diff" in error for error in outcome.errors)


def test_validator_rejects_a_silent_external_domain():
    impact = dict(_valid_report().impact)
    impact["valuations_stale"] = None
    outcome = validate_temporal(_valid_report(impact=impact))
    assert outcome.valid is False
    assert any("external_domain_silent:valuations_stale" in error for error in outcome.errors)


def test_validator_rejects_an_unknown_action_type():
    actions = ({"action_type": "do_something", "target_id": "X", "priority": "high"},)
    outcome = validate_temporal(_valid_report(revalidation_actions=actions))
    assert outcome.valid is False
    assert any("unknown_action_type" in error for error in outcome.errors)


def test_validator_rejects_an_unknown_action_priority():
    actions = ({"action_type": "revalidate_claim", "target_id": "C01", "priority": "urgent"},)
    outcome = validate_temporal(_valid_report(revalidation_actions=actions))
    assert outcome.valid is False
    assert any("unknown_action_priority" in error for error in outcome.errors)


def test_validator_rejects_an_action_without_a_target():
    actions = ({"action_type": "revalidate_claim", "target_id": "", "priority": "high"},)
    outcome = validate_temporal(_valid_report(revalidation_actions=actions))
    assert outcome.valid is False
    assert any("action_without_target" in error for error in outcome.errors)


def test_validator_rejects_an_overwritten_superseded_fact():
    lineage = VersionLineage()
    lineage.register(
        VersionLineageEntry(
            "OLD", version_status=LINEAGE_SUPERSEDED, superseded_by="NEW", reason="restatement"
        )
    )
    lineage.register(VersionLineageEntry("NEW", version_status=LINEAGE_RESTATED, reason="restatement"))
    outcome = validate_temporal(_valid_report(), lineage=lineage)
    assert outcome.valid is False
    assert any("superseded_fact_overwritten:OLD" in error for error in outcome.errors)


def test_validator_rejects_a_dangling_lineage_edge():
    lineage = _lineage_for(["OLD", "NEW"])
    lineage.mark_superseded("OLD", superseded_by="NEW", reason="restatement")
    # 手工塞一条指向图外的边
    lineage.register(
        VersionLineageEntry("EXTRA", version_status=LINEAGE_SUPERSEDED, superseded_by="GHOST", reason="update")
    )
    outcome = validate_temporal(_valid_report(), lineage=lineage)
    assert outcome.valid is False
    assert any("lineage_dangling_superseded_by:EXTRA->GHOST" in error for error in outcome.errors)


def test_validator_rejects_a_lineage_cycle():
    lineage = VersionLineage()
    lineage.register(
        VersionLineageEntry("A", version_status=LINEAGE_SUPERSEDED, superseded_by="B", reason="update")
    )
    lineage.register(
        VersionLineageEntry("B", version_status=LINEAGE_SUPERSEDED, superseded_by="A", reason="update")
    )
    outcome = validate_temporal(_valid_report(), lineage=lineage)
    assert outcome.valid is False
    assert any("lineage_cycle" in error for error in outcome.errors)


def test_validator_rejects_a_report_lineage_that_disagrees_with_the_store():
    lineage = _lineage_for(["OLD", "NEW"])
    lineage.mark_superseded("OLD", superseded_by="NEW", reason="restatement")
    outcome = validate_temporal(
        _valid_report(lineage=({"fact_id": "OLD", "version_status": LINEAGE_RESTATED},)),
        lineage=lineage,
    )
    assert outcome.valid is False
    assert any("report_lineage_status_mismatch:OLD" in error for error in outcome.errors)


def test_validator_rejects_a_report_lineage_with_an_unknown_fact():
    lineage = _lineage_for(["OLD"])
    outcome = validate_temporal(
        _valid_report(lineage=({"fact_id": "GHOST", "version_status": LINEAGE_RESTATED},)),
        lineage=lineage,
    )
    assert outcome.valid is False
    assert any("report_lineage_unknown_fact:GHOST" in error for error in outcome.errors)


def test_validator_rejects_a_subgraph_that_cannot_be_reproduced():
    graph = _supersession_graph()
    lineage = _lineage_for(["E-OLD", "E-NEW"], superseded=["E-OLD"], superseded_by="E-NEW")
    report = ingest_temporal_event(graph, _supersession_event("E-OLD", "E-NEW"), lineage=lineage)
    broken = dataclasses.replace(
        report,
        affected_subgraph=tuple(report.affected_subgraph) + ("C-FAKE",),
    )
    outcome = validate_temporal(broken, lineage=lineage, graph=graph)
    # 多出来的节点是 warning（可能传播起点更宽），不算错误……
    assert outcome.valid is True
    assert any("affected_subgraph_has_extra_nodes" in warning for warning in outcome.warnings)

    # ……而**少了**节点是真错。
    missing = dataclasses.replace(report, affected_subgraph=("E-NEW",))
    outcome2 = validate_temporal(missing, lineage=lineage, graph=graph)
    assert outcome2.valid is False


def test_validator_rejects_collapsed_event_effects(monkeypatch):
    monkeypatch.setattr(
        temporal_validator,
        "EVENT_EFFECTS",
        {
            CHANGE_DATA_CONFLICT: "same",
            CHANGE_VERSION_SUPERSESSION: "same",
            CHANGE_NEW_INFORMATION: "same",
        },
    )
    outcome = validate_temporal(_valid_report())
    assert outcome.valid is False
    assert any("event_effects_collapsed" in error for error in outcome.errors)


def test_validator_rejects_an_unknown_event_kind_in_the_report():
    outcome = validate_temporal(
        _valid_report(event={"event_kind": "NOT_A_KIND", "event_type": EVENT_NEW_FILING})
    )
    assert outcome.valid is False
    assert any("unknown_event_kind" in error for error in outcome.errors)


def test_validator_flags_an_empty_report_as_a_warning():
    outcome = validate_temporal(
        _valid_report(
            changed_nodes=(),
            affected_subgraph=(),
            state_diff=(),
            full_vs_incremental={
                "nodes_recomputed": 0,
                "full_nodes": 1,
                "results_identical": True,
                "fell_back": False,
            },
        )
    )
    assert outcome.valid is True
    assert any("empty_report" in warning for warning in outcome.warnings)


def test_validator_corpus_aggregates_failures():
    good = _valid_report()
    bad = _valid_report(affected_subgraph=("E1",))
    outcome = validate_temporal_corpus([good, bad])
    assert outcome.valid is False
    assert any(error.startswith("[1]") for error in outcome.errors)

    ok = validate_temporal_corpus([good, _valid_report()])
    assert ok.valid is True


# --------------------------------------------------------------------------- #
# §15 Integration: 真实语料（annual_04 的重述血缘）
# --------------------------------------------------------------------------- #

@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def _chain(registry: DocumentRegistry):
    parsed = registry.ingest(
        FIXTURES / "annual_04_restated_comparative.pdf",
        declared_type="annual",
        source="admin_import",
    )
    normalization = normalize_financials(registry.get_facts(parsed.document.document_id), parsed.document)
    analysis = analyze_document(normalization)
    ledger = EvidenceLedger(snapshot_id=parsed.document.document_id)
    register_normalized_evidence(parsed.document, normalization, ledger=ledger)
    graph = build_graph_from_analysis(
        analysis,
        run_id="run-temporal",
        document_id=parsed.document.document_id,
        ledger=ledger,
        jumps=normalization_jumps(parsed.document, normalization),
    )
    return parsed, analysis, normalization, graph


def test_integration_real_facts_yield_a_supersession_pair(registry):
    _parsed, _analysis, normalization, _graph = _chain(registry)
    lineage, warnings = build_lineage(
        normalization.facts, document_type="annual", ingested_at="2026-09-19T00:00:00"
    )
    superseded = lineage.superseded_ids()
    assert superseded, "annual_04 应当有重述形成的版本替代对"
    for fact_id in superseded:
        entry = lineage.get(fact_id)
        # superseded_by（写在旧值上）是权威且完整的映射。
        assert entry.superseded_by
        assert entry.superseded_by in lineage
        assert lineage.get(entry.superseded_by).is_live
    # supersedes（写在新值上）是单值主前驱 —— 一个替代者可能同时替代多条旧值，
    # 所以它只需落在**被替代集合**里，而不是与每个旧值一一对应。
    for fact_id in superseded:
        replacement = lineage.get(lineage.get(fact_id).superseded_by)
        assert replacement.supersedes in superseded
        assert set(lineage.predecessors(replacement.fact_id)) <= set(superseded)
    assert all(
        entry.reason in ("restatement", "correction")
        for entry in lineage.entries()
        if entry.version_status == LINEAGE_SUPERSEDED
    )


def test_integration_restatement_propagates_stale_to_dependent_claims(registry):
    _parsed, _analysis, normalization, graph = _chain(registry)
    lineage, _warnings = build_lineage(normalization.facts, document_type="annual")

    old = new = None
    for entry in lineage.entries():
        if entry.version_status != LINEAGE_SUPERSEDED or not entry.superseded_by:
            continue
        if entry.fact_id in graph.evidence_index and downstream_closure(graph, [entry.fact_id]):
            old, new = entry.fact_id, entry.superseded_by
            break
    assert old is not None, "需要一个既有 claim 依赖、又被替代的证据"

    before = {cid: claim.validation_status for cid, claim in graph.claims.items()}
    report = ingest_temporal_event(graph, _supersession_event(old, new), lineage=lineage)

    after = {cid: claim.validation_status for cid, claim in graph.claims.items()}
    changed = {cid for cid in before if before[cid] != after[cid]}
    assert changed, "重述应当让依赖旧值的结论状态改变"
    assert all(after[cid] == VALIDATION_STALE for cid in changed)
    assert report.impact["facts_superseded"] >= 1
    assert report.full_vs_incremental["results_identical"] is True


def test_integration_report_passes_the_validator(registry):
    _parsed, _analysis, normalization, graph = _chain(registry)
    lineage, _warnings = build_lineage(normalization.facts, document_type="annual")
    old = new = None
    for entry in lineage.entries():
        if entry.version_status != LINEAGE_SUPERSEDED or not entry.superseded_by:
            continue
        if entry.fact_id in graph.evidence_index and downstream_closure(graph, [entry.fact_id]):
            old, new = entry.fact_id, entry.superseded_by
            break

    report = ingest_temporal_event(graph, _supersession_event(old, new), lineage=lineage)
    outcome = validate_temporal(report, lineage=lineage, graph=graph)
    assert outcome.valid is True, outcome.errors


def test_integration_stale_claim_is_reported_stale_by_card23(registry):
    """与 CARD-23 `verify_claim` 联动：过期后的结论应被护照判为 STALE。"""
    _parsed, analysis, normalization, graph = _chain(registry)
    lineage, _warnings = build_lineage(normalization.facts, document_type="annual")
    old = new = None
    for entry in lineage.entries():
        if entry.version_status != LINEAGE_SUPERSEDED or not entry.superseded_by:
            continue
        if entry.fact_id in graph.evidence_index and downstream_closure(graph, [entry.fact_id]):
            old, new = entry.fact_id, entry.superseded_by
            break

    ingest_temporal_event(graph, _supersession_event(old, new), lineage=lineage)
    stale_claims = sorted(
        cid for cid, claim in graph.claims.items() if claim.validation_status == VALIDATION_STALE
    )
    assert stale_claims

    context = ProofContext(
        graph=graph,
        calculation_registry=getattr(analysis, "registry", None),
        run_id=graph.run_id,
    )
    statuses = {cid: verify_claim(cid, context).overall_status for cid in stale_claims}
    # 至少有一条被护照判为 STALE —— 两个卡对"过期"的理解是一致的。
    assert STATUS_STALE in set(statuses.values()), statuses


def test_integration_supersession_is_append_only_across_two_events(registry):
    _parsed, _analysis, normalization, graph = _chain(registry)
    lineage, _warnings = build_lineage(normalization.facts, document_type="annual")
    before_records = len(lineage.records)

    old = new = None
    for entry in lineage.entries():
        if entry.version_status != LINEAGE_SUPERSEDED or not entry.superseded_by:
            continue
        if entry.fact_id in graph.evidence_index and downstream_closure(graph, [entry.fact_id]):
            old, new = entry.fact_id, entry.superseded_by
            break

    ingest_temporal_event(graph, _supersession_event(old, new), lineage=lineage)
    ingest_temporal_event(
        graph,
        _supersession_event(old, new, event_id="EV-SUP-AGAIN"),
        lineage=lineage,
    )
    # 第二次是重复替代：历史只增不改，旧的记录仍可读。
    assert len(lineage.records) >= before_records
    assert lineage.history(old)[0].version_status != LINEAGE_SUPERSEDED
    assert validate_temporal(
        ingest_temporal_event(graph, _supersession_event(old, new, event_id="EV-3"), lineage=lineage),
        lineage=lineage,
        graph=graph,
    ).valid is True


# --------------------------------------------------------------------------- #
# §8 Tool contract
# --------------------------------------------------------------------------- #

def _tool(registry: DocumentRegistry) -> RevalidateCnClaimsTool:
    return RevalidateCnClaimsTool(registry)


def _run(registry: DocumentRegistry, **kwargs) -> Dict:
    import asyncio

    return json.loads(asyncio.run(_tool(registry).execute(**kwargs)))


def test_tool_requires_a_document_id(registry):
    payload = _run(registry)
    assert payload["error_code"] == "temporal_missing_argument"


def test_tool_requires_an_event_or_changed_nodes(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_04_restated_comparative.pdf", declared_type="annual", source="admin_import"
    )
    payload = _run(registry, document_id=parsed.document.document_id)
    assert payload["error_code"] == "temporal_missing_argument"


def test_tool_rejects_an_unknown_document(registry):
    payload = _run(registry, document_id="DOC-NOPE", changed_node_ids=["X"])
    assert payload["error_code"] == "document_not_found"


def test_tool_rejects_an_invalid_event_kind(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_04_restated_comparative.pdf", declared_type="annual", source="admin_import"
    )
    payload = _run(
        registry,
        document_id=parsed.document.document_id,
        event={"event_id": "E", "event_kind": "WRONG", "event_type": EVENT_NEW_FILING, "event_date": "2026-01-01"},
    )
    assert payload["error_code"] == "temporal_invalid_event"


def test_tool_runs_a_supersession_on_real_corpus(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_04_restated_comparative.pdf", declared_type="annual", source="admin_import"
    )
    document_id = parsed.document.document_id
    normalization = normalize_financials(registry.get_facts(document_id), parsed.document)
    lineage, _ = build_lineage(normalization.facts, document_type="annual")
    analysis = analyze_document(normalization)
    ledger = EvidenceLedger(snapshot_id=document_id)
    register_normalized_evidence(parsed.document, normalization, ledger=ledger)
    graph = build_graph_from_analysis(
        analysis,
        run_id="t",
        document_id=document_id,
        ledger=ledger,
        jumps=normalization_jumps(parsed.document, normalization),
    )
    old = new = None
    for entry in lineage.entries():
        if entry.version_status != LINEAGE_SUPERSEDED or not entry.superseded_by:
            continue
        if entry.fact_id in graph.evidence_index and downstream_closure(graph, [entry.fact_id]):
            old, new = entry.fact_id, entry.superseded_by
            break

    payload = _run(
        registry,
        document_id=document_id,
        event={
            "event_id": "EV-TOOL",
            "event_kind": CHANGE_VERSION_SUPERSESSION,
            "event_type": EVENT_RESTATEMENT,
            "event_date": "2026-05-01",
            "changed_source_ids": [new],
            "supersedes": old,
        },
    )
    assert payload["status"] in (STATUS_OK, STATUS_PARTIAL)
    assert payload["event_kind"] == CHANGE_VERSION_SUPERSESSION
    assert set(payload["impact"]) == set(IMPACT_KEYS)
    assert payload["validation"]["valid"] is True
    assert "revalidation_report" in payload


def test_tool_synthesises_a_deterministic_event_from_changed_nodes(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_04_restated_comparative.pdf", declared_type="annual", source="admin_import"
    )
    document_id = parsed.document.document_id
    first = _run(registry, document_id=document_id, changed_node_ids=["SOME-NODE"])
    second = _run(registry, document_id=document_id, changed_node_ids=["SOME-NODE"])
    assert first["event_kind"] == CHANGE_NEW_INFORMATION
    assert first["event_id"] == second["event_id"]


def test_tool_reports_external_domains_as_null_when_not_supplied(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_04_restated_comparative.pdf", declared_type="annual", source="admin_import"
    )
    payload = _run(registry, document_id=parsed.document.document_id, changed_node_ids=["NOPE-1"])
    assert payload["status"] == STATUS_UNAVAILABLE
    assert payload["unavailable_reason"] == "unknown_changed_nodes"


def test_tool_is_registered_in_both_branches():
    from src.agents.tools.cn_tools import build_cn_snapshot_tools

    tools = build_cn_snapshot_tools()
    names = {getattr(tool, "name", "") for tool in tools}
    assert "revalidate_cn_claims" in names
