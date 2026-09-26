"""CARD-07 command-line helper (Demo method: export manifest + replay).

Usage:
    python -m src.cn.runs.cli list
    python -m src.cn.runs.cli show <run_id>
    python -m src.cn.runs.cli verify <run_id>
    python -m src.cn.runs.cli replay <run_id> [--executor-module mod:func]

``replay`` needs an executor that re-runs the task. Without one it prints the
replay plan and exits non-zero (replay is blocked, not faked). For a live demo
this is typically wired to the research pipeline that produced the original run.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from .recorder import _DEFAULT_STORE_DIR, load_events, load_manifest, replay
from .recorder import RunRecorder as _RR


def _cmd_list(_args: argparse.Namespace) -> int:
    runs = sorted(_RR(_DEFAULT_STORE_DIR).store_dir.iterdir()) if _DEFAULT_STORE_DIR.exists() else []
    for run_dir in runs:
        if (run_dir / "manifest.json").exists():
            m = load_manifest(_DEFAULT_STORE_DIR, run_dir.name)
            print(f"{m.run_id}  mode={m.execution_mode}  status={m.status}  docs={len(m.input_document_sha256)}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    m = load_manifest(_DEFAULT_STORE_DIR, args.run_id)
    print(json.dumps(m.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    print(f"\n# events: {len(load_events(_DEFAULT_STORE_DIR, args.run_id))}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    plan = _RR(_DEFAULT_STORE_DIR).verify_replayable(args.run_id)
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    return 0 if plan.replayable else 2


def _cmd_replay(args: argparse.Namespace) -> int:
    plan = RunRecorder(_DEFAULT_STORE_DIR).verify_replayable(args.run_id)
    if not plan.replayable:
        print("REPLAY REJECTED:", "; ".join(plan.reasons), file=sys.stderr)
        return 2
    if not args.executor:
        print("Replay plan OK, but no --executor supplied. Replay blocked (not faked).")
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 3
    mod_name, func_name = args.executor.split(":")
    mod = importlib.import_module(mod_name)
    executor = getattr(mod, func_name)
    manifest = load_manifest(_DEFAULT_STORE_DIR, args.run_id)
    result = replay(args.run_id, executor=executor, store_dir=_DEFAULT_STORE_DIR)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fintrace-runs", description="CARD-07 run manifest CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list recorded runs").set_defaults(func=_cmd_list)
    p_show = sub.add_parser("show", help="print a run manifest")
    p_show.add_argument("run_id")
    p_show.set_defaults(func=_cmd_show)
    p_ver = sub.add_parser("verify", help="replay pre-flight check")
    p_ver.add_argument("run_id")
    p_ver.set_defaults(func=_cmd_verify)
    p_rep = sub.add_parser("replay", help="re-execute a run and diff layers")
    p_rep.add_argument("run_id")
    p_rep.add_argument("--executor", help="module:func returning re-run event dicts")
    p_rep.set_defaults(func=_cmd_replay)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
