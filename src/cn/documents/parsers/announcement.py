"""Announcement parser: title / date / key paragraphs, located by page."""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import FACT_KIND_TEXT, FACT_STATUS_OK, RawFact
from .base import BaseParser, iter_pdf_pages, line_bbox, open_pdf_bytes

_TITLE_RE = re.compile(r"^(.{4,40}?)(公告|通知|报告书|提示性公告|报告)$")
_PUB_DATE_RE = re.compile(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})")
_COMPANY_RE = re.compile(r"([\u4e00-\u9fa5]{2,12}(?:股份)?有限公司|[\u4e00-\u9fa5]{2,12}公司)")


class AnnouncementParser(BaseParser):
    name = "announcement"

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> "ParserOutput":  # type: ignore[name-defined]
        from ..models import DocumentRecord, ParsePageEvent, ParserOutput
        with open_pdf_bytes(data) as pdf:
            pages = iter_pdf_pages(pdf)
            doc = self._new_document(page_count=len(pages))
            facts: List[RawFact] = []
            events: List[ParsePageEvent] = []
            warnings: List[str] = []
            title = date = company = None

            for idx, page in enumerate(pages, start=1):
                words = page.extract_words()
                if not words:
                    events.append(ParsePageEvent(page=idx, parser="announcement", status=FACT_STATUS_OK, note="空页"))
                    continue
                lines = line_bbox(words)
                for line in lines:
                    text = line["text"].strip()
                    if not text:
                        continue
                    facts.append(RawFact(
                        page=idx, bbox=line["bbox"], raw_text=text, kind=FACT_KIND_TEXT,
                        section="正文", meta={},
                    ))
                    if title is None:
                        m = _TITLE_RE.match(text)
                        if m:
                            title = m.group(1) + m.group(2)
                            facts[-1].section = "标题"
                            facts[-1].meta = {"role": "title"}
                    if date is None:
                        dm = _PUB_DATE_RE.search(text)
                        if dm:
                            date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
                            facts[-1].section = "日期"
                            facts[-1].meta = {"role": "publication_date"}
                    if company is None:
                        cm = _COMPANY_RE.search(text)
                        if cm:
                            company = cm.group(1)
                events.append(ParsePageEvent(page=idx, parser="announcement", status=FACT_STATUS_OK, note=f"{len(lines)} 行"))

            if title:
                doc.company = company
            if date:
                doc.publication_date = date
            if title:
                # A title strongly implies an announcement, not a report.
                self._apply_type_check(doc, "announcement", warnings)
        return ParserOutput(document=doc, facts=facts, events=events, warnings=warnings)
