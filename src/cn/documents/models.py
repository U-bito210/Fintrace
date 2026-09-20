"""Data models for the Document Intelligence Layer.

These dataclasses are the contract between the registry, the parsers and the
rest of the system (CARD-02/05/06/09/12 depend on them). They follow the
CARD-01 specification exactly: a registered ``DocumentRecord`` plus a list of
located ``ExtractedFact`` objects, each carrying page / bbox so a number can be
jumped back to in the original PDF.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class DocumentType(str, Enum):
    """Supported intake document kinds (declared by the caller, re-checked by parsers)."""

    ANNUAL = "annual"
    INTERIM = "interim"
    QUARTERLY = "quarterly"
    ANNOUNCEMENT = "announcement"
    REPORT_DRAFT = "report_draft"
    OTHER = "other"

    @classmethod
    def coerce(cls, value: "DocumentType | str | None") -> "DocumentType":
        """Best-effort conversion of a declared type to a member.

        ``DocumentType`` mixes in ``str``, so ``isinstance(x, str)`` is True for
        members too. Never feed a member through ``str()``: on Python 3.11+
        ``str(DocumentType.ANNUAL)`` is ``"DocumentType.ANNUAL"``, not
        ``"annual"``, which would silently degrade to ``OTHER``.
        """
        if value is None:
            return cls.OTHER
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        if not text:
            return cls.OTHER
        # Accept both "annual" and the legacy "documenttype.annual" spelling.
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        try:
            return cls(text)
        except ValueError:
            return cls.OTHER


# Document-level status. ``error`` documents are never written to the registry.
DOC_STATUS_OK = "ok"
DOC_STATUS_PARTIAL = "partial"          # some pages/facts missing but usable
DOC_STATUS_DEGRADED = "degraded"        # available_at inferred; "仅供参考"
DOC_STATUS_ERROR = "error"              # corrupt/encrypted/too-large; not stored

# Per-fact / per-page status used by the fail-closed policy.
FACT_STATUS_OK = "ok"
FACT_STATUS_PARTIAL = "partial"
FACT_STATUS_FAILED = "failed"

# Fact kinds.
FACT_KIND_TEXT = "text"
FACT_KIND_TABLE_CELL = "table_cell"


@dataclass
class DocumentRecord:
    """A registered intake document. Immutable once stored except for audit flags."""

    document_id: str                     # internally generated (uuid)
    sha256: str                          # file fingerprint, dedup + tamper key
    filename: str
    source: str                         # user_upload | data_dir | admin_import
    document_type: str                  # DocumentType value
    company: Optional[str] = None       # best-effort from title/cover rules
    report_period: Optional[str] = None  # e.g. 2025FY / 2026Q1
    publication_date: Optional[str] = None
    ingested_at: str = ""               # ISO-8601 UTC
    parser_name: str = ""
    parser_version: str = ""
    page_count: Optional[int] = None

    # Audit / safety flags (Controlled Environment Guard).
    status: str = DOC_STATUS_OK
    suspected_injection: bool = False
    injection_segments: List[str] = field(default_factory=list)
    type_mismatch_warning: Optional[str] = None
    stored_path: Optional[str] = None    # absolute path if ingested from disk
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "document_id": self.document_id,
            "sha256": self.sha256,
            "filename": self.filename,
            "source": self.source,
            "document_type": self.document_type,
            "company": self.company,
            "report_period": self.report_period,
            "publication_date": self.publication_date,
            "ingested_at": self.ingested_at,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "page_count": self.page_count,
            "status": self.status,
            "suspected_injection": self.suspected_injection,
            "injection_segments": list(self.injection_segments),
            "type_mismatch_warning": self.type_mismatch_warning,
            "stored_path": self.stored_path,
            "notes": list(self.notes),
        }


@dataclass
class ExtractedFact:
    """A located, structured slice of a document.

    ``raw_text`` is the verbatim source slice (kept for human review); it is
    never overwritten by a normalized value. ``normalized_value`` is filled by
    CARD-02 and stays ``None`` here.
    """

    document_id: str
    raw_text: str
    page: Optional[int] = None
    bbox: Optional[Tuple[float, float, float, float]] = None  # (x0, y0, x1, y1)
    section: Optional[str] = None       # section / statement heading
    table: Optional[str] = None         # logical table id (multi-page merged)
    row: Optional[int] = None           # global row index in the logical table
    column: Optional[int] = None
    raw_value: Optional[str] = None     # raw numeric string if applicable
    normalized_value: Optional[float] = None  # filled by CARD-02
    currency: Optional[str] = None
    unit: Optional[str] = None
    period: Optional[str] = None
    scope: Optional[str] = None         # 合并 | 母公司
    available_at: Optional[str] = None
    status: str = FACT_STATUS_OK
    kind: str = FACT_KIND_TEXT
    fact_id: Optional[str] = None       # assigned by registry / extractor
    meta: Dict[str, object] = field(default_factory=dict)  # e.g. column_label, period_kind

    def to_dict(self) -> Dict[str, object]:
        return {
            "document_id": self.document_id,
            "fact_id": self.fact_id,
            "page": self.page,
            "bbox": list(self.bbox) if self.bbox else None,
            "section": self.section,
            "table": self.table,
            "row": self.row,
            "column": self.column,
            "raw_text": self.raw_text,
            "raw_value": self.raw_value,
            "normalized_value": self.normalized_value,
            "currency": self.currency,
            "unit": self.unit,
            "period": self.period,
            "scope": self.scope,
            "available_at": self.available_at,
            "status": self.status,
            "kind": self.kind,
            "meta": dict(self.meta),
        }


@dataclass
class ParsePageEvent:
    """Per-page parse outcome for the fail-closed audit log."""

    page: int
    parser: str
    status: str                         # ok | partial | failed
    note: Optional[str] = None


@dataclass
class ParseResult:
    """Aggregate output of a single document parse."""

    document: DocumentRecord
    facts: List[ExtractedFact] = field(default_factory=list)
    events: List[ParsePageEvent] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def fact_count(self) -> int:
        return len(self.facts)


@dataclass
class ParserOutput:
    """Parser return value: partial document + located raw facts + events.

    Parsers emit this; the registry promotes ``facts`` (``RawFact``) to
    ``ExtractedFact`` and fills document identity before storage.
    """

    document: DocumentRecord
    facts: List[RawFact] = field(default_factory=list)
    events: List[ParsePageEvent] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class RawFact:
    """Parser-internal fact before registry/normalization.

    Parsers emit ``RawFact``; ``fact_extraction`` promotes them to
    ``ExtractedFact`` (filling document-level metadata, validating, preserving
    ``raw_text``).
    """

    page: Optional[int] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    section: Optional[str] = None
    table: Optional[str] = None
    row: Optional[int] = None
    column: Optional[int] = None
    raw_text: str = ""
    raw_value: Optional[str] = None
    kind: str = FACT_KIND_TEXT
    currency: Optional[str] = None
    unit: Optional[str] = None
    scope: Optional[str] = None
    available_at: Optional[str] = None
    meta: Dict[str, object] = field(default_factory=dict)


def sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 of raw bytes (used for dedup + tamper detection)."""
    return hashlib.sha256(data).hexdigest()
