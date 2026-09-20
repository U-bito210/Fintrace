"""CARD-22 §7 F2 — Table Structural Constraints (小计勾稽).

``Subtotal ≈ Sum(children)`` is only a *check* if the children are known. The
first version of this module guessed them: a subtotal row was assumed to sum the
contiguous block of numeric rows above it. Running it against the committed
corpus immediately produced two false violations on
``annual_06_equity_shares.pdf`` — 资产总计 was read as the sum of 期末总股本 and
归母净资产, two rows that are not assets at all. A detector with a false-alarm
rate like that is worse than no detector, so the heuristic is gone.

What replaces it is **declared structure**: :data:`SUBTOTAL_SPECS` names the
subtotal and its children *by label*, and every entry carries the accounting rule
it comes from. The specs are drawn from published statement structure
(资产负债表 sub-totals), i.e. they are cited relations, not inferred ones — and
label matching is exact after :func:`~src.cn.normalization.metrics.normalize_label`,
so an OCR-mangled row does not silently match something else.

What this can and cannot see, stated plainly:

* **can** — a subtotal whose components are themselves aggregate rows
  (资产总计 = 流动资产合计 + 非流动资产合计). Mis-signed, mis-columned or
  mis-transcribed components all show up as a residual.
* **cannot** — a subtotal of *line items* (存货 + 应收账款 + … = 流动资产合计),
  because the metric dictionary is not a chart of accounts and inventing one here
  would fabricate the very relation being checked. That reach is deliberately
  left to a 决赛 constraint-solver pass, and recorded as a limitation rather than
  papered over with adjacency guessing.

A comparison is unit-independent: parent and children are cells of one column of
one table, so they share a unit and the scale cancels. Findings therefore report
``unit="TABLE_UNIT"`` and the §7 policy gives that unit a relative-only band.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..normalization.metrics import normalize_label
from ..normalization.units import parse_numeric
from .models import (
    ConstraintOperand,
    ConstraintViolation,
    FAMILY_TABLE_STRUCTURAL,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    STATUS_VIOLATED,
    SourceRef,
    make_violation_id,
)
from .tolerance import TolerancePolicy

CONSTRAINT_SUBTOTAL_RECONCILIATION = "subtotal_reconciliation"

#: The pseudo-unit every F2 finding reports (see the module docstring).
TABLE_UNIT = "TABLE_UNIT"

#: A spec must name at least two children; one child is a relabelled value.
MIN_CHILDREN = 2

FACT_KIND_TABLE_CELL = "table_cell"


@dataclass(frozen=True)
class SubtotalSpec:
    """A declared ``subtotal = sum(children)`` relation.

    ``source`` is mandatory and non-empty: an unreferenced relation would be an
    invented accounting rule, which is exactly what the project forbids.
    """

    spec_id: str
    subtotal_labels: Tuple[str, ...]
    child_labels: Tuple[str, ...]
    source: str

    def __post_init__(self) -> None:
        if len(self.child_labels) < MIN_CHILDREN:
            raise ValueError(f"{self.spec_id}: 勾稽关系至少需要两个子项")
        if not self.source.strip():
            raise ValueError(f"{self.spec_id}: 勾稽关系必须注明来源，无来源即发明")


#: §7 F2's shipped relations — published 资产负债表 sub-total structure. They are
#: *aggregate-of-aggregate* relations on purpose: every operand is a row that a
#: standard balance sheet actually prints, so no chart of accounts has to be
#: invented to evaluate them.
SUBTOTAL_SPECS: Tuple[SubtotalSpec, ...] = (
    SubtotalSpec(
        spec_id="total_assets_from_current_and_noncurrent",
        subtotal_labels=("资产总计", "资产合计"),
        child_labels=("流动资产合计", "非流动资产合计"),
        source="资产负债表结构：资产总计 = 流动资产合计 + 非流动资产合计",
    ),
    SubtotalSpec(
        spec_id="total_liabilities_from_current_and_noncurrent",
        subtotal_labels=("负债合计", "负债总计"),
        child_labels=("流动负债合计", "非流动负债合计"),
        source="资产负债表结构：负债合计 = 流动负债合计 + 非流动负债合计",
    ),
    SubtotalSpec(
        spec_id="equity_from_parent_and_minority",
        subtotal_labels=("所有者权益合计", "股东权益合计"),
        child_labels=("归属于母公司所有者权益合计", "归属于母公司股东权益合计",
                      "少数股东权益"),
        source="合并资产负债表结构：所有者权益合计 = 归母权益 + 少数股东权益",
    ),
)


@dataclass(frozen=True)
class SubtotalBlock:
    """One spec resolved against one table column."""

    spec: SubtotalSpec
    table: str
    column: int
    subtotal_row: int
    subtotal_label: str
    subtotal_fact: object
    parent_value: float
    children: Tuple[object, ...]
    child_values: Tuple[float, ...]
    missing_labels: Tuple[str, ...] = ()

    @property
    def expected(self) -> float:
        return float(sum(self.child_values))


def _numeric(fact: object) -> Optional[float]:
    """The cell's own number, or ``None`` when it is a label / placeholder.

    ``parse_numeric`` already refuses ``—`` / ``N/A`` / ``不适用`` instead of
    reading them as zero, which matters here: a "no value" cell collapsing to 0
    would silently make a subtotal look reconciled.
    """
    return parse_numeric(getattr(fact, "raw_value", None))


def _by_table_row(facts: Sequence[object]) -> Dict[Tuple[str, int], Dict[int, object]]:
    """``(table, row) -> column -> cell`` for every located table cell."""
    out: Dict[Tuple[str, int], Dict[int, object]] = {}
    for fact in facts:
        if getattr(fact, "kind", None) != FACT_KIND_TABLE_CELL:
            continue
        table = getattr(fact, "table", None)
        row = getattr(fact, "row", None)
        column = getattr(fact, "column", None)
        if not table or row is None or column is None:
            continue
        out.setdefault((str(table), int(row)), {})[int(column)] = fact
    return out


def _label_of(cells: Dict[int, object]) -> str:
    """The row's label: the left-most cell that is not a number."""
    for column in sorted(cells):
        if _numeric(cells[column]) is None:
            return normalize_label(getattr(cells[column], "raw_text", ""))
    return ""


def _match(labels: Sequence[str], text: str) -> Optional[str]:
    """Exact match after normalisation — a mangled label must not match."""
    normalised = {normalize_label(item): item for item in labels}
    return normalised.get(text)


def find_subtotal_blocks(
    facts: Sequence[object],
    *,
    specs: Sequence[SubtotalSpec] = SUBTOTAL_SPECS,
) -> List[SubtotalBlock]:
    """Resolve every declared spec against every column of every table."""
    rows = _by_table_row(facts)
    # label index: (table, row) -> normalised label
    label_index: Dict[Tuple[str, int], str] = {
        key: _label_of(cells) for key, cells in rows.items()
    }
    blocks: List[SubtotalBlock] = []

    for (table, subtotal_row), cells in sorted(rows.items(), key=lambda item: str(item[0])):
        label = label_index[(table, subtotal_row)]
        if not label:
            continue
        for spec in specs:
            if _match(spec.subtotal_labels, label) is None:
                continue
            for column in sorted(cells):
                parent = _numeric(cells[column])
                if parent is None:
                    continue
                children: List[object] = []
                values: List[float] = []
                missing: List[str] = []
                for child_label in spec.child_labels:
                    found = _child_in_column(rows, label_index, table, column, child_label)
                    if found is None:
                        missing.append(child_label)
                        continue
                    value = _numeric(found)
                    if value is None:
                        missing.append(child_label)
                        continue
                    children.append(found)
                    values.append(value)
                if len(children) < MIN_CHILDREN:
                    # Not enough of the declared components are present to
                    # reconcile: §11 says skip, and the family-level report says
                    # the column was not checked rather than that it passed.
                    continue
                blocks.append(
                    SubtotalBlock(
                        spec=spec, table=table, column=column, subtotal_row=subtotal_row,
                        subtotal_label=label, subtotal_fact=cells[column],
                        parent_value=parent,
                        children=tuple(children), child_values=tuple(values),
                        missing_labels=tuple(missing),
                    )
                )
    return blocks


def _child_in_column(
    rows: Dict[Tuple[str, int], Dict[int, object]],
    label_index: Dict[Tuple[str, int], str],
    table: str,
    column: int,
    child_label: str,
) -> Optional[object]:
    """The child cell in *this* column, so a subtotal cannot borrow another column.

    Search order is row order, so the first matching row wins and the result is
    as deterministic as the document's own layout.
    """
    target = normalize_label(child_label)
    for (row_table, row), cells in sorted(rows.items(), key=lambda item: (item[0][0], item[0][1])):
        if row_table != table or column not in cells:
            continue
        if label_index.get((row_table, row)) != target:
            continue
        return cells[column]
    return None


def _operand(fact: object) -> ConstraintOperand:
    return ConstraintOperand(
        ref=str(getattr(fact, "fact_id", "") or ""),
        value=_numeric(fact),
        unit=TABLE_UNIT,
        label=normalize_label(getattr(fact, "raw_text", ""))[:40] or None,
    )


def _source_ref(fact: object) -> SourceRef:
    bbox = getattr(fact, "bbox", None)
    return SourceRef(
        document_id=str(getattr(fact, "document_id", "") or "") or None,
        page=getattr(fact, "page", None),
        bbox=bbox if bbox and len(bbox) == 4 else None,
        locator=(
            f"{getattr(fact, 'table', '')} r{getattr(fact, 'row', '')}"
            f"c{getattr(fact, 'column', '')}"
        ),
    )


def check_table_structural(
    facts: Sequence[object],
    *,
    policy: TolerancePolicy,
    specs: Sequence[SubtotalSpec] = SUBTOTAL_SPECS,
    detected_at: str = "",
) -> List[ConstraintViolation]:
    """§7 F2 over CARD-01 table cells.

    Takes raw facts rather than CARD-02 normalized facts on purpose: a subtotal
    row is a subtotal only *in its table*; once projected it is one more metric
    and the column/row geometry that made it checkable is gone.
    """
    results: List[ConstraintViolation] = []
    for block in find_subtotal_blocks(facts, specs=specs):
        inputs = tuple(_operand(item) for item in block.children)
        refs = (_source_ref(block.subtotal_fact),) + tuple(
            _source_ref(item) for item in block.children
        )
        expected = block.expected
        actual = block.parent_value
        status, tolerance, residual = policy.classify(
            expected, actual, constraint_id=CONSTRAINT_SUBTOTAL_RECONCILIATION,
            unit=TABLE_UNIT,
        )
        if status == STATUS_VIOLATED:
            note = (
                f"小计「{block.subtotal_label}」应等于 {' + '.join(block.spec.child_labels)} "
                f"（规则 {block.spec.spec_id}，依据：{block.spec.source}）；"
                "同一列同一单位内比较，单位已约去。"
                "常见成因：单元格串行 / 列错位 / 负号丢失 / 单位不一致"
            )
        else:
            note = (
                f"小计「{block.subtotal_label}」与 {' + '.join(block.spec.child_labels)} "
                f"一致（规则 {block.spec.spec_id}）"
            )
        results.append(
            ConstraintViolation(
                violation_id=make_violation_id(
                    CONSTRAINT_SUBTOTAL_RECONCILIATION,
                    FAMILY_TABLE_STRUCTURAL,
                    (block.spec.spec_id, block.table, str(block.column), str(block.subtotal_row))
                    + tuple(str(item.ref) for item in inputs),
                ),
                constraint_family=FAMILY_TABLE_STRUCTURAL,
                constraint_id=CONSTRAINT_SUBTOTAL_RECONCILIATION,
                inputs=inputs,
                expected_value=expected,
                actual_value=actual,
                residual=residual,
                tolerance=tolerance,
                status=status,
                source_refs=refs,
                severity=SEVERITY_ERROR if status == STATUS_VIOLATED else SEVERITY_WARNING,
                detected_at=detected_at,
                note=note,
            )
        )
    return results
