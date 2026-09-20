"""The 14 analysis metrics and their **period alignment table** (CARD-03 §7).

The period alignment table is the part the v3.0 plan was missing (role M3): a
metric is only meaningful on a declared period basis, and two metrics on
different bases may not be differenced or compared. Each definition therefore
declares:

* ``period_basis`` — the basis the metric is computed on (``FY`` / ``TTM`` /
  ``SINGLE_QUARTER`` / ``POINT_IN_TIME``);
* ``comparison`` — ``yoy`` / ``qoq`` / ``level`` (a stock value has no YoY);
* ``inputs`` — the canonical CARD-02 metric keys it consumes;
* ``mixed_period`` — True when the ratio legitimately mixes a flow and a stock
  (e.g. receivables / revenue), which must be *labelled* in the output.

Canonical keys come from CARD-02's ``METRIC_INDEX``; note that CARD-02 projects
``net_profit_parent`` onto the statement value key ``net_profit`` (the repo's
historical key for 归母净利润) — see ``statement_value_key()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .models import ComparisonKind, Unit

# -- metric IDs (the 14 rows of §7) ------------------------------------------

REVENUE_YOY = "revenue_yoy"
REVENUE_QOQ_SINGLE_QUARTER = "revenue_qoq_single_quarter"
NET_PROFIT_YOY = "net_profit_yoy"
DEDUCTED_NET_PROFIT_YOY = "deducted_net_profit_yoy"
GROSS_MARGIN = "gross_margin"
NET_MARGIN = "net_margin"
ROE = "roe"
CFO_TO_NET_PROFIT = "cfo_to_net_profit"
RECEIVABLES_TO_REVENUE = "receivables_to_revenue"
INVENTORY_TO_REVENUE = "inventory_to_revenue"
CONTRACT_LIABILITIES = "contract_liabilities"
CAPEX = "capex"
FREE_CASH_FLOW = "free_cash_flow"
NON_RECURRING_RATIO = "non_recurring_ratio"

BASIS_FY = "FY"
BASIS_TTM = "TTM"
BASIS_SINGLE_QUARTER = "SINGLE_QUARTER"
BASIS_POINT_IN_TIME = "POINT_IN_TIME"


@dataclass(frozen=True)
class MetricDefinition:
    """One row of the §7 period alignment table."""

    metric: str
    label: str
    period_basis: str
    statement: str                     # income | balance | cashflow | derived
    comparison: Optional[ComparisonKind]
    inputs: Tuple[str, ...] = ()       # canonical CARD-02 metric keys
    unit: str = Unit.YUAN.value
    mixed_period: bool = False
    derived: bool = False
    note: Optional[str] = None

    @property
    def flow(self) -> bool:
        return self.period_basis in (BASIS_FY, BASIS_TTM, BASIS_SINGLE_QUARTER)


#: §7 「14 项指标」+ 期间对齐表.
ANALYSIS_METRICS: Tuple[MetricDefinition, ...] = (
    MetricDefinition(
        REVENUE_YOY, "营业收入同比", BASIS_FY, "income", ComparisonKind.YOY,
        inputs=("revenue",), unit=Unit.PERCENT.value,
        note="年报口径为 FY，另在 TTM 基上并行输出",
    ),
    MetricDefinition(
        REVENUE_QOQ_SINGLE_QUARTER, "营业收入环比（单季）", BASIS_SINGLE_QUARTER, "income",
        ComparisonKind.QOQ, inputs=("revenue",), unit=Unit.PERCENT.value,
        note="输入必须是引擎单季化产品，禁止用累计值直接环比",
    ),
    MetricDefinition(
        NET_PROFIT_YOY, "归母净利润同比", BASIS_FY, "income", ComparisonKind.YOY,
        inputs=("net_profit_parent",), unit=Unit.PERCENT.value,
        note="归母净利润，非合并净利润（含少数股东）",
    ),
    MetricDefinition(
        DEDUCTED_NET_PROFIT_YOY, "扣非归母净利润同比", BASIS_FY, "income", ComparisonKind.YOY,
        inputs=("net_profit_deducted",), unit=Unit.PERCENT.value,
        note="缺失时跳过该指标，不得用净利润替代",
    ),
    MetricDefinition(
        GROSS_MARGIN, "毛利率", BASIS_FY, "derived", None,
        inputs=("revenue", "operating_cost"), unit=Unit.PERCENT.value, derived=True,
        note="分母必须是与分子同期间的 revenue",
    ),
    MetricDefinition(
        NET_MARGIN, "净利率", BASIS_FY, "derived", None,
        inputs=("net_profit_parent", "revenue"), unit=Unit.PERCENT.value, derived=True,
    ),
    MetricDefinition(
        ROE, "净资产收益率", BASIS_FY, "derived", None,
        inputs=("net_profit_parent", "equity_parent"), unit=Unit.PERCENT.value, derived=True,
        note="披露的加权平均 ROE 优先；缺失则用期初期末平均净资产近似并标注口径",
    ),
    MetricDefinition(
        CFO_TO_NET_PROFIT, "经营现金流／归母净利润", BASIS_FY, "derived", None,
        inputs=("cfo", "net_profit_parent"), unit=Unit.PERCENT.value, derived=True,
        note="盈利质量诊断的分母；净利润为 0 时不计算",
    ),
    MetricDefinition(
        RECEIVABLES_TO_REVENUE, "应收账款／营业收入", BASIS_FY, "derived", None,
        inputs=("receivables", "revenue"), unit=Unit.PERCENT.value, derived=True,
        mixed_period=True,
        note="分子为期末存量、分母为期间流量，属混合口径，输出必须标注",
    ),
    MetricDefinition(
        INVENTORY_TO_REVENUE, "存货／营业收入", BASIS_FY, "derived", None,
        inputs=("inventory", "revenue"), unit=Unit.PERCENT.value, derived=True,
        mixed_period=True,
        note="分子为期末存量、分母为期间流量，属混合口径，输出必须标注",
    ),
    MetricDefinition(
        CONTRACT_LIABILITIES, "合同负债", BASIS_POINT_IN_TIME, "balance", ComparisonKind.LEVEL,
        inputs=("contract_liabilities",),
        note="存量值：不做同比／环比，仅输出水平值与报告期",
    ),
    MetricDefinition(
        CAPEX, "资本开支", BASIS_FY, "cashflow", ComparisonKind.LEVEL, inputs=("capex",),
        note=(
            "只输出水平值：资本开支的披露符号（正流出/负流出）在不同报表间不一致，"
            "跨期百分比在符号不一致时方向会误读，故不产生同比"
        ),
    ),
    MetricDefinition(
        FREE_CASH_FLOW, "自由现金流（简化）", BASIS_FY, "derived", ComparisonKind.LEVEL,
        inputs=("cfo_minus_capex",),
        note=(
            "cfo − capex（披露/派生值优先）；只输出水平值，跨零时百分比不可解释"
        ),
    ),
    MetricDefinition(
        NON_RECURRING_RATIO, "非经常性损益占归母净利润比重", BASIS_FY, "derived", None,
        inputs=("net_profit_parent", "net_profit_deducted"), unit=Unit.PERCENT.value, derived=True,
        note="(归母 − 扣非) / 归母；归母为 0 时不计算",
    ),
)

METRIC_INDEX: Dict[str, MetricDefinition] = {item.metric: item for item in ANALYSIS_METRICS}

#: Metrics that require their own YoY row on the FY basis, and also on TTM.
FY_AND_TTM_YOY_METRICS: Tuple[str, ...] = (
    REVENUE_YOY,
    NET_PROFIT_YOY,
    DEDUCTED_NET_PROFIT_YOY,
    CAPEX,
    FREE_CASH_FLOW,
)

#: Ratio metrics produced on both FY and TTM bases.
DUAL_BASIS_RATIO_METRICS: Tuple[str, ...] = (
    GROSS_MARGIN,
    NET_MARGIN,
    ROE,
    CFO_TO_NET_PROFIT,
    RECEIVABLES_TO_REVENUE,
    INVENTORY_TO_REVENUE,
    NON_RECURRING_RATIO,
)

#: Canonical CARD-02 key backing each ratio metric's numerator / denominator.
RATIO_INPUTS: Dict[str, Tuple[str, str]] = {
    GROSS_MARGIN: ("gross_profit", "revenue"),
    NET_MARGIN: ("net_profit_parent", "revenue"),
    CFO_TO_NET_PROFIT: ("cfo", "net_profit_parent"),
    RECEIVABLES_TO_REVENUE: ("receivables", "revenue"),
    INVENTORY_TO_REVENUE: ("inventory", "revenue"),
    NON_RECURRING_RATIO: ("non_recurring_profit", "net_profit_parent"),
}

RATIO_FORMULAS: Dict[str, str] = {
    GROSS_MARGIN: "(revenue - operating_cost) / revenue * 100",
    NET_MARGIN: "net_profit_parent / revenue * 100",
    ROE: "net_profit_parent / ((equity_parent_begin + equity_parent_end) / 2) * 100",
    CFO_TO_NET_PROFIT: "cfo / net_profit_parent * 100",
    RECEIVABLES_TO_REVENUE: "receivables_period_end / revenue * 100",
    INVENTORY_TO_REVENUE: "inventory_period_end / revenue * 100",
    NON_RECURRING_RATIO: "(net_profit_parent - net_profit_deducted) / net_profit_parent * 100",
}


def metric_definition(metric: str) -> Optional[MetricDefinition]:
    return METRIC_INDEX.get(metric)


def metrics_for_basis(basis: str) -> Tuple[MetricDefinition, ...]:
    return tuple(item for item in ANALYSIS_METRICS if item.period_basis == basis)


__all__ = [
    "ANALYSIS_METRICS",
    "BASIS_FY",
    "BASIS_POINT_IN_TIME",
    "BASIS_SINGLE_QUARTER",
    "BASIS_TTM",
    "CAPEX",
    "CFO_TO_NET_PROFIT",
    "CONTRACT_LIABILITIES",
    "DEDUCTED_NET_PROFIT_YOY",
    "DUAL_BASIS_RATIO_METRICS",
    "FREE_CASH_FLOW",
    "FY_AND_TTM_YOY_METRICS",
    "GROSS_MARGIN",
    "INVENTORY_TO_REVENUE",
    "METRIC_INDEX",
    "MetricDefinition",
    "NET_MARGIN",
    "NET_PROFIT_YOY",
    "NON_RECURRING_RATIO",
    "RATIO_FORMULAS",
    "RATIO_INPUTS",
    "RECEIVABLES_TO_REVENUE",
    "REVENUE_QOQ_SINGLE_QUARTER",
    "REVENUE_YOY",
    "ROE",
    "metric_definition",
    "metrics_for_basis",
]
