"""Parser package for the Document Intelligence Layer.

Each parser turns raw bytes into a :class:`ParserOutput` (a partial
``DocumentRecord`` + located ``RawFact`` rows + per-page events). The registry
promotes ``RawFact`` to ``ExtractedFact`` and fills document identity.

``build_parser`` selects a parser from the declared document type and the file
name/extension. Every parser fails *closed*: a missing dependency or an
unreadable page raises (or returns a ``failed`` page event) rather than
fabricating facts.
"""

from __future__ import annotations

from typing import Optional

from ..models import DocumentRecord, DocumentType, ParsePageEvent, ParserOutput, RawFact
from .base import (
    PDFPLUMBER_AVAILABLE,
    BaseParser,
    OCRUnavailable,
    ParserDependencyError,
    open_pdf_bytes,
)
from .pdf_text import PDFTextParser
from .table import TableParser
from .ocr import OCRFallback
from .financial_statement import FinancialStatementParser
from .announcement import AnnouncementParser
from .report_draft import ReportDraftParser
from .text import TextParser
from .excel import ExcelParser


def build_parser(doc_type: DocumentType | str, filename: str | None = None) -> BaseParser:
    """Choose a parser by declared type and (fallback) extension.

    ``DocumentType`` subclasses ``str``, so the enum check must come *first*;
    routing an already-coerced member through ``isinstance(x, str)`` would
    mis-handle it (see ``DocumentType.coerce``).
    """
    if not isinstance(doc_type, DocumentType):
        doc_type = DocumentType.coerce(doc_type)
    ext = (filename or "").lower().rsplit(".", 1)[-1] if filename else ""

    if ext in ("txt", "md", "csv"):
        return TextParser(declared_type=doc_type.value)
    if ext in ("xlsx", "xls"):
        return ExcelParser(declared_type=doc_type.value)

    # PDF (or unknown extension) -> type-specific parser.
    # The declared type is passed to *every* parser: otherwise the document is
    # built with the BaseParser default ("other") and the content-vs-declared
    # check in ``_apply_type_check`` fires a false mismatch warning on every
    # correctly-declared filing (CARD-01 edge case 3 must stay meaningful).
    if doc_type is DocumentType.ANNOUNCEMENT:
        return AnnouncementParser(declared_type=doc_type.value)
    if doc_type is DocumentType.REPORT_DRAFT:
        return ReportDraftParser(declared_type=doc_type.value)
    if doc_type in (DocumentType.ANNUAL, DocumentType.INTERIM, DocumentType.QUARTERLY):
        return FinancialStatementParser(declared_type=doc_type.value)
    # OTHER / fallback: full text + tables.
    return PDFTextParser(declared_type=doc_type.value)


__all__ = [
    "build_parser",
    "BaseParser",
    "ParserOutput",
    "ParserDependencyError",
    "OCRUnavailable",
    "PDFPLUMBER_AVAILABLE",
    "open_pdf_bytes",
    "PDFTextParser",
    "TableParser",
    "OCRFallback",
    "FinancialStatementParser",
    "AnnouncementParser",
    "ReportDraftParser",
    "TextParser",
    "ExcelParser",
    "DocumentRecord",
    "DocumentType",
    "ParsePageEvent",
    "RawFact",
]
