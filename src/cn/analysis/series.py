"""Period series built from CARD-02 output.

Analysis needs *comparable* periods, not a flat list of facts. This module
indexes CARD-02's two products:

* disclosed values — ``NormalizedFact`` (canonical metric keys);
* engine-derived values — ``DerivedPeriod`` (single quarters and TTM), whose
  ``values`` dict is keyed by the **statement value key**, not the canonical
  metric key (``net_profit_parent`` lives under ``net_profit`` — the repo's
  historical key for 归母净利润). ``statement_value_key()`` from CARD-02 performs
  that projection so nothing here hard-codes the mapping.

Comparisons only ever happen inside one ``(metric, scope, version, currency)``
group (§8 rule 5 — cross-scope pairing is refused by construction, never
"handled" by a fallback), and a missing prior period is a break in the series
(§8 rule 6) that yields "not computable" instead of a silent gap-jump.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..normalization import NormalizedFact, PeriodKind, Version, version_rank
from ..normalization.metrics import statement_value_key

BASIS_Q1 = "Q1"
BASIS_H1 = "H1"
BASIS_9M = "9M"
BASIS_FY = "FY"
BASIS_TTM = "TTM"
BASIS_SINGLE_QUARTER = "SINGLE_QUARTER"
BASIS_POINT_IN_TIME = "POINT_IN_TIME"

CUMULATIVE_BASES = (BASIS_Q1, BASIS_H1, BASIS_9M, BASIS_FY)

#: Series key slot used by :func:`merge_versions` — its presence means "the
#: version dimension was deliberately collapsed", never "the filing declared no
#: version". Downstream group selection must therefore not filter on it.
MERGED_VERSION = "__merged__"

_QUARTERS = ("Q1", "Q2", "Q3", "Q4")


def version_rank_of(token: Optional[str]) -> int:
    """Rank a version *token*; an unknown/absent token ranks as oldest.

    Merging compares tokens rather than enum members because an engine-derived
    value has no disclosure version of its own and carries ``""``.
    """
    try:
        return version_rank(Version(str(token)))
    except ValueError:
        return 0


def newest_version(tokens: Iterable[Optional[str]]) -> str:
    """The most recent version among several disclosures of one figure."""
    cleaned = [str(token) for token in tokens if token]
    if not cleaned:
        return ""
    return max(cleaned, key=version_rank_of)


@dataclass(frozen=True)
class PeriodValue:
    """One metric value anchored to one period."""

    period_key: str
    basis: str
    value: float
    normalized_fact_id: str = ""       # evidence ID when disclosed
    calculation_id: str = ""           # calculation ID when engine-derived
    source_periods: Tuple[str, ...] = ()
    origin: str = "disclosed"          # disclosed | derived
    raw_value: str = ""
    source_table: str = ""
    #: Disclosure version of the fact this value came from. Carried (rather than
    #: looked up later) so a comparison's operand trail can say *which* version
    #: each end rests on — CARD-04 §10 rule 3 is exactly that question.
    version: str = ""

    @property
    def evidence_id(self) -> str:
        return self.normalized_fact_id or self.calculation_id

    def to_dict(self) -> Dict[str, object]:
        return {
            "period_key": self.period_key,
            "basis": self.basis,
            "value": self.value,
            "normalized_fact_id": self.normalized_fact_id,
            "calculation_id": self.calculation_id,
            "source_periods": list(self.source_periods),
            "origin": self.origin,
            "version": self.version,
        }


@dataclass
class MetricSeries:
    """All periods of one metric inside one scope/version/currency group."""

    metric: str
    scope: Optional[str]
    version: str
    currency: str
    values: Dict[Tuple[str, str], PeriodValue] = field(default_factory=dict)

    def put(self, item: PeriodValue) -> None:
        key = (item.basis, item.period_key)
        existing = self.values.get(key)
        if existing is None or _prefer(item, existing):
            self.values[key] = item

    def get(self, basis: str, period_key: str) -> Optional[PeriodValue]:
        return self.values.get((basis, period_key))

    def periods(self, basis: str) -> List[str]:
        return sorted(key for bs, key in self.values if bs == basis)

    def has_any(self) -> bool:
        return bool(self.values)

    def to_dict(self) -> Dict[str, object]:
        return {
            "metric": self.metric,
            "scope": self.scope,
            "version": self.version,
            "currency": self.currency,
            "values": [item.to_dict() for _, item in sorted(self.values.items())],
        }


def _prefer(candidate: PeriodValue, existing: PeriodValue) -> bool:
    """Newest version wins; then disclosed over derived; then stable tie-break.

    Version first is the supersession rule: when the same period of the same
    metric was disclosed twice (调整前 / 调整后), the later disclosure is the
    effective value. Only :func:`merge_versions` ever produces that contest, so
    a normal run — where both candidates sit in one version group — behaves
    exactly as before.
    """
    candidate_rank = version_rank_of(candidate.version)
    existing_rank = version_rank_of(existing.version)
    if candidate_rank != existing_rank:
        return candidate_rank > existing_rank
    if candidate.origin != existing.origin:
        return candidate.origin == "disclosed"
    return candidate.period_key < existing.period_key


# -- period-key arithmetic ---------------------------------------------------


def split_period_key(period_key: str) -> Tuple[int, str]:
    """``2024H1`` → ``(2024, "H1")``; ``2024Q2`` → ``(2024, "Q2")``."""
    text = str(period_key or "")
    year, _, suffix = text[:4], text[4:5], text[4:]
    if len(text) < 5 or not year.isdigit():
        raise ValueError(f"无法解析期间键 '{period_key}'，拒绝猜测。")
    return int(year), suffix


def previous_year_key(period_key: str) -> str:
    """Same period one year earlier (``2024H1`` → ``2023H1``)."""
    year, kind = split_period_key(period_key)
    return f"{year - 1}{kind}"


def previous_quarter_key(period_key: str) -> Optional[str]:
    """The quarter before (``2024Q2`` → ``2024Q1``, ``2024Q1`` → ``2023Q4``)."""
    year, kind = split_period_key(period_key)
    if kind not in _QUARTERS:
        return None
    index = _QUARTERS.index(kind)
    if index == 0:
        return f"{year - 1}Q4"
    return f"{year}{_QUARTERS[index - 1]}"


def basis_for_fact(fact: NormalizedFact) -> Optional[str]:
    """Map a fact's ``period_kind`` + key onto its comparison basis."""
    if fact.period_kind is PeriodKind.POINT_IN_TIME:
        return BASIS_POINT_IN_TIME
    if fact.period_kind is PeriodKind.SINGLE_QUARTER:
        return BASIS_SINGLE_QUARTER
    if fact.period_kind is PeriodKind.TTM:
        return BASIS_TTM
    _, suffix = split_period_key(fact.period_key)
    return suffix if suffix in CUMULATIVE_BASES else None


# -- series construction -----------------------------------------------------


def build_series(
    facts: Iterable[NormalizedFact],
    *,
    single_quarters: Sequence[object] = (),
    ttm: Sequence[object] = (),
    warnings: Optional[List[str]] = None,
) -> Dict[Tuple[str, str, str], MetricSeries]:
    """Index every disclosed and engine-derived value into metric series.

    Keyed by ``(metric, scope_token, version)`` so a 合并 series can never mix
    with a 母公司 one.
    """
    series: Dict[Tuple[str, str, str], MetricSeries] = {}

    def bucket(metric: str, scope: Optional[str], version: str, currency: str) -> MetricSeries:
        key = (metric, _scope_token(scope), version)
        found = series.get(key)
        if found is None:
            found = MetricSeries(metric=metric, scope=scope, version=version, currency=currency)
            series[key] = found
        return found

    # 1) disclosed values
    for fact in facts:
        basis = basis_for_fact(fact)
        if basis is None:
            _warn(warnings, f"指标 {fact.metric}（{fact.period_key}）的期间语义无法映射到分析基期，已跳过。")
            continue
        if fact.period_kind is None:
            _warn(warnings, f"指标 {fact.metric}（{fact.period_key}）period_kind 不可判定，分析已跳过该值。")
            continue
        bucket(
            fact.metric, fact.scope, fact.version.value, fact.currency
        ).put(
            PeriodValue(
                period_key=fact.period_key,
                basis=basis,
                value=float(fact.value),
                normalized_fact_id=fact.normalized_fact_id,
                origin="disclosed",
                raw_value=str(fact.raw_value or ""),
                source_table=str(fact.source_table or ""),
                version=fact.version.value,
            )
        )

    # 2) engine-derived single quarters / TTM (keyed by statement value key)
    for metric in _derived_metrics(facts):
        value_key = statement_value_key(metric)
        if value_key is None:
            continue
        for record in single_quarters:
            _put_derived(series, bucket, metric, value_key, record, BASIS_SINGLE_QUARTER)
        for record in ttm:
            _put_derived(series, bucket, metric, value_key, record, BASIS_TTM)

    return series


def _derived_metrics(facts: Iterable[NormalizedFact]) -> List[str]:
    """Canonical metrics for which the engine may have produced derived periods."""
    seen: List[str] = []
    for fact in facts:
        if fact.metric not in seen:
            seen.append(fact.metric)
    return seen


def _put_derived(series, bucket, metric, value_key, record, basis) -> None:
    values = getattr(record, "values", None)
    if not isinstance(values, dict):
        return
    value = values.get(value_key)
    if value is None:
        return
    period_key = str(getattr(record, "fiscal_period", "") or "")
    if not period_key:
        return
    statement_type = str(getattr(record, "statement_type", "") or "")
    scope = _default_scope_for(series, metric)
    version = _default_version_for(series, metric)
    currency = _default_currency_for(series, metric)
    bucket(metric, scope, version, currency).put(
        PeriodValue(
            period_key=period_key,
            basis=basis,
            value=float(value),
            calculation_id=f"calc_engine_{statement_type}_{metric}_{basis.lower()}_{period_key}",
            source_periods=tuple(getattr(record, "source_periods", ()) or ()),
            origin="derived",
            source_table=f"FinancialPeriodEngine:{statement_type}",
            version=version,
        )
    )


def _matching_series(series, metric) -> List[MetricSeries]:
    return [item for key, item in series.items() if key[0] == metric]


def _default_scope_for(series, metric) -> Optional[str]:
    found = _matching_series(series, metric)
    for item in found:
        if item.scope == "合并":
            return item.scope
    return found[0].scope if found else None


def _default_version_for(series, metric) -> str:
    found = _matching_series(series, metric)
    return found[0].version if found else "AS_REPORTED"


def _default_currency_for(series, metric) -> str:
    found = _matching_series(series, metric)
    return found[0].currency if found else "CNY"


def merge_versions(
    series: Dict[Tuple[str, str, str], MetricSeries],
    *,
    warnings: Optional[List[str]] = None,
) -> Dict[Tuple[str, str, str], MetricSeries]:
    """Collapse the version dimension, newest disclosure winning per period.

    **Call this only when CARD-04 decided ``use_adjusted``.** The default rule
    (CARD-02 §7.3 rule 4 / §8 rule 5) is that two versions of the same figure may
    never be paired, and :func:`build_series` enforces it by keying series on
    ``version``. That rule is about *one figure disclosed twice* — it is not a
    statement that 2025 on the new basis cannot be compared with 2024 restated to
    that same basis. A restatement that reprints the comparative (上期金额（已重述）)
    is exactly the case where CARD-04 says "use the adjusted data", and no single
    version group holds both ends, so the comparison simply vanished:

    * current period → unmarked column → ``AS_REPORTED``
    * comparative   → 已重述 column     → ``RESTATED``
    * ⇒ no group had both ends ⇒ ``缺少上一可比期间``, and the ``use_adjusted``
      decision had no effect on any number.

    Merging is therefore a **decision**, taken elsewhere and recorded here: each
    (metric, scope) becomes one series in which every period keeps its newest
    disclosure, so both ends are on the newest basis the filing provides. Series
    keys carry :data:`MERGED_VERSION` so a caller cannot mistake the merged view
    for a declared version.

    Returns the merged series; ``warnings`` gains a line naming the versions that
    were folded together.
    """
    merged: Dict[Tuple[str, str, str], MetricSeries] = {}
    folded: List[str] = []
    superseded: List[str] = []

    for key in sorted(series.keys()):
        item = series[key]
        merged_key = (key[0], key[1], MERGED_VERSION)
        found = merged.get(merged_key)
        if found is None:
            found = MetricSeries(
                metric=item.metric, scope=item.scope, version=MERGED_VERSION, currency=item.currency
            )
            merged[merged_key] = found
        for value in item.values.values():
            previous = found.values.get((value.basis, value.period_key))
            if (
                previous is not None
                and previous.version
                and value.version
                and previous.version != value.version
            ):
                newest = newest_version([previous.version, value.version])
                superseded.append(
                    f"{item.metric} {value.period_key}（{previous.version} → {newest}）"
                )
            found.put(value)

    for item in merged.values():
        item.version = newest_version(value.version for value in item.values.values())
        if item.version:
            folded.append(item.version)

    if warnings is not None and merged:
        _warn(
            warnings,
            "CARD-04 判定为 use_adjusted：同一指标跨披露版本取值，"
            f"每个期间取最新披露版本（合并后版本：{'、'.join(sorted(set(folded)))}），"
            "未在同一期间对同一指标的两个版本取平均或并列。",
        )
        if superseded:
            _warn(
                warnings,
                "以下同期同指标存在两个披露版本，已取最新版本："
                + "、".join(sorted(set(superseded)))
                + "。",
            )
    return merged


def primary_series_key(series: Dict[Tuple[str, str, str], MetricSeries]) -> Optional[Tuple[str, str, str]]:
    """Pick the group the report should speak about (合并 > 母公司; then 稳定序).

    A document carrying several scopes does not get silently averaged — one
    group is chosen and the choice is reported.
    """
    if not series:
        return None
    keys = sorted(series.keys(), key=lambda item: (_scope_rank(series[item].scope), item[0], item[1]))
    return keys[0]


def _scope_rank(scope: Optional[str]) -> int:
    if scope == "合并":
        return 0
    if scope == "母公司":
        return 1
    return 2


def _scope_token(scope: Optional[str]) -> str:
    return scope or "_"


def _warn(warnings: Optional[List[str]], message: str) -> None:
    if warnings is not None and message not in warnings:
        warnings.append(message)


__all__ = [
    "BASIS_9M",
    "BASIS_FY",
    "BASIS_H1",
    "BASIS_POINT_IN_TIME",
    "BASIS_Q1",
    "BASIS_SINGLE_QUARTER",
    "BASIS_TTM",
    "CUMULATIVE_BASES",
    "MERGED_VERSION",
    "MetricSeries",
    "PeriodValue",
    "basis_for_fact",
    "build_series",
    "merge_versions",
    "newest_version",
    "previous_quarter_key",
    "previous_year_key",
    "primary_series_key",
    "split_period_key",
    "version_rank_of",
]
