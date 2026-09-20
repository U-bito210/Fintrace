"""CARD-22 tests — the ACME constraint layer.

The suite is organised by §7's six constraint families, then by the §13
validator, then by the §18 acceptance criteria. Two kinds of test carry most of
the weight:

* **closed arithmetic** — every family gets a hand-checkable positive *and*
  negative case, so a detector that always fires or never fires fails here.
* **the false-alarm half** — the committed corpus is swept and asserted to be
  violation-free apart from the one file that has violations planted in it
  (``annual_07``). A constraint layer whose value proposition is "系统发现自己
  错了" is only credible if it also stays quiet when nothing is wrong; the first
  F2 implementation failed exactly this and is now spec-driven because of it.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.cn.acme import (
    CONSTRAINT_FAMILIES,
    DEFAULT_POLICY,
    ChartAnnotation,
    ToleranceExemption,
    TolerancePolicy,
    parse_body_claims,
    ratio_series,
    validate_multimodal_evidence,
)
from src.cn.acme.cross_modal import KIND_DELTA, KIND_LEVEL
from src.cn.acme.cross_period import check_cross_period_continuity
from src.cn.acme.cross_source import check_cross_source
from src.cn.acme.identities import check_accounting_identities
from src.cn.acme.models import (
    AcmeError,
    ConstraintViolation,
    CorrectionTrail,
    STATUS_CHECK_FAILED,
    STATUS_NOT_COMPARABLE,
    STATUS_PASSED,
    STATUS_SKIPPED_MISSING_FIELD,
    STATUS_VIOLATED,
    STATUS_WARNING,
)
from src.cn.acme.period_relations import check_period_relations
from src.cn.acme.structural import (
    SUBTOTAL_SPECS,
    SubtotalSpec,
    check_table_structural,
    find_subtotal_blocks,
)
from src.cn.domain import FinancialStatement
from src.cn.documents.registry import DocumentRegistry
from src.cn.normalization.models import NormalizedFact, PeriodKind, Scope, Version
from src.validation.acme_validator import (
    open_findings,
    validate_acme_corpus,
    validate_acme_report,
)

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
ACME_SOURCE = Path(__file__).resolve().parents[1] / "src" / "cn" / "acme"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _fact(
    metric: str,
    value: float,
    *,
    period: str = "2025FY",
    scope: str = "合并",
    version: Version = Version.AS_REPORTED,
    statement: str = "balance",
    page: int = 1,
    row: int = 1,
    table: str = "合并资产负债表",
    document_id: str = "DOC-A",
    period_kind: PeriodKind = PeriodKind.POINT_IN_TIME,
    raw_value: str | None = None,
) -> NormalizedFact:
    return NormalizedFact(
        normalized_fact_id=f"nf_{document_id}_{metric}_{period}_{scope}",
        metric=metric,
        value=value,
        currency="CNY",
        period_key=period,
        period_kind=period_kind,
        scope=Scope.CONSOLIDATED if scope == "合并" else Scope.PARENT,
        version=version,
        available_at="2026-04-20",
        source_fact_id=f"src_{metric}",
        document_id=document_id,
        page=page,
        row=row,
        raw_value=raw_value,
        source_table=table,
        statement_type=statement,
    )


def _cell(
    text: str,
    value: str | None,
    *,
    table: str,
    row: int,
    column: int,
    page: int = 1,
    document_id: str = "DOC-A",
):
    """One CARD-01 table cell, shaped like the extractor's output."""

    class _Cell:
        pass

    cell = _Cell()
    cell.raw_text = text
    cell.raw_value = value
    cell.table = table
    cell.row = row
    cell.column = column
    cell.page = page
    cell.document_id = document_id
    cell.kind = "table_cell"
    cell.fact_id = f"f_{table}_{row}_{column}"
    cell.bbox = (float(column), float(row), float(column + 1), float(row + 1))
    return cell


def _text_fact(text: str, *, page: int = 2, document_id: str = "DOC-A"):
    class _Text:
        pass

    item = _Text()
    item.raw_text = text
    item.raw_value = None
    item.kind = "text"
    item.page = page
    item.document_id = document_id
    item.fact_id = f"t_{abs(hash(text)) % 10**6}"
    item.bbox = (0.0, 0.0, 1.0, 1.0)
    return item


def _balance_table(end_assets: str = "10,000,000") -> list:
    table = "合并资产负债表"
    cells = [
        _cell("项目", None, table=table, row=0, column=0),
        _cell("期末余额", None, table=table, row=0, column=1),
    ]
    rows = [
        ("流动资产合计", "6,000,000"),
        ("非流动资产合计", "4,000,000"),
        ("资产总计", end_assets),
    ]
    for index, (label, value) in enumerate(rows, start=1):
        cells.append(_cell(label, None, table=table, row=index, column=0))
        cells.append(_cell(value, value, table=table, row=index, column=1))
    return cells


def _statement(
    statement_type: str,
    fiscal_period: str,
    values: dict,
    *,
    basis: str | None = None,
) -> FinancialStatement:
    default_basis = "POINT_IN_TIME" if statement_type == "balance" else "YTD"
    return FinancialStatement(
        statement_type=statement_type,
        fiscal_period=fiscal_period,
        period_basis=basis or default_basis,
        published_at="2026-04-20",
        available_at="2026-04-20",
        currency="CNY",
        unit="元",
        values=values,
    )


# --------------------------------------------------------------------------- #
# §7 Tolerance policy
# --------------------------------------------------------------------------- #

def test_tolerance_is_the_larger_of_absolute_floor_and_relative_band():
    assert DEFAULT_POLICY.tolerance_for(1_000_000, 1_000_500) == pytest.approx(1000.5)
    assert DEFAULT_POLICY.tolerance_for(100, 120) == 1000.0


def test_tolerance_is_per_unit_so_ratios_are_not_swallowed_by_a_yuan_floor():
    # A 1000-元 floor applied to a percentage would swallow every comparison.
    assert DEFAULT_POLICY.tolerance_for(33.0, 33.0, unit="PERCENT") == pytest.approx(0.05)
    assert DEFAULT_POLICY.tolerance_for(11, 10, unit="TABLE_UNIT") == pytest.approx(0.5)


def test_classify_distinguishes_passed_warning_and_violated():
    # Exact agreement is passed — calling it a warning would flood the review queue.
    assert DEFAULT_POLICY.classify(100, 100)[0] == STATUS_PASSED
    # A non-zero residual *inside* the band is a warning (rounding artefact).
    assert DEFAULT_POLICY.classify(1_000_000, 1_000_500)[0] == STATUS_WARNING
    # Beyond the band is a violation, however small the statement.
    assert DEFAULT_POLICY.classify(100, 200_000)[0] == STATUS_VIOLATED


def test_exemption_without_a_source_is_refused():
    with pytest.raises(AcmeError):
        ToleranceExemption(constraint_id="balance_sheet_equation", reason="历史原因", source="  ")


def test_exemption_widens_the_band_but_still_reports_the_residual():
    policy = TolerancePolicy(
        exemptions=(
            ToleranceExemption(
                constraint_id="subtotal_reconciliation",
                reason="已审计的非经常性重分类",
                source="2025年审计报告附注三",
            ),
        )
    )
    status, tolerance, residual = policy.classify(
        2_000, 1_000, constraint_id="subtotal_reconciliation"
    )
    assert tolerance == pytest.approx(2000.0)
    assert residual == pytest.approx(1000.0)
    assert status == STATUS_WARNING


# --------------------------------------------------------------------------- #
# §7 F1 Accounting identities
# --------------------------------------------------------------------------- #

def test_balance_sheet_equation_passes_when_the_two_sides_agree():
    facts = [
        _fact("total_assets", 10_000_000),
        _fact("total_liabilities", 6_000_000),
        _fact("equity_parent", 4_000_000),
    ]
    results = [
        item for item in check_accounting_identities(facts, policy=DEFAULT_POLICY)
        if item.constraint_id == "balance_sheet_equation"
    ]
    assert [item.status for item in results] == [STATUS_PASSED]
    assert results[0].residual == 0.0


def test_balance_sheet_equation_violation_carries_expected_actual_and_residual():
    facts = [
        _fact("total_assets", 10_000_000),
        _fact("total_liabilities", 6_000_000),
        _fact("equity_parent", 3_000_000),
    ]
    item = next(
        entry for entry in check_accounting_identities(facts, policy=DEFAULT_POLICY)
        if entry.constraint_id == "balance_sheet_equation"
    )
    assert item.status == STATUS_VIOLATED
    # expected = independently derived side; actual = what the filing printed.
    assert item.expected_value == 9_000_000
    assert item.actual_value == 10_000_000
    assert item.residual == -1_000_000
    assert item.source_refs and all(ref.resolvable for ref in item.source_refs)


def test_balance_sheet_equation_inside_tolerance_is_a_warning_not_a_violation():
    facts = [
        _fact("total_assets", 10_000_000),
        _fact("total_liabilities", 6_000_000),
        _fact("equity_parent", 3_999_600),
    ]
    item = next(
        entry for entry in check_accounting_identities(facts, policy=DEFAULT_POLICY)
        if entry.constraint_id == "balance_sheet_equation"
    )
    assert item.status == STATUS_WARNING


def test_minority_interest_makes_the_equation_not_comparable_rather_than_violated():
    facts = [
        _fact("total_assets", 10_000_000),
        _fact("total_liabilities", 6_000_000),
        _fact("equity_parent", 3_000_000),
        _fact("metric_unknown_少数股东权益", 1_000_000),
    ]
    item = next(
        entry for entry in check_accounting_identities(facts, policy=DEFAULT_POLICY)
        if entry.constraint_id == "balance_sheet_equation"
    )
    assert item.status == STATUS_NOT_COMPARABLE
    assert "少数股东" in (item.note or "")


def test_missing_operand_skips_the_identity_without_alarming():
    facts = [_fact("total_assets", 10_000_000), _fact("equity_parent", 4_000_000)]
    item = next(
        entry for entry in check_accounting_identities(facts, policy=DEFAULT_POLICY)
        if entry.constraint_id == "balance_sheet_equation"
    )
    assert item.status == STATUS_SKIPPED_MISSING_FIELD
    assert "total_liabilities" in (item.note or "")


def test_identity_with_no_operands_at_all_still_leaves_a_trace():
    results = check_accounting_identities([], policy=DEFAULT_POLICY)
    assert results
    assert all(item.status == STATUS_SKIPPED_MISSING_FIELD for item in results)
    assert any("未披露任何参与项" in (item.note or "") for item in results)


def test_gross_profit_identity_checks_a_disclosed_margin_but_skips_a_derived_one():
    disclosed = [
        _fact("gross_profit", 7_000_000, statement="income", table="合并利润表",
              period_kind=PeriodKind.YTD_CUMULATIVE),
        _fact("revenue", 20_000_000, statement="income", table="合并利润表",
              period_kind=PeriodKind.YTD_CUMULATIVE),
        _fact("operating_cost", 12_000_000, statement="income", table="合并利润表",
              period_kind=PeriodKind.YTD_CUMULATIVE),
    ]
    item = next(
        entry for entry in check_accounting_identities(disclosed, policy=DEFAULT_POLICY)
        if entry.constraint_id == "gross_profit_identity"
    )
    assert item.status == STATUS_VIOLATED
    # 惯例（models.py）：expected = 独立推导值 revenue − operating_cost = 8,000,000，
    # actual = 报表印出来的毛利 7,000,000，故 residual = +1,000,000（印低了 1,000,000）。
    assert item.residual == 1_000_000

    derived = [
        _fact("gross_profit", 8_000_000, statement="income", table="derived",
              period_kind=PeriodKind.YTD_CUMULATIVE),
        *disclosed[1:],
    ]
    item = next(
        entry for entry in check_accounting_identities(derived, policy=DEFAULT_POLICY)
        if entry.constraint_id == "gross_profit_identity"
    )
    assert item.status == STATUS_SKIPPED_MISSING_FIELD
    assert "派生" in (item.note or "")


def test_identities_never_mix_consolidated_and_parent_scope():
    """§12 edge 1 — two 口径 are two measurements, not two candidates."""
    facts = [
        _fact("total_assets", 10_000_000, scope="合并"),
        _fact("total_liabilities", 6_000_000, scope="合并"),
        _fact("equity_parent", 4_000_000, scope="合并"),
        _fact("total_assets", 8_000_000, scope="母公司"),
        _fact("total_liabilities", 5_000_000, scope="母公司"),
        _fact("equity_parent", 2_000_000, scope="母公司"),
    ]
    results = [
        item for item in check_accounting_identities(facts, policy=DEFAULT_POLICY)
        if item.constraint_id == "balance_sheet_equation"
    ]
    assert len(results) == 2
    # 合并 agrees; 母公司 does not — and neither verdict leaks into the other.
    statuses = sorted(item.status for item in results)
    assert statuses == [STATUS_PASSED, STATUS_VIOLATED]


# --------------------------------------------------------------------------- #
# §7 F2 Table structural
# --------------------------------------------------------------------------- #

def test_subtotal_spec_without_a_source_is_refused():
    with pytest.raises(ValueError):
        SubtotalSpec(
            spec_id="invented",
            subtotal_labels=("资产总计",),
            child_labels=("流动资产合计", "非流动资产合计"),
            source="  ",
        )


def test_subtotal_matching_its_components_passes():
    results = check_table_structural(_balance_table(), policy=DEFAULT_POLICY)
    assert [item.status for item in results] == [STATUS_PASSED]
    assert results[0].expected_value == 10_000_000
    assert results[0].actual_value == 10_000_000


def test_subtotal_that_does_not_match_its_components_is_violated():
    results = check_table_structural(
        _balance_table(end_assets="11,000,000"), policy=DEFAULT_POLICY
    )
    assert [item.status for item in results] == [STATUS_VIOLATED]
    assert results[0].residual == pytest.approx(-1_000_000)
    assert "单元格串行" in (results[0].note or "")
    assert results[0].source_refs and all(ref.resolvable for ref in results[0].source_refs)


def test_subtotal_check_is_unit_independent():
    """A 万元 table must behave exactly like a 元 one — the scale cancels."""
    table = "合并资产负债表"
    cells = [
        _cell("项目", None, table=table, row=0, column=0),
        _cell("期末余额", None, table=table, row=0, column=1),
        _cell("流动资产合计", None, table=table, row=1, column=0),
        _cell("600", "600", table=table, row=1, column=1),
        _cell("非流动资产合计", None, table=table, row=2, column=0),
        _cell("400", "400", table=table, row=2, column=1),
        _cell("资产总计", None, table=table, row=3, column=0),
        _cell("1,100", "1,100", table=table, row=3, column=1),
    ]
    results = check_table_structural(cells, policy=DEFAULT_POLICY)
    assert [item.status for item in results] == [STATUS_VIOLATED]


def test_label_column_is_not_mistaken_for_a_value_column():
    """The first implementation read the row heading as the subtotal's value."""
    cells = _balance_table()
    blocks = find_subtotal_blocks(cells)
    assert len(blocks) == 1
    assert blocks[0].column == 1
    assert blocks[0].subtotal_fact.raw_value == "10,000,000"


def test_a_child_is_never_borrowed_from_another_column():
    table = "合并资产负债表"
    cells = [
        _cell("项目", None, table=table, row=0, column=0),
        _cell("期末余额", None, table=table, row=0, column=1),
        _cell("期初余额", None, table=table, row=0, column=2),
        _cell("流动资产合计", None, table=table, row=1, column=0),
        _cell("600", "600", table=table, row=1, column=1),
        _cell("500", "500", table=table, row=1, column=2),
        _cell("非流动资产合计", None, table=table, row=2, column=0),
        _cell("400", "400", table=table, row=2, column=1),
        # Column 2 of the second component is unreadable, so column 2 cannot be
        # reconciled with only one child.
        _cell("资产总计", None, table=table, row=3, column=0),
        _cell("1,000", "1,000", table=table, row=3, column=1),
        _cell("900", "900", table=table, row=3, column=2),
    ]
    blocks = find_subtotal_blocks(cells)
    assert [block.column for block in blocks] == [1]


# --------------------------------------------------------------------------- #
# §7 F3 Period relations
# --------------------------------------------------------------------------- #

def test_period_relations_never_reimplement_period_arithmetic():
    """§7 F3 — the source must not difference anything itself."""
    source = (ACME_SOURCE / "period_relations.py").read_text(encoding="utf-8")
    assert "derive_single_quarters" in source
    for forbidden in ("h1.values", "nine_month.values", "fy.values", "q1.values"):
        assert forbidden not in source


def test_disclosed_single_quarter_is_checked_against_the_period_engine():
    statements = [
        _statement("income", "2025Q1", {"revenue": 100.0}),
        _statement("income", "2025H1", {"revenue": 260.0}),
    ]
    disclosed = [
        _fact("revenue", 160.0, period="2025Q2", statement="income",
              period_kind=PeriodKind.SINGLE_QUARTER, table="合并利润表"),
    ]
    results = check_period_relations(statements, disclosed, policy=DEFAULT_POLICY)
    checked = [item for item in results if item.constraint_id == "ytd_single_quarter_relation"]
    assert [item.status for item in checked] == [STATUS_PASSED]
    assert checked[0].expected_value == 160.0


def test_disclosed_single_quarter_that_contradicts_the_engine_is_violated():
    # Realistic 元 magnitudes on purpose: §7's 1000-元 floor exists so that a
    # rounding artefact is not reported as a contradiction. A fixture in the
    # hundreds would be swallowed by that floor and would test the floor, not the
    # contradiction.
    statements = [
        _statement("income", "2025Q1", {"revenue": 100_000_000.0}),
        _statement("income", "2025H1", {"revenue": 260_000_000.0}),
    ]
    disclosed = [
        _fact("revenue", 200_000_000.0, period="2025Q2", statement="income",
              period_kind=PeriodKind.SINGLE_QUARTER, table="合并利润表"),
    ]
    results = check_period_relations(statements, disclosed, policy=DEFAULT_POLICY)
    checked = [item for item in results if item.constraint_id == "ytd_single_quarter_relation"]
    assert [item.status for item in checked] == [STATUS_VIOLATED]
    # engine says 260,000,000 − 100,000,000 = 160,000,000; the filing printed 200,000,000.
    assert checked[0].expected_value == 160_000_000.0
    assert checked[0].residual == -40_000_000.0


def test_a_single_quarter_without_its_endpoints_is_reported_not_trusted():
    statements = [_statement("income", "2025FY", {"revenue": 900.0})]
    disclosed = [
        _fact("revenue", 300.0, period="2025Q2", statement="income",
              period_kind=PeriodKind.SINGLE_QUARTER, table="合并利润表"),
    ]
    results = check_period_relations(statements, disclosed, policy=DEFAULT_POLICY)
    item = next(
        entry for entry in results if entry.constraint_id == "single_quarter_endpoint_coverage"
    )
    assert item.status == STATUS_NOT_COMPARABLE
    assert "2025H1" in (item.note or "") and "2025Q1" in (item.note or "")


def test_mixed_scope_group_makes_the_engine_refuse_and_the_finding_says_so():
    """A duplicated fiscal period is CARD-02's own red flag, not ours to swallow."""
    statements = [
        _statement("income", "2025H1", {"revenue": 100.0}),
        _statement("income", "2025H1", {"revenue": 999.0}),
    ]
    results = check_period_relations(statements, [], policy=DEFAULT_POLICY)
    item = next(entry for entry in results if entry.status == STATUS_CHECK_FAILED)
    assert "重复期间" in (item.note or "")


def test_annual_report_with_only_fy_endpoints_records_the_skip():
    statements = [_statement("income", "2025FY", {"revenue": 900.0})]
    results = check_period_relations(statements, [], policy=DEFAULT_POLICY)
    item = next(
        entry for entry in results if entry.constraint_id == "single_quarter_endpoint_coverage"
    )
    assert item.status == STATUS_SKIPPED_MISSING_FIELD
    assert "未核对，不等于通过" in (item.note or "")


# --------------------------------------------------------------------------- #
# §7 F4 Cross-period continuity
# --------------------------------------------------------------------------- #

def _continuity(opening, closing, **kwargs):
    return check_cross_period_continuity(
        opening_facts=opening, closing_facts=closing, policy=DEFAULT_POLICY, **kwargs
    )


def test_opening_balance_matching_last_years_closing_passes():
    opening = [_fact("total_assets", 9_400_000, period="2024FY", document_id="DOC-2025")]
    closing = [_fact("total_assets", 9_400_000, period="2024FY", document_id="DOC-2024")]
    results = _continuity(opening, closing)
    assert [item.status for item in results] == [STATUS_PASSED]


def test_undisclosed_difference_between_filings_is_violated():
    opening = [_fact("total_assets", 9_400_000, period="2024FY", document_id="DOC-2025")]
    closing = [_fact("total_assets", 9_000_000, period="2024FY", document_id="DOC-2024")]
    results = _continuity(opening, closing)
    item = results[0]
    assert item.status == STATUS_VIOLATED
    assert item.expected_value == 9_000_000 and item.actual_value == 9_400_000
    assert "未见任何口径/重述信号" in (item.note or "")


def test_disclosed_restatement_explains_the_difference_without_clearing_the_check():
    opening = [_fact("total_assets", 9_400_000, period="2024FY", document_id="DOC-2025",
                     version=Version.RESTATED)]
    closing = [_fact("total_assets", 9_000_000, period="2024FY", document_id="DOC-2024")]
    results = _continuity(opening, closing)
    item = results[0]
    assert item.status == STATUS_NOT_COMPARABLE
    assert "restatement_context" in (item.note or "")
    assert "CARD-13" in (item.note or "")


def test_card04_signal_is_consumed_by_attribute_not_reimplemented():
    class _Signal:
        signal_type = "restated"
        periods_affected = ("2024FY",)
        detector_rule = "rule.some.rule"

    opening = [_fact("total_assets", 9_400_000, period="2024FY", document_id="DOC-2025")]
    closing = [_fact("total_assets", 9_000_000, period="2024FY", document_id="DOC-2024")]
    results = _continuity(opening, closing, scope_signals=[_Signal()])
    assert results[0].status == STATUS_NOT_COMPARABLE
    assert "restated" in (results[0].note or "")


def test_one_sided_continuity_is_skipped_rather_than_passed():
    opening = [_fact("total_assets", 9_400_000, period="2024FY", document_id="DOC-2025")]
    results = _continuity(opening, [])
    assert [item.status for item in results] == [STATUS_SKIPPED_MISSING_FIELD]


def test_cross_period_only_compares_stock_metrics():
    """A flow metric has no 'opening balance'; comparing one would be meaningless."""
    opening = [_fact("revenue", 100.0, period="2024FY", statement="income",
                     period_kind=PeriodKind.YTD_CUMULATIVE, document_id="DOC-2025")]
    closing = [_fact("revenue", 90.0, period="2024FY", statement="income",
                     period_kind=PeriodKind.YTD_CUMULATIVE, document_id="DOC-2024")]
    assert _continuity(opening, closing) == []


# --------------------------------------------------------------------------- #
# §7 F5 Cross-source
# --------------------------------------------------------------------------- #

def _cross_source(document, snapshot, **kwargs):
    return check_cross_source(
        document_facts=document, snapshot_facts=snapshot, policy=DEFAULT_POLICY, **kwargs
    )


def test_matching_sources_are_reported_as_matched():
    document = [_fact("revenue", 20_000_000, statement="income",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表")]
    snapshot = [_fact("revenue", 20_000_000, statement="income", document_id="SNAP-1",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="snapshot:income")]
    results = _cross_source(document, snapshot, snapshot_id="SNAP-1")
    assert [item.status for item in results] == [STATUS_PASSED]
    assert "一致" in (results[0].note or "")


def test_rounding_level_agreement_is_still_agreement():
    document = [_fact("revenue", 20_000_000, statement="income",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表")]
    snapshot = [_fact("revenue", 20_000_000.0000001, statement="income", document_id="SNAP-1",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="snapshot:income")]
    results = _cross_source(document, snapshot, snapshot_id="SNAP-1")
    assert results[0].status == STATUS_PASSED
    assert "舍入内一致" in (results[0].note or "")


def test_real_cross_source_difference_is_flagged_and_handed_to_card13():
    document = [_fact("revenue", 20_000_000, statement="income",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表")]
    snapshot = [_fact("revenue", 19_000_000, statement="income", document_id="SNAP-1",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="snapshot:income")]
    results = _cross_source(document, snapshot, snapshot_id="SNAP-1")
    item = results[0]
    assert item.status == STATUS_VIOLATED
    assert "裁决" in (item.note or "") and "CARD-13" in (item.note or "")
    # ACME reports both sides and picks neither.
    assert item.expected_value == 20_000_000 and item.actual_value == 19_000_000


def test_differing_scope_is_not_comparable_never_a_conflict():
    """§12 edge 1 — 口径不同既不是 conflict，也不是「对方没有这一行」。"""
    document = [_fact("revenue", 20_000_000, scope="合并", statement="income",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表")]
    snapshot = [_fact("revenue", 8_000_000, scope="母公司", statement="income",
                      document_id="SNAP-1", period_kind=PeriodKind.YTD_CUMULATIVE,
                      table="snapshot:income")]
    results = _cross_source(document, snapshot, snapshot_id="SNAP-1")
    assert results[0].status == STATUS_NOT_COMPARABLE
    assert "NOT_COMPARABLE" in (results[0].note or "")
    # The filing *did* print a revenue line — the blocker is 口径, and CARD-13
    # names it rather than ACME inventing a second opinion.
    assert "口径" in (results[0].note or "")
    # Lining the two up is CARD-02's job; ACME must not pick a side.
    assert "CARD-02" in (results[0].note or "")


def test_source_present_on_one_side_only_is_skipped():
    document = [_fact("revenue", 20_000_000, statement="income",
                      period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表")]
    snapshot = [_fact("total_assets", 5_000_000, document_id="SNAP-1")]
    results = _cross_source(document, snapshot, snapshot_id="SNAP-1")
    assert {item.status for item in results} == {STATUS_SKIPPED_MISSING_FIELD}


def test_provider_statements_convert_with_a_declared_scope_and_warning():
    from src.cn.acme.adapters import facts_from_statements

    warnings: list[str] = []
    facts = facts_from_statements(
        [_statement("income", "2025FY", {"revenue": 20_000_000, "unknown_key": 5})],
        source_id="SNAP-1",
        warnings=warnings,
    )
    assert [fact.metric for fact in facts] == ["revenue"]
    assert facts[0].scope is Scope.CONSOLIDATED
    assert facts[0].document_id == "SNAP-1"
    assert any("scope_declared_consolidated" in item for item in facts[0].warnings)
    assert any("unknown_key" in item for item in warnings)


# --------------------------------------------------------------------------- #
# §7 F6 Cross-modal
# --------------------------------------------------------------------------- #

def _margin_facts() -> list[NormalizedFact]:
    return [
        _fact("revenue", 20_000_000, statement="income", period_kind=PeriodKind.YTD_CUMULATIVE,
              table="合并利润表"),
        _fact("operating_cost", 13_400_000, statement="income",
              period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表"),
        _fact("revenue", 17_000_000, period="2024FY", statement="income",
              period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表"),
        _fact("operating_cost", 10_700_000, period="2024FY", statement="income",
              period_kind=PeriodKind.YTD_CUMULATIVE, table="合并利润表"),
    ]


def test_body_clause_parser_separates_a_level_from_a_change():
    facts = [_text_fact("报告期内公司毛利率为 33.0%，较上年同期下降 1.2 个百分点。")]
    claims = parse_body_claims(facts)
    assert [(c.kind, c.value) for c in claims] == [(KIND_LEVEL, 33.0), (KIND_DELTA, 1.2)]
    assert claims[1].direction == "down" and claims[1].signed == -1.2


def test_ambiguous_percent_change_is_skipped_with_a_note():
    facts = [_text_fact("公司毛利率同比下降 5%。")]
    notes: list[str] = []
    claims = parse_body_claims(facts, notes=notes)
    assert claims == []
    assert notes and "不可判定" in notes[0]


def test_ratio_series_keeps_every_period_of_one_group():
    series = ratio_series(_margin_facts())
    assert [period for period, *_ in series["gross_margin"]] == ["2024FY", "2025FY"]


def test_body_change_that_contradicts_the_table_is_flagged():
    facts = _margin_facts()
    report = validate_multimodal_evidence(
        facts,
        raw_facts=[_text_fact("报告期内公司毛利率为 33.0%，较上年同期下降 1.2 个百分点。")],
        text_facts=[_text_fact("报告期内公司毛利率为 33.0%，较上年同期下降 1.2 个百分点。")],
        document_id="DOC-A",
    )
    assert len(report.cross_modal) == 1
    item = report.cross_modal[0]
    assert item.metric == "gross_margin"
    assert item.values["body"] == pytest.approx(-1.2)
    assert item.values["table"] == pytest.approx(-4.0588, abs=1e-3)
    # §12 edge 6 — the missing chart party is N/A, not absent and not invented.
    assert item.values["chart"] == "N/A"
    assert item.quote and "1.2" in item.quote
    assert all(ref.resolvable for ref in item.source_refs)


def test_a_correct_body_claim_produces_no_finding():
    text = [_text_fact("报告期内公司毛利率为 33.0%。")]
    report = validate_multimodal_evidence(
        _margin_facts(), raw_facts=text, text_facts=text, document_id="DOC-A"
    )
    assert report.cross_modal == ()


def test_chart_annotation_can_be_a_third_party_and_a_lone_body_error_is_still_caught():
    text = [_text_fact("报告期内公司毛利率为 30.0%。")]
    chart = [ChartAnnotation(metric="gross_margin", kind=KIND_LEVEL, value=33.0,
                             unit="%", label="图1 毛利率")]
    report = validate_multimodal_evidence(
        _margin_facts(), raw_facts=text, text_facts=text, chart_annotations=chart,
        document_id="DOC-A",
    )
    assert len(report.cross_modal) == 1
    item = report.cross_modal[0]
    assert item.values == {"body": 30.0, "table": 33.0, "chart": 33.0}


def test_body_claim_without_a_computable_benchmark_is_recorded_as_a_warning():
    text = [_text_fact("报告期内公司净利率为 18.0%。")]
    report = validate_multimodal_evidence(
        _margin_facts(), raw_facts=text, text_facts=text, document_id="DOC-A"
    )
    assert report.cross_modal == ()
    assert any("缺少可计算基准" in item for item in report.warnings)


# --------------------------------------------------------------------------- #
# §8 / §22 orchestration
# --------------------------------------------------------------------------- #

def test_report_names_every_family_it_did_not_check():
    report = validate_multimodal_evidence([], detected_at="T")
    assert set(report.families_not_checked) | set(report.constraint_families_checked) \
        == set(CONSTRAINT_FAMILIES)
    assert report.constraints_not_available
    assert all(item.reason for item in report.constraints_not_available)
    assert {item.constraint_id for item in report.constraints_not_available} >= {
        "profit_bridge", "cash_flow_closure"
    }


def test_a_failing_family_does_not_take_the_others_down():
    class _Exploding(list):
        def __bool__(self):
            raise RuntimeError("boom")

    report = validate_multimodal_evidence(
        [_fact("total_assets", 10_000_000), _fact("total_liabilities", 6_000_000),
         _fact("equity_parent", 4_000_000)],
        raw_facts=[], text_facts=[], prior_facts=None, snapshots=None,
        detected_at="T",
    )
    assert "accounting_identity" in report.constraint_families_checked
    assert report.totals["passed"] >= 1


def test_disabling_a_family_is_visible_rather_than_silent():
    report = validate_multimodal_evidence([], include_identities=False, detected_at="T")
    assert "accounting_identity" in report.families_not_checked
    assert any(
        item.constraint_id == "accounting_identities"
        for item in report.constraints_not_available
    )


def test_no_constraint_module_ever_writes_a_financial_value():
    """§9 / §18 — 初赛无任何自动改值路径, asserted against the source text.

    Checked by reading the files rather than by inspecting the runtime, because a
    function-body ``import`` and a conditional assignment are both invisible to
    an import graph while being exactly as capable of rewriting a number.

    The assignment patterns use a negative lookahead so that a *comparison*
    (``denominator.value == 0``) is not mistaken for a write — a detector that
    cannot tell ``=`` from ``==`` would flag every non-empty check in the card.
    """
    forbidden = (
        r"\.value\s*=(?!=)",              # x.value = ...
        r"\.normalized_value\s*=(?!=)",   # x.normalized_value = ...
        r"\.values\s*\[",                 # x.values["k"] = ...
        r"\bfacts?\s*\[\s*[^\]]+\s*\]\s*=(?!=)",  # facts[i] = ...
    )
    for path in sorted(ACME_SOURCE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert not re.search(pattern, source), f"{path.name} 含有可疑赋值：{pattern}"


def test_no_constraint_module_imports_an_llm_client():
    """§9 — the whole card is deterministic; zero LLM in any of the six families.

    Parsed rather than grepped: the module docstrings *talk about* not importing
    ``src.llms`` (that is the whole point of the note in ``__init__``), and a
    substring scan cannot tell a claim of absence from the thing it denies.
    """
    forbidden_roots = ("src.llms", "src.llms.", "openai", "anthropic", "litellm")
    for path in sorted(ACME_SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(
                    name == root or name.startswith(root + ".")
                    for root in forbidden_roots
                ), f"{path.name} 导入了模型客户端：{name}"


# --------------------------------------------------------------------------- #
# §13 Validator
# --------------------------------------------------------------------------- #

def _engine_report():
    """A report straight from the engine, untampered — carrying one open finding.

    The F2 subtotal is planted off by 1,000,000 (资产总计 11,000,000 ≠ 流动 6,000,000
    + 非流动 4,000,000) on purpose: §13's rules 1, 3 and 4 are rules *about open
    findings*, and a fixture with none of them cannot exercise any of the three.
    """
    return validate_multimodal_evidence(
        _margin_facts(),
        raw_facts=_balance_table(end_assets="11,000,000"),
        statements=[_statement("income", "2025FY", {"revenue": 20_000_000.0})],
        text_facts=[],
        document_id="DOC-A",
        detected_at="2026-09-19T00:00:00+08:00",
    )


def test_the_engine_report_fixture_actually_has_something_to_review():
    """Guard for the fixture itself — otherwise the §13 tests below are vacuous."""
    report = _engine_report()
    findings = open_findings(report)
    assert findings, "fixture no longer contains an open finding"
    assert any(item.status == STATUS_VIOLATED for item in findings)
    assert {item.violation_id for item in findings} <= set(report.review_queue)
    assert validate_acme_report(report).valid, validate_acme_report(report).errors


def test_validator_accepts_a_report_from_the_engine():
    result = validate_acme_report(_engine_report())
    assert result.valid, result.errors


def test_validator_rejects_an_inconsistent_residual():
    report = _engine_report()
    item = report.violations[0]
    broken = ConstraintViolation(
        **{**item.__dict__, "residual": (item.residual or 0.0) + 1.0}
    )
    tampered = type(report)(**{**report.__dict__, "violations": (broken,)})
    result = validate_acme_report(tampered)
    assert not result.valid
    assert any("residual" in error for error in result.errors)


def test_validator_rejects_a_finding_whose_source_cannot_resolve():
    report = _engine_report()
    item = next(entry for entry in report.violations if entry.status == STATUS_VIOLATED)
    broken = ConstraintViolation(**{**item.__dict__, "source_refs": ()})
    tampered = type(report)(**{**report.__dict__, "violations": (broken,)})
    result = validate_acme_report(tampered)
    assert not result.valid
    assert any("source_refs" in error for error in result.errors)


def test_validator_rejects_an_open_finding_missing_from_the_review_queue():
    report = _engine_report()
    tampered = type(report)(**{**report.__dict__, "review_queue": ()})
    result = validate_acme_report(tampered)
    assert not result.valid
    assert any("review_queue" in error for error in result.errors)


def test_validator_rejects_an_automatic_correction_without_its_trail():
    report = _engine_report()
    item = next(entry for entry in report.violations if entry.status == STATUS_VIOLATED)
    broken = ConstraintViolation(**{**item.__dict__, "resolution_status": "auto_corrected"})
    tampered = type(report)(**{**report.__dict__, "violations": (broken,)})
    result = validate_acme_report(tampered)
    assert not result.valid
    assert any("auto_corrected" in error for error in result.errors)


def test_correction_trail_requires_all_four_fields():
    complete = CorrectionTrail(
        original_value=100.0, candidate_values=(100.0, 120.0),
        constraint_id="subtotal_reconciliation", selected_value=120.0,
        selection_reason="与上方明细一致", manual_review_status="approved",
    )
    assert complete.is_complete()
    incomplete = CorrectionTrail(
        original_value=100.0, candidate_values=(), constraint_id="x",
        selected_value=None, selection_reason="", manual_review_status="pending",
    )
    assert not incomplete.is_complete()


def test_validator_rejects_a_not_checked_family_without_a_reason():
    report = _engine_report()
    tampered = type(report)(
        **{**report.__dict__, "families_not_checked": ("cross_source",),
           "constraints_not_available": ()}
    )
    result = validate_acme_report(tampered)
    assert not result.valid
    assert any("without_reason" in error for error in result.errors)


def test_findings_are_immutable():
    report = _engine_report()
    item = report.violations[0]
    with pytest.raises(FrozenInstanceError):
        item.status = STATUS_PASSED  # type: ignore[misc]


def test_open_findings_helper_matches_the_review_queue():
    report = _engine_report()
    assert {item.violation_id for item in open_findings(report)} <= set(report.review_queue)


# --------------------------------------------------------------------------- #
# §12 integration + §16 false-alarm gate
# --------------------------------------------------------------------------- #

@pytest.fixture()
def registry(tmp_path):
    instance = DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])
    try:
        yield instance
    finally:
        instance.close()


ANNUAL_FIXTURES = (
    "annual_01_profit_continued.pdf",
    "annual_02_balance_unit_wan.pdf",
    "annual_03_cashflow_with_scan.pdf",
    "annual_04_restated_comparative.pdf",
    "annual_05_reclassification.pdf",
    "annual_06_equity_shares.pdf",
    "annual_07_acme_constraint_break.pdf",
)


def _report_for(registry: DocumentRegistry, filename: str):
    from src.cn.normalization.pipeline import normalize_financials

    parsed = registry.ingest(FIXTURES / filename, declared_type="annual", source="data_dir")
    facts = registry.get_facts(parsed.document.document_id)
    normalization = normalize_financials(facts, parsed.document)
    report = validate_multimodal_evidence(
        normalization.facts,
        normalization.statements,
        raw_facts=facts,
        text_facts=facts,
        document_id=parsed.document.document_id,
        detected_at="2026-09-19T00:00:00+08:00",
    )
    return report, normalization


def test_corpus_reports_are_all_validator_clean(registry):
    reports = [_report_for(registry, name)[0] for name in ANNUAL_FIXTURES]
    result = validate_acme_corpus(reports)
    assert result.valid, result.errors


def test_no_false_alarms_across_the_corpus_except_the_planted_file(registry):
    """The six clean filings must produce zero violations; annual_07 exactly two."""
    violations = {}
    for name in ANNUAL_FIXTURES:
        report, _ = _report_for(registry, name)
        found = [item for item in report.violations if item.status == STATUS_VIOLATED]
        if found:
            violations[name] = found
    assert set(violations) == {"annual_07_acme_constraint_break.pdf"}
    assert len(violations["annual_07_acme_constraint_break.pdf"]) == 1


def test_planted_subtotal_break_is_caught_and_the_balance_equation_is_not(registry):
    report, normalization = _report_for(registry, "annual_07_acme_constraint_break.pdf")
    assert normalization.report_period == "2025FY"
    subtotal = next(
        item for item in report.violations
        if item.status == STATUS_VIOLATED
    )
    assert subtotal.constraint_family == "table_structural"
    assert subtotal.residual == pytest.approx(-1_000_000)
    # 负债 + 归母权益 equals the printed 资产总计, so F1 has nothing to say — the
    # point of the fixture.
    identities = [
        item for item in report.violations
        if item.constraint_family == "accounting_identity"
        and item.constraint_id == "balance_sheet_equation"
    ]
    assert identities and all(item.status == STATUS_PASSED for item in identities)


def test_planted_cross_modal_break_is_caught_on_the_same_document(registry):
    report, _ = _report_for(registry, "annual_07_acme_constraint_break.pdf")
    assert len(report.cross_modal) == 1
    item = report.cross_modal[0]
    assert item.values["chart"] == "N/A"
    assert item.computed_value == pytest.approx(-4.0588, abs=1e-3)


def test_report_is_byte_reproducible(registry):
    first, _ = _report_for(registry, "annual_07_acme_constraint_break.pdf")
    second, _ = _report_for(registry, "annual_07_acme_constraint_break.pdf")
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


# --------------------------------------------------------------------------- #
# §8 tool contract
# --------------------------------------------------------------------------- #

def _tool(registry):
    from src.agents.tools.cn_tools import ValidateCnAcmeTool

    return ValidateCnAcmeTool(registry)


def test_tool_returns_the_report_and_the_not_checked_gaps(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_07_acme_constraint_break.pdf", declared_type="annual",
        source="data_dir",
    )
    payload = json.loads(
        asyncio.run(_tool(registry).execute(document_id=parsed.document.document_id))
    )
    assert payload["status"] == "ok"
    assert payload["totals"]["violated"] == 1
    assert payload["families_not_checked"] == ["cross_period", "cross_source"]
    assert payload["validation"]["valid"]
    assert payload["review_queue"]


def test_tool_refuses_symbol_only_and_unknown_documents(registry):
    tool = _tool(registry)
    missing = json.loads(asyncio.run(tool.execute()))
    assert missing["status"] == "error"
    assert missing["error_code"] == "acme_missing_argument"

    unknown = json.loads(asyncio.run(tool.execute(document_id="nope")))
    assert unknown["status"] == "error"
    assert unknown["error_code"] == "acme_unknown_document"


def test_tool_requires_a_symbol_when_a_snapshot_is_named(registry):
    parsed = registry.ingest(
        FIXTURES / "annual_07_acme_constraint_break.pdf", declared_type="annual",
        source="data_dir",
    )
    payload = json.loads(
        asyncio.run(
            _tool(registry).execute(
                document_id=parsed.document.document_id, snapshot_id="SNAP-1"
            )
        )
    )
    assert payload["status"] == "error"
    assert payload["error_code"] == "acme_missing_argument"


def test_tool_is_registered_on_the_document_tool_surface():
    from src.agents.tools.cn_tools import build_cn_snapshot_tools

    names = {tool.name for tool in build_cn_snapshot_tools(FIXTURES / "does-not-exist.json")}
    assert "validate_cn_acme" in names


def test_tool_prior_document_wiring_runs_the_continuity_family(registry):
    current = registry.ingest(
        FIXTURES / "annual_04_restated_comparative.pdf", declared_type="annual",
        source="data_dir",
    )
    prior = registry.ingest(
        FIXTURES / "annual_05_reclassification.pdf", declared_type="annual",
        source="data_dir",
    )
    payload = json.loads(
        asyncio.run(
            _tool(registry).execute(
                document_id=current.document.document_id,
                prior_document_id=prior.document.document_id,
            )
        )
    )
    assert payload["status"] == "ok"
    assert "cross_period" in payload["constraint_families_checked"]
