#!/usr/bin/env python3
"""CARD-11 — acquire the real-tier filings for ``cn_agent_v2``.

This script deliberately does **nothing clever** without a network and an
explicit authorization note:

* it tries the sources in order (巨潮 → 交易所 → 公司官网);
* it verifies the downloaded bytes against a sha256 recorded in
  ``real_acquisition.json``;
* on any failure it exits non-zero with the reason and writes **no placeholder**
  — a stub PDF would poison every downstream number (CARD-11 §8 edge 1).

Copyrighted filings never enter Git (`data/` is gitignored); only the sha256
manifest and the acquisition record are meant to be committed.

Usage::

    python scripts/fetch_benchmark_real.py --symbol 600519.SH --document annual --year 2024
    python scripts/fetch_benchmark_real.py --verify          # checksum only, no download
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cn.benchmark_v2 import REAL_FILENAME_PREFIX, TIER_HOLDOUT, TIER_REAL  # noqa: E402

REAL_ROOT = PROJECT_ROOT / "data" / "benchmark_real"
ACQUISITION_RECORD = REAL_ROOT / "real_acquisition.json"
MANIFEST = REAL_ROOT / "manifest.sha256"

#: Source templates, tried in order. ``{symbol}`` is the exchange-qualified code
#: and ``{year}`` the fiscal year. These are the public disclosure portals; none
#: of them is scraped here automatically — an operator supplies the direct URL
#: via ``--url`` when the portal needs a session.
SOURCES: Dict[str, List[str]] = {
    "cninfo": ["https://www.cninfo.com.cn/new/hisAnnouncement/query"],
    "sse": ["https://www.sse.com.cn/disclosure/listedinfo/announcement/"],
    "szse": ["https://www.szse.cn/disclosure/listed/notice/"],
}


class AcquisitionError(RuntimeError):
    """Raised when a filing cannot be obtained and verified."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_manifest(*, root: Path = REAL_ROOT) -> Dict[str, object]:
    """Verify every file listed in ``manifest.sha256``; report missing/extra."""
    if not MANIFEST.exists():
        raise AcquisitionError(
            f"缺少 {MANIFEST}：没有 sha256 清单就无法证明本地文件与来源一致。"
        )
    expected: Dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        if not digest or not name:
            raise AcquisitionError(f"manifest.sha256 行格式错误：{line!r}")
        expected[name.strip()] = digest.strip()

    mismatched: List[str] = []
    missing: List[str] = []
    present: List[str] = []
    for name, digest in sorted(expected.items()):
        path = root / name
        if not path.exists():
            missing.append(name)
            continue
        actual = sha256_of(path)
        if actual != digest:
            mismatched.append(f"{name}（期望 {digest[:12]}…，实际 {actual[:12]}…）")
        else:
            present.append(name)

    ok = not mismatched
    return {
        "ok": ok,
        "verified": present,
        "missing": missing,
        "mismatched": mismatched,
        "note": "missing 不等于失败：清单可先于下载提交。" if missing and ok else "",
    }


def record_acquisition(entry: Dict[str, object], *, root: Path = REAL_ROOT) -> None:
    """Append one acquisition record (and its sha256 line) — append-only."""
    root.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, object] = {"schema_version": 1, "records": []}
    if ACQUISITION_RECORD.exists():
        existing = json.loads(ACQUISITION_RECORD.read_text(encoding="utf-8"))
    existing.setdefault("records", [])
    existing["records"].append(entry)  # type: ignore[union-attr]
    ACQUISITION_RECORD.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines: List[str] = []
    if MANIFEST.exists():
        lines = [line for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]
    name = str(entry["filename"])
    digest = str(entry["sha256"])
    lines = [line for line in lines if not line.endswith(f"  {name}")]
    lines.append(f"{digest}  {name}")
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")


def download(url: str, target: Path) -> str:
    """Fetch ``url`` to ``target``; return the sha256. Never writes on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": "FinTrace-CN benchmark/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - operator-supplied URL
            payload = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AcquisitionError(f"下载失败（{url}）：{exc}") from exc
    if not payload:
        raise AcquisitionError(f"下载得到空文件（{url}）——拒绝写入占位内容。")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return sha256_of(target)


def build_target(document_id: str, *, tier: str, suffix: str) -> Path:
    if tier not in (TIER_REAL, TIER_HOLDOUT):
        raise AcquisitionError(f"未知层级：{tier!r}（应为 {TIER_REAL}/{TIER_HOLDOUT}）")
    prefix = REAL_FILENAME_PREFIX if tier == TIER_REAL else f"{REAL_FILENAME_PREFIX}{TIER_HOLDOUT}_"
    return REAL_ROOT / f"{prefix}{document_id}{suffix}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="采集 cn_agent_v2 Real/Holdout 集原文")
    parser.add_argument("--symbol", default="", help="交易所代码，如 600519.SH")
    parser.add_argument("--document", default="annual", help="annual / semi_annual / quarter / announcement / draft")
    parser.add_argument("--year", default="", help="会计年度")
    parser.add_argument("--url", default="", help="直接下载地址（门户需要会话时由操作者提供）")
    parser.add_argument("--tier", default=TIER_REAL, choices=[TIER_REAL, TIER_HOLDOUT])
    parser.add_argument("--expected-sha256", default="", help="已知 sha256；不符则拒绝写入")
    parser.add_argument("--license-note", default="公开披露文件，仅用于本评测，不再分发")
    parser.add_argument("--verify", action="store_true", help="只校验 sha256，不下载")
    parser.add_argument("--root", default=str(REAL_ROOT))
    args = parser.parse_args(argv)

    root = Path(args.root)

    if args.verify:
        try:
            verdict = verify_manifest(root=root)
        except AcquisitionError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0 if verdict["ok"] else 1

    if not args.symbol or not args.url:
        print(
            "[FAIL] 需要 --symbol 与 --url。门户下载地址通常需要会话，"
            "本脚本不绕过登录：请操作者从巨潮/交易所页面复制直链后传入，"
            "或先运行 --verify 检查既有清单。",
            file=sys.stderr,
        )
        return 2

    document_id = f"{args.symbol}_{args.year}_{args.document}".strip("_")
    target = build_target(document_id, tier=args.tier, suffix=".pdf")
    try:
        digest = download(args.url, target)
        if args.expected_sha256 and digest != args.expected_sha256:
            target.unlink(missing_ok=True)
            raise AcquisitionError(
                f"sha256 不符（期望 {args.expected_sha256[:12]}…，实际 {digest[:12]}…）——"
                "已删除下载文件，不写入清单。"
            )
        record_acquisition(
            {
                "filename": target.name,
                "document_id": document_id,
                "tier": args.tier,
                "symbol": args.symbol,
                "document": args.document,
                "year": args.year,
                "source_url": args.url,
                "sha256": digest,
                "bytes": target.stat().st_size,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "license_note": args.license_note,
            },
            root=root,
        )
    except AcquisitionError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    print(f"[OK] {target} sha256={digest}")
    print(f"     下一步：按 docs/benchmark/LABELING_SPEC.md 人工标注 → "
          f"{target.with_suffix('.labels.json').name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
