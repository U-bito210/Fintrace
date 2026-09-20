"""``analyze_financials`` — CARD-03 主流程.

The engine sits **on top of CARD-02** and adds no new period maths: when the
caller does not hand over the engine-derived periods, they are produced by
CARD-02's own projection + ``derive_periods`` (which delegates to
``FinancialPeriodEngine``). Single quarters and TTM therefore have exactly one
implementation in this codebase, and CARD-03 cannot drift from it.

Every produced number is registered in a :class:`CalculationRegistry` with its
formula and operands; every number that could *not* be produced is registered in
``AnalysisResult.missing``. There is no third state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..normalization import (
    NormalizedFact,
    derive_periods,
    project_statements,
)
from .calculations import (
    CalculationRegistry,
    Operand,
    OPERAND_CALCULATION,
    calculation_operand,
    evidence_operand,
)
from .scope_signals import (
    ComparabilityDecision,
    aggregate_comparability,
    detect_scope_signals,
)
from .comparisons import (
    average_operand,
    compute_ratio,
    compute_single_quarter_qoq,
    compute_yoy,
)
from .diagnostics import run_diagnostics
from .metrics import (
    BASIS_FY,
    BASIS_POINT_IN_TIME,
    BASIS_SINGLE_QUARTER,
    BASIS_TTM,
    CAPEX,
    CFO_TO_NET_PROFIT,
    CONTRACT_LIABILITIES,
    DEDUCTED_NET_PROFIT_YOY,
    FREE_CASH_FLOW,
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
)
from .models import AnalysisResult, ComparisonKind, MetricResult, MissingMetric, Unit
from .series import (
    BASIS_9M,
    BASIS_H1,
    BASIS_Q1,
    MetricSeries,
    PeriodValue,
    build_series,
    merge_versions,
    newest_version,
    previous_quarter_key,
    previous_year_key,
    primary_series_key,
    split_period_key,
)

ENGINE = "FinancialPeriodEngine (src/cn/periods.py)"

#: Cumulative bases in ascending duration — used to pick a document's own basis.
_BASIS_ORDER = (BASIS_Q1, BASIS_H1, BASIS_9M, BASIS_FY)

#: YoY metric ids that are reported on the document's own basis and on TTM.
_FLOW_METRICS = (
    REVENUE_YOY,
    NET_PROFIT_YOY,
    DEDUCTED_NET_PROFIT_YOY,
    CAPEX,
    FREE_CASH_FLOW,
)

#: Ratio metric ids reported on the document's own basis and on TTM.
_RATIO_METRICS = (
    GROSS_MARGIN,
    NET_MARGIN,
    ROE,
    CFO_TO_NET_PROFIT,
    RECEIVABLES_TO_REVENUE,
    INVENTORY_TO_REVENUE,
    NON_RECURRING_RATIO,
)

#: Ratio metrics that legitimately mix a flow with a period-end stock.
_MIXED_PERIOD_RATIOS = (RECEIVABLES_TO_REVENUE, INVENTORY_TO_REVENUE)

#: Canonical CARD-02 key consumed as the numerator of a flow-only ratio.
_RATIO_NUMERATOR: Dict[str, Optional[str]] = {
    GROSS_MARGIN: "gross_profit",
    NET_MARGIN: "net_profit_parent",
    CFO_TO_NET_PROFIT: "cfo",
    NON_RECURRING_RATIO: None,          # an intermediate calculation, not a metric
}

#: Canonical CARD-02 key consumed as the denominator of a ratio.
_RATIO_DENOMINATOR: Dict[str, Optional[str]] = {
    GROSS_MARGIN: "revenue",
    NET_MARGIN: "revenue",
    CFO_TO_NET_PROFIT: "net_profit_parent",
    RECEIVABLES_TO_REVENUE: "revenue",
    INVENTORY_TO_REVENUE: "revenue",
    NON_RECURRING_RATIO: "net_profit_parent",
}

_ROE_DISCLOSED_KEY = "roe_weighted_disclosed"


def analyze_financials(
    facts: Sequence[NormalizedFact],
    *,
    document_id: str = "",
    report_period: Optional[str] = None,
    single_quarters: Optional[Sequence[object]] = None,
    ttm: Optional[Sequence[object]] = None,
    scope: Optional[str] = None,
    version: Optional[str] = None,
    disclosed_ratios: Optional[Dict[str, float]] = None,
    registry: Optional[CalculationRegistry] = None,
    scope_signals: Optional[Sequence[object]] = None,
) -> AnalysisResult:
    """Analyse one document's normalized facts.

    ``single_quarters`` / ``ttm`` may be supplied (CARD-02's ``NormalizationResult``
    carries them); otherwise CARD-02's projection + ``derive_periods`` is invoked
    to produce them here, so the function is usable from a bare fact list.

    ``scope_signals`` are CARD-04's 会计口径 signals. When they say ``block_yoy``,
    no percentage is produced for this document: the two ends of every year-on-year
    comparison are not comparable, and §9's rule is 宁可阻断也不误算. The level
    rows survive — they are still facts — but with no comparison attached.
    """
    warnings: List[str] = list()
    reg = registry if registry is not None else CalculationRegistry()

    signals = list(scope_signals or ())
    comparability = aggregate_comparability(signals)
    comparability_value = comparability.value if comparability is not None else None
    block_reason: Optional[str] = None
    #: CARD-04 §7.2: when the adjusted data exists, the comparison must *use* it.
    #: That is a change to how periods are resolved, not just a label — see
    #: ``series.merge_versions``.
    use_adjusted = comparability is ComparabilityDecision.USE_ADJUSTED
    if comparability is ComparabilityDecision.BLOCK_YOY:
        blocked = sorted(
            {
                signal.signal_type.value
                for signal in signals
                if getattr(signal, "comparability", None) is ComparabilityDecision.BLOCK_YOY
            }
        )
        block_reason = (
            "可比性已阻断（CARD-04）："
            + "、".join(blocked)
            + "；两端数据不可比，不输出同比百分比，仅保留水平值。"
        )
        warnings.append(block_reason)
    elif use_adjusted:
        adjusted_types = sorted(
            {
                signal.signal_type.value
                for signal in signals
                if getattr(signal, "comparability", None) is ComparabilityDecision.USE_ADJUSTED
            }
        )
        warnings.append(
            "可比性提示（CARD-04）：比较数据取自调整后/重述后版本（"
            + "、".join(adjusted_types)
            + "），直接比较原始披露值将不可比；本次已按该决策取数。"
        )

    facts_list = list(facts)
    if single_quarters is None or ttm is None:
        derived_sq, derived_ttm = _derive_periods(facts_list, warnings)
        if single_quarters is None:
            single_quarters = derived_sq
        if ttm is None:
            ttm = derived_ttm

    series = build_series(
        facts_list, single_quarters=single_quarters or (), ttm=ttm or (), warnings=warnings
    )
    if series and use_adjusted:
        series = merge_versions(series, warnings=warnings)
    if not series:
        result = AnalysisResult(
            document_id=document_id or _first_document_id(facts_list),
            report_period=report_period,
            warnings=warnings,
            registry=reg,
            scope_signals=signals,
            comparability=comparability_value,
        )
        for definition in METRIC_INDEX.values():
            result.missing.append(
                MissingMetric(
                    metric=definition.metric,
                    period_key=report_period,
                    period_basis=definition.period_basis,
                    reason="无可用的规范化事实（未识别出任何可分析的指标行）",
                    required_inputs=definition.inputs,
                )
            )
        return result

    group_scope, group_version = _select_group(
        series, scope=scope, version=version, warnings=warnings, merged=use_adjusted
    )
    primary_basis, primary_period = _select_basis(series, report_period=report_period)
    if primary_period is None:
        warnings.append("无法确定文档的报告期基期，仅输出存量指标与已有期间的水平值。")

    def series_for(metric: str) -> Optional[MetricSeries]:
        return _series_in_group(series, metric, group_scope, group_version, merged=use_adjusted)

    def lookup(metric: str, basis: str, period_key: str) -> Optional[PeriodValue]:
        found = series_for(metric)
        return found.get(basis, period_key) if found else None

    result = AnalysisResult(
        document_id=document_id or _first_document_id(facts_list),
        report_period=report_period or primary_period,
        warnings=warnings,
        registry=reg,
        scope=group_scope,
        version=group_version,
        currency=_group_currency(series, group_scope, group_version, merged=use_adjusted),
        scope_signals=signals,
        comparability=comparability_value,
    )

    bases: List[str] = []
    if primary_basis:
        bases.append(primary_basis)
    # TTM adds nothing at FY: the engine returns the FY value itself as its own
    # TTM (``derive_ttm`` short-circuits annual records), so a second row would
    # duplicate the same number under a different label.
    if primary_basis and primary_basis != BASIS_FY:
        bases.append(BASIS_TTM)

    for basis in bases:
        period_key = primary_period
        if period_key is None:
            continue
        for metric_id in _FLOW_METRICS:
            _emit_yoy(
                result,
                reg,
                metric_id=metric_id,
                basis=basis,
                period_key=period_key,
                lookup=lookup,
                scope=group_scope,
                version=group_version,
                currency=result.currency,
                suppress_reason=block_reason,
            )
        for metric_id in _RATIO_METRICS:
            _emit_ratio(
                result,
                reg,
                metric_id=metric_id,
                basis=basis,
                period_key=period_key,
                lookup=lookup,
                scope=group_scope,
                version=group_version,
                currency=result.currency,
                disclosed_ratios=disclosed_ratios or {},
            )

    # Single-quarter QoQ rows: one per quarter that has a predecessor.
    _emit_single_quarter_qoq(
        result,
        reg,
        series_for=series_for,
        scope=group_scope,
        version=group_version,
        currency=result.currency,
        quarterly_disclosed=any(
            basis in (BASIS_Q1, BASIS_H1, BASIS_9M)
            for item in series.values()
            for basis, _key in item.values
        ),
    )

    # Stock metric: level only, never differenced (§7 contract liabilities row).
    _emit_stock_levels(
        result, reg, series_for=series_for, scope=group_scope, version=group_version,
        currency=result.currency,
    )

    result.signals = run_diagnostics(
        series=series,
        metrics=result.metrics,
        scope=group_scope,
        # In a merged view every series carries ``MERGED_VERSION`` as its key slot
        # and its own newest disclosure as ``version``, so filtering on the
        # document-level summary would drop a metric that only exists as-reported.
        # The metric+scope match is the whole criterion there.
        version=None if use_adjusted else group_version,
    )
    return result


def analyze_document(normalization_result: object, *, documents: object = None, **kwargs) -> AnalysisResult:
    """Convenience wrapper over CARD-02's ``NormalizationResult``.

    CARD-04's signals are derived from the result's own ``source_facts``, so the
    comparability gate cannot be skipped by forgetting to pass them. A caller may
    still override ``scope_signals`` explicitly (the service layer already has
    them and would otherwise pay for detecting twice).
    """
    facts = list(getattr(normalization_result, "facts", ()) or ())
    if "scope_signals" not in kwargs:
        source = list(getattr(normalization_result, "source_facts", ()) or ())
        if source:
            kwargs["scope_signals"] = detect_scope_signals(source, documents)
    return analyze_financials(
        facts,
        document_id=str(getattr(normalization_result, "document_id", "") or ""),
        report_period=getattr(normalization_result, "report_period", None),
        single_quarters=list(getattr(normalization_result, "single_quarters", ()) or ()),
        ttm=list(getattr(normalization_result, "ttm", ()) or ()),
        **kwargs,
    )


# -- internals ---------------------------------------------------------------


def _derive_periods(
    facts: List[NormalizedFact], warnings: List[str]
) -> Tuple[List[object], List[object]]:
    """Reuse CARD-02's projection + derivation so period maths has one home."""
    try:
        projection = project_statements(facts, warnings=warnings)
        groups = derive_periods(projection, warnings=warnings)
    except Exception as exc:  # pragma: no cover - defensive: never abort the analysis
        warnings.append(f"期间派生失败（{type(exc).__name__}: {exc}），单季与 TTM 指标不可用。")
        return [], []
    return (
        [period for group in groups for period in group.single_quarters],
        [period for group in groups for period in group.ttm],
    )


def _first_document_id(facts: List[NormalizedFact]) -> str:
    for fact in facts:
        if fact.document_id:
            return fact.document_id
    return ""


def _select_group(
    series: Dict[Tuple[str, str, str], MetricSeries],
    *,
    scope: Optional[str],
    version: Optional[str],
    warnings: List[str],
    merged: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Pick the 口径/版本 the report speaks about (never a silent average).

    ``merged`` says the version dimension has already been collapsed by CARD-04's
    decision (``series.merge_versions``). The version is then a *summary* of the
    newest disclosure each series rests on — the most recent one wins, because
    "newest data in the document" is the only reading consistent with
    ``use_adjusted``; picking alphabetically would select ``AS_REPORTED`` over
    ``RESTATED`` and silently undo the decision.
    """
    scopes = sorted({item.scope for item in series.values() if item.scope}, key=lambda item: item or "")
    versions = sorted({item.version for item in series.values() if item.version})
    chosen_scope = scope or ("合并" if "合并" in scopes else (scopes[0] if scopes else None))
    if version:
        chosen_version: Optional[str] = version
    elif merged:
        chosen_version = newest_version(versions) or None
    else:
        chosen_version = versions[0] if versions else "AS_REPORTED"
    if len(scopes) > 1 and scope is None:
        warnings.append(
            f"文档同时包含多个口径（{'、'.join(str(item) for item in scopes)}），"
            f"分析只采用『{chosen_scope}』，其余口径未参与计算。"
        )
    if len(versions) > 1 and version is None and not merged:
        warnings.append(
            f"文档同时包含多个披露版本（{'、'.join(versions)}），分析只采用『{chosen_version}』。"
        )
    return chosen_scope, chosen_version


def _series_in_group(
    series: Dict[Tuple[str, str, str], MetricSeries],
    metric: str,
    scope: Optional[str],
    version: Optional[str],
    *,
    merged: bool = False,
) -> Optional[MetricSeries]:
    """Find ``metric``'s series in the chosen group.

    With ``merged`` the version slot holds :data:`~.series.MERGED_VERSION` for
    every series, so the exact-key probe would miss; the scan below matches on
    metric + scope only, which is precisely what a merged view means.
    """
    if not merged:
        direct = series.get((metric, scope or "_", version or ""))
        if direct is not None:
            return direct
    for (key_metric, _scope, key_version), item in sorted(series.items()):
        if key_metric != metric:
            continue
        if scope is not None and item.scope != scope:
            continue
        if not merged and version is not None and key_version != version:
            continue
        return item
    return None


def _group_currency(
    series: Dict[Tuple[str, str, str], MetricSeries],
    scope: Optional[str],
    version: Optional[str],
    *,
    merged: bool = False,
) -> Optional[str]:
    for item in series.values():
        if item.scope == scope and (merged or item.version == version):
            return item.currency
    for item in series.values():
        return item.currency
    return None


def _select_basis(
    series: Dict[Tuple[str, str, str], MetricSeries], *, report_period: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """The document's own cumulative basis + latest period (FY when annual).

    A quarterly filing legitimately compares 9M against 9M; forcing FY would
    fabricate a basis the filing never disclosed.
    """
    if report_period:
        try:
            _year, suffix = split_period_key(report_period)
        except ValueError:
            suffix = ""
        if suffix in _BASIS_ORDER:
            return suffix, report_period

    candidates: List[Tuple[int, int, str, str]] = []
    for (metric, _scope, _version), item in series.items():
        if metric not in ("revenue", "net_profit_parent"):
            continue
        for (basis, period_key) in item.values:
            if basis not in _BASIS_ORDER:
                continue
            year, _ = split_period_key(period_key)
            candidates.append((year, _BASIS_ORDER.index(basis), basis, period_key))
    if not candidates:
        return None, None
    candidates.sort()
    year, _rank, basis, period_key = candidates[-1]
    return basis, period_key


def _operand(value: PeriodValue, label: str = "") -> Operand:
    """Disclosed value → evidence operand; engine product → calculation operand."""
    if value.normalized_fact_id:
        return evidence_operand(value)
    return Operand(
        kind=OPERAND_CALCULATION,
        operand_id=value.calculation_id or f"engine_{value.basis}_{value.period_key}",
        value=float(value.value),
        label=label or value.source_table,
    )


def _emit_yoy(
    result: AnalysisResult,
    registry: CalculationRegistry,
    *,
    metric_id: str,
    basis: str,
    period_key: str,
    lookup,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
    suppress_reason: Optional[str] = None,
) -> None:
    definition = METRIC_INDEX[metric_id]
    if definition.comparison is not ComparisonKind.YOY:
        # Level-only rows (CapEx / FCF): a percentage across a sign change or an
        # inconsistent disclosure sign is not interpretable, so it is not produced.
        level = _resolve_level(
            registry,
            metric_id=metric_id,
            definition=definition,
            basis=basis,
            period_key=period_key,
            lookup=lookup,
            scope=scope,
            version=version,
            currency=currency,
        )
        if level is None:
            result.missing.append(
                MissingMetric(metric_id, period_key, basis, f"缺少 {definition.inputs[0]}", definition.inputs)
            )
            return
        _append_level(
            result, registry, definition=definition, level=level, basis=basis,
            scope=scope, version=version, currency=currency,
        )
        return

    metric_key = definition.inputs[0]
    current = lookup(metric_key, basis, period_key)
    previous = lookup(metric_key, basis, previous_year_key(period_key))
    if current is None or previous is None:
        # Both ends of a YoY are the *same* metric key, so a naive comprehension
        # reports it twice ("缺少 net_profit_deducted、net_profit_deducted") when
        # the whole series is absent. ``required_inputs`` is a set of metric keys.
        missing_inputs = (metric_key,)
        if current is None and previous is None:
            reason = f"缺少本期与上一可比期间（{previous_year_key(period_key)}）的 {metric_key}"
        elif current is None:
            reason = f"缺少本期 {metric_key}"
        else:
            reason = (
                f"缺少上一可比期间（{previous_year_key(period_key)}）的 {metric_key}，"
                "序列断点处不做跨断点同比"
            )
        result.missing.append(MissingMetric(metric_id, period_key, basis, reason, missing_inputs))
        return

    metric_result = compute_yoy(
        registry,
        metric=metric_key,
        label=definition.label,
        current_fact=current,
        previous_fact=previous,
        current_period=period_key,
        previous_period=previous_year_key(period_key),
        period_basis=basis,
        scope=scope,
        version=version,
        currency=currency,
        suppress_reason=suppress_reason,
    )
    if metric_result is None:
        result.missing.append(
            MissingMetric(metric_id, period_key, basis, "基期为 0，同比不可计算（不输出百分比）", (metric_key,))
        )
        return
    result.metrics.append(_relabel(metric_result, definition=definition, basis=basis))


@dataclass(frozen=True)
class _Level:
    """A resolved level value plus the operand that proves where it came from."""

    value: float
    operand: Operand
    evidence_id: str
    period_key: str
    origin: str
    engine: Optional[str] = None
    note: Optional[str] = None
    record_id: Optional[str] = None      # set when the level *is* a derivation


#: Level rows that fall back to a declared derivation when the combined value is
#: not disclosed. The definitions mirror CARD-02's ``_derive_metrics`` rules, so
#: a bare fact list and a full CARD-02 run agree.
_LEVEL_FALLBACK: Dict[str, Tuple[str, str, str]] = {
    FREE_CASH_FLOW: ("cfo_minus_capex", "cfo", "capex"),
}


def _level_from_period(value: PeriodValue, *, period_key: Optional[str] = None) -> _Level:
    return _Level(
        value=float(value.value),
        operand=_operand(value),
        evidence_id=value.evidence_id,
        period_key=period_key or value.period_key,
        origin=value.origin,
        engine=ENGINE if value.origin == "derived" else None,
    )


def _resolve_level(
    registry: CalculationRegistry,
    *,
    metric_id: str,
    definition,
    basis: str,
    period_key: str,
    lookup,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
) -> Optional[_Level]:
    """Resolve a level row, deriving it when the disclosure is absent."""
    current = lookup(definition.inputs[0], basis, period_key)
    if current is not None:
        return _level_from_period(current)

    rule = _LEVEL_FALLBACK.get(metric_id)
    if rule is None:
        return None
    out_key, minuend_key, subtrahend_key = rule
    minuend = lookup(minuend_key, basis, period_key)
    subtrahend = lookup(subtrahend_key, basis, period_key)
    if minuend is None or subtrahend is None:
        return None
    record = registry.register(
        metric=out_key,
        formula=f"{minuend_key} - {subtrahend_key}",
        operation="subtract",
        operands=[_operand(minuend), _operand(subtrahend)],
        value=minuend.value - subtrahend.value,
        unit=Unit.YUAN.value,
        period_key=period_key,
        period_basis=basis,
        scope=scope,
        version=version,
        currency=currency,
        source="derived",
        engine="src.cn.analysis.pipeline",
        note=f"未披露 {out_key}，按 CARD-02 同一规则由 {minuend_key} − {subtrahend_key} 派生",
        key_hint=out_key,
    )
    return _Level(
        value=record.value,
        operand=calculation_operand(record, definition.label),
        evidence_id=record.calculation_id,
        period_key=period_key,
        origin="derived",
        engine="src.cn.analysis.pipeline",
        note=f"派生值（{minuend_key} − {subtrahend_key}），与 CARD-02 同一规则",
        record_id=record.calculation_id,
    )


def _append_level(
    result: AnalysisResult,
    registry: CalculationRegistry,
    *,
    definition,
    level: _Level,
    basis: str,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
) -> None:
    """A level-only row still gets a calculation record (formula = the disclosure).

    ``identity`` is deliberate: it records *that no arithmetic was applied*, so
    a reader can tell a disclosed value apart from a derived one by looking at
    the calculation, not by trusting a flag.
    """
    if level.record_id:
        # The value already has its own derivation record; wrapping it in an
        # extra ``identity`` would duplicate the same number with a second ID.
        calculation_id = level.record_id
    else:
        calculation_id = registry.register(
            metric=definition.inputs[0] or definition.metric,
            formula=f"{definition.inputs[0]}（原始披露值，未做算术）",
            operation="identity",
            operands=[level.operand],
            value=float(level.value),
            unit=definition.unit,
            period_key=level.period_key,
            period_basis=basis,
            scope=scope,
            version=version,
            currency=currency,
            source=level.origin,
            engine=level.engine,
            note="水平值行：不做同比／环比（跨零或披露符号不一致时百分比不可解释）",
            key_hint="level",
        ).calculation_id
    note = definition.note
    if level.note:
        note = f"{note}；{level.note}" if note else level.note
    result.metrics.append(
        MetricResult(
            metric=definition.metric,
            label=definition.label,
            value=float(level.value),
            unit=definition.unit,
            period_key=level.period_key,
            period_basis=basis,
            comparison=ComparisonKind.LEVEL,
            calculation_id=calculation_id,
            scope=scope,
            version=version,
            currency=currency,
            inputs=(level.evidence_id,),
            note=note,
        )
    )


def _relabel(metric_result: MetricResult, *, definition, basis: str) -> MetricResult:
    from dataclasses import replace

    return replace(metric_result, metric=definition.metric, label=definition.label, period_basis=basis)


def _emit_gross_margin(
    result: AnalysisResult,
    registry: CalculationRegistry,
    *,
    definition,
    basis: str,
    period_key: str,
    lookup,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
) -> None:
    """毛利率: disclosed ``gross_profit`` first, else revenue − operating_cost.

    The fallback mirrors CARD-02's ``_derive_metrics`` rule for ``gross_profit``,
    so the value matches what CARD-02 would have derived — the formula recorded
    in the calculation says which path was taken.
    """
    numerator = lookup("gross_profit", basis, period_key)
    denominator = lookup("revenue", basis, period_key)
    if denominator is None or denominator.value == 0:
        result.missing.append(
            MissingMetric(
                definition.metric, period_key, basis,
                "缺少营业收入（或收入为 0），毛利率不可计算", ("revenue",),
            )
        )
        return

    if numerator is not None:
        numerator_operand = _operand(numerator)
        formula = "gross_profit / revenue * 100"
        note = definition.note
    else:
        revenue = denominator
        cost = lookup("operating_cost", basis, period_key)
        if cost is None:
            result.missing.append(
                MissingMetric(
                    definition.metric, period_key, basis,
                    "缺少毛利（也未同时披露营业收入与营业成本，无法派生）",
                    ("gross_profit", "revenue", "operating_cost"),
                )
            )
            return
        record = registry.register(
            metric="gross_profit",
            formula="revenue - operating_cost",
            operation="subtract",
            operands=[_operand(revenue), _operand(cost)],
            value=revenue.value - cost.value,
            unit=Unit.YUAN.value,
            period_key=period_key,
            period_basis=basis,
            scope=scope,
            version=version,
            currency=currency,
            source="derived",
            engine="src.cn.analysis.pipeline",
            note="未披露毛利，按 CARD-02 同一规则由收入 − 营业成本派生",
            key_hint="intermediate",
        )
        numerator_operand = calculation_operand(record, "毛利（派生）")
        formula = "(revenue - operating_cost) / revenue * 100"
        note = f"{definition.note}；未披露毛利，已按 CARD-02 同一规则派生"

    metric_result = compute_ratio(
        registry,
        metric=definition.metric,
        label=definition.label,
        numerator_operand=numerator_operand,
        denominator_operand=_operand(denominator),
        value=numerator_operand.value / denominator.value * 100.0,
        unit=definition.unit,
        period_key=period_key,
        period_basis=basis,
        formula=formula,
        scope=scope,
        version=version,
        currency=currency,
        note=note,
    )
    if metric_result is not None:
        result.metrics.append(metric_result)


def _emit_ratio(
    result: AnalysisResult,
    registry: CalculationRegistry,
    *,
    metric_id: str,
    basis: str,
    period_key: str,
    lookup,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
    disclosed_ratios: Dict[str, float],
) -> None:
    definition = METRIC_INDEX[metric_id]
    note = definition.note

    if metric_id is ROE:
        _emit_roe(
            result, registry, definition=definition, basis=basis, period_key=period_key,
            lookup=lookup, scope=scope, version=version, currency=currency,
            disclosed_ratios=disclosed_ratios,
        )
        return

    if metric_id is GROSS_MARGIN:
        _emit_gross_margin(
            result, registry, definition=definition, basis=basis, period_key=period_key,
            lookup=lookup, scope=scope, version=version, currency=currency,
        )
        return

    if metric_id in _MIXED_PERIOD_RATIOS:
        numerator_key = "receivables" if metric_id == RECEIVABLES_TO_REVENUE else "inventory"
        numerator = lookup(numerator_key, BASIS_POINT_IN_TIME, period_key)
        denominator_key = "revenue"
    elif metric_id is NON_RECURRING_RATIO:
        numerator = None
        denominator_key = "net_profit_parent"
    else:
        numerator_key = _RATIO_NUMERATOR.get(metric_id)
        numerator = lookup(numerator_key, basis, period_key) if numerator_key else None
        denominator_key = _RATIO_DENOMINATOR.get(metric_id) or "revenue"

    denominator = lookup(denominator_key, basis, period_key)

    if metric_id is NON_RECURRING_RATIO:
        parent = lookup("net_profit_parent", basis, period_key)
        deducted = lookup("net_profit_deducted", basis, period_key)
        if parent is None or deducted is None:
            result.missing.append(
                MissingMetric(
                    metric_id, period_key, basis,
                    "缺少扣非归母净利润（或归母净利润），不得用净利润替代",
                    ("net_profit_parent", "net_profit_deducted"),
                )
            )
            return
        if parent.value == 0:
            result.missing.append(
                MissingMetric(metric_id, period_key, basis, "归母净利润为 0，比率不可计算", ("net_profit_parent",))
            )
            return
        numerator_record = registry.register(
            metric="non_recurring_profit",
            formula="net_profit_parent - net_profit_deducted",
            operation="subtract",
            operands=[_operand(parent), _operand(deducted)],
            value=parent.value - deducted.value,
            unit=Unit.YUAN.value,
            period_key=period_key,
            period_basis=basis,
            scope=scope,
            version=version,
            currency=currency,
            source="derived",
            engine="src.cn.analysis.pipeline",
            note="非经常性损益（近似：归母 − 扣非）",
            key_hint="intermediate",
        )
        metric_result = compute_ratio(
            registry,
            metric=definition.metric,
            label=definition.label,
            numerator_operand=calculation_operand(numerator_record, "非经常性损益"),
            denominator_operand=_operand(parent),
            value=(parent.value - deducted.value) / parent.value * 100.0,
            unit=definition.unit,
            period_key=period_key,
            period_basis=basis,
            formula=RATIO_FORMULAS[NON_RECURRING_RATIO],
            scope=scope,
            version=version,
            currency=currency,
            note=note,
        )
        if metric_result is not None:
            result.metrics.append(metric_result)
        return

    if numerator is None or denominator is None:
        missing = [
            name for name, found in (
                (numerator_key if metric_id not in _MIXED_PERIOD_RATIOS else ("receivables" if metric_id == RECEIVABLES_TO_REVENUE else "inventory"), numerator),
                (denominator_key, denominator),
            ) if found is None
        ]
        result.missing.append(
            MissingMetric(metric_id, period_key, basis, f"缺少 {('、'.join(missing))}", tuple(missing))
        )
        return
    if denominator.value == 0:
        result.missing.append(
            MissingMetric(metric_id, period_key, basis, f"{denominator_key} 为 0，比率不可计算", (denominator_key,))
        )
        return

    effective_note = note
    if definition.mixed_period:
        effective_note = (
            f"{note}；分子为 {period_key} 期末存量、分母为 {basis} {period_key} 期间流量"
        )
    metric_result = compute_ratio(
        registry,
        metric=definition.metric,
        label=definition.label,
        numerator_operand=_operand(numerator),
        denominator_operand=_operand(denominator),
        value=numerator.value / denominator.value * 100.0,
        unit=definition.unit,
        period_key=period_key,
        period_basis=basis,
        formula=RATIO_FORMULAS[metric_id],
        scope=scope,
        version=version,
        currency=currency,
        note=effective_note,
    )
    if metric_result is not None:
        result.metrics.append(metric_result)


def _emit_roe(
    result: AnalysisResult,
    registry: CalculationRegistry,
    *,
    definition,
    basis: str,
    period_key: str,
    lookup,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
    disclosed_ratios: Dict[str, float],
) -> None:
    """ROE with the card's three-tier priority (§7 table + §8.4 fallback)."""
    disclosed = disclosed_ratios.get(_ROE_DISCLOSED_KEY) or disclosed_ratios.get(ROE)
    if disclosed is not None:
        record = registry.register(
            metric="roe",
            formula="披露的加权平均净资产收益率（原始披露值，未做算术）",
            operation="add",
            operands=[
                Operand(kind="constant", operand_id="disclosed", value=float(disclosed), label="披露值"),
                Operand(kind="constant", operand_id="0", value=0.0, label="加性单位元"),
            ],
            value=float(disclosed),
            unit=Unit.PERCENT.value,
            period_key=period_key,
            period_basis=basis,
            scope=scope,
            version=version,
            currency=currency,
            source="disclosed",
            note="披露的加权平均 ROE 优先于任何近似计算",
            key_hint="disclosed",
        )
        result.metrics.append(
            MetricResult(
                metric=ROE,
                label=definition.label,
                value=float(disclosed),
                unit=Unit.PERCENT.value,
                period_key=period_key,
                period_basis=basis,
                comparison=ComparisonKind.LEVEL,
                calculation_id=record.calculation_id,
                scope=scope,
                version=version,
                currency=currency,
                note="口径：披露的加权平均 ROE",
            )
        )
        return

    profit = lookup("net_profit_parent", basis, period_key)
    equity_end = lookup("equity_parent", BASIS_POINT_IN_TIME, period_key)
    equity_begin = lookup("equity_parent", BASIS_POINT_IN_TIME, previous_year_key(period_key))
    if profit is None or equity_end is None:
        missing = [name for name, found in (("net_profit_parent", profit), ("equity_parent", equity_end)) if found is None]
        result.missing.append(
            MissingMetric(ROE, period_key, basis, f"缺少 {('、'.join(missing))}", tuple(missing))
        )
        return

    if equity_begin is not None and (equity_begin.value + equity_end.value) != 0:
        average = average_operand(
            registry,
            first=equity_begin,
            second=equity_end,
            metric="equity_parent",
            period_key=period_key,
            period_basis=basis,
            formula="(equity_parent_begin + equity_parent_end) / 2",
            scope=scope,
            version=version,
            currency=currency,
        )
        if average.value == 0:
            result.missing.append(
                MissingMetric(ROE, period_key, basis, "平均净资产为 0，ROE 不可计算", ("equity_parent",))
            )
            return
        metric_result = compute_ratio(
            registry,
            metric=ROE,
            label=definition.label,
            numerator_operand=_operand(profit),
            denominator_operand=calculation_operand(average, "平均净资产"),
            value=profit.value / average.value * 100.0,
            unit=Unit.PERCENT.value,
            period_key=period_key,
            period_basis=basis,
            formula=RATIO_FORMULAS[ROE],
            scope=scope,
            version=version,
            currency=currency,
            note="口径：期初与期末净资产的算术平均（两点近似），非披露的加权平均 ROE",
        )
        if metric_result is not None:
            result.metrics.append(metric_result)
        return

    if equity_end.value == 0:
        result.missing.append(
            MissingMetric(ROE, period_key, basis, "期末净资产为 0，ROE 不可计算", ("equity_parent",))
        )
        return
    metric_result = compute_ratio(
        registry,
        metric=ROE,
        label=definition.label,
        numerator_operand=_operand(profit),
        denominator_operand=_operand(equity_end),
        value=profit.value / equity_end.value * 100.0,
        unit=Unit.PERCENT.value,
        period_key=period_key,
        period_basis=basis,
        formula="net_profit_parent / equity_parent_period_end * 100",
        scope=scope,
        version=version,
        currency=currency,
        note="口径：期初净资产不可得，改用期末净资产近似（§8.4 降级），与加权平均 ROE 不可比",
    )
    if metric_result is not None:
        result.metrics.append(metric_result)
        result.warnings.append(
            f"ROE（{period_key}）缺少期初净资产，已降级为期初不可得的期末近似，口径已在指标 note 标注。"
        )


def _emit_single_quarter_qoq(
    result: AnalysisResult,
    registry: CalculationRegistry,
    *,
    series_for,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
    quarterly_disclosed: bool,
) -> None:
    revenue = series_for("revenue")
    if revenue is None:
        result.missing.append(
            MissingMetric(
                REVENUE_QOQ_SINGLE_QUARTER, None, BASIS_SINGLE_QUARTER,
                "缺少营业收入序列，单季环比不可计算", ("revenue",),
            )
        )
        return
    quarters = revenue.periods(BASIS_SINGLE_QUARTER)
    if not quarters:
        result.missing.append(
            MissingMetric(
                REVENUE_QOQ_SINGLE_QUARTER, None, BASIS_SINGLE_QUARTER,
                (
                    "文档未披露季度累计期间（Q1/H1/9M），单季环比不适用于本份文档"
                    if not quarterly_disclosed
                    else "文档披露的累计期间不足以差分出单季值（引擎未产出单季序列）"
                ),
                ("revenue",),
                blocking=quarterly_disclosed,
            )
        )
        return
    produced = 0
    for period_key in quarters:
        previous_key = previous_quarter_key(period_key)
        current = revenue.get(BASIS_SINGLE_QUARTER, period_key)
        previous = revenue.get(BASIS_SINGLE_QUARTER, previous_key) if previous_key else None
        if current is None or previous is None:
            continue
        metric_result = compute_single_quarter_qoq(
            registry,
            metric="revenue",
            label=METRIC_INDEX[REVENUE_QOQ_SINGLE_QUARTER].label,
            current_quarter=current,
            previous_quarter=previous,
            scope=scope,
            version=version,
            currency=currency,
            left_label=f"单季 {period_key}",
            right_label=f"单季 {previous_key}",
        )
        if metric_result is None:
            continue
        from dataclasses import replace

        result.metrics.append(replace(metric_result, metric=REVENUE_QOQ_SINGLE_QUARTER, period_key=period_key))
        produced += 1
    if produced == 0:
        result.missing.append(
            MissingMetric(
                REVENUE_QOQ_SINGLE_QUARTER, quarters[-1], BASIS_SINGLE_QUARTER,
                "单季序列不足两期，无法计算环比", ("revenue",),
            )
        )


def _emit_stock_levels(
    result: AnalysisResult,
    registry: CalculationRegistry,
    *,
    series_for,
    scope: Optional[str],
    version: Optional[str],
    currency: Optional[str],
) -> None:
    definition = METRIC_INDEX[CONTRACT_LIABILITIES]
    found = series_for("contract_liabilities")
    if found is None or not found.has_any():
        result.missing.append(
            MissingMetric(
                CONTRACT_LIABILITIES, None, BASIS_POINT_IN_TIME,
                "文档未披露合同负债", ("contract_liabilities",),
            )
        )
        return
    produced = 0
    for basis, period_key in sorted(found.values):
        if basis != BASIS_POINT_IN_TIME:
            continue
        value = found.get(basis, period_key)
        if value is None:
            continue
        _append_level(
            result, registry, definition=definition, level=_level_from_period(value),
            basis=BASIS_POINT_IN_TIME, scope=scope, version=version, currency=currency,
        )
        produced += 1
    if produced == 0:
        result.missing.append(
            MissingMetric(
                CONTRACT_LIABILITIES, None, BASIS_POINT_IN_TIME,
                "合同负债缺少可用的期末期间键（存量值必须有报告期）", ("contract_liabilities",),
            )
        )


__all__ = ["analyze_document", "analyze_financials"]
