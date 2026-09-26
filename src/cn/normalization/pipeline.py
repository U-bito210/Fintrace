"""The CARD-02 pipeline: located CARD-01 facts -> canonical financial data.

Order of operations (card §6):

1. reconstruct the *table context* (header row, column roles, row labels) from
   the located cells, so a numeric cell can be paired with its row label;
2. per cell: unit -> period key -> ``period_kind`` -> 口径/版本 -> value;
3. derive ``gross_profit`` / ``cfo_minus_capex`` when they are not disclosed;
4. project onto ``FinancialStatement`` and delegate single-quarter / TTM
   derivation to the existing ``FinancialPeriodEngine``.

Everything that cannot be decided is dropped **with a warning** — never
defaulted. The result carries the CARD-01 back-link for every value.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..domain import FinancialStatement
from ..periods import DerivedPeriod
from .adapter import DerivedGroup, derive_periods, pick_primary, project_statements
from .metrics import METRIC_INDEX, MetricSemantics, resolve_metric
from .models import (
    DEFAULT_CURRENCY,
    NormalizedFact,
    PeriodKind,
    Scope,
    Version,
)
from .periods import (
    canonical_period,
    column_period_role,
    period_kind_decision,
    period_year,
    previous_period_key,
)
from .scope import detect_scope, scope_token
from .units import UnitNormalizationError, canonical_unit, normalize_unit, parse_numeric
from .version import detect_version, version_from_column
from .warning_log import WarningLog

# Statement-body tables, used to decide the document's dominant 口径.
_BODY_TABLE_MARKERS = ("资产负债表", "利润表", "现金流量表")

# Derived metrics: (canonical key, minuend, subtrahend, statement_type).
_DERIVED_RULES: Tuple[Tuple[str, str, str, str], ...] = (
    ("gross_profit", "revenue", "operating_cost", "income"),
    ("cfo_minus_capex", "cfo", "capex", "cashflow"),
)


@dataclass(frozen=True)
class TableContext:
    """Header row, column roles and row labels of one logical table."""

    table: str
    header_row: Optional[int]
    headers: Dict[int, str]
    roles: Dict[int, str]
    labels: Dict[int, str]
    undecided_columns: Tuple[int, ...] = ()

    def header_text(self, column: Optional[int]) -> Optional[str]:
        if column is None:
            return None
        return self.headers.get(column)

    def role(self, column: Optional[int]) -> str:
        if column is None:
            return "unknown"
        return self.roles.get(column, "unknown")

    def label(self, row: Optional[int]) -> Optional[str]:
        if row is None:
            return None
        return self.labels.get(row)


@dataclass
class NormalizationResult:
    """Everything CARD-02 produces for one document."""

    document_id: str
    report_period: Optional[str]
    facts: List[NormalizedFact]
    warnings: List[str]
    statements: List[FinancialStatement]
    groups: List[DerivedGroup]
    single_quarters: List[DerivedPeriod] = field(default_factory=list)
    ttm: List[DerivedPeriod] = field(default_factory=list)
    tables: Dict[str, TableContext] = field(default_factory=dict)
    statement_groups: Dict[object, List[FinancialStatement]] = field(default_factory=dict)
    #: The CARD-01 facts this result was built from, by reference.
    #:
    #: Kept so CARD-04 can run its text rules without every caller having to hand
    #: the extracted facts in separately: a caller that forgets would silently
    #: lose the comparability gate, which is exactly the failure CARD-04 exists
    #: to prevent. Not serialised — the registry already exposes these facts.
    source_facts: List[object] = field(default_factory=list)

    @property
    def fact_count(self) -> int:
        return len(self.facts)

    def statement_ids(self) -> List[str]:
        return [key.group_id for key in self.statement_groups]

    def to_dict(self) -> Dict[str, object]:
        return {
            "document_id": self.document_id,
            "report_period": self.report_period,
            "fact_count": self.fact_count,
            "facts": [fact.to_dict() for fact in self.facts],
            "statements": [
                {
                    "statement_type": statement.statement_type,
                    "fiscal_period": statement.fiscal_period,
                    "period_basis": statement.period_basis,
                    "currency": statement.currency,
                    "unit": statement.unit,
                    "available_at": statement.available_at,
                    "is_restated": statement.is_restated,
                    "values": dict(statement.values),
                }
                for statement in self.statements
            ],
            "groups": [group.to_dict() for group in self.groups],
            "single_quarters": [_derived_period_dict(p) for p in self.single_quarters],
            "ttm": [_derived_period_dict(p) for p in self.ttm],
            "warnings": list(self.warnings),
        }


def _derived_period_dict(period: DerivedPeriod) -> Dict[str, object]:
    return {
        "statement_type": period.statement_type,
        "fiscal_period": period.fiscal_period,
        "period_basis": period.period_basis,
        "values": dict(period.values),
        "source_periods": list(period.source_periods),
    }


def build_table_context(table: str, facts: Sequence[object]) -> TableContext:
    """Reconstruct column headers / roles / row labels from located cells."""
    by_row: Dict[int, List[object]] = {}
    for fact in facts:
        if fact.row is None:
            continue
        by_row.setdefault(int(fact.row), []).append(fact)

    header_row: Optional[int] = None
    for row in sorted(by_row):
        cells = by_row[row]
        if len(cells) < 2:
            continue
        if all(parse_numeric(getattr(cell, "raw_value", None)) is None for cell in cells):
            header_row = row
            break

    headers: Dict[int, str] = {}
    tokens: Dict[int, str] = {}
    if header_row is not None:
        for cell in by_row[header_row]:
            column = getattr(cell, "column", None)
            if column is None or int(column) == 0:
                continue
            headers[int(column)] = str(getattr(cell, "raw_text", "") or "")
            token = (getattr(cell, "meta", None) or {}).get("column_label")
            if token:
                tokens[int(column)] = str(token)

    # A column may still carry a period token even when its header cell landed on
    # a different row (e.g. a two-line header); collect tokens from every cell.
    for cells in by_row.values():
        for cell in cells:
            column = getattr(cell, "column", None)
            token = (getattr(cell, "meta", None) or {}).get("column_label")
            if column is not None and token and int(column) not in tokens:
                tokens[int(column)] = str(token)

    roles: Dict[int, str] = {}
    for column in sorted(set(headers) | set(tokens)):
        roles[column] = column_period_role(headers.get(column), token=tokens.get(column))

    labels: Dict[int, str] = {}
    for row, cells in by_row.items():
        if row == header_row:
            continue
        textual = [
            cell for cell in cells
            if parse_numeric(getattr(cell, "raw_value", None)) is None
            and str(getattr(cell, "raw_text", "") or "").strip()
        ]
        if not textual:
            continue
        labels[row] = str(getattr(min(textual, key=lambda cell: int(getattr(cell, "column", 0) or 0)), "raw_text", ""))

    data_columns = sorted(column for column in set(headers) | set(tokens) if column != 0)
    undecided = tuple(column for column in data_columns if roles.get(column) == "unknown")
    # One undecided data column is the universal single-period layout; two or more
    # means we genuinely cannot tell which column is the reporting period.
    if len(undecided) < 2:
        undecided = ()

    return TableContext(
        table=table,
        header_row=header_row,
        headers=headers,
        roles=roles,
        labels=labels,
        undecided_columns=undecided,
    )


def _document_scope(facts: Sequence[object]) -> Optional[Scope]:
    """The document's dominant 口径, inferred only from its own statement bodies."""
    scopes = set()
    for fact in facts:
        table = str(getattr(fact, "table", "") or "")
        if not any(marker in table for marker in _BODY_TABLE_MARKERS):
            continue
        resolved = Scope.coerce(getattr(fact, "scope", None)) or detect_scope(table)
        if resolved is not None:
            scopes.add(resolved)
    if len(scopes) == 1:
        return scopes.pop()
    return None


def _document_id(document: object, facts: Sequence[object]) -> str:
    value = getattr(document, "document_id", None)
    if value:
        return str(value)
    for fact in facts:
        if getattr(fact, "document_id", None):
            return str(fact.document_id)
    return ""


def _document_blob(document: object) -> str:
    parts: List[str] = []
    for name in ("filename", "company", "report_period", "document_type"):
        value = getattr(document, name, None)
        if value:
            parts.append(str(value))
    for note in list(getattr(document, "notes", None) or []):
        parts.append(str(note))
    return " ".join(parts)


def normalize_financials(
    facts: Iterable[object],
    document: object = None,
    *,
    default_unit: Optional[str] = None,
    default_scope: Optional[Scope] = None,
    reporting_currency: str = DEFAULT_CURRENCY,
) -> NormalizationResult:
    """Full CARD-02 pipeline for one document's located facts.

    ``default_unit`` exists for documents whose money unit is declared once in a
    cover-page note rather than per table; passing ``None`` (the default) means
    "fail closed" — a value with no declared unit is dropped, never assumed to
    be 元.
    """
    log = WarningLog()
    fact_list = list(facts)
    document_id = _document_id(document, fact_list)

    raw_report_period = getattr(document, "report_period", None) or next(
        (getattr(fact, "period", None) for fact in fact_list if getattr(fact, "period", None)), None
    )
    declared_report_type = getattr(document, "document_type", None) or raw_report_period
    try:
        report_period = canonical_period(raw_report_period, declared_report_type)
    except ValueError as exc:
        log.add(f"文档未声明可用的报告期，无法规范化：{exc}")
        return NormalizationResult(
            document_id, None, [], log.as_list(), [], [], [], [], {},
            source_facts=fact_list,
        )

    document_version = detect_version(_document_blob(document))
    if document_version is not Version.AS_REPORTED:
        log.add(
            f"文档携带 {document_version.value} 信号，未另行声明版本的列按该版本标注"
            "（信号需 CARD-04 确认）。"
        )

    tables: Dict[str, TableContext] = {}
    grouped: Dict[str, List[object]] = {}
    for fact in fact_list:
        table = str(getattr(fact, "table", "") or getattr(fact, "section", "") or "")
        grouped.setdefault(table, []).append(fact)
    for table, items in grouped.items():
        context = build_table_context(table, items)
        tables[table] = context
        if context.undecided_columns:
            log.add(
                f"表『{table}』有 {len(context.undecided_columns)} 个列语义未声明"
                f"（列 {list(context.undecided_columns)}），已按最左数据列作为本报告期，需人工复核。"
            )

    fallback_scope = default_scope or _document_scope(fact_list)
    declared_unit = canonical_unit(default_unit) if default_unit else None

    normalized: List[NormalizedFact] = []
    seen_ids: set = set()

    for fact in fact_list:
        table = str(getattr(fact, "table", "") or getattr(fact, "section", "") or "")
        context = tables[table]
        column = getattr(fact, "column", None)
        row = getattr(fact, "row", None)

        raw_value = getattr(fact, "raw_value", None)
        if raw_value is None or parse_numeric(raw_value) is None:
            # A label / note cell: it provides the row label, not a figure.
            continue

        meta = getattr(fact, "meta", None) or {}
        token = meta.get("column_label")
        role = context.role(column) if column is not None else "current"
        if meta.get("period_year_unresolved"):
            log.tally(
                "year-column-unresolved",
                "年份表头与数值列无法可靠对应，已跳过该值",
                f"{context.table} 第{getattr(fact, 'page', None)}页第{column}列",
            )
            continue
        explicit_year = meta.get("period_year")
        if explicit_year is not None:
            if explicit_year == period_year(report_period):
                role = "current"
            elif explicit_year == period_year(report_period) - 1:
                role = "prior"
            else:
                log.tally(
                    "year-column-outside-report",
                    "年份表头与报告期不相邻，已跳过该值",
                    f"{context.table} 第{getattr(fact, 'page', None)}页第{column}列",
                )
                continue

        match = resolve_metric(context.label(row))
        if not match.known:
            log.tally(
                f"unknown:{match.reason}",
                f"未知指标标签（{match.reason}），已按 {match.key} 保留并禁止下游引用",
                f"{context.label(row)}",
            )

        decision = period_kind_decision(
            context.table,
            context.header_text(column),
            declared_report_type,
            column_token=token,
        )
        if decision.warning:
            log.add(decision.warning)

        if role == "change":
            log.tally("change-column", "同比/环比列不是水平值，未规范化", f"{context.table}/{match.key}")
            continue

        period_key = report_period
        if role == "prior":
            if decision.kind is None:
                # Without a decided kind we cannot say *which* prior period it is.
                log.tally(
                    "prior-undecided",
                    "上年同期/期初列的期间语义未判定，已跳过（不猜测上一可比期间）",
                    f"{context.table}/{match.key}",
                )
                continue
            prior = previous_period_key(
                report_period, point_in_time=decision.kind is PeriodKind.POINT_IN_TIME
            )
            if prior is None:
                log.tally("prior-unknown", "上一可比期间无法确定，已跳过", f"{context.table}/{match.key}")
                continue
            period_key = prior

        fact_unit = canonical_unit(getattr(fact, "unit", None))
        unit_token = fact_unit or declared_unit
        if unit_token is None:
            log.tally(
                "no-unit",
                "未声明单位，无法确定数值量级，已跳过（拒绝按元处理）",
                f"{context.table}/{match.key} 第{getattr(fact, 'page', None)}页",
            )
            continue
        if declared_unit is not None and fact_unit is not None and fact_unit != declared_unit:
            # Card §7.3 rule 2: the table header wins, but never silently.
            log.add(
                f"表『{context.table}』声明单位（{fact_unit}）与文档级单位（{declared_unit}）冲突，"
                f"以表头声明 {fact_unit} 为准。"
            )

        try:
            value = normalize_unit(raw_value, unit_token)
        except UnitNormalizationError as exc:
            log.tally("bad-value", f"数值无法规范化：{exc}", str(raw_value))
            continue

        scope = Scope.coerce(getattr(fact, "scope", None)) or detect_scope(context.table) or fallback_scope
        if scope is None:
            log.tally(
                "no-scope",
                "表与文档均未声明口径（合并/母公司），保留为未标注口径，禁止与已标注口径配对计算",
                context.table,
            )
        currency = str(getattr(fact, "currency", None) or reporting_currency).upper()

        # A 前期差错更正 note presents one period under two versions; the column
        # header is the only thing that tells them apart. Falling back to the
        # document label for an unmarked column, never overriding it the other
        # way round: the document label describes the filing, the column label
        # describes this figure.
        column_version = version_from_column(getattr(fact, "meta", None))
        fact_version = column_version or document_version
        if column_version is not None and column_version is not document_version:
            log.add(
                f"表『{context.table}』第 {column} 列声明 {column_version.value}，"
                f"该列事实按列声明标注（文档级为 {document_version.value}），"
                "两版本值禁止配对计算。"
            )

        normalized_fact_id = (
            f"nf_{document_id}_{match.key}_{period_key}_{scope_token(scope)}_"
            f"{getattr(fact, 'page', None)}_{row}_{column}"
        )
        if normalized_fact_id in seen_ids:
            continue
        seen_ids.add(normalized_fact_id)

        normalized.append(
            NormalizedFact(
                normalized_fact_id=normalized_fact_id,
                metric=match.key,
                value=value,
                currency=currency,
                period_key=period_key,
                period_kind=decision.kind,
                scope=scope,
                version=fact_version,
                available_at=getattr(fact, "available_at", None),
                source_fact_id=str(getattr(fact, "fact_id", "") or ""),
                document_id=document_id,
                page=getattr(fact, "page", None),
                bbox=getattr(fact, "bbox", None),
                row=int(row) if row is not None else None,
                column=int(column) if column is not None else None,
                raw_value=str(raw_value),
                source_unit=unit_token,
                source_table=context.table or None,
                statement_type=(match.statement if match.statement in ("income", "balance", "cashflow") else None),
                metric_known=match.known,
                warnings=(match.reason,) if match.reason else (),
            )
        )

    derived = _derive_metrics(normalized, log)
    normalized.extend(derived)

    projection = project_statements(normalized, warnings=log.as_list())
    assignments = projection.assignments
    normalized = [
        replace(fact, financial_statement_id=assignments.get(fact.normalized_fact_id))
        if assignments.get(fact.normalized_fact_id)
        else fact
        for fact in normalized
    ]

    # The projection owns the deduplicated/sorted warning list from here on.
    warnings = list(projection.warnings)
    groups = derive_periods(projection, warnings=warnings)
    single_quarters = [period for group in groups for period in group.single_quarters]
    ttm = [period for group in groups for period in group.ttm]

    return NormalizationResult(
        document_id=document_id,
        report_period=report_period,
        facts=normalized,
        warnings=warnings,
        statements=projection.statements,
        groups=groups,
        single_quarters=single_quarters,
        ttm=ttm,
        tables=tables,
        statement_groups=projection.groups,
        source_facts=fact_list,
    )


def _derive_metrics(facts: List[NormalizedFact], log: WarningLog) -> List[NormalizedFact]:
    """Derive §7.1 派生指标 (gross_profit / cfo_minus_capex) when not disclosed."""
    by_group: Dict[Tuple[str, str, Optional[Scope], Version, str], Dict[str, List[NormalizedFact]]] = {}
    for fact in facts:
        spec = METRIC_INDEX.get(fact.metric)
        if spec is None or spec.semantics is MetricSemantics.MARKET:
            continue
        key = (spec.statement, fact.period_key, fact.scope, fact.version, fact.currency)
        by_group.setdefault(key, {}).setdefault(fact.metric, []).append(fact)

    derived: List[NormalizedFact] = []
    for out_key, minuend_key, subtrahend_key, statement_type in _DERIVED_RULES:
        for key, candidates in sorted(by_group.items(), key=lambda item: str(item[0])):
            if key[0] != statement_type:
                continue
            if candidates.get(out_key):
                continue  # disclosed value wins (card §7.1: 若有披露则取披露值)
            if not candidates.get(minuend_key) or not candidates.get(subtrahend_key):
                continue
            # Use the same precedence as the projection, so the derived figure is
            # consistent with the operands that end up in the statement.
            minuend = pick_primary(candidates[minuend_key])
            subtrahend = pick_primary(candidates[subtrahend_key])
            if minuend.period_kind is not subtrahend.period_kind:
                continue
            # Both ends agree on the period kind, but that agreement is
            # worthless when the kind is *undecided*: a 前期差错更正 note table is
            # not in the statement whitelist, so its figures carry
            # ``period_kind=None`` and a period_key borrowed from the report.
            # Deriving from them stamped "2025FY gross_profit" onto what were
            # actually two restated 2024 figures. §7.3's rule is to stop when the
            # semantics are unknown, not to compute anyway.
            if minuend.period_kind is None:
                log.tally(
                    "derive-undecided-period",
                    f"派生指标 {out_key} 跳过：参与计算的事实期间语义未判定"
                    "（来源表不在三大表/摘要表白名单内），不在未知语义上做计算",
                    f"{minuend.period_key}/{scope_token(minuend.scope)}",
                )
                continue
            derived.append(
                NormalizedFact(
                    normalized_fact_id=(
                        f"nf_{minuend.document_id}_{out_key}_{minuend.period_key}_"
                        f"{scope_token(minuend.scope)}_derived"
                    ),
                    metric=out_key,
                    value=minuend.value - subtrahend.value,
                    currency=minuend.currency,
                    period_key=minuend.period_key,
                    period_kind=minuend.period_kind,
                    scope=minuend.scope,
                    version=minuend.version,
                    available_at=minuend.available_at,
                    source_fact_id=f"derived({minuend.source_fact_id},{subtrahend.source_fact_id})",
                    document_id=minuend.document_id,
                    raw_value=f"{minuend.raw_value} - {subtrahend.raw_value}",
                    source_unit=minuend.source_unit,
                    source_table="derived",
                    statement_type=statement_type,
                    metric_known=True,
                    warnings=(f"由 {minuend_key} − {subtrahend_key} 派生",),
                )
            )
            log.add(
                f"派生指标 {out_key}（{minuend.period_key}/{scope_token(minuend.scope)}）："
                f"{minuend_key} − {subtrahend_key} = {minuend.value - subtrahend.value:.2f} 元，"
                "可用披露值核对。"
            )
    return derived


def normalize_document(
    facts: Iterable[object], document: object = None
) -> Tuple[List[NormalizedFact], List[str]]:
    """CARD-02 §6 主入口: ``(NormalizedFact[], warnings[])``."""
    result = normalize_financials(facts, document)
    return result.facts, list(result.warnings)
