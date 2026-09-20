"""Offline unit tests for the CARD-02 Financial Normalization Engine.

These are the card's §11 closed-form cases, hand-verifiable on paper:

* unit conversion matrix (元 ↔ 千元 ↔ 万元 ↔ 百万元 ↔ 亿元), zero tolerance;
* the instruction's YTD case ``Q1=100, H1=230, 9M=390, FY=540`` →
  single quarters ``100 / 130 / 160 / 150`` — asserted **through the bridge**,
  which proves the values come from the existing ``FinancialPeriodEngine`` and
  not from a re-implementation;
* the TTM case ``FY2023=540, H1 2024=230, H1 2023=220`` → ``H1 2024 TTM = 550``;
* ``stock`` input raises ``PeriodEngineError`` (never silently differenced);
* undecidable ``period_kind`` → ``None`` + warning, never a default.

Plus the integration chain CARD-01 (synthetic annual report) → CARD-02, which is
where the unit bug of the document layer was caught: ``单位：万元`` used to be
detected as ``元``, a 10,000× error.

Everything here is offline: synthetic fixtures only, no network, no wall clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from src.cn.documents.models import (
    FACT_KIND_TABLE_CELL,
    DocumentRecord,
    ExtractedFact,
)
from src.cn.documents.registry import DocumentRegistry
from src.cn.domain import FinancialStatement
from src.cn.normalization import (
    METRIC_INDEX,
    PeriodKind,
    Scope,
    UnitNormalizationError,
    canonical_period,
    derive_periods,
    derive_single_quarters,
    derive_ttm,
    detect_period_kind,
    detect_scope,
    detect_version,
    normalize_financials,
    normalize_unit,
    parse_numeric,
    period_kind_decision,
    project_statements,
    resolve_metric,
    statement_value_key,
)
from src.cn.normalization.periods import previous_period_key
from src.cn.normalization.units import UNIT_SCALES
from src.cn.periods import PeriodEngineError

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "documents"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _fact(
    raw_text: str,
    raw_value: Optional[str],
    *,
    table: str = "合并利润表",
    row: int = 1,
    column: int = 1,
    unit: Optional[str] = "元",
    scope: Optional[str] = "合并",
    page: int = 1,
    meta: Optional[dict] = None,
    section: Optional[str] = None,
    document_id: str = "DOC-1",
    period: Optional[str] = "2025FY",
) -> ExtractedFact:
    """A located CARD-01 fact, shaped exactly like the table parser emits."""
    return ExtractedFact(
        document_id=document_id,
        raw_text=raw_text,
        page=page,
        bbox=(1.0, 2.0, 30.0, 8.0),
        section=section if section is not None else table,
        table=table,
        row=row,
        column=column,
        raw_value=raw_value,
        normalized_value=None,
        currency="CNY",
        unit=unit,
        period=period,
        scope=scope,
        available_at="2026-03-28",
        status="ok",
        kind=FACT_KIND_TABLE_CELL,
        fact_id=f"fact_{table}_{row}_{column}",
        meta=dict(meta or {}),
    )


def _row(
    label: str,
    values: "list[str]",
    *,
    row: int,
    table: str = "合并利润表",
    unit: Optional[str] = "元",
    scope: Optional[str] = "合并",
    page: int = 1,
    meta: Optional[dict] = None,
    document_id: str = "DOC-1",
    period: Optional[str] = "2025FY",
) -> "list[ExtractedFact]":
    """One logical table row: the label cell plus its data cells (col 0..n)."""
    cells = [
        _fact(
            label, None, table=table, row=row, column=0, unit=unit, scope=scope,
            page=page, document_id=document_id, period=period,
        )
    ]
    for index, value in enumerate(values, start=1):
        column_meta = dict(meta or {})
        if index == 1 and "column_label" not in column_meta:
            column_meta["column_label"] = meta.get("column_label", "current_period") if meta else "current_period"
        cells.append(
            _fact(
                value, value, table=table, row=row, column=index, unit=unit, scope=scope,
                page=page, meta=column_meta, document_id=document_id, period=period,
            )
        )
    return cells


def _header_row(
    header: "list[str]",
    *,
    table: str = "合并利润表",
    page: int = 1,
    column_labels: Optional[dict] = None,
    unit: Optional[str] = "元",
    scope: Optional[str] = "合并",
    document_id: str = "DOC-1",
    period: Optional[str] = "2025FY",
) -> "list[ExtractedFact]":
    """The header row of a logical table (col 0 = 项目, then the period columns)."""
    cells = []
    for column, text in enumerate(header):
        meta = {}
        if column in (column_labels or {}):
            meta["column_label"] = column_labels[column]
        cells.append(
            _fact(
                text, None, table=table, row=0, column=column, unit=unit, scope=scope,
                page=page, meta=meta, document_id=document_id, period=period,
            )
        )
    return cells


def _document(**overrides) -> DocumentRecord:
    base = dict(
        document_id="DOC-1",
        sha256="0" * 64,
        filename="annual_2025.pdf",
        source="user_upload",
        document_type="annual",
        company="示例股份有限公司",
        report_period="2025FY",
        publication_date="2026-03-28",
        ingested_at="2026-03-28T00:00:00+00:00",
        parser_name="financial_statement",
        parser_version="1",
        page_count=2,
    )
    base.update(overrides)
    return DocumentRecord(**base)


def _statement(
    fiscal_period: str,
    values: dict,
    *,
    statement_type: str = "income",
) -> FinancialStatement:
    suffix = "_END" if statement_type == "balance" else "_CUMULATIVE"
    return FinancialStatement(
        statement_type=statement_type,                       # type: ignore[arg-type]
        fiscal_period=fiscal_period,
        period_basis=f"{fiscal_period}{suffix}",
        published_at="2026-03-28",
        available_at="2026-03-28",
        currency="CNY",
        unit="元",
        values=values,
    )


# --------------------------------------------------------------------------- #
# §11.1 单位换算全矩阵 — zero tolerance
# --------------------------------------------------------------------------- #

def test_unit_conversion_matrix_is_exact_for_every_declared_unit():
    for unit, scale in UNIT_SCALES.items():
        assert normalize_unit("1", unit) == scale, unit
        assert normalize_unit("1,000", unit) == 1000 * scale, unit
        # 1 亿元 expressed in every other unit round-trips exactly
        assert normalize_unit(str(int(1e8 / scale)), unit) == 1e8, unit


def test_unit_scales_are_powers_of_ten():
    assert UNIT_SCALES == {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6, "亿元": 1e8}


def test_parse_numeric_enumerates_accounting_conventions():
    assert parse_numeric("1,234,567.89") == 1234567.89
    assert parse_numeric("(1,234)") == -1234.0          # parentheses = negative
    assert parse_numeric("（１，２３４）") == -1234.0      # full-width
    assert parse_numeric("1,234-") == -1234.0           # right-aligned sign
    assert parse_numeric("—") is None
    assert parse_numeric("N/A") is None
    assert parse_numeric("不适用") is None
    assert parse_numeric("") is None
    assert parse_numeric("1 234 567") == 1234567.0
    assert parse_numeric("营业收入") is None
    # A placeholder must never become 0.
    assert parse_numeric("—") != 0


def test_normalize_unit_refuses_unknown_or_missing_units():
    with pytest.raises(UnitNormalizationError):
        normalize_unit("100", None)                      # type: ignore[arg-type]
    with pytest.raises(UnitNormalizationError):
        normalize_unit("100", "美元素")
    with pytest.raises(UnitNormalizationError):
        normalize_unit("—", "元")
    # 万元 is a magnitude, not an alias of 元
    assert normalize_unit("1", "万元") != normalize_unit("1", "元")


# --------------------------------------------------------------------------- #
# §11.2 / §11.3 封闭算例：YTD 与 TTM（经桥接层，证明走的是现有引擎）
# --------------------------------------------------------------------------- #

def test_ytd_closed_form_yields_single_quarters_100_130_160_150():
    statements = [
        _statement("2025Q1", {"revenue": 100.0}),
        _statement("2025H1", {"revenue": 230.0}),
        _statement("2025 9M".replace(" ", ""), {"revenue": 390.0}),
        _statement("2025FY", {"revenue": 540.0}),
    ]
    derived = {p.fiscal_period: p.values["revenue"] for p in derive_single_quarters(statements)}
    assert derived == {"2025Q1": 100.0, "2025Q2": 130.0, "2025Q3": 160.0, "2025Q4": 150.0}


def test_ttm_closed_form_is_550():
    statements = [
        _statement("2023FY", {"revenue": 540.0}),
        _statement("2024H1", {"revenue": 230.0}),
        _statement("2023H1", {"revenue": 220.0}),
    ]
    ttm = {p.fiscal_period: p.values["revenue"] for p in derive_ttm(statements)}
    assert ttm["2024H1"] == 550.0                      # 230 + 540 − 220
    assert ttm["2023FY"] == 540.0                      # FY is its own TTM
    # The derivation must cite the exact periods it combined.
    source = next(p for p in derive_ttm(statements) if p.fiscal_period == "2024H1")
    assert source.source_periods == ("2024H1", "2023FY", "2023H1")


def test_engine_bridge_is_the_only_single_quarter_source():
    """CARD-02 §15: the bridge is present, and no local differencing exists."""
    source = (Path(__file__).parents[1] / "src" / "cn" / "normalization").read_text  # noqa: F841
    adapter = (Path(__file__).parents[1] / "src" / "cn" / "normalization" / "adapter.py").read_text(
        encoding="utf-8"
    )
    assert "FinancialPeriodEngine.derive_single_quarters" in adapter
    assert "FinancialPeriodEngine.derive_ttm" in adapter
    # A hand-rolled difference would show up as a subtraction inside the bridge.
    assert "_subtract" not in adapter
    assert "- previous" not in adapter


def test_missing_9m_leaves_q4_absent_instead_of_guessing():
    statements = [
        _statement("2025Q1", {"revenue": 100.0}),
        _statement("2025H1", {"revenue": 230.0}),
        _statement("2025FY", {"revenue": 540.0}),
    ]
    periods = {p.fiscal_period for p in derive_single_quarters(statements)}
    assert periods == {"2025Q1", "2025Q2"}             # no 9M → no Q3, no Q4


# --------------------------------------------------------------------------- #
# §11.4 stock 语义：PeriodEngineError 必须重抛，不得吞掉
# --------------------------------------------------------------------------- #

def test_balance_sheet_input_raises_period_engine_error():
    balance = [_statement("2025FY", {"total_assets": 1.0}, statement_type="balance")]
    with pytest.raises(PeriodEngineError):
        derive_single_quarters(balance, "balance")
    with pytest.raises(PeriodEngineError):
        derive_ttm(balance, "balance")


def test_mixed_scope_group_is_rejected_before_differencing():
    # A mixed 合并/母公司 group surfaces as two records for one fiscal period.
    mixed = [
        _statement("2025H1", {"revenue": 230.0}),
        _statement("2025H1", {"revenue": 999.0}),
    ]
    with pytest.raises(PeriodEngineError) as excinfo:
        derive_single_quarters(mixed, "income")
    assert "合并/母公司" in str(excinfo.value) or "重复期间" in str(excinfo.value)


def test_stock_and_flow_metrics_are_tagged():
    assert METRIC_INDEX["total_assets"].semantics.value == "stock"
    assert METRIC_INDEX["inventory"].semantics.value == "stock"
    assert METRIC_INDEX["revenue"].semantics.value == "flow"
    assert METRIC_INDEX["gross_profit"].semantics.value == "derived"
    assert METRIC_INDEX["raw_price"].semantics.value == "market"


# --------------------------------------------------------------------------- #
# §11.5 单位：表头优先 + 冲突告警；未声明单位 → fail closed
# --------------------------------------------------------------------------- #

def test_table_header_unit_wins_over_document_unit_with_warning():
    facts = _header_row(["项目", "本报告期"], unit="万元") + _row("营业收入", ["12,500"], row=1, unit="万元")
    result = normalize_financials(facts, _document(), default_unit="元")
    assert result.facts[0].value == 125000000.0        # 万元 wins
    assert result.facts[0].source_unit == "万元"
    assert any("冲突" in message for message in result.warnings), result.warnings


def test_undeclared_unit_is_dropped_not_assumed_to_be_yuan():
    facts = _header_row(["项目", "本报告期"], unit=None) + _row("营业收入", ["12,500"], row=1, unit=None)
    result = normalize_financials(facts, _document())
    assert result.facts == []
    assert any("未声明单位" in message for message in result.warnings), result.warnings


def test_non_cny_currency_is_preserved_without_conversion():
    facts = _header_row(["项目", "本报告期"], unit="元") + _row("营业收入", ["1,000"], row=1, unit="元")
    facts[-1].currency = "USD"
    result = normalize_financials(facts, _document())
    normalised = [fact for fact in result.facts if fact.metric == "revenue"]
    assert normalised[0].currency == "USD"              # labelled, never converted
    assert normalised[0].value == 1000.0


# --------------------------------------------------------------------------- #
# §11.6 period_kind 判定（摘要表双列 / 三大表主体 / 无法判定）
# --------------------------------------------------------------------------- #

def test_period_kind_of_statement_bodies():
    assert detect_period_kind("合并利润表", None, "annual").value == "YTD_CUMULATIVE"
    assert detect_period_kind("合并现金流量表", None, "annual").value == "YTD_CUMULATIVE"
    assert detect_period_kind("合并资产负债表", None, "annual").value == "POINT_IN_TIME"
    assert detect_period_kind("母公司资产负债表", None, "interim").value == "POINT_IN_TIME"


def test_summary_dual_columns_are_classified_by_column():
    # 三季报：本报告期 = 单季，年初至本报告期末 = 累计（指令 edge case 7）
    assert detect_period_kind("主要会计数据和财务指标", "本报告期", "quarterly").value == "SINGLE_QUARTER"
    assert detect_period_kind("主要会计数据和财务指标", "年初至本报告期末", "quarterly").value == "YTD_CUMULATIVE"
    # 通知/摘要表可能只给 parser 的内部 token
    assert detect_period_kind("主要会计数据", None, "quarterly", column_token="ytd").value == "YTD_CUMULATIVE"
    assert detect_period_kind("主要会计数据", None, "annual", column_token="current_period").value == "YTD_CUMULATIVE"


def test_summary_column_denotes_the_reporting_period_itself():
    """『本报告期』 = 本报告期覆盖的期间：年报/半年报为累计，季报为单季。"""
    assert detect_period_kind("主要会计数据和财务指标", "本报告期", "annual").value == "YTD_CUMULATIVE"
    assert detect_period_kind("主要会计数据和财务指标", "本报告期", "interim").value == "YTD_CUMULATIVE"
    assert detect_period_kind("主要会计数据和财务指标", "本报告期", "quarterly").value == "SINGLE_QUARTER"


def test_change_and_unknown_columns_never_get_a_kind():
    change = detect_period_kind("主要会计数据和财务指标", "本报告期比上年同期增减", "annual")
    assert change is None
    assert period_kind_decision("主要会计数据和财务指标", "本报告期比上年同期增减", "annual").warning
    unknown = detect_period_kind("主要会计数据和财务指标", None, "annual")
    assert unknown is None
    assert "未声明" in (period_kind_decision("主要会计数据和财务指标", None, "annual").warning or "")
    # 公告 / 附注 / 未识别表 → 不推断
    assert detect_period_kind("关于董事会决议的公告", None, "annual") is None
    assert detect_period_kind("", None, "annual") is None


def test_prior_column_derivation_is_deterministic():
    assert previous_period_key("2025FY") == "2024FY"
    assert previous_period_key("2025 9M".replace(" ", "")) == "2024 9M".replace(" ", "")
    assert previous_period_key("2025FY", point_in_time=True) == "2024FY"
    assert previous_period_key("20259M", point_in_time=True) == "2024FY"


# --------------------------------------------------------------------------- #
# §11.7-§11.9 期间键 / 口径 / 未知指标
# --------------------------------------------------------------------------- #

def test_bad_period_keys_raise_instead_of_guessing():
    for bad in ("FY2024", "2024Q1_YTD", "Q1 2024", "2024-01-31", "2024年Q2", ""):
        with pytest.raises(ValueError):
            canonical_period(bad, "annual")


def test_canonical_period_accepts_declared_forms():
    assert canonical_period("2025FY", "annual") == "2025FY"
    assert canonical_period("2025", "annual") == "2025FY"
    assert canonical_period("2025年", "interim") == "2025H1"
    assert canonical_period("2025-09-30", "quarterly") == "20259M"
    assert canonical_period("2025年12月31日", "annual") == "2025FY"
    assert canonical_period("2024年度", "annual") == "2024FY"
    assert canonical_period("2024前三季度", "quarterly") == "20249M"
    # A bare year in a quarterly report is genuinely ambiguous.
    with pytest.raises(ValueError):
        canonical_period("2025", "quarterly")


def test_both_scopes_are_kept_separately():
    facts = [
        _fact("营业收入", "1,000", table="合并利润表", row=1, column=1, scope="合并"),
        _fact("营业收入", "900", table="母公司利润表", row=1, column=1, scope="母公司", section="母公司利润表"),
    ]
    result = normalize_financials(facts, _document())
    assert {fact.scope for fact in result.facts} == {Scope.CONSOLIDATED, Scope.PARENT}
    assert len({group.group.group_id for group in result.groups}) <= 2
    assert detect_scope("合并资产负债表") is Scope.CONSOLIDATED
    assert detect_scope("母公司利润表") is Scope.PARENT
    assert detect_scope("合并及母公司资产负债表") is None


def test_unknown_metric_gets_placeholder_and_warning():
    match = resolve_metric("销售费用")
    assert not match.known
    assert match.key == "metric_unknown_销售费用"
    assert "不在 §7.1" in (match.reason or "")

    facts = _header_row(["项目", "本报告期"]) + _row("销售费用", ["120,000"], row=1)
    result = normalize_financials(facts, _document())
    assert [fact.metric for fact in result.facts] == ["metric_unknown_销售费用"]
    assert result.facts[0].metric_known is False
    assert result.statements == []                       # 下游不得引用未知 metric
    assert any("未知指标标签" in message for message in result.warnings)


def test_net_profit_levels_never_mix():
    """含少数股东 vs 归母 must land on different statement keys (§7.1)."""
    assert statement_value_key("net_profit") == "net_profit_incl_minority"
    assert statement_value_key("net_profit_parent") == "net_profit"
    assert resolve_metric("净利润").key == "net_profit"
    assert resolve_metric("归属于母公司股东的净利润").key == "net_profit_parent"
    assert resolve_metric("归属于上市公司股东的净利润").key == "net_profit_parent"
    assert resolve_metric("归母净利润").key == "net_profit_parent"
    assert resolve_metric("扣除非经常性损益后归属于母公司股东的净利润").key == "net_profit_deducted"

    facts = (
        _header_row(["项目", "本期金额"])
        + _row("净利润", ["345,678"], row=1)
        + _row("归属于母公司股东的净利润", ["330,000"], row=2)
    )
    result = normalize_financials(facts, _document())
    projected = result.statements[0].values
    assert projected["net_profit_incl_minority"] == 345678.0
    assert projected["net_profit"] == 330000.0
    assert projected["net_profit"] != projected["net_profit_incl_minority"]


def test_ratio_and_contra_labels_are_rejected():
    for label in ("毛利率", "营业收入同比增长率", "存货跌价准备", "基本每股收益", "占营业收入的比例"):
        match = resolve_metric(label)
        assert not match.known, label
        assert match.reason, label


def test_narrower_subtotals_never_impersonate_their_parent_total():
    """`流动资产合计` contains `资产合计`; it must not become `total_assets`.

    Found by CARD-22's F1 check the first time it ran against a filing carrying
    the standard 资产负债表 sub-total rows: longest-synonym-wins mapped both
    流动资产合计 and 非流动资产合计 onto ``total_assets``, so the projection saw
    three candidates for one metric and CARD-02's precedence (document order)
    handed the metric to a *component* — silently replacing the printed 资产总计.
    The guard is narrow: only "prefix + sub-total marker + matched synonym is a
    proper suffix" is rejected, so ``归属于上市公司股东的净利润`` still resolves.
    """
    for label in ("流动资产合计", "非流动资产合计", "流动负债合计", "非流动负债合计"):
        match = resolve_metric(label)
        assert not match.known, label
        assert "小计" in (match.reason or ""), label

    # The genuine totals keep resolving, exactly and by longest match.
    assert resolve_metric("资产总计").key == "total_assets"
    assert resolve_metric("负债合计").key == "total_liabilities"
    assert resolve_metric("归属于上市公司股东的净利润").key == "net_profit_parent"

    # End to end: with both sub-totals present, 资产总计 must survive as
    # total_assets rather than being displaced by 流动资产合计.
    facts = (
        _header_row(["项目", "期末余额"], table="合并资产负债表")
        + _row("流动资产合计", ["6,000,000"], row=1, table="合并资产负债表")
        + _row("非流动资产合计", ["4,000,000"], row=2, table="合并资产负债表")
        + _row("资产总计", ["10,000,000"], row=3, table="合并资产负债表")
    )
    result = normalize_financials(facts, _document())
    projected = result.statements[0].values
    assert projected["total_assets"] == 10000000.0


def test_metric_synonyms_project_onto_existing_statement_keys():
    expected = {
        "revenue": "revenue",
        "operating_cost": "operating_cost",
        "cfo": "operating_cash_flow",
        "cfo_minus_capex": "free_cash_flow",
        "equity_parent": "equity",
        "diluted_shares": "total_shares",
        "total_assets": "total_assets",
    }
    for metric, value_key in expected.items():
        assert statement_value_key(metric) == value_key, metric
    # market metrics never enter a financial statement
    assert statement_value_key("raw_price") is None
    assert statement_value_key("peers") is None


# --------------------------------------------------------------------------- #
# §11.10 摘要表单季列：fact 保留、报表不投影（引擎是单季唯一来源）
# --------------------------------------------------------------------------- #

def test_disclosed_single_quarter_is_kept_but_not_projected():
    table = "主要会计数据和财务指标"
    facts = (
        _header_row(
            ["项目", "本报告期", "年初至本报告期末"],
            table=table, scope=None, column_labels={1: "current_period", 2: "ytd"},
        )
        + _row("营业收入", ["1,234,567", "2,345,678"], row=1, table=table, scope=None)
    )
    document = _document(document_type="quarterly", report_period="20259M")
    result = normalize_financials(facts, document)

    kinds = {fact.period_kind for fact in result.facts}
    assert kinds == {PeriodKind.SINGLE_QUARTER, PeriodKind.YTD_CUMULATIVE}
    single = next(f for f in result.facts if f.period_kind is PeriodKind.SINGLE_QUARTER)
    assert single.value == 1234567.0
    # The engine derives single quarters by differencing: the disclosed one is
    # cross-check material, never a statement input.
    assert result.statements[0].fiscal_period == "20259M"
    assert result.statements[0].values["revenue"] == 2345678.0
    assert any("披露的单季值不投影" in message for message in result.warnings)


# --------------------------------------------------------------------------- #
# 集成：CARD-01 合成年报 → CARD-02（含 bbox/页码回链）
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    root = tmp_path_factory.mktemp("card02") / "documents"
    return DocumentRegistry(root=root, allow_roots=[FIXTURES])


def _normalize_fixture(registry, filename: str, declared_type: str = "annual"):
    parsed = registry.ingest(FIXTURES / filename, declared_type=declared_type, source="admin_import")
    return parsed, normalize_financials(parsed.facts, parsed.document)


def test_card01_annual_report_normalizes_with_backlinks(registry):
    parsed, result = _normalize_fixture(registry, "annual_01_profit_continued.pdf")
    assert result.report_period == "2025FY"
    assert result.fact_count > 0

    revenue = [fact for fact in result.facts if fact.metric == "revenue"]
    assert revenue, [fact.metric for fact in result.facts]
    for fact in revenue:
        assert fact.value == float(fact.raw_value.replace(",", ""))   # 元 → 元
        assert fact.period_key == "2025FY"
        assert fact.period_kind is PeriodKind.YTD_CUMULATIVE
        assert fact.scope is Scope.CONSOLIDATED
        # back-link to CARD-01 (card §12)
        assert fact.document_id == parsed.document.document_id
        assert fact.page and fact.bbox
        assert fact.source_fact_id

    # 归母 vs 含少数股东 stay separate through the whole chain
    values = result.statements[0].values
    assert values["net_profit"] == 330000.0
    assert values["net_profit_incl_minority"] == 345678.0
    assert values["revenue"] == 1234567.0


def test_card01_wan_unit_report_converts_to_yuan(registry):
    _, result = _normalize_fixture(registry, "annual_02_balance_unit_wan.pdf")
    receivables = [fact for fact in result.facts if fact.metric == "receivables"]
    assert receivables, [fact.metric for fact in result.facts]
    assert {fact.source_unit for fact in receivables} == {"万元"}
    latest = next(fact for fact in receivables if fact.period_key == "2024FY")
    assert latest.value == 83000000.0                   # 8,300 万元
    # 期初余额 → previous fiscal year end, derived deterministically
    prior = next(fact for fact in receivables if fact.period_key == "2023FY")
    assert prior.value == 79000000.0
    assert prior.period_kind is PeriodKind.POINT_IN_TIME


def test_card01_scanned_page_yields_no_invented_values(registry):
    parsed, result = _normalize_fixture(registry, "annual_03_cashflow_with_scan.pdf")
    pages = {fact.page for fact in result.facts}
    assert 3 not in pages                               # the scan page contributes nothing
    assert result.facts, "the text pages must still normalize"


def test_derived_metrics_use_the_same_primary_disclosure_as_the_projection(registry):
    _, result = _normalize_fixture(registry, "annual_01_profit_continued.pdf")
    values = result.statements[0].values
    gross = next(fact for fact in result.facts if fact.metric == "gross_profit")
    assert gross.value == values["revenue"] - values["operating_cost"]
    assert gross.source_fact_id.startswith("derived(")
    assert any("派生指标 gross_profit" in message for message in result.warnings)


def test_conflicting_dual_columns_are_reported_not_silently_picked(registry):
    """The synthetic fixture repeats 营业收入 in both columns with different
    values; the leftmost wins and the conflict is surfaced for CARD-13."""
    _, result = _normalize_fixture(registry, "annual_01_profit_continued.pdf")
    conflicts = [message for message in result.warnings if "跨来源数值不一致" in message]
    assert conflicts, result.warnings
    assert any("revenue" in message for message in conflicts)


def test_projection_is_deterministic(registry):
    parsed = registry.ingest(FIXTURES / "annual_01_profit_continued.pdf", declared_type="annual", source="admin_import")
    first = normalize_financials(parsed.facts, parsed.document)
    second = normalize_financials(parsed.facts, parsed.document)
    dump = lambda result: json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)  # noqa: E731
    assert dump(first) == dump(second)
    assert [fact.normalized_fact_id for fact in first.facts] == [
        fact.normalized_fact_id for fact in second.facts
    ]


def test_disclosed_gross_profit_beats_derivation():
    facts = (
        _header_row(["项目", "本期金额"])
        + _row("营业收入", ["1,000"], row=1)
        + _row("营业成本", ["600"], row=2)
        + _row("毛利", ["380"], row=3)
    )
    result = normalize_financials(facts, _document())
    gross = [fact for fact in result.facts if fact.metric == "gross_profit"]
    assert [fact.value for fact in gross] == [380.0]     # disclosed value wins
    assert not any("派生指标 gross_profit" in message for message in result.warnings)


def test_normalize_document_returns_the_card_contract():
    facts = _header_row(["项目", "本期金额"]) + _row("营业收入", ["1,000"], row=1)
    from src.cn.normalization import normalize_document

    normalized, warnings = normalize_document(facts, _document())
    assert isinstance(normalized, list) and isinstance(warnings, list)
    assert normalized[0].metric == "revenue"
    assert normalized[0].financial_statement_id is not None
    assert all(isinstance(message, str) for message in warnings)


def test_derive_periods_groups_skip_balance_sheets_without_error():
    facts = _header_row(["项目", "期末余额"]) + _row("资产总计", ["5,000"], row=1, table="合并资产负债表", scope="合并")
    result = normalize_financials(facts, _document())
    assert result.single_quarters == [] and result.ttm == []
    assert result.statements[0].statement_type == "balance"


def test_version_signals_are_labelled():
    assert detect_version("本公告为更正公告") == detect_version("更正后")
    assert detect_version("关于前期会计差错更正的公告").value == "CORRECTED"
    assert detect_version("追溯重述上年同期数据").value == "RESTATED"
    assert detect_version("年度报告正文").value == "AS_REPORTED"

    facts = _header_row(["项目", "本期金额"]) + _row("营业收入", ["1,000"], row=1)
    restated = normalize_financials(facts, _document(notes=["本报告为追溯重述后数据"]))
    assert restated.facts[0].version.value == "RESTATED"
    assert restated.statements[0].is_restated is True


# --------------------------------------------------------------------------- #
# §12 证据账本桥接 + Agent 工具信封
# --------------------------------------------------------------------------- #

def test_normalized_facts_reach_the_evidence_ledger_with_backlinks(registry):
    from src.cn.evidence import EvidenceLedger
    from src.cn.normalization.evidence_bridge import normalization_jumps, register_normalized_evidence

    parsed = registry.ingest(FIXTURES / "annual_02_balance_unit_wan.pdf", declared_type="annual", source="admin_import")
    result = normalize_financials(parsed.facts, parsed.document)

    ledger = EvidenceLedger(snapshot_id=parsed.document.document_id)
    payload = register_normalized_evidence(parsed.document, result, ledger=ledger)

    assert payload["evidence_count"] == result.fact_count
    assert payload["evidence_skipped"] == []
    assert payload["evidence_snapshot_id"] == parsed.document.document_id

    fact = result.facts[0]
    record = ledger.get(fact.normalized_fact_id)
    assert record.provider == "normalization"
    assert record.value == fact.value
    assert record.unit == "元"
    assert record.fiscal_period == fact.period_key
    assert record.period_basis == fact.period_kind.value
    assert parsed.document.document_id in record.field_path
    # a normalization-sourced evidence id must never collide with a raw doc fact id
    assert record.evidence_id.startswith("nf_")

    jumps = normalization_jumps(parsed.document, result)
    assert jumps[fact.normalized_fact_id]["page"] == fact.page
    assert jumps[fact.normalized_fact_id]["bbox"] == list(fact.bbox)
    assert jumps[fact.normalized_fact_id]["source_unit"] == "万元"
    assert jumps[fact.normalized_fact_id]["value"] == fact.value


def test_normalize_tool_envelope_is_offline_and_traceable(tmp_path):
    import asyncio
    import json

    from src.agents.tools.cn_tools import NormalizeCnFinancialsTool

    registry = DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])
    parsed = registry.ingest(FIXTURES / "annual_01_profit_continued.pdf", declared_type="annual", source="admin_import")

    tool = NormalizeCnFinancialsTool(registry)
    payload = json.loads(asyncio.run(tool.execute(document_id=parsed.document.document_id)))

    assert payload["status"] == "ok"
    assert payload["unit"] == "元"
    assert payload["report_period"] == "2025FY"
    assert payload["statements"][0]["values"]["revenue"] == 1234567.0
    assert payload["engine"].startswith("FinancialPeriodEngine")

    missing = json.loads(asyncio.run(tool.execute(document_id="does-not-exist")))
    assert missing["status"] == "error"
    assert missing["error_code"] == "document_not_found"

    blank = json.loads(asyncio.run(tool.execute()))
    assert blank["status"] == "error"
    assert blank["error_code"] == "normalization_missing_argument"


def test_normalize_tool_registers_evidence_with_bbox_backlink(tmp_path):
    import asyncio
    import json

    from src.agents.tools.cn_tools import NormalizeCnFinancialsTool

    registry = DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])
    parsed = registry.ingest(FIXTURES / "annual_02_balance_unit_wan.pdf", declared_type="annual", source="admin_import")

    tool = NormalizeCnFinancialsTool(registry)
    payload = json.loads(
        asyncio.run(tool.execute(document_id=parsed.document.document_id, register_evidence=True))
    )
    assert payload["evidence_count"] == len(payload["evidence_ids"]) > 0
    assert payload["evidence_snapshot_id"] == parsed.document.document_id
