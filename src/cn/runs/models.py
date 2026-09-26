"""Data models for CARD-07: Run Manifest & Audit Trail.

This module is the contract layer for :mod:`src.cn.runs`. It defines the
17-field :class:`RunManifest`, the append-only :class:`RunEvent` line that is
written to ``events.jsonl``, the :class:`RunConfig` accepted by
:class:`~src.cn.runs.recorder.RunRecorder.start`, and the replay result types
(:class:`ReplayPlan`, :class:`RunResult`).

Design invariants (enforced by the card and the project's zero-silent policy):

* The manifest is a *descriptor* of a run. Offline mode may leave the model
  series (``model`` / ``model_provider`` / ``model_version`` / ``temperature``)
  as ``None`` — but the keys MUST exist explicitly (17 fields, never 16).
* ``events.jsonl`` is append-only. Nothing in this package ever rewrites a
  historical event line. Any code path that seeks back and mutates an event is
  a red line.
* Digests are deterministic: identical inputs always produce identical digests
  regardless of wall-clock time, so the replay diff is meaningful offline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

# Schema version for the run package (independent of the 17 manifest fields).
RUN_SCHEMA_VERSION = "1.0.0"

# Packages whose versions are worth recording in ``tool_versions``. Resolution is
# best-effort and offline-safe: any missing package is recorded as "unknown".
_TRACKED_TOOL_PACKAGES = (
    "tushare",
    "akshare",
    "openpyxl",
    "pandas",
    "numpy",
    "requests",
    "pydantic",
    "streamlit",
    "fastapi",
    "reportlab",
)


class Stage(str, Enum):
    """The nine pipeline stages an audit event may belong to.

    ``str`` mixin so the value serializes to the bare string in JSONL.
    """

    FILE_LOAD = "file_load"
    PARSE = "parse"
    NORMALIZE = "normalize"
    RETRIEVAL = "retrieval"
    TOOL_CALL = "tool_call"
    CALCULATION = "calculation"
    CLAIM = "claim"
    VALIDATION = "validation"
    REPORT = "report"

    @classmethod
    def coerce(cls, value: "Stage | str | None") -> "Stage":
        if value is None:
            raise ValueError("stage must not be None")
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        try:
            return cls(text)
        except ValueError as exc:
            raise ValueError(f"unknown stage: {value!r}") from exc


class ExecutionMode(str, Enum):
    """How a run was executed (drives replay semantics)."""

    OFFLINE = "offline"
    REAL_MODEL = "real_model"
    REPLAY = "replay"

    @classmethod
    def coerce(cls, value: "ExecutionMode | str | None") -> "ExecutionMode":
        if value is None:
            raise ValueError("execution_mode must not be None")
        if isinstance(value, cls):
            return value
        text = str(value).strip().lower()
        try:
            return cls(text)
        except ValueError as exc:
            raise ValueError(f"unknown execution_mode: {value!r}") from exc


# The 17 manifest fields, in declaration order. Used by the validator so the
# count can never silently drift.
MANIFEST_FIELDS: List[str] = [
    "run_id",
    "started_at",
    "finished_at",
    "input_document_sha256",
    "snapshot_id",
    "model",
    "model_provider",
    "model_version",
    "temperature",
    "prompt_hash",
    "skill_hash",
    "parser_name",
    "parser_version",
    "git_commit",
    "tool_versions",
    "random_seed",
    "execution_mode",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class RunManifestError(RuntimeError):
    """A run manifest is malformed (missing/invalid 17 fields)."""


class RunRecordError(RuntimeError):
    """An audit event could not be persisted (fail-closed trigger)."""


class ReplayRejected(RuntimeError):
    """Replay refused because the run is not reproducible as-is."""


# ---------------------------------------------------------------------------
# Config + input documents
# ---------------------------------------------------------------------------
@dataclass
class RunInputDoc:
    """One input document handed to a run.

    Either ``sha256`` is supplied (trust caller) or ``path`` is supplied (the
    recorder computes the sha256). ``path`` is retained so ``replay`` can
    re-verify the document has not been mutated.
    """

    sha256: Optional[str] = None
    path: Optional[str] = None

    def resolve_sha256(self) -> str:
        if self.sha256:
            return self.sha256
        if self.path:
            return sha256_file(Path(self.path))
        raise RunManifestError("RunInputDoc needs sha256 or path")


@dataclass
class RunConfig:
    """Everything needed to start a run and seed its manifest skeleton."""

    input_documents: List[RunInputDoc]
    parser_name: str
    parser_version: str
    execution_mode: str
    snapshot_id: Optional[str] = None
    model: Optional[str] = None
    model_provider: Optional[str] = None
    model_version: Optional[str] = None
    temperature: Optional[float] = None
    prompt_hash: Optional[str] = None
    #: CARD-14: the *names* of the registered prompts this run uses. ``start``
    #: turns them into ``prompt_hash`` via the registry, so a caller states which
    #: prompts it used and the hash can never drift from the actual template
    #: bytes. Still ``Optional``: a run that sends no prompt records no hash.
    prompt_names: Optional[List[str]] = None
    skill_hash: Optional[str] = None
    tool_versions: Optional[Dict[str, str]] = None
    random_seed: Optional[int] = None


# ---------------------------------------------------------------------------
# Manifest (17 fields)
# ---------------------------------------------------------------------------
@dataclass
class RunManifest:
    """The 17-field run descriptor. Mutable (``finished_at`` is filled at end)."""

    run_id: str
    started_at: str
    finished_at: str
    input_document_sha256: List[str]
    snapshot_id: Optional[str]
    model: Optional[str]
    model_provider: Optional[str]
    model_version: Optional[str]
    temperature: Optional[float]
    prompt_hash: Optional[str]
    skill_hash: Optional[str]
    parser_name: str
    parser_version: str
    git_commit: str
    tool_versions: Dict[str, str]
    random_seed: Optional[int]
    execution_mode: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunManifest":
        validate_manifest_fields(data)
        return cls(**{k: data.get(k) for k in MANIFEST_FIELDS})

    @property
    def is_finished(self) -> bool:
        return bool(self.finished_at)

    @property
    def status(self) -> str:
        """Derived run status (not a manifest field — keeps the 17-field count)."""
        return "completed" if self.finished_at else "interrupted"


def validate_manifest_fields(data: Dict[str, Any]) -> None:
    """Assert all 17 manifest keys are present (values may be None)."""
    missing = [k for k in MANIFEST_FIELDS if k not in data]
    if missing:
        raise RunManifestError(f"manifest missing fields: {missing}")
    if len(data) != len(MANIFEST_FIELDS):
        extra = [k for k in data if k not in MANIFEST_FIELDS]
        raise RunManifestError(f"manifest has unexpected fields: {extra}")


# ---------------------------------------------------------------------------
# Event (one JSONL line)
# ---------------------------------------------------------------------------
@dataclass
class RunEvent:
    """A single append-only audit event.

    The eight required keys match the card's ``events.jsonl`` schema exactly;
    ``calculation_id`` / ``evidence_id`` / ``sha256`` / ``path`` / ``sampled``
    are optional extensions required by §7 (calculation events carry a
    ``calculation_id`` aligning with CARD-09; file_load events carry ``sha256``).
    """

    ts: str
    run_id: str
    stage: str
    event_type: str
    input_digest: str
    output_digest: str
    status: str
    detail: Dict[str, Any] = field(default_factory=dict)
    calculation_id: Optional[str] = None
    evidence_id: Optional[str] = None
    sha256: Optional[str] = None
    path: Optional[str] = None
    sampled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ts": self.ts,
            "run_id": self.run_id,
            "stage": self.stage,
            "event_type": self.event_type,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "status": self.status,
            "detail": self.detail,
        }
        if self.calculation_id is not None:
            out["calculation_id"] = self.calculation_id
        if self.evidence_id is not None:
            out["evidence_id"] = self.evidence_id
        if self.sha256 is not None:
            out["sha256"] = self.sha256
        if self.path is not None:
            out["path"] = self.path
        if self.sampled:
            out["sampled"] = True
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunEvent":
        return cls(
            ts=data["ts"],
            run_id=data["run_id"],
            stage=data["stage"],
            event_type=data["event_type"],
            input_digest=data["input_digest"],
            output_digest=data["output_digest"],
            status=data["status"],
            detail=data.get("detail", {}) or {},
            calculation_id=data.get("calculation_id"),
            evidence_id=data.get("evidence_id"),
            sha256=data.get("sha256"),
            path=data.get("path"),
            sampled=bool(data.get("sampled", False)),
        )


# ---------------------------------------------------------------------------
# Replay result types
# ---------------------------------------------------------------------------
@dataclass
class ReplayPlan:
    """Pre-flight check result for a replay."""

    run_id: str
    replayable: bool
    reasons: List[str]
    input_docs: List[Dict[str, Any]]  # {path, sha256, present, sha_match}
    git_commit: Optional[str]
    git_commit_advanced: bool
    mode: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    """Outcome of :func:`~src.cn.runs.recorder.replay`."""

    run_id: str
    status: str  # ok | replay_drift | rejected | replay_blocked
    replayable: bool
    plan: ReplayPlan
    deterministic_full_match: bool
    deterministic_events: int
    llm_sampled_total: int
    llm_sampled_diff: List[Dict[str, Any]]
    deterministic_mismatches: List[Dict[str, Any]]
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "replayable": self.replayable,
            "plan": self.plan.to_dict(),
            "deterministic_full_match": self.deterministic_full_match,
            "deterministic_events": self.deterministic_events,
            "llm_sampled_total": self.llm_sampled_total,
            "llm_sampled_diff": self.llm_sampled_diff,
            "deterministic_mismatches": self.deterministic_mismatches,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Digest helpers (deterministic, offline-safe)
# ---------------------------------------------------------------------------
def digest_of(obj: Any) -> str:
    """Return a stable sha256 hex digest for any JSON-serializable input.

    ``None`` digests to a fixed token (never an empty string) so "no input" is
    distinguishable from "empty string input". Bytes are hashed directly.
    """
    if obj is None:
        return hashlib.sha256(b"__none__").hexdigest()
    if isinstance(obj, bytes):
        return hashlib.sha256(obj).hexdigest()
    if isinstance(obj, str):
        payload = obj.encode("utf-8")
    else:
        try:
            payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        except TypeError:
            payload = repr(obj).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """sha256 of a file, read in chunks (works for large PDFs)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_tool_versions() -> Dict[str, str]:
    """Best-effort, offline version snapshot of tracked dependencies."""
    from importlib.metadata import PackageNotFoundError, version

    out: Dict[str, str] = {}
    for pkg in _TRACKED_TOOL_PACKAGES:
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "unknown"
    return out


def detect_git_commit(repo_root: Path) -> str:
    """Return the current ``git rev-parse HEAD`` or ``"unknown"``.

    Fail-open: a missing git repo or any error yields ``"unknown"`` rather than
    crashing an offline run.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
