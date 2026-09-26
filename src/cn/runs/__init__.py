"""CARD-07: Run Manifest & Audit Trail.

Public surface for recording and replaying A-share research runs. Each run
produces ``runs/<run_id>/manifest.json`` (17 fields) and an append-only
``runs/<run_id>/events.jsonl`` audit trail.
"""

from __future__ import annotations

from .models import (
    ExecutionMode,
    RUN_SCHEMA_VERSION,
    ReplayPlan,
    RunConfig,
    RunEvent,
    RunInputDoc,
    RunManifest,
    RunManifestError,
    RunRecordError,
    RunResult,
    Stage,
    collect_tool_versions,
    detect_git_commit,
    digest_of,
    sha256_file,
    validate_manifest_fields,
)
from .recorder import (
    RunRecorder,
    list_runs,
    load_events,
    load_manifest,
    replay,
)

__all__ = [
    "RUN_SCHEMA_VERSION",
    "ExecutionMode",
    "Stage",
    "RunConfig",
    "RunInputDoc",
    "RunManifest",
    "RunEvent",
    "RunResult",
    "ReplayPlan",
    "RunRecorder",
    "RunManifestError",
    "RunRecordError",
    "digest_of",
    "sha256_file",
    "validate_manifest_fields",
    "collect_tool_versions",
    "detect_git_commit",
    "list_runs",
    "load_manifest",
    "load_events",
    "replay",
]
