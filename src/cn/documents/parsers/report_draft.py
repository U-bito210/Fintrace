"""Research-draft parser: paragraph-level location only, no judgment.

Per CARD-01, a research draft is segmented and located by page; the parser makes
NO analytical judgment (that belongs to CARD-05). Tables are also captured as
cells so figures remain traceable, but nothing is interpreted.
"""

from __future__ import annotations

import re
from typing import List

from ..models import FACT_KIND_TABLE_CELL, FACT_KIND_TEXT, FACT_STATUS_OK, FACT_STATUS_PARTIAL, RawFact
from .base import BaseParser, iter_pdf_pages, looks_numeric, open_pdf_bytes
from .table import _reconstruct_grid


class ReportDraftParser(BaseParser):
    name = "report_draft"

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> "ParserOutput":  # type: ignore[name-defined]
        from ..models import DocumentRecord, ParsePageEvent, ParserOutput
        with open_pdf_bytes(data) as pdf:
            pages = iter_pdf_pages(pdf)
            doc = self._new_document(page_count=len(pages))
            facts: List[RawFact] = []
            events: List[ParsePageEvent] = []
            warnings: List[str] = []

            for idx, page in enumerate(pages, start=1):
                text = page.extract_text() or ""
                if not text.strip():
                    events.append(ParsePageEvent(page=idx, parser="report_draft", status=FACT_STATUS_PARTIAL, note="空页"))
                    continue
                paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
                for para in paragraphs:
                    facts.append(RawFact(
                        page=idx, raw_text=para, kind=FACT_KIND_TEXT,
                        section="正文", meta={"role": "paragraph"},
                    ))
                # Capture any embedded tables so figures stay locatable. Uses the
                # same word-layer grid reconstruction as TableParser — pdfplumber's
                # TableCell API is version-fragile and made this path silently
                # drop every figure.
                page_words = page.extract_words()
                for t_idx, table in enumerate(page.find_tables()):
                    table_bbox = getattr(table, "bbox", None)
                    if not table_bbox:
                        continue
                    for cell in _reconstruct_grid(page_words, table_bbox):
                        cell_text = (cell["text"] or "").strip()
                        if not cell_text:
                            continue
                        facts.append(RawFact(
                            page=idx, bbox=cell["bbox"],
                            section="正文", table=f"draft_p{idx}_t{t_idx}",
                            row=cell["row"], column=cell["col"], raw_text=cell_text,
                            raw_value=looks_numeric(cell_text),
                            kind=FACT_KIND_TABLE_CELL, meta={"role": "draft_table"},
                        ))
                events.append(ParsePageEvent(page=idx, parser="report_draft", status=FACT_STATUS_OK,
                                             note=f"{len(paragraphs)} 段"))
        _ = DocumentRecord  # imported for typing symmetry
        return ParserOutput(document=doc, facts=facts, events=events, warnings=warnings)
