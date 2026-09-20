"""Period comparison with profit-state semantics (CARD-03 §6).

Two financial-logic bugs from the v2 design are fixed here and are the reason
this module exists as its own unit:

1. **YTD fed straight into a QoQ.** In A-share disclosure, Q1/H1/9M/FY are
   *cumulative* — ``H1 / Q1 - 1`` is not a quarter-on-quarter change, it is a
   comparison of a half year against a quarter. Single-quarter values come only
   from :meth:`~src.cn.periods.FinancialPeriodEngine.derive_single_quarters`
   (reached through CARD-02); :func:`compute_single_quarter_qoq` refuses values
   that are not labelled ``SINGLE_QUARTER``.

2. **A percentage against a loss-making base.** ``(cur - prev) / prev`` flips
   sign when ``prev < 0``: a loss shrinking from −100 to −50 reports "−50%",
   which every reader parses as "profit fell 50%". Loss base periods therefore
   produce an **absolute** delta and no percentage at all.

A zero base period is ``not_computable`` — never ``0%``, never ``inf``.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .calculations import CalculationRegistry, Operand, constant_operand, evidence_operand
from .models import ComparisonKind, ComparisonSemantic, Unit

#: Metrics whose sign carries a profit/loss meaning. Revenue-like metrics are
#: always ``NORMAL``: a revenue drop is a revenue drop, not a "loss widening".
PROFIT_METRICS = frozenset(
    {
        "net_profit_parent",
        "net_profit_deducted",
        "net_profit",
        "operating_profit",
        "gross_profit",
    }
)

_PERCENT_UNIT = Unit.PERCENT.value


def _is_profit_metric(metric: str) -> bool:
    return metric in PROFIT_METRICS


def classify(current: float, previous: float, *, metric: str) -> ComparisonSemantic:
    """Deterministic profit-state classification (§6 table)."""
    if previous == 0:
        return ComparisonSemantic.NOT_COMPUTABLE
    if not _is_profit_metric(metric):
        return ComparisonSemantic.NORMAL
    if previous < 0 < current:
        return ComparisonSemantic.TURNAROUND
    if previous > 0 > current:
        return ComparisonSemantic.TO_LOSS
    if previous < 0 and current < 0:
        if current == previous:
            # Same loss in both periods: no movement to describe.
            return ComparisonSemantic.NOT_COMPUTABLE
        return (
            ComparisonSemantic.LOSS_NARROWING
            if current > previous
            else ComparisonSemantic.LOSS_WIDENING
        )
    return ComparisonSemantic.NORMAL


def compare(
    current: float,
    previous: float,
    *,
    metric: str,
    kind: ComparisonKind,
) -> Tuple[ComparisonSemantic, Optional[float], Optional[float]]:
    """Return ``(semantic, delta_abs, delta_pct)`` for one comparison.

    ``delta_pct`` is populated **only** for :attr:`ComparisonSemantic.NORMAL`.
    The transition and loss states get ``delta_abs`` and ``delta_pct=None`` so a
    percentage can never leak out of a case where it would mislead (§10).
    """
    semantic = classify(current, previous, metric=metric)
    if semantic is ComparisonSemantic.NOT_COMPUTABLE:
        return semantic, None, None
    delta_abs = float(current - previous)
    if semantic is not ComparisonSemantic.NORMAL:
        return semantic, delta_abs, None
    return semantic, delta_abs, float(delta_abs / abs(previous) * 100.0)


def build_comparison_calculation(
    registry: CalculationRegistry,
    *,
    metric: str,
    current_operand: Operand,
    previous_operand: Operand,
    semantic: ComparisonSemantic,
    delta_abs: Optional[float],
    delta_pct: Optional[float],
    period_key: str,
    period_basis: str,
    scope: Optional[str] = None,
    version: Optional[str] = None,
    currency: Optional[str] = None,
    comparison: ComparisonKind = ComparisonKind.YOY,
):
    """Record the comparison arithmetic, keeping the *semantic* branch auditable.

    ``growth_pct`` is used only for ``NORMAL``; the transition states record a
    plain ``subtract`` (the absolute change) and are therefore structurally
    incapable of carrying a percentage.
    """
    token = comparison.value
    if semantic is ComparisonSemantic.NORMAL:
        value = delta_pct
        return registry.register(
            metric=f"{metric}_{token}",
            formula=f"({metric}_current - {metric}_previous) / |{metric}_previous| * 100",
            operation="growth_pct",
            operands=[current_operand, previous_operand],
            value=value,
            unit=_PERCENT_UNIT,
            period_key=period_key,
            period_basis=period_basis,
            scope=scope,
            version=version,
            currency=currency,
            source="derived",
            engine="src.cn.analysis.comparisons",
            note="同比/环比正常分支（百分比）",
            key_hint=token,
        )
    if delta_abs is None:
        return None
    return registry.register(
        metric=f"{metric}_{token}",
        formula=f"({metric}_current - {metric}_previous)  # {semantic.value}：仅绝对变动",
        operation="subtract",
        operands=[current_operand, previous_operand],
        value=delta_abs,
        unit=Unit.YUAN.value,
        period_key=period_key,
        period_basis=period_basis,
        scope=scope,
        version=version,
        currency=currency,
        source="derived",
        engine="src.cn.analysis.comparisons",
        note=f"状态迁移语义（{semantic.value}），按 §10 不输出百分比",
        key_hint=token,
    )


def compute_yoy(
    registry: CalculationRegistry,
    *,
    metric: str,
    label: str,
    current_fact: object,
    previous_fact: object,
    current_period: str,
    previous_period: str,
    period_basis: str,
    scope: Optional[str] = None,
    version: Optional[str] = None,
    currency: Optional[str] = None,
    suppress_reason: Optional[str] = None,
):
    """Build one YoY ``MetricResult`` from two CARD-02 facts.

    Returns ``None`` when the comparison is not computable (zero base) — the
    caller registers a ``MissingMetric`` instead of inventing a number.

    ``suppress_reason`` is CARD-04 speaking: when a 会计口径 disclosure makes the
    two ends of the comparison non-comparable, the *level* is still a fact and is
    returned, but no percentage is produced and no calculation is registered —
    there is no formula whose operands would be comparable. Passing a reason
    rather than a boolean forces the caller to say why, so the audit trail can
    never contain a bare "blocked".
    """
    current_value = float(getattr(current_fact, "value"))
    previous_value = float(getattr(previous_fact, "value"))

    if suppress_reason:
        from .models import MetricResult

        return MetricResult(
            metric=metric,
            label=label,
            value=current_value,
            unit=Unit.YUAN.value,
            period_key=current_period,
            period_basis=period_basis,
            comparison=ComparisonKind.LEVEL,
            comparison_semantic=None,
            calculation_id=None,
            scope=scope,
            version=version,
            currency=currency,
            inputs=(
                str(getattr(current_fact, "normalized_fact_id", "")),
                str(getattr(previous_fact, "normalized_fact_id", "")),
            ),
            note=suppress_reason,
            warnings=("comparability_blocked",),
        )

    semantic, delta_abs, delta_pct = compare(
        current_value, previous_value, metric=metric, kind=ComparisonKind.YOY
    )

    from .models import MetricResult

    if semantic is ComparisonSemantic.NOT_COMPUTABLE:
        return MetricResult(
            metric=metric,
            label=label,
            value=current_value,
            unit=Unit.YUAN.value,
            period_key=current_period,
            period_basis=period_basis,
            comparison=ComparisonKind.YOY,
            comparison_semantic=semantic,
            calculation_id=None,
            scope=scope,
            version=version,
            currency=currency,
            inputs=(
                str(getattr(current_fact, "normalized_fact_id", "")),
                str(getattr(previous_fact, "normalized_fact_id", "")),
            ),
            note="基期为 0，同比不可计算（禁止输出百分比）",
            warnings=("previous_period_is_zero",),
        )

    record = build_comparison_calculation(
        registry,
        metric=metric,
        current_operand=evidence_operand(current_fact),
        previous_operand=evidence_operand(previous_fact),
        semantic=semantic,
        delta_abs=delta_abs,
        delta_pct=delta_pct,
        period_key=current_period,
        period_basis=period_basis,
        scope=scope,
        version=version,
        currency=currency,
        comparison=ComparisonKind.YOY,
    )
    return MetricResult(
        metric=metric,
        label=label,
        value=current_value,
        unit=Unit.YUAN.value,
        period_key=current_period,
        period_basis=period_basis,
        comparison=ComparisonKind.YOY,
        comparison_semantic=semantic,
        delta_abs=delta_abs,
        delta_pct=delta_pct,
        calculation_id=record.calculation_id,
        scope=scope,
        version=version,
        currency=currency,
        inputs=(
            str(getattr(current_fact, "normalized_fact_id", "")),
            str(getattr(previous_fact, "normalized_fact_id", "")),
        ),
        note=_semantic_note(semantic, previous_period),
    )


def _comparison_basis(operand: object) -> Optional[str]:
    """The declared period semantics of an operand, however it is expressed.

    Three carriers exist across the chain and all must be inspected, because
    silently defaulting to ``SINGLE_QUARTER`` would let a cumulative YTD value
    slip through the one guard that exists to stop it:

    * ``MetricResult.period_basis`` — an explicit override;
    * ``PeriodValue.basis`` — how the analysis series tags an engine-derived value;
    * ``NormalizedFact.period_kind`` — CARD-02's declared semantics.
    """
    for field in ("period_basis", "basis"):
        explicit = getattr(operand, field, None)
        if explicit:
            return str(explicit)
    kind = getattr(operand, "period_kind", None)
    if kind is None:
        return None
    return str(getattr(kind, "value", kind))


def compute_single_quarter_qoq(
    registry: CalculationRegistry,
    *,
    metric: str,
    label: str,
    current_quarter: object,
    previous_quarter: object,
    period_basis: str = "SINGLE_QUARTER",
    scope: Optional[str] = None,
    version: Optional[str] = None,
    currency: Optional[str] = None,
    left_label: str = "",
    right_label: str = "",
):
    """Build one quarter-on-quarter ``MetricResult`` from two single quarters.

    Both operands **must** already be single-quarter values (produced by the
    existing period engine via CARD-02): feeding a cumulative YTD value here is
    the v2 bug this function exists to prevent, so it raises instead.
    """
    from .models import MetricResult

    for name, operand in (("current", current_quarter), ("previous", previous_quarter)):
        basis = _comparison_basis(operand)
        if basis is None:
            raise ValueError(
                f"环比输入的期间语义不可判定（{name} 既无 period_basis 也无 period_kind），拒绝按单季处理。"
            )
        if basis != "SINGLE_QUARTER":
            raise ValueError(
                f"环比输入必须是单季值（SINGLE_QUARTER），{name} 收到 '{basis}'。"
                "累计值（YTD）必须先经 FinancialPeriodEngine 单季化。"
            )

    current_value = float(getattr(current_quarter, "value"))
    previous_value = float(getattr(previous_quarter, "value"))
    semantic, delta_abs, delta_pct = compare(
        current_value, previous_value, metric=metric, kind=ComparisonKind.QOQ
    )
    current_period = str(getattr(current_quarter, "period_key", ""))
    previous_period = str(getattr(previous_quarter, "period_key", ""))

    if semantic is ComparisonSemantic.NOT_COMPUTABLE:
        return MetricResult(
            metric=metric,
            label=label,
            value=current_value,
            unit=Unit.YUAN.value,
            period_key=current_period,
            period_basis=period_basis,
            comparison=ComparisonKind.QOQ,
            comparison_semantic=semantic,
            scope=scope,
            version=version,
            currency=currency,
            inputs=(
                str(getattr(current_quarter, "normalized_fact_id", "") or getattr(current_quarter, "calculation_id", "")),
                str(getattr(previous_quarter, "normalized_fact_id", "") or getattr(previous_quarter, "calculation_id", "")),
            ),
            note="上季为 0，环比不可计算（禁止输出百分比）",
            warnings=("previous_quarter_is_zero",),
        )

    def operand_for(value: object, label_text: str) -> Operand:
        # Single quarters come from the period engine, so they are *calculated*
        # rather than disclosed: keep that distinction in the audit trail.
        calc_id = getattr(value, "calculation_id", None)
        if calc_id:
            return Operand(
                kind="calculation", operand_id=str(calc_id), value=float(getattr(value, "value")), label=label_text
            )
        fact_id = getattr(value, "normalized_fact_id", None)
        if fact_id:
            return evidence_operand(value)
        return Operand(kind="calculation", operand_id=label_text or "single_quarter", value=float(getattr(value, "value")))

    record = build_comparison_calculation(
        registry,
        metric=metric,
        current_operand=operand_for(current_quarter, left_label),
        previous_operand=operand_for(previous_quarter, right_label),
        semantic=semantic,
        delta_abs=delta_abs,
        delta_pct=delta_pct,
        period_key=current_period,
        period_basis=period_basis,
        scope=scope,
        version=version,
        currency=currency,
        comparison=ComparisonKind.QOQ,
    )
    return MetricResult(
        metric=metric,
        label=label,
        value=current_value,
        unit=Unit.YUAN.value,
        period_key=current_period,
        period_basis="SINGLE_QUARTER",
        comparison=ComparisonKind.QOQ,
        comparison_semantic=semantic,
        delta_abs=delta_abs,
        delta_pct=delta_pct,
        calculation_id=record.calculation_id,
        scope=scope,
        version=version,
        currency=currency,
        inputs=(str(getattr(current_quarter, "normalized_fact_id", "")), str(getattr(previous_quarter, "normalized_fact_id", ""))),
        note=_semantic_note(semantic, previous_period) or "单季环比（引擎差分产品）",
    )


def compute_ratio(
    registry: CalculationRegistry,
    *,
    metric: str,
    label: str,
    numerator_operand: Operand,
    denominator_operand: Operand,
    value: Optional[float],
    unit: str,
    period_key: str,
    period_basis: str,
    formula: str,
    scope: Optional[str] = None,
    version: Optional[str] = None,
    currency: Optional[str] = None,
    note: Optional[str] = None,
    engine: Optional[str] = None,
    extra_operands: Tuple[Operand, ...] = (),
):
    """Record a ratio metric (margin / ROE / turnover-style) with its formula."""
    from .models import MetricResult

    if value is None:
        return None
    record = registry.register(
        metric=metric,
        formula=formula,
        operation="percent_of",
        operands=[numerator_operand, denominator_operand],
        value=value,
        unit=unit,
        period_key=period_key,
        period_basis=period_basis,
        scope=scope,
        version=version,
        currency=currency,
        source="derived",
        engine=engine or "src.cn.analysis.metrics",
        note=note,
        key_hint=period_basis.lower(),
    )
    return MetricResult(
        metric=metric,
        label=label,
        value=value,
        unit=unit,
        period_key=period_key,
        period_basis=period_basis,
        comparison=ComparisonKind.LEVEL,
        # A level ratio has no base period, so it carries *no* comparison
        # semantic. Declaring ``NORMAL`` here would set ``comparable=True`` on a
        # row that has nothing to compare, and a downstream claim generator
        # reading ``delta_pct`` would then be handed ``None`` with no signal that
        # the row was never a comparison.
        comparison_semantic=None,
        calculation_id=record.calculation_id,
        scope=scope,
        version=version,
        currency=currency,
        inputs=tuple(op.operand_id for op in (numerator_operand, denominator_operand) + extra_operands),
        note=note,
    )


def _semantic_note(semantic: ComparisonSemantic, previous_period: str) -> Optional[str]:
    if semantic is ComparisonSemantic.TURNAROUND:
        return f"{previous_period} 亏损、本期盈利（扭亏为盈）；按 §10 仅给出绝对变动额"
    if semantic is ComparisonSemantic.TO_LOSS:
        return f"{previous_period} 盈利、本期亏损（由盈转亏）；按 §10 仅给出绝对变动额"
    if semantic is ComparisonSemantic.LOSS_WIDENING:
        return f"两期均亏损且亏损扩大（对比 {previous_period}）；仅给出绝对变动额"
    if semantic is ComparisonSemantic.LOSS_NARROWING:
        return f"两期均亏损且亏损收窄（对比 {previous_period}）；仅给出绝对变动额"
    return None


def average_operand(
    registry: CalculationRegistry,
    *,
    first: object,
    second: object,
    metric: str,
    period_key: str,
    period_basis: str,
    formula: str,
    scope: Optional[str] = None,
    version: Optional[str] = None,
    currency: Optional[str] = None,
):
    """``(begin + end) / 2`` as an explicit calculation (used by ROE)."""
    return registry.register(
        metric=f"{metric}_average",
        formula=formula,
        operation="average",
        operands=[evidence_operand(first), evidence_operand(second)],
        value=(float(getattr(first, "value")) + float(getattr(second, "value"))) / 2.0,
        unit=Unit.YUAN.value,
        period_key=period_key,
        period_basis=period_basis,
        scope=scope,
        version=version,
        currency=currency,
        source="derived",
        engine="src.cn.analysis.comparisons",
        note="期初与期末的算术平均（两点近似）",
        key_hint="average",
    )


__all__ = [
    "PROFIT_METRICS",
    "average_operand",
    "build_comparison_calculation",
    "classify",
    "compare",
    "compute_ratio",
    "compute_single_quarter_qoq",
    "compute_yoy",
    "constant_operand",
    "evidence_operand",
]
