"""Table parser with A-share multi-page merge and dual-column split.

Two blind spots from the v3.0 design (called out in CARD-01 edge cases 7-8):

* **多页表合并** — A-share 三大表 routinely span 2-4 pages, repeating the
  header and continuing columns. The parser (1) matches a statement header on
  the first page, (2) detects ``续表``/``续`` markers on later pages, (3) skips
  the repeated header row, and (4) assigns a single global row index so the
  whole logical table is reconstructed.
* **双列表拆分** — the "主要会计数据和财务指标" summary carries dual period
  columns (``本报告期`` / ``年初至本报告期末``). Each column is labelled with its
  ``period_kind`` so CARD-02's ``detect_period_kind`` can consume it.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ...disclosure_lexicon import VERSION_COLUMN_KEYWORDS
from ..models import (
    FACT_KIND_TABLE_CELL,
    FACT_KIND_TEXT,
    FACT_STATUS_OK,
    FACT_STATUS_PARTIAL,
    RawFact,
)
from .base import (
    NUMERIC_RE,
    BaseParser,
    iter_pdf_pages,
    looks_numeric,
    open_pdf_bytes,
    resolve_textless_page,
)

# statement header -> (statement_type, scope)
STATEMENT_HEADERS: Dict[str, Tuple[str, Optional[str]]] = {
    "合并资产负债表": ("balance", "合并"),
    "母公司资产负债表": ("balance", "母公司"),
    "资产负债表": ("balance", "合并"),
    "合并利润表": ("income", "合并"),
    "母公司利润表": ("income", "母公司"),
    "利润表": ("income", "合并"),
    "合并利润及利润分配表": ("income", "合并"),
    "合并现金流量表": ("cashflow", "合并"),
    "母公司现金流量表": ("cashflow", "母公司"),
    "现金流量表": ("cashflow", "合并"),
    "主要会计数据和财务指标": ("summary", None),
}

CONTINUATION_MARKERS = re.compile(r"(续表|续\s*表|\(续\)|（续）|接上表|接下表)")

# Declared money unit. The unit alternatives MUST be longest-first so that
# "万元" is never matched as a bare "元"; the currency group is bounded so it
# cannot swallow the magnitude word.
_MONEY_RE = re.compile(
    r"(?:单位|币种)[：:]\s*"
    r"(?P<currency>人民币|美元|港元|港币|CNY|RMB|USD|HKD)?\s*"
    r"(?P<unit>亿元|千万元|百万元|万元|千元|元)"
)

# Dual-column period keywords -> period_kind label.
# More specific patterns MUST precede shorter ones they contain as substrings
# (e.g. "年初至本报告期末" contains "本报告期"), otherwise the generic match wins.
PERIOD_COLUMN_KEYWORDS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"年初至本报告期末|年初至报告期末"), "ytd"),
    (re.compile(r"本报告期比上年同期增减|比上年同期"), "yoy_change"),
    (re.compile(r"上年度末|上年末"), "prior_year_end"),
    (re.compile(r"上年同期|去年同期"), "prior_year"),
    (re.compile(r"本报告期末"), "period_end"),
    (re.compile(r"本报告期"), "current_period"),
]
_YEAR_HEADER_RE = re.compile(r"(20\d{2})\s*年(?:度)?")

# Version-column keywords live in the shared lexicon so CARD-01's reader and
# CARD-02's labeller can never drift apart. ``_version_label`` below turns a
# header cell into a token; deciding that a restatement actually happened is
# CARD-04's job, not this table's.
def _version_label(text: str) -> Optional[str]:
    """Disclosure-version token for a column header, or ``None`` when unmarked."""
    cleaned = re.sub(r"\s+", "", str(text or ""))
    if not cleaned:
        return None
    for pattern, label in VERSION_COLUMN_KEYWORDS:
        if pattern.search(cleaned):
            return label
    return None


def _looks_numeric(text: str) -> Optional[str]:
    """Backward-compatible alias for :func:`base.looks_numeric`."""
    return looks_numeric(text)


def _row_cells(row) -> list:
    """Keep for backward compatibility; not used by the grid reconstructor."""
    return []


def _in_bbox(word: dict, bbox) -> bool:
    if not bbox:
        return False
    x0, y0, x1, y1 = bbox
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["top"] + word["bottom"]) / 2
    return x0 <= cx <= x1 and y0 <= cy <= y1


def _cluster(values: List[float], tol: float) -> List[tuple]:
    """Greedy 1-D clustering; returns ``[(center, [indices]), ...]`` sorted ascending."""
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i])
    clusters: List[tuple] = []
    for i in order:
        v = values[i]
        if clusters and abs(v - clusters[-1][0]) <= tol:
            clusters[-1][1].append(i)
            members = clusters[-1][1]
            clusters[-1] = (sum(values[j] for j in members) / len(members), members)
        else:
            clusters.append((v, [i]))
    return clusters


def _nearest_cluster(clusters: List[tuple], value: float) -> int:
    best, best_d = 0, float("inf")
    for idx, (center, _) in enumerate(clusters):
        d = abs(center - value)
        if d < best_d:
            best, best_d = idx, d
    return best


def _reconstruct_grid(words: list, bbox, row_tol: float = 4.0, col_tol: float = 22.0) -> List[dict]:
    """Rebuild a table's cell grid from the word layer inside ``bbox``.

    Words are clustered into rows (by vertical center) and columns (by left
    edge). Returns ``[{"row", "col", "text", "bbox"}, ...]`` ordered top-to-bottom,
    left-to-right. Version-independent: it never touches pdfplumber Cell objects.
    """
    inside = [w for w in words if _in_bbox(w, bbox)]
    if not inside:
        return []
    row_clusters = _cluster([(w["top"] + w["bottom"]) / 2 for w in inside], row_tol)
    col_clusters = _cluster([w["x0"] for w in inside], col_tol)
    cells: Dict[tuple, dict] = {}
    for w in inside:
        ri = _nearest_cluster(row_clusters, (w["top"] + w["bottom"]) / 2)
        ci = _nearest_cluster(col_clusters, w["x0"])
        entry = cells.setdefault((ri, ci), {"parts": [], "xs": [], "ys": []})
        entry["parts"].append(w["text"])
        entry["xs"].extend([w["x0"], w["x1"]])
        entry["ys"].extend([w["top"], w["bottom"]])
    grid = []
    for (ri, ci), entry in cells.items():
        grid.append({
            "row": ri,
            "col": ci,
            "text": "".join(entry["parts"]),
            "bbox": (min(entry["xs"]), min(entry["ys"]), max(entry["xs"]), max(entry["ys"])),
        })
    grid.sort(key=lambda c: (c["row"], c["col"]))
    return grid


def _grid_header_row(grid: List[dict]) -> int:
    """Index of the first grid row carrying a dual-column period keyword, else 0."""
    for i, cell in enumerate(grid):
        for pattern, _ in PERIOD_COLUMN_KEYWORDS:
            if pattern.search(cell["text"] or ""):
                # Period keywords live in the header row; return that row index.
                return cell["row"]
    return 0


def _label_columns_from_grid(grid: List[dict], header_row_idx: int) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    for cell in grid:
        if cell["row"] != header_row_idx:
            continue
        for pattern, kind in PERIOD_COLUMN_KEYWORDS:
            if pattern.search(cell["text"] or ""):
                labels[cell["col"]] = kind
                break
    return labels


def _label_version_columns_from_grid(grid: List[dict], header_row_idx: int) -> Dict[int, str]:
    """Column index -> disclosure-version token, read from the header row.

    Applied to *every* cell of that column, not just the header: CARD-02 reads
    the token off the numeric fact it is normalising, and the header cell itself
    carries no value.
    """
    labels: Dict[int, str] = {}
    for cell in grid:
        if cell["row"] != header_row_idx:
            continue
        token = _version_label(cell["text"] or "")
        if token:
            labels[cell["col"]] = token
    return labels


def _year_header_anchors(grid: List[dict], header_row_idx: int) -> List[Tuple[float, Optional[int]]]:
    """Locate explicit year headings by their PDF positions, not grid column ids."""
    header = [cell for cell in grid if cell["row"] == header_row_idx and cell["text"].strip()]
    years = [
        int(match.group(1)) for cell in header
        if (match := _YEAR_HEADER_RE.fullmatch(cell["text"].strip()))
    ]
    if len(set(years)) < 2:
        return []
    anchors: List[Tuple[float, Optional[int]]] = []
    for cell in header:
        match = _YEAR_HEADER_RE.fullmatch(cell["text"].strip())
        x0, _, x1, _ = cell["bbox"]
        anchors.append(((x0 + x1) / 2, int(match.group(1)) if match else None))
    return anchors


def _year_for_cell(cell: dict, anchors: List[Tuple[float, Optional[int]]]) -> Optional[int]:
    if not anchors:
        return None
    x0, _, x1, _ = cell["bbox"]
    center = (x0 + x1) / 2
    distances = sorted(((abs(center - x), year) for x, year in anchors), key=lambda item: item[0])
    if distances[0][0] > 80 or (len(distances) > 1 and distances[1][0] - distances[0][0] < 4):
        return None
    return distances[0][1]


class TableParser(BaseParser):
    name = "table"

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> "ParserOutput":  # type: ignore[name-defined]
        from ..models import ParserOutput, ParsePageEvent
        with open_pdf_bytes(data) as pdf:
            pages = iter_pdf_pages(pdf)
            doc = self._new_document(page_count=len(pages))
            facts: List[RawFact] = []
            events: List[ParsePageEvent] = []
            warnings: List[str] = []

            logical_rows: Dict[str, int] = {}   # statement_key -> next global row index
            active_statement: Optional[str] = None

            for idx, page in enumerate(pages, start=1):
                text = page.extract_text() or ""
                currency, unit = self._detect_money(text)
                header_hit = self._detect_statement(text)
                is_continuation = bool(CONTINUATION_MARKERS.search(text))

                # Decide the statement this page's tables belong to.
                if header_hit:
                    active_statement = header_hit
                elif is_continuation and active_statement:
                    pass  # continue previous statement
                elif not header_hit:
                    active_statement = None

                tables = page.find_tables()
                if not tables and not text.strip():
                    # No text layer at all -> likely a scanned statement page. Try
                    # OCR; with no backend the page *fails* (fail-closed) and emits
                    # no facts, so the registry degrades the document instead of
                    # reporting a silently empty page (CARD-01 edge case 1).
                    event, warning, ocr_text = resolve_textless_page(idx, page, "table")
                    events.append(event)
                    warnings.append(warning)
                    if ocr_text:
                        facts.append(RawFact(
                            page=idx, raw_text=ocr_text, raw_value=looks_numeric(ocr_text),
                            kind=FACT_KIND_TEXT, meta={"ocr": True},
                        ))
                    continue

                # CARD-01 edge case 7 rule ③: on a continuation page the first row
                # repeats the previous page's header and must be skipped. A real
                # filing may mark the page with "续表", "（续）", or by re-stating the
                # table title — all three mean "same logical table continues", so
                # the skip must not depend on the statement title being absent.
                skip_first_row = bool(is_continuation and active_statement)
                page_cell_count = 0
                page_words = page.extract_words()
                for t_idx, table in enumerate(tables):
                    table_bbox = getattr(table, "bbox", None)
                    if not table_bbox:
                        continue
                    # Reconstruct the cell grid from the table bbox + word layer.
                    # pdfplumber's TableCell object API is version-fragile, so we
                    # cluster words by row (top) and column (x0) instead.
                    grid = _reconstruct_grid(page_words, table_bbox)
                    if not grid:
                        continue
                    statement_key = active_statement or f"table_p{idx}_t{t_idx}"
                    stype, scope = STATEMENT_HEADERS.get(statement_key, (None, None))
                    next_global_row = logical_rows.setdefault(statement_key, 0)
                    # Column labels from the first row carrying period keywords.
                    header_idx = _grid_header_row(grid)
                    column_labels = _label_columns_from_grid(grid, header_idx)
                    version_labels = _label_version_columns_from_grid(grid, header_idx)
                    year_anchors = _year_header_anchors(grid, header_idx)
                    # Group cells by grid row so the global row index advances once
                    # per logical table row (not per cell).
                    by_row: Dict[int, List[dict]] = {}
                    for cell in grid:
                        by_row.setdefault(cell["row"], []).append(cell)
                    for gr in sorted(by_row):
                        if skip_first_row and gr == 0:
                            continue  # repeated header on a 续表 page
                        global_row = next_global_row
                        next_global_row += 1
                        for cell in by_row[gr]:
                            cell_text = cell["text"].strip()
                            raw_value = None if year_anchors and gr == header_idx else _looks_numeric(cell_text)
                            meta: Dict[str, object] = {}
                            if statement_key in STATEMENT_HEADERS:
                                meta["statement_type"] = stype
                                meta["statement"] = statement_key
                            if cell["col"] in column_labels:
                                meta["column_label"] = column_labels[cell["col"]]
                                meta["period_kind"] = column_labels[cell["col"]]
                            if cell["col"] in version_labels:
                                meta["version_label"] = version_labels[cell["col"]]
                            if year_anchors and gr != header_idx and raw_value is not None:
                                period_year = _year_for_cell(cell, year_anchors)
                                if period_year is None:
                                    meta["period_year_unresolved"] = True
                                else:
                                    meta["period_year"] = period_year
                            facts.append(RawFact(
                                page=idx,
                                bbox=cell["bbox"],
                                section=statement_key if statement_key in STATEMENT_HEADERS else None,
                                table=statement_key,
                                row=global_row,
                                column=cell["col"],
                                raw_text=cell_text,
                                raw_value=raw_value,
                                kind=FACT_KIND_TABLE_CELL,
                                currency=currency,
                                unit=unit,
                                scope=scope,
                                meta=meta,
                            ))
                            page_cell_count += 1
                    logical_rows[statement_key] = next_global_row
                    if skip_first_row and t_idx == 0:
                        warnings.append(f"第 {idx} 页为续表，已跳过重复表头行。")
                events.append(ParsePageEvent(
                    page=idx, parser="table", status=FACT_STATUS_OK,
                    note=f"{page_cell_count} 单元格" + ("（续表）" if is_continuation else ""),
                ))
        return ParserOutput(document=doc, facts=facts, events=events, warnings=warnings)

    # -- heuristics ---------------------------------------------------------

    @staticmethod
    def _detect_statement(text: str) -> Optional[str]:
        best: Optional[str] = None
        for header in STATEMENT_HEADERS:
            if header in text:
                # Prefer the most specific (longest) header match.
                if best is None or len(header) > len(best):
                    best = header
        return best

    @staticmethod
    def _detect_money(text: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract the declared money unit / currency from a page or table header.

        The unit alternatives are ordered longest-first and the currency words are
        consumed explicitly: a greedy ``[\\u4e00-\\u9fa5]+`` label group used to
        swallow the ``万`` of ``单位：万元`` and then match only the trailing
        ``元``, i.e. it reported ``元`` for a 万元 table — a 10,000× error that
        CARD-02's unit normalisation would have faithfully propagated.
        """
        currency = "CNY"
        match = _MONEY_RE.search(text)
        if not match:
            return currency, None
        # Currency words may sit inside the match ("币种：人民币 单位：万元") or
        # immediately before it; look at both, never beyond the local phrase.
        declared = f"{match.group('currency') or ''}{text[max(0, match.start() - 8):match.start()]}"
        if "美元" in declared or "USD" in declared.upper():
            currency = "USD"
        elif "港元" in declared or "港币" in declared or "HKD" in declared.upper():
            currency = "HKD"
        return currency, match.group("unit")
