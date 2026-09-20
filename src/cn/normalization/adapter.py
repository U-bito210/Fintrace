"""Bridge: ``NormalizedFact`` -> existing ``FinancialStatement`` -> period engine.

The whole point of this module is to **not** reimplement period maths. Single
quarters and TTM come from :class:`~src.cn.periods.FinancialPeriodEngine`
(``derive_single_quarters`` / ``derive_ttm``); this adapter only validates and
projects, exactly as CARD-02 §6/§10 require:

* ``PeriodEngineError`` is never swallowed — a stock value fed to the flow engine
  is a programming error, and the engine's guard must reach the caller;
* a group that mixes 合并/母公司 (or two disclosure versions) is rejected with a
  clear message instead of being silently differenced;
* the projection is deterministic: the same fact set always yields the same
  statements, the same ids and the same group order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Iterable, Optional, Sequence, Tuple

from ..domain import FinancialStatement
from ..periods import DerivedPeriod, FinancialPeriodEngine, PeriodEngineError
from .metrics import METRIC_INDEX, MetricSemantics, statement_value_key
from .models import BASE_UNIT, NormalizedFact, PeriodKind, Scope, Version
from .periods import period_token, period_year
from .scope import scope_token
from .warning_log import WarningLog

_FLOW_STATEMENT_TYPES = ("income", "cashflow")
_STATEMENT_TYPES = ("income", "balance", "cashflow")

#: Marker used by a derived group whose records span every disclosed period
#: (the engine needs the whole cumulative series to difference a quarter).
ALL_PERIODS = "*"

# Cross-source priority when two disclosures of the same metric+period disagree.
# The audited statement body is the primary source; an extract table repeating
# the same figure is secondary; an unidentified table is last.
_RANK_STATEMENT_BODY = 0
_RANK_SUMMARY = 1
_RANK_UNKNOWN = 2

_SUMMARY_TABLE_MARKERS = ("主要会计数据", "主要财务数据", "主要财务指标")
_BODY_TABLES = (
    "合并资产负债表", "母公司资产负债表", "资产负债表",
    "合并利润表", "母公司利润表", "利润表", "合并利润及利润分配表", "利润及利润分配表",
    "合并现金流量表", "母公司现金流量表", "现金流量表",
)

_REL_TOLERANCE = 1e-9
_ABS_TOLERANCE = 1e-6


@dataclass(frozen=True)
class GroupKey:
    """Identity of one projectable statement: type + period + 口径 + 版本 + 币种."""

    statement_type: str
    fiscal_period: str
    scope: Optional[Scope]
    version: Version
    currency: str

    def describe(self) -> str:
        return (
            f"{self.statement_type}/{self.fiscal_period}/{scope_token(self.scope)}"
            f"/{self.version.value}/{self.currency}"
        )

    @property
    def group_id(self) -> str:
        return (
            f"stmt_{scope_token(self.scope)}_{self.statement_type}_"
            f"{self.fiscal_period}_{self.version.value}"
        )


@dataclass
class ProjectionResult:
    """Statements plus the bookkeeping needed to keep every id traceable."""

    statements: List[FinancialStatement]
    groups: Dict[GroupKey, List[FinancialStatement]]
    assignments: Dict[str, str]          # normalized_fact_id -> statement id
    warnings: List[str]


@dataclass(frozen=True)
class DerivedGroup:
    """Single-quarter and TTM periods derived for one homogeneous group."""

    group: GroupKey
    single_quarters: Tuple[DerivedPeriod, ...]
    ttm: Tuple[DerivedPeriod, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "group": self.group.describe(),
            "single_quarters": [_derived_to_dict(p) for p in self.single_quarters],
            "ttm": [_derived_to_dict(p) for p in self.ttm],
        }


def _derived_to_dict(period: DerivedPeriod) -> Dict[str, object]:
    return {
        "statement_type": period.statement_type,
        "fiscal_period": period.fiscal_period,
        "period_basis": period.period_basis,
        "values": dict(period.values),
        "source_periods": list(period.source_periods),
    }


def period_basis_for(period_key: str, period_kind: Optional[PeriodKind]) -> str:
    """Match the provider convention: ``2025FY_CUMULATIVE`` / ``2025Q1_END``."""
    token = period_token(period_key)
    suffix = "_END" if period_kind is PeriodKind.POINT_IN_TIME else "_CUMULATIVE"
    return f"{period_year(period_key)}{token}{suffix}"


def _values_differ(left: float, right: float) -> bool:
    if left == right:
        return False
    return abs(left - right) > max(_ABS_TOLERANCE, _REL_TOLERANCE * max(abs(left), abs(right)))


def _source_rank(fact: NormalizedFact) -> int:
    table = str(fact.source_table or "")
    if table in _BODY_TABLES:
        return _RANK_STATEMENT_BODY
    if any(marker in table for marker in _SUMMARY_TABLE_MARKERS):
        return _RANK_SUMMARY
    return _RANK_UNKNOWN


def primary_sort_key(fact: NormalizedFact):
    """Deterministic priority among competing disclosures of one metric+period.

    Order of precedence: audited statement body -> summary table -> unidentified
    table, then document order (page, row, column). The same key is used by the
    projection and by the derived-metric pass so a derived figure can never
    contradict the operands that were actually projected.
    """
    return (_source_rank(fact), fact.page or 0, fact.row or 0, fact.column or 0)


def pick_primary(candidates: Sequence[NormalizedFact]) -> NormalizedFact:
    """The single disclosure that wins for one value key (never a random one)."""
    return sorted(candidates, key=primary_sort_key)[0]


def _cell_ref(fact: NormalizedFact) -> str:
    """Human-readable cell reference for conflict messages and the jump UI."""
    table = fact.source_table or "未知表"
    location = f"第{fact.page}页" if fact.page is not None else "页未知"
    if fact.row is not None:
        location = f"{location} r{fact.row}"
        if fact.column is not None:
            location = f"{location}c{fact.column}"
    return f"{table} {location}"


def _statement_type_for(fact: NormalizedFact, log: WarningLog) -> Optional[str]:
    """Prefer the §7.1 dictionary's statement; flag a table/metric mismatch."""
    spec = METRIC_INDEX.get(fact.metric)
    if spec is None or spec.semantics is MetricSemantics.MARKET:
        return None
    declared = fact.statement_type
    if declared in _STATEMENT_TYPES and declared != spec.statement:
        log.add(
            f"指标 {fact.metric} 出现在 {declared} 表却被归入 {spec.statement} 类别"
            f"（文档 {fact.document_id} 第 {fact.page} 页），按 §7.1 指标定义归属。"
        )
    return spec.statement


def project_statements(
    normalized_facts: Iterable[NormalizedFact],
    *,
    warnings: Optional[List[str]] = None,
) -> ProjectionResult:
    """Project facts onto ``FinancialStatement`` records, one per homogeneous group."""
    log = WarningLog()
    log.extend(warnings or [])

    buckets: Dict[GroupKey, Dict[str, List[NormalizedFact]]] = {}
    assignments: Dict[str, str] = {}

    for fact in normalized_facts:
        statement_type = _statement_type_for(fact, log)
        if statement_type is None:
            if fact.metric in METRIC_INDEX:
                log.tally("market", "行情/同业输入不入财务报表", fact.metric)
            else:
                log.tally("unknown-metric", "未知 metric 不入财务报表（下游不得引用）", fact.metric)
            continue
        if fact.period_kind is None:
            log.tally("no-kind", "无法判定期间语义，未投影到财务报表", fact.metric)
            continue
        if fact.period_kind is PeriodKind.SINGLE_QUARTER:
            log.tally(
                "disclosed-single",
                "披露的单季值不投影：单季唯一来源是引擎差分（此处仅用于交叉校验）",
                fact.metric,
            )
            continue
        if fact.period_kind is PeriodKind.TTM:
            log.tally("disclosed-ttm", "披露的 TTM 值不投影：TTM 唯一来源是引擎推导", fact.metric)
            continue

        value_key = statement_value_key(fact.metric) or fact.metric
        key = GroupKey(statement_type, fact.period_key, fact.scope, fact.version, fact.currency)
        buckets.setdefault(key, {}).setdefault(value_key, []).append(fact)

    statements: List[FinancialStatement] = []
    groups: Dict[GroupKey, List[FinancialStatement]] = {}

    for key in sorted(buckets, key=lambda item: item.describe()):
        metric_cells = buckets[key]
        values: Dict[str, Optional[float]] = {}
        used: List[NormalizedFact] = []
        for value_key in sorted(metric_cells):
            candidates = sorted(metric_cells[value_key], key=primary_sort_key)
            chosen, *rest = candidates
            for other in rest:
                if _values_differ(chosen.value, other.value):
                    log.add(
                        f"同期间同指标跨来源数值不一致（{key.describe()} / {value_key}）："
                        f"采用 {chosen.raw_value}（{_cell_ref(chosen)}），"
                        f"未采用 {other.raw_value}（{_cell_ref(other)}），冲突需 CARD-13 裁决。"
                    )
            values[value_key] = chosen.value
            used.append(chosen)

        kind = used[0].period_kind if used else None
        available = sorted(f.available_at for f in used if f.available_at)
        statement = FinancialStatement(
            statement_type=key.statement_type,
            fiscal_period=key.fiscal_period,
            period_basis=period_basis_for(key.fiscal_period, kind),
            published_at=available[0] if available else None,
            available_at=available[-1] if available else None,
            currency=key.currency,
            unit=BASE_UNIT,
            values=values,
            source_url=None,
            is_restated=key.version is Version.RESTATED,
        )
        statements.append(statement)
        groups.setdefault(key, []).append(statement)
        for fact in used:
            assignments[fact.normalized_fact_id] = key.group_id

    return ProjectionResult(
        statements=statements,
        groups=groups,
        assignments=assignments,
        warnings=log.as_list(),
    )


def facts_to_financial_statements(
    normalized_facts: Iterable[NormalizedFact],
    *,
    warnings: Optional[List[str]] = None,
) -> List[FinancialStatement]:
    """CARD-02 §6 signature.

    A single fact that cannot be projected never aborts the batch: it is skipped
    and counted in ``warnings`` (which the caller passes in, matching the card's
    ``(NormalizedFact[], warnings[])`` pipeline contract).
    """
    return project_statements(normalized_facts, warnings=warnings).statements


def assert_homogeneous_group(
    statements: Sequence[FinancialStatement], statement_type: str
) -> List[FinancialStatement]:
    """Reject a mixed 口径/版本 group before the engine sees it.

    ``FinancialStatement`` carries no explicit scope field (the provider
    contract is consolidated-only), so a mixed group shows up as two records for
    one fiscal period. The engine raises ``Duplicate ... record`` for that case;
    this pre-check turns it into an actionable message instead.
    """
    records = [record for record in statements if record.statement_type == statement_type]
    seen: Dict[str, FinancialStatement] = {}
    for record in records:
        if record.fiscal_period in seen:
            raise PeriodEngineError(
                f"{statement_type} 组内出现重复期间 {record.fiscal_period}："
                "通常是合并/母公司口径或不同披露版本被混入同一组，禁止单季化与 TTM 推导。"
            )
        seen[record.fiscal_period] = record
    return records


def derive_single_quarters(
    statements: Sequence[FinancialStatement], statement_type: str = "income"
) -> List[DerivedPeriod]:
    """Delegate to ``FinancialPeriodEngine.derive_single_quarters`` (§6).

    ``statement_type="balance"`` (or any stock input) raises ``PeriodEngineError``
    from the engine itself — the exception is deliberately **not** caught.
    """
    records = assert_homogeneous_group(statements, statement_type)
    return FinancialPeriodEngine.derive_single_quarters(records, statement_type)  # type: ignore[arg-type]


def derive_ttm(
    statements: Sequence[FinancialStatement], statement_type: str = "income"
) -> List[DerivedPeriod]:
    """Delegate to ``FinancialPeriodEngine.derive_ttm`` (v3.0 missed this entirely)."""
    records = assert_homogeneous_group(statements, statement_type)
    return FinancialPeriodEngine.derive_ttm(records, statement_type)  # type: ignore[arg-type]


def derive_periods(
    projection: ProjectionResult, *, warnings: Optional[List[str]] = None
) -> List[DerivedGroup]:
    """Derive single quarters + TTM for every *flow* group of a projection.

    A flow group spans **all** periods of one homogeneous
    ``(statement_type, 口径, 版本, 币种)`` set — the engine needs the whole
    cumulative series (Q1/H1/9M/FY) to difference a single quarter, so grouping
    by fiscal period would hand it one record at a time and silently produce
    nothing. ``projection.groups`` is period-keyed for statement identity;
    ``derived`` re-keys it here.

    Balance-sheet groups are skipped by design (stock values have no quarterly
    difference — that is not an error). A genuine engine failure inside a flow
    group is surfaced as a warning and that group is skipped; other groups still
    produce results.
    """
    engine_log = WarningLog()
    out: List[DerivedGroup] = []

    flow_groups: Dict[Tuple[str, Optional[Scope], Version, str], List[FinancialStatement]] = {}
    for key in sorted(projection.groups, key=lambda item: item.describe()):
        if key.statement_type not in _FLOW_STATEMENT_TYPES:
            continue
        flow_groups.setdefault(
            (key.statement_type, key.scope, key.version, key.currency), []
        ).extend(projection.groups[key])

    for group_key in sorted(flow_groups, key=lambda item: _flow_group_describe(*item)):
        statement_type, scope, version, currency = group_key
        records = flow_groups[group_key]
        key = GroupKey(statement_type, ALL_PERIODS, scope, version, currency)
        try:
            single = derive_single_quarters(records, statement_type)
            ttm = derive_ttm(records, statement_type)
        except PeriodEngineError as exc:
            engine_log.add(f"期间推导失败（{key.describe()}）：{exc}")
            continue
        out.append(DerivedGroup(key, tuple(single), tuple(ttm)))

    if warnings is not None:
        for message in engine_log.as_list():
            if message not in warnings:
                warnings.append(message)
    return out


def _flow_group_describe(
    statement_type: str, scope: Optional[Scope], version: Version, currency: str
) -> str:
    return f"{statement_type}/{scope_token(scope)}/{version.value}/{currency}"
