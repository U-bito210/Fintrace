"""Base classes and shared helpers for document parsers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..models import DocumentRecord, ParsePageEvent, ParseResult, ParserOutput, RawFact

PARSER_VERSION = "1.0.0"

# A whole numeric cell: thousands separators, optional sign and an explicitly
# supported suffix.  This parser is intentionally stricter than CARD-02
# normalization: a number embedded in prose (a date, phone number or narrative
# sentence) must not become a financial fact merely because it appeared in a
# table cell.
NUMERIC_RE = re.compile(
    r"^\s*(?:人民币|RMB|CNY)?\s*"
    r"(?P<value>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?)\s*"
    r"(?:人民币|RMB|CNY)?\s*(?:亿元|万元|千元|百万|万股|亿股|元|股)?\s*$",
    re.IGNORECASE,
)


def looks_numeric(text: str) -> Optional[str]:
    """Return the numeric slice of ``text`` (raw string) or ``None``.

    Never converts or rounds — the verbatim string is preserved so no fact is
    invented downstream. Shared by the table, text and excel parsers so every
    ``table_cell`` fact carries a consistent ``raw_value``.
    """
    text = (text or "").strip()
    if not text:
        return None
    match = NUMERIC_RE.fullmatch(text)
    if match is None:
        return None
    # Preserve the numeric token, but remove grouping separators for the
    # downstream deterministic parser.  Units remain in ``raw_text`` and are
    # interpreted only by normalization with its own validation rules.
    return match.group("value").replace(",", "")


def resolve_textless_page(page_index: int, page, parser_name: str):
    """Handle a page with no extractable text layer (CARD-01 edge case 1).

    A-share filings are often scanned. The contract is fail-closed: try
    ``OCRFallback``; with no backend the page is marked ``failed`` and must
    contribute no facts, so the registry can degrade the document instead of it
    looking silently empty.

    Returns ``(event, warning, ocr_text)``; ``ocr_text`` is ``""`` unless a
    backend actually returned text.
    """
    from ..models import FACT_STATUS_FAILED, FACT_STATUS_PARTIAL, ParsePageEvent
    from .ocr import OCRFallback

    try:
        ocr_text = OCRFallback().ocr_page(page)
    except OCRUnavailable as exc:
        return (
            ParsePageEvent(
                page=page_index, parser=parser_name, status=FACT_STATUS_FAILED,
                note=f"扫描页：文本层为空且 OCR 不可用（{exc}）",
            ),
            f"第 {page_index} 页为扫描页，OCR 不可用，已降级（该页无事实）。",
            "",
        )
    except Exception as exc:  # pragma: no cover - backend runtime failure
        return (
            ParsePageEvent(
                page=page_index, parser=parser_name, status=FACT_STATUS_FAILED,
                note=f"扫描页 OCR 失败：{exc!r}",
            ),
            f"第 {page_index} 页 OCR 失败，该页无事实。",
            "",
        )
    if ocr_text:
        return (
            ParsePageEvent(
                page=page_index, parser=parser_name, status=FACT_STATUS_PARTIAL,
                note="OCR 文本（未做结构化）",
            ),
            f"第 {page_index} 页改用 OCR 文本，仅定位未结构化。",
            ocr_text,
        )
    return (
        ParsePageEvent(
            page=page_index, parser=parser_name, status=FACT_STATUS_FAILED,
                note="扫描页 OCR 未返回文本",
        ),
            f"第 {page_index} 页为扫描页，但 OCR 未返回文本。",
        "",
    )


class ParserDependencyError(RuntimeError):
    """A required parsing dependency (e.g. pdfplumber) is not installed."""


class OCRUnavailable(RuntimeError):
    """OCR backend (MinerU / PaddleOCR) is not installed."""


# pdfplumber is optional; parsers degrade gracefully when it is absent.
try:  # pragma: no cover - import guard
    import pdfplumber  # noqa: F401
    PDFPLUMBER_AVAILABLE = True
except Exception:  # pragma: no cover
    PDFPLUMBER_AVAILABLE = False


class BaseParser:
    """Common interface for all parsers."""

    name: str = "base"
    version: str = PARSER_VERSION

    def __init__(self, declared_type: str = "other"):
        self.declared_type = declared_type

    def parse_bytes(self, data: bytes, declared_type: Optional[str] = None) -> ParserOutput:
        raise NotImplementedError

    # -- helpers shared by PDF parsers --

    def _new_document(self, page_count: Optional[int], parser_name: Optional[str] = None) -> DocumentRecord:
        return DocumentRecord(
            document_id="",  # filled by registry
            sha256="",       # filled by registry
            filename="",
            source="",
            document_type=self.declared_type,
            ingested_at="",
            parser_name=parser_name or self.name,
            parser_version=self.version,
            page_count=page_count,
        )

    def _apply_type_check(self, doc: DocumentRecord, detected_type: str, warnings: List[str]) -> None:
        """Edge case 3: declared type disagrees with detected content."""
        if detected_type and detected_type != doc.document_type and detected_type != "other":
            doc.type_mismatch_warning = (
                f"声明类型 '{doc.document_type}' 与内容判定 '{detected_type}' 不符；按内容重新判定。"
            )
            warnings.append(doc.type_mismatch_warning)
            doc.document_type = detected_type


def open_pdf_bytes(data: bytes):
    """Open PDF bytes with pdfplumber, raising a clear error if unavailable.

    Accepts raw ``bytes`` (wrapped in ``BytesIO``) or a path. Returns a pdfplumber
    PDF object; the caller must close it (use as context manager).
    """
    if not PDFPLUMBER_AVAILABLE:
        raise ParserDependencyError(
            "pdfplumber 未安装，无法解析 PDF。请安装 pdfplumber 后重试（OCR 类文档可 partial）。"
        )
    import io
    import pdfplumber
    try:
        if isinstance(data, (bytes, bytearray)):
            return pdfplumber.open(io.BytesIO(bytes(data)))
        return pdfplumber.open(data)
    except Exception as exc:  # encrypted / corrupt / not a PDF
        raise ParserDependencyError(f"无法打开 PDF（可能加密或损坏）: {exc!r}") from exc


def iter_pdf_pages(pdf) -> List[Any]:
    """Return all pages (pdfplumber pages are lazy; materialize for stable indexing)."""
    return list(pdf.pages)


def line_bbox(words: List[Any], y_tolerance: float = 3.0) -> List[dict]:
    """Group pdfplumber words into text lines, each with a bounding box.

    Returns ``[{"text", "bbox": (x0,y0,x1,y1), "top"}]`` sorted by page order.
    Empty pages yield no lines (so a scan page can be detected as text-less).
    """
    if not words:
        return []
    words = sorted(words, key=lambda w: (round(w["top"] / y_tolerance), w["x0"]))
    lines: List[dict] = []
    current_key = None
    current: List[Any] = []
    for w in words:
        key = round(w["top"] / y_tolerance)
        if current_key is None or key == current_key:
            current.append(w)
            current_key = key
        else:
            lines.append(_pack_line(current))
            current = [w]
            current_key = key
    if current:
        lines.append(_pack_line(current))
    return lines


def _pack_line(words: List[Any]) -> dict:
    text = "".join(w["text"] for w in words)
    x0 = min(w["x0"] for w in words)
    y0 = min(w["top"] for w in words)
    x1 = max(w["x1"] for w in words)
    y1 = max(w["bottom"] for w in words)
    return {"text": text, "bbox": (x0, y0, x1, y1), "top": words[0]["top"]}
