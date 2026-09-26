#!/usr/bin/env python3
"""Build a curated 128-stock A-share snapshot universe from Tushare.

The run is intentionally sequential: Tushare rate limits are easier to reason
about than a concurrent fan-out, and every symbol gets its own probe/build
record in the manifest.  Existing snapshots for the requested cutoff are
skipped unless ``--force`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE = PROJECT_ROOT / "scripts" / "probe_tushare_cn0.py"
BUILDER = PROJECT_ROOT / "scripts" / "build_cn_snapshot_from_probe.py"
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "snapshots" / "cn"
PROBE_ROOT = PROJECT_ROOT / "data" / "provider_probes" / "cn0"
DEFAULT_MANIFEST = PROJECT_ROOT / "output" / "cn_universe_rebuild_manifest.json"

# Core industries plus additional sectors for a more useful beta universe.
# Codes are the same frozen Shenwan L1 mapping used by peer selection.
#
# INVARIANT: this list and ``src.cn.industries._STOCK_INDUSTRY`` must describe
# exactly the same code set.  A symbol that is collected here but missing an
# industry code silently gets zero peers (``industry_peers()`` returns []), so
# the two are locked together by ``tests/test_cn_universe_consistency.py``.
CURATED_UNIVERSE = [
    # 食品饮料
    "600519.SH", "000858.SZ", "000568.SZ", "600809.SH", "002304.SZ",
    "000596.SZ", "603369.SH", "600600.SH", "000895.SZ", "603288.SH", "002714.SZ",
    # 家用电器
    "000333.SZ", "000651.SZ", "600690.SH", "002032.SZ", "000921.SZ",
    # 医药生物
    "600276.SH", "300760.SZ", "000538.SZ", "600196.SH",
    "002007.SZ", "300015.SZ", "603259.SH",
    # 银行
    "601398.SH", "601939.SH", "600036.SH", "601166.SH",
    "601288.SH", "601988.SH", "000001.SZ", "600016.SH", "002142.SZ",
    # 非银金融
    "601318.SH", "601628.SH", "601601.SH", "600030.SH", "601688.SH",
    # 房地产
    "000002.SZ", "001979.SZ", "600048.SH", "600383.SH", "601155.SH",
    # 电子
    "000725.SZ", "002415.SZ", "600703.SH", "603501.SH", "688981.SH",
    # 计算机
    "002230.SZ", "000938.SZ", "600570.SH", "688111.SH", "600588.SH",
    # 电力设备
    "300750.SZ", "002594.SZ", "601012.SH", "300274.SZ", "600438.SH",
    # 汽车
    "000625.SZ", "600104.SH", "601238.SH", "601633.SH", "600741.SH",
    # 有色金属
    "601899.SH", "600547.SH", "002460.SZ", "603993.SH", "600111.SH",
    # 基础化工 / 石油石化 / 煤炭 / 钢铁 / 机械设备 / 建筑
    "600309.SH", "002601.SZ", "600426.SH", "000792.SZ", "600143.SH",
    "600028.SH", "601857.SH", "600688.SH", "601808.SH", "600871.SH",
    "601088.SH", "000983.SZ", "601225.SH", "600188.SH", "600123.SH",
    "600019.SH", "000709.SZ", "000932.SZ", "600010.SH", "600808.SH", "600282.SH",
    "600031.SH", "601100.SH", "000157.SZ", "000425.SZ", "601717.SH",
    "601668.SH", "601390.SH", "601186.SH", "601800.SH", "601669.SH",
    "600585.SH", "000877.SZ", "000401.SZ", "002271.SZ", "000786.SZ",
    # 交通运输
    "601006.SH", "002352.SZ", "600009.SH", "601111.SH", "601919.SH",
    # 国防军工
    "600760.SH", "002179.SZ", "600893.SH", "000768.SZ", "600150.SH",
    # 通信
    "600941.SH", "601728.SH", "600050.SH", "000063.SZ", "300308.SZ",
    # 公用事业
    "600900.SH", "601985.SH", "600886.SH", "600795.SH", "003816.SZ",
    # 美容护理 — added as one whole sector: a single stock in a new sector
    # would have an empty peer group, so new sectors enter five at a time.
    "603605.SH", "300957.SZ", "300896.SZ", "600315.SH", "603983.SH",
]


def _run(command: list[str], dry_run: bool) -> str:
    print("  $ " + " ".join(str(part) for part in command))
    if dry_run:
        return ""
    result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        for marker in ("pandas.errors.", "ConnectionError", "ReadTimeout", "TimeoutError", "FileNotFoundError"):
            matches = [line for line in lines if marker in line]
            if matches:
                detail = matches[-1]
                break
        else:
            detail = lines[-1] if lines else detail
        raise RuntimeError(detail)
    return result.stdout.strip()


def _latest_probe(symbol: str) -> Path:
    required_files = (
        "raw_stock_basic.csv",
        "raw_daily.csv",
        "raw_income.csv",
        "raw_balancesheet.csv",
        "raw_cashflow.csv",
        "raw_fina_indicator.csv",
    )
    def has_csv_header(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        try:
            first_line = path.read_text(encoding="utf-8-sig").splitlines()[0].strip()
        except (OSError, UnicodeDecodeError, IndexError):
            return False
        return bool(first_line and "," in first_line)

    matches = sorted(
        (
            path for path in PROBE_ROOT.glob(f"{symbol}_*")
            if path.is_dir() and all(has_csv_header(path / filename) for filename in required_files)
        ),
        key=lambda path: path.stat().st_mtime,
    )
    if not matches:
        raise FileNotFoundError(f"no complete probe artifact found for {symbol}")
    return matches[-1]


def _snapshot_path(symbol: str, date_tag: str) -> Path:
    return SNAPSHOT_DIR / f"{symbol}_{date_tag}_tushare_v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start-date", default="20260901")
    parser.add_argument("--end-date", default="20260923")
    parser.add_argument("--research-as-of", default="2026-09-23T23:00:00+08:00")
    parser.add_argument("--only", action="append", help="limit to one or more symbols")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="rebuild snapshots that already exist")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args()

    env_path = PROJECT_ROOT / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    token_ready = any(
        line.startswith("TUSHARE_TOKEN=") and line.split("=", 1)[1].strip()
        for line in env_text.splitlines()
    ) or bool(os.environ.get("TUSHARE_TOKEN"))
    if not args.dry_run and not token_ready:
        raise SystemExit("TUSHARE_TOKEN is empty; configure it in .env before a real run")

    requested = [item.upper() for item in args.only] if args.only else list(CURATED_UNIVERSE)
    unknown = sorted(set(requested) - set(CURATED_UNIVERSE))
    if unknown:
        raise SystemExit("--only contains symbols outside curated universe: " + ", ".join(unknown))

    date_tag = args.research_as_of[:10].replace("-", "")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for symbol in requested:
        output = _snapshot_path(symbol, date_tag)
        row: dict[str, object] = {
            "symbol": symbol,
            "status": "pending",
            "research_as_of": args.research_as_of,
            "output": str(output),
            "probe_dir": None,
            "error": None,
        }
        if output.exists() and not args.force:
            row["status"] = "skipped_existing"
            rows.append(row)
            print(f"SKIP {symbol}: {output.name} already exists")
            continue
        print(f"=== {symbol} ({len(rows) + 1}/{len(requested)}) ===")
        try:
            _run([
                sys.executable, str(PROBE), "--symbol", symbol,
                "--start-date", args.start_date, "--end-date", args.end_date,
            ], args.dry_run)
            if args.dry_run:
                row["status"] = "dry_run"
            else:
                probe_dir = _latest_probe(symbol)
                row["probe_dir"] = str(probe_dir)
                output_text = _run([
                    sys.executable, str(BUILDER), str(probe_dir),
                    "--output", str(output), "--research-as-of", args.research_as_of,
                ], False)
                row["status"] = "built"
                row["builder_output"] = output_text.splitlines()[-1] if output_text else ""
        except (OSError, RuntimeError, FileNotFoundError) as exc:
            row["status"] = "failed"
            row["error"] = str(exc)
            print(f"FAILED {symbol}: {exc}")
            if not args.continue_on_error:
                rows.append(row)
                break
        rows.append(row)

    manifest = {
        "schema_version": "cn-curated-universe-rebuild-1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_as_of": args.research_as_of,
        "requested_symbols": requested,
        "universe_size": len(CURATED_UNIVERSE),
        "built_count": sum(row["status"] == "built" for row in rows),
        "skipped_count": sum(row["status"] == "skipped_existing" for row in rows),
        "failed_count": sum(row["status"] == "failed" for row in rows),
        "rows": rows,
    }
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {manifest_path}")
    print(json.dumps({key: manifest[key] for key in ("built_count", "skipped_count", "failed_count")}, ensure_ascii=False))
    return 1 if manifest["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
