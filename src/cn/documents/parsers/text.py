"""Plain-text / Markdown / CSV parser.

These formats have no pages; we model the whole file as a single "page" (page=1)
so the reverse-jump contract (fact -> page) stays uniform, and locate rows by
line number. CSV is parsed into row/column cells. No judgment is made.
"""

from __future__ import annotations

import csv
import io
from typing import List

from ..models import FACT_KIND_TABLE_CELL, FACT_KIND_TEXT, FACT_STATUS_OK, RawFact
from .base import BaseParser, looks_numeric


class TextParser(BaseParser):
    name = "text"

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> "ParserOutput":  # type: ignore[name-defined]
        from ..models import DocumentRecord, ParsePageEvent, ParserOutput
        doc = self._new_document(page_count=1)
        facts: List[RawFact] = []
        warnings: List[str] = []
        text = data.decode("utf-8", errors="replace")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        is_csv = declared_type == "csv" or (declared_type == "other" and "," in first_line)

        if is_csv:
            try:
                reader = csv.reader(io.StringIO(text))
                rows = list(reader)
            except Exception as exc:
                warnings.append(f"CSV 解析失败: {exc!r}")
                rows = [line.split(",") for line in text.splitlines()]
            for r_idx, row in enumerate(rows):
                for c_idx, cell in enumerate(row):
                    cell = cell.strip()
                    if not cell:
                        continue
                    facts.append(RawFact(
                        page=1, raw_text=cell, raw_value=looks_numeric(cell),
                        kind=FACT_KIND_TABLE_CELL,
                        table="csv", row=r_idx, column=c_idx, section="csv",
                    ))
        else:
            for line_no, line in enumerate(text.splitlines(), start=1):
                line = line.rstrip()
                if not line.strip():
                    continue
                facts.append(RawFact(
                    page=1, raw_text=line, raw_value=looks_numeric(line),
                    kind=FACT_KIND_TEXT,
                    section="text", row=line_no, meta={"line": line_no},
                ))
        events = [ParsePageEvent(page=1, parser="text", status=FACT_STATUS_OK, note=f"{len(facts)} 项")]
        _ = DocumentRecord
        return ParserOutput(document=doc, facts=facts, events=events, warnings=warnings)
