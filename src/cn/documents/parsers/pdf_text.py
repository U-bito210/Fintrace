"""PDF text parser: page + per-line bbox extraction, scan-page degradation.

This is the generic PDF parser (used for ``other``/unknown types). It also
implements edge case 1: a page with no text layer triggers ``OCRFallback``; if
OCR is unavailable the page is marked ``failed`` and contributes no facts (the
document degrades to ``partial`` but is never silently empty).
"""

from __future__ import annotations

from typing import List, Optional

from ..models import FACT_KIND_TEXT, FACT_STATUS_OK, RawFact
from .base import (
    BaseParser,
    iter_pdf_pages,
    line_bbox,
    open_pdf_bytes,
    resolve_textless_page,
)
from .ocr import OCRFallback


class PDFTextParser(BaseParser):
    name = "pdf_text"

    def __init__(self, declared_type: str = "other"):
        super().__init__(declared_type)
        self._ocr = OCRFallback()

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> "ParserOutput":  # type: ignore[name-defined]
        from ..models import ParserOutput
        with open_pdf_bytes(data) as pdf:
            pages = iter_pdf_pages(pdf)
            doc = self._new_document(page_count=len(pages))
            facts: List[RawFact] = []
            events = []
            warnings: List[str] = []
            for idx, page in enumerate(pages, start=1):
                words = page.extract_words()
                if not words:
                    # Scan page (no text layer) -> OCR attempt; unavailable means
                    # the page fails and contributes no facts (fail-closed).
                    event, warning, ocr_text = resolve_textless_page(idx, page, "pdf_text")
                    events.append(event)
                    warnings.append(warning)
                    if ocr_text:
                        facts.append(RawFact(page=idx, raw_text=ocr_text, kind=FACT_KIND_TEXT,
                                             meta={"ocr": True}))
                    continue
                lines = line_bbox(words)
                for line in lines:
                    facts.append(RawFact(
                        page=idx,
                        bbox=line["bbox"],
                        raw_text=line["text"],
                        kind=FACT_KIND_TEXT,
                    ))
                events.append(self._event(idx, FACT_STATUS_OK, f"{len(lines)} 行文本"))
        return ParserOutput(document=doc, facts=facts, events=events, warnings=warnings)

    @staticmethod
    def _event(page: int, status: str, note: Optional[str]) -> object:
        from ..models import ParsePageEvent
        return ParsePageEvent(page=page, parser="pdf_text", status=status, note=note)
