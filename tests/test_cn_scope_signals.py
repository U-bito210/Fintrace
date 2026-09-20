"""Offline tests for CARD-04 会计口径与重述检测.

The card's §11 cases are the spine:

* one closed-form example per signal type (six of them), each bound to a located
  fact;
* the three decision paths — ``block_yoy`` / ``use_adjusted`` / ``warning`` —
  including the branch that distinguishes them (a restatement with adjusted data
  is usable; the same restatement without it must block);
* the §10 validator rules, each with a case that *must* fail and a case that
  must pass;
* the §12 integration chain CARD-01 → CARD-02 → CARD-04 → CARD-03: a filing with
  a restated comparative produces its YoY from the adjusted column, a filing with
  an unclassifiable 口径 disclosure produces no percentage at all.

Two invariants get their own tests because they are easy to break silently and
expensive to notice:

* **no rule fires outside the table** — a near-miss (pattern matches, context
  does not) must produce *no* signal rather than a weaker one;
* **silence is not cleanliness** — a document with no signal reports
  ``comparability is None`` and an explicit coverage statement, never "ok".

Everything is offline: synthetic fixtures, hand-built facts, no network, no LLM.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import List, Optional

import pytest

from src.cn.analysis import (
    GROSS_MARGIN,
    REVENUE_YOY,
    AccountingScopeSignal,
    CalculationRegistry,
    ComparabilityDecision,
    ScopeSignalType,
    SignalSeverity,
    SURFACE_CHAPTER,
    SURFACE_PARAGRAPH,
    SURFACE_TABLE_COLUMN,
    SURFACE_TITLE,
    aggregate_comparability,
    analyze_document,
    analyze_financials,
    build_scope_report,
    comparability_decision,
    detect_scope_signals,
    detection_coverage,
)
from src.cn.analysis.series import MERGED_VERSION, MetricSeries, PeriodValue, merge_versions
from src.cn.documents.models import (
    FACT_KIND_TABLE_CELL,
    FACT_KIND_TEXT,
    DocumentRecord,
    ExtractedFact,
)
from src.cn.documents.registry import DocumentRegistry
from src.cn.normalization import (
    NormalizedFact,
    PeriodKind,
    Scope,
    Version,
    normalize_financials,
)
from src.validation import validate_comparability

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
STAMP = "2026-09-17T00:00:00+08:00"

ANNUAL_RESTATED = "annual_04_restated_comparative.pdf"   # 已重述比较列 + 调整前/调整后双列表
ANNUAL_RECLASS = "annual_05_reclassification.pdf"        # 只有一条无法分类的口径披露
ANNUAL_PLAIN = "annual_01_profit_continued.pdf"          # 干净的年报续页
CORRECTION = "announcement_04_correction.pdf"            # 更正公告（无报表结构）
CLEAN_FILINGS = (
    "annual_02_balance_unit_wan.pdf",
    "annual_03_cashflow_with_scan.pdf",
    "announcement_01_meeting.pdf",
    "announcement_02_forecast_with_url.pdf",
    "announcement_03_injection.pdf",
    "report_draft_01_summary.pdf",
    "report_draft_02_valuation.pdf",
    "report_draft_03_industry.pdf",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _document(document_id: str = "DOC-1", company: str = "示例股份") -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        sha256="0" * 64,
        filename=f"{document_id}.pdf",
        source="admin_import",
        document_type="annual",
        company=company,
        report_period="2025FY",
    )


def _text(
    text: str,
    *,
    fact_id: str,
    page: int = 1,
    section: str = "正文",
    document_id: str = "DOC-1",
) -> ExtractedFact:
    """One CARD-01 body/note text fact (``raw_value`` is always ``None``)."""
    return ExtractedFact(
        document_id=document_id,
        fact_id=fact_id,
        raw_text=text,
        page=page,
        section=section,
        row=None,
        column=None,
        raw_value=None,
        kind=FACT_KIND_TEXT,
    )


def _cell(
    text: str,
    *,
    fact_id: str,
    version_label: Optional[str] = None,
    table: str = "合并利润表",
    column: int = 1,
    value: Optional[str] = None,
    page: int = 1,
    document_id: str = "DOC-1",
) -> ExtractedFact:
    """One CARD-01 table cell, optionally carrying a version column label."""
    meta = {"version_label": version_label} if version_label else {}
    return ExtractedFact(
        document_id=document_id,
        fact_id=fact_id,
        raw_text=text,
        page=page,
        section=table,
        table=table,
        row=0 if value is None else 1,
        column=column,
        raw_value=value,
        kind=FACT_KIND_TABLE_CELL,
        meta=meta,
    )


def _signals(facts, document_id: str = "DOC-1") -> List[AccountingScopeSignal]:
    return detect_scope_signals(facts, _document(document_id), detected_at=STAMP)


def _only(facts, signal_type: ScopeSignalType, document_id: str = "DOC-1") -> AccountingScopeSignal:
    found = [item for item in _signals(facts, document_id) if item.signal_type is signal_type]
    assert len(found) == 1, f"期望恰好一条 {signal_type.value}，实际 {[item.signal_type.value for item in _signals(facts, document_id)]}"
    return found[0]


def _normalized(
    metric: str,
    value: float,
    period_key: str,
    *,
    version: Version = Version.AS_REPORTED,
    kind: PeriodKind = PeriodKind.YTD_CUMULATIVE,
    document_id: str = "DOC-1",
) -> NormalizedFact:
    return NormalizedFact(
        normalized_fact_id=f"nf_{document_id}_{metric}_{period_key}_{version.value}",
        metric=metric,
        value=float(value),
        currency="CNY",
        period_key=period_key,
        period_kind=kind,
        scope=Scope.CONSOLIDATED,
        version=version,
        available_at="2026-04-25T00:00:00+08:00",
        source_fact_id=f"fact_{metric}_{period_key}",
        document_id=document_id,
        page=1,
        row=1,
        column=1,
        raw_value=str(value),
        source_unit="元",
        source_table="合并利润表",
        statement_type="income",
    )


@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    """A throwaway registry that may read (never write) the bundled fixtures."""
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def _ingest(registry: DocumentRegistry, name: str, declared: str = "annual"):
    return registry.ingest(FIXTURES / name, declared_type=declared, source="admin_import")


# --------------------------------------------------------------------------- #
# §5 信号类型 —— 六类各有封闭算例
# --------------------------------------------------------------------------- #

def test_prior_period_error_correction_heading():
    fact = _text("前期会计差错更正", fact_id="fact_1", section="六、前期差错更正")
    signal = _only([fact], ScopeSignalType.PRIOR_PERIOD_ERROR_CORRECTION)

    assert signal.detector_rule == "zh_prior_period_error_correction"
    assert signal.evidence_ids == ("fact_1",)
    assert signal.surfaces == (SURFACE_CHAPTER,)
    assert signal.severity is SignalSeverity.UNDETERMINED


def test_prior_period_error_correction_stated_amount_and_period_are_read_verbatim():
    fact = _text(
        "本公司对2024年度营业收入确认时点进行了更正，更正后金额为1,000,000元。",
        fact_id="fact_2",
    )
    signal = _only([fact], ScopeSignalType.PRIOR_PERIOD_ERROR_CORRECTION)

    assert signal.detector_rule == "zh_correction_disclosure"
    assert signal.periods_affected == ("2024FY",)
    # A figure in the sentence means the note asserts a magnitude we did not have
    # to guess — that is what "material" means here (§8 edge case 3).
    assert signal.severity is SignalSeverity.MATERIAL


def test_immaterial_assertion_is_recorded_not_filtered():
    """§8 edge case 3: a disclosed change stays a signal even when immaterial."""
    facts = [
        _text("前期差错更正", fact_id="fact_3"),
        _text("本次前期差错更正对本公司无重大影响。", fact_id="fact_4"),
    ]
    signal = _only(facts, ScopeSignalType.PRIOR_PERIOD_ERROR_CORRECTION)

    assert signal.severity is SignalSeverity.IMMATERIAL
    # The signal is not withdrawn: materiality is reported, never used to suppress.
    assert set(signal.evidence_ids) == {"fact_3", "fact_4"}


def test_accounting_policy_change_from_a_new_standard():
    fact = _text("本公司自2026年1月1日起执行新租赁准则。", fact_id="fact_5")
    signal = _only([fact], ScopeSignalType.ACCOUNTING_POLICY_CHANGE)

    assert signal.detector_rule == "zh_accounting_policy_change"
    assert signal.severity is SignalSeverity.MATERIAL


def test_accounting_estimate_change_shares_the_policy_signal_type():
    """会计估计变更 is a 口径 change; the signal type is shared, the rule is not."""
    fact = _text("本期发生会计估计变更，应收款项坏账准备比例调整。", fact_id="fact_6")
    signal = _only([fact], ScopeSignalType.ACCOUNTING_POLICY_CHANGE)

    assert signal.detector_rule == "zh_accounting_estimate_change"


def test_consolidation_scope_change():
    fact = _text("报告期内本公司合并范围发生变化，新增纳入合并范围的子公司一家。", fact_id="fact_7")
    signal = _only([fact], ScopeSignalType.CONSOLIDATION_SCOPE_CHANGE)

    assert signal.detector_rule == "zh_consolidation_scope_change"
    assert signal.severity is SignalSeverity.UNDETERMINED


def test_comparable_period_adjusted_names_the_comparison():
    fact = _text("按准则要求，本公司对可比期间数据进行了追溯调整。", fact_id="fact_8")
    signal = _only([fact], ScopeSignalType.COMPARABLE_PERIOD_ADJUSTED)

    # The more specific rule must win over bare 追溯调整: naming the affected
    # comparable period is strictly more informative than "something changed".
    assert signal.detector_rule == "zh_comparable_period_adjusted"


def test_traceable_restatement_without_a_named_period():
    fact = _text("对以前年度数据进行追溯重述。", fact_id="fact_9")
    signal = _only([fact], ScopeSignalType.RESTATED)

    assert signal.detector_rule == "zh_traceable_restatement"
    assert signal.periods_affected == ()


def test_restatement_period_is_read_from_the_text():
    fact = _text("本公司追溯重述2024年度财务数据。", fact_id="fact_10")
    signal = _only([fact], ScopeSignalType.RESTATED)

    assert signal.periods_affected == ("2024FY",)


def test_unclassified_reclassification_becomes_unknown():
    """§7.3: outside the table → unknown + review, never a silent pass."""
    fact = _text("本公司对部分报表项目进行了重新分类列报。", fact_id="fact_11")
    signal = _only([fact], ScopeSignalType.UNKNOWN)

    assert signal.detector_rule == "zh_reclassification_unclassified"


def test_a_near_miss_does_not_fall_through_to_a_weaker_rule():
    """『重分类』 without a statement-project context is not evidence of anything."""
    facts = [_text("重分类调整", fact_id="fact_12")]
    assert _signals(facts) == []


def test_plain_prose_produces_no_signal():
    facts = [
        _text("公司主营业务保持稳定，产能利用率处于合理区间。", fact_id="fact_13"),
        _text("已定位正文与附注段落 6 行（含页内章节归属）。", fact_id="fact_14"),
    ]
    assert _signals(facts) == []


# --------------------------------------------------------------------------- #
# §5 表格版本列 —— 双列重述表
# --------------------------------------------------------------------------- #

def test_two_version_columns_become_one_adjusted_signal():
    facts = [
        _cell("调整前金额", fact_id="fact_h1", version_label="as_reported", column=1, value=None),
        _cell("调整后金额", fact_id="fact_h2", version_label="restated", column=2, value=None),
        _cell("1,000,000", fact_id="fact_v1", version_label="as_reported", column=1, value="1,000,000"),
        _cell("1,000,000", fact_id="fact_v2", version_label="restated", column=2, value="1,000,000"),
    ]
    signal = _only(facts, ScopeSignalType.COMPARABLE_PERIOD_ADJUSTED)

    assert signal.detector_rule == "table_version_columns"
    assert signal.surfaces == (SURFACE_TABLE_COLUMN,)
    # The header cells are the evidence a reader jumps to, so they win the slot.
    assert "fact_h2" in signal.evidence_ids


def test_an_as_reported_only_table_is_not_a_signal():
    facts = [
        _cell("本期金额", fact_id="fact_h3", column=1, value=None),
        _cell("1,000,000", fact_id="fact_v3", column=1, value="1,000,000"),
    ]
    assert _signals(facts) == []


# --------------------------------------------------------------------------- #
# §6 决策表 —— 三条路径
# --------------------------------------------------------------------------- #

def test_restated_with_adjusted_data_uses_the_adjusted_figure():
    facts = [
        _text("对以前年度数据进行追溯重述。", fact_id="fact_20"),
        _text("更正后2024年度营业收入为1,000,000元。", fact_id="fact_21"),
    ]
    signal = _only(facts, ScopeSignalType.RESTATED)

    assert signal.adjusted_data_available is True
    assert signal.adjusted_evidence_ids == ("fact_21",)
    assert comparability_decision(signal) is ComparabilityDecision.USE_ADJUSTED


def test_restated_without_adjusted_data_blocks():
    """§8 edge case 4: detected, but no restated figure to compare against."""
    signal = _only([_text("对以前年度数据进行追溯重述。", fact_id="fact_22")], ScopeSignalType.RESTATED)

    assert signal.adjusted_data_available is False
    assert comparability_decision(signal) is ComparabilityDecision.BLOCK_YOY


def test_a_plain_figure_is_never_treated_as_adjusted():
    """Otherwise every filing would be comparable and the block path would vanish."""
    facts = [
        _text("对以前年度数据进行追溯重述。", fact_id="fact_23"),
        _text("2025年度营业收入为1,200,000元。", fact_id="fact_24"),
    ]
    signal = _only(facts, ScopeSignalType.RESTATED)

    assert signal.adjusted_evidence_ids == ()
    assert comparability_decision(signal) is ComparabilityDecision.BLOCK_YOY


def test_policy_change_is_usable_only_when_applied_retrospectively():
    without = _only(
        [_text("本公司自2026年1月1日起执行新租赁准则。", fact_id="fact_25")],
        ScopeSignalType.ACCOUNTING_POLICY_CHANGE,
    )
    with_adjusted = _only(
        [
            _text("本公司自2026年1月1日起执行新租赁准则。", fact_id="fact_26"),
            _cell("调整后金额", fact_id="fact_h26", version_label="restated", column=2, value=None),
        ],
        ScopeSignalType.ACCOUNTING_POLICY_CHANGE,
    )

    assert comparability_decision(without) is ComparabilityDecision.WARNING
    assert comparability_decision(with_adjusted) is ComparabilityDecision.USE_ADJUSTED


def test_a_restated_comparative_is_always_usable():
    """The restated comparative is already in the figures — nothing to switch to."""
    facts = [
        _text("按准则要求，本公司对可比期间数据进行了追溯调整。", fact_id="fact_27"),
    ]
    signal = _only(facts, ScopeSignalType.COMPARABLE_PERIOD_ADJUSTED)

    assert comparability_decision(signal) is ComparabilityDecision.USE_ADJUSTED


def test_consolidation_scope_change_warns_but_does_not_block():
    signal = _only(
        [_text("报告期内本公司合并范围发生变化，新增纳入合并范围的子公司一家。", fact_id="fact_28")],
        ScopeSignalType.CONSOLIDATION_SCOPE_CHANGE,
    )
    assert comparability_decision(signal) is ComparabilityDecision.WARNING


def test_unknown_blocks():
    """§9 fails closed."""
    signal = _only(
        [_text("本公司对部分报表项目进行了重新分类列报。", fact_id="fact_29")],
        ScopeSignalType.UNKNOWN,
    )
    assert comparability_decision(signal) is ComparabilityDecision.BLOCK_YOY


def test_aggregation_takes_the_strictest_decision():
    from src.cn.analysis.scope_signals import AccountingScopeSignal as Signal

    def _signal(kind: ScopeSignalType, adjusted: bool) -> Signal:
        return Signal(
            signal_type=kind,
            company="示例股份",
            periods_affected=(),
            evidence_ids=("fact_x",),
            detected_at=STAMP,
            detector_rule="test",
            adjusted_evidence_ids=("fact_y",) if adjusted else (),
        )

    warning = _signal(ScopeSignalType.CONSOLIDATION_SCOPE_CHANGE, False)
    adjusted = _signal(ScopeSignalType.RESTATED, True)
    blocked = _signal(ScopeSignalType.UNKNOWN, False)

    assert aggregate_comparability([]) is None
    assert aggregate_comparability([warning]) is ComparabilityDecision.WARNING
    assert aggregate_comparability([warning, adjusted]) is ComparabilityDecision.USE_ADJUSTED
    assert aggregate_comparability([adjusted, blocked]) is ComparabilityDecision.BLOCK_YOY
    # Order of the input must not matter.
    assert aggregate_comparability([blocked, warning]) is ComparabilityDecision.BLOCK_YOY


def test_an_unmapped_signal_type_never_passes_as_comparable():
    """The decision function defaults to blocking, not to "fine"."""
    from types import SimpleNamespace

    alien = SimpleNamespace(signal_type="something_new", adjusted_data_available=True)
    assert comparability_decision(alien) is ComparabilityDecision.BLOCK_YOY


# --------------------------------------------------------------------------- #
# §5/§9 证据绑定、覆盖范围与确定性
# --------------------------------------------------------------------------- #

def test_every_signal_binds_resolvable_evidence(registry):
    parsed = _ingest(registry, ANNUAL_RESTATED)
    known = {fact.fact_id for fact in parsed.facts}
    signals = detect_scope_signals(parsed.facts, parsed.document, detected_at=STAMP)

    assert signals
    for signal in signals:
        assert signal.evidence_ids, f"{signal.signal_type.value} 未绑定证据"
        assert set(signal.evidence_ids) <= known
        assert signal.company == parsed.document.company
        assert signal.detected_at == STAMP
        assert signal.detector_rule


def test_detection_is_a_pure_function_of_the_facts():
    facts = [
        _text("前期会计差错更正", fact_id="fact_30"),
        _text("更正后2024年度营业收入为1,000,000元。", fact_id="fact_31"),
    ]
    first = [item.to_dict() for item in _signals(facts)]
    second = [item.to_dict() for item in _signals(facts)]
    assert first == second


def test_detection_module_does_not_reach_for_a_model_or_the_network():
    """§7.3: the model does not participate in signal determination."""
    import src.cn.analysis.scope_signals as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    forbidden = ("src.llms", "requests", "httpx", "urllib", "openai", "anthropic")
    hits = [name for name in forbidden if re.search(rf"^\s*(from|import)\s+.*{re.escape(name)}", source, re.M)]
    assert hits == [], f"检测模块引入了非确定性依赖：{hits}"


def test_coverage_statement_names_the_rules_and_the_limits():
    """§9: publishing no signal must be accompanied by what was looked for."""
    coverage = detection_coverage()

    assert coverage["rule_count"] == len(coverage["rules"]) >= 8
    assert "table_version_columns" in coverage["extra_rules"]
    assert set(coverage["surfaces"]) == {
        SURFACE_TITLE, SURFACE_CHAPTER, SURFACE_PARAGRAPH, SURFACE_TABLE_COLUMN,
    }
    joined = " ".join(coverage["limits"])
    assert "不等于不存在口径变化" in joined
    assert "未在真实披露语料上拟合" in joined
    # The body-text chapter attribution is per page by design, and the coverage
    # statement has to say so — a cross-page inheritance would be a guess.
    assert "跨页续写不继承标题" in joined


def test_scope_report_states_status_and_blocked_documents():
    facts = [
        _text("本公司对部分报表项目进行了重新分类列报。", fact_id="fact_32"),
    ]
    with_signal = build_scope_report(facts, _document(), detected_at=STAMP)
    clean = build_scope_report([_text("公司经营正常。", fact_id="fact_33")], _document(), detected_at=STAMP)

    assert with_signal["status"] == "ok"
    assert with_signal["comparability"][0]["decision"] == "block_yoy"
    assert with_signal["blocked_documents"] == ["DOC-1"]
    assert with_signal["comparability"][0]["signal_types"] == ["unknown"]

    assert clean["status"] == "ok"
    assert clean["signal_count"] == 0
    assert clean["comparability"] == [] and clean["blocked_documents"] == []


def test_signal_labels_cover_every_signal_type():
    from src.cn.analysis import COMPARABILITY_LABELS, SIGNAL_TYPE_LABELS

    assert {item.value for item in ScopeSignalType} == set(SIGNAL_TYPE_LABELS)
    assert {item.value for item in ComparabilityDecision} == set(COMPARABILITY_LABELS)


# --------------------------------------------------------------------------- #
# §12 merge_versions —— use_adjusted 如何真正改变取数
# --------------------------------------------------------------------------- #

def _series(values) -> dict:
    built: dict = {}
    for metric, scope, version, period, value in values:
        key = (metric, scope, version)
        found = built.get(key)
        if found is None:
            found = MetricSeries(metric=metric, scope=scope, version=version, currency="CNY")
            built[key] = found
        found.put(PeriodValue(period_key=period, basis="FY", value=value, version=version))
    return built


def test_the_default_series_keeps_versions_apart():
    """CARD-02 §7.3 rule 4 — two versions of one figure must never be paired."""
    series = _series(
        [
            ("revenue", "合并", "AS_REPORTED", "2024FY", 900_000.0),
            ("revenue", "合并", "RESTATED", "2024FY", 1_000_000.0),
        ]
    )
    assert set(series) == {
        ("revenue", "合并", "AS_REPORTED"),
        ("revenue", "合并", "RESTATED"),
    }


def test_merge_versions_takes_the_newest_disclosure_per_period():
    series = _series(
        [
            ("revenue", "合并", "AS_REPORTED", "2025FY", 1_100_000.0),
            ("revenue", "合并", "AS_REPORTED", "2024FY", 900_000.0),
            ("revenue", "合并", "RESTATED", "2024FY", 1_000_000.0),
        ]
    )
    warnings: List[str] = []
    merged = merge_versions(series, warnings=warnings)

    assert set(merged) == {("revenue", "合并", MERGED_VERSION)}
    item = merged[("revenue", "合并", MERGED_VERSION)]
    assert item.get("FY", "2024FY").value == 1_000_000.0    # newest wins
    assert item.get("FY", "2024FY").version == "RESTATED"
    assert item.get("FY", "2025FY").value == 1_100_000.0
    assert item.version == "RESTATED"
    assert any("同期同指标存在两个披露版本" in text for text in warnings)


def test_merge_versions_never_averages_two_versions():
    series = _series(
        [
            ("revenue", "合并", "AS_REPORTED", "2024FY", 900_000.0),
            ("revenue", "合并", "RESTATED", "2024FY", 1_000_000.0),
        ]
    )
    merged = merge_versions(series)
    value = merged[("revenue", "合并", MERGED_VERSION)].get("FY", "2024FY").value
    assert value in (900_000.0, 1_000_000.0)
    assert value != 950_000.0


def test_merge_versions_keeps_scopes_apart():
    series = _series(
        [
            ("revenue", "合并", "RESTATED", "2025FY", 10.0),
            ("revenue", "母公司", "RESTATED", "2025FY", 4.0),
        ]
    )
    merged = merge_versions(series)
    assert set(merged) == {
        ("revenue", "合并", MERGED_VERSION),
        ("revenue", "母公司", MERGED_VERSION),
    }


def test_diagnostics_still_find_metrics_that_were_never_restated():
    """A merged view must not filter a series by the *document's* newest version.

    Only the comparative is restated in practice — 应收账款 is not. If the group
    selection summary ("newest version in the document") were used to match series,
    a metric disclosed only as-reported would silently vanish from diagnostics
    while still appearing in the metric table: the two rows would disagree about
    what the filing contains.
    """
    from src.cn.analysis import SIGNAL_RECEIVABLE_RISK

    facts = [
        _normalized("revenue", 1_200.0, "2025FY"),
        _normalized("revenue", 1_000.0, "2024FY", version=Version.RESTATED),
        _normalized("receivables", 300.0, "2025FY", kind=PeriodKind.POINT_IN_TIME),
        _normalized("receivables", 200.0, "2024FY", kind=PeriodKind.POINT_IN_TIME),
    ]
    signal = AccountingScopeSignal(
        signal_type=ScopeSignalType.RESTATED,
        company="示例股份",
        periods_affected=("2024FY",),
        evidence_ids=("fact_x",),
        detected_at=STAMP,
        detector_rule="test",
        adjusted_evidence_ids=("fact_y",),
    )
    result = analyze_financials(facts, scope_signals=[signal], report_period="2025FY")

    assert result.comparability == "use_adjusted"
    assert result.metric(REVENUE_YOY).delta_pct == pytest.approx(20.0)
    # 收入 +20% 而应收 +50% → 增速差 30pp ≥ 阈值 10pp
    assert SIGNAL_RECEIVABLE_RISK in result.signal_ids()


# --------------------------------------------------------------------------- #
# §12 集成：CARD-01 → CARD-02 → CARD-04 → CARD-03
# --------------------------------------------------------------------------- #

def test_restated_filing_takes_its_yoy_from_the_adjusted_column(registry):
    """The whole point of ``use_adjusted``: the percentage must change."""
    parsed = _ingest(registry, ANNUAL_RESTATED)
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization, documents=parsed.document)

    assert result.comparability == "use_adjusted"
    assert result.comparability_blocked is False

    yoy = result.metric(REVENUE_YOY)
    assert yoy is not None
    # 本期 1,100,000 (AS_REPORTED) vs 上期（已重述）1,000,000 → +10%.
    # Against the superseded 900,000 it would have read +22.22%.
    assert yoy.value == pytest.approx(1_100_000.0)
    assert yoy.delta_pct == pytest.approx(10.0)
    assert yoy.version == "RESTATED"

    result_versions = {
        fact.normalized_fact_id: fact.version.value for fact in normalization.facts
    }
    current_id, previous_id = yoy.inputs
    assert result_versions[current_id] == "AS_REPORTED"
    assert result_versions[previous_id] == "RESTATED"

    assert result.metric(GROSS_MARGIN).value == pytest.approx(400_000.0 / 1_100_000.0 * 100.0)
    assert any("use_adjusted" in text or "调整后" in text for text in result.warnings)


def test_unclassifiable_disclosure_blocks_every_percentage(registry):
    """§9: 宁可阻断也不误算. The level survives; no percentage does."""
    parsed = _ingest(registry, ANNUAL_RECLASS)
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization, documents=parsed.document)

    assert result.comparability == "block_yoy"
    assert result.comparability_blocked is True

    # Without the block this filing would report 1,000,000 / 900,000 → +11.11%.
    assert not any(item.delta_pct is not None for item in result.metrics), [
        (item.metric, item.delta_pct) for item in result.metrics if item.delta_pct is not None
    ]
    level = result.metric(REVENUE_YOY)
    assert level.value == pytest.approx(1_000_000.0)
    assert level.comparison is not None and level.comparison.value == "level"
    assert level.calculation_id is None
    assert "comparability_blocked" in level.warnings
    assert "阻断" in (level.note or "")


def test_a_blocked_comparison_produces_no_calculation_record(registry):
    parsed = _ingest(registry, ANNUAL_RECLASS)
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization, documents=parsed.document)

    blocked = [item for item in result.metrics if "comparability_blocked" in item.warnings]
    assert blocked
    # No percentage, no absolute change and — the part that matters — no
    # calculation record at all: there is no formula whose operands would be
    # comparable, so nothing may be registered as if there were.
    assert all(item.calculation_id is None for item in blocked)
    assert all(item.delta_pct is None and item.delta_abs is None for item in blocked)
    assert not [row for row in result.registry.records() if row.metric.endswith("_yoy")]


def test_clean_filings_produce_no_signal_and_no_false_block(registry):
    parsed = _ingest(registry, ANNUAL_PLAIN)
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization, documents=parsed.document)

    assert result.scope_signals == []
    assert result.comparability is None
    assert result.comparability_blocked is False
    # A clean filing is analysed exactly as before CARD-04 existed.
    assert result.metric(REVENUE_YOY) is None  # annual_01 has no comparative column
    assert "缺少上一可比期间" in " ".join(item.reason for item in result.missing)


@pytest.mark.parametrize("filename", CLEAN_FILINGS)
def test_no_signal_is_detected_in_the_clean_corpus(registry, filename):
    declared = "announcement" if filename.startswith("announcement") else (
        "report_draft" if filename.startswith("report_draft") else "annual"
    )
    parsed = _ingest(registry, filename, declared)
    signals = detect_scope_signals(parsed.facts, parsed.document, detected_at=STAMP)
    assert signals == [], [(item.signal_type.value, item.detector_rule, item.detail) for item in signals]


def test_a_correction_announcement_is_detected_even_without_a_statement(registry):
    """A correction announcement declares no report period, so it never normalises."""
    parsed = _ingest(registry, CORRECTION, "announcement")
    signals = detect_scope_signals(parsed.facts, parsed.document, detected_at=STAMP)

    kinds = {item.signal_type for item in signals}
    assert ScopeSignalType.PRIOR_PERIOD_ERROR_CORRECTION in kinds
    assert ScopeSignalType.RESTATED in kinds
    assert aggregate_comparability(signals) is ComparabilityDecision.USE_ADJUSTED
    # Every signal still resolves to a page, which is the only reason this is
    # usable at all: the value is superseded in a *different* document.
    assert all(item.evidence_ids for item in signals)


def test_the_parser_note_does_not_stamp_a_clean_filing_as_restated(registry):
    """A regression guard with a real history.

    ``DocumentRecord.notes`` is scanned by CARD-02 to label the *document's*
    disclosure version. A parser bookkeeping sentence containing the word 重述
    (the first wording of the body-text note did) therefore stamped every fact of
    an otherwise clean filing RESTATED — the filing's own text never said so.
    """
    parsed = _ingest(registry, ANNUAL_PLAIN)
    assert parsed.document.notes, "正文/附注定位结果应当被记录，而不是静默"
    for note in parsed.document.notes:
        assert "重述" not in note and "更正" not in note

    normalization = normalize_financials(parsed.facts, parsed.document)
    versions = {fact.version for fact in normalization.facts}
    assert versions == {Version.AS_REPORTED}


# --------------------------------------------------------------------------- #
# §10 Validator
# --------------------------------------------------------------------------- #

class _Row(dict):
    """Duck-typed metric row for the validator's attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc


class _Result:
    def __init__(self, metrics, comparability):
        self.metrics = [_Row(row) for row in metrics]
        self.comparability = comparability
        self.comparability_blocked = comparability == "block_yoy"
        self.scope_signals = []


def _yoy_row(metric="revenue_yoy", *, delta_pct=11.11, inputs=("nf_a", "nf_b")):
    return {
        "metric": metric,
        "period_key": "2025FY",
        "comparison": "yoy",
        "delta_pct": delta_pct,
        "inputs": list(inputs),
    }


def test_validator_rejects_a_percentage_under_block_yoy():
    verdict = validate_comparability(_Result([_yoy_row()], "block_yoy"))
    assert verdict.valid is False
    assert any("block_yoy 期间出现百分比同比" in item for item in verdict.errors)


def test_validator_passes_when_block_yoy_carries_no_percentage():
    verdict = validate_comparability(_Result([_yoy_row(delta_pct=None)], "block_yoy"))
    assert verdict.valid is True and verdict.errors == []


def test_validator_skips_rules_whose_inputs_were_not_supplied():
    """A skipped rule is reported, so ``valid`` never overstates coverage."""
    verdict = validate_comparability(_Result([_yoy_row()], "use_adjusted"))
    assert verdict.valid is True
    assert any("normalized_facts" in item for item in verdict.warnings)


def test_validator_rejects_a_signal_without_resolvable_evidence():
    facts = [_text("本公司对部分报表项目进行了重新分类列报。", fact_id="fact_40")]
    signals = _signals(facts)
    verdict = validate_comparability(
        _Result([], "block_yoy"), known_fact_ids={"fact_other"}, signals=signals
    )
    assert verdict.valid is False
    assert any("无法解析到文档页" in item for item in verdict.errors)


def test_validator_rejects_a_signal_with_no_evidence_at_all():
    bare = AccountingScopeSignal(
        signal_type=ScopeSignalType.UNKNOWN,
        company="示例股份",
        periods_affected=(),
        evidence_ids=(),
        detected_at=STAMP,
        detector_rule="test",
    )
    verdict = validate_comparability(
        _Result([], "block_yoy"), known_fact_ids=set(), signals=[bare]
    )
    assert verdict.valid is False
    assert any("未绑定任何证据" in item for item in verdict.errors)


def test_validator_rejects_use_adjusted_against_an_as_reported_comparative():
    facts = [
        _normalized("revenue", 1_100_000.0, "2025FY"),
        _normalized("revenue", 900_000.0, "2024FY"),  # the superseded figure
    ]
    row = _yoy_row(inputs=(facts[0].normalized_fact_id, facts[1].normalized_fact_id))
    verdict = validate_comparability(
        _Result([row], "use_adjusted"), normalized_facts=facts
    )
    assert verdict.valid is False
    assert any("AS_REPORTED 的比较值" in item for item in verdict.errors)


def test_validator_accepts_use_adjusted_against_a_restated_comparative():
    facts = [
        _normalized("revenue", 1_100_000.0, "2025FY"),
        _normalized("revenue", 1_000_000.0, "2024FY", version=Version.RESTATED),
    ]
    row = _yoy_row(inputs=(facts[0].normalized_fact_id, facts[1].normalized_fact_id))
    verdict = validate_comparability(
        _Result([row], "use_adjusted"), normalized_facts=facts
    )
    assert verdict.valid is True and verdict.errors == []


def test_the_validator_passes_on_both_integration_filings(registry):
    for name, declared in ((ANNUAL_RESTATED, "annual"), (ANNUAL_RECLASS, "annual")):
        parsed = _ingest(registry, name, declared)
        normalization = normalize_financials(parsed.facts, parsed.document)
        result = analyze_document(normalization, documents=parsed.document)
        verdict = validate_comparability(
            result,
            normalized_facts=normalization.facts,
            known_fact_ids={fact.fact_id for fact in parsed.facts},
            signals=result.scope_signals,
        )
        assert verdict.valid is True, (name, verdict.errors)
        assert verdict.warnings == [], (name, verdict.warnings)


# --------------------------------------------------------------------------- #
# §6 Agent tool 契约
# --------------------------------------------------------------------------- #

def _run_tool(tool, **kwargs):
    return json.loads(asyncio.run(tool.execute(**kwargs)))


def test_scope_tool_envelope_matches_the_card_contract(registry):
    from src.agents.tools.cn_tools import DetectCnScopeSignalsTool

    parsed = _ingest(registry, ANNUAL_RESTATED)
    payload = _run_tool(
        DetectCnScopeSignalsTool(registry),
        document_id=parsed.document.document_id,
        detected_at=STAMP,
    )

    assert set(payload) >= {"status", "signals", "comparability"}
    assert payload["status"] in {"ok", "clean", "blocked"}
    assert payload["signal_count"] == len(payload["signals"]) > 0
    assert payload["comparability"]
    assert payload["comparability"][0]["decision"] == "use_adjusted"
    assert payload["blocked_documents"] == []
    assert payload["coverage"]["rule_count"] >= 8
    assert payload["labels"]["comparability"]["block_yoy"] == "阻断同比"
    for signal in payload["signals"]:
        assert signal["evidence_ids"]
        assert signal["comparability"] in {item.value for item in ComparabilityDecision}


def test_scope_tool_reports_clean_filings_as_clean_and_says_why(registry):
    from src.agents.tools.cn_tools import DetectCnScopeSignalsTool

    parsed = _ingest(registry, ANNUAL_PLAIN)
    payload = _run_tool(
        DetectCnScopeSignalsTool(registry),
        document_id=parsed.document.document_id,
        detected_at=STAMP,
    )

    assert payload["status"] == "clean"
    assert payload["signals"] == [] and payload["comparability"] == []
    assert "不等于" in payload["note"]


def test_scope_tool_reports_typed_errors_for_bad_arguments(registry):
    from src.agents.tools.cn_tools import DetectCnScopeSignalsTool

    tool = DetectCnScopeSignalsTool(registry)

    blank = _run_tool(tool)
    assert blank["status"] == "error" and blank["error_code"] == "scope_missing_argument"
    assert "symbol" in blank["error"]

    unknown = _run_tool(tool, document_id="does-not-exist")
    assert unknown["status"] == "error" and unknown["error_code"] == "document_not_found"


def test_scope_tool_is_blocked_rather_than_error_when_there_are_no_facts(registry):
    from src.agents.tools.cn_tools import DetectCnScopeSignalsTool

    parsed = _ingest(registry, "scan_sample.pdf")
    payload = _run_tool(DetectCnScopeSignalsTool(registry), document_id=parsed.document.document_id)

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "scope_no_facts"
    assert payload["signals"] == []


def test_scope_tool_is_registered_with_the_document_layer_tools():
    from src.agents.tools.cn_tools import build_cn_snapshot_tools

    names = {tool.name for tool in build_cn_snapshot_tools(FIXTURES / "does-not-exist.json")}
    assert {"ingest_cn_document", "normalize_cn_financials", "detect_cn_scope_signals"} <= names


# --------------------------------------------------------------------------- #
# §14 读模型（UI 与工具共用）
# --------------------------------------------------------------------------- #

@pytest.fixture()
def service_registry(tmp_path, registry):
    from src.cn import document_service as service

    previous = service.get_registry()
    service._REGISTRY = registry
    try:
        yield registry
    finally:
        service._REGISTRY = previous


def test_read_model_carries_the_decision_the_banner_and_the_pages(service_registry):
    from src.cn import document_service as service

    parsed = _ingest(service_registry, ANNUAL_RESTATED)
    payload = service.document_analysis(parsed.document.document_id)

    assert payload["comparability"] == "use_adjusted"
    assert payload["comparability_blocked"] is False

    banner = payload["comparability_banner"]
    assert banner["decision_label"] == "改用调整后数据"
    assert banner["blocked"] is False
    assert banner["pages"] and all(isinstance(page, int) for page in banner["pages"])
    assert "不可比" in banner["message"]

    assert payload["scope_signals"]
    for signal in payload["scope_signals"]:
        assert signal["label"] and signal["decision_label"]
        assert signal["evidence"], "每条信号都要给出可跳转的证据"
        assert signal["locatable"] is True, signal["evidence_ids"]
        assert all(item["jumpable"] for item in signal["evidence"])


def test_read_model_banner_is_absent_for_a_clean_filing(service_registry):
    from src.cn import document_service as service

    parsed = _ingest(service_registry, ANNUAL_PLAIN)
    payload = service.document_analysis(parsed.document.document_id)

    assert payload["comparability"] is None
    assert payload["comparability_banner"] is None
    assert payload["scope_signals"] == []


def test_read_model_marks_the_blocked_banner(service_registry):
    from src.cn import document_service as service

    parsed = _ingest(service_registry, ANNUAL_RECLASS)
    payload = service.document_analysis(parsed.document.document_id)

    banner = payload["comparability_banner"]
    assert banner["blocked"] is True
    assert banner["decision_label"] == "阻断同比"
    assert "未输出同比百分比" in banner["message"]
    assert all(row["delta_pct"] is None for row in payload["metrics"])
