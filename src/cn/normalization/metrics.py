"""§7.1 standard metric dictionary: one disclosed line -> one unambiguous concept.

The dictionary exists because A-share disclosures reuse short labels with very
different meanings. Two collisions matter most and are handled explicitly here:

1. **归母 vs 含少数股东.** 合并利润表 contains both ``净利润`` (total, including
   minority interests) and ``归属于母公司股东的净利润``. Substring matching alone
   would map both onto the same key and silently mix the two levels.
2. **PB / EPS inputs.** ``equity`` and ``total_shares`` are already the
   system-wide ``FinancialStatement.values`` keys for 归母权益 and 期末总股本
   (see ``src/cn/providers/tushare.py`` and ``src/cn/demo_data.py``), while this
   card's canonical vocabulary is ``equity_parent`` / ``diluted_shares``. The
   projection map below bridges the two instead of forking the contract.

Matching is longest-synonym-wins and ratio/contra-account labels are rejected
outright (``毛利率``, ``占营业收入的比例``, ``存货跌价准备`` …) — a growth rate or a
provision is not a statement level, and mapping one to a level is exactly the
class of "looks right, is wrong" number the project forbids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .models import MetricSemantics


@dataclass(frozen=True)
class MetricSpec:
    """One canonical metric and its projection into the existing statement model."""

    key: str
    synonyms: Tuple[str, ...]
    statement: str                    # income | balance | cashflow | market
    semantics: MetricSemantics
    value_key: Optional[str] = None   # FinancialStatement.values key (None = 不入表)
    note: str = ""


# NOTE on ``value_key``: ``net_profit`` and ``equity`` are the *existing*
# system-wide keys and both mean the **归母** level (see
# ``src/cn/providers/tushare.py``: ``n_income_attr_p`` / ``total_hldr_eqy_exc_min_int``,
# and ``src/cn/report.py`` which labels ``net_profit`` as 归母净利润).
# The canonical vocabulary of this card spells them ``net_profit_parent`` and
# ``equity_parent``, so the projection maps canonical -> existing key and never
# the reverse. 含少数股东的净利润 gets its own key so the two can never merge.
STANDARD_METRICS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        "revenue", ("营业收入", "营业总收入", "营业收入合计", "主营业务收入", "营收"),
        "income", MetricSemantics.FLOW, "revenue",
        note="合并利润表首行；取合并口径，不取母公司",
    ),
    MetricSpec(
        "operating_cost", ("营业成本", "营业成本合计", "主营业务成本"),
        "income", MetricSemantics.FLOW, "operating_cost", note="不含税金及期间费用",
    ),
    MetricSpec(
        "gross_profit", ("毛利", "毛利润", "营业毛利"),
        "income", MetricSemantics.DERIVED, "gross_profit",
        note="无披露行时由 revenue − operating_cost 派生",
    ),
    MetricSpec(
        "operating_profit", ("营业利润",),
        "income", MetricSemantics.FLOW, "operating_profit", note="合并利润表",
    ),
    MetricSpec(
        "net_profit", ("净利润", "合并净利润", "净利润合计"),
        "income", MetricSemantics.FLOW, "net_profit_incl_minority",
        note="含少数股东损益，禁止与归母混用",
    ),
    MetricSpec(
        "net_profit_parent",
        (
            "归属于母公司股东的净利润", "归属于母公司所有者的净利润",
            "归属于上市公司股东的净利润", "归属于母公司股东的净利", "归母净利润",
            "归属母公司股东的净利润", "归属于母公司净利润", "归母净利",
        ),
        "income", MetricSemantics.FLOW, "net_profit",
        note="估值 EPS/PE 的分子；投影到现有 value key『net_profit』",
    ),
    MetricSpec(
        "net_profit_deducted",
        (
            "扣除非经常性损益后归属于母公司股东的净利润", "扣除非经常性损益后的净利润",
            "扣除非经常性损益后归属于母公司所有者的净利润", "扣非归母净利润", "扣非净利润",
        ),
        "income", MetricSemantics.FLOW, "net_profit_deducted", note="CARD-03 扣非指标来源",
    ),
    MetricSpec(
        "cfo", ("经营活动产生的现金流量净额", "经营活动现金流量净额", "经营活动产生的现金净流量"),
        "cashflow", MetricSemantics.FLOW, "operating_cash_flow", note="合并现金流量表",
    ),
    MetricSpec(
        "capex",
        (
            "购建固定资产、无形资产和其他长期资产支付的现金",
            "购建固定资产无形资产和其他长期资产支付的现金",
            "购建固定资产、无形资产和其他长期资产所支付的现金",
            "资本性支出",
        ),
        "cashflow", MetricSemantics.FLOW, "capex", note="资本开支",
    ),
    MetricSpec(
        "cfo_minus_capex", (),
        "cashflow", MetricSemantics.DERIVED, "free_cash_flow",
        note="无披露行时由 cfo − capex 派生（简化 FCF）",
    ),
    MetricSpec(
        "total_assets", ("资产总计", "资产合计", "总资产"),
        "balance", MetricSemantics.STOCK, "total_assets", note="stock，禁止差分",
    ),
    MetricSpec(
        "total_liabilities", ("负债合计", "负债总计", "总负债"),
        "balance", MetricSemantics.STOCK, "total_liabilities", note="stock，禁止差分",
    ),
    MetricSpec(
        "equity_parent",
        (
            "归属于母公司股东权益合计", "归属于母公司所有者权益合计",
            "归属于母公司股东的所有者权益", "归属于上市公司股东的净资产",
            "归属于母公司股东的权益", "归母所有者权益", "归母净资产", "股东权益归属母公司",
        ),
        "balance", MetricSemantics.STOCK, "equity",
        note="PB 的 BPS 来源；投影到现有 value key『equity』",
    ),
    MetricSpec(
        "inventory", ("存货", "存货净额"),
        "balance", MetricSemantics.STOCK, "inventory", note="stock；不含跌价准备",
    ),
    MetricSpec(
        "receivables", ("应收账款", "应收帐款", "应收账款净额", "应收账款合计"),
        "balance", MetricSemantics.STOCK, "receivables", note="stock；不含应收票据",
    ),
    MetricSpec(
        "contract_liabilities", ("合同负债",),
        "balance", MetricSemantics.STOCK, "contract_liabilities", note="stock",
    ),
    MetricSpec(
        "diluted_shares",
        ("期末总股本", "总股本", "股本总数", "普通股股份总数", "期末股本", "实收资本（或股本）"),
        "balance", MetricSemantics.STOCK, "total_shares",
        note="时点；缺失则估值阻断。投影到现有 value key『total_shares』",
    ),
    MetricSpec(
        "raw_price", ("收盘价", "最新收盘价"),
        "market", MetricSemantics.MARKET, None, note="行情链，必须是 RAW 价格",
    ),
    MetricSpec(
        "peers", ("可比公司", "可比公司集"),
        "market", MetricSemantics.MARKET, None, note="同业估值输入，不入财务报表",
    ),
)

METRIC_INDEX: Dict[str, MetricSpec] = {spec.key: spec for spec in STANDARD_METRICS}

# Longest-first synonym index so "归属于母公司股东的净利润" beats "净利润".
_SYNONYM_INDEX: Tuple[Tuple[str, str], ...] = tuple(
    sorted(
        ((synonym, spec.key) for spec in STANDARD_METRICS for synonym in spec.synonyms),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

# Labels that describe a *ratio*, a *change* or a *contra account* rather than a
# statement level. Rejected before synonym matching (fail-closed, no guessing).
_REJECT_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"同比|环比|增减|变动幅|增长率|增幅|降幅"), "变动态列/增长率"),
    (re.compile(r"占比|比例|百分比|毛利率|净利率|周转率|负债率|收益率"), "比率类标签"),
    (re.compile(r"每股|元/股|股收益"), "每股指标"),
    (re.compile(r"跌价|减值|准备|折旧|摊销|累计折耗"), "备抵/摊销类科目"),
    (re.compile(r"其中：.*余额$"), "附注明细余额"),
)

_LEADING_MARKERS = re.compile(
    r"^(?:其中[：:]?|减[：:]|加[：:]|注[：:]|\(?[一二三四五六七八九十]+\)?[、.]?|"
    r"\d+[、.]|\([0-9]+\)|（[0-9]+）)+"
)
_TRAILING_UNIT_NOTE = re.compile(r"[（(](?:元|千元|万元|百万元|亿元|元/股|%|人民币元)[）)]$")


@dataclass(frozen=True)
class MetricMatch:
    """Result of resolving one disclosed row label."""

    spec: Optional[MetricSpec]
    label: str
    unknown_key: Optional[str] = None
    reason: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.spec is not None

    @property
    def key(self) -> str:
        """Canonical key, or the ``metric_unknown_<slug>`` placeholder (§7.1)."""
        return self.spec.key if self.spec else (self.unknown_key or "metric_unknown")

    @property
    def semantics(self) -> MetricSemantics:
        return self.spec.semantics if self.spec else MetricSemantics.FLOW

    @property
    def statement(self) -> Optional[str]:
        return self.spec.statement if self.spec else None

    @property
    def statement_value_key(self) -> Optional[str]:
        if not self.spec or self.spec.semantics is MetricSemantics.MARKET:
            return None
        return self.spec.value_key or self.spec.key


def normalize_label(label: object) -> str:
    """Strip numbering, unit notes and separators so labels can be matched."""
    if label is None:
        return ""
    text = str(label)
    text = text.translate(str.maketrans("（）［］：，．", "()[]:,.", ))
    text = re.sub(r"\s+", "", text)
    text = _TRAILING_UNIT_NOTE.sub("", text)
    text = _LEADING_MARKERS.sub("", text)
    return text.strip("：:、.，")


def unknown_metric_key(label: object) -> str:
    """Deterministic placeholder key for a label outside the §7.1 dictionary."""
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "_", normalize_label(label)) or "blank"
    return f"metric_unknown_{slug[:40]}"


def resolve_metric(label: object) -> MetricMatch:
    """Map a disclosed row label onto a canonical metric (never guesses)."""
    text = normalize_label(label)
    if not text:
        return MetricMatch(None, text, unknown_key=unknown_metric_key(label),
                           reason="空标签，无法命名指标")
    for pattern, reason in _REJECT_PATTERNS:
        if pattern.search(text):
            return MetricMatch(None, text, unknown_key=unknown_metric_key(label), reason=reason)

    exact = [key for synonym, key in _SYNONYM_INDEX if synonym == text]
    if len(set(exact)) == 1:
        return MetricMatch(METRIC_INDEX[exact[0]], text)

    hits = [(len(synonym), key) for synonym, key in _SYNONYM_INDEX if len(synonym) >= 2 and synonym in text]
    if hits:
        best_len = max(length for length, _ in hits)
        best_keys = {key for length, key in hits if length == best_len}
        if len(best_keys) == 1:
            key = best_keys.pop()
            if _is_narrower_aggregate(text, key):
                # 见 _is_narrower_aggregate：更细分的小计不得冒充更宽的合计。
                return MetricMatch(
                    None, text, unknown_key=unknown_metric_key(label),
                    reason="更细分的小计标签（前缀+合计），不得映射到更宽的合计指标",
                )
            return MetricMatch(METRIC_INDEX[key], text)
        return MetricMatch(None, text, unknown_key=unknown_metric_key(label),
                           reason=f"标签歧义：同时匹配 {sorted(best_keys)}")
    return MetricMatch(None, text, unknown_key=unknown_metric_key(label),
                       reason="不在 §7.1 标准指标字典内")


def _is_narrower_aggregate(text: str, key: str) -> bool:
    """Whether *text* is a **narrower** subtotal than the synonym it matched.

    ``流动资产合计`` contains ``资产合计``, and ``流动负债合计`` contains
    ``负债合计``, so longest-synonym-wins silently mapped both subtotals onto
    their own parent — ``resolve_metric("流动资产合计")`` returned
    ``total_assets``. On a real balance sheet both rows exist in the same column,
    so the projection then saw two candidates for ``total_assets`` and CARD-02's
    precedence (document order) picked the *component*, silently replacing the
    printed 资产总计 with 流动资产合计.

    CARD-22's F1 check surfaced this the first time it ran against a filing that
    carries the standard sub-total rows. The fix is deliberately narrow: it fires
    only when the label ends with a sub-total marker **and** the matched synonym
    is a proper suffix of it, which is exactly the "prefix narrows the total"
    shape. Everything else keeps the existing longest-match behaviour, because
    ``归属于上市公司股东的净利润`` must still resolve through ``净利润``.
    """
    if not text.endswith(("合计", "总计", "小计")):
        return False
    spec = METRIC_INDEX.get(key)
    if spec is None:
        return False
    return any(
        synonym != text and len(text) > len(synonym) and text.endswith(synonym)
        for synonym in spec.synonyms
    )


def metric_spec(key: str) -> Optional[MetricSpec]:
    """Look up a canonical metric (``None`` for unknown/placeholder keys)."""
    return METRIC_INDEX.get(key)


def is_known_metric(key: str) -> bool:
    return key in METRIC_INDEX


def statement_value_key(key: str) -> Optional[str]:
    """Projection of a canonical key onto ``FinancialStatement.values``."""
    spec = METRIC_INDEX.get(key)
    if not spec or spec.semantics is MetricSemantics.MARKET:
        return None
    return spec.value_key or spec.key


def is_stock_metric(key: str) -> bool:
    spec = METRIC_INDEX.get(key)
    return bool(spec and spec.semantics is MetricSemantics.STOCK)


def is_flow_metric(key: str) -> bool:
    spec = METRIC_INDEX.get(key)
    return bool(spec and spec.semantics in (MetricSemantics.FLOW, MetricSemantics.DERIVED))
