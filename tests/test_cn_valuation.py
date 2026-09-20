"""Offline tests for CARD-08 relative valuation, sensitivity and assumptions.

The card's §11 cases are the spine here:

* the **3 × 3 hand-computed matrix** (EPS 0.8/1.0/1.2 × target PE 10/15/20, all
  nine cells checked against arithmetic done by hand, not by the engine);
* **monotonicity** in both axes, plus the validator's ability to *fail* a grid
  whose cells are monotonic but do not reproduce from their own axes;
* **negative EPS → PE not_applicable** (PB/PS must continue);
* **missing 总股本 → 整条每股估值阻断** (fail-closed, card §8 edge 2);
* **assumption integrity**: an unresolvable id, a missing reason, or an
  assumption that tries to override a sourced parameter is rejected; a result
  whose assumptions all lack a sourced basis is labelled "纯假设演示" (§9).

Plus the §12 chain CARD-01 → CARD-02 → CARD-08 on a really-ingested PDF, and the
§7.1 rule that the driver is the **TTM** figure rather than a static FY value.

Everything is offline: synthetic fixtures, an in-memory registry, no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from src.cn.domain import FinancialStatement
from src.cn.documents.registry import DocumentRegistry
from src.cn.normalization import NormalizedFact, PeriodKind, Scope, Version
from src.cn.periods import FinancialPeriodEngine
from src.cn.valuation import (
    SHARES_BLOCK_REASON,
    AssumptionConflictError,
    AssumptionError,
    AssumptionRegistry,
    PeerValuationInput,
    SensitivityMatrix,
    ValuationInputs,
    build_valuation,
    implied_price,
    method_dispersion,
    register_scenario_assumptions,
    sensitivity_pe_eps,
    summarize_peer_multiple,
    valuation_inputs_from_normalization,
)
from src.validation import validate_valuation
from src.cn.analysis.calculations import CalculationRegistry

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures" / "documents"


def peer(symbol, earnings, equity=100, revenue=200, price=10, shares=10):
    return PeerValuationInput(
        symbol=symbol, raw_price=price, total_shares=shares, net_profit=earnings,
        book_equity=equity, revenue=revenue, period_basis="2025FY",
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def base_inputs(**overrides) -> ValuationInputs:
    """A fully populated, self-consistent input set (EPS 0.9 / BPS 7.5)."""
    payload = dict(
        symbol="TEST",
        raw_price=15.0,
        total_shares=4_000_000.0,
        net_profit_parent_ttm=3_600_000.0,
        revenue_ttm=20_000_000.0,
        equity_parent_end=30_000_000.0,
        equity_parent_begin=26_000_000.0,
        period_key="2025FY",
        evidence_ids={"total_shares": "nf_shares", "raw_price": "nf_price"},
    )
    payload.update(overrides)
    return ValuationInputs(**payload)  # type: ignore[arg-type]


def registry_for(inputs: ValuationInputs, **kwargs) -> AssumptionRegistry:
    return AssumptionRegistry(
        sourced={key: value for key, value in inputs.evidence_ids.items() if value},
        **kwargs,
    )


def valuation(inputs: ValuationInputs | None = None, **kwargs):
    inputs = inputs or base_inputs()
    registry = kwargs.pop("registry", None) or registry_for(inputs)
    calculations = kwargs.pop("calculations", None) or CalculationRegistry()
    result = build_valuation(
        inputs, registry, target_multiples={"PE": 18.0, "PB": 2.5, "PS": 4.0},
        calculations=calculations, **kwargs,
    )
    return result, registry, calculations


def _run_tool(tool, **kwargs):
    return json.loads(asyncio.run(tool.execute(**kwargs)))


# --------------------------------------------------------------------------- #
# §11 — the hand-computed 3 × 3 matrix
# --------------------------------------------------------------------------- #

def test_hand_computed_eps_pe_matrix_has_nine_correct_cells():
    """EPS {0.8, 1.0, 1.2} × PE {10, 15, 20} — each product worked out by hand."""
    grid = sensitivity_pe_eps([0.8, 1.0, 1.2], [10.0, 15.0, 20.0], 1_000_000)

    assert grid.x_values == (10.0, 15.0, 20.0)
    assert grid.y_values == (0.8, 1.0, 1.2)
    # matrix[row over y=EPS][column over x=PE]
    assert grid.matrix == (
        (8.0, 12.0, 16.0),
        (10.0, 15.0, 20.0),
        (12.0, 18.0, 24.0),
    )
    assert grid.value_at(row=2, column=0) == pytest.approx(12.0)
    assert grid.cell(x=20.0, y=1.0) == pytest.approx(20.0)
    assert grid.monotonic is True


def test_hand_computed_bps_pb_matrix_has_nine_correct_cells():
    """PB uses BPS × target PB (card §7.3 option ①) — ROE is not an axis."""
    from src.cn.valuation import sensitivity_pb_bps

    grid = sensitivity_pb_bps([6.0, 7.5, 9.0], [2.0, 2.5, 3.0], 1_000_000)

    assert grid.matrix == (
        (12.0, 15.0, 18.0),
        (15.0, 18.75, 22.5),
        (18.0, 22.5, 27.0),
    )
    assert "ROE" not in grid.y_name and "BPS" in grid.y_name
    assert grid.monotonic is True


def test_matrix_axis_is_sorted_and_deduplicated():
    grid = sensitivity_pe_eps([1.2, 0.8, 1.0, 0.8], [20.0, 10.0], 1_000_000)
    assert grid.x_values == (10.0, 20.0)
    assert grid.y_values == (0.8, 1.0, 1.2)


def test_matrix_rejects_a_missing_share_count():
    """A per-share grid without a valid share count cannot exist (§8 edge 2)."""
    for shares in (0.0, -1.0):
        with pytest.raises(ValueError) as excinfo:
            sensitivity_pe_eps([1.0], [10.0], shares)
        assert SHARES_BLOCK_REASON in str(excinfo.value)


def test_matrix_rejects_a_non_positive_axis():
    with pytest.raises(ValueError):
        sensitivity_pe_eps([0.0], [10.0], 1_000_000)


def test_monotonicity_is_false_when_a_row_decreases():
    """The property must be able to say "no" — otherwise §10 is a no-op."""
    broken = SensitivityMatrix(
        name="eps_x_target_pe", x_name="目标 PE", y_name="EPS",
        x_values=(10.0, 20.0), y_values=(1.0,),
        matrix=((20.0, 10.0),),           # decreasing in x
        row_labels=("EPS=1.0",), column_labels=("PE=10.0", "PE=20.0"),
        formula="隐含价格 = EPS × 目标 PE", shares_used=1_000_000,
    )
    assert broken.monotonic is False


# --------------------------------------------------------------------------- #
# §10 — validator rules
# --------------------------------------------------------------------------- #

def test_validator_passes_a_clean_result_and_reports_no_silent_gap():
    result, registry, _ = valuation()
    outcome = validate_valuation(result, registry)
    assert outcome.valid is True
    assert outcome.errors == []
    assert outcome.warnings == []


def test_validator_fails_a_monotonic_but_non_reproducible_grid():
    """Cells doubled but still increasing: only re-derivation catches this."""
    result, registry, _ = valuation()
    grid = result.sensitivity[0]
    forged = SensitivityMatrix(
        name=grid.name, x_name=grid.x_name, y_name=grid.y_name,
        x_values=grid.x_values, y_values=grid.y_values,
        matrix=tuple(tuple(cell * 2 for cell in row) for row in grid.matrix),
        row_labels=grid.row_labels, column_labels=grid.column_labels,
        formula=grid.formula, shares_used=grid.shares_used,
        assumption_ids=grid.assumption_ids,
    )
    assert forged.monotonic is True
    outcome = validate_valuation(
        SimpleNamespace(
            scenarios=result.scenarios, sensitivity=(forged,),
            method_dispersion=result.method_dispersion, inputs=result.inputs,
            status=result.status, blocked_reason=result.blocked_reason,
        ),
        registry,
    )
    assert outcome.valid is False
    assert any("sensitivity_cell_not_reproducible" in item for item in outcome.errors)


def test_validator_flags_a_single_point_dispersion_report():
    result, registry, _ = valuation()
    thinned = dict(result.method_dispersion)
    thinned["PE"] = {**thinned["PE"], "count": 1}
    outcome = validate_valuation(
        SimpleNamespace(
            scenarios=result.scenarios, sensitivity=result.sensitivity,
            method_dispersion=thinned, inputs=result.inputs,
            status=result.status, blocked_reason=result.blocked_reason,
        ),
        registry,
    )
    assert "dispersion_single_point:PE" in outcome.errors


def test_validator_flags_an_unresolvable_assumption_instead_of_passing():
    result, registry, _ = valuation()
    broken = result.scenarios[0]
    object.__setattr__(broken, "assumption_ids", ("pe_growth_bear", "nope_999"))
    outcome = validate_valuation(result, registry)
    assert outcome.valid is False
    assert "unresolved_assumption:nope_999" in outcome.errors


def test_validator_warns_rather_than_passes_when_the_registry_is_absent():
    result, _, _ = valuation()
    outcome = validate_valuation(result, None)
    assert outcome.warnings and "assumption_registry_not_supplied" in outcome.warnings[0]
    assert outcome.valid is True  # a gap is not an error, but it is never silence


def test_validator_requires_the_dispersion_report_to_exist():
    result, registry, _ = valuation()
    outcome = validate_valuation(
        SimpleNamespace(
            scenarios=result.scenarios, sensitivity=result.sensitivity,
            method_dispersion=None, inputs=result.inputs,
            status=result.status, blocked_reason=result.blocked_reason,
        ),
        registry,
    )
    assert any("dispersion_report_missing" in item for item in outcome.errors)


# --------------------------------------------------------------------------- #
# §10 rule 1 + calculations — the four-layer chain
# --------------------------------------------------------------------------- #

def test_every_scenario_assumption_resolves_and_every_calculation_reproduces():
    result, registry, calculations = valuation()

    for item in result.scenarios:
        assert item.assumption_ids
        for assumption_id in item.assumption_ids:
            assert registry.has(assumption_id), (item.method, item.scenario, assumption_id)
        if item.applicable:
            assert item.calculation_ids, (item.method, item.scenario)

    assert calculations.records()
    assert calculations.validate() == []
    assert set(result.calculation_ids) == {item.calculation_id for item in calculations.records()}


def test_assumption_registry_rejects_an_override_of_a_sourced_parameter():
    """§8 edge 3: an assumption may never replace a data-derived value."""
    inputs = base_inputs()
    registry = registry_for(inputs)
    with pytest.raises(AssumptionConflictError):
        registry.register(
            "total_shares", 1.0, source="assumption: 我改成 1", reason="想让它更大",
        )
    with pytest.raises(AssumptionConflictError):
        registry.register(
            "raw_price", 99.0, source="nf_price", reason="顺手覆盖",
        )


def test_reserved_parameters_are_protected_even_without_an_evidence_id():
    registry = AssumptionRegistry(reserve=("equity_parent_end",))
    with pytest.raises(AssumptionConflictError):
        registry.register(
            "equity_parent_end", 1.0, source="assumption: x", reason="y",
        )


def test_assumption_registry_rejects_untraceable_sources_and_missing_reasons():
    registry = AssumptionRegistry(evidence_ids=("nf_ok",))
    with pytest.raises(AssumptionError):
        registry.register("growth", 0.1, source="nf_unknown", reason="引用不存在的证据")
    with pytest.raises(AssumptionError):
        registry.register("growth", 0.1, source="assumption: ", reason="没有理由")
    with pytest.raises(AssumptionError):
        registry.register("growth", 0.1, source="nf_ok", reason="   ")
    item = registry.register("growth", 0.1, source="nf_ok", reason="有据可依")
    assert item.sourced is True and item.grade == "sourced"


def test_register_scenario_assumptions_gives_each_scenario_its_own_multiple():
    inputs = base_inputs()
    registry = registry_for(inputs)
    register_scenario_assumptions(registry, target_multiples={"PE": 18.0})
    assert registry.get("pe_target_bear").value == pytest.approx(18.0 * 0.9)
    assert registry.get("pe_target_base").value == pytest.approx(18.0)
    assert registry.get("pe_target_bull").value == pytest.approx(18.0 * 1.1)
    # a method with no target multiple is simply not registered
    assert not registry.has("pb_target_base")
    assert "情景倍数系数" in registry.get("pe_target_bull").reason


def test_every_registered_assumption_carries_a_reason_and_a_valid_from_slot():
    result, _, _ = valuation()
    assert len(result.assumptions) == 27  # 3 methods × 3 scenarios × 3 assumptions
    for item in result.assumptions:
        assert item.reason.strip()
        assert item.source
        assert isinstance(item.valid_from, str)


# --------------------------------------------------------------------------- #
# scenarios — applicability, degradation and dispersion
# --------------------------------------------------------------------------- #

def test_scenario_numbers_follow_the_registered_growth_and_multiple():
    result, _, _ = valuation()
    rows = {(item.method, item.scenario): item for item in result.scenarios}

    # EPS = 0.9; base keeps TTM growth, so mid = 0.9 × 18 = 16.2
    assert rows[("PE", "base")].value_mid == pytest.approx(16.2)
    assert rows[("PE", "bear")].value_mid == pytest.approx(0.9 * 0.8 * 16.2)
    assert rows[("PE", "bull")].value_mid == pytest.approx(0.9 * 1.2 * 19.8)
    # PB uses BPS = 7.5, PS uses per-share revenue = 5.0
    assert rows[("PB", "base")].value_mid == pytest.approx(7.5 * 2.5)
    assert rows[("PS", "base")].value_mid == pytest.approx(5.0 * 4.0)
    for item in rows.values():
        assert item.ordered
        assert item.value_low < item.value_mid < item.value_high


def test_negative_eps_disables_pe_only():
    """§8 edge 4: a loss maker has no PE, but PB/PS still apply."""
    inputs = base_inputs(net_profit_parent_ttm=-900_000.0)
    result, registry, _ = valuation(inputs)

    pe = [item for item in result.scenarios if item.method == "PE"]
    assert all(not item.applicable for item in pe)
    assert {item.not_applicable_reason for item in pe} == {"eps_non_positive"}
    assert all(item.value_mid is None for item in pe)
    assert all(item.applicable for item in result.scenarios if item.method in {"PB", "PS"})
    assert result.method_dispersion["PE"]["applicable"] is False
    assert result.method_dispersion["PE"]["count"] == 0
    assert validate_valuation(result, registry).valid is True
    # no negative implied price ever reaches the payload
    assert all(
        item["value_mid"] is None or item["value_mid"] > 0
        for item in result.to_dict()["scenarios"]
    )


def test_status_is_partial_when_a_method_is_inapplicable_but_assumptions_are_sourced():
    """The two degradations are distinct: 方法不适用 vs 纯假设（§9）。"""
    inputs = base_inputs(net_profit_parent_ttm=-900_000.0)
    registry = registry_for(inputs)
    register_scenario_assumptions(registry, target_multiples={"PE": 18.0, "PB": 2.5, "PS": 4.0})
    registry.register(
        "pb_target_base", 2.5, source="nf_price", reason="以披露的可比区间为依据",
    )
    result = build_valuation(inputs, registry, register_defaults=False)
    assert registry.all_assumption_grade is False
    assert result.status == "partial"
    assert not any("pure_assumption" in item for item in result.warnings)


def test_negative_bps_disables_pb_only():
    inputs = base_inputs(equity_parent_end=-1.0)
    result, registry, _ = valuation(inputs)
    pb = [item for item in result.scenarios if item.method == "PB"]
    assert all(item.not_applicable_reason == "bps_non_positive" for item in pb)
    assert all(item.applicable for item in result.scenarios if item.method == "PE")
    assert validate_valuation(result, registry).valid is True


def test_missing_target_multiple_is_reported_not_invented():
    """Zero guessing: with no peers and no analyst multiple, nothing is filled in."""
    inputs = base_inputs()
    registry = registry_for(inputs)
    result = build_valuation(inputs, registry, target_multiples=None)

    assert result.status == "blocked"
    assert result.blocked_reason == "missing_target_multiple"
    assert all(not item.applicable for item in result.scenarios)
    assert all(item.not_applicable_reason == "missing_target_multiple" for item in result.scenarios)
    assert result.sensitivity == ()


def test_missing_total_shares_blocks_every_per_share_value():
    with pytest.raises(ValueError) as excinfo:
        base_inputs(total_shares=0.0)
    assert SHARES_BLOCK_REASON in str(excinfo.value)


def test_missing_ttm_inputs_leave_the_driver_none_rather_than_zero():
    inputs = base_inputs(net_profit_parent_ttm=None, revenue_ttm=None)
    result, registry, _ = valuation(inputs)
    assert inputs.eps_ttm is None and inputs.revenue_per_share_ttm is None
    assert all(
        item.not_applicable_reason in {"eps_non_positive", "revenue_per_share_non_positive"}
        for item in result.scenarios
        if item.method in {"PE", "PS"}
    )
    assert all(item.applicable for item in result.scenarios if item.method == "PB")
    assert validate_valuation(result, registry).valid is True


def test_all_assumption_grade_result_is_labelled_a_pure_assumption_demo():
    """§9: no sourced basis anywhere → the whole result is degraded, loudly."""
    result, registry, _ = valuation()
    assert registry.all_assumption_grade is True
    assert result.status == "assumption_only"
    assert any("pure_assumption" in item for item in result.warnings)


def test_a_sourced_assumption_upgrades_the_status_off_assumption_only():
    inputs = base_inputs()
    registry = registry_for(inputs)
    register_scenario_assumptions(
        registry, target_multiples={"PE": 18.0, "PB": 2.5, "PS": 4.0},
    )
    registry.register(
        "pe_target_base", 18.0, source="nf_price", reason="以披露的可比区间上限为依据",
    )
    result = build_valuation(inputs, registry, register_defaults=False)
    assert registry.all_assumption_grade is False
    assert result.status == "ok"
    assert not any("pure_assumption" in item for item in result.warnings)


def test_dispersion_reports_every_method_separately_without_blending():
    result, _, _ = valuation()
    report = method_dispersion(result.scenarios)

    for method in ("PE", "PB", "PS"):
        entry = report[method]
        assert entry["applicable"] is True
        assert entry["count"] == 3
        assert set(entry["scenario_mids"]) == {"bear", "base", "bull"}
        assert entry["range"] == pytest.approx(entry["max"] - entry["min"])
    assert report["across_methods"]["base_scenario_mids"] == {
        "PE": pytest.approx(16.2), "PB": pytest.approx(18.75), "PS": pytest.approx(20.0),
    }
    assert "不合成" in report["note"]
    assert result.method_dispersion["note"] == report["note"]


def test_dispersion_entry_exists_even_when_a_method_is_inapplicable():
    inputs = base_inputs(net_profit_parent_ttm=-900_000.0)
    result, _, _ = valuation(inputs)
    entry = result.method_dispersion["PE"]
    assert entry["applicable"] is False
    assert entry["count"] == 0
    assert entry["inapplicable_reasons"] == ["eps_non_positive"]


def test_applicability_note_is_a_deterministic_template_with_the_pb_boundary():
    result, _, _ = valuation()
    note = result.applicability_note
    assert "BPS × 目标 PB" in note
    assert "ROE 不进入价格公式" in note
    assert "12.86" in note  # the ROE approximation is reported, not used as an axis
    assert "不合成单一" in note
    assert "PE" in note and "PS" in note


# --------------------------------------------------------------------------- #
# §7.1 — the driver must be TTM, never a static FY value
# --------------------------------------------------------------------------- #

def _income(year: str, **values) -> FinancialStatement:
    return FinancialStatement(
        statement_type="income", fiscal_period=year, period_basis=f"{year}_CUMULATIVE",
        published_at=None, available_at=None, currency="CNY", unit="元", values=dict(values),
    )


def _normalized_fact(metric: str, value: float, period_key: str, *, kind=PeriodKind.YTD_CUMULATIVE):
    token = "c" if kind is PeriodKind.YTD_CUMULATIVE else "p"
    return NormalizedFact(
        normalized_fact_id=f"nf_doc_{metric}_{period_key}_{token}",
        metric=metric, value=float(value), currency="CNY", period_key=period_key,
        period_kind=kind, scope=Scope.CONSOLIDATED, version=Version.AS_REPORTED,
        available_at="2026-08-30T00:00:00+08:00", source_fact_id=f"x_{metric}_{period_key}",
        document_id="DOC-TTM", page=1, row=1, column=1, raw_value=str(value),
        source_unit="元", source_table="合并利润表", statement_type="income",
    )


def test_driver_uses_the_engine_ttm_not_the_current_interim_value():
    """``TTM = H1(current) + FY(prev) − H1(prev)`` — never ``H1(current)`` alone.

    A H1 report cannot supply the missing FY leg, so the engine's own output is
    used verbatim and the naive "just take the interim" answer is asserted
    *different* — that is the whole point of CARD-08 §7.1.
    """
    income = [
        _income("2025H1", net_profit=2_000_000.0, revenue=10_000_000.0),
        _income("2025FY", net_profit=3_000_000.0, revenue=19_000_000.0),
        _income("2026H1", net_profit=2_600_000.0, revenue=13_000_000.0),
    ]
    ttm = FinancialPeriodEngine.derive_ttm(income, "income")
    latest = max(ttm, key=lambda item: item.fiscal_period)
    assert latest.fiscal_period == "2026H1"
    assert latest.values["net_profit"] == pytest.approx(2_600_000.0 + 3_000_000.0 - 2_000_000.0)

    balance = FinancialStatement(
        statement_type="balance", fiscal_period="2026H1", period_basis="2026H1_END",
        published_at=None, available_at=None, currency="CNY", unit="元",
        values={"equity": 32_000_000.0, "total_shares": 4_000_000.0},
    )
    prior_balance = FinancialStatement(
        statement_type="balance", fiscal_period="2025FY", period_basis="2025FY_END",
        published_at=None, available_at=None, currency="CNY", unit="元",
        values={"equity": 28_000_000.0, "total_shares": 4_000_000.0},
    )
    result = SimpleNamespace(
        document_id="DOC-TTM",
        ttm=list(ttm),
        statements=[prior_balance, balance],
        facts=[
            _normalized_fact("net_profit_parent", 2_600_000.0, "2026H1"),
            _normalized_fact("net_profit_parent", 2_000_000.0, "2025H1"),
            _normalized_fact("net_profit_parent", 3_000_000.0, "2025FY"),
            _normalized_fact("revenue", 13_000_000.0, "2026H1"),
            _normalized_fact("revenue", 10_000_000.0, "2025H1"),
            _normalized_fact("revenue", 19_000_000.0, "2025FY"),
            _normalized_fact("diluted_shares", 4_000_000.0, "2026H1", kind=PeriodKind.POINT_IN_TIME),
            _normalized_fact("equity_parent", 32_000_000.0, "2026H1", kind=PeriodKind.POINT_IN_TIME),
            _normalized_fact("equity_parent", 28_000_000.0, "2025FY", kind=PeriodKind.POINT_IN_TIME),
        ],
    )

    inputs = valuation_inputs_from_normalization(result, symbol="TEST", raw_price=12.0)
    assert inputs.period_key == "2026H1"
    assert inputs.net_profit_parent_ttm == pytest.approx(3_600_000.0)
    assert inputs.eps_ttm == pytest.approx(0.9)
    assert inputs.eps_ttm != pytest.approx(2_600_000.0 / 4_000_000.0)
    assert inputs.roe_ttm_pct == pytest.approx(3_600_000.0 / 30_000_000.0 * 100.0)
    # the derived value keeps every contributing fact, not a single one
    assert len(inputs.evidence_all["net_profit_parent_ttm"]) == 3
    assert inputs.evidence_all["net_profit_parent_ttm"] == (
        "nf_doc_net_profit_parent_2026H1_c",
        "nf_doc_net_profit_parent_2025H1_c",
        "nf_doc_net_profit_parent_2025FY_c",
    )
    assert "derive_ttm" in inputs.formulas["net_profit_parent_ttm"]


def test_fy_boundary_ttm_is_reported_as_such():
    """At an annual boundary the engine's TTM *is* the FY value — say so."""
    income = [_income("2025FY", net_profit=3_600_000.0, revenue=20_000_000.0)]
    balance = FinancialStatement(
        statement_type="balance", fiscal_period="2025FY", period_basis="2025FY_END",
        published_at=None, available_at=None, currency="CNY", unit="元",
        values={"equity": 30_000_000.0, "total_shares": 4_000_000.0},
    )
    result = SimpleNamespace(
        document_id="DOC", ttm=FinancialPeriodEngine.derive_ttm(income, "income"),
        statements=[balance], facts=[],
    )
    inputs = valuation_inputs_from_normalization(result, symbol="TEST", raw_price=15.0)
    assert inputs.net_profit_parent_ttm == pytest.approx(3_600_000.0)
    assert any("ttm_period_is_fy_boundary" in item for item in inputs.warnings)


def test_bridge_reports_missing_balance_sheet_instead_of_guessing():
    result = SimpleNamespace(
        document_id="DOC",
        ttm=FinancialPeriodEngine.derive_ttm(
            [_income("2025FY", net_profit=1.0, revenue=2.0)], "income"
        ),
        statements=[], facts=[],
    )
    with pytest.raises(ValueError) as excinfo:
        valuation_inputs_from_normalization(result, symbol="TEST", raw_price=15.0)
    assert SHARES_BLOCK_REASON in str(excinfo.value)


# --------------------------------------------------------------------------- #
# §12 — CARD-01 → CARD-02 → CARD-08 on a really-ingested PDF
# --------------------------------------------------------------------------- #

@pytest.fixture()
def registry(tmp_path: Path) -> DocumentRegistry:
    return DocumentRegistry(root=tmp_path / "documents", allow_roots=[FIXTURES])


def test_card02_to_card08_chain_keeps_every_number_traceable(registry):
    """归母净利润 3.6m / 股本 4m / 归母权益 30m / 期初 26m, RAW price 15.0."""
    from src.agents.tools.cn_tools import RunCnRelativeValuationTool

    parsed = registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import",
    )
    document_id = parsed.document.document_id
    payload = _run_tool(
        RunCnRelativeValuationTool(registry), document_id=document_id, raw_price=15.0,
        target_multiples={"PE": 18.0, "PB": 2.5, "PS": 4.0},
    )

    # §6 envelope
    assert set(payload) >= {
        "status", "scenarios", "sensitivity", "dispersion", "assumptions",
        "inputs", "validation",
    }
    assert payload["validation"]["valid"] is True
    assert payload["calculations_checked"] == []

    inputs = payload["inputs"]
    assert inputs["price_basis"] == "RAW"
    assert inputs["market_cap"] == pytest.approx(60_000_000.0)
    assert inputs["eps_ttm"] == pytest.approx(0.9)
    assert inputs["bps"] == pytest.approx(7.5)
    assert inputs["roe_ttm_pct"] == pytest.approx(3_600_000.0 / 28_000_000.0 * 100.0)
    assert inputs["period_basis"] == "TTM"

    rows = {(item["method"], item["scenario"]): item for item in payload["scenarios"]}
    assert rows[("PE", "base")]["value_mid"] == pytest.approx(16.2)
    assert rows[("PE", "base")]["target_multiple"] == pytest.approx(18.0)
    assert rows[("PB", "base")]["value_mid"] == pytest.approx(18.75)

    # 单元格 → 假设 → 数据 → 证据
    assumption_ids = rows[("PE", "base")]["assumption_ids"]
    registered = {item["assumption_id"]: item for item in payload["assumptions"]}
    assert set(assumption_ids) <= set(registered)
    for assumption_id in assumption_ids:
        assert registered[assumption_id]["reason"]
    evidence = inputs["evidence_ids"]
    assert evidence["net_profit_parent_ttm"].startswith("nf_")
    assert evidence["total_shares"].startswith("nf_")

    # the evidence ids are CARD-02 normalized fact ids; CARD-02's own jump map
    # resolves them to a page, so the click-through chain really terminates.
    from src.cn.normalization import normalize_financials
    from src.cn.normalization.evidence_bridge import normalization_jumps

    document = registry.get(document_id)
    normalized = normalize_financials(registry.get_facts(document_id), document)
    jumps = normalization_jumps(document, normalized)
    assert evidence["total_shares"] in jumps
    assert jumps[evidence["total_shares"]]["page"] == 1
    assert jumps[evidence["total_shares"]]["value"] == pytest.approx(4_000_000.0)
    assert jumps[evidence["net_profit_parent_ttm"]]["page"] == 2
    assert jumps[evidence["net_profit_parent_ttm"]]["value"] == pytest.approx(3_600_000.0)

    # only the FY-boundary TTM caveat is expected, and it is reported
    assert any("ttm_period_is_fy_boundary" in item for item in payload["input_warnings"])


def test_the_tool_blocks_a_filing_without_total_shares(registry):
    """§8 edge 2 / failure injection #1: no shares → blocked, not filled in."""
    from src.agents.tools.cn_tools import RunCnRelativeValuationTool

    parsed = registry.ingest(
        FIXTURES / "annual_01_profit_continued.pdf", declared_type="annual", source="admin_import",
    )
    payload = _run_tool(
        RunCnRelativeValuationTool(registry),
        document_id=parsed.document.document_id, raw_price=15.0,
    )
    assert payload.get("error_code") == SHARES_BLOCK_REASON
    assert "fail-closed" in payload["error"]
    assert "scenarios" not in payload


def test_the_tool_refuses_symbol_only_calls_like_the_analysis_tool(registry):
    from src.agents.tools.cn_tools import RunCnRelativeValuationTool

    payload = _run_tool(RunCnRelativeValuationTool(registry), symbol="600519.SH", raw_price=15.0)
    assert payload["error_code"] == "valuation_missing_argument"
    assert "document_id" in payload["error"]


def test_the_tool_requires_a_raw_price(registry):
    from src.agents.tools.cn_tools import RunCnRelativeValuationTool

    parsed = registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import",
    )
    payload = _run_tool(
        RunCnRelativeValuationTool(registry), document_id=parsed.document.document_id,
    )
    assert payload["error_code"] == "valuation_missing_raw_price"


def test_peer_medians_seed_the_target_multiple_and_selection_is_reported(registry):
    """With a peer set the multiple comes from the *existing* selection engine."""
    from src.agents.tools.cn_tools import RunCnRelativeValuationTool

    parsed = registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import",
    )
    peers = [
        {"symbol": "P1", "raw_price": 12.0, "total_shares": 4_000_000, "net_profit": 2_000_000,
         "book_equity": 24_000_000, "revenue": 16_000_000, "period_basis": "2025FY",
         "industry_code": "PHARMA", "evidence_ids": {"price": "pev1"}},
        {"symbol": "P2", "raw_price": 18.0, "total_shares": 4_000_000, "net_profit": 3_600_000,
         "book_equity": 30_000_000, "revenue": 22_000_000, "period_basis": "2025FY",
         "industry_code": "PHARMA", "evidence_ids": {"price": "pev2"}},
        {"symbol": "OTHER", "raw_price": 10.0, "total_shares": 1_000_000, "net_profit": 1_000_000,
         "book_equity": 5_000_000, "revenue": 8_000_000, "period_basis": "2025FY",
         "industry_code": "BANK", "evidence_ids": {"price": "pev3"}},
    ]
    payload = _run_tool(
        RunCnRelativeValuationTool(registry),
        document_id=parsed.document.document_id, raw_price=15.0,
        peers=peers, peer_industry_code="PHARMA",
    )
    assert payload["peer_selection"]["included"] == ["P1", "P2"]
    assert payload["peer_selection"]["excluded"] == [{"symbol": "OTHER", "reason": "industry_mismatch"}]
    assert payload["peer_selection"]["sample_warning"].startswith("peer_sample_below_3")
    # medians: PE = median(24.0, 20.0) = 22.0; PB = median(2.0, 2.4) = 2.2
    assert payload["peer_selection"]["medians"]["PE"] == pytest.approx(22.0)
    rows = {(item["method"], item["scenario"]): item for item in payload["scenarios"]}
    assert rows[("PE", "base")]["target_multiple"] == pytest.approx(22.0)
    assert payload["peer_evidence_ids"] == ["pev1", "pev2"]
    assert payload["validation"]["valid"] is True


def test_peer_set_without_industry_code_is_refused(registry):
    from src.agents.tools.cn_tools import RunCnRelativeValuationTool

    parsed = registry.ingest(
        FIXTURES / "annual_06_equity_shares.pdf", declared_type="annual", source="admin_import",
    )
    payload = _run_tool(
        RunCnRelativeValuationTool(registry),
        document_id=parsed.document.document_id, raw_price=15.0,
        peers=[{"symbol": "P1", "raw_price": 12.0, "total_shares": 10, "period_basis": "2025FY"}],
    )
    assert payload["error_code"] == "valuation_peer_industry_required"


# --------------------------------------------------------------------------- #
# existing peer-multiple behaviour must be untouched
# --------------------------------------------------------------------------- #

def test_peer_engine_still_excludes_loss_makers_and_uses_raw_price():
    grid = summarize_peer_multiple([peer("A", 10), peer("B", 5), peer("LOSS", -1)], "PE")
    assert grid.median == 15
    assert grid.included_symbols == ["A", "B"]
    assert grid.excluded["LOSS"] == "non_positive_PE_denominator"
    assert grid.confidence == "low"
    target = peer("TARGET", 20, shares=10)
    assert implied_price(target, multiple=grid, denominator="net_profit") == 30


def test_iqr_outlier_is_excluded_only_with_sufficient_sample_size():
    peers = [
        peer("A", 10), peer("B", 10), peer("C", 10),
        peer("D", 10), peer("E", 10), peer("F", 10),
        peer("OUTLIER", 0.1),
    ]
    grid = summarize_peer_multiple(peers, "PE")
    assert grid.excluded["OUTLIER"] == "iqr_outlier"
    assert grid.included_symbols == ["A", "B", "C", "D", "E", "F"]


def test_implied_price_uses_per_share_math():
    grid = summarize_peer_multiple([peer("A", 10), peer("B", 10, price=20)], "PE")
    target = peer("TARGET", 20, shares=10)
    assert implied_price(target, multiple=grid, denominator="net_profit") == 30
