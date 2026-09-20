"""Read-only document service for the workbench UI and the HTTP API.

This is the read side of CARD-01's headline capability — **报告数字 → 原始 PDF 页**.
It wraps :class:`DocumentRegistry` and returns browser-safe payloads:

* ``stored_path`` (an absolute server path) is **never** returned. Callers get
  ``document_id`` + ``filename`` + ``page`` + ``bbox`` and fetch the bytes
  through :func:`document_source` or :func:`page_image`.
* A document whose file left the workspace allowlist, or whose bytes drifted
  from the registered ``sha256``, is reported and never previewed.
* Reading is offline: no provider, no network, no LLM.

Nothing here computes a financial value — figures come from the registry, which
stores what the parser actually located on the page.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .analysis import analysis_jumps, analyze_document
from .documents.guard import PathTraversalError, require_allowed_path
from .documents.models import (
    DOC_STATUS_DEGRADED,
    DOC_STATUS_ERROR,
    FACT_STATUS_FAILED,
    DocumentRecord,
    ExtractedFact,
)
from .documents.registry import DocumentRegistry
from .normalization import normalize_financials

PDF_MEDIA_TYPE = "application/pdf"

# Preview sources we can render a page image for.
_RENDERABLE_SUFFIXES = {".pdf"}
_VIEWABLE_SUFFIXES = {".pdf"}

_REGISTRY: Optional[DocumentRegistry] = None


class DocumentNotFound(KeyError):
    """The document_id is not registered."""


class FactNotFound(KeyError):
    """The fact_id is not registered for that document.

    Both subclass ``KeyError`` so callers that only care about "not found" keep
    working, while the API can still return a precise typed error.
    """


def get_registry() -> DocumentRegistry:
    """One process-wide registry, so ingested documents are visible to the UI.

    Sharing the instance matters: ``ingest_cn_document`` and
    ``normalize_cn_financials`` both default to the same
    ``data/documents/registry.db``, and the workbench must read that same store.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = DocumentRegistry()
    return _REGISTRY


def reset_registry() -> None:
    """Drop the cached registry (tests and explicit reloads)."""
    global _REGISTRY
    _REGISTRY = None


# ---------------------------------------------------------------------------
# payload builders — every one of these is path-free
# ---------------------------------------------------------------------------

def _document_summary(doc: DocumentRecord) -> Dict[str, Any]:
    return {
        "document_id": doc.document_id,
        "filename": doc.filename,
        "document_type": doc.document_type,
        "source": doc.source,
        "company": doc.company,
        "report_period": doc.report_period,
        "publication_date": doc.publication_date,
        "ingested_at": doc.ingested_at,
        "page_count": doc.page_count,
        "status": doc.status,
        "parser": f"{doc.parser_name}@{doc.parser_version}",
        "suspected_injection": doc.suspected_injection,
        "injection_segment_count": len(doc.injection_segments or []),
        "type_mismatch_warning": doc.type_mismatch_warning,
        "is_fact_source": doc.status not in {DOC_STATUS_DEGRADED, DOC_STATUS_ERROR},
    }


def _page_count(doc: DocumentRecord, facts: Sequence[ExtractedFact]) -> Optional[int]:
    if doc.page_count:
        return doc.page_count
    pages = [fact.page for fact in facts if fact.page]
    return max(pages) if pages else None


def _fact_payload(fact: ExtractedFact, context: Any = None, *, is_header: bool = False) -> Dict[str, Any]:
    """One located fact, with everything the UI needs to jump and highlight.

    ``row_label`` / ``column_label`` are reconstructed from the table context
    (CARD-02's :func:`build_table_context`) rather than read off the cell: a
    value cell's own text is the number, never its row name.
    """
    meta = fact.meta or {}
    invalidated = fact.status == FACT_STATUS_FAILED
    row_label = context.label(fact.row) if context is not None else None
    column_label = context.header_text(fact.column) if context is not None else None
    column_role = context.role(fact.column) if context is not None else "unknown"
    return {
        "fact_id": fact.fact_id,
        "page": fact.page,
        "bbox": list(fact.bbox) if fact.bbox else None,
        # Verbatim slice of the source row, so the panel never paraphrases.
        "raw_text": fact.raw_text,
        "raw_value": fact.raw_value,
        "normalized_value": fact.normalized_value,
        "row_label": row_label or fact.raw_text,
        "row_label_source": "table_context" if row_label else "cell",
        "table": fact.table,
        "section": fact.section,
        "row": fact.row,
        "column": fact.column,
        "column_label": column_label or meta.get("column_label"),
        "column_role": column_role,
        "is_header_cell": is_header,
        "unit": fact.unit,
        "currency": fact.currency,
        "period": fact.period,
        "scope": fact.scope,
        "available_at": fact.available_at,
        "status": fact.status,
        "kind": fact.kind,
        "invalidated": invalidated,
        # A fact with no page slot can never be located — never faked.
        "jumpable": (not invalidated) and fact.page is not None,
    }


def _table_contexts(facts: Sequence[ExtractedFact]) -> Dict[str, Any]:
    """Rebuild each logical table's headers/roles/row labels (CARD-02 reuse)."""
    from .normalization.pipeline import build_table_context

    grouped: Dict[str, List[ExtractedFact]] = {}
    for fact in facts:
        grouped.setdefault(str(fact.table or ""), []).append(fact)
    return {table: build_table_context(table, cells) for table, cells in grouped.items()}


def _context_for(contexts: Dict[str, Any], fact: ExtractedFact) -> Any:
    return contexts.get(str(fact.table or ""))


def _is_header_cell(context: Any, fact: ExtractedFact) -> bool:
    return (
        context is not None
        and context.header_row is not None
        and fact.row is not None
        and int(fact.row) == int(context.header_row)
    )


def _metric_hint(label: object) -> Dict[str, Any]:
    """Canonical metric for a disclosed row label, via CARD-02's dictionary."""
    from .normalization.metrics import resolve_metric

    match = resolve_metric(label)
    if match.spec is not None:
        return {"metric": match.spec.key, "metric_known": True, "metric_note": None}
    return {"metric": None, "metric_known": False, "metric_note": match.reason or "不在 §7.1 指标字典内"}


def list_documents(
    *, document_type: Optional[str] = None, company: Optional[str] = None
) -> Dict[str, Any]:
    registry = get_registry()
    items = []
    for doc in registry.list(document_type=document_type, company=company):
        summary = _document_summary(doc)
        summary["fact_count"] = len(registry.get_facts(doc.document_id))
        items.append(summary)
    return {"items": items, "count": len(items)}


def document_detail(document_id: str) -> Dict[str, Any]:
    """Metadata + parse events + guard status. Raises ``DocumentNotFound`` if unknown.

    Reading the detail runs the same integrity check as
    ``DocumentRegistry.verify_integrity``: a stored file whose sha256 drifted
    invalidates its facts before anything is reported, so the view can never
    advertise a location that CARD-01 has already ruled unusable.
    """
    registry = get_registry()
    doc = registry.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    integrity = registry.verify_integrity(document_id)
    # Read the facts *after* the integrity check, otherwise a tamper detected by
    # this very call would still be reported as jumpable.
    facts = registry.get_facts(document_id)
    events = registry.get_events(document_id)
    jumpable = sum(1 for fact in facts if fact.page is not None and fact.status != FACT_STATUS_FAILED)

    detail = _document_summary(doc)
    detail.update(
        {
            "fact_count": len(facts),
            "jumpable_fact_count": jumpable,
            "page_count": _page_count(doc, facts),
            "notes": list(doc.notes or []),
            "injection_segments": list(doc.injection_segments or []),
            "parse_events": [
                {"page": event.page, "parser": event.parser, "status": event.status, "note": event.note}
                for event in events
            ],
            "integrity": {
                "file_exists": integrity["exists"],
                "tampered": integrity["tampered"],
                "facts_invalidated": integrity["facts_invalidated"],
            },
            # Whether an embedded preview is even possible, without naming a path.
            "preview": _preview_capability(doc),
        }
    )
    return detail


def document_facts(
    document_id: str,
    *,
    page: Optional[int] = None,
    numeric_only: bool = True,
    include_header_cells: bool = False,
) -> Dict[str, Any]:
    """Located facts, each annotated with its row label and canonical metric.

    ``numeric_only`` keeps the table to the cells a reader would click — the
    disclosed numbers. Row-label and header cells stay in the registry and are
    still used to reconstruct labels; pass ``include_header_cells=True`` to see
    them in the payload as well.
    """
    registry = get_registry()
    doc = registry.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    all_facts = registry.get_facts(document_id)
    contexts = _table_contexts(all_facts)

    selected = [fact for fact in all_facts if page is None or fact.page == page]
    payloads: List[Dict[str, Any]] = []
    for fact in selected:
        context = _context_for(contexts, fact)
        header = _is_header_cell(context, fact)
        if header and not include_header_cells:
            continue
        if numeric_only and fact.raw_value is None:
            continue
        item = _fact_payload(fact, context, is_header=header)
        item.update(_metric_hint(item["row_label"]))
        payloads.append(item)
    payloads.sort(key=lambda item: (item["page"] or 0, item["row"] or 0, item["column"] or 0))
    return {
        "document_id": document_id,
        "filename": doc.filename,
        "page": page,
        "page_count": _page_count(doc, all_facts),
        "fact_count": len(payloads),
        "numeric_only": numeric_only,
        "facts": payloads,
    }


def _scope_signal_payload(signal: Any, registry: DocumentRegistry) -> Dict[str, Any]:
    """One CARD-04 signal, with every evidence id resolved to a page.

    The §14 banner has to say *which announcement, on which page, for which
    period* — a signal whose evidence cannot be located is reported with
    ``locatable: False`` rather than silently dropped, because an unprovable
    signal is exactly what the reader needs to be told about.
    """
    from .analysis import COMPARABILITY_LABELS, SIGNAL_TYPE_LABELS

    raw_type = getattr(signal, "signal_type", None)
    signal_type = str(getattr(raw_type, "value", raw_type) or "")
    raw_severity = getattr(signal, "severity", None)
    severity = str(getattr(raw_severity, "value", raw_severity) or "")
    raw_decision = getattr(signal, "comparability", None)
    decision = str(getattr(raw_decision, "value", raw_decision) or "")
    evidence: List[Dict[str, Any]] = []
    pages: List[int] = []
    for fact_id in getattr(signal, "evidence_ids", ()) or ():
        located = registry.locate_fact(str(getattr(signal, "document_id", "") or ""), fact_id)
        if located is None:
            evidence.append({"fact_id": fact_id, "page": None, "jumpable": False})
            continue
        page = located.get("page")
        if page and page not in pages:
            pages.append(int(page))
        evidence.append(
            {
                "fact_id": fact_id,
                "page": page,
                "bbox": located.get("bbox"),
                "raw_text": located.get("raw_text"),
                "jumpable": bool(located.get("jumpable")),
            }
        )
    return {
        "signal_type": signal_type,
        "label": SIGNAL_TYPE_LABELS.get(signal_type, signal_type),
        "decision": decision,
        "decision_label": COMPARABILITY_LABELS.get(decision, decision),
        "severity": severity,
        "periods_affected": [str(item) for item in getattr(signal, "periods_affected", ()) or ()],
        "surfaces": [str(item) for item in getattr(signal, "surfaces", ()) or ()],
        "detector_rule": str(getattr(signal, "detector_rule", "") or ""),
        "detail": str(getattr(signal, "detail", "") or ""),
        "document_id": str(getattr(signal, "document_id", "") or ""),
        "adjusted_data_available": bool(getattr(signal, "adjusted_data_available", False)),
        "adjusted_evidence_ids": [str(item) for item in getattr(signal, "adjusted_evidence_ids", ()) or ()],
        "pages": pages,
        "evidence": evidence,
        "locatable": all(item["jumpable"] for item in evidence) if evidence else False,
    }


def _comparability_banner(payload: Dict[str, Any], result: Any) -> Optional[Dict[str, Any]]:
    """§14 可比性警告横幅 data: what decision, why, and where to look.

    Returns ``None`` when CARD-04 found nothing, so the UI distinguishes *not
    assessed* (``comparability`` missing entirely) from *assessed and clean*
    (``comparability: None`` with an empty signal list). Both render as no
    banner, but only the former is a gap.
    """
    from .analysis import COMPARABILITY_LABELS

    decision = getattr(result, "comparability", None)
    if not decision:
        return None
    return {
        "decision": decision,
        "decision_label": COMPARABILITY_LABELS.get(decision, decision),
        "blocked": bool(getattr(result, "comparability_blocked", False)),
        "signal_types": sorted({item["signal_type"] for item in payload["scope_signals"]}),
        "periods_affected": sorted(
            {period for item in payload["scope_signals"] for period in item["periods_affected"]}
        ),
        "pages": sorted({page for item in payload["scope_signals"] for page in item["pages"]}),
        "message": (
            "该文档存在会计口径/重述事项：两端数据不可比，本次未输出同比百分比，仅保留水平值。"
            if getattr(result, "comparability_blocked", False)
            else "该文档存在会计口径/重述事项：比较数据取自调整后/重述后版本，直接与原始披露值比较将不可比。"
        ),
    }


def document_analysis(
    document_id: str, *, registry: Optional[DocumentRegistry] = None
) -> Dict[str, Any]:
    """CARD-02 + CARD-03 read model: located facts → analysed metrics.

    The UI must never recompute a financial value. This runs the *same*
    deterministic pipeline the HTTP API and the Agent tool use, then attaches the
    resolvable formula + operand chain to every row so §14's "每行可展开公式与输入
    证据" is data, not prose:

    * ``metrics[].calculation`` — formula, operands (with evidence IDs) and the
      registry's own recomputed value, or ``None`` for a level row that needed
      no calculation;
    * ``missing[]`` — every metric that could not be produced, with ``blocking``
      separating "the document does not disclose it" from "it does not apply to
      this kind of filing";
    * ``validation_errors[]`` — output of the registry's re-derivation check, so a
      broken formula surfaces in the UI instead of hiding behind a pretty table.

    CARD-04 adds ``comparability`` / ``scope_signals`` / ``comparability_banner``:
    when a 口径或重述 signal makes the two ends of a YoY non-comparable, the
    affected rows simply have no percentage, and the banner says why. The
    withholding is visible in the payload, not just in the absence of a number.

    ``registry`` may be injected so an Agent tool and the workbench share one
    read model while still using their own store.
    """
    registry = registry or get_registry()
    doc = registry.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    facts = registry.get_facts(document_id)

    normalization = normalize_financials(facts, doc)
    # ``documents=doc`` lets CARD-04 stamp the signal with the company name; the
    # facts alone cannot say which issuer they belong to.
    result = analyze_document(normalization, documents=doc)

    jumps = analysis_jumps(result)
    rows: List[Dict[str, Any]] = []
    for item in result.metrics:
        payload = item.to_dict()
        payload["calculation"] = jumps.get(item.calculation_id or "")
        rows.append(payload)
    rows.sort(key=lambda row: (str(row["metric"]), str(row["period_key"] or "")))

    validation_errors: List[str] = []
    if result.registry is not None:
        validation_errors = list(result.registry.validate())

    scope_signals = [
        _scope_signal_payload(signal, registry) for signal in result.scope_signals
    ]
    body: Dict[str, Any] = {
        "document_id": document_id,
        "filename": doc.filename,
        "document_type": doc.document_type,
        "company": doc.company,
        "report_period": result.report_period,
        "scope": result.scope,
        "version": result.version,
        "currency": result.currency,
        "data_complete": result.data_complete,
        "metric_count": len(rows),
        "metrics": rows,
        "signals": [signal.to_dict() for signal in result.signals],
        "missing": [item.to_dict() for item in result.missing],
        "warnings": list(result.warnings),
        "normalization_warnings": list(normalization.warnings),
        "validation_errors": validation_errors,
        "comparability": result.comparability,
        "comparability_blocked": result.comparability_blocked,
        "scope_signals": scope_signals,
    }
    body["comparability_banner"] = _comparability_banner(body, result)
    return body


def jump_target(document_id: str, fact_id: str) -> Dict[str, Any]:
    """The reverse-jump payload for one fact: page + bbox + verbatim source.

    Raises ``DocumentNotFound`` when the document is unknown, ``FactNotFound``
    when the fact is, and ``ValueError`` when it cannot be located — a location
    is never invented. The document is resolved first so an unknown document
    reports the document error rather than a misleading fact error.
    """
    registry = get_registry()
    doc = registry.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    located = registry.locate_fact(document_id, fact_id)
    if located is None:
        raise FactNotFound(fact_id)
    fact = registry.get_fact(document_id, fact_id)
    if fact is None:  # pragma: no cover - locate_fact already checked
        raise FactNotFound(fact_id)
    if not located["jumpable"]:
        reason = (
            "该事实已被完整性质检判为失效（文件被改动或缺失），不再提供定位。"
            if located["invalidated"]
            else "该事实没有页码，无法定位到原始页面。"
        )
        raise ValueError(reason)

    context = _context_for(_table_contexts(registry.get_facts(document_id)), fact)
    target = _fact_payload(fact, context, is_header=_is_header_cell(context, fact))
    target.update(_metric_hint(target["row_label"]))
    target["document"] = {
        "document_id": doc.document_id,
        "filename": doc.filename,
        "document_type": doc.document_type,
        "company": doc.company,
        "report_period": doc.report_period,
    }
    target["preview"] = _preview_capability(doc)
    return target


def _preview_capability(doc: DocumentRecord) -> Dict[str, Any]:
    """Can we stream this document for an embedded, page-targeted preview?"""
    suffix = Path(doc.filename or "").suffix.lower()
    viewable = suffix in _VIEWABLE_SUFFIXES
    renderable = suffix in _RENDERABLE_SUFFIXES
    available, reason = _source_path(doc)
    return {
        "embedded_pdf": bool(viewable and available),
        "page_image": bool(renderable and available),
        "reason": reason if not available else (None if viewable else f"{suffix or '未知格式'} 无法内嵌预览"),
    }


# ---------------------------------------------------------------------------
# byte access — gated by the workspace allowlist and the registered sha256
# ---------------------------------------------------------------------------

def _source_path(doc: DocumentRecord) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve the stored file, refusing anything outside the allowlist."""
    if not doc.stored_path:
        return None, "该文档以内存字节注册，没有可读取的本地文件。"
    try:
        resolved = require_allowed_path(doc.stored_path, get_registry().allow_roots)
    except PathTraversalError:
        return None, "该文档的存储路径不在工作区白名单内，已拒绝读取。"
    if not resolved.exists():
        return None, "该文档的原始文件已不存在。"
    return resolved, None


def document_source(document_id: str) -> Tuple[bytes, str]:
    """Raw bytes for the embedded viewer. Raises ``KeyError``/``ValueError``."""
    registry = get_registry()
    doc = registry.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    suffix = Path(doc.filename or "").suffix.lower()
    if suffix not in _VIEWABLE_SUFFIXES:
        raise ValueError(f"「{doc.filename}」不是 PDF，无法内嵌预览。")
    path, reason = _source_path(doc)
    if path is None:
        raise ValueError(reason or "原始文件不可读。")
    return path.read_bytes(), PDF_MEDIA_TYPE


def render_pdf_page(
    source: Path,
    page: int,
    *,
    highlights: Iterable[Sequence[float]] = (),
    resolution: int = 110,
) -> bytes:
    """Render one page of a PDF *file* to PNG, outlining each bbox.

    Split out of :func:`page_image` so a caller that holds a file rather than a
    registry record can render identically — CARD-15's demo pages own a separate
    read-only registry, and a second renderer would drift. A highlight that lands
    one rectangle off is worse than no highlight: it accuses the wrong number.

    ``bbox`` uses the same top-left origin and point units as pdfplumber, which
    is what the parsers stored — so the rectangle lands on the disclosed value
    without any re-derivation. Raises ``ValueError`` when the page is out of
    range.
    """
    import pdfplumber  # local import: heavy, and only needed by the UI
    from PIL import ImageDraw

    with pdfplumber.open(str(source)) as pdf:
        if page < 1 or page > len(pdf.pages):
            raise ValueError(f"页码 {page} 超出范围（共 {len(pdf.pages)} 页）。")
        pdf_page = pdf.pages[page - 1]
        rendered = pdf_page.to_image(resolution=resolution)
        image = rendered.original
        scale_x = image.width / float(pdf_page.width or 1.0)
        scale_y = image.height / float(pdf_page.height or 1.0)
        draw = ImageDraw.Draw(image)
        for bbox in highlights:
            try:
                x0, top, x1, bottom = (float(value) for value in bbox)
            except (TypeError, ValueError):
                continue
            draw.rectangle(
                [x0 * scale_x, top * scale_y, x1 * scale_x, bottom * scale_y],
                outline=(220, 38, 38),
                width=3,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def page_image(
    document_id: str,
    page: int,
    *,
    highlights: Iterable[Sequence[float]] = (),
    resolution: int = 110,
) -> bytes:
    """Render one PDF page to PNG, outlining each bbox in the fact's coordinates.

    ``bbox`` uses the same top-left origin and point units as pdfplumber, which
    is what the parsers stored — so the rectangle lands on the disclosed value
    without any re-derivation. Raises ``ValueError`` when the page is out of
    range or the format cannot be rendered.
    """
    registry = get_registry()
    doc = registry.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    path, reason = _source_path(doc)
    if path is None:
        raise ValueError(reason or "原始文件不可读。")
    if path.suffix.lower() not in _RENDERABLE_SUFFIXES:
        raise ValueError(f"「{doc.filename}」不是可渲染的 PDF。")
    return render_pdf_page(path, page, highlights=highlights, resolution=resolution)
