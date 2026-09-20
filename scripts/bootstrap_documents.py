#!/usr/bin/env python3
"""Register the bundled synthetic filings so the workbench has documents to show.

Copies the version-controlled PDFs from ``tests/fixtures/documents`` into the
git-ignored ``data/documents/`` store, then ingests them through the real
:class:`DocumentRegistry`. Everything happens inside the workspace allowlist, so
the preview and the reverse jump (报告数字 → 原始 PDF 页) work afterwards.

The file list comes from the corpus **manifest**, not a hand-maintained tuple: a
hard-coded list silently went stale when the corpus grew, so the demo store kept
missing the newest filings (including every restatement sample CARD-04 needs to
show). Declared types come from the same manifest, which is also what the tests
ingest with — the registry refuses to guess a type from a filename.

Re-running is safe: the registry dedups by sha256 and the copy is idempotent.

Usage::

    python scripts/bootstrap_documents.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cn.documents.registry import DocumentRegistry  # noqa: E402

SOURCE_DIR = ROOT / "tests" / "fixtures" / "documents"
STORE_DIR = ROOT / "data" / "documents"
MANIFEST = SOURCE_DIR / "manifest.json"


def load_documents() -> tuple[list[tuple[str, str]], list[str]]:
    """``[(filename, declared_type)]`` from the corpus manifest, plus problems."""
    if not MANIFEST.is_file():
        return [], [f"语料清单缺失：{MANIFEST}（先运行 scripts/gen_document_fixtures.py）"]
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"语料清单无法解析：{exc}"]
    documents: list[tuple[str, str]] = []
    problems: list[str] = []
    for entry in manifest.get("documents") or []:
        name = str(entry.get("filename") or "").strip()
        declared = str(entry.get("document_type") or "").strip()
        if not name or not declared:
            problems.append(f"清单条目缺少 filename/document_type：{entry!r}")
            continue
        documents.append((name, declared))
    return documents, problems


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(f"Fixture directory missing: {SOURCE_DIR}", file=sys.stderr)
        return 1
    documents, problems = load_documents()
    if not documents:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    registry = DocumentRegistry(root=STORE_DIR)

    created, deduped, failed = 0, 0, list(problems)
    for name, declared in documents:
        source = SOURCE_DIR / name
        if not source.exists():
            failed.append((name, "fixture missing"))
            continue
        stored = STORE_DIR / name
        if not stored.exists() or stored.read_bytes() != source.read_bytes():
            shutil.copy2(source, stored)
        try:
            result = registry.ingest(stored, declared_type=declared, source="data_dir")
        except Exception as exc:  # noqa: BLE001 - report, never hide
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            continue
        marker = "exists" if any("重复上传" in warning for warning in result.warnings) else "new"
        if marker == "new":
            created += 1
        else:
            deduped += 1
        print(
            f"[{result.document.status:>8}] {marker:<6} {name} "
            f"-> {result.document.document_id} ({len(result.facts)} facts, {result.document.page_count or '?'} pages)"
        )

    print(f"\nregistered: {created} new, {deduped} already present, {len(failed)} failed")
    for name, reason in failed:
        print(f"  - {name}: {reason}")
    return 1 if failed and not (created or deduped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
