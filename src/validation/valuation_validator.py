"""CARD-08 §10 validator rules for the relative-valuation engine.

The card names three rules; each is written as a check that can actually *fail*
on a plausible mistake rather than a restatement of the engine's own code:

1. **Every assumption id resolves.** A scenario whose ``assumption_ids`` cannot be
   resolved in the Assumption Registry is an unsourced number wearing a citation —
   exactly the failure mode the registry exists to prevent.
2. **The sensitivity matrix is monotonic *and arithmetically reproducible*.**
   Monotonicity alone would pass a matrix whose cells were all doubled; so every
   cell is also re-derived from its own axes. A monotonic-but-wrong grid is
   precisely the "looks right, is wrong" payload this project forbids.
3. **The dispersion report exists and is not a single point.** Card §7.4 forbids a
   manufactured "final range"; the complementary failure is reporting one number
   and calling it a report, which is what the ``count >= 2`` check catches.

Anything the validator cannot judge — a missing registry, absent inputs — becomes
a **warning**, never a silent pass: "input not supplied" and "input correct" must
not look the same to the caller.

Pure by construction: no network, no LLM, no wall-clock. It reports; it never
repairs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Methods whose intervals must be shown separately (card §7.4).
REQUIRED_METHODS: Sequence[str] = ("PE", "PB", "PS")

#: Tolerance for re-deriving a scenario or matrix cell.
TOLERANCE = 1e-9


@dataclass(frozen=True)
class ValuationValidation:
    """Outcome of the CARD-08 §10 checks."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"valid": self.valid, "errors": list(self.errors), "warnings": list(self.warnings)}


def _close(left: Any, right: Any, *, tolerance: float = TOLERANCE) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance * max(1.0, abs(float(right)))
    except (TypeError, ValueError):
        return False


def validate_valuation(
    result: object,
    assumption_registry: Optional[object] = None,
    *,
    tolerance: float = TOLERANCE,
) -> ValuationValidation:
    """Run the three §10 rules over one :class:`~src.cn.valuation.ValuationResult`."""
    errors: List[str] = []
    warnings: List[str] = []

    scenarios = list(getattr(result, "scenarios", ()) or ())
    grids = list(getattr(result, "sensitivity", ()) or ())
    dispersion = getattr(result, "method_dispersion", None)
    status = str(getattr(result, "status", "") or "")

    if not scenarios:
        warnings.append("scenarios_missing：结果没有任何情景记录，无法判定假设链路。")
    if assumption_registry is None:
        warnings.append(
            "assumption_registry_not_supplied：未提供假设登记表，"
            "无法校验 assumption_ids 是否可解析（不作通过处理）。"
        )
    else:
        errors.extend(_assumption_errors(scenarios, assumption_registry))

    errors.extend(_scenario_errors(scenarios, tolerance=tolerance))
    errors.extend(_sensitivity_errors(grids, scenarios, tolerance=tolerance))
    errors.extend(_dispersion_errors(dispersion, scenarios))

    inputs = getattr(result, "inputs", None) or {}
    if isinstance(inputs, dict):
        if not inputs.get("evidence_ids"):
            warnings.append(
                "inputs_without_evidence：估值输入没有登记任何 evidence id，"
                "数据层不可回溯（不等于数值错误，但不可作为有据结论）。"
            )
        if str(inputs.get("price_basis") or "") != "RAW":
            errors.append("price_basis_not_raw：市值与倍数必须建立在 RAW 价格上。")
    if status == "blocked" and not str(getattr(result, "blocked_reason", "") or ""):
        errors.append("blocked_without_reason：结果被阻断但没有给出 blocked_reason。")

    return ValuationValidation(valid=not errors, errors=errors, warnings=warnings)


# -- rule 1: assumption resolution -----------------------------------------

def _assumption_errors(scenarios: Sequence[object], registry: object) -> List[str]:
    errors: List[str] = []
    seen: set = set()
    for item in scenarios:
        ids = list(getattr(item, "assumption_ids", ()) or ())
        if not ids:
            errors.append(
                f"scenario_without_assumption_ids:{_label(item)}"
            )
            continue
        for assumption_id in ids:
            if str(assumption_id) in seen:
                continue
            seen.add(str(assumption_id))
            if not registry.has(str(assumption_id)):  # type: ignore[attr-defined]
                errors.append(f"unresolved_assumption:{assumption_id}")
    return errors


# -- rule 2: sensitivity monotonicity + reproducibility ---------------------

def _sensitivity_errors(
    grids: Sequence[object], scenarios: Sequence[object], *, tolerance: float
) -> List[str]:
    errors: List[str] = []
    applicable_methods = {
        str(getattr(item, "method", "")) for item in scenarios if getattr(item, "applicable", False)
    }
    covered = {str(getattr(grid, "name", "")) for grid in grids}
    for method, expected in (("PE", "eps_x_target_pe"), ("PB", "bps_x_target_pb")):
        if method in applicable_methods and expected not in covered:
            errors.append(f"sensitivity_matrix_missing:{expected}")

    for grid in grids:
        name = str(getattr(grid, "name", ""))
        x_values = [float(item) for item in getattr(grid, "x_values", ()) or ()]
        y_values = [float(item) for item in getattr(grid, "y_values", ()) or ()]
        matrix = [[float(cell) for cell in row] for row in getattr(grid, "matrix", ()) or ()]
        if len(matrix) != len(y_values) or any(len(row) != len(x_values) for row in matrix):
            errors.append(f"sensitivity_shape_mismatch:{name}")
            continue
        if len(x_values) < 2 or len(y_values) < 2:
            errors.append(f"sensitivity_axis_single_point:{name}")
        if any(b <= a for a, b in zip(x_values, x_values[1:])):
            errors.append(f"sensitivity_x_not_increasing:{name}")
        if any(b <= a for a, b in zip(y_values, y_values[1:])):
            errors.append(f"sensitivity_y_not_increasing:{name}")
        for row_index, y in enumerate(y_values):
            for column_index, x in enumerate(x_values):
                if not _close(matrix[row_index][column_index], y * x, tolerance=tolerance):
                    errors.append(
                        f"sensitivity_cell_not_reproducible:{name}[{row_index}][{column_index}]"
                    )
        if not getattr(grid, "monotonic", True):
            errors.append(f"sensitivity_not_monotonic:{name}")
        shares = getattr(grid, "shares_used", None)
        if shares is None or float(shares) <= 0:
            errors.append(f"sensitivity_without_valid_shares:{name}")
    return errors


# -- rule 3: dispersion report ---------------------------------------------

def _dispersion_errors(dispersion: Any, scenarios: Sequence[object]) -> List[str]:
    errors: List[str] = []
    if not isinstance(dispersion, dict):
        errors.append("dispersion_report_missing：必须给出各方法离散度报告（禁止只给单点值）。")
        return errors
    if not dispersion:
        errors.append("dispersion_report_empty")
    for method in REQUIRED_METHODS:
        entry = dispersion.get(method)
        if not isinstance(entry, dict):
            errors.append(f"dispersion_entry_missing:{method}")
            continue
        if not entry.get("applicable"):
            continue
        if int(entry.get("count") or 0) < 2:
            errors.append(f"dispersion_single_point:{method}")
        if "range" not in entry:
            errors.append(f"dispersion_range_missing:{method}")
    if "across_methods" not in dispersion:
        errors.append("dispersion_across_methods_missing")
    applicable = [item for item in scenarios if getattr(item, "applicable", False)]
    singles = [item for item in applicable if getattr(item, "value_mid", None) is None]
    if singles:
        errors.append(
            "applicable_scenario_without_mid:" + ",".join(_label(item) for item in singles)
        )
    return errors


# -- rule extras ------------------------------------------------------------

def _scenario_errors(scenarios: Sequence[object], *, tolerance: float) -> List[str]:
    """Internal consistency every scenario must satisfy, applicable or not."""
    errors: List[str] = []
    for item in scenarios:
        label = _label(item)
        applicable = bool(getattr(item, "applicable", False))
        low = getattr(item, "value_low", None)
        mid = getattr(item, "value_mid", None)
        high = getattr(item, "value_high", None)
        if not applicable:
            if not str(getattr(item, "not_applicable_reason", "") or ""):
                errors.append(f"not_applicable_without_reason:{label}")
            if any(value is not None for value in (low, mid, high)):
                errors.append(f"not_applicable_with_values:{label}")
            continue
        if low is None or mid is None or high is None:
            errors.append(f"applicable_without_interval:{label}")
            continue
        if not (float(low) <= float(mid) <= float(high)):
            errors.append(f"interval_not_ordered:{label}")
        if any(float(value) <= 0 for value in (low, mid, high)):
            errors.append(f"non_positive_implied_price:{label}")
        driver = getattr(item, "driver_mid", None)
        target = getattr(item, "target_multiple", None)
        if driver is None or target is None:
            errors.append(f"scenario_without_driver_or_multiple:{label}")
        elif not _close(mid, float(driver) * float(target), tolerance=tolerance):
            errors.append(f"scenario_mid_not_reproducible:{label}")
    return errors


def _label(item: object) -> str:
    return f"{getattr(item, 'method', '?')}/{getattr(item, 'scenario', '?')}"


def validate_valuation_corpus(results: Iterable[object], registry: object) -> ValuationValidation:
    """Run :func:`validate_valuation` over many results, merging the outcomes.

    Used as a free end-to-end invariant: every scenario in the whole corpus must
    validate, so a defect that only shows up on one input shape cannot hide.
    """
    errors: List[str] = []
    warnings: List[str] = []
    count = 0
    for result in results:
        count += 1
        outcome = validate_valuation(result, registry)
        errors.extend(outcome.errors)
        warnings.extend(outcome.warnings)
    if count == 0:
        warnings.append("corpus_empty：没有可校验的估值结果。")
    return ValuationValidation(valid=not errors, errors=errors, warnings=warnings)
