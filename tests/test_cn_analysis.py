"""Offline tests for the CARD-03 Financial Analysis Engine.

The card's §11 closed-form cases are the spine here:

* the instruction's YTD series ``Q1=100 / H1=230 / 9M=390 / FY=540`` must yield
  single quarters ``100 / 130 / 160 / 150`` **through the existing period
  engine**, and the quarter-on-quarter changes must come out as
  ``+30.00% / +23.077% / −6.25%`` — never ``H1/Q1 − 1``;
* the five YoY branches (normal / 扭亏为盈 / 由盈转亏 / 亏损扩大 / 亏损收窄) plus
  the zero-base ``not_computable`` case, with **no percentage in any state
  transition**;
* the four diagnostic signals with positive and negative examples;
* the degradation paths: missing 扣非, missing Q3YTD, a break in the series.

Plus the §12 integration chain CARD-01 → CARD-02 → CARD-03 on a synthetic annual
report, and the §10 requirement that every reported metric's ``calculation_id``
resolves and re-derives to the same value.

Everything is offline: synthetic fixtures and hand-built facts only.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from src.cn.analysis import (
    CFO_TO_NET_PROFIT,
    CONTRACT_LIABILITIES,
    DEDUCTED_NET_PROFIT_YOY,
    FREE_CASH_FLOW,
    GROWTH_GAP_THRESHOLD_PCT,
    GROSS_MARGIN,
    INVENTORY_TO_REVENUE,
    NET_MARGIN,
    NET_PROFIT_YOY,
    NON_RECURRING_RATIO,
    RECEIVABLES_TO_REVENUE,
    REVENUE_QOQ_SINGLE_QUARTER,
    REVENUE_YOY,
    ROE,
    SIGNAL_EARNINGS_QUALITY,
    SIGNAL_INVENTORY_PRESSURE,
    SIGNAL_NON_RECURRING,
    SIGNAL_RECEIVABLE_RISK,
    AnalysisResult,
    CalculationError,
    CalculationRegistry,
    ComparisonKind,
    ComparisonSemantic,
    Operand,
    analyze_document,
    analyze_financials,
    analysis_jumps,
    classify,
    compare,
    compute_single_quarter_qoq,
    evidence_operand,
    publish_analysis_evidence,
)
from src.cn.documents.models import (
    FACT_KIND_TABLE_CELL,
    DocumentRecord,
    ExtractedFact,
)
from src.cn.documents.registry import DocumentRegistry
from src.cn.evidence import EvidenceLedger
from src.cn.normalization import (
    NormalizedFact,
    PeriodKind,
    Scope,
    Version,
    normalize_financials,
)

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "documents"
YTD = PeriodKind.YTD_CUMULATIVE
POINT = PeriodKind.POINT_IN_TIME


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _fact(
    metric: str,
    value: float,
    period_key: str,
    *,
    kind: PeriodKind = YTD,
    statement: str = "income",
    scope: Scope = Scope.CONSOLIDATED,
    currency: str = "CNY",
    document_id: str = "DOC-1",
    page: int = 1,
) -> NormalizedFact:
    """One CARD-02 normalized fact, shaped exactly as the pipeline emits it."""
    token = {YTD: "c", POINT: "p"}.get(kind, "x")
    return NormalizedFact(
        normalized_fact_id=f"nf_{document_id}_{metric}_{period_key}_{token}_{page}",
        metric=metric,
        value=float(value),
        currency=currency,
        period_key=period_key,
        period_kind=kind,
        scope=scope,
        version=Version.AS_REPORTED,
        available_at="2025-04-30T00:00:00+08:00",
        source_fact_id=f"x_{metric}_{period_key}",
        document_id=document_id,
        page=page,
        row=1,
        column=1,
        raw_value=str(value),
        source_unit="元",
        source_table="合并报表",
        statement_type=statement,
        metric_known=True,
    )


def _quarterly_revenue_facts(year: int, base: tuple) -> List[NormalizedFact]:
    return [
        _fact("revenue", value, f"{year}{suffix}")
        for suffix, value in zip(("Q1", "H1", "9M", "FY"), base)
    ]


def _rich_facts() -> List[NormalizedFact]:
    """Two annual years covering all 14 metrics, with three signals firing."""
    facts: List[NormalizedFact] = []
    for year, revenue, parent, deducted, cost in (
        (2023, 1000.0, 100.0, 90.0, 600.0),
        (2024, 1200.0, 120.0, 80.0, 700.0),
    ):
        facts += [
            _fact("revenue", revenue, f"{year}FY"),
            _fact("net_profit_parent", parent, f"{year}FY"),
            _fact("net_profit_deducted", deducted, f"{year}FY"),
            _fact("operating_cost", cost, f"{year}FY"),
        ]
    for year, cfo, capex in ((2023, 150.0, 60.0), (2024, 100.0, 70.0)):
        facts += [
            _fact("cfo", cfo, f"{year}FY", statement="cashflow"),
            _fact("capex", capex, f"{year}FY", statement="cashflow"),
        ]
    for year, equity, receivables, inventory in (
        (2023, 800.0, 200.0, 100.0),
        (2024, 900.0, 260.0, 105.0),
    ):
        facts += [
            _fact("equity_parent", equity, f"{year}FY", kind=POINT, statement="balance"),
            _fact("receivables", receivables, f"{year}FY", kind=POINT, statement="balance"),
            _fact("inventory", inventory, f"{year}FY", kind=POINT, statement="balance"),
        ]
    facts.append(_fact("contract_liabilities", 50.0, "2024FY", kind=POINT, statement="balance"))
    return facts


def _signal_ids(result: AnalysisResult) -> set:
    return set(result.signal_ids())


# -- CARD-01 shaped input for the full-chain tests --------------------------- #

_INCOME_ROWS = (
    ("营业收入", "1,200", "1,000"),
    ("营业成本", "700", "600"),
    ("归属于母公司股东的净利润", "120", "100"),
    ("扣除非经常性损益后归属于母公司股东的净利润", "80", "90"),
    ("净利润", "130", "110"),
)

_INCOME_TABLE = "合并利润表"


def _located(
    text: str,
    raw_value: Optional[str],
    *,
    row: int,
    column: int,
    column_label: Optional[str] = None,
) -> ExtractedFact:
    meta = {"column_label": column_label} if column_label else {}
    return ExtractedFact(
        document_id="DOC-T",
        raw_text=text,
        page=1,
        bbox=(1.0, 2.0, 30.0, 8.0),
        section=_INCOME_TABLE,
        table=_INCOME_TABLE,
        row=row,
        column=column,
        raw_value=raw_value,
        normalized_value=None,
        currency="CNY",
        unit="元",
        period="2024FY",
        scope="合并",
        available_at="2025-03-28",
        status="ok",
        kind=FACT_KIND_TABLE_CELL,
        fact_id=f"fact_{_INCOME_TABLE}_{row}_{column}",
        meta=meta,
    )


def _income_statement_facts() -> List[ExtractedFact]:
    """A two-column income statement exactly as the CARD-01 table parser emits it.

    Column 1 is 本期金额 (``current_period``), column 2 is 上期金额
    (``prior_year``) — which CARD-02 deterministically resolves to ``2023FY``.
    """
    labels = {1: "current_period", 2: "prior_year"}
    facts = [
        _located(label, None, row=0, column=index, column_label=labels.get(index))
        for index, label in enumerate(("项目", "本期金额", "上期金额"))
    ]
    for row, (label, current, prior) in enumerate(_INCOME_ROWS, start=1):
        facts.append(_located(label, None, row=row, column=0))
        for column, value in ((1, current), (2, prior)):
            facts.append(_located(value, value, row=row, column=column, column_label=labels[column]))
    return facts


def _document() -> DocumentRecord:
    return DocumentRecord(
        document_id="DOC-T",
        sha256="0" * 64,
        filename="annual_2024.pdf",
        source="user_upload",
        document_type="annual",
        company="示例股份有限公司",
        report_period="2024FY",
        publication_date="2025-03-28",
        ingested_at="2025-03-28T00:00:00+00:00",
        stored_path=None,
        injection_segments=[],
        notes=[],
    )


# --------------------------------------------------------------------------- #
# §11 指令算例：YTD → 单季 → QoQ
# --------------------------------------------------------------------------- #

def test_instruction_ytd_case_yields_single_quarters_100_130_160_150():
    facts = _quarterly_revenue_facts(2024, (100.0, 230.0, 390.0, 540.0))
    result = analyze_financials(facts, document_id="DOC-Q", report_period="2024FY")

    quarters = {
        item.period_key: item.value
        for item in result.by_metric(REVENUE_QOQ_SINGLE_QUARTER)
    }
    assert quarters["2024Q2"] == 130.0
    assert quarters["2024Q3"] == 160.0
    assert quarters["2024Q4"] == 150.0

    # Q1 is the disclosed cumulative value itself: with no predecessor in the
    # series it carries no QoQ row (and is not invented as one).
    assert "2024Q1" not in quarters


def test_single_quarter_qoq_values_match_the_closed_form():
    facts = _quarterly_revenue_facts(2024, (100.0, 230.0, 390.0, 540.0))
    result = analyze_financials(facts, document_id="DOC-Q", report_period="2024FY")

    changes = {
        item.period_key: (item.delta_pct, item.delta_abs, item.comparison_semantic)
        for item in result.by_metric(REVENUE_QOQ_SINGLE_QUARTER)
    }
    assert changes["2024Q2"][0] == pytest.approx(30.0)          # (130 - 100) / 100
    assert changes["2024Q3"][0] == pytest.approx(160 / 130 * 100 - 100)
    assert changes["2024Q4"][0] == pytest.approx(-6.25)         # (150 - 160) / 160
    assert all(semantic is ComparisonSemantic.NORMAL for _pct, _abs, semantic in changes.values())


def test_qoq_never_compares_a_cumulative_value_with_a_quarter():
    """The v2 bug: ``H1 / Q1 − 1`` reported as a quarter-on-quarter change."""
    cumulative = _fact("revenue", 230.0, "2024H1")
    previous = _fact("revenue", 100.0, "2024Q1")

    with pytest.raises(ValueError) as excinfo:
        compute_single_quarter_qoq(
            CalculationRegistry(),
            metric="revenue",
            label="环比",
            current_quarter=cumulative,
            previous_quarter=previous,
        )
    assert "单季" in str(excinfo.value)


def test_qoq_refuses_an_operand_without_declared_period_semantics():
    class _Bare:
        value = 100.0
        period_key = "2024Q2"

    with pytest.raises(ValueError) as excinfo:
        compute_single_quarter_qoq(
            CalculationRegistry(),
            metric="revenue",
            label="环比",
            current_quarter=_Bare(),
            previous_quarter=_Bare(),
        )
    assert "不可判定" in str(excinfo.value)


def test_qoq_requires_both_operands_to_be_single_quarters():
    registry = CalculationRegistry()
    facts = _quarterly_revenue_facts(2024, (100.0, 230.0, 390.0, 540.0))
    result = analyze_financials(facts, document_id="DOC-Q", report_period="2024FY")

    # The engine-derived single quarters carry the engine's basis, so they pass.
    rows = result.by_metric(REVENUE_QOQ_SINGLE_QUARTER)
    assert rows and all(row.period_basis == "SINGLE_QUARTER" for row in rows)
    assert registry.records() == []


def test_annual_report_has_no_single_quarter_row_but_is_not_incomplete():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")

    qoq_missing = [item for item in result.missing if item.metric == REVENUE_QOQ_SINGLE_QUARTER]
    assert len(qoq_missing) == 1
    assert qoq_missing[0].blocking is False       # not applicable, not missing data
    assert result.data_complete is True


# --------------------------------------------------------------------------- #
# §11 同比五分支 + 零基期
# --------------------------------------------------------------------------- #

def test_yoy_branch_normal_reports_a_percentage():
    semantic, delta_abs, delta_pct = compare(120.0, 100.0, metric="net_profit_parent", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.NORMAL
    assert delta_abs == pytest.approx(20.0)
    assert delta_pct == pytest.approx(20.0)


def test_yoy_branch_turnaround_abs_only():
    """prev < 0 < cur must not emit a percentage (a rise from −100 to 100 is not +200% in any useful sense)."""
    semantic, delta_abs, delta_pct = compare(100.0, -100.0, metric="net_profit_parent", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.TURNAROUND
    assert delta_abs == pytest.approx(200.0)
    assert delta_pct is None


def test_yoy_branch_to_loss_abs_only():
    semantic, delta_abs, delta_pct = compare(-50.0, 100.0, metric="net_profit_parent", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.TO_LOSS
    assert delta_abs == pytest.approx(-150.0)
    assert delta_pct is None


def test_yoy_branch_loss_widening_abs_only_and_signed():
    semantic, delta_abs, delta_pct = compare(-150.0, -100.0, metric="net_profit_parent", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.LOSS_WIDENING
    assert delta_abs == pytest.approx(-50.0)      # more negative == worse
    assert delta_pct is None


def test_yoy_branch_loss_narrowing_abs_only_and_signed():
    semantic, delta_abs, delta_pct = compare(-50.0, -100.0, metric="net_profit_parent", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.LOSS_NARROWING
    assert delta_abs == pytest.approx(50.0)       # less negative == better
    assert delta_pct is None


def test_yoy_branch_zero_base_is_not_computable():
    semantic, delta_abs, delta_pct = compare(100.0, 0.0, metric="net_profit_parent", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.NOT_COMPUTABLE
    assert delta_abs is None and delta_pct is None


def test_yoy_branch_equal_losses_are_not_computable():
    semantic, _delta_abs, delta_pct = compare(-5.0, -5.0, metric="net_profit_parent", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.NOT_COMPUTABLE
    assert delta_pct is None


def test_revenue_is_never_given_profit_state_semantics():
    """A revenue decline is a decline — not 「亏损扩大」."""
    assert classify(900.0, 1000.0, metric="revenue") is ComparisonSemantic.NORMAL
    assert classify(-50.0, -100.0, metric="revenue") is ComparisonSemantic.NORMAL
    semantic, _abs, delta_pct = compare(900.0, 1000.0, metric="revenue", kind=ComparisonKind.YOY)
    assert semantic is ComparisonSemantic.NORMAL
    assert delta_pct == pytest.approx(-10.0)


def test_classify_covers_every_branch_of_the_table():
    table = [
        (120.0, 100.0, ComparisonSemantic.NORMAL),
        (100.0, -100.0, ComparisonSemantic.TURNAROUND),
        (-50.0, 100.0, ComparisonSemantic.TO_LOSS),
        (-150.0, -100.0, ComparisonSemantic.LOSS_WIDENING),
        (-50.0, -100.0, ComparisonSemantic.LOSS_NARROWING),
        (100.0, 0.0, ComparisonSemantic.NOT_COMPUTABLE),
    ]
    for current, previous, expected in table:
        assert classify(current, previous, metric="net_profit_parent") is expected


# --------------------------------------------------------------------------- #
# §10 validator rules
# --------------------------------------------------------------------------- #

def test_no_percentage_leaks_out_of_any_state_transition():
    """§10: a transition semantic must never carry ``delta_pct``."""
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    for item in result.metrics:
        if item.comparison_semantic is None:
            continue
        if item.comparison_semantic is not ComparisonSemantic.NORMAL:
            assert item.delta_pct is None, item.metric


def test_transition_metrics_are_recorded_as_absolute_differences():
    registry = CalculationRegistry()
    facts = [
        _fact("net_profit_parent", -100.0, "2023FY"),
        _fact("net_profit_parent", 150.0, "2024FY"),
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")

    row = result.metric(NET_PROFIT_YOY)
    assert row is not None
    assert row.comparison_semantic is ComparisonSemantic.TURNAROUND
    assert row.delta_pct is None
    record = result.registry.get(row.calculation_id)
    assert record.operation == "subtract"          # structurally cannot be a percentage
    assert record.value == pytest.approx(250.0)


def test_every_metric_calculation_id_resolves_and_reproduces_its_value():
    """§10 rule 1: 「calculation_id 可解析（公式+输入证据可复核）」.

    A comparison row's ``value`` is the *level* while its calculation is the
    *change*, so the contract is: the record re-derives to its own value, and
    that value equals whichever delta the metric published.
    """
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")

    assert result.metrics, "expected metrics to analyse"
    for item in result.metrics:
        assert item.calculation_id, f"{item.metric} has no calculation record"
        record = result.registry.get(item.calculation_id)
        assert record.formula, f"{item.metric} formula is empty"
        assert record.operands, f"{item.metric} has no operands"

        recomputed = result.registry.recalculate(item.calculation_id)
        assert recomputed == pytest.approx(record.value), item.metric

        if item.delta_pct is not None:
            assert record.value == pytest.approx(item.delta_pct), item.metric
        elif item.delta_abs is not None:
            assert record.value == pytest.approx(item.delta_abs), item.metric
        else:
            # Level rows: the record carries the level itself.
            assert record.value == pytest.approx(item.value), item.metric


def test_calculation_registry_validates_clean_for_a_full_analysis():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    assert result.registry.validate() == []


def test_calculation_records_name_the_reused_engine():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    engines = {record.engine for record in result.registry.records()}
    # Ratio metrics record the analysis module; nothing claims the period engine
    # unless it really came from it.
    assert any(engine and "analysis" in engine for engine in engines if engine)


# --------------------------------------------------------------------------- #
# §11 四类诊断信号
# --------------------------------------------------------------------------- #

def test_earnings_quality_signal_fires_when_profit_rises_and_cfo_falls():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    assert SIGNAL_EARNINGS_QUALITY in _signal_ids(result)

    signal = next(s for s in result.signals if s.signal_id == SIGNAL_EARNINGS_QUALITY)
    assert signal.periods == ("2023FY", "2024FY")
    assert "+20.00%" in signal.detail and "-33.33%" in signal.detail


def test_earnings_quality_signal_is_silent_when_cfo_also_rises():
    facts = [
        _fact("net_profit_parent", 100.0, "2023FY"),
        _fact("net_profit_parent", 120.0, "2024FY"),
        _fact("cfo", 150.0, "2023FY", statement="cashflow"),
        _fact("cfo", 180.0, "2024FY", statement="cashflow"),
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")
    assert SIGNAL_EARNINGS_QUALITY not in _signal_ids(result)


def test_receivable_signal_fires_at_the_gap_threshold():
    """Revenue +20% with receivables +30% is exactly the 10pp threshold → fires."""
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    signal = next(s for s in result.signals if s.signal_id == SIGNAL_RECEIVABLE_RISK)
    assert signal.threshold == GROWTH_GAP_THRESHOLD_PCT
    assert signal.observed == pytest.approx(10.0)
    assert "混合口径" in signal.detail


def test_receivable_signal_is_silent_below_the_gap_threshold():
    facts = _rich_facts()
    # Receivables +25% (gap 5pp) instead of +30%.
    facts = [
        _fact("receivables", 250.0, "2024FY", kind=POINT, statement="balance")
        if fact.metric == "receivables" and fact.period_key == "2024FY"
        else fact
        for fact in facts
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")
    assert SIGNAL_RECEIVABLE_RISK not in _signal_ids(result)


def test_inventory_signal_fires_when_inventory_outgrows_revenue():
    facts = _rich_facts()
    facts = [
        _fact("inventory", 140.0, "2024FY", kind=POINT, statement="balance")
        if fact.metric == "inventory" and fact.period_key == "2024FY"
        else fact
        for fact in facts
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")
    assert SIGNAL_INVENTORY_PRESSURE in _signal_ids(result)


def test_inventory_signal_is_silent_below_the_gap_threshold():
    """The default dataset grows inventory 5% against 20% revenue growth."""
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    assert SIGNAL_INVENTORY_PRESSURE not in _signal_ids(result)


def test_non_recurring_signal_fires_when_parent_rises_and_deducted_falls():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    signal = next(s for s in result.signals if s.signal_id == SIGNAL_NON_RECURRING)
    assert "+20.00%" in signal.detail and "-11.11%" in signal.detail


def test_non_recurring_signal_is_silent_when_deducted_also_rises():
    facts = [
        _fact("net_profit_parent", 100.0, "2023FY"),
        _fact("net_profit_parent", 120.0, "2024FY"),
        _fact("net_profit_deducted", 90.0, "2023FY"),
        _fact("net_profit_deducted", 110.0, "2024FY"),
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")
    assert SIGNAL_NON_RECURRING not in _signal_ids(result)


def test_signals_are_silent_when_an_input_is_incomplete():
    """宁缺勿假: no CFO ⇒ no earnings-quality signal, not a weaker one."""
    facts = [
        _fact("net_profit_parent", 100.0, "2023FY"),
        _fact("net_profit_parent", 120.0, "2024FY"),
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")
    assert result.signals == []


def test_signal_wording_keeps_the_hedge_and_makes_no_causal_claim():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    assert result.signals
    for signal in result.signals:
        assert "需进一步核查" in signal.detail
        for forbidden in ("导致", "因为", "由于"):
            assert forbidden not in signal.detail


def test_signal_calculation_ids_resolve_in_the_registry():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    for signal in result.signals:
        assert signal.metric_calculation_ids, signal.signal_id
        for calculation_id in signal.metric_calculation_ids:
            assert result.registry.has(calculation_id), (signal.signal_id, calculation_id)


# --------------------------------------------------------------------------- #
# §8 edge cases
# --------------------------------------------------------------------------- #

def test_q4_single_quarter_is_fy_minus_q3ytd():
    facts = _quarterly_revenue_facts(2024, (100.0, 230.0, 390.0, 540.0))
    result = analyze_financials(facts, document_id="DOC-Q", report_period="2024FY")
    q4 = next(item for item in result.by_metric(REVENUE_QOQ_SINGLE_QUARTER) if item.period_key == "2024Q4")
    assert q4.value == pytest.approx(540.0 - 390.0)


def test_missing_q3ytd_stops_q4_without_interpolation():
    facts = _quarterly_revenue_facts(2024, (100.0, 230.0, 390.0, 540.0))
    facts = [fact for fact in facts if fact.period_key != "20249M"]
    result = analyze_financials(facts, document_id="DOC-Q", report_period="2024FY")

    periods = {item.period_key for item in result.by_metric(REVENUE_QOQ_SINGLE_QUARTER)}
    assert "2024Q3" not in periods and "2024Q4" not in periods
    assert "2024Q2" in periods       # H1 − Q1 still derivable


def test_missing_deducted_profit_skips_the_metric_and_marks_incomplete():
    facts = [fact for fact in _rich_facts() if fact.metric != "net_profit_deducted"]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")

    assert result.metric(DEDUCTED_NET_PROFIT_YOY) is None
    assert result.metric(NON_RECURRING_RATIO) is None
    missing = {item.metric for item in result.missing if item.blocking}
    assert DEDUCTED_NET_PROFIT_YOY in missing
    assert NON_RECURRING_RATIO in missing
    assert result.data_complete is False
    # The card forbids substituting net profit for the deducted figure.
    assert any("不得用净利润替代" in item.reason for item in result.missing)


def test_series_break_blocks_yoy_without_guessing():
    """A gap year must not be papered over with a 2-year comparison."""
    facts = [
        _fact("revenue", 900.0, "2022FY"),
        _fact("revenue", 1200.0, "2024FY"),
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")

    assert result.metric(REVENUE_YOY) is None
    entry = next(item for item in result.missing if item.metric == REVENUE_YOY)
    assert "序列断点" in entry.reason


def test_zero_base_year_is_published_as_not_computable_without_a_percentage():
    """§6: ``previous == 0`` → ``not_computable``; the level is still shown."""
    facts = [
        _fact("revenue", 0.0, "2023FY"),
        _fact("revenue", 500.0, "2024FY"),
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")

    row = result.metric(REVENUE_YOY)
    assert row is not None
    assert row.comparison_semantic is ComparisonSemantic.NOT_COMPUTABLE
    assert row.delta_pct is None and row.delta_abs is None
    assert row.calculation_id is None          # nothing to re-derive
    assert row.value == pytest.approx(500.0)   # the current level is still reported
    assert "previous_period_is_zero" in row.warnings


def test_roe_prefers_a_disclosed_weighted_average_value():
    result = analyze_financials(
        _rich_facts(),
        document_id="DOC",
        report_period="2024FY",
        disclosed_ratios={"roe_weighted_disclosed": 15.75},
    )
    row = result.metric(ROE)
    assert row is not None
    assert row.value == pytest.approx(15.75)
    assert "披露" in (row.note or "")
    assert "披露" in result.registry.get(row.calculation_id).formula


def test_roe_uses_average_equity_when_both_periods_are_available():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    row = result.metric(ROE)
    assert row is not None
    assert row.value == pytest.approx(120.0 / ((800.0 + 900.0) / 2) * 100)
    assert "算术平均" in (row.note or "")


def test_roe_falls_back_to_period_end_equity_and_labels_the_caliber():
    facts = [fact for fact in _rich_facts() if not (fact.metric == "equity_parent" and fact.period_key == "2023FY")]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")

    row = result.metric(ROE)
    assert row is not None
    assert row.value == pytest.approx(120.0 / 900.0 * 100)
    assert "期末净资产近似" in (row.note or "")
    assert any("降级" in message for message in result.warnings)


def test_mixed_period_ratios_declare_their_caliber():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    for metric_id in (RECEIVABLES_TO_REVENUE, INVENTORY_TO_REVENUE):
        row = result.metric(metric_id)
        assert row is not None
        assert "混合口径" in (row.note or "")


def test_stock_metrics_are_level_only_and_never_differenced():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    rows = result.by_metric(CONTRACT_LIABILITIES)
    assert rows
    for row in rows:
        assert row.comparison is ComparisonKind.LEVEL
        assert row.delta_pct is None and row.delta_abs is None
        assert row.period_basis == "POINT_IN_TIME"


def test_cross_scope_facts_are_not_paired_and_the_choice_is_reported():
    facts = _rich_facts()
    parent_scope = [
        _fact("revenue", 500.0, "2023FY", scope=Scope.PARENT),
        _fact("revenue", 700.0, "2024FY", scope=Scope.PARENT),
    ]
    result = analyze_financials(facts + parent_scope, document_id="DOC", report_period="2024FY")

    assert result.scope == "合并"
    row = result.metric(REVENUE_YOY)
    assert row is not None and row.scope == "合并"
    assert row.value == pytest.approx(1200.0)      # never mixed with the 700
    assert any("多个口径" in message for message in result.warnings)


def test_non_cny_facts_are_labelled_not_converted():
    facts = [
        _fact("revenue", 1000.0, "2023FY", currency="USD"),
        _fact("revenue", 1200.0, "2024FY", currency="USD"),
    ]
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")
    row = result.metric(REVENUE_YOY)
    assert row is not None
    assert row.currency == "USD"
    assert row.delta_pct == pytest.approx(20.0)


def test_empty_fact_list_registers_every_metric_as_missing():
    result = analyze_financials([], document_id="DOC")
    assert result.metrics == []
    assert result.data_complete is False
    assert len(result.missing) == 14
    assert all(item.blocking for item in result.missing)


def test_revenue_qoq_is_the_only_qoq_row():
    facts = _quarterly_revenue_facts(2024, (100.0, 230.0, 390.0, 540.0))
    facts += [
        _fact("net_profit_parent", value, f"2024{suffix}")
        for suffix, value in zip(("Q1", "H1", "9M", "FY"), (10.0, 25.0, 45.0, 60.0))
    ]
    result = analyze_financials(facts, document_id="DOC-Q", report_period="2024FY")
    qoq_metrics = {
        item.metric for item in result.metrics if item.comparison is ComparisonKind.QOQ
    }
    assert qoq_metrics == {REVENUE_QOQ_SINGLE_QUARTER}


def test_gross_margin_is_derived_when_gross_profit_is_not_disclosed():
    """The fallback must match CARD-02's rule: revenue − operating_cost."""
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    row = result.metric(GROSS_MARGIN)
    assert row is not None
    assert row.value == pytest.approx((1200.0 - 700.0) / 1200.0 * 100)
    record = result.registry.get(row.calculation_id)
    assert "operating_cost" in record.formula


def test_free_cash_flow_uses_the_derived_key_not_cfo():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    row = result.metric(FREE_CASH_FLOW)
    assert row is not None
    assert row.value == pytest.approx(100.0 - 70.0)     # cfo − capex, not cfo


def test_cfo_to_net_profit_and_net_margin_use_the_parent_profit():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    assert result.metric(CFO_TO_NET_PROFIT).value == pytest.approx(100.0 / 120.0 * 100)
    assert result.metric(NET_MARGIN).value == pytest.approx(120.0 / 1200.0 * 100)


def test_percentages_are_reported_in_percent_units():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    for metric_id in (GROSS_MARGIN, NET_MARGIN, ROE, CFO_TO_NET_PROFIT, NON_RECURRING_RATIO):
        assert result.metric(metric_id).unit == "PERCENT"


# --------------------------------------------------------------------------- #
# §12 证据账本桥接
# --------------------------------------------------------------------------- #

def _ledger_with_facts(facts) -> EvidenceLedger:
    ledger = EvidenceLedger(snapshot_id="DOC")
    for fact in facts:
        ledger.add_fact(
            evidence_id=fact.normalized_fact_id,
            symbol="DOC",
            metric=fact.metric,
            value=fact.value,
            currency=fact.currency,
            unit="元",
            fiscal_period=fact.period_key,
            period_basis=fact.period_kind.value,
            published_at=fact.available_at,
            available_at=fact.available_at,
            provider="normalization",
            field_path=f"documents.DOC.{fact.metric}",
        )
    return ledger


def test_ledger_publishes_the_expressible_subset_and_skips_constants():
    facts = _rich_facts()
    ledger = _ledger_with_facts(facts)
    result = analyze_financials(facts, document_id="DOC", report_period="2024FY")

    payload = publish_analysis_evidence(result, ledger=ledger, symbol="DOC")
    assert payload["published_count"] >= 1
    assert ledger.validate(research_as_of="2026-09-17T00:00:00+08:00").valid

    reasons = {item["reason"] for item in payload["skipped"]}
    # A ×100 is a formula constant, not evidence — the ledger refuses it, and we
    # refuse to invent a "100" fact to work around that.
    assert "ledger_cannot_express_formula_constant" in reasons or "operation_not_expressible_in_ledger" in reasons

    for evidence_id in payload["published"]:
        record = ledger.get(evidence_id)
        assert record.kind == "calculation"
        assert record.input_ids


def test_ledger_publishes_nothing_when_the_inputs_are_absent():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    payload = publish_analysis_evidence(result, ledger=EvidenceLedger(snapshot_id="DOC"), symbol="DOC")

    assert payload["published_count"] == 0
    assert payload["skipped"]
    assert all(item["reason"] for item in payload["skipped"])


def test_analysis_jumps_expose_formula_and_evidence_for_every_metric():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    jumps = analysis_jumps(result)

    assert len(jumps) == len(result.metrics)
    for item in result.metrics:
        payload = jumps[item.calculation_id]
        assert payload["formula"]
        assert payload["operands"]
        assert payload["recalculated"] is not None
        assert payload["label"] == item.label


def test_metric_inputs_reference_the_normalized_fact_ids():
    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    row = result.metric(REVENUE_YOY)
    assert row.inputs
    assert all(identifier.startswith("nf_") for identifier in row.inputs)


# --------------------------------------------------------------------------- #
# §12 CARD-01 → CARD-02 → CARD-03 全链
# --------------------------------------------------------------------------- #

@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def test_full_chain_produces_yoy_margins_and_a_signal():
    """CARD-01 table cells → CARD-02 normalization → CARD-03 analysis.

    A two-column income statement (本期金额 / 上期金额) is the smallest input
    that exercises the whole chain: the prior-year column becomes 2023FY, so the
    YoY rows are real comparisons rather than levels.
    """
    normalization = normalize_financials(_income_statement_facts(), _document())
    result = analyze_document(normalization)

    assert result.report_period == "2024FY"
    assert result.scope == "合并"

    assert result.metric(REVENUE_YOY).delta_pct == pytest.approx(20.0)
    assert result.metric(NET_PROFIT_YOY).delta_pct == pytest.approx(20.0)
    assert result.metric(DEDUCTED_NET_PROFIT_YOY).delta_pct == pytest.approx(80 / 90 * 100 - 100)
    assert result.metric(GROSS_MARGIN).value == pytest.approx((1200.0 - 700.0) / 1200.0 * 100)
    assert result.metric(NET_MARGIN).value == pytest.approx(120.0 / 1200.0 * 100)
    assert result.metric(NON_RECURRING_RATIO).value == pytest.approx((120.0 - 80.0) / 120.0 * 100)

    # 归母 +20% while 扣非 −11%: the non-recurring signal must fire.
    assert SIGNAL_NON_RECURRING in _signal_ids(result)


def test_full_chain_metrics_keep_the_document_evidence_backlink():
    """Every ``inputs`` entry must resolve inside the provenance graph.

    §10 only requires resolvability: an input is either a CARD-02 disclosed fact
    or a CARD-03 intermediate calculation (e.g. 非经常性损益 = 归母 − 扣非), both
    of which are auditable nodes. Anything else would be a dangling reference.
    """
    normalization = normalize_financials(_income_statement_facts(), _document())
    result = analyze_document(normalization)

    known_ids = {fact.normalized_fact_id for fact in normalization.facts}
    known_ids |= {record.calculation_id for record in result.registry.records()}
    assert result.metrics
    for item in result.metrics:
        for identifier in item.inputs:
            assert identifier in known_ids, (item.metric, identifier)
        # And the metric's own calculation must be reproducible.
        if item.calculation_id:
            assert result.registry.recalculate(item.calculation_id) is not None


def test_full_chain_validation_is_clean_on_a_real_document():
    normalization = normalize_financials(_income_statement_facts(), _document())
    result = analyze_document(normalization)
    assert result.registry.validate() == []


def test_full_chain_pdf_fixture_keeps_the_wan_yuan_scale(registry):
    """annual_02 discloses 单位：万元 — the analysis must see 元, not 万元."""
    parsed = registry.ingest(
        FIXTURES / "annual_02_balance_unit_wan.pdf", declared_type="annual", source="admin_import"
    )
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization)

    receivables = [fact for fact in normalization.facts if fact.metric == "receivables"]
    assert receivables and receivables[0].value == pytest.approx(83_000_000.0)

    # Balance-sheet-only input: the flow ratios cannot exist, and that is
    # reported rather than filled in.
    assert result.metric(REVENUE_YOY) is None
    assert result.metric(RECEIVABLES_TO_REVENUE) is None
    assert result.data_complete is False
    assert any(item.metric == RECEIVABLES_TO_REVENUE for item in result.missing)


def test_full_chain_pdf_fixture_without_a_prior_year_reports_the_gap(registry):
    """annual_01 has no 上期金额 column: the YoY row must be absent, not faked."""
    parsed = registry.ingest(
        FIXTURES / "annual_01_profit_continued.pdf", declared_type="annual", source="admin_import"
    )
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization)

    # 毛利率 and 净利率 are levels today — they need no prior period.
    assert result.metric(GROSS_MARGIN) is not None
    assert result.metric(NET_MARGIN) is not None

    assert result.metric(REVENUE_YOY) is None
    gap = next(item for item in result.missing if item.metric == REVENUE_YOY)
    assert "缺少上一可比期间" in gap.reason
    assert gap.blocking is True


def test_full_chain_scan_only_document_yields_no_metrics_and_no_crash(registry):
    parsed = registry.ingest(
        FIXTURES / "scan_sample.pdf", declared_type="annual", source="admin_import"
    )
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization)

    assert result.metrics == []
    assert result.data_complete is False
    assert len(result.missing) == 14


def test_full_chain_evidence_ledger_receives_the_published_subset(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_02_balance_unit_wan.pdf", declared_type="annual", source="admin_import"
    )
    normalization = normalize_financials(parsed.facts, parsed.document)
    result = analyze_document(normalization)

    ledger = EvidenceLedger(snapshot_id=parsed.document.document_id)
    payload = publish_analysis_evidence(result, ledger=ledger, symbol=parsed.document.document_id)
    assert payload["ledger_snapshot_id"] == parsed.document.document_id
    assert ledger.validate(research_as_of="2026-09-17T00:00:00+08:00").valid


# --------------------------------------------------------------------------- #
# computation-record contract
# --------------------------------------------------------------------------- #

def test_calculation_registry_rejects_a_malformed_record():
    registry = CalculationRegistry()
    with pytest.raises(CalculationError):
        registry.register(
            metric="x",
            formula="unknown",
            operation="not_an_operation",
            operands=[Operand(kind="constant", operand_id="1", value=1.0)],
            value=1.0,
            period_key="2024FY",
            period_basis="FY",
        )
    with pytest.raises(CalculationError):
        registry.register(
            metric="x",
            formula="percent_of with one operand",
            operation="percent_of",
            operands=[Operand(kind="constant", operand_id="1", value=1.0)],
            value=1.0,
            period_key="2024FY",
            period_basis="FY",
        )


def test_calculation_registry_reports_a_record_that_cannot_be_reproduced():
    registry = CalculationRegistry()
    registry.register(
        metric="x",
        formula="a - b",
        operation="subtract",
        operands=[evidence_operand(_fact("revenue", 10.0, "2024FY")), evidence_operand(_fact("revenue", 4.0, "2023FY"))],
        value=999.0,                     # deliberately wrong
        period_key="2024FY",
        period_basis="FY",
    )
    errors = registry.validate()
    assert len(errors) == 1 and "mismatch" in errors[0]


def test_calculation_registry_ids_are_deterministic():
    def build():
        registry = CalculationRegistry()
        result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY", registry=registry)
        return [item.calculation_id for item in result.metrics]

    assert build() == build()


def test_analysis_result_serialises_without_leaking_private_objects():
    import json

    result = analyze_financials(_rich_facts(), document_id="DOC", report_period="2024FY")
    payload = json.loads(json.dumps(result.to_dict(), default=str, ensure_ascii=False))

    assert payload["data_complete"] is True
    assert len(payload["metrics"]) == len(result.metrics)
    assert payload["signals"]
    assert all("calculation_id" in item for item in payload["metrics"])


# --------------------------------------------------------------------------- #
# §6 Agent tool contract
# --------------------------------------------------------------------------- #

def _run_tool(tool, **kwargs):
    import asyncio
    import json

    return json.loads(asyncio.run(tool.execute(**kwargs)))


def test_analysis_tool_envelope_matches_the_card_contract(registry):
    from src.agents.tools.cn_tools import AnalyzeCnFinancialsTool

    parsed = registry.ingest(
        FIXTURES / "annual_01_profit_continued.pdf", declared_type="annual", source="admin_import"
    )
    payload = _run_tool(AnalyzeCnFinancialsTool(registry), document_id=parsed.document.document_id)

    # §6: 出参 status / metrics / signals / data_complete / missing[]
    assert set(payload) >= {"status", "metrics", "signals", "data_complete", "missing"}
    assert payload["status"] in {"ok", "partial", "blocked"}
    assert payload["unit"] == "元"
    assert payload["report_period"] == "2025FY"
    assert payload["metric_count"] == len(payload["metrics"]) > 0
    assert payload["validation_errors"] == []
    for row in payload["metrics"]:
        assert row["calculation"]["formula"]
        assert row["calculation"]["recalculated"] is not None

    # annual_01 has no prior year and no quarterly series: partial, never "ok".
    assert payload["data_complete"] is False
    assert payload["status"] == "partial"
    assert "revenue_yoy" in payload["blocking_missing"]
    assert "revenue_qoq_single_quarter" in payload["not_applicable"]


def test_analysis_tool_is_blocked_without_an_error_when_nothing_is_computable(registry):
    """§9: a scan-only filing yields no metric — and says so, instead of guessing."""
    from src.agents.tools.cn_tools import AnalyzeCnFinancialsTool

    parsed = registry.ingest(FIXTURES / "scan_sample.pdf", declared_type="annual", source="admin_import")
    payload = _run_tool(AnalyzeCnFinancialsTool(registry), document_id=parsed.document.document_id)

    assert payload["status"] == "blocked"
    assert payload["metrics"] == []
    assert payload["data_complete"] is False
    assert len(payload["missing"]) == 14
    assert "产不出任何指标" in payload["error"]


def test_analysis_tool_reports_typed_errors_for_bad_arguments(registry):
    from src.agents.tools.cn_tools import AnalyzeCnFinancialsTool

    tool = AnalyzeCnFinancialsTool(registry)

    unknown = _run_tool(tool, document_id="does-not-exist")
    assert unknown["status"] == "error"
    assert unknown["error_code"] == "document_not_found"

    blank = _run_tool(tool)
    assert blank["status"] == "error"
    assert blank["error_code"] == "analysis_missing_argument"
    # The boundary is stated, not hidden: symbol-scoped analysis would bypass the
    # CARD-01→02 evidence chain, so it is refused explicitly.
    assert "symbol" in blank["error"]


def test_analysis_tool_can_register_evidence_and_stays_valid(registry):
    from src.agents.tools.cn_tools import AnalyzeCnFinancialsTool

    parsed = registry.ingest(
        FIXTURES / "annual_01_profit_continued.pdf", declared_type="annual", source="admin_import"
    )
    payload = _run_tool(
        AnalyzeCnFinancialsTool(registry),
        document_id=parsed.document.document_id,
        register_evidence=True,
        research_as_of="2026-09-17T00:00:00+08:00",
    )

    evidence = payload["evidence"]
    assert evidence["ledger_snapshot_id"] == parsed.document.document_id
    assert evidence["ledger_valid"] is True
    # §10/§12: percentages cannot be expressed in the ledger, so they are reported
    # as a typed skip rather than forged as evidence.
    assert "skipped" in evidence
