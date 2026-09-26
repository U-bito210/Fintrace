"""Run recorder and replay engine for CARD-07.

The recorder produces, per executed task:

* ``runs/<run_id>/manifest.json`` — the 17-field descriptor (mutable: only
  ``finished_at`` is back-filled at the end).
* ``runs/<run_id>/events.jsonl`` — an append-only audit trail. Every
  :meth:`RunRecorder.emit` opens the file in *append* mode and writes exactly
  one line. No method in this module ever seeks back to rewrite a historical
  event — that is the red line from CARD-07 §9.

``replay()`` re-executes a run via a caller-supplied ``executor`` and diffs the
deterministic layer (must be fully equal) from the sampled/LLM layer (may differ
when ``temperature > 0``). See CARD-07 §8 edge cases 2–4.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import (
    RUN_SCHEMA_VERSION,
    ExecutionMode,
    RunConfig,
    RunEvent,
    RunInputDoc,
    RunManifest,
    RunResult,
    ReplayPlan,
    ReplayRejected,
    RunManifestError,
    RunRecordError,
    Stage,
    collect_tool_versions,
    detect_git_commit,
    digest_of,
    sha256_file,
    validate_manifest_fields,
)

# Repo root = src/cn/runs -> 3 parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_STORE_DIR = _REPO_ROOT / "runs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(store_dir: Path, run_id: str) -> Path:
    return store_dir / run_id


def _manifest_path(store_dir: Path, run_id: str) -> Path:
    return _run_dir(store_dir, run_id) / "manifest.json"


def _events_path(store_dir: Path, run_id: str) -> Path:
    return _run_dir(store_dir, run_id) / "events.jsonl"


def load_manifest(store_dir: Path, run_id: str) -> RunManifest:
    """Load and validate a run manifest."""
    path = _manifest_path(store_dir, run_id)
    if not path.exists():
        raise RunManifestError(f"manifest not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return RunManifest.from_dict(json.load(fh))


def load_events(store_dir: Path, run_id: str) -> List[RunEvent]:
    """Load all audit events for a run, in append order."""
    path = _events_path(store_dir, run_id)
    events: List[RunEvent] = []
    if not path.exists():
        return events
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            events.append(RunEvent.from_dict(json.loads(line)))
    return events


def list_runs(store_dir: Path = _DEFAULT_STORE_DIR) -> List[str]:
    """Return run_ids present under ``store_dir`` (directory names)."""
    if not store_dir.exists():
        return []
    return sorted(
        p.name for p in store_dir.iterdir() if p.is_dir() and (p / "manifest.json").exists()
    )


class RunRecorder:
    """Records a single run's manifest skeleton + append-only event stream."""

    def __init__(self, store_dir: Path = _DEFAULT_STORE_DIR, *, repo_root: Path = _REPO_ROOT):
        self.store_dir = Path(store_dir)
        self.repo_root = Path(repo_root)
        self.run_id: Optional[str] = None
        self._manifest: Optional[RunManifest] = None

    # -- lifecycle --------------------------------------------------------
    def start(self, config: RunConfig) -> str:
        """Write the manifest skeleton and return the new ``run_id``."""
        from uuid import uuid4

        run_id = uuid4().hex
        input_shas = [doc.resolve_sha256() for doc in config.input_documents]
        tool_versions = config.tool_versions or collect_tool_versions()

        # CARD-14: resolve prompt names → covering hash here, so the manifest's
        # ``prompt_hash`` is derived from the registry rather than typed in by a
        # caller who could get it wrong. An explicitly supplied hash still wins.
        prompt_hash = config.prompt_hash
        if prompt_hash is None and config.prompt_names:
            from src.agents.prompts import prompt_bundle_hash, record_usage

            prompt_hash = prompt_bundle_hash(list(config.prompt_names))
            for name in config.prompt_names:
                record_usage(name, config.model, config.temperature)

        manifest = RunManifest(
            run_id=run_id,
            started_at=_now(),
            finished_at="",
            input_document_sha256=input_shas,
            snapshot_id=config.snapshot_id,
            model=config.model,
            model_provider=config.model_provider,
            model_version=config.model_version,
            temperature=config.temperature,
            prompt_hash=prompt_hash,
            skill_hash=config.skill_hash,
            parser_name=config.parser_name,
            parser_version=config.parser_version,
            git_commit=detect_git_commit(self.repo_root),
            tool_versions=tool_versions,
            random_seed=config.random_seed,
            execution_mode=ExecutionMode.coerce(config.execution_mode).value,
        )
        run_dir = _run_dir(self.store_dir, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._write_manifest(manifest)
        self.run_id = run_id
        self._manifest = manifest
        # A run always begins with a file_load audit of its inputs.
        for doc, sha in zip(config.input_documents, input_shas):
            self.emit(
                Stage.FILE_LOAD,
                "input_loaded",
                input_=None,
                output={"sha256": sha, "path": doc.path},
                status="ok",
                sha256=sha,
                path=doc.path,
            )
        return run_id

    def emit(
        self,
        stage: "Stage | str",
        event_type: str,
        input_: Any,
        output: Any,
        status: str,
        *,
        calculation_id: Optional[str] = None,
        evidence_id: Optional[str] = None,
        sha256: Optional[str] = None,
        path: Optional[str] = None,
        sampled: bool = False,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one audit event. Computes input/output digests automatically.

        Raises :class:`RunRecordError` (fail-closed) if the line cannot be
        persisted — a run must never silently continue after a lost event.
        """
        if self.run_id is None or self._manifest is None:
            raise RunRecordError("emit() called before start()")
        event = RunEvent(
            ts=_now(),
            run_id=self.run_id,
            stage=Stage.coerce(stage).value,
            event_type=event_type,
            input_digest=digest_of(input_),
            output_digest=digest_of(output),
            status=status,
            detail=detail or {},
            calculation_id=calculation_id,
            evidence_id=evidence_id,
            sha256=sha256,
            path=path,
            sampled=sampled,
        )
        self._append_event(event)

    def finish(self, outcome: str = "ok") -> None:
        """Back-fill ``finished_at`` and append a terminal ``run_end`` event."""
        if self.run_id is None or self._manifest is None:
            raise RunRecordError("finish() called before start()")
        self._manifest.finished_at = _now()
        self._write_manifest(self._manifest)
        # Terminal event keeps the audit trail append-only and captures outcome.
        self.emit(Stage.REPORT, "run_end", input_=None, output={"outcome": outcome}, status=outcome)

    # -- replay planning --------------------------------------------------
    def verify_replayable(self, run_id: Optional[str] = None) -> ReplayPlan:
        """Check whether a run can be replayed deterministically.

        Replay is refused when an input document is missing or its sha256 no
        longer matches (CARD-07 §8 edge case 4). An advanced ``git_commit`` does
        NOT block replay but is flagged in the plan (edge case 2).
        """
        run_id = run_id or self.run_id
        if run_id is None:
            raise RunManifestError("no run_id to verify")
        manifest = load_manifest(self.store_dir, run_id)
        events = load_events(self.store_dir, run_id)

        reasons: List[str] = []
        input_docs: List[Dict[str, Any]] = []
        manifest_shas = set(manifest.input_document_sha256)

        for ev in events:
            if ev.stage != Stage.FILE_LOAD.value:
                continue
            doc_info: Dict[str, Any] = {
                "path": ev.path,
                "sha256": ev.sha256,
                "present": False,
                "sha_match": False,
            }
            if ev.path and Path(ev.path).exists():
                doc_info["present"] = True
                current = sha256_file(Path(ev.path))
                doc_info["sha_match"] = current == ev.sha256
                if current != ev.sha256:
                    reasons.append(f"input document modified: {ev.path}")
                if ev.sha256 not in manifest_shas:
                    reasons.append(f"file_load sha256 not in manifest set: {ev.path}")
            elif ev.sha256 and ev.sha256 not in manifest_shas:
                reasons.append(f"input sha256 absent from filesystem and manifest: {ev.sha256}")
            input_docs.append(doc_info)

        if not manifest.is_finished:
            reasons.append("run interrupted (no finished_at)")

        current_commit = detect_git_commit(self.repo_root)
        git_advanced = bool(manifest.git_commit) and manifest.git_commit != "unknown" and current_commit != manifest.git_commit

        return ReplayPlan(
            run_id=run_id,
            replayable=len([r for r in reasons if "modified" in r or "interrupted" in r or "absent" in r]) == 0,
            reasons=reasons,
            input_docs=input_docs,
            git_commit=manifest.git_commit,
            git_commit_advanced=git_advanced,
            mode=manifest.execution_mode,
        )

    # -- persistence (append-only) ----------------------------------------
    def _write_manifest(self, manifest: RunManifest) -> None:
        path = _manifest_path(self.store_dir, manifest.run_id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=2, sort_keys=True)

    def _append_event(self, event: RunEvent) -> None:
        path = _events_path(self.store_dir, event.run_id)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            raise RunRecordError(f"failed to append audit event: {exc}") from exc


def replay(
    run_id: str,
    *,
    executor: Callable[[RunManifest], List[Dict[str, Any]]],
    store_dir: Path = _DEFAULT_STORE_DIR,
) -> RunResult:
    """Re-execute a run via ``executor`` and diff deterministic vs sampled layers.

    ``executor`` receives the original :class:`RunManifest` and must return the
    re-run's events as a list of dicts (the same shape as ``RunEvent.to_dict``).
    Deterministic events (``sampled=False``) must match exactly; sampled events
    (LLM / ``temperature>0``) are reported as permissible drift.
    """
    store_dir = Path(store_dir)
    manifest = load_manifest(store_dir, run_id)
    plan = RunRecorder(store_dir).verify_replayable(run_id)

    if not plan.replayable:
        raise ReplayRejected("; ".join(plan.reasons))

    original_events = [e for e in load_events(store_dir, run_id)]
    rerun_raw = executor(manifest)
    rerun_events = [RunEvent.from_dict(d) for d in rerun_raw]

    orig_det = [e for e in original_events if not e.sampled]
    rerun_det = [e for e in rerun_events if not e.sampled]
    orig_llm = [e for e in original_events if e.sampled]
    rerun_llm = [e for e in rerun_events if e.sampled]

    deterministic_full_match = True
    mismatches: List[Dict[str, Any]] = []
    for i, (o, r) in enumerate(zip(orig_det, rerun_det)):
        if (o.stage, o.event_type, o.input_digest, o.output_digest) != (
            r.stage,
            r.event_type,
            r.input_digest,
            r.output_digest,
        ):
            deterministic_full_match = False
            mismatches.append({
                "index": i,
                "original": {"stage": o.stage, "event_type": o.event_type,
                             "output_digest": o.output_digest},
                "rerun": {"stage": r.stage, "event_type": r.event_type,
                          "output_digest": r.output_digest},
            })
    if len(orig_det) != len(rerun_det) and not mismatches:
        deterministic_full_match = False
        mismatches.append({"index": -1, "note": "deterministic event count drift",
                           "original": len(orig_det), "rerun": len(rerun_det)})

    llm_diff: List[Dict[str, Any]] = []
    for o, r in zip(orig_llm, rerun_llm):
        if o.output_digest != r.output_digest:
            llm_diff.append({
                "stage": o.stage,
                "event_type": o.event_type,
                "original_output_digest": o.output_digest,
                "rerun_output_digest": r.output_digest,
            })

    if not deterministic_full_match:
        status = "replay_drift"
        message = "deterministic layer diverged — run is NOT reproducible"
    elif llm_diff:
        status = "replay_drift"
        message = "deterministic layer fully equal; LLM/sampled layer shows expected sampling drift"
    else:
        status = "ok"
        message = "deterministic layer fully equal; no sampled drift"

    return RunResult(
        run_id=run_id,
        status=status,
        replayable=True,
        plan=plan,
        deterministic_full_match=deterministic_full_match,
        deterministic_events=len(orig_det),
        llm_sampled_total=len(orig_llm),
        llm_sampled_diff=llm_diff,
        deterministic_mismatches=mismatches,
        message=message,
    )
