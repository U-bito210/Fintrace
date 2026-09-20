"""SQLite-backed document registry for the Document Intelligence Layer.

The registry is the single entry point for ingesting real financial materials.
It enforces the CARD-01 contracts:

* **sha256 dedup** — re-uploading the same bytes returns the existing id.
* **allowlist** — only workspace dirs (data/ output/ runs/) are readable.
* **fail-closed** — corrupt/encrypted/too-large docs are not stored.
* **injection audit** — heuristic hits are flagged, never silently dropped.
* **tamper detection** — a stored file whose sha256 drifted invalidates its facts.

The DB lives under ``data/documents/registry.db`` (git-ignored). No network.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from .guard import PathTraversalError, require_allowed_path, scan_audit_flags, scan_injection_flag
from .models import (
    DOC_STATUS_DEGRADED,
    DOC_STATUS_ERROR,
    DOC_STATUS_PARTIAL,
    FACT_STATUS_FAILED,
    FACT_STATUS_PARTIAL,
    DocumentRecord,
    DocumentType,
    ExtractedFact,
    ParsePageEvent,
    ParseResult,
    sha256_bytes,
)
from .parsers import build_parser
from .fact_extraction import promote_facts


MAX_FILE_BYTES = 200 * 1024 * 1024  # 200 MB hard limit

# CARD-01 §9 Failure Mode: once the failed-page ratio exceeds this, the whole
# document is demoted to "仅供参考，不可作为事实来源".
FAILED_PAGE_RATIO_THRESHOLD = 0.5


class DocumentRegistryError(RuntimeError):
    """Registry-level failure (e.g. path reject, parse fatal)."""


class DocumentRegistry:
    """Persists registered documents and their located facts in SQLite."""

    def __init__(self, root: str | Path | None = None, allow_roots: Optional[Iterable[str | Path]] = None):
        if root is None:
            root = Path(__file__).resolve().parents[3] / "data" / "documents"
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Always permit ingesting files from the registry's own data tree
        # (and its parent dir), in addition to any caller-supplied roots.
        roots = list(allow_roots) if allow_roots is not None else []
        roots.append(self.root)
        roots.append(self.root.parent)
        self.allow_roots = roots
        self.db_path = self.root / "registry.db"
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- schema -------------------------------------------------------------

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                filename TEXT,
                source TEXT,
                document_type TEXT,
                company TEXT,
                report_period TEXT,
                publication_date TEXT,
                ingested_at TEXT,
                parser_name TEXT,
                parser_version TEXT,
                page_count INTEGER,
                status TEXT,
                suspected_injection INTEGER,
                injection_segments TEXT,
                type_mismatch_warning TEXT,
                stored_path TEXT,
                notes TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                fact_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                page INTEGER,
                bbox TEXT,
                section TEXT,
                table_id TEXT,
                row_idx INTEGER,
                column_idx INTEGER,
                raw_text TEXT,
                raw_value TEXT,
                normalized_value REAL,
                currency TEXT,
                unit TEXT,
                period TEXT,
                scope TEXT,
                available_at TEXT,
                status TEXT,
                kind TEXT,
                meta TEXT,
                FOREIGN KEY (document_id) REFERENCES documents(document_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS parse_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                page INTEGER,
                parser TEXT,
                status TEXT,
                note TEXT,
                FOREIGN KEY (document_id) REFERENCES documents(document_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_facts_doc ON facts(document_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_sha ON documents(sha256)")
        self._conn.commit()

    # -- ingest -------------------------------------------------------------

    def ingest(
        self,
        path_or_bytes,
        declared_type: str | None = None,
        *,
        source: str = "user_upload",
        company: Optional[str] = None,
        filename: Optional[str] = None,
        fail_on_injection: bool = False,
    ) -> ParseResult:
        """Register and parse one document. Returns a :class:`ParseResult`.

        Accepts either a filesystem ``path`` (validated against the allowlist) or
        raw ``bytes`` (parsed in memory, not written to disk). On a fatal parse
        error the document is NOT stored and its status is ``error``.
        """
        stored_path: Optional[str] = None

        if isinstance(path_or_bytes, (str, Path)):
            resolved = require_allowed_path(path_or_bytes, self.allow_roots)
            data = resolved.read_bytes()
            stored_path = str(resolved)
            display_name = filename or resolved.name
        elif isinstance(path_or_bytes, (bytes, bytearray)):
            data = bytes(path_or_bytes)
            display_name = filename or "in-memory-bytes"
        else:
            raise DocumentRegistryError(f"不支持的输入类型: {type(path_or_bytes)}")

        # Size guard (edge case 5).
        if len(data) > MAX_FILE_BYTES:
            raise DocumentRegistryError(
                f"文件过大（{len(data)} 字节 > 200MB 上限），已拒绝。请拆分或压缩后重试。"
            )

        sha = sha256_bytes(data)
        existing = self.find_by_sha256(sha)
        if existing is not None:
            # Dedup (edge case 4): return the already-registered record.
            return ParseResult(
                document=existing,
                facts=self.get_facts(existing.document_id),
                events=self.get_events(existing.document_id),
                warnings=[f"重复上传：命中已有 document_id={existing.document_id}"],
            )

        doc_type = DocumentType.coerce(declared_type)
        parser = build_parser(doc_type, display_name)

        try:
            parsed = parser.parse_bytes(data, declared_type=doc_type.value)
        except Exception as exc:  # corrupt/encrypted PDF etc. -> fail closed
            failed = DocumentRecord(
                document_id=_new_id(),
                sha256=sha,
                filename=display_name,
                source=source,
                document_type=doc_type.value,
                ingested_at=_now(),
                parser_name=getattr(parser, "name", "unknown"),
                parser_version=getattr(parser, "version", "?"),
                status=DOC_STATUS_ERROR,
                stored_path=stored_path,
                notes=[f"解析失败: {exc!r}"],
            )
            return ParseResult(document=failed, facts=[], events=[], warnings=[failed.notes[0]])

        # Injection scan across all extracted text (Controlled Environment Guard).
        full_text = "\n".join(f.raw_text for f in parsed.facts if f.raw_text)
        suspected, segments = scan_injection_flag(full_text)
        if suspected:
            parsed.document.suspected_injection = True
            parsed.document.injection_segments = segments
            parsed.warnings.append(f"检测到疑似注入特征（{len(segments)} 处），已记录审计，解析未中断。")
            if fail_on_injection:
                parsed.document.status = DOC_STATUS_ERROR
                parsed.document.notes.append("fail_on_injection=True：疑似注入，拒绝入库。")
                return parsed

        # Audit-only signals (external links are normal in real filings, so they
        # are recorded but never mark the document as injected or block it).
        audit_flags = scan_audit_flags(full_text)
        if audit_flags:
            parsed.document.notes.append(
                f"审计记录：{len(audit_flags)} 处外链文本（不计入注入判定）。示例：{audit_flags[0][:80]}"
            )

        # Fill document-level defaults from parser.committed fields.
        parsed.document.document_id = _new_id()
        parsed.document.sha256 = sha
        parsed.document.filename = display_name
        parsed.document.source = source
        parsed.document.document_type = doc_type.value
        parsed.document.ingested_at = _now()
        parsed.document.stored_path = stored_path
        if company:
            parsed.document.company = company

        # Promote raw parser facts to ExtractedFact (registry validation included).
        facts = promote_facts(parsed.document, parsed.facts)

        # Document status: aggregate the per-page outcomes (§9 Failure Mode).
        # A failed-page ratio above the threshold demotes the whole document to
        # "仅供参考，不可作为事实来源"; any failure at all still marks it partial.
        total_pages = len(parsed.events) or (parsed.document.page_count or 0)
        failed_pages = sum(1 for e in parsed.events if e.status == FACT_STATUS_FAILED)
        partial_pages = sum(1 for e in parsed.events if e.status == FACT_STATUS_PARTIAL)
        if failed_pages and total_pages:
            ratio = failed_pages / total_pages
            if ratio > FAILED_PAGE_RATIO_THRESHOLD:
                parsed.document.status = DOC_STATUS_DEGRADED
                parsed.document.notes.append(
                    f"{failed_pages}/{total_pages} 页解析失败（比例 {ratio:.0%} > 阈值 "
                    f"{FAILED_PAGE_RATIO_THRESHOLD:.0%}）：文档仅供参考，不可作为事实来源。"
                )
            else:
                parsed.document.status = DOC_STATUS_PARTIAL
                parsed.document.notes.append(
                    f"{failed_pages}/{total_pages} 页解析失败，文档状态 partial。"
                )
        elif total_pages and partial_pages == total_pages:
            parsed.document.status = DOC_STATUS_PARTIAL
            parsed.document.notes.append("所有页均为 partial，文档仅供参考。")

        self._store(parsed.document, facts, parsed.events)
        return ParseResult(document=parsed.document, facts=facts, events=parsed.events, warnings=parsed.warnings)

    # -- storage ------------------------------------------------------------

    def _store(self, doc: DocumentRecord, facts: List[ExtractedFact], events: List[ParsePageEvent]) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO documents (
                document_id, sha256, filename, source, document_type, company,
                report_period, publication_date, ingested_at, parser_name,
                parser_version, page_count, status, suspected_injection,
                injection_segments, type_mismatch_warning, stored_path, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                doc.document_id, doc.sha256, doc.filename, doc.source, doc.document_type,
                doc.company, doc.report_period, doc.publication_date, doc.ingested_at,
                doc.parser_name, doc.parser_version, doc.page_count, doc.status,
                1 if doc.suspected_injection else 0,
                json.dumps(doc.injection_segments, ensure_ascii=False),
                doc.type_mismatch_warning, doc.stored_path,
                json.dumps(doc.notes, ensure_ascii=False),
            ),
        )
        for fact in facts:
            cur.execute(
                """
                INSERT OR REPLACE INTO facts (
                    fact_id, document_id, page, bbox, section, table_id, row_idx,
                    column_idx, raw_text, raw_value, normalized_value, currency,
                    unit, period, scope, available_at, status, kind, meta
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fact.fact_id, fact.document_id, fact.page,
                    json.dumps(list(fact.bbox)) if fact.bbox else None,
                    fact.section, fact.table, fact.row, fact.column,
                    fact.raw_text, fact.raw_value, fact.normalized_value,
                    fact.currency, fact.unit, fact.period, fact.scope,
                    fact.available_at, fact.status, fact.kind,
                    json.dumps(fact.meta, ensure_ascii=False),
                ),
            )
        for ev in events:
            cur.execute(
                "INSERT INTO parse_events (document_id, page, parser, status, note) VALUES (?,?,?,?,?)",
                (doc.document_id, ev.page, ev.parser, ev.status, ev.note),
            )
        self._conn.commit()

    # -- queries ------------------------------------------------------------

    def get(self, document_id: str) -> Optional[DocumentRecord]:
        row = self._conn.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        return _row_to_document(row) if row else None

    def find_by_sha256(self, sha256: str) -> Optional[DocumentRecord]:
        row = self._conn.execute("SELECT * FROM documents WHERE sha256=?", (sha256,)).fetchone()
        return _row_to_document(row) if row else None

    def list(self, document_type: Optional[str] = None, company: Optional[str] = None) -> List[DocumentRecord]:
        sql = "SELECT * FROM documents WHERE 1=1"
        params: list = []
        if document_type:
            sql += " AND document_type=?"
            params.append(document_type)
        if company:
            sql += " AND company=?"
            params.append(company)
        sql += " ORDER BY ingested_at DESC"
        return [_row_to_document(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_facts(self, document_id: str) -> List[ExtractedFact]:
        rows = self._conn.execute("SELECT * FROM facts WHERE document_id=?", (document_id,)).fetchall()
        return [_row_to_fact(r) for r in rows]

    def get_events(self, document_id: str) -> List[ParsePageEvent]:
        rows = self._conn.execute(
            "SELECT * FROM parse_events WHERE document_id=? ORDER BY page", (document_id,)
        ).fetchall()
        return [ParsePageEvent(page=r["page"], parser=r["parser"], status=r["status"], note=r["note"]) for r in rows]

    def get_fact(self, document_id: str, fact_id: str) -> Optional[ExtractedFact]:
        row = self._conn.execute(
            "SELECT * FROM facts WHERE document_id=? AND fact_id=?", (document_id, fact_id)
        ).fetchone()
        return _row_to_fact(row) if row else None

    def locate_fact(self, document_id: str, fact_id: str) -> Optional[dict]:
        """Reverse-jump payload for one fact: the number → its source page slot.

        This is the data side of CARD-01's headline capability (报告数字 →
        原始 PDF 页): given a fact, return the document it came from, the page,
        the bbox to highlight, and the verbatim source slice. The UI (CARD-15)
        only has to render it. Returns ``None`` when the fact is unknown or was
        invalidated (never a fabricated location).
        """
        fact = self.get_fact(document_id, fact_id)
        if fact is None:
            return None
        doc = self.get(document_id)
        if doc is None:
            return None
        invalidated = fact.status == FACT_STATUS_FAILED
        return {
            "document_id": document_id,
            "fact_id": fact_id,
            "filename": doc.filename,
            "document_type": doc.document_type,
            "company": doc.company,
            "report_period": doc.report_period,
            "page": fact.page,
            "bbox": list(fact.bbox) if fact.bbox else None,
            "raw_text": fact.raw_text,
            "raw_value": fact.raw_value,
            "table": fact.table,
            "row": fact.row,
            "column": fact.column,
            "unit": fact.unit,
            "currency": fact.currency,
            "period": fact.period,
            "scope": fact.scope,
            "available_at": fact.available_at,
            "stored_path": doc.stored_path,
            "invalidated": invalidated,
            "jumpable": (not invalidated) and fact.page is not None,
        }

    # -- integrity ----------------------------------------------------------

    def verify_integrity(self, document_id: str) -> dict:
        """Recompute the stored file's sha256 and compare to the registry.

        If the file was modified (tampered) or is missing, every fact of this
        document is reported ``failed`` per validator rule 3.
        """
        doc = self.get(document_id)
        if doc is None:
            return {"document_id": document_id, "exists": False, "tampered": None, "facts_invalidated": 0}
        if not doc.stored_path:
            # In-memory ingest: nothing to re-hash; consider intact.
            return {"document_id": document_id, "exists": True, "tampered": False, "facts_invalidated": 0}
        path = Path(doc.stored_path)
        if not path.exists():
            return {"document_id": document_id, "exists": False, "tampered": True, "facts_invalidated": len(self.get_facts(document_id))}
        current = sha256_bytes(path.read_bytes())
        tampered = current != doc.sha256
        if tampered:
            # Invalidate facts in the DB.
            self._conn.execute(
                "UPDATE facts SET status=? WHERE document_id=?", (FACT_STATUS_FAILED, document_id)
            )
            self._conn.commit()
        return {
            "document_id": document_id,
            "exists": True,
            "tampered": tampered,
            "facts_invalidated": len(self.get_facts(document_id)) if tampered else 0,
        }

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    import uuid
    return f"doc_{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_document(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        document_id=row["document_id"],
        sha256=row["sha256"],
        filename=row["filename"],
        source=row["source"],
        document_type=row["document_type"],
        company=row["company"],
        report_period=row["report_period"],
        publication_date=row["publication_date"],
        ingested_at=row["ingested_at"],
        parser_name=row["parser_name"],
        parser_version=row["parser_version"],
        page_count=row["page_count"],
        status=row["status"],
        suspected_injection=bool(row["suspected_injection"]),
        injection_segments=json.loads(row["injection_segments"] or "[]"),
        type_mismatch_warning=row["type_mismatch_warning"],
        stored_path=row["stored_path"],
        notes=json.loads(row["notes"] or "[]"),
    )


def _row_to_fact(row: sqlite3.Row) -> ExtractedFact:
    bbox = json.loads(row["bbox"]) if row["bbox"] else None
    return ExtractedFact(
        document_id=row["document_id"],
        fact_id=row["fact_id"],
        page=row["page"],
        bbox=tuple(bbox) if bbox else None,
        section=row["section"],
        table=row["table_id"],
        row=row["row_idx"],
        column=row["column_idx"],
        raw_text=row["raw_text"],
        raw_value=row["raw_value"],
        normalized_value=row["normalized_value"],
        currency=row["currency"],
        unit=row["unit"],
        period=row["period"],
        scope=row["scope"],
        available_at=row["available_at"],
        status=row["status"],
        kind=row["kind"],
        meta=json.loads(row["meta"] or "{}"),
    )
