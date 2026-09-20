"""Document Intelligence Layer for FinTrace-CN.

Unifies intake of real A-share financial materials (annual/interim/quarterly
reports, announcements, research-draft PDFs, TXT/MD/Excel/CSV). Every ingested
document is registered, parsed and located so a report figure can be traced back
to the exact PDF page (and bbox) it came from — the strongest form of
"source verifiable" the system can offer.

This module never calls a network, never treats document text as an instruction,
and fails closed: a fact that cannot be parsed simply does not exist.
"""

from .models import (
    DocumentRecord,
    DocumentType,
    ExtractedFact,
    ParsePageEvent,
    ParseResult,
    RawFact,
)
from .registry import DocumentRegistry
from .guard import (
    NetworkPolicyViolation,
    OfflineGuard,
    PathTraversalError,
    scan_injection,
    is_path_allowed,
)

__all__ = [
    "DocumentRecord",
    "DocumentType",
    "ExtractedFact",
    "ParsePageEvent",
    "ParseResult",
    "RawFact",
    "DocumentRegistry",
    "NetworkPolicyViolation",
    "OfflineGuard",
    "PathTraversalError",
    "scan_injection",
    "is_path_allowed",
]
