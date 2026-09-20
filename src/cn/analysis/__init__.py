"""CARD-03 Financial Analysis Engine（财务分析引擎）.

Deterministic analysis on top of CARD-02's normalized data: 14 metrics with an
explicit period alignment table, plus four diagnostic signals. It fixes the two
financial-logic bugs of the v2 design:

* **YTD fed straight into a QoQ** — single-quarter comparisons accept only
  values the existing ``FinancialPeriodEngine`` produced by differencing
  cumulative disclosures, so ``H1 / Q1 − 1`` can no longer masquerade as a
  quarter-on-quarter change.
* **A percentage against a loss-making base** — a loss base period yields an
  **absolute** change and no percentage at all (扭亏为盈 / 由盈转亏 / 亏损扩大 /
  亏损收窄), because ``(cur − prev) / prev`` flips sign when ``prev < 0``.

Design rules this package enforces:

* **Period maths has one home.** Single quarters and TTM come from
  ``src/cn/periods.py`` via CARD-02's projection; this package adds no
  differencing of its own.
* **Every number is a recorded calculation.** Each metric row carries a
  ``calculation_id`` resolvable in :class:`CalculationRegistry` with its formula
  and operands; the tests re-run every one of them.
* **Signals, not verdicts.** Diagnostics state observed movements only. A signal
  never becomes a causal claim the numbers cannot support (§10).
* **Zero silence.** A metric that cannot be produced is registered in
  ``AnalysisResult.missing`` with the inputs it lacked.

Quick start::

    from src.cn.analysis import analyze_document

    result = analyze_document(normalization_result)   # CARD-02 output
    result.metrics        # MetricResult[]  (with calculation_id + inputs)
    result.signals        # DiagnosticSignal[]
    result.data_complete  # False when anything was dropped
"""

from .calculations import (
    LEDGER_EXPRESSIBLE,
    OPERAND_CALCULATION,
    OPERAND_CONSTANT,
    OPERAND_EVIDENCE,
    SUPPORTED_OPERATIONS,
    Calculation,
    CalculationError,
    CalculationRegistry,
    Operand,
    calculation_operand,
    constant_operand,
    evidence_operand,
    is_ledger_publishable,
)
from .comparisons import (
    PROFIT_METRICS,
    average_operand,
    build_comparison_calculation,
    classify,
    compare,
    compute_ratio,
    compute_single_quarter_qoq,
    compute_yoy,
)
from .diagnostics import (
    GROWTH_GAP_THRESHOLD_PCT,
    SIGNAL_EARNINGS_QUALITY,
    SIGNAL_INVENTORY_PRESSURE,
    SIGNAL_LABELS,
    SIGNAL_NON_RECURRING,
    SIGNAL_RECEIVABLE_RISK,
    run_diagnostics,
)
from .evidence_bridge import (
    SKIPPED_CONSTANT,
    SKIPPED_MISSING_INPUT,
    SKIPPED_NOT_LEDGER_SHAPED,
    analysis_jumps,
    publish_analysis_evidence,
)
from .metrics import (
    ANALYSIS_METRICS,
    BASIS_FY,
    BASIS_POINT_IN_TIME,
    BASIS_SINGLE_QUARTER,
    BASIS_TTM,
    CAPEX,
    CFO_TO_NET_PROFIT,
    CONTRACT_LIABILITIES,
    DEDUCTED_NET_PROFIT_YOY,
    DUAL_BASIS_RATIO_METRICS,
    FREE_CASH_FLOW,
    FY_AND_TTM_YOY_METRICS,
    GROSS_MARGIN,
    INVENTORY_TO_REVENUE,
    METRIC_INDEX,
    NET_MARGIN,
    NET_PROFIT_YOY,
    NON_RECURRING_RATIO,
    RATIO_FORMULAS,
    RECEIVABLES_TO_REVENUE,
    REVENUE_QOQ_SINGLE_QUARTER,
    REVENUE_YOY,
    ROE,
    MetricDefinition,
    metric_definition,
    metrics_for_basis,
)
from .models import (
    AnalysisResult,
    ComparisonKind,
    ComparisonSemantic,
    DiagnosticSignal,
    MetricResult,
    MissingMetric,
    Unit,
)
from .pipeline import analyze_document, analyze_financials
from .scope_signals import (
    COMPARABILITY_LABELS,
    DECISION_PRECEDENCE,
    DECISION_RULES,
    SIGNAL_TYPE_LABELS,
    SURFACE_CHAPTER,
    SURFACE_PARAGRAPH,
    SURFACE_TABLE_COLUMN,
    SURFACE_TITLE,
    AccountingScopeSignal,
    ComparabilityDecision,
    ScopeSignalType,
    SignalSeverity,
    aggregate_comparability,
    build_scope_report,
    comparability_decision,
    detect_scope_signals,
    detection_coverage,
)
from .series import (
    MetricSeries,
    PeriodValue,
    basis_for_fact,
    build_series,
    previous_quarter_key,
    previous_year_key,
    primary_series_key,
    split_period_key,
)

__all__ = [
    # models
    "AnalysisResult", "MetricResult", "DiagnosticSignal", "MissingMetric",
    "ComparisonKind", "ComparisonSemantic", "Unit",
    # calculations
    "Calculation", "CalculationRegistry", "CalculationError", "Operand",
    "SUPPORTED_OPERATIONS", "LEDGER_EXPRESSIBLE", "OPERAND_EVIDENCE",
    "OPERAND_CALCULATION", "OPERAND_CONSTANT", "evidence_operand",
    "calculation_operand", "constant_operand", "is_ledger_publishable",
    # comparisons (§6 / §10)
    "compare", "classify", "compute_yoy", "compute_single_quarter_qoq",
    "compute_ratio", "average_operand", "build_comparison_calculation",
    "PROFIT_METRICS",
    # metric dictionary (§7)
    "ANALYSIS_METRICS", "METRIC_INDEX", "MetricDefinition", "metric_definition",
    "metrics_for_basis", "BASIS_FY", "BASIS_TTM", "BASIS_SINGLE_QUARTER",
    "BASIS_POINT_IN_TIME", "RATIO_FORMULAS", "FY_AND_TTM_YOY_METRICS",
    "DUAL_BASIS_RATIO_METRICS",
    "REVENUE_YOY", "REVENUE_QOQ_SINGLE_QUARTER", "NET_PROFIT_YOY",
    "DEDUCTED_NET_PROFIT_YOY", "GROSS_MARGIN", "NET_MARGIN", "ROE",
    "CFO_TO_NET_PROFIT", "RECEIVABLES_TO_REVENUE", "INVENTORY_TO_REVENUE",
    "CONTRACT_LIABILITIES", "CAPEX", "FREE_CASH_FLOW", "NON_RECURRING_RATIO",
    # series
    "MetricSeries", "PeriodValue", "build_series", "basis_for_fact",
    "split_period_key", "previous_year_key", "previous_quarter_key",
    "primary_series_key",
    # diagnostics (§7)
    "run_diagnostics", "GROWTH_GAP_THRESHOLD_PCT", "SIGNAL_LABELS",
    "SIGNAL_EARNINGS_QUALITY", "SIGNAL_RECEIVABLE_RISK",
    "SIGNAL_INVENTORY_PRESSURE", "SIGNAL_NON_RECURRING",
    # pipeline
    "analyze_financials", "analyze_document",
    # evidence bridge (§10 / §12)
    "publish_analysis_evidence", "analysis_jumps", "SKIPPED_CONSTANT",
    "SKIPPED_NOT_LEDGER_SHAPED", "SKIPPED_MISSING_INPUT",
    # scope signals (CARD-04)
    "AccountingScopeSignal", "ScopeSignalType", "SignalSeverity",
    "ComparabilityDecision", "DECISION_RULES", "DECISION_PRECEDENCE",
    "detect_scope_signals", "comparability_decision", "aggregate_comparability",
    "build_scope_report", "detection_coverage",
    "SURFACE_TITLE", "SURFACE_CHAPTER", "SURFACE_PARAGRAPH", "SURFACE_TABLE_COLUMN",
    "SIGNAL_TYPE_LABELS", "COMPARABILITY_LABELS",
]
