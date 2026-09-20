"""Excel (xlsx/xls) parser — row/column located facts, no judgment."""

from __future__ import annotations

import io
from typing import List

from ..models import FACT_KIND_TABLE_CELL, FACT_STATUS_OK, RawFact
from .base import BaseParser, ParserDependencyError, looks_numeric


class ExcelParser(BaseParser):
    name = "excel"

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> "ParserOutput":  # type: ignore[name-defined]
        from ..models import DocumentRecord, ParsePageEvent, ParserOutput
        try:
            import openpyxl
            from openpyxl.utils import get_column_letter
        except Exception as exc:  # pragma: no cover
            raise ParserDependencyError("openpyxl 未安装，无法解析 Excel。") from exc

        doc = self._new_document(page_count=1)
        facts: List[RawFact] = []
        warnings: List[str] = []
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=0):
                for c_idx, cell in enumerate(row):
                    if cell is None:
                        continue
                    value = str(cell).strip()
                    if not value:
                        continue
                    facts.append(RawFact(
                        page=1, raw_text=value, raw_value=looks_numeric(value),
                        kind=FACT_KIND_TABLE_CELL,
                        table=f"sheet:{sheet}", row=r_idx, column=c_idx,
                        section=sheet, meta={"column_letter": _col_letter(c_idx)},
                    ))
        events = [ParsePageEvent(page=1, parser="excel", status=FACT_STATUS_OK, note=f"{len(facts)} 单元格")]
        _ = DocumentRecord
        return ParserOutput(document=doc, facts=facts, events=events, warnings=warnings)


def _col_letter(idx: int) -> str:
    try:
        from openpyxl.utils import get_column_letter
        return get_column_letter(idx + 1)
    except Exception:
        return str(idx + 1)
