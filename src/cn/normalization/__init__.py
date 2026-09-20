"""CARD-02 Financial Normalization Engine（财务规范化引擎）.

Turns heterogeneous A-share disclosure into canonical financial data across six
axes — unit, currency, ratio, period, 口径 (scope) and version — and then bridges
the result into the *existing* ``FinancialPeriodEngine``
(``src/cn/periods.py``) instead of reimplementing single-quarter / TTM maths.

Design rules this package enforces:

* **Base unit is 元.** ``万元``/``百万元``/``亿元`` are fixed powers of ten; an
  undeclared unit is a *failure*, not an assumption (§7.3 rule 1).
* **Period keys are the engine's**: ``YYYYQ1`` / ``YYYYH1`` / ``YYYY9M`` /
  ``YYYYFY``. Anything else raises (``FY2024``, ``2024Q1_YTD``, ``Q1 2024``).
* **flow vs stock is explicit.** Single quarters and TTM come only from the
  engine; a stock value fed to it raises ``PeriodEngineError`` and that
  exception is never swallowed (§10).
* **``period_kind`` of ``None`` + a warning beats a guess** (§7.2 / §9). 判错比
  不转换更糟.
* **Zero silence.** Every dropped value is counted in ``warnings``.

Quick start::

    from src.cn.normalization import normalize_financials

    result = normalize_financials(document_facts, document)
    result.facts            # NormalizedFact[]  (unit = 元, with back-links)
    result.single_quarters  # DerivedPeriod[]    (from the existing engine)
    result.ttm              # DerivedPeriod[]
    result.warnings         # every decision that was not automatic
"""

from .adapter import (
    DerivedGroup,
    GroupKey,
    ProjectionResult,
    derive_periods,
    derive_single_quarters,
    derive_ttm,
    facts_to_financial_statements,
    period_basis_for,
    pick_primary,
    primary_sort_key,
    project_statements,
)
from .metrics import (
    METRIC_INDEX,
    STANDARD_METRICS,
    MetricMatch,
    MetricSpec,
    is_flow_metric,
    is_stock_metric,
    metric_spec,
    resolve_metric,
    statement_value_key,
)
from .models import (
    BASE_UNIT,
    DEFAULT_CURRENCY,
    Currency,
    MetricSemantics,
    NormalizedFact,
    PeriodKind,
    Ratio,
    Scope,
    Unit,
    Version,
)
from .periods import (
    PeriodKindDecision,
    canonical_period,
    column_period_role,
    detect_period_kind,
    is_differentiable,
    is_supported_period_key,
    period_kind_decision,
    previous_period_key,
    report_kind,
)
from .pipeline import NormalizationResult, normalize_document, normalize_financials
from .evidence_bridge import (
    normalization_jumps,
    register_normalized_evidence,
)
from .scope import detect_scope, scope_token
from .units import (
    UNIT_SCALES,
    UnitNormalizationError,
    canonical_unit,
    normalize_unit,
    parse_numeric,
    split_embedded_unit,
    unit_scale,
)
from .version import detect_version, pick_effective, version_from_column, version_rank
from .warning_log import WarningLog

__all__ = [
    # models
    "Unit", "Currency", "Ratio", "Scope", "Version", "PeriodKind", "MetricSemantics",
    "NormalizedFact", "BASE_UNIT", "DEFAULT_CURRENCY",
    # units
    "UNIT_SCALES", "UnitNormalizationError", "normalize_unit", "canonical_unit",
    "unit_scale", "parse_numeric", "split_embedded_unit",
    # metric dictionary
    "MetricSpec", "MetricMatch", "STANDARD_METRICS", "METRIC_INDEX", "resolve_metric",
    "metric_spec", "statement_value_key", "is_stock_metric", "is_flow_metric",
    # periods
    "canonical_period", "detect_period_kind", "period_kind_decision", "PeriodKindDecision",
    "column_period_role", "previous_period_key", "report_kind", "is_supported_period_key",
    "is_differentiable",
    # scope / version
    "detect_scope", "scope_token", "detect_version", "version_rank", "pick_effective",
    "version_from_column",
    # adapter (bridges the existing period engine)
    "facts_to_financial_statements", "project_statements", "ProjectionResult", "GroupKey",
    "derive_single_quarters", "derive_ttm", "derive_periods", "DerivedGroup",
    "period_basis_for", "pick_primary", "primary_sort_key",
    # pipeline
    "normalize_document", "normalize_financials", "NormalizationResult", "WarningLog",
    # evidence ledger bridge (§12)
    "register_normalized_evidence", "normalization_jumps",
]
