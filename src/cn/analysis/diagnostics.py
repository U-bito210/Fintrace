"""Deterministic diagnostic signals (CARD-03 §7) — signals, never verdicts.

Four rules, each a pure function of period-aligned series:

===============  ==================================================
rule             condition
===============  ==================================================
earnings_quality 利润上涨 + 经营现金流下降
receivable_risk  收入上涨 + 应收增速显著高于收入增速
inventory_press  收入上涨 + 存货增速显著高于收入增速
non_recurring    归母净利润上涨 + 扣非净利润下降
===============  ==================================================

All thresholds live here as named constants with a stated basis, and every rule
fails **closed**: if either side of a comparison is missing, the signal is not
produced (宁缺勿假). Nothing here writes causality — ``detail`` only states the
observed movements, because the numbers cannot establish *why*.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .models import DiagnosticSignal, MetricResult
from .series import MetricSeries, PeriodValue, previous_year_key

SIGNAL_EARNINGS_QUALITY = "earnings_quality_warning"
SIGNAL_RECEIVABLE_RISK = "receivable_risk"
SIGNAL_INVENTORY_PRESSURE = "inventory_pressure"
SIGNAL_NON_RECURRING = "non_recurring_contribution"

SIGNAL_LABELS: Dict[str, str] = {
    SIGNAL_EARNINGS_QUALITY: "盈利质量提示（利润与经营现金流背离）",
    SIGNAL_RECEIVABLE_RISK: "应收账款提示（应收增速快于收入）",
    SIGNAL_INVENTORY_PRESSURE: "存货提示（存货增速快于收入）",
    SIGNAL_NON_RECURRING: "非经常性损益提示（利润增长依赖非经常性损益）",
}

#: 应收/存货增速需要**超出**收入增速多少个百分点才算「显著高于」。
#: 依据：A 股应收账款周转天数通常在 60–120 天区间，收入与应收同向增长时，
#: 两者增速差在 10 个百分点以内多数属季节性/账期波动；取 10pp 作为
#: 「不显著」的上界，避免把常规波动报成风险。数值集中于此，便于复核与调参。
GROWTH_GAP_THRESHOLD_PCT = 10.0

#: 诊断只在同比可比期间上进行，且两期必须恰好相隔一年。
REQUIRED_COMPARISON_YEARS = 1


def _growth_pct(current: float, previous: float) -> Optional[float]:
    if previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


def _yoy_pair(series: MetricSeries, basis: str, period_key: str) -> Optional[Tuple[PeriodValue, PeriodValue]]:
    current = series.get(basis, period_key)
    if current is None:
        return None
    previous = series.get(basis, previous_year_key(period_key))
    if previous is None:
        return None
    return current, previous


def _calc_id(metrics: Dict[str, MetricResult], metric: str, period_key: str) -> Tuple[str, ...]:
    """Calculation IDs backing a signal, so a report can cite the exact numbers."""
    found = [item for item in metrics.values() if item.metric == metric and item.period_key == period_key]
    return tuple(sorted(item.calculation_id for item in found if item.calculation_id))


def run_diagnostics(
    *,
    series: Dict[Tuple[str, str, str], MetricSeries],
    metrics: Sequence[MetricResult],
    scope: Optional[str] = None,
    version: Optional[str] = None,
) -> List[DiagnosticSignal]:
    """Apply the four rules over the primary group's series."""
    metrics_index = {
        (item.metric, item.period_key, item.period_basis): item for item in metrics
    }
    signals: List[DiagnosticSignal] = []

    def matching(metric: str) -> Optional[MetricSeries]:
        for (key_metric, _scope, key_version), item in series.items():
            if key_metric != metric:
                continue
            if scope is not None and item.scope != scope:
                continue
            if version is not None and item.version != version:
                continue
            return item
        return None

    signals.extend(
        _earnings_quality(matching("net_profit_parent"), matching("cfo"), metrics_index)
    )
    revenue_series = matching("revenue")
    receivables_series = matching("receivables")
    inventory_series = matching("inventory")
    if revenue_series is not None and receivables_series is not None:
        signals.extend(
            _growth_gap(
                revenue_series=revenue_series,
                driver_series=receivables_series,
                signal_id=SIGNAL_RECEIVABLE_RISK,
                driver_label="应收账款",
                metrics_index=metrics_index,
            )
        )
    if revenue_series is not None and inventory_series is not None:
        signals.extend(
            _growth_gap(
                revenue_series=revenue_series,
                driver_series=inventory_series,
                signal_id=SIGNAL_INVENTORY_PRESSURE,
                driver_label="存货",
                metrics_index=metrics_index,
            )
        )
    signals.extend(_non_recurring(matching("net_profit_parent"), matching("net_profit_deducted"), metrics_index))
    return sorted(signals, key=lambda item: (item.signal_id, item.periods))


def _earnings_quality(
    profit_series: Optional[MetricSeries],
    cfo_series: Optional[MetricSeries],
    metrics_index: Dict[Tuple[str, str, str], MetricResult],
) -> List[DiagnosticSignal]:
    if profit_series is None or cfo_series is None:
        return []
    out: List[DiagnosticSignal] = []
    for basis in ("FY", "H1", "9M", "Q1"):
        for period_key in profit_series.periods(basis):
            profit_pair = _yoy_pair(profit_series, basis, period_key)
            cfo_pair = _yoy_pair(cfo_series, basis, period_key)
            if profit_pair is None or cfo_pair is None:
                continue
            profit_growth = _growth_pct(profit_pair[0].value, profit_pair[1].value)
            cfo_growth = _growth_pct(cfo_pair[0].value, cfo_pair[1].value)
            if profit_growth is None or cfo_growth is None:
                continue
            if profit_growth > 0 and cfo_growth < 0:
                out.append(
                    DiagnosticSignal(
                        signal_id=SIGNAL_EARNINGS_QUALITY,
                        label=SIGNAL_LABELS[SIGNAL_EARNINGS_QUALITY],
                        periods=(previous_year_key(period_key), period_key),
                        detail=(
                            f"{previous_year_key(period_key)} → {period_key}："
                            f"归母净利润 {_money(profit_pair[0].value)} 元"
                            f"（{_signed(profit_growth)}%），"
                            f"经营活动现金流净额 {_money(cfo_pair[0].value)} 元"
                            f"（{_signed(cfo_growth)}%）；"
                            "利润与经营现金流方向相反，盈利质量需进一步核查。"
                        ),
                        metric_calculation_ids=(
                            _calc_id(metrics_index, "net_profit_parent", period_key)
                            + _calc_id(metrics_index, "cfo_to_net_profit", period_key)
                        ),
                        threshold=None,
                        observed=float(profit_growth - cfo_growth),
                    )
                )
    return out


def _growth_gap(
    *,
    revenue_series: MetricSeries,
    driver_series: MetricSeries,
    signal_id: str,
    driver_label: str,
    metrics_index: Dict[Tuple[str, str, str], MetricResult],
) -> List[DiagnosticSignal]:
    out: List[DiagnosticSignal] = []
    ratio_metric = (
        "receivables_to_revenue" if signal_id == SIGNAL_RECEIVABLE_RISK else "inventory_to_revenue"
    )
    for basis in ("FY", "H1", "9M"):
        for period_key in revenue_series.periods(basis):
            revenue_pair = _yoy_pair(revenue_series, basis, period_key)
            driver_pair = _yoy_pair(driver_series, "POINT_IN_TIME", period_key)
            if revenue_pair is None or driver_pair is None:
                continue
            revenue_growth = _growth_pct(revenue_pair[0].value, revenue_pair[1].value)
            driver_growth = _growth_pct(driver_pair[0].value, driver_pair[1].value)
            if revenue_growth is None or driver_growth is None:
                continue
            if revenue_growth <= 0:
                continue
            gap = driver_growth - revenue_growth
            if gap < GROWTH_GAP_THRESHOLD_PCT:
                continue
            out.append(
                DiagnosticSignal(
                    signal_id=signal_id,
                    label=SIGNAL_LABELS[signal_id],
                    periods=(previous_year_key(period_key), period_key),
                    detail=(
                        f"{previous_year_key(period_key)} → {period_key}："
                        f"营业收入同比 {_signed(revenue_growth)}%，"
                        f"{driver_label}（期末）同比 {_signed(driver_growth)}%，"
                        f"增速差 {gap:.2f} 个百分点（阈值 {GROWTH_GAP_THRESHOLD_PCT:.0f}pp）；"
                        f"{driver_label}增速快于收入增速，占用或周转情况需进一步核查。"
                        "（注：分子为期末存量、分母为期间流量，属混合口径）"
                    ),
                    metric_calculation_ids=_calc_id(metrics_index, ratio_metric, period_key),
                    threshold=GROWTH_GAP_THRESHOLD_PCT,
                    observed=float(gap),
                )
            )
    return out


def _non_recurring(
    profit_series: Optional[MetricSeries],
    deducted_series: Optional[MetricSeries],
    metrics_index: Dict[Tuple[str, str, str], MetricResult],
) -> List[DiagnosticSignal]:
    if profit_series is None or deducted_series is None:
        return []
    out: List[DiagnosticSignal] = []
    for basis in ("FY", "H1", "9M", "Q1"):
        for period_key in profit_series.periods(basis):
            profit_pair = _yoy_pair(profit_series, basis, period_key)
            deducted_pair = _yoy_pair(deducted_series, basis, period_key)
            if profit_pair is None or deducted_pair is None:
                continue
            profit_growth = _growth_pct(profit_pair[0].value, profit_pair[1].value)
            deducted_growth = _growth_pct(deducted_pair[0].value, deducted_pair[1].value)
            if profit_growth is None or deducted_growth is None:
                continue
            if profit_growth > 0 and deducted_growth < 0:
                out.append(
                    DiagnosticSignal(
                        signal_id=SIGNAL_NON_RECURRING,
                        label=SIGNAL_LABELS[SIGNAL_NON_RECURRING],
                        periods=(previous_year_key(period_key), period_key),
                        detail=(
                            f"{previous_year_key(period_key)} → {period_key}："
                            f"归母净利润同比 {_signed(profit_growth)}%，"
                            f"扣非归母净利润同比 {_signed(deducted_growth)}%；"
                            "利润增长与扣非口径方向相反，非经常性损益贡献需进一步核查。"
                        ),
                        metric_calculation_ids=(
                            _calc_id(metrics_index, "net_profit_parent", period_key)
                            + _calc_id(metrics_index, "non_recurring_ratio", period_key)
                        ),
                        threshold=None,
                        observed=float(profit_growth - deducted_growth),
                    )
                )
    return out


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _signed(value: float) -> str:
    return f"{value:+.2f}"


__all__ = [
    "GROWTH_GAP_THRESHOLD_PCT",
    "REQUIRED_COMPARISON_YEARS",
    "SIGNAL_EARNINGS_QUALITY",
    "SIGNAL_INVENTORY_PRESSURE",
    "SIGNAL_LABELS",
    "SIGNAL_NON_RECURRING",
    "SIGNAL_RECEIVABLE_RISK",
    "run_diagnostics",
]
