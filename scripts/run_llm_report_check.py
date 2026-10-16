r"""Run the competition report-checking flow with a real configured LLM.

The model is used only to copy literals from the draft into structured Claims.
Reference filings, normalisation, calculations, error decisions and evidence links
remain deterministic.  This command makes no network call until a registered model
and its API key are explicitly supplied by the operator.

Example (PowerShell; values are deliberately not stored in the repository)::

    $env:DASHSCOPE_API_KEY = "..."
    python scripts/run_llm_report_check.py `
      --model qwen-plus --draft .\materials\draft.pdf `
      --annual .\materials\annual.pdf --as-of 2026-10-18 --out .\output\final_check.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# Match the workbench's local configuration behavior. ``override=False`` keeps
# a deliberately supplied process-level key authoritative, and this call never
# prints or serializes any secret.
load_dotenv(ROOT / ".env", override=False)

from src.agents.prompts import bundle_for
from src.cn.checker import ConfiguredClaimExtractionClient, acheck_draft
from src.cn.documents.registry import DocumentRegistry


def _add_files(registry: DocumentRegistry, paths: Iterable[Path], document_type: str) -> list[str]:
    """Ingest explicit reference files; their IDs become the audit scope."""
    ids: list[str] = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"材料不存在：{path}")
        result = registry.ingest(
            path.read_bytes(), document_type, filename=path.name, source="competition_cli"
        )
        ids.append(result.document.document_id)
    return ids


def _run_status(report: dict) -> tuple[str, str | None]:
    """Do not label a provider-wide extraction failure as a clean run.

    The checker correctly records failed batches as ``unextracted`` so it can
    still return a structurally valid report.  At the CLI boundary, however, an
    all-unextracted report is an unusable live-model run, not a successful
    competition result.  This status is about execution coverage; it never
    rewrites the checker's financial findings.
    """
    summary = dict(report.get("summary") or {})
    claims = int(summary.get("claims_checked") or 0)
    unextracted = int(summary.get("sentences_unextracted") or 0)
    if claims <= 0:
        return "blocked", "模型未产出任何可逐字对应的 Claim；本次未执行实质核查。"
    if unextracted:
        return "partial", f"仍有 {unextracted} 句未提取，未覆盖内容不能视为已核查。"
    return "ok", None


async def run(args: argparse.Namespace) -> dict:
    """Perform one real extraction + deterministic-check run, returning JSON-safe data."""
    if not args.draft.is_file():
        raise FileNotFoundError(f"草稿不存在：{args.draft}")
    with tempfile.TemporaryDirectory(prefix="fintrace_competition_") as tmp:
        registry = DocumentRegistry(root=Path(tmp) / "documents.sqlite")
        try:
            reference_ids: list[str] = []
            reference_ids += _add_files(registry, args.annual, "annual")
            reference_ids += _add_files(registry, args.interim, "interim")
            reference_ids += _add_files(registry, args.quarterly, "quarterly")
            reference_ids += _add_files(registry, args.announcement, "announcement")
            if not reference_ids:
                raise ValueError("至少提供一份参考披露材料（--annual/--interim/--quarterly/--announcement）。")
            draft = registry.ingest(
                args.draft.read_bytes(), "report_draft", filename=args.draft.name, source="competition_cli"
            )
            client = ConfiguredClaimExtractionClient(args.model)
            report = await acheck_draft(
                draft.document.document_id,
                registry=registry,
                reference_document_ids=reference_ids,
                as_of=args.as_of or None,
                llm_client=client,
                llm_batch_size=args.llm_batch_size,
            )
            status, reason = _run_status(report)
            return {
                "status": status,
                "reason": reason,
                "run_kind": "competition_report_check",
                "model": client.model_name,
                "prompt_bundle": bundle_for("report_checker"),
                "reference_document_ids": reference_ids,
                "report": report,
            }
        finally:
            registry.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="LLM Claim extraction + deterministic A-share report checking")
    result.add_argument("--model", required=True, help="src.llms.model_registry 中登记的模型名")
    result.add_argument("--draft", required=True, type=Path, help="待核查研报草稿 PDF")
    result.add_argument("--annual", action="append", type=Path, default=[], help="年报 PDF，可重复")
    result.add_argument("--interim", action="append", type=Path, default=[], help="半年报 PDF，可重复")
    result.add_argument("--quarterly", action="append", type=Path, default=[], help="季报 PDF，可重复")
    result.add_argument("--announcement", action="append", type=Path, default=[], help="公告 PDF，可重复")
    result.add_argument("--as-of", default="", help="草稿撰写时点（ISO-8601）；缺省则过期引用规则不运行")
    result.add_argument(
        "--llm-batch-size", type=int, default=3,
        help="每次发送给模型的草稿句数；默认 3，较大值可缩短运行时间但会增大单次超时风险",
    )
    result.add_argument("--out", type=Path, help="可选：将完整 JSON 结果写入此文件")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        payload = asyncio.run(run(args))
    except (FileNotFoundError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # provider errors retain type but never print secrets
        print(
            json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
