#!/usr/bin/env python3
"""Rebuild every local Tushare snapshot the offline suite depends on.

Why this exists: ``data/`` is gitignored, so a fresh clone has **no** snapshots
and ~23 tests fail.  Running this script with a configured ``TUSHARE_TOKEN``
restores them from the real provider.

Each entry pins three constraints that were learned the hard way — do not
"simplify" them:

1. **Peers must be rebuilt together.** ``research_peer_valuation`` keeps only
   snapshots whose ``research_as_of`` is within 3 days of the target's.  600519
   is L1_01 (食品饮料); without its five liquor peers the peer tests see
   ``peer_count == 0`` even though the four headline symbols exist.
2. **The bar window must stop before the research instant.**
   ``test_cn_report`` builds 300750 with ``research_as_of=2026-08-12T10:45``,
   so a snapshot carrying the 08-12 close is judged ``future_fact``.  Hence
   ``end_date=20260811`` for 300750 while its snapshot id still says 20260812 —
   the id date comes from ``--research-as-of``, not from the bar window.
3. **688836 listed on 2026-08-19**, so its window must reach past that date or
   ``daily`` returns an empty frame and the builder fails.

Snapshots keep bars in **descending** (newest-first) order — that is the
provider's order and ``test_cn_tushare_provider`` asserts ``bars[0]`` is the
latest date.  Do not "fix" it to ascending.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "scripts" / "probe_tushare_cn0.py"
BUILDER = PROJECT_ROOT / "scripts" / "build_cn_snapshot_from_probe.py"
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots" / "cn"
PROBE_ROOT = PROJECT_ROOT / "data" / "provider_probes" / "cn0"

# (symbol, bar window start, bar window end, research_as_of)
# The four headline symbols come first; the rest are 600519's L1_01 peers.
SPEC = [
    ("600519.SH", "20260101", "20260810", "2026-08-10T23:00:00+08:00"),
    ("300750.SZ", "20260101", "20260811", "2026-08-12T23:00:00+08:00"),
    ("688981.SH", "20260101", "20260813", "2026-08-13T23:00:00+08:00"),
    ("688836.SH", "20260101", "20260822", "2026-08-22T23:00:00+08:00"),
    # L1_01 peers of 600519 — same research_as_of, otherwise they are filtered
    # out by the 3-day rule and peer valuation reports peer_count == 0.
    ("000858.SZ", "20260101", "20260810", "2026-08-10T23:00:00+08:00"),
    ("000568.SZ", "20260101", "20260810", "2026-08-10T23:00:00+08:00"),
    ("600809.SH", "20260101", "20260810", "2026-08-10T23:00:00+08:00"),
    ("002304.SZ", "20260101", "20260810", "2026-08-10T23:00:00+08:00"),
    ("000596.SZ", "20260101", "20260810", "2026-08-10T23:00:00+08:00"),
]


def _run(cmd: list[str], dry_run: bool) -> str:
    printable = " ".join(str(part) for part in cmd)
    print(f"  $ {printable}")
    if dry_run:
        return ""
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        raise SystemExit(f"FAILED ({result.returncode}): {printable}\n{result.stderr.strip()}")
    return result.stdout.strip()


def _latest_probe_dir(symbol: str) -> Path:
    matches = sorted(PROBE_ROOT.glob(f"{symbol}_*"))
    if not matches:
        raise SystemExit(f"probe produced no artifact directory for {symbol}")
    return matches[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the commands without calling Tushare")
    parser.add_argument("--only", default=None, help="rebuild a single symbol, e.g. 300750.SZ")
    parser.add_argument("--continue-on-error", action="store_true", help="keep processing other symbols after a failed probe or build")
    parser.add_argument(
        "--manifest",
        default=str(PROJECT_ROOT / "output" / "tushare_rebuild_manifest.json"),
        help="write a JSON run manifest with per-symbol status (default: output/tushare_rebuild_manifest.json)",
    )
    args = parser.parse_args()

    env_path = PROJECT_ROOT / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if "TUSHARE_TOKEN=" not in env_text:
        print("WARNING: .env has no TUSHARE_TOKEN line; the probe will refuse to run.")
    if not args.dry_run and not any(
        line.startswith("TUSHARE_TOKEN=") and line.split("=", 1)[1].strip()
        for line in env_text.splitlines()
    ):
        raise SystemExit("TUSHARE_TOKEN is empty in .env — fill it before rebuilding.")

    todo = [row for row in SPEC if args.only is None or row[0] == args.only]
    if not todo:
        raise SystemExit(f"--only {args.only} matches nothing in SPEC")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    built = []
    manifest_rows = []
    for symbol, start, end, research_as_of in todo:
        date_tag = research_as_of[:10].replace("-", "")
        output = SNAPSHOT_DIR / f"{symbol}_{date_tag}_tushare_v1.json"
        print(f"=== {symbol} → {output.name} (bars {start}~{end}, as-of {research_as_of}) ===")
        row = {
            "symbol": symbol, "status": "dry_run" if args.dry_run else "failed",
            "research_as_of": research_as_of, "bar_start": start, "bar_end": end,
            "output": str(output), "probe_dir": None, "error": None,
        }
        try:
            _run([sys.executable, str(PROBE), "--symbol", symbol,
                  "--start-date", start, "--end-date", end], args.dry_run)
            if args.dry_run:
                _run([sys.executable, str(BUILDER), "<probe_dir>", "--output", str(output),
                      "--research-as-of", research_as_of], True)
            else:
                probe_dir = _latest_probe_dir(symbol)
                row["probe_dir"] = str(probe_dir)
                _run([sys.executable, str(BUILDER), str(probe_dir),
                      "--output", str(output), "--research-as-of", research_as_of], False)
                built.append(output)
                row["status"] = "built"
        except (SystemExit, OSError, subprocess.SubprocessError) as exc:
            row["status"] = "failed"
            row["error"] = str(exc)
            print(f"FAILED {symbol}: {exc}")
            if not args.continue_on_error:
                manifest_rows.append(row)
                break
        manifest_rows.append(row)

    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "tushare-rebuild-manifest-1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
        "continue_on_error": bool(args.continue_on_error),
        "requested_symbols": [row[0] for row in todo],
        "built_count": sum(row["status"] == "built" for row in manifest_rows),
        "failed_count": sum(row["status"] == "failed" for row in manifest_rows),
        "rows": manifest_rows,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {manifest_path}")

    if args.dry_run:
        print(f"\ndry run: {len(todo)} symbol(s), no provider calls made")
        return 0
    print(f"\nrebuilt {len(built)} snapshot(s):")
    for path in built:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
