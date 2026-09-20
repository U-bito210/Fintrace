"""Data models for the CARD-03 Financial Analysis Engine.

The whole point of this package is that **a financial comparison has a
semantic, not just a number**. The v2 design shipped ``compute_yoy(cur, prev)``
returning ``(cur - prev) / prev * 100`` unconditionally, which silently turns a
loss-making base period into an absurd, sign-flipped percentage
(``-100 → +50`` reads as "+50% growth" when the company actually lost money in
both years). Every comparison therefore carries a
:class:`ComparisonSemantic` alongside its value, and the state-transition cases
carry an **absolute** delta with ``delta_pct is None`` — enforced by
``src/validation`` and by the tests, not by convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ComparisonKind(str, Enum):
    """Which comparison produced a row (or none, for pure level rows)."""

    YOY = "yoy"
    QOQ = "qoq"
    LEVEL = "level"
    NONE = "none"


class ComparisonSemantic(str, Enum):
    """Profit-state aware semantics of a comparison (§6).

    Only :attr:`NORMAL` may carry a percentage. The transition/loss states are
    defined for a signed *profit* series and are meaningless for revenue; the
    comparison helpers therefore only apply them to profit metrics.
    """

    NORMAL = "normal"
    TURNAROUND = "turnaround"            # 扭亏为盈: prev < 0 < cur
    TO_LOSS = "to_loss"                  # 由盈转亏: prev > 0 > cur
    LOSS_WIDENING = "loss_widening"      # 同负，亏损扩大
    LOSS_NARROWING = "loss_narrowing"    # 同负，亏损收窄
    NOT_COMPUTABLE = "not_computable"    # 零基期 / 无变化 / 输入缺失


class Unit(str, Enum):
    """Output unit of an analysis metric."""

    YUAN = "元"
    PERCENT = "PERCENT"
    MULTIPLE = "MULTIPLE"


@dataclass(frozen=True)
class MetricResult:
    """One analysed metric for one period.

    ``value`` is the **level** of the metric in its own unit; ``delta_abs`` /
    ``delta_pct`` describe the comparison. ``calculation_id`` resolves inside
    :class:`~src.cn.analysis.calculations.CalculationRegistry`, which stores the
    formula, the operands (with their evidence IDs) and any formula constants —
    so "where did this number come from" is answerable without trusting the LLM.
    """

    metric: str
    label: str
    value: Optional[float]
    unit: str
    period_key: str
    period_basis: str                       # FY | TTM | SINGLE_QUARTER | POINT_IN_TIME
    comparison: Optional[ComparisonKind] = None
    comparison_semantic: Optional[ComparisonSemantic] = None
    delta_abs: Optional[float] = None
    delta_pct: Optional[float] = None
    calculation_id: Optional[str] = None
    scope: Optional[str] = None
    version: Optional[str] = None
    currency: Optional[str] = None
    inputs: Tuple[str, ...] = ()            # evidence IDs (CARD-02 normalized fact IDs)
    note: Optional[str] = None              # 口径 caveat, e.g. mixed-period ratio
    warnings: Tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        return self.comparison_semantic is ComparisonSemantic.NORMAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "period_key": self.period_key,
            "period_basis": self.period_basis,
            "comparison": self.comparison.value if self.comparison else None,
            "comparison_semantic": (
                self.comparison_semantic.value if self.comparison_semantic else None
            ),
            "delta_abs": self.delta_abs,
            "delta_pct": self.delta_pct,
            "calculation_id": self.calculation_id,
            "scope": self.scope,
            "version": self.version,
            "currency": self.currency,
            "inputs": list(self.inputs),
            "note": self.note,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DiagnosticSignal:
    """A **signal**, never a verdict (§7).

    The wording in ``detail`` is assembled deterministically from the numbers
    that triggered it. Downstream text (reports, memos) may quote it but must
    keep the hedge — the validator rejects "因为…所以导致" style causal claims
    that the numbers do not support.
    """

    signal_id: str
    label: str
    periods: Tuple[str, ...]
    detail: str
    metric_calculation_ids: Tuple[str, ...] = ()
    threshold: Optional[float] = None
    observed: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "label": self.label,
            "periods": list(self.periods),
            "detail": self.detail,
            "metric_calculation_ids": list(self.metric_calculation_ids),
            "threshold": self.threshold,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class MissingMetric:
    """A metric that could not be produced, and exactly why.

    ``blocking`` separates "this document does not carry the input" from "this
    metric does not apply to this kind of filing at all" (an annual report has no
    quarterly cumulative series, so a single-quarter QoQ is *not applicable*
    rather than missing data). Both are reported; only the former makes
    :attr:`AnalysisResult.data_complete` false.
    """

    metric: str
    period_key: Optional[str]
    period_basis: Optional[str]
    reason: str
    required_inputs: Tuple[str, ...] = ()
    blocking: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "period_key": self.period_key,
            "period_basis": self.period_basis,
            "reason": self.reason,
            "required_inputs": list(self.required_inputs),
            "blocking": self.blocking,
        }


@dataclass
class AnalysisResult:
    """Everything CARD-03 produces for one document / symbol."""

    document_id: str
    report_period: Optional[str]
    metrics: List[MetricResult] = field(default_factory=list)
    signals: List[DiagnosticSignal] = field(default_factory=list)
    missing: List[MissingMetric] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    registry: Any = None                     # CalculationRegistry (import cycle guard)
    scope: Optional[str] = None
    version: Optional[str] = None
    currency: Optional[str] = None
    #: CARD-04 signals that bear on this document's comparability. Populated by
    #: the pipeline; a caller that never ran CARD-04 leaves both empty, which
    #: reads as "not assessed" rather than "assessed and clean".
    scope_signals: List[Any] = field(default_factory=list)
    #: ``block_yoy`` / ``use_adjusted`` / ``warning`` / ``None`` (no signal).
    comparability: Optional[str] = None

    @property
    def data_complete(self) -> bool:
        """True only when no *blocking* input was dropped.

        A metric that is structurally not applicable to this filing (e.g. a
        single-quarter QoQ on an annual report) is reported in ``missing`` but
        does not make the analysis incomplete.
        """
        return not any(item.blocking for item in self.missing)

    @property
    def comparability_blocked(self) -> bool:
        """True when CARD-04 refused the comparison — no percentage was produced.

        Deliberately separate from ``data_complete``: withholding a number the
        evidence cannot support is the analysis working, not the analysis failing.
        """
        return self.comparability == "block_yoy"

    def metric(self, metric_id: str, *, period_key: Optional[str] = None) -> Optional[MetricResult]:
        for item in self.metrics:
            if item.metric != metric_id:
                continue
            if period_key is None or item.period_key == period_key:
                return item
        return None

    def by_metric(self, metric_id: str) -> List[MetricResult]:
        return [item for item in self.metrics if item.metric == metric_id]

    def signal_ids(self) -> List[str]:
        return [signal.signal_id for signal in self.signals]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "report_period": self.report_period,
            "scope": self.scope,
            "version": self.version,
            "currency": self.currency,
            "data_complete": self.data_complete,
            "comparability": self.comparability,
            "comparability_blocked": self.comparability_blocked,
            "scope_signals": [
                signal.to_dict() if hasattr(signal, "to_dict") else signal
                for signal in self.scope_signals
            ],
            "metrics": [item.to_dict() for item in self.metrics],
            "signals": [signal.to_dict() for signal in self.signals],
            "missing": [item.to_dict() for item in self.missing],
            "warnings": list(self.warnings),
        }
