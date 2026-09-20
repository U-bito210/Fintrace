"""Financial statement parser (annual / interim / quarterly reports).

Reuses :class:`TableParser` for the three-statement extraction and adds cover
extraction: company name, report period and publication date from the title
page. Edge case 6 (合并/母公司 dual tables) is handled by the header rules in
``TableParser`` — both scopes are emitted and tagged separately.
"""

from __future__ import annotations

import re
from typing import Optional

from ..models import DocumentType, RawFact
from .base import BaseParser, iter_pdf_pages, open_pdf_bytes
from .notes import extract_body_text_facts
from .table import TableParser

_COMPANY_RE = re.compile(r"([\u4e00-\u9fa5]{2,12}(?:股份)?有限公司|[\u4e00-\u9fa5]{2,12}公司)")
_REPORT_PERIOD_RE = re.compile(
    r"(20\d{2})\s*年\s*(半年度|第一季度|第二季度|第三季度|前三季度|年度报告|中期报告|报告)"
)
_PERIOD_TAG = {
    "年度报告": "FY", "半年度报告": "H1", "中期报告": "H1", "半年度": "H1",
    "第一季度报告": "Q1", "第一季度": "Q1",
    "第三季度报告": "9M", "前三季度": "9M",
    "第二季度报告": "Q2", "第二季度": "Q2",
}
_PUB_DATE_RE = re.compile(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})")


class FinancialStatementParser(TableParser):
    name = "financial_statement"

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> "ParserOutput":  # type: ignore[name-defined]
        from ..models import ParserOutput
        output = super().parse_bytes(data, declared_type=declared_type)
        with open_pdf_bytes(data) as pdf:
            pages = iter_pdf_pages(pdf)
            cover_text = "\n".join((p.extract_text() or "") for p in pages[:3])
            # Body / note text (CARD-01 → CARD-04). ``TableParser`` keeps only
            # cells, which left every annual report with zero ``text`` facts and
            # therefore no note content for scope-signal detection to read.
            body_facts = extract_body_text_facts(pages)
        output.facts.extend(body_facts)
        if body_facts:
            # Recorded, not silent: the caller can tell how much of the filing is
            # now located as prose rather than tabulated.
            #
            # The wording is deliberately free of disclosure vocabulary ("重述",
            # "更正", "追溯调整"…). ``DocumentRecord.notes`` is scanned by CARD-02's
            # ``_document_blob`` to label the *document's* disclosure version, so a
            # bookkeeping sentence containing such a word would stamp every fact
            # in an otherwise clean filing RESTATED. This note describes the
            # parser's work; it is not something the filing declared.
            output.document.notes.append(
                f"已定位正文与附注段落 {len(body_facts)} 行（含页内章节归属）。"
            )
        company = self._extract_company(cover_text)
        period = self._extract_period(cover_text)
        pub_date = self._extract_pub_date(cover_text)
        if company:
            output.document.company = company
        if period:
            output.document.report_period = period
            detected_type = self._period_to_type(period)
            self._apply_type_check(output.document, detected_type, output.warnings)
        if pub_date:
            output.document.publication_date = pub_date
        return output

    @staticmethod
    def _extract_company(text: str) -> Optional[str]:
        m = _COMPANY_RE.search(text)
        return m.group(1) if m else None

    @staticmethod
    def _extract_period(text: str) -> Optional[str]:
        m = _REPORT_PERIOD_RE.search(text)
        if not m:
            return None
        year = m.group(1)
        kind = m.group(2) or "年度报告"
        tag = "FY"
        for key, value in _PERIOD_TAG.items():
            if key in kind:
                tag = value
                break
        return f"{year}{tag}"

    @staticmethod
    def _period_to_type(period: str) -> str:
        if period.endswith("FY"):
            return DocumentType.ANNUAL.value
        if period.endswith("H1") or period.endswith("Q2"):
            return DocumentType.INTERIM.value
        return DocumentType.QUARTERLY.value

    @staticmethod
    def _extract_pub_date(text: str) -> Optional[str]:
        m = _PUB_DATE_RE.search(text)
        if not m:
            return None
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
