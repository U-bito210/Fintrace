"""Deterministic peer-multiple calculations and CARD-08 scenario valuation.

Two layers live here and both are deliberately model-free:

* **Peer multiples** (the original layer) — PE/PB/PS medians over an
  evidence-backed peer set, IQR outlier removal, and implied prices computed
  from **RAW** prices only.
* **Scenario valuation** (CARD-08) — Bear/Base/Bull scenarios, EPS × target-PE
  and BPS × target-PB sensitivity matrices, a method-by-method dispersion
  report, and an **Assumption Registry** so every number can be walked back
  through 数据 → 公式 → 假设 → 输出.

Design rules this module enforces (each is a card requirement, not a style
preference):

* **TTM, never a static FY EPS** — the per-share drivers are built from CARD-02's
  existing period engine output (``NormalizationResult.ttm``); see
  :func:`valuation_inputs_from_normalization`.  When the engine's own TTM
  short-circuits at an FY boundary the fact is *reported* as a warning rather
  than hidden, because at that boundary TTM equals FY.
* **ROE does not enter the price formula** — the pre-release PB matrix is
  ``BPS × target PB`` (card §7.3 option ①); the ROE × PB form would produce
  cells unrelated to its own axis.  No ROE axis is emitted.
* **No fabricated multiples** — when neither a peer set nor an analyst target
  multiple is supplied, the method is reported ``not_applicable``; nothing is
  invented to fill the gap.
* **Failure injection stays closed** — a missing/invalid ``diluted_shares``
  blocks every per-share result (``ValuationInputs`` raises), so a blocked
  snapshot can never leak an invented per-share number.
* **Zero silence** — an unresolvable assumption id raises; a value that cannot
  be computed is ``None`` with a warning, never a zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import isfinite
from statistics import median
from typing import Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple


MultipleName = Literal["PE", "PB", "PS"]


@dataclass(frozen=True)
class PeerValuationInput:
    symbol: str
    raw_price: float
    total_shares: float
    net_profit: Optional[float]
    book_equity: Optional[float]
    revenue: Optional[float]
    period_basis: str

    @property
    def market_cap(self) -> float:
        return self.raw_price * self.total_shares


@dataclass(frozen=True)
class PeerMultiple:
    symbol: str
    name: MultipleName
    value: Optional[float]
    exclusion_reason: Optional[str] = None


@dataclass(frozen=True)
class MultipleSummary:
    name: MultipleName
    median: Optional[float]
    included_symbols: List[str]
    excluded: Dict[str, str]
    confidence: Literal["high", "medium", "low", "unavailable"]


def calculate_multiple(peer: PeerValuationInput, name: MultipleName) -> PeerMultiple:
    if peer.raw_price <= 0 or peer.total_shares <= 0:
        return PeerMultiple(peer.symbol, name, None, "invalid_raw_price_or_shares")
    denominator = {
        "PE": peer.net_profit,
        "PB": peer.book_equity,
        "PS": peer.revenue,
    }[name]
    if denominator is None or denominator <= 0:
        return PeerMultiple(peer.symbol, name, None, f"non_positive_{name}_denominator")
    return PeerMultiple(peer.symbol, name, peer.market_cap / denominator)


def _iqr_bounds(values: List[float]) -> tuple[float, float]:
    sorted_values = sorted(values)
    midpoint = len(sorted_values) // 2
    lower_half = sorted_values[:midpoint]
    upper_half = sorted_values[midpoint + (len(sorted_values) % 2):]
    q1, q3 = median(lower_half), median(upper_half)
    iqr = q3 - q1
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


def summarize_peer_multiple(peers: Iterable[PeerValuationInput], name: MultipleName) -> MultipleSummary:
    """Filter invalid values and IQR outliers before calculating a peer median."""
    computed = [calculate_multiple(peer, name) for peer in peers]
    excluded = {item.symbol: item.exclusion_reason for item in computed if item.exclusion_reason}
    valid = [item for item in computed if item.value is not None]
    if not valid:
        return MultipleSummary(name, None, [], excluded, "unavailable")

    # IQR becomes meaningful only with four or more valid peers.  Smaller samples
    # remain visible but explicitly have low confidence rather than fabricated
    # outlier decisions.
    included = valid
    if len(valid) >= 4:
        lower, upper = _iqr_bounds([item.value for item in valid if item.value is not None])
        included = []
        for item in valid:
            if item.value is not None and not lower <= item.value <= upper:
                excluded[item.symbol] = "iqr_outlier"
            else:
                included.append(item)
    confidence: Literal["high", "medium", "low", "unavailable"]
    confidence = "high" if len(included) >= 6 else "medium" if len(included) >= 4 else "low"
    return MultipleSummary(
        name=name,
        median=median([item.value for item in included if item.value is not None]) if included else None,
        included_symbols=[item.symbol for item in included],
        excluded=excluded,
        confidence=confidence if included else "unavailable",
    )


def implied_price(
    target: PeerValuationInput, *, multiple: MultipleSummary, denominator: Literal["net_profit", "book_equity", "revenue"]
) -> Optional[float]:
    """Return a per-share value only when the target and peer basis are valid."""
    if multiple.median is None or target.total_shares <= 0:
        return None
    value = getattr(target, denominator)
    if value is None or value <= 0:
        return None
    return multiple.median * value / target.total_shares


# ---------------------------------------------------------------------------
# CARD-08 — Assumption Registry
# ---------------------------------------------------------------------------

#: Scenarios, in the order they are rendered and dispersed.
SCENARIO_BEAR = "bear"
SCENARIO_BASE = "base"
SCENARIO_BULL = "bull"
SCENARIO_ORDER: Tuple[str, ...] = (SCENARIO_BEAR, SCENARIO_BASE, SCENARIO_BULL)
SCENARIO_LABELS: Dict[str, str] = {SCENARIO_BEAR: "悲观", SCENARIO_BASE: "基准", SCENARIO_BULL: "乐观"}

#: An assumption whose ``source`` starts with this prefix carries no sourced
#: basis and must explain itself in ``reason``.  Anything else must resolve to a
#: known evidence id — see :meth:`AssumptionRegistry.register`.
ASSUMPTION_PREFIX = "assumption:"

#: The per-share driver each method multiplies by its target multiple.
DRIVER_KEYS: Dict[str, str] = {"PE": "eps_ttm", "PB": "bps", "PS": "revenue_per_share_ttm"}
DRIVER_LABELS: Dict[str, str] = {
    "PE": "每股收益（TTM 归母净利润 / 期末总股本）",
    "PB": "每股净资产（期末归母权益 / 期末总股本）",
    "PS": "每股营业收入（TTM 营业收入 / 期末总股本）",
}

#: 方法适用边界（确定性模板，不含任何判断性结论）。
APPLICABILITY_NOTES: Dict[str, str] = {
    "PE": (
        "PE 法适用于盈利为正且相对稳定的公司；分母是 TTM 归母净利润，"
        "禁止用 FY 静态盈利代替未来 12 个月盈利。EPS ≤ 0 时该方法不适用，"
        "不输出负市盈率伪区间。"
    ),
    "PB": (
        "PB 法适用于重资产与金融业公司，以期末归母权益的每股账面价值为基数。"
        "初赛采用 BPS × 目标 PB 矩阵：ROE 不进入价格公式，因此不作为矩阵轴"
        "（显式 PB = f(ROE, COE, g) 模型留待决赛）。BPS ≤ 0 时该方法不适用。"
    ),
    "PS": (
        "PS 法适用于亏损但收入口径可比的成长型公司；分母是 TTM 营业收入，"
        "每股收入 ≤ 0 时该方法不适用。"
    ),
}
DISPERSION_NOTE = "各方法与各情景的区间分别展示，只报告离散度，不合成单一『最终区间』。"


class AssumptionError(ValueError):
    """An assumption is malformed, unresolvable, or overrides a sourced input."""


class AssumptionConflictError(AssumptionError):
    """An assumption tried to override a parameter that came from the data."""


@dataclass(frozen=True)
class Assumption:
    """One row of the Assumption Registry.

    ``source`` is either an evidence id the value actually rests on, or an
    ``assumption: …`` string.  The distinction is load-bearing: CARD-08 §9
    degrades a result to "纯假设演示" only when *every* assumption lacks a
    sourced basis, so the two forms must never be blurred.
    """

    assumption_id: str
    value: object
    source: str
    reason: str
    valid_from: str

    @property
    def grade(self) -> str:
        """``sourced`` when the assumption rests on evidence, else ``assumption``."""
        return "assumption" if str(self.source).startswith(ASSUMPTION_PREFIX) else "sourced"

    @property
    def sourced(self) -> bool:
        return self.grade == "sourced"

    def to_dict(self) -> Dict[str, object]:
        return {
            "assumption_id": self.assumption_id,
            "value": self.value,
            "source": self.source,
            "reason": self.reason,
            "valid_from": self.valid_from,
            "grade": self.grade,
        }


class AssumptionRegistry:
    """Deterministic store of the assumptions one valuation rests on.

    Parameters
    ----------
    sourced:
        ``parameter name -> evidence id`` for every parameter that came from the
        data (TTM 归母净利润、期末总股本、RAW 价格 …).  Registering an assumption
        under one of those names raises :class:`AssumptionConflictError`, which is
        card §8 edge case 3: an assumption may never *override* a sourced number.
    reserved:
        Data-derived parameter names whose evidence id is unknown.  They are
        protected exactly like ``sourced`` (an assumption may not take their
        name), but they contribute nothing to the evidence set.
    evidence_ids:
        Additional evidence ids the registry will accept as an assumption's
        source (e.g. peer-multiple evidence).  An id outside this set is rejected
        rather than trusted — a dangling reference is worse than no reference.
    """

    def __init__(
        self,
        *,
        sourced: Optional[Mapping[str, str]] = None,
        reserve: Optional[Iterable[str]] = None,
        evidence_ids: Iterable[str] = (),
    ) -> None:
        self._sourced: Dict[str, str] = {
            str(key): str(value) for key, value in dict(sourced or {}).items()
        }
        for name in reserve or ():
            self._sourced.setdefault(str(name), "")
        self._known_evidence = {str(item) for item in evidence_ids} | {
            value for value in self._sourced.values() if value
        }
        self._items: Dict[str, Assumption] = {}

    # -- inspection -------------------------------------------------------

    @property
    def sourced_parameters(self) -> Dict[str, str]:
        return dict(self._sourced)

    @property
    def known_evidence_ids(self) -> frozenset:
        return frozenset(self._known_evidence)

    def has(self, assumption_id: str) -> bool:
        return assumption_id in self._items

    def get(self, assumption_id: str) -> Assumption:
        item = self._items.get(assumption_id)
        if item is None:
            raise AssumptionError(f"未登记的假设: {assumption_id}")
        return item

    def ids(self) -> List[str]:
        return list(self._items)

    def items(self) -> List[Assumption]:
        return list(self._items.values())

    def unresolved(self, assumption_ids: Iterable[str]) -> List[str]:
        """The subset of *assumption_ids* that cannot be resolved (order kept)."""
        return [str(item) for item in assumption_ids if str(item) not in self._items]

    def resolve(self, assumption_ids: Iterable[str]) -> List[Assumption]:
        """Resolve every id or raise — never silently drop one."""
        missing = self.unresolved(assumption_ids)
        if missing:
            raise AssumptionError(f"无法解析的假设 id: {', '.join(missing)}")
        return [self._items[str(item)] for item in assumption_ids]

    def require(self, assumption_ids: Iterable[str]) -> List[Assumption]:
        """Alias of :meth:`resolve` with an explicit "fail-closed" reading."""
        return self.resolve(assumption_ids)

    @property
    def all_assumption_grade(self) -> bool:
        """True when no assumption in the registry carries a sourced basis."""
        return bool(self._items) and all(not item.sourced for item in self._items.values())

    def to_dict(self) -> Dict[str, object]:
        return {
            "sourced_parameters": dict(self._sourced),
            "assumptions": [item.to_dict() for item in self._items.values()],
            "pure_assumption": self.all_assumption_grade,
        }

    # -- construction -----------------------------------------------------

    def register(
        self,
        assumption_id: str,
        value: object,
        *,
        source: str,
        reason: str,
        valid_from: str = "",
    ) -> Assumption:
        """Record one assumption, refusing anything that cannot be traced.

        Raises :class:`AssumptionConflictError` when the id names a sourced
        parameter, :class:`AssumptionError` when the source is neither a known
        evidence id nor an ``assumption:`` rationale, and when no reason is given
        (the "假设依据" scoring point leaves no room for an unexplained number).
        """
        name = str(assumption_id).strip()
        if not name:
            raise AssumptionError("假设必须有 assumption_id。")
        if name in self._sourced:
            origin = self._sourced[name] or "数据层"
            raise AssumptionConflictError(
                f"『{name}』是 sourced 参数（来源 {origin}），"
                "假设只能登记 assumption 性参数，不能覆盖数据来源的取值。"
            )
        text = str(source or "").strip()
        if not text:
            raise AssumptionError(f"假设『{name}』必须给出 source（evidence id 或 assumption: 理由）。")
        if text.startswith(ASSUMPTION_PREFIX):
            if not text[len(ASSUMPTION_PREFIX):].strip():
                raise AssumptionError(f"假设『{name}』的 source 只有前缀，缺少理由。")
        elif text not in self._known_evidence:
            raise AssumptionError(
                f"假设『{name}』的 source『{text}』既不是已知 evidence id，也未以 "
                f"『{ASSUMPTION_PREFIX}』开头；无法追溯的假设一律拒绝。"
            )
        if not str(reason or "").strip():
            raise AssumptionError(f"假设『{name}』必须给出 reason（假设依据）。")
        item = Assumption(
            assumption_id=name,
            value=value,
            source=text,
            reason=str(reason).strip(),
            valid_from=str(valid_from or ""),
        )
        self._items[name] = item
        return item

    def register_many(self, rows: Iterable[Mapping[str, object]]) -> List[Assumption]:
        return [
            self.register(
                str(row["assumption_id"]),
                row.get("value"),
                source=str(row.get("source") or ""),
                reason=str(row.get("reason") or ""),
                valid_from=str(row.get("valid_from") or ""),
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# CARD-08 — valuation inputs (the 数据 layer)
# ---------------------------------------------------------------------------

#: Card §8 edge case 2 / demo failure injection #1.
SHARES_BLOCK_REASON = "missing_or_invalid_total_shares"

#: Parameters that always come from the data.  An assumption may never take one
#: of these names, whether or not its evidence id is known (card §8 edge case 3).
SOURCED_PARAMETERS: Tuple[str, ...] = (
    "symbol",
    "raw_price",
    "total_shares",
    "net_profit_parent_ttm",
    "revenue_ttm",
    "equity_parent_end",
    "equity_parent_begin",
)


@dataclass(frozen=True)
class ValuationInputs:
    """Everything a relative valuation may use, each with its evidence trail.

    ``evidence_ids`` maps a parameter name to its primary evidence id;
    ``evidence_all`` keeps *every* supporting fact id (a derived TTM figure rests
    on up to three disclosures), and ``formulas`` records how each value was
    obtained.  Together they are the "数据" and "公式" layers of the card's
    four-layer traceability chain.
    """

    symbol: str
    raw_price: float
    total_shares: float
    net_profit_parent_ttm: Optional[float] = None
    revenue_ttm: Optional[float] = None
    equity_parent_end: Optional[float] = None
    equity_parent_begin: Optional[float] = None
    period_key: str = ""
    period_basis: str = "TTM"
    document_id: Optional[str] = None
    evidence_ids: Dict[str, str] = field(default_factory=dict)
    evidence_all: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    formulas: Dict[str, str] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Fail-closed: a RAW price and a share count are the two inputs that make
        # a per-share number possible at all.  Without them the correct answer is
        # "blocked", never "assume 1 share".
        if not _positive(self.raw_price):
            raise ValueError("invalid_raw_price")
        if not _positive(self.total_shares):
            raise ValueError(SHARES_BLOCK_REASON)

    # -- 输出层之前的所有每股驱动量 ---------------------------------------

    @property
    def market_cap(self) -> float:
        return self.raw_price * self.total_shares

    @property
    def eps_ttm(self) -> Optional[float]:
        return _per_share(self.net_profit_parent_ttm, self.total_shares)

    @property
    def bps(self) -> Optional[float]:
        return _per_share(self.equity_parent_end, self.total_shares)

    @property
    def revenue_per_share_ttm(self) -> Optional[float]:
        return _per_share(self.revenue_ttm, self.total_shares)

    @property
    def average_equity_parent(self) -> Optional[float]:
        """期初+期末 / 2 — an *approximation* whose basis is reported, not hidden."""
        if self.equity_parent_begin is None or self.equity_parent_end is None:
            return None
        return (float(self.equity_parent_begin) + float(self.equity_parent_end)) / 2.0

    @property
    def roe_ttm_pct(self) -> Optional[float]:
        average = self.average_equity_parent
        if self.net_profit_parent_ttm is None or average is None or average <= 0:
            return None
        return float(self.net_profit_parent_ttm) / average * 100.0

    def driver(self, method: MultipleName) -> Optional[float]:
        return {
            "PE": self.eps_ttm,
            "PB": self.bps,
            "PS": self.revenue_per_share_ttm,
        }[method]

    def to_peer_input(self, *, period_basis: Optional[str] = None) -> PeerValuationInput:
        """Adapt to the peer-multiple engine.

        The peer *comparison* is gated on the period tag, so the target carries its
        TTM period key (``2025FY`` / ``2026H1``) — a peer has to be on the same
        period to be comparable.  The TTM basis itself stays visible in
        :attr:`period_basis` and in the payload's ``period_basis`` field.
        """
        return PeerValuationInput(
            symbol=self.symbol,
            raw_price=self.raw_price,
            total_shares=self.total_shares,
            net_profit=self.net_profit_parent_ttm,
            book_equity=self.equity_parent_end,
            revenue=self.revenue_ttm,
            period_basis=period_basis or self.period_key or self.period_basis or "TTM",
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "document_id": self.document_id,
            "raw_price": self.raw_price,
            "price_basis": "RAW",
            "total_shares": self.total_shares,
            "market_cap": self.market_cap,
            "period_key": self.period_key,
            "period_basis": self.period_basis,
            "net_profit_parent_ttm": self.net_profit_parent_ttm,
            "revenue_ttm": self.revenue_ttm,
            "equity_parent_end": self.equity_parent_end,
            "equity_parent_begin": self.equity_parent_begin,
            "average_equity_parent": self.average_equity_parent,
            "eps_ttm": self.eps_ttm,
            "bps": self.bps,
            "revenue_per_share_ttm": self.revenue_per_share_ttm,
            "roe_ttm_pct": self.roe_ttm_pct,
            "evidence_ids": dict(self.evidence_ids),
            "evidence_all": {key: list(value) for key, value in self.evidence_all.items()},
            "formulas": dict(self.formulas),
            "warnings": list(self.warnings),
        }


def valuation_inputs_from_normalization(
    result: object,
    *,
    symbol: str,
    raw_price: float,
    document_id: Optional[str] = None,
) -> ValuationInputs:
    """Build :class:`ValuationInputs` from a CARD-02 ``NormalizationResult``.

    Reuses the existing engine's output verbatim — ``result.ttm`` (derived by
    :class:`~src.cn.periods.FinancialPeriodEngine` through CARD-02's projection)
    for the flow drivers and ``result.statements`` for the balance values.  No
    differencing, no re-derivation and no FY substitution happens here; a value
    the pipeline did not produce stays ``None`` with a warning.
    """
    warnings: List[str] = []
    evidence_all: Dict[str, Tuple[str, ...]] = {}
    formulas: Dict[str, str] = {}
    evidence_ids: Dict[str, str] = {}

    def _facts(metric: str, periods: Sequence[str]) -> Tuple[str, ...]:
        wanted = {str(period) for period in periods}
        return tuple(
            str(getattr(fact, "normalized_fact_id", ""))
            for fact in getattr(result, "facts", ()) or ()
            if str(getattr(fact, "metric", "")) == metric
            and str(getattr(fact, "period_key", "")) in wanted
            and getattr(fact, "normalized_fact_id", None)
        )

    def _claim(parameter: str, ids: Tuple[str, ...], formula: str) -> None:
        if ids:
            evidence_all[parameter] = ids
            evidence_ids[parameter] = ids[0]
        formulas[parameter] = formula

    # -- flow drivers from the engine's TTM ---------------------------------
    ttm_records = sorted(
        (item for item in getattr(result, "ttm", ()) or () if item.statement_type == "income"),
        key=lambda item: _period_sort_key(item.fiscal_period),
    )
    period_key = ""
    net_profit: Optional[float] = None
    revenue: Optional[float] = None
    if not ttm_records:
        warnings.append("no_ttm_period：CARD-02 的期间引擎没有产出 TTM 利润表，PE/PS 不可计算。")
    else:
        latest = ttm_records[-1]
        period_key = str(latest.fiscal_period)
        values = dict(latest.values)
        net_profit = _finite(values.get("net_profit"))
        revenue = _finite(values.get("revenue"))
        sources = tuple(str(item) for item in latest.source_periods)
        if len(sources) == 1:
            # The engine short-circuits at an FY boundary: TTM *is* the FY value.
            warnings.append(
                f"ttm_period_is_fy_boundary：{period_key} 的 TTM 由 FY 记录直接承接"
                "（期间引擎在年报边界不做插值），已如实标注，未用 FY 静态值替代未来 12 个月盈利。"
            )
        _claim(
            "net_profit_parent_ttm",
            _facts("net_profit_parent", sources),
            f"TTM = FinancialPeriodEngine.derive_ttm({'+'.join(sources)})",
        )
        _claim("revenue_ttm", _facts("revenue", sources), f"TTM 营业收入（来源期间 {', '.join(sources)}）")
        if net_profit is None:
            warnings.append("ttm_net_profit_missing：TTM 利润表没有归母净利润，PE 不适用。")
        if revenue is None:
            warnings.append("ttm_revenue_missing：TTM 利润表没有营业收入，PS 不适用。")

    # -- balance values ------------------------------------------------------
    balance_records = sorted(
        (item for item in getattr(result, "statements", ()) or () if item.statement_type == "balance"),
        key=lambda item: _period_sort_key(item.fiscal_period),
    )
    shares: Optional[float] = None
    shares_period = ""
    equity_end: Optional[float] = None
    equity_begin: Optional[float] = None
    for record in balance_records:  # last writer wins == latest period
        value = _finite(dict(record.values).get("total_shares"))
        if value is not None and value > 0:
            shares = value
            shares_period = str(record.fiscal_period)
    if shares is None:
        warnings.append(
            "total_shares_missing：资产负债表没有可用的期末总股本，每股估值被阻断（fail-closed）。"
        )
    else:
        _claim("total_shares", _facts("diluted_shares", (shares_period,)), "披露的期末总股本")

    if balance_records:
        latest = balance_records[-1]
        equity_end = _finite(dict(latest.values).get("equity"))
        equity_end_period = str(latest.fiscal_period)
        priors = [item for item in balance_records if item.fiscal_period != equity_end_period]
        if priors:
            equity_begin = _finite(dict(priors[-1].values).get("equity"))
            _claim(
                "equity_parent_begin",
                _facts("equity_parent", (str(priors[-1].fiscal_period),)),
                f"期初归母权益（{priors[-1].fiscal_period} 时点）",
            )
        else:
            warnings.append("equity_begin_missing：没有更早一期的资产负债表，ROE 的平均净资产不可近似。")
        _claim(
            "equity_parent_end",
            _facts("equity_parent", (equity_end_period,)),
            f"期末归母权益（{equity_end_period} 时点）",
        )
        if equity_end is None:
            warnings.append("equity_missing：资产负债表没有归母权益，BPS 不可计算，PB 不适用。")
    else:
        warnings.append("balance_sheet_missing：文档没有可用的资产负债表记录，BPS 与 ROE 不可计算。")

    return ValuationInputs(
        symbol=symbol,
        raw_price=float(raw_price),
        total_shares=float(shares or 0.0),   # 0.0 → __post_init__ raises (fail-closed)
        net_profit_parent_ttm=net_profit,
        revenue_ttm=revenue,
        equity_parent_end=equity_end,
        equity_parent_begin=equity_begin,
        period_key=period_key,
        period_basis="TTM",
        document_id=document_id or getattr(result, "document_id", None),
        evidence_ids=evidence_ids,
        evidence_all=evidence_all,
        formulas=formulas,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# CARD-08 — scenarios, sensitivity matrices, dispersion
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScenarioValuation:
    """One (method, scenario) valuation, with its interval and its trail.

    ``value_low`` / ``value_mid`` / ``value_high`` are the card's three-point
    interval; they are ``None`` exactly when ``applicable`` is False, and
    ``not_applicable_reason`` then says why (a negative EPS produces no PE
    interval rather than a negative one).
    """

    method: str
    scenario: str
    value_low: Optional[float]
    value_mid: Optional[float]
    value_high: Optional[float]
    driver_key: str
    driver_mid: Optional[float]
    target_multiple: Optional[float]
    band: Optional[float]
    formula: str
    assumption_ids: Tuple[str, ...] = ()
    calculation_ids: Tuple[str, ...] = ()
    applicable: bool = True
    not_applicable_reason: Optional[str] = None
    note: Optional[str] = None

    @property
    def ordered(self) -> bool:
        if self.value_low is None or self.value_mid is None or self.value_high is None:
            return False
        return self.value_low <= self.value_mid <= self.value_high

    def to_dict(self) -> Dict[str, object]:
        return {
            "method": self.method,
            "scenario": self.scenario,
            "scenario_label": SCENARIO_LABELS.get(self.scenario, self.scenario),
            "value_low": self.value_low,
            "value_mid": self.value_mid,
            "value_high": self.value_high,
            "unit": "元/股",
            "applicable": self.applicable,
            "not_applicable_reason": self.not_applicable_reason,
            "driver_key": self.driver_key,
            "driver_mid": self.driver_mid,
            "target_multiple": self.target_multiple,
            "band": self.band,
            "formula": self.formula,
            "assumption_ids": list(self.assumption_ids),
            "calculation_ids": list(self.calculation_ids),
            "note": self.note,
        }


@dataclass(frozen=True)
class SensitivityMatrix:
    """An ``x`` × ``y`` implied-price grid.

    Orientation is fixed by card §7.3: **rows follow the y axis** (the per-share
    driver — EPS for PE, BPS for PB) and **columns follow the x axis** (the target
    multiple), so ``matrix[i][j] == y_values[i] * x_values[j]``.  ``row_labels`` /
    ``column_labels`` carry the scenario each axis value came from, so a cell can
    always be traced back to an assumption.
    """

    name: str
    x_name: str
    y_name: str
    x_values: Tuple[float, ...]
    y_values: Tuple[float, ...]
    matrix: Tuple[Tuple[float, ...], ...]
    row_labels: Tuple[str, ...]
    column_labels: Tuple[str, ...]
    formula: str
    shares_used: float
    assumption_ids: Tuple[str, ...] = ()
    note: str = ""

    def value_at(self, row: int, column: int) -> float:
        return self.matrix[row][column]

    def cell(self, *, x: float, y: float) -> float:
        """The cell for an exact (x, y) pair; raises when the pair is not on the grid."""
        try:
            column = self.x_values.index(x)
            row = self.y_values.index(y)
        except ValueError as exc:  # pragma: no cover - defensive
            raise KeyError(f"({x}, {y}) 不在敏感性矩阵网格上") from exc
        return self.matrix[row][column]

    @property
    def monotonic(self) -> bool:
        """Rows increase with x and columns increase with y (card §10)."""
        for row in self.matrix:
            if any(b <= a for a, b in zip(row, row[1:])):
                return False
        for column in zip(*self.matrix):
            if any(b <= a for a, b in zip(column, column[1:])):
                return False
        return True

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "x_name": self.x_name,
            "y_name": self.y_name,
            "x_values": list(self.x_values),
            "y_values": list(self.y_values),
            "matrix": [list(row) for row in self.matrix],
            "row_labels": list(self.row_labels),
            "column_labels": list(self.column_labels),
            "orientation": "matrix[行=y 轴驱动量][列=x 轴目标倍数]",
            "formula": self.formula,
            "shares_used": self.shares_used,
            "assumption_ids": list(self.assumption_ids),
            "monotonic": self.monotonic,
            "note": self.note,
        }


@dataclass(frozen=True)
class ValuationResult:
    """Everything CARD-08 produces for one symbol/document."""

    status: str
    inputs: Dict[str, object]
    scenarios: Tuple[ScenarioValuation, ...]
    sensitivity: Tuple[SensitivityMatrix, ...]
    method_dispersion: Dict[str, object]
    applicability_note: str
    assumptions: Tuple[Assumption, ...]
    warnings: Tuple[str, ...] = ()
    calculation_ids: Tuple[str, ...] = ()
    blocked_reason: Optional[str] = None

    def scenarios_for(self, method: str) -> List[ScenarioValuation]:
        return [item for item in self.scenarios if item.method == method]

    def applicable_methods(self) -> List[str]:
        return sorted({item.method for item in self.scenarios if item.applicable})

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "inputs": self.inputs,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "sensitivity": [item.to_dict() for item in self.sensitivity],
            "dispersion": self.method_dispersion,
            "applicability_note": self.applicability_note,
            "assumptions": [item.to_dict() for item in self.assumptions],
            "warnings": list(self.warnings),
            "calculation_ids": list(self.calculation_ids),
        }


#: Demonstration defaults.  They are *assumption-grade by construction*: the
#: source string says out loud that they are not analyst input, so a run that
#: uses them can never be mistaken for an evidence-backed valuation (§9).
DEMO_SCENARIO_GROWTH: Dict[str, float] = {SCENARIO_BEAR: -0.2, SCENARIO_BASE: 0.0, SCENARIO_BULL: 0.2}
DEMO_SCENARIO_BAND: Dict[str, float] = {SCENARIO_BEAR: 0.1, SCENARIO_BASE: 0.1, SCENARIO_BULL: 0.1}

#: How each scenario shifts the *base* target multiple (card §7.2: "Bear/Base/Bull
#: 的 EPS 增速与目标倍数各登记假设").  Applied to whatever base multiple the
#: peers or the analyst supplied, and written into every assumption's reason so
#: the scaling is never a hidden constant.
SCENARIO_MULTIPLE_FACTOR: Dict[str, float] = {
    SCENARIO_BEAR: 0.9,
    SCENARIO_BASE: 1.0,
    SCENARIO_BULL: 1.1,
}

DEMO_GROWTH_SOURCE = f"{ASSUMPTION_PREFIX} 演示默认增速（非分析师输入）"
DEMO_BAND_SOURCE = f"{ASSUMPTION_PREFIX} 演示默认带宽（非分析师输入）"
ANALYST_SOURCE = f"{ASSUMPTION_PREFIX} 分析师输入的目标倍数"
PEER_SOURCE = f"{ASSUMPTION_PREFIX} 承接同业可比中位数（该中位数本身来自证据）"


def assumption_ids_for(method: MultipleName, scenario: str) -> Tuple[str, ...]:
    """The three assumption ids one (method, scenario) cell rests on."""
    prefix = method.lower()
    return (
        f"{prefix}_growth_{scenario}",
        f"{prefix}_target_{scenario}",
        f"{prefix}_band_{scenario}",
    )


def register_scenario_assumptions(
    registry: AssumptionRegistry,
    *,
    target_multiples: Optional[Mapping[str, float]] = None,
    target_source: str = ANALYST_SOURCE,
    growth: Optional[Mapping[str, float]] = None,
    band: Optional[Mapping[str, float]] = None,
    methods: Sequence[MultipleName] = ("PE", "PB", "PS"),
    valid_from: str = "",
    skip_existing: bool = True,
) -> List[str]:
    """Register the scenario growth / band / target-multiple assumptions.

    A method with no target multiple is *not* registered — the caller (or the
    tool) decides whether the multiple comes from peers or from an analyst, and
    inventing one here would be exactly the silent guess the project forbids.
    Every scenario nevertheless gets a *distinct* target multiple, because the
    sensitivity matrix needs a real x axis and card §7.2 says each scenario
    registers its own multiple; the shift factor is named in the reason.
    """
    growth = dict(growth or DEMO_SCENARIO_GROWTH)
    band = dict(band or DEMO_SCENARIO_BAND)
    targets = {str(key): float(value) for key, value in dict(target_multiples or {}).items()}
    registered: List[str] = []
    for method in methods:
        for scenario in SCENARIO_ORDER:
            ids = assumption_ids_for(method, scenario)
            rows = [
                (ids[0], float(growth.get(scenario, 0.0)), DEMO_GROWTH_SOURCE,
                 f"{scenario} 情景下每股驱动量的相对 TTM 变化（{DRIVER_LABELS[method]}）"),
                (ids[2], float(band.get(scenario, 0.0)), DEMO_BAND_SOURCE,
                 f"{scenario} 情景下驱动量的区间带宽（±）"),
            ]
            base_target = targets.get(method)
            if base_target is not None:
                factor = SCENARIO_MULTIPLE_FACTOR[scenario]
                rows.insert(
                    1,
                    (
                        ids[1],
                        base_target * factor,
                        target_source,
                        f"{scenario} 情景下 {method} 的目标倍数 = 基准 {base_target!r} × "
                        f"情景倍数系数 {factor!r}",
                    ),
                )
            for name, value, source, reason in rows:
                if skip_existing and registry.has(name):
                    registered.append(name)
                    continue
                registry.register(name, value, source=source, reason=reason, valid_from=valid_from)
                registered.append(name)
    return registered


def build_scenarios(
    base_inputs: ValuationInputs,
    assumption_registry: AssumptionRegistry,
    *,
    calculations: Optional[object] = None,
    valid_from: str = "",
) -> List[ScenarioValuation]:
    """Build the Bear/Base/Bull scenarios for every method (card §6).

    Each scenario's driver is ``每股驱动量(TTM) × (1 + growth)`` and its interval
    is that driver ± ``band`` multiplied by the (fixed, registered) target
    multiple.  Growth, band and multiple are all read from the registry; a
    missing one raises rather than defaulting silently.

    ``calculations`` may be a CARD-03 ``CalculationRegistry``; every scenario then
    registers re-runnable calculations and carries their ids, which is the
    "公式" layer of the traceability chain.
    """
    out: List[ScenarioValuation] = []
    for method in ("PE", "PB", "PS"):
        driver = base_inputs.driver(method)  # type: ignore[arg-type]
        for scenario in SCENARIO_ORDER:
            ids = assumption_ids_for(method, scenario)  # type: ignore[arg-type]
            if not assumption_registry.has(ids[1]):
                out.append(
                    ScenarioValuation(
                        method=method, scenario=scenario, value_low=None, value_mid=None,
                        value_high=None, driver_key=DRIVER_KEYS[method], driver_mid=driver,
                        target_multiple=None, band=None,
                        formula=f"隐含价格 = {DRIVER_KEYS[method]} × (1 + 增速) × 目标{method}",
                        assumption_ids=(ids[0], ids[2]), applicable=False,
                        not_applicable_reason="missing_target_multiple",
                        note="未提供同业可比集或分析师目标倍数，不编造倍数（零猜测）。",
                    )
                )
                continue
            registry_items = assumption_registry.require(ids)
            growth = _number(registry_items[0].value)
            target = _number(registry_items[1].value)
            band = _number(registry_items[2].value)
            if driver is None or driver <= 0:
                reason = {
                    "PE": "eps_non_positive",
                    "PB": "bps_non_positive",
                    "PS": "revenue_per_share_non_positive",
                }[method]
                out.append(
                    ScenarioValuation(
                        method=method, scenario=scenario, value_low=None, value_mid=None,
                        value_high=None, driver_key=DRIVER_KEYS[method], driver_mid=driver,
                        target_multiple=target, band=band,
                        formula=f"隐含价格 = {DRIVER_KEYS[method]} × (1 + 增速) × 目标{method}",
                        assumption_ids=ids, applicable=False, not_applicable_reason=reason,
                        note=f"{DRIVER_LABELS[method]} ≤ 0，该方法整体不适用（不输出伪区间）。",
                    )
                )
                continue
            if growth is None or target is None or band is None:
                raise AssumptionError(
                    f"{method}/{scenario} 的增速、目标倍数或带宽不是数值："
                    f"{[item.value for item in registry_items]}"
                )
            driver_mid = driver * (1.0 + growth)
            item = ScenarioValuation(
                method=method, scenario=scenario,
                value_low=_finite(driver_mid * (1.0 - band) * target),
                value_mid=_finite(driver_mid * target),
                value_high=_finite(driver_mid * (1.0 + band) * target),
                driver_key=DRIVER_KEYS[method], driver_mid=_finite(driver_mid),
                target_multiple=target, band=band,
                formula=(
                    f"隐含价格 = {DRIVER_KEYS[method]}({driver!r}) × (1 + {growth!r}) "
                    f"× 目标{method}({target!r})；区间 = ±{band!r}"
                ),
                assumption_ids=ids,
                note="区间仅由已登记假设与 TTM 驱动量决定，未使用任何 LLM 生成的数字。",
            )
            if calculations is not None:
                item = replace(
                    item,
                    calculation_ids=_scenario_calculations(
                        calculations, base_inputs, item, valid_from=valid_from
                    ),
                )
            out.append(item)
    return out


def sensitivity_pe_eps(
    eps_values: Sequence[float],
    target_pe_values: Sequence[float],
    shares: float,
    *,
    assumption_ids: Sequence[str] = (),
    period_key: str = "",
) -> SensitivityMatrix:
    """EPS × target PE grid; each cell is an implied price (card §6/§7.3)."""
    return _sensitivity_grid(
        name="eps_x_target_pe", method="PE",
        driver_values=eps_values, multiple_values=target_pe_values,
        driver_name="EPS（元/股，TTM 归母）", multiple_name="目标 PE（倍）",
        shares=shares, assumption_ids=assumption_ids, period_key=period_key,
    )


def sensitivity_pb_bps(
    bps_values: Sequence[float],
    target_pb_values: Sequence[float],
    shares: float,
    *,
    assumption_ids: Sequence[str] = (),
    period_key: str = "",
) -> SensitivityMatrix:
    """BPS × target PB grid (card §7.3 option ①).

    The card's §6 sketch named this ``sensitivity_pb_roe``; §7.3 rules that out
    because ROE never enters the price formula, so the y axis carries BPS
    scenarios and the ROE → PB mapping is deferred to the final round.  The
    deviation is recorded in CARD-08 §16.
    """
    return _sensitivity_grid(
        name="bps_x_target_pb", method="PB",
        driver_values=bps_values, multiple_values=target_pb_values,
        driver_name="BPS（元/股，期末归母）", multiple_name="目标 PB（倍）",
        shares=shares, assumption_ids=assumption_ids, period_key=period_key,
    )


def _sensitivity_grid(
    *,
    name: str,
    method: str,
    driver_values: Sequence[float],
    multiple_values: Sequence[float],
    driver_name: str,
    multiple_name: str,
    shares: float,
    assumption_ids: Sequence[str],
    period_key: str,
) -> SensitivityMatrix:
    """Shared builder for both grids; keeps the two axes symmetric by construction."""
    if not _positive(shares):
        raise ValueError(SHARES_BLOCK_REASON)
    drivers = sorted({float(item) for item in driver_values})
    multiples = sorted({float(item) for item in multiple_values})
    if not drivers or not multiples:
        raise ValueError("敏感性矩阵的坐标轴不能为空。")
    if any(not isfinite(item) or item <= 0 for item in drivers + multiples):
        raise ValueError("敏感性矩阵的坐标轴必须是有限正数。")
    matrix = tuple(
        tuple(_finite(driver * multiple) for multiple in multiples) for driver in drivers
    )
    if any(any(cell is None for cell in row) for row in matrix):  # pragma: no cover - defensive
        raise ValueError("敏感性矩阵出现非有限值，已拒绝。")
    return SensitivityMatrix(
        name=name, x_name=multiple_name, y_name=driver_name,
        x_values=tuple(multiples), y_values=tuple(drivers),
        matrix=matrix,  # type: ignore[arg-type]
        row_labels=tuple(f"{driver_name}={item!r}" for item in drivers),
        column_labels=tuple(f"{multiple_name}={item!r}" for item in multiples),
        formula=f"隐含价格 = {driver_name} × {multiple_name}（{method}）",
        shares_used=float(shares),
        assumption_ids=tuple(str(item) for item in assumption_ids),
        note=(
            "格子的 y 轴是每股驱动量情景，x 轴是目标倍数情景；"
            f"{method} 每股价格由此二轴相乘得到，shares 仅用于校验每股口径是否成立"
            "（单元格本身已是每股金额）。"
        ),
    )


def method_dispersion(scenarios: Sequence[ScenarioValuation]) -> Dict[str, object]:
    """Dispersion of each method's scenario mids, plus cross-method spread.

    Card §7.4: methods are shown separately and the report must exist *without*
    manufacturing a "final range" — so this returns per-method statistics and an
    explicit note, never a single blended interval.
    """
    report: Dict[str, object] = {}
    base_mids: Dict[str, float] = {}
    for method in ("PE", "PB", "PS"):
        rows = [item for item in scenarios if item.method == method]
        applicable = [item for item in rows if item.applicable and item.value_mid is not None]
        mids = {item.scenario: item.value_mid for item in applicable}
        entry: Dict[str, object] = {
            "applicable": bool(applicable),
            "count": len(applicable),
            "scenario_mids": mids,
            "inapplicable_reasons": sorted(
                {str(item.not_applicable_reason) for item in rows if not item.applicable}
            ),
        }
        if applicable:
            values = [float(item.value_mid) for item in applicable]  # type: ignore[arg-type]
            entry.update(_spread(values))
            base = next((item.value_mid for item in applicable if item.scenario == SCENARIO_BASE), None)
            if base is not None:
                base_mids[method] = float(base)
        report[method] = entry

    if base_mids:
        report["across_methods"] = {
            "base_scenario_mids": dict(base_mids),
            **_spread(list(base_mids.values())),
        }
    else:
        report["across_methods"] = {"base_scenario_mids": {}, "count": 0}
    report["note"] = DISPERSION_NOTE
    return report


def build_valuation(
    inputs: ValuationInputs,
    registry: AssumptionRegistry,
    *,
    target_multiples: Optional[Mapping[str, float]] = None,
    target_source: str = ANALYST_SOURCE,
    register_defaults: bool = True,
    calculations: Optional[object] = None,
    valid_from: str = "",
    warnings: Sequence[str] = (),
    extra_assumption_ids: Sequence[str] = (),
) -> ValuationResult:
    """Compose the full CARD-08 result: scenarios → sensitivity → dispersion.

    ``calculations`` may be a CARD-03 :class:`CalculationRegistry`; every scenario
    then registers re-derivable calculations, which is how the "公式" layer stays
    checkable (the registry's ``validate()`` re-runs them).
    """
    if register_defaults:
        register_scenario_assumptions(
            registry, target_multiples=target_multiples, target_source=target_source,
            valid_from=valid_from,
        )
    all_warnings = list(inputs.warnings) + list(warnings)
    calculation_ids: List[str] = []
    if calculations is not None:
        calculation_ids.extend(
            _register_driver_calculations(calculations, inputs, valid_from=valid_from)
        )
    scenarios = build_scenarios(
        inputs, registry, calculations=calculations, valid_from=valid_from
    )
    for item in scenarios:
        calculation_ids.extend(item.calculation_ids)

    grids: List[SensitivityMatrix] = []
    for method, builder in (("PE", sensitivity_pe_eps), ("PB", sensitivity_pb_bps)):
        rows = [item for item in scenarios if item.method == method and item.applicable]
        if not rows:
            continue
        ids: List[str] = []
        for item in rows:
            ids.extend(item.assumption_ids)
        ids.extend(str(item) for item in extra_assumption_ids)
        grids.append(
            builder(
                [float(item.driver_mid) for item in rows],  # type: ignore[arg-type]
                [float(item.target_multiple) for item in rows],  # type: ignore[arg-type]
                inputs.total_shares,
                assumption_ids=ids,
                period_key=inputs.period_key,
            )
        )
    # The PS layer intentionally exposes no second axis: the card asks for the
    # PE (EPS) and PB (BPS) grids only, and inventing a PS grid would add an axis
    # nobody registered an assumption for.
    dispersion = method_dispersion(scenarios)

    applicable = [item for item in scenarios if item.applicable]
    if not applicable:
        status = "blocked"
        blocked_reason = _blocked_reason(inputs, scenarios)
    elif registry.all_assumption_grade:
        status = "assumption_only"
        blocked_reason = None
        all_warnings.append(
            "pure_assumption：全部假设都没有 sourced 依据，本次结果整体降级为『纯假设演示』（§9）。"
        )
    elif any(not item.applicable for item in scenarios):
        status = "partial"
        blocked_reason = None
    else:
        status = "ok"
        blocked_reason = None

    note = _applicability_note(scenarios, inputs)
    return ValuationResult(
        status=status,
        inputs=inputs.to_dict(),
        scenarios=tuple(scenarios),
        sensitivity=tuple(grids),
        method_dispersion=dispersion,
        applicability_note=note,
        assumptions=tuple(registry.items()),
        warnings=tuple(all_warnings),
        calculation_ids=tuple(calculation_ids),
        blocked_reason=blocked_reason,
    )


def _applicability_note(
    scenarios: Sequence[ScenarioValuation], inputs: ValuationInputs
) -> str:
    """The deterministic method-boundary template (card §5 ``applicability_note``)."""
    methods = [method for method in ("PE", "PB", "PS") if any(item.method == method for item in scenarios)]
    parts = [APPLICABILITY_NOTES[method] for method in methods]
    parts.append(DISPERSION_NOTE)
    if inputs.roe_ttm_pct is not None:
        parts.append(
            f"参考：ROE(TTM) ≈ {inputs.roe_ttm_pct:.2f}%，"
            f"按（期初 {inputs.equity_parent_begin!r} + 期末 {inputs.equity_parent_end!r}）/2 近似，"
            "该近似口径已显式标注，未参与任何价格公式。"
        )
    else:
        parts.append("参考：ROE 不可近似（缺期初或期末归母权益），故不参与估值。")
    return " ".join(parts)


def _blocked_reason(inputs: ValuationInputs, scenarios: Sequence[ScenarioValuation]) -> str:
    reasons = {str(item.not_applicable_reason) for item in scenarios}
    if reasons == {"missing_target_multiple"}:
        return "missing_target_multiple"
    if inputs.driver("PE") is None or inputs.driver("PB") is None:
        return "missing_ttm_drivers"
    return "no_applicable_method"


# -- calculation registration (the 公式 layer) ------------------------------

def _register_driver_calculations(
    calculations: object,
    inputs: ValuationInputs,
    *,
    valid_from: str,
) -> List[str]:
    """Record how each per-share driver was obtained (divide by 期末总股本).

    The operands are formula constants, not evidence operands, and that is
    deliberate: the numerator is itself a *derived* value (CARD-02's TTM output),
    so the evidence chain does not stop here — it continues through
    ``inputs.evidence_all`` / ``inputs.formulas``, which name the source periods
    and engine.  Recording a fact id against a TTM aggregate would point at a
    number the fact does not contain.
    """
    from .analysis.calculations import constant_operand

    period = inputs.period_key or "TTM"
    ids: List[str] = []
    for method, numerator, parameter in (
        ("PE", inputs.net_profit_parent_ttm, "net_profit_parent_ttm"),
        ("PB", inputs.equity_parent_end, "equity_parent_end"),
        ("PS", inputs.revenue_ttm, "revenue_ttm"),
    ):
        if numerator is None:
            continue
        record = calculations.register(  # type: ignore[attr-defined]
            metric=DRIVER_KEYS[method],
            formula=f"{DRIVER_KEYS[method]} = {parameter} / 期末总股本",
            operation="divide",
            operands=[
                constant_operand(float(numerator), parameter),
                constant_operand(float(inputs.total_shares), "total_shares"),
            ],
            value=float(numerator) / float(inputs.total_shares),
            unit="元/股",
            period_key=period,
            period_basis="TTM",
            source="derived",
            engine="src.cn.valuation（CARD-08）",
            note=(
                f"分子取得方式：{inputs.formulas.get(parameter, '未声明')}；"
                f"分母为披露的期末总股本；假设时点 {valid_from or '未声明'}。"
            ),
            key_hint=method.lower(),
        )
        ids.append(record.calculation_id)
    return ids


def _scenario_calculations(
    calculations: object,
    inputs: ValuationInputs,
    item: ScenarioValuation,
    *,
    valid_from: str,
) -> Tuple[str, ...]:
    """Record one scenario as re-runnable calculations (the "公式" layer).

    Four records per cell: the scenario driver, then its low / mid / high implied
    prices.  ``CalculationRegistry.validate()`` re-runs every one of them, so a
    scenario number that cannot be reproduced shows up as a defect rather than a
    quietly wrong cell.
    """
    from .analysis.calculations import constant_operand

    if not item.applicable or item.value_mid is None:
        return ()
    driver = inputs.driver(item.method)  # type: ignore[arg-type]
    if driver is None or item.target_multiple is None or item.band is None or item.driver_mid is None:
        return ()
    period = inputs.period_key or "TTM"
    growth = (float(item.driver_mid) / float(driver)) - 1.0
    base = calculations.register(  # type: ignore[attr-defined]
        metric=f"{item.method.lower()}_scenario_driver_{item.scenario}",
        formula=f"{DRIVER_KEYS[item.method]} × (1 + 增速 {growth!r})",
        operation="multiply",
        operands=[
            constant_operand(float(driver), DRIVER_KEYS[item.method]),
            constant_operand(1.0 + growth, "1 + 增速"),
        ],
        value=float(item.driver_mid),
        unit="元/股",
        period_key=period,
        period_basis="TTM",
        source="derived",
        engine="src.cn.valuation（CARD-08）",
        note=f"{item.scenario} 情景驱动量；假设 {'、'.join(item.assumption_ids)}。",
        key_hint=f"{item.method.lower()}_{item.scenario}",
    )
    ids: List[str] = [base.calculation_id]
    for label, value, factor in (
        ("low", item.value_low, 1.0 - item.band),
        ("mid", item.value_mid, 1.0),
        ("high", item.value_high, 1.0 + item.band),
    ):
        if value is None:
            continue
        record = calculations.register(  # type: ignore[attr-defined]
            metric=f"{item.method.lower()}_scenario_{item.scenario}_{label}",
            formula=(
                f"隐含价格 = {DRIVER_KEYS[item.method]}_scenario × "
                f"目标{item.method}({item.target_multiple!r}) × {factor!r}"
            ),
            operation="multiply",
            operands=[
                constant_operand(float(item.driver_mid), f"{DRIVER_KEYS[item.method]}_scenario"),
                constant_operand(
                    float(item.target_multiple) * factor, f"目标{item.method}×{factor!r}"
                ),
            ],
            value=float(value),
            unit="元/股",
            period_key=period,
            period_basis="TTM",
            source="derived",
            engine="src.cn.valuation（CARD-08）",
            note=f"{item.method}/{item.scenario}/{label}；登记于 {valid_from or '未声明时点'}。",
            key_hint=f"{item.method.lower()}_{item.scenario}_{label}",
        )
        ids.append(record.calculation_id)
    return tuple(ids)


# -- small numeric helpers --------------------------------------------------

_PERIOD_KINDS = ("Q1", "H1", "9M", "FY")


def _period_sort_key(fiscal_period: object) -> Tuple[int, int, str]:
    """Order ``YYYYQ1 < YYYYH1 < YYYY9M < YYYYFY``; unknown keys sort first."""
    text = str(fiscal_period or "")
    for index, kind in enumerate(_PERIOD_KINDS):
        if text.endswith(kind):
            year = text[: -len(kind)]
            if year.isdigit():
                return (int(year), index, text)
    return (0, 0, text)


def _finite(value: object) -> Optional[float]:
    """``None``/NaN/±inf → ``None``: a non-finite number is not a value."""
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _number(value: object) -> Optional[float]:
    return _finite(value)


def _positive(value: object) -> bool:
    number = _finite(value)
    return number is not None and number > 0


def _per_share(total: Optional[float], shares: float) -> Optional[float]:
    if total is None or not _positive(shares):
        return None
    return _finite(float(total) / float(shares))


def _spread(values: Sequence[float]) -> Dict[str, object]:
    """min/max/mean/range for a set of point estimates (no synthetic interval)."""
    if not values:
        return {"count": 0}
    low, high = min(values), max(values)
    mean = sum(values) / len(values)
    return {
        "count": len(values),
        "min": low,
        "max": high,
        "mean": mean,
        "range": high - low,
        "range_pct_of_mean": ((high - low) / mean * 100.0) if mean else None,
    }
