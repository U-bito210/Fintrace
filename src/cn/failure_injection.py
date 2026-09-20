"""CARD-12 §14 — the seven scripted failure-injection demonstrations.

Each scenario injects one real fault into a real pipeline and asserts the
*system's* response, not the injection's. The point of the demo is the sentence
"FinTrace knows when it must not conclude", so a scenario only passes when the
pipeline refuses, warns, or blocks — and fails loudly if it sails on.

Design rules:

* **Real modules, real fixtures.** No mocks. A scenario that only proves a mock
  behaves would prove nothing on stage.
* **A scenario that cannot run says so.** CARD-13's source-conflict scenario is
  registered but reports ``ok=False`` with ``status="deferred"`` and the reason,
  rather than being quietly dropped from the list (§14 lists seven).
* **Failures are reported, not raised.** ``run_all`` returns results; the caller
  decides what a failed scenario means.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .conflict import (
    CHANGE_VERSION_SUPERSESSION,
    RULE_REGULATORY_CORRECTION,
    STATUS_RESOLVED,
    conflict_choices_for_metric,
    correction_candidates,
    correction_pairs,
    default_source_class,
    resolve_conflicts,
    summarize as summarize_conflicts,
)
from .documents.registry import DocumentRegistry
from .evidence import EvidenceLedger
from .graph import build_graph_from_analysis, deactivate_evidence, revalidate
from .normalization import normalize_financials
from .normalization.evidence_bridge import normalization_jumps, register_normalized_evidence
from .valuation import valuation_inputs_from_normalization
from .analysis import analyze_document
from ..validation import FinancialValidator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
DOCUMENTS = FIXTURES / "documents"
DRAFTS = FIXTURES / "report_drafts"

#: Filings used by more than one scenario.
REFERENCE_FILINGS = (
    ("annual_01_profit_continued.pdf", "annual"),
    ("annual_04_restated_comparative.pdf", "annual"),
    ("annual_05_reclassification.pdf", "annual"),
    ("announcement_04_correction.pdf", "announcement"),
)


@dataclass
class FailureInjectionResult:
    """Outcome of one scripted failure-injection scenario (card §14)."""

    scenario_id: int
    slug: str
    title: str
    injection: str
    expected_behavior: str
    observed: str
    ok: bool
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "slug": self.slug,
            "title": self.title,
            "injection": self.injection,
            "expected_behavior": self.expected_behavior,
            "observed": self.observed,
            "ok": self.ok,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: int
    slug: str
    title: str
    injection: str
    expected_behavior: str
    #: ``None`` means "implemented here"; otherwise the card that must land first.
    deferred_to: Optional[str] = None


SCENARIOS: Tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        1,
        "missing_total_shares",
        "缺失总股本",
        "抹掉资产负债表里的期末总股本，再构建估值输入",
        "阻断每股估值（fail-closed），并明确报缺，不得用任何替代值填补",
    ),
    ScenarioSpec(
        2,
        "page_removed",
        "删除 PDF 某页",
        "让证据节点的页码缺失（相当于该页不可定位）",
        "citation incomplete 告警，claim 的 trace 不再可定位到页面",
    ),
    ScenarioSpec(
        3,
        "unit_wan_to_yi",
        "单位万元→亿元",
        "把同指标的一条证据单位改成不兼容的量纲族",
        "unit checker 报 unit_mismatch 错误",
    ),
    ScenarioSpec(
        4,
        "draft_wrong_yoy",
        "草稿给错误同比",
        "撰写含错误同比的研报草稿（语料已植入）",
        "report checker 定位该句并给出正确值与披露来源",
    ),
    ScenarioSpec(
        5,
        "stale_filing_correction",
        "旧财报+更正公告",
        "同一事项存在旧披露与更正公告两个来源",
        "source conflict：按优先级取更正值并留痕",
    ),
    ScenarioSpec(
        6,
        "evidence_deleted",
        "删除关键 Evidence",
        "审计式撤回一条被 fact 依赖的证据",
        "对应 fact 及其下游 claim 变 blocked，旧→新状态 diff 可读",
    ),
    ScenarioSpec(
        7,
        "unsupported_causal",
        "改写无证据因果结论",
        "草稿写入只给单侧来源的因果断言（语料已植入）",
        "unsupported causal inference 被标记，要求补双侧独立来源或改写",
    ),
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _registry() -> DocumentRegistry:
    return DocumentRegistry(
        root=Path(tempfile.mkdtemp(prefix="cn_injection_")) / "documents",
        allow_roots=[DOCUMENTS],
    )


def _ingest_all(registry: DocumentRegistry, names: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    ids: Dict[str, str] = {}
    for filename, declared in names:
        path = DOCUMENTS / filename
        if not path.exists():
            continue
        parsed = registry.ingest(path, declared_type=declared, source="admin_import")
        ids[filename] = parsed.document.document_id
    return ids


def _chain(registry: DocumentRegistry, document_id: str, *, run_id: str = ""):
    """CARD-01 → 02 → 03 → 09 for one ingested document (the shared test seam)."""
    document = registry.get(document_id)
    if document is None:
        raise KeyError(document_id)
    normalization = normalize_financials(registry.get_facts(document_id), document)
    analysis = analyze_document(normalization)
    ledger = EvidenceLedger(snapshot_id=document_id)
    register_normalized_evidence(document, normalization, ledger=ledger)
    jumps = normalization_jumps(document, normalization)
    graph = build_graph_from_analysis(
        analysis,
        run_id=run_id or f"injection-{document_id}",
        document_id=document_id,
        ledger=ledger,
        jumps=jumps,
    )
    return normalization, analysis, ledger, graph


def _result(
    spec: ScenarioSpec,
    *,
    ok: bool,
    observed: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> FailureInjectionResult:
    return FailureInjectionResult(
        scenario_id=spec.scenario_id,
        slug=spec.slug,
        title=spec.title,
        injection=spec.injection,
        expected_behavior=spec.expected_behavior,
        observed=observed,
        ok=ok,
        evidence={"status": "ok" if ok else "unexpected", **(evidence or {})},
    )


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
def scenario_missing_total_shares() -> Any:
    spec = SCENARIOS[0]
    registry = _registry()
    try:
        ids = _ingest_all(registry, (("annual_06_equity_shares.pdf", "annual"),))
        if not ids:
            return _result(spec, ok=False, observed="缺少 annual_06_equity_shares.pdf 夹具")
        document = registry.get(next(iter(ids.values())))
        normalization = normalize_financials(registry.get_facts(document.document_id), document)
        if not normalization.statements:
            return _result(spec, ok=False, observed="夹具未产出任何期间记录，无法注入")

        # The injection: drop total shares from the balance record.
        statements: List[Any] = []
        removed = False
        for record in normalization.statements:
            if record.statement_type == "balance" and "total_shares" in dict(record.values):
                values = {k: v for k, v in dict(record.values).items() if k != "total_shares"}
                removed = True
                statements.append(
                    SimpleNamespace(
                        statement_type="balance",
                        values=values,
                        fiscal_period=record.fiscal_period,
                        source_periods=getattr(record, "source_periods", ()),
                    )
                )
            else:
                statements.append(record)
        if not removed:
            return _result(
                spec,
                ok=False,
                observed="夹具的资产负债表本就没有 total_shares，注入无效果（无法证明阻断路径）",
            )

        stub = SimpleNamespace(
            ttm=normalization.ttm, statements=statements, facts=normalization.facts
        )
        try:
            valuation_inputs_from_normalization(stub, symbol="SYN-INJ-1", raw_price=10.0)
        except (TypeError, ValueError) as exc:
            return _result(
                spec,
                ok=True,
                observed=f"每股估值被阻断（{type(exc).__name__}: {exc}）",
                evidence={"injection": "removed total_shares from balance record", "error_type": type(exc).__name__},
            )
        return _result(spec, ok=False, observed="未阻断：缺少总股本仍产出了估值输入")
    finally:
        registry.close()


def scenario_page_removed() -> Any:
    spec = SCENARIOS[1]
    registry = _registry()
    try:
        ids = _ingest_all(registry, (("annual_01_profit_continued.pdf", "annual"),))
        if not ids:
            return _result(spec, ok=False, observed="缺少 annual_01_profit_continued.pdf 夹具")
        document_id = next(iter(ids.values()))
        _normalization, _analysis, _ledger, graph = _chain(registry, document_id)

        def paged_claims() -> List[str]:
            out: List[str] = []
            for claim in sorted(graph.facts, key=lambda item: item.claim_id):
                pages = [node for node in graph.trace(claim.claim_id).flatten() if node.node_type == "page"]
                if pages:
                    out.append(claim.claim_id)
            return out

        before = paged_claims()
        if not before:
            return _result(spec, ok=False, observed="证据账本没有任何可定位到页面的 fact，注入无效果")

        # The injection: the page is gone, so the document locator goes with it.
        # The snapshot timestamp deliberately survives — it is a *different*
        # locator, and pretending otherwise would overstate the damage.
        missing_pages = 0
        for evidence_id, node in list(graph.evidence_index.items()):
            if node.page is not None:
                graph.evidence_index[evidence_id] = dataclasses.replace(
                    node, page=None, bbox=None
                )
                missing_pages += 1

        after = paged_claims()
        snapshot_only = [
            claim.claim_id
            for claim in graph.facts
            if graph.trace(claim.claim_id).locatable
        ]
        if not after:
            return _result(
                spec,
                ok=True,
                observed=(
                    f"citation incomplete：{len(before)}/{len(before)} 条 fact 的 trace 不再落到任何 PDF 页"
                    f"（快照时点定位仍在，故 locatable 仍为 True 的 claim 有 {len(snapshot_only)} 条）"
                ),
                evidence={
                    "pages_removed": missing_pages,
                    "claims_with_page_before": before[:10],
                    "claims_with_page_after": after,
                    "snapshot_locator_still_present": len(snapshot_only),
                },
            )
        return _result(
            spec,
            ok=False,
            observed=f"页码移除后仍有 {len(after)} 条 fact 的 trace 落到页面，告警未触发",
        )
    finally:
        registry.close()


def scenario_unit_family_mismatch() -> Any:
    spec = SCENARIOS[2]
    registry = _registry()
    try:
        ids = _ingest_all(registry, (("annual_01_profit_continued.pdf", "annual"),))
        if not ids:
            return _result(spec, ok=False, observed="缺少夹具")
        document_id = next(iter(ids.values()))
        _normalization, _analysis, ledger, _graph = _chain(registry, document_id)

        records = ledger.records()
        if len(records) < 2:
            return _result(spec, ok=False, observed="证据记录不足 2 条，无法构造同指标单位冲突")

        # The injection: make one record of a repeated metric switch unit family
        # (万元 → 亿元 is such a pair: 万 vs 亿 are different families here).
        counts: Dict[str, int] = {}
        for record in records:
            counts[record.metric] = counts.get(record.metric, 0) + 1
        target = next((record for record in records if counts.get(record.metric, 0) > 1), None)
        if target is None:
            return _result(spec, ok=False, observed="没有重复出现的指标，无法构造单位冲突")
        mutated_unit = "亿元" if str(target.unit) != "亿元" else "万元"
        rebuilt = EvidenceLedger(snapshot_id=document_id)
        rebuilt._records = {
            record.evidence_id: (
                dataclasses.replace(record, unit=mutated_unit)
                if record.evidence_id == target.evidence_id
                else record
            )
            for record in records
        }

        verdict = FinancialValidator().validate(rebuilt, research_as_of="2026-12-31T00:00:00+08:00")
        mismatches = [item for item in verdict.errors if str(item).startswith("unit_mismatch")]
        if mismatches:
            return _result(
                spec,
                ok=True,
                observed=f"unit checker 报错：{mismatches[0]}",
                evidence={"metric": target.metric, "from": str(target.unit), "to": mutated_unit},
            )
        return _result(
            spec,
            ok=False,
            observed=f"单位改写为 {mutated_unit} 后未报 unit_mismatch（errors={list(verdict.errors)[:3]}）",
        )
    finally:
        registry.close()


def _run_corpus_draft(filename: str, check_type: str) -> Any:
    """Ingest one committed draft with its references and run the real checker."""
    from .checker.draft_corpus import DRAFT_SPECS, market_documents, market_normalized_facts
    from .checker.pipeline import check_draft

    spec = next((item for item in DRAFT_SPECS if item.filename == filename), None)
    if spec is None:
        raise LookupError(f"语料中没有 {filename}")
    registry = DocumentRegistry(
        root=Path(tempfile.mkdtemp(prefix="cn_inj_draft_")) / "documents",
        allow_roots=[DOCUMENTS, DRAFTS],
    )
    try:
        _ingest_all(registry, REFERENCE_FILINGS)
        draft_path = DRAFTS / filename
        if not draft_path.exists():
            raise FileNotFoundError(draft_path)
        parsed = registry.ingest(draft_path, declared_type="report_draft", source="admin_import")
        reference_ids = [
            document.document_id
            for document in registry.list()
            if document.filename in spec.references
        ]
        report = check_draft(
            parsed.document.document_id,
            registry=registry,
            reference_document_ids=reference_ids,
            as_of=spec.as_of,
            extra_facts=market_normalized_facts() if spec.uses_market_document else (),
            extra_documents=market_documents() if spec.uses_market_document else (),
        )
        findings = [item for item in report.get("findings", []) if item.get("check_type") == check_type]
        return report, findings
    finally:
        registry.close()


def scenario_draft_wrong_yoy() -> Any:
    spec = SCENARIOS[3]
    try:
        report, findings = _run_corpus_draft("draft_03_planted_figures.pdf", "calculation_error")
    except (LookupError, FileNotFoundError) as exc:
        return _result(spec, ok=False, observed=str(exc))
    if not findings:
        return _result(
            spec,
            ok=False,
            observed=f"未检出 calculation_error（实际类型：{sorted({item.get('check_type') for item in report.get('findings', [])})}）",
        )
    finding = findings[0]
    located = bool(finding.get("claim_id")) and bool(finding.get("expected"))
    if located:
        return _result(
            spec,
            ok=True,
            observed=f"定位到错误同比：原文「{finding.get('original')}」应为「{finding.get('expected')}」",
            evidence={
                "claim_id": finding.get("claim_id"),
                "claim_page": finding.get("claim_page"),
                "evidence_ids": list(finding.get("evidence_ids") or [])[:5],
                "severity": finding.get("severity"),
            },
        )
    return _result(spec, ok=False, observed="检出了 calculation_error 但未给出 claim_id/正确值")


def scenario_unsupported_causal() -> Any:
    spec = SCENARIOS[6]
    try:
        report, findings = _run_corpus_draft("draft_04_planted_citations.pdf", "unsupported_causal_claim")
    except (LookupError, FileNotFoundError) as exc:
        return _result(spec, ok=False, observed=str(exc))
    if not findings:
        return _result(spec, ok=False, observed="未检出 unsupported_causal_claim")
    finding = findings[0]
    return _result(
        spec,
        ok=True,
        observed=f"无证据因果断言被标记：{finding.get('suggestion') or finding.get('expected')}",
        evidence={
            "claim_id": finding.get("claim_id"),
            "original": finding.get("original"),
            "severity": finding.get("severity"),
            "evidence_ids": list(finding.get("evidence_ids") or [])[:5],
        },
    )


def scenario_evidence_deleted() -> Any:
    spec = SCENARIOS[5]
    registry = _registry()
    try:
        ids = _ingest_all(registry, (("annual_06_equity_shares.pdf", "annual"),))
        if not ids:
            return _result(spec, ok=False, observed="缺少夹具")
        document_id = next(iter(ids.values()))
        _normalization, _analysis, _ledger, graph = _chain(registry, document_id, run_id="injection-6")

        # Pick a fact whose evidence we can actually retract, and watch both it
        # and anything derived from it.
        candidates = [
            claim
            for claim in sorted(graph.facts, key=lambda item: item.claim_id)
            if claim.evidence_ids and claim.validation_status != "blocked"
        ]
        if not candidates:
            return _result(spec, ok=False, observed="没有可撤回证据的 fact，无法演示阻断")
        target = candidates[0]
        downstream = [
            claim.claim_id
            for claim in graph.claims.values()
            if target.claim_id in claim.derived_from
        ]

        retracted: List[str] = []
        for evidence_id in target.evidence_ids:
            deactivate_evidence(graph, evidence_id, "失败注入 #6：来源证据被撤回")
            retracted.append(evidence_id)
        report = revalidate(graph)
        blocked = list(report.blocked_claims)

        if target.claim_id in blocked:
            observed = f"撤回 {len(retracted)} 条证据后，{target.claim_id} 变 blocked"
            if downstream:
                hit = [item for item in downstream if item in blocked]
                observed += f"；下游 {len(hit)}/{len(downstream)} 条 inference 一并 blocked"
            return _result(
                spec,
                ok=True,
                observed=observed,
                evidence={
                    "evidence_ids": retracted,
                    "blocked_claim": target.claim_id,
                    "downstream_claims": downstream,
                    "blocked_claims": blocked[:10],
                    "audit_mode": "append_only_no_physical_delete",
                },
            )
        return _result(
            spec,
            ok=False,
            observed=f"撤回证据后 {target.claim_id} 未变 blocked（当前 {graph.claim(target.claim_id).validation_status}）",
        )
    finally:
        registry.close()


def scenario_stale_filing_correction() -> Any:
    """旧财报 + 更正公告：CARD-13 按优先级取更正值并留痕。

    Uses a real pair from the fixture corpus: 示例材料股份有限公司's annual report
    discloses 2024 revenue as 900,000 元, and a later 更正公告 restates it to
    1,000,000 元. The scenario passes only when the pipeline selects the corrected
    value *and* leaves a rule-attributed record behind — a pipeline that quietly
    kept the older number, or swapped it with no trace, must fail here.
    """
    spec = SCENARIOS[4]
    registry = _registry()
    try:
        ids = _ingest_all(
            registry,
            (
                ("annual_05_reclassification.pdf", "annual"),
                ("announcement_04_correction.pdf", "announcement"),
            ),
        )
        if len(ids) < 2:
            return _result(spec, ok=False, observed="缺少旧财报或更正公告夹具")

        filing_id = ids["annual_05_reclassification.pdf"]
        correction_id = ids["announcement_04_correction.pdf"]
        filing = registry.get(filing_id)
        correction_doc = registry.get(correction_id)
        if filing is None or correction_doc is None:
            return _result(spec, ok=False, observed="文档未能取回")

        normalization = normalize_financials(registry.get_facts(filing_id), filing)

        # The correction lives in prose, not in a table, so CARD-02 has nothing
        # to normalise; CARD-13 reads the 更正前/更正后 pair out of the sentence.
        # The 口径 is declared by the caller because the announcement does not
        # state one — an undeclared scope is never inferred here.
        pairs, pair_warnings = correction_pairs(
            registry.get_facts(correction_id),
            scope="合并",
            published_at=correction_doc.publication_date,
        )
        corrected = [pair for pair in pairs if pair.metric == "revenue" and pair.period_key == "2024FY"]
        if not corrected:
            return _result(
                spec,
                ok=False,
                observed="更正公告未解析出可裁决的更正对（未取到更正值）",
                evidence={"pair_warnings": pair_warnings, "pairs": [p.to_dict() for p in pairs]},
            )

        choices, records = resolve_conflicts(
            list(normalization.facts),
            source_class_by_document={
                filing_id: default_source_class("annual"),
                correction_id: default_source_class("announcement", is_correction=True),
            },
            extra_candidates=correction_candidates(pairs, source_id=correction_id),
            # The announcement does not name its company, so the grouping cannot
            # be verified from the documents; the resolver says so in a warning
            # rather than pretending the link was checked.
            occurred_at="failure-injection-5",
        )
        choice = conflict_choices_for_metric(choices, "revenue", "2024FY")
        selected = choice.value if choice is not None else None
        supersession = [
            record
            for record in records
            if record.change_kind == CHANGE_VERSION_SUPERSESSION
            and record.metric == "revenue"
            and record.period_key == "2024FY"
        ]

        if selected is None:
            return _result(
                spec,
                ok=False,
                observed="2024 年营业收入未能裁决出唯一值",
                evidence=summarize_conflicts(records),
            )
        if not supersession:
            return _result(
                spec,
                ok=False,
                observed="采用了数值但未留下 VERSION_SUPERSESSION 记录（静默覆盖）",
                evidence=summarize_conflicts(records),
            )

        record = supersession[0]
        expected = corrected[0].after_value
        if abs(float(selected) - expected) > 1e-6:
            return _result(
                spec,
                ok=False,
                observed=f"采用了 {selected}，不是更正后值 {expected}",
                evidence=summarize_conflicts(records),
            )
        if record.selected_source != correction_id or record.status != STATUS_RESOLVED:
            return _result(
                spec,
                ok=False,
                observed="记录未指向更正公告作为选定来源",
                evidence={"record": record.to_dict()},
            )

        return _result(
            spec,
            ok=True,
            observed=(
                f"2024 年营业收入 {corrected[0].before_raw} → {corrected[0].after_raw}"
                f"（{record.reason}），报告采用更正后值，"
                f"旧值保留为 superseded 来源 {record.superseded_source[:12]}"
            ),
            evidence={
                "conflict": record.to_dict(),
                "summary": summarize_conflicts(records),
                "pair": corrected[0].to_dict(),
                "pair_warnings": pair_warnings,
                "rule_id": record.reason,
                "rule_expected": RULE_REGULATORY_CORRECTION,
            },
        )
    finally:
        registry.close()


_RUNNERS: Dict[int, Callable[[], Any]] = {
    1: scenario_missing_total_shares,
    2: scenario_page_removed,
    3: scenario_unit_family_mismatch,
    4: scenario_draft_wrong_yoy,
    5: scenario_stale_filing_correction,
    6: scenario_evidence_deleted,
    7: scenario_unsupported_causal,
}


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def run_scenario(scenario_id: int) -> Any:
    """Run one scenario by its card number (1–7)."""
    runner = _RUNNERS.get(int(scenario_id))
    if runner is None:
        raise KeyError(f"unknown failure-injection scenario: {scenario_id!r}")
    return runner()


def run_all(*, only: Optional[Sequence[int]] = None) -> List[Any]:
    selected = [int(item) for item in only] if only else [spec.scenario_id for spec in SCENARIOS]
    return [run_scenario(item) for item in selected]


def render_injection_markdown(results: Sequence[Any]) -> str:
    lines: List[str] = ["# 失败注入演示（7 场景）", ""]
    passed = sum(1 for item in results if item.ok)
    lines.append(f"- 通过：{passed}/{len(results)}")
    lines.append("")
    lines.append("| # | 注入 | 预期系统行为 | 实测 | 判定 |")
    lines.append("|---|---|---|---|---|")
    for item in results:
        lines.append(
            f"| {item.scenario_id} | {item.injection} | {item.expected_behavior} | "
            f"{item.observed} | {'✅' if item.ok else '❌'} |"
        )
    deferred = [item for item in results if item.evidence.get("status") == "deferred"]
    if deferred:
        lines.append("")
        lines.append("## 未实现（如实列出，不掩盖）")
        lines.append("")
        for item in deferred:
            lines.append(f"- #{item.scenario_id} {item.title}：{item.evidence.get('deferred_to')}")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行 CARD-12 失败注入演示")
    parser.add_argument("--scenario", type=int, default=0, help="只跑指定场景（1–7）；缺省跑全部")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    results = run_all(only=[args.scenario] if args.scenario else None)
    if args.json:
        print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))
    else:
        print(render_injection_markdown(results))
    return 0 if all(item.ok for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
