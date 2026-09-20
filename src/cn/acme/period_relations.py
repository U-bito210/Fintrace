"""CARD-22 §7 F3 — Period Constraints (``Q2 = H1 − Q1`` …).

**The arithmetic in this module is entirely CARD-02's.** Single-quarter values
come from ``FinancialPeriodEngine.derive_single_quarters()`` via the CARD-02
adapter, and nothing here differences two cumulative values — §7 F3 is explicit
that a second period engine is how the two halves of the system start disagreeing
about what a quarter is.

What *is* local is the **naming of the operands**: which cumulative endpoints feed
which single quarter (:data:`SINGLE_QUARTER_ENDPOINTS`, transcribed from §7 F3).
That table is used for reporting *what is missing*, never for computing a value.

The rules that can actually fire, in order of usefulness:

* **disclosed vs derived** — a document that reports a single quarter *and* its
  cumulative endpoints is checked against the engine's derivation. This is the
  one outcome with real detection power for "期间错配".
* **unverifiable single quarter** — a disclosed single quarter whose endpoints
  are absent is ``not_comparable`` naming the missing endpoints, rather than
  silently trusted.
* **engine refusal** — a mixed 口径/版本 group makes the engine raise
  ``PeriodEngineError``; that becomes a ``check_failed`` record carrying the
  engine's own message. Swallowing it would hide exactly the defect the engine
  was built to catch.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..domain import FinancialStatement
from ..normalization.adapter import derive_single_quarters
from ..normalization.models import NormalizedFact, PeriodKind
from ..periods import DerivedPeriod, PeriodEngineError
from .models import (
    ConstraintOperand,
    ConstraintViolation,
    FAMILY_PERIOD,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    STATUS_CHECK_FAILED,
    STATUS_NOT_COMPARABLE,
    STATUS_SKIPPED_MISSING_FIELD,
    STATUS_VIOLATED,
    SourceRef,
    make_violation_id,
)
from .tolerance import TolerancePolicy

CONSTRAINT_YTD_SINGLE_QUARTER = "ytd_single_quarter_relation"
CONSTRAINT_ENDPOINT_COVERAGE = "single_quarter_endpoint_coverage"

#: §7 F3, transcribed as data: which cumulative endpoints each single quarter
#: needs. Used to *name* a missing operand in a finding — the value itself is
#: computed by the engine.
SINGLE_QUARTER_ENDPOINTS: Dict[str, Tuple[str, ...]] = {
    "Q1": ("Q1",),
    "Q2": ("H1", "Q1"),
    "Q3": ("9M", "H1"),
    "Q4": ("FY", "9M"),
}

#: ``PeriodKind`` is the shared schema; only YTD series are differentiable.
FLOW_STATEMENTS: Tuple[str, ...] = ("income", "cashflow")


def _statement_ref(statement: FinancialStatement) -> SourceRef:
    return SourceRef(
        document_id=None,
        locator=f"财务报表 {statement.statement_type} {statement.fiscal_period}",
    )


def _operand(statement: FinancialStatement, metric: str) -> ConstraintOperand:
    return ConstraintOperand(
        ref=f"statement:{statement.statement_type}:{statement.fiscal_period}",
        value=statement.values.get(metric),
        period=statement.fiscal_period,
        version="RESTATED" if statement.is_restated else "AS_REPORTED",
        unit=statement.unit,
        currency=statement.currency,
        label=metric,
    )


def _disclosed_single_quarters(
    facts: Iterable[NormalizedFact],
) -> Dict[Tuple[str, str], NormalizedFact]:
    out: Dict[Tuple[str, str], NormalizedFact] = {}
    for fact in facts:
        if fact.period_kind is not PeriodKind.SINGLE_QUARTER:
            continue
        out.setdefault((fact.metric, fact.period_key), fact)
    return out


def _index_statements(
    statements: Sequence[FinancialStatement],
) -> Dict[Tuple[str, str], FinancialStatement]:
    return {(item.statement_type, item.fiscal_period): item for item in statements}


def _engine_derivations(
    statements: Sequence[FinancialStatement],
    *,
    detected_at: str,
) -> Tuple[Dict[str, List[DerivedPeriod]], List[ConstraintViolation], set]:
    """Run the engine per statement type; a refusal becomes a finding, not a crash.

    Returns ``(derived, failures, refused_types)`` — the refused set is what keeps
    rule 3 from also reporting "nothing to derive" for a type whose derivation
    was actually attempted and rejected.
    """
    derived: Dict[str, List[DerivedPeriod]] = {}
    failures: List[ConstraintViolation] = []
    refused: set = set()
    for statement_type in FLOW_STATEMENTS:
        try:
            derived[statement_type] = list(
                derive_single_quarters(statements, statement_type)
            )
        except PeriodEngineError as exc:
            refused.add(statement_type)
            failures.append(
                ConstraintViolation(
                    violation_id=make_violation_id(
                        CONSTRAINT_YTD_SINGLE_QUARTER, FAMILY_PERIOD,
                        (statement_type, "engine-refused"),
                    ),
                    constraint_family=FAMILY_PERIOD,
                    constraint_id=CONSTRAINT_YTD_SINGLE_QUARTER,
                    inputs=(),
                    expected_value=None,
                    actual_value=None,
                    residual=None,
                    tolerance=0.0,
                    status=STATUS_CHECK_FAILED,
                    source_refs=(),
                    severity=SEVERITY_ERROR,
                    detected_at=detected_at,
                    note=f"{statement_type} 单季化被期间引擎拒绝：{exc}",
                )
            )
    return derived, failures, refused


def check_period_relations(
    statements: Sequence[FinancialStatement],
    facts: Sequence[NormalizedFact],
    *,
    policy: TolerancePolicy,
    detected_at: str = "",
) -> List[ConstraintViolation]:
    """§7 F3 over one document's projected statements + normalized facts."""
    results: List[ConstraintViolation] = []
    derived, failures, refused = _engine_derivations(statements, detected_at=detected_at)
    results.extend(failures)

    by_key = _index_statements(statements)
    metric_keys: Dict[str, set] = {}
    for statement in statements:
        metric_keys.setdefault(statement.statement_type, set()).update(statement.values)

    metrics_by_type: Dict[str, Dict[str, str]] = {}
    for fact in facts:
        spec_type = fact.statement_type
        if spec_type in FLOW_STATEMENTS:
            metrics_by_type.setdefault(spec_type, {})[fact.metric] = fact.metric

    disclosed = _disclosed_single_quarters(facts)

    # -- rule 1: disclosed single quarter vs the engine's derivation ---------
    for statement_type, periods in sorted(derived.items()):
        for period in periods:
            for metric, value in sorted(period.values.items()):
                if value is None:
                    continue
                fact = disclosed.get((metric, period.fiscal_period))
                if fact is None:
                    continue
                status, tolerance, residual = policy.classify(
                    value, fact.value, constraint_id=CONSTRAINT_YTD_SINGLE_QUARTER
                )
                sources = tuple(
                    _statement_ref(by_key[(statement_type, source)])
                    for source in period.source_periods
                    if (statement_type, source) in by_key
                )
                results.append(
                    ConstraintViolation(
                        violation_id=make_violation_id(
                            CONSTRAINT_YTD_SINGLE_QUARTER, FAMILY_PERIOD,
                            (statement_type, period.fiscal_period, metric) + period.source_periods,
                        ),
                        constraint_family=FAMILY_PERIOD,
                        constraint_id=CONSTRAINT_YTD_SINGLE_QUARTER,
                        inputs=tuple(
                            _operand(by_key[(statement_type, source)], metric)
                            for source in period.source_periods
                            if (statement_type, source) in by_key
                        )
                        + (
                            ConstraintOperand(
                                ref=fact.normalized_fact_id,
                                value=fact.value,
                                period=fact.period_key,
                                unit="元",
                                currency=str(fact.currency),
                                label=metric,
                            ),
                        ),
                        expected_value=value,
                        actual_value=fact.value,
                        residual=residual,
                        tolerance=tolerance,
                        status=status,
                        source_refs=sources
                        + (
                            SourceRef(
                                document_id=fact.document_id or None,
                                page=fact.page,
                                bbox=fact.bbox,
                                locator=f"{fact.source_table or '未知表'}",
                            ),
                        ),
                        severity=SEVERITY_ERROR if status == STATUS_VIOLATED else SEVERITY_WARNING,
                        detected_at=detected_at,
                        note=(
                            f"{period.fiscal_period} 单季 {metric}：文档披露 "
                            f"{fact.value} vs 期间引擎按 {' − '.join(period.source_periods)} "
                            f"推导 {value}"
                        ),
                    )
                )

    # -- rule 2: a disclosed single quarter the engine cannot reproduce ------
    derived_keys = {
        (statement_type, period.fiscal_period)
        for statement_type, periods in derived.items()
        for period in periods
    }
    for (metric, period_key), fact in sorted(disclosed.items()):
        year, _, quarter = period_key.partition("Q")
        quarter = f"Q{quarter}" if quarter else period_key
        statement_type = str(fact.statement_type or "")
        if (statement_type, period_key) in derived_keys:
            continue
        missing = [
            f"{year}{kind}"
            for kind in SINGLE_QUARTER_ENDPOINTS.get(quarter, ())
            if f"{year}{kind}" not in metric_keys.get(statement_type, set())
        ]
        if not missing:
            # The endpoints are all present; the engine produced no quarter
            # because the metric itself is missing from them. Still not a pass.
            missing = [f"{year}{kind}（缺少 {metric}）"
                       for kind in SINGLE_QUARTER_ENDPOINTS.get(quarter, ())]
        results.append(
            ConstraintViolation(
                violation_id=make_violation_id(
                    CONSTRAINT_ENDPOINT_COVERAGE, FAMILY_PERIOD,
                    (metric, period_key, statement_type),
                ),
                constraint_family=FAMILY_PERIOD,
                constraint_id=CONSTRAINT_ENDPOINT_COVERAGE,
                inputs=(
                    ConstraintOperand(
                        ref=fact.normalized_fact_id, value=fact.value, period=period_key,
                        unit="元", currency=str(fact.currency), label=metric,
                    ),
                ),
                expected_value=None,
                actual_value=fact.value,
                residual=None,
                tolerance=0.0,
                status=STATUS_NOT_COMPARABLE,
                source_refs=(
                    SourceRef(
                        document_id=fact.document_id or None,
                        page=fact.page,
                        bbox=fact.bbox,
                        locator=f"{fact.source_table or '未知表'}",
                    ),
                ),
                severity=SEVERITY_WARNING,
                detected_at=detected_at,
                note=(
                    f"披露了 {period_key} 单季 {metric}，但累计端点 "
                    f"{'、'.join(missing) or '未知'} 不足以推导同口径单季值，"
                    "该单季数无法核对（不判错，也不视为通过）"
                ),
            )
        )
    # -- rule 3: a statement type with no derivable quarter is reported ------
    #
    # An annual report discloses FY and prior-FY only, so the engine legitimately
    # derives nothing. Leaving that as an *empty* family would make "nothing to
    # derive" indistinguishable from "derived and found nothing wrong" — §11
    # asks for the skip to be recorded, and §22 asks for the gap to be visible.
    for statement_type in FLOW_STATEMENTS:
        if derived.get(statement_type):
            continue
        if statement_type in refused:
            continue
        periods = sorted(
            {item.fiscal_period for item in statements if item.statement_type == statement_type}
        )
        if not periods:
            continue
        results.append(
            ConstraintViolation(
                violation_id=make_violation_id(
                    CONSTRAINT_ENDPOINT_COVERAGE, FAMILY_PERIOD,
                    (statement_type, "no-derivable-quarter", ",".join(periods)),
                ),
                constraint_family=FAMILY_PERIOD,
                constraint_id=CONSTRAINT_ENDPOINT_COVERAGE,
                inputs=tuple(
                    ConstraintOperand(
                        ref=f"statement:{statement_type}:{period}",
                        value=None, period=period, unit="元", label=statement_type,
                    )
                    for period in periods
                ),
                expected_value=None,
                actual_value=None,
                residual=None,
                tolerance=0.0,
                status=STATUS_SKIPPED_MISSING_FIELD,
                source_refs=tuple(
                    SourceRef(locator=f"财务报表 {statement_type} {period}")
                    for period in periods
                ),
                severity=SEVERITY_WARNING,
                detected_at=detected_at,
                note=(
                    f"{statement_type} 仅披露累计端点 {'、'.join(periods)}，"
                    "缺少单季化所需的中间端点（Q1/H1/9M），本次未产出任何单季值"
                    "（未核对，不等于通过）"
                ),
            )
        )
    return results
