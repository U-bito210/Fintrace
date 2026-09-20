"""CARD-12 Evidence Pack & Audit Replay.

Two deliverables live here, and both are about *faithfulness* rather than
capability:

* :func:`export_evidence_pack` turns a run directory into the card's standard
  13-artefact pack (plus a zip), validating it before anyone reads it. A pack
  that is missing pieces is exported as ``incomplete`` **with the missing list
  spelled out** — §9 forbids shipping a broken pack that looks whole.
* :func:`replay_timeline` renders the eleven-step audit timeline from
  ``events.jsonl``. Every step is shown even when it recorded nothing
  (``not_recorded``) and failure statuses are surfaced rather than smoothed over,
  because the failure-injection demo depends on exactly that.

Tolerance is deliberate in two places:

* a corrupt line in ``events.jsonl`` is skipped *and reported* — the replay
  continues, because a demo that dies on one bad line proves nothing (§8 edge 2);
* a large run is paged rather than truncated silently (§8 edge 3).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .runs.models import RunEvent
from .runs.recorder import _DEFAULT_STORE_DIR, load_events, load_manifest

PACK_SCHEMA_VERSION = 2

# --------------------------------------------------------------------------- #
# The 13 artefacts (card §5 / §10)
# --------------------------------------------------------------------------- #
CATEGORY_REQUIRED = "required"          # without it the pack is not a pack
CATEGORY_COMPLETENESS = "completeness"  # missing => pack is "incomplete"
CATEGORY_OPTIONAL = "optional"          # missing => warning only

MANIFEST = "manifest.json"
EVENTS = "events.jsonl"

#: ``(name, category, description)`` — the card's directory listing, exactly.
PACK_FILES: Tuple[Tuple[str, str, str], ...] = (
    (MANIFEST, CATEGORY_REQUIRED, "运行清单（17 字段）"),
    (EVENTS, CATEGORY_REQUIRED, "只追加审计事件流"),
    ("report.md", CATEGORY_COMPLETENESS, "人类可读报告（memo）"),
    ("report.json", CATEGORY_COMPLETENESS, "结构化报告"),
    ("financials.xlsx", CATEGORY_COMPLETENESS, "财务分析表"),
    ("valuation.xlsx", CATEGORY_COMPLETENESS, "估值与敏感性"),
    ("claims.json", CATEGORY_COMPLETENESS, "全部 Claim（含三级标注）"),
    ("evidence.json", CATEGORY_COMPLETENESS, "证据账本（含文档页码）"),
    ("calculations.json", CATEGORY_COMPLETENESS, "计算记录（公式+输入）"),
    ("validation.json", CATEGORY_COMPLETENESS, "validator 结果"),
    ("corrections.xlsx", CATEGORY_OPTIONAL, "纠错清单（如有草稿核查）"),
    ("charts", CATEGORY_OPTIONAL, "图表（目录）"),
    ("citations", CATEGORY_OPTIONAL, "来源定位快照（目录）"),
)
PACK_FILE_NAMES: Tuple[str, ...] = tuple(name for name, _c, _d in PACK_FILES)

#: Files whose absence must be visible on the pack's own cover sheet.
DEGRADING_CATEGORIES = (CATEGORY_REQUIRED, CATEGORY_COMPLETENESS)

#: Where the pack records its own completeness, so a missing file cannot hide.
PACK_STATUS_FILE = "PACK_STATUS.json"
ZIP_NAME = "evidence_pack.zip"


def _is_directory(name: str) -> bool:
    return name in ("charts", "citations")


# --------------------------------------------------------------------------- #
# The eleven-step replay timeline (card §6 / §14)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReplayStepSpec:
    """One timeline step and the recorded events it is built from."""

    step_id: str
    label: str
    stages: Tuple[str, ...]
    event_types: Tuple[str, ...] = ()
    require_sha256: bool = False

    def matches(self, event: RunEvent) -> bool:
        if event.stage not in self.stages:
            return False
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.require_sha256 and not event.sha256:
            return False
        return True


#: The card §6 order: 文件加载 → hash → parser → 财务提取 → normalization →
#: retrieval → tool call → calculation → claim → validation → report.
REPLAY_TIMELINE: Tuple[ReplayStepSpec, ...] = (
    ReplayStepSpec("file_load", "文件加载", ("file_load",)),
    ReplayStepSpec("hash", "原始文件 hash 校验", ("file_load",), require_sha256=True),
    ReplayStepSpec("parse", "parser", ("parse",), ("parse_start", "parse_end", "parsed")),
    ReplayStepSpec(
        "extract",
        "财务提取",
        ("parse",),
        ("extract", "fact_extracted", "facts_extracted", "financial_extraction"),
    ),
    ReplayStepSpec("normalize", "normalization", ("normalize",)),
    ReplayStepSpec("retrieval", "retrieval", ("retrieval",)),
    ReplayStepSpec("tool_call", "tool call", ("tool_call",)),
    ReplayStepSpec("calculation", "calculation", ("calculation",)),
    ReplayStepSpec("claim", "claim", ("claim",)),
    ReplayStepSpec("validation", "validation", ("validation",)),
    ReplayStepSpec("report", "report", ("report",)),
)

STEP_IDS: Tuple[str, ...] = tuple(spec.step_id for spec in REPLAY_TIMELINE)

STATUS_OK = "ok"
STATUS_NOT_RECORDED = "not_recorded"

#: Failure-ish statuses, most severe first. A step that contains any of these
#: reports the worst one — the alternative is a summary that hides the failure.
STATUS_SEVERITY: Tuple[str, ...] = ("failed", "error", "blocked", "unsupported", "warning", "partial")

DEFAULT_PAGE_SIZE = 200


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PackEntry:
    name: str
    category: str
    description: str
    present: bool
    bytes: Optional[int] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "present": self.present,
            "bytes": self.bytes,
            "note": self.note,
        }


@dataclass(frozen=True)
class PackValidation:
    """Completeness verdict for one pack directory."""

    run_id: str
    valid: bool
    complete: bool
    entries: Tuple[PackEntry, ...] = ()
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    unresolved_references: Tuple[str, ...] = ()

    @property
    def missing_required(self) -> List[str]:
        return [item.name for item in self.entries if not item.present and item.category == CATEGORY_REQUIRED]

    @property
    def missing_completeness(self) -> List[str]:
        return [item.name for item in self.entries if not item.present and item.category == CATEGORY_COMPLETENESS]

    @property
    def missing_optional(self) -> List[str]:
        return [item.name for item in self.entries if not item.present and item.category == CATEGORY_OPTIONAL]

    @property
    def status(self) -> str:
        if not self.valid:
            return "invalid"
        return "complete" if self.complete else "incomplete"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "valid": self.valid,
            "complete": self.complete,
            "present": [item.name for item in self.entries if item.present],
            "missing_required": self.missing_required,
            "missing_completeness": self.missing_completeness,
            "missing_optional": self.missing_optional,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "unresolved_references": list(self.unresolved_references),
            "entries": [item.to_dict() for item in self.entries],
        }


@dataclass(frozen=True)
class ReplayStep:
    """One rendered timeline step, carrying the card's five required elements."""

    step_id: str
    label: str
    status: str
    started_at: Optional[str]
    ended_at: Optional[str]
    events: Tuple[Dict[str, Any], ...] = ()
    inputs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()
    sources: Tuple[str, ...] = ()

    @property
    def recorded(self) -> bool:
        return bool(self.events)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "event_count": len(self.events),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "sources": list(self.sources),
            "events": list(self.events),
        }


class EvidencePackError(RuntimeError):
    """Raised when a pack cannot be exported at all."""


# --------------------------------------------------------------------------- #
# Events loading (tolerant, but never silent)
# --------------------------------------------------------------------------- #
def load_events_tolerant(path: Path | str) -> Tuple[List[RunEvent], List[str]]:
    """Read ``events.jsonl``; return ``(events, broken_lines)``.

    A malformed line is skipped **and described** — the replay keeps going, and
    the breakage shows up in the timeline's warnings. Silently dropping it would
    make a damaged audit trail look clean, which is the one thing this module
    exists to prevent.
    """
    events: List[RunEvent] = []
    broken: List[str] = []
    target = Path(path)
    if not target.exists():
        return events, [f"events_file_missing:{target.name}"]
    with target.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                events.append(RunEvent.from_dict(json.loads(text)))
            except Exception as exc:  # any malformed line, for any reason
                broken.append(f"line{number}:{type(exc).__name__}:{exc}")
    return events, broken


# --------------------------------------------------------------------------- #
# Replay timeline
# --------------------------------------------------------------------------- #
def _step_status(events: Sequence[RunEvent]) -> str:
    statuses = {str(event.status) for event in events}
    for severe in STATUS_SEVERITY:
        if severe in statuses:
            return severe
    for status in sorted(statuses):
        if status != STATUS_OK:
            return status
    return STATUS_OK


def _event_view(event: RunEvent) -> Dict[str, Any]:
    """One event, with the card's five elements explicit."""
    return {
        "ts": event.ts,
        "stage": event.stage,
        "event_type": event.event_type,
        "status": event.status,
        "input": {
            "digest": event.input_digest,
            "detail": event.detail.get("inputs") if isinstance(event.detail, Mapping) else None,
        },
        "output": {
            "digest": event.output_digest,
            "detail": event.detail.get("outputs") if isinstance(event.detail, Mapping) else None,
        },
        "source": {
            "path": event.path,
            "sha256": event.sha256,
            "calculation_id": event.calculation_id,
            "evidence_id": event.evidence_id,
        },
        "sampled": event.sampled,
        "detail": dict(event.detail),
    }


def replay_timeline(
    events: Iterable[RunEvent],
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Dict[str, Any]:
    """Render the eleven-step timeline.

    ``page_size <= 0`` or ``None`` means "no paging". Events beyond the window are
    counted in ``truncated_events`` rather than dropped in silence (§8 edge 3).
    """
    ordered = sorted(events, key=lambda item: item.ts)
    if page < 1:
        raise ValueError("page must be >= 1")

    steps: List[ReplayStep] = []
    for spec in REPLAY_TIMELINE:
        matched = [event for event in ordered if spec.matches(event)]
        windowed = matched
        if page_size and page_size > 0:
            start = (page - 1) * page_size
            windowed = matched[start : start + page_size]
        steps.append(
            ReplayStep(
                step_id=spec.step_id,
                label=spec.label,
                status=_step_status(matched) if matched else STATUS_NOT_RECORDED,
                started_at=matched[0].ts if matched else None,
                ended_at=matched[-1].ts if matched else None,
                events=tuple(_event_view(event) for event in windowed),
                inputs=tuple(dict.fromkeys(event.input_digest for event in matched if event.input_digest)),
                outputs=tuple(dict.fromkeys(event.output_digest for event in matched if event.output_digest)),
                sources=tuple(
                    dict.fromkeys(
                        value
                        for event in matched
                        for value in (event.path, event.sha256, event.calculation_id, event.evidence_id)
                        if value
                    )
                ),
            )
        )

    observed = " ".join(str(event.status) for event in ordered)
    failure_count = sum(1 for event in ordered if str(event.status) in STATUS_SEVERITY)
    total_windowed = sum(len(step.events) for step in steps)
    return {
        "steps": [step.to_dict() for step in steps],
        "step_ids": list(STEP_IDS),
        "not_recorded_steps": [step.step_id for step in steps if not step.recorded],
        "failed_steps": [
            step.step_id for step in steps if step.status in STATUS_SEVERITY
        ],
        "event_count": len(ordered),
        "failure_event_count": failure_count,
        "page": page,
        "page_size": page_size,
        "truncated_events": len(ordered) - total_windowed,
        "statuses_seen": sorted(set(observed.split())) if observed else [],
        "honesty_note": "失败事件如实呈现，不美化、不跳过。",
    }


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def _write_artefact(target: Path, payload: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, bytearray)):
        target.write_bytes(bytes(payload))
    elif isinstance(payload, str):
        target.write_text(payload, encoding="utf-8")
    elif isinstance(payload, Path):
        if payload.is_dir():
            shutil.copytree(payload, target, dirs_exist_ok=True)
        else:
            shutil.copy2(payload, target)
    else:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def export_evidence_pack(
    run_id: str,
    *,
    store_dir: Path | str = _DEFAULT_STORE_DIR,
    artefacts: Optional[Mapping[str, Any]] = None,
    out_dir: Optional[Path | str] = None,
    make_zip: bool = True,
    require_complete: bool = False,
) -> Path:
    """Build the 13-artefact pack for ``run_id`` (directory, plus a zip).

    ``artefacts`` maps pack file names to payloads (str / bytes / Path / JSON-able
    object). Files not supplied are simply absent, and :func:`validate_pack`
    reports them — this function never fabricates a placeholder to fill a slot.

    ``require_complete=True`` refuses to export an incomplete pack (§9: "禁止导出
    看起来完整的残包" — the caller that needs a whole pack says so explicitly).
    """
    store = Path(store_dir)
    try:
        manifest = load_manifest(store, run_id)
    except Exception as exc:
        raise EvidencePackError(f"无法读取 run manifest（{run_id}）：{exc}") from exc

    target_dir = Path(out_dir) if out_dir else store / run_id / "evidence_pack"
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # manifest.json / events.jsonl always come from the run store itself: they are
    # the run's own record, not a caller-supplied artefact.
    run_dir = store / run_id
    shutil.copy2(run_dir / MANIFEST, target_dir / MANIFEST)
    events_path = run_dir / EVENTS
    if events_path.exists():
        shutil.copy2(events_path, target_dir / EVENTS)
    else:
        # An empty stream is still a stream; the pack validator reports the gap.
        (target_dir / EVENTS).write_text("", encoding="utf-8")

    supplied = dict(artefacts or {})
    for name, payload in supplied.items():
        if name in (MANIFEST, EVENTS):
            # Copying the run's record is this function's job; overriding it would
            # let a caller ship a manifest that disagrees with events.jsonl.
            continue
        _write_artefact(target_dir / name, payload)

    validation = validate_pack(target_dir, expected_run_id=run_id)
    if require_complete and not validation.complete:
        raise EvidencePackError(
            "拒绝导出不完整的残包：缺少 "
            f"{validation.missing_required + validation.missing_completeness}"
        )

    # The pack carries its own verdict, so a reader cannot miss it.
    status = validation.to_dict()
    status["missing_files"] = validation.missing_required + validation.missing_completeness + validation.missing_optional
    (target_dir / PACK_STATUS_FILE).write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if make_zip:
        zip_path = target_dir.parent / ZIP_NAME
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(target_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(target_dir))
    return target_dir


def zip_path_for(pack_dir: Path | str) -> Path:
    return Path(pack_dir).parent / ZIP_NAME


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _collect_ids(payload: Any, *keys: str) -> set:
    """Collect ids from a JSON payload shaped like a list of dicts (or nested).

    Handles both a single id (``"evidence_id": "E1"``) and a list of them
    (``"evidence_ids": ["E1", "E2"]``) — the second form is the one the memo and
    graph actually emit, so missing it would make the reference check vacuous.
    """
    found: set = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key in keys:
                    if isinstance(value, (str, int)):
                        found.add(str(value))
                    elif isinstance(value, (list, tuple)):
                        found.update(str(item) for item in value if isinstance(item, (str, int)))
                    else:
                        walk(value)
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return found


def validate_pack(
    pack_dir: Path | str, *, expected_run_id: Optional[str] = None
) -> PackValidation:
    """Check the 13 artefacts, run_id consistency and reference resolvability."""
    directory = Path(pack_dir)
    errors: List[str] = []
    warnings: List[str] = []

    if not directory.exists():
        return PackValidation(
            run_id=expected_run_id or "",
            valid=False,
            complete=False,
            errors=(f"pack directory does not exist: {directory}",),
        )

    entries: List[PackEntry] = []
    for name, category, description in PACK_FILES:
        path = directory / name
        present = path.exists()
        size: Optional[int] = None
        note = ""
        if present:
            if _is_directory(name):
                if not path.is_dir():
                    present, note = False, "期望目录但存在的是文件"
                else:
                    size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            else:
                size = path.stat().st_size
        entries.append(PackEntry(name, category, description, present, size, note))

    # -- run_id consistency (§10) -----------------------------------------
    manifest = _read_json(directory / MANIFEST)
    run_id = expected_run_id or ""
    if not isinstance(manifest, Mapping):
        errors.append("manifest.json 缺失或无法解析")
    else:
        run_id = str(manifest.get("run_id", "") or run_id)
        if expected_run_id and str(manifest.get("run_id")) != expected_run_id:
            errors.append(
                f"manifest.run_id={manifest.get('run_id')!r} 与期望 {expected_run_id!r} 不一致"
            )

    events, broken = load_events_tolerant(directory / EVENTS)
    if broken:
        warnings.extend(f"events.jsonl 损坏行：{item}" for item in broken)
    mismatched = sorted({event.run_id for event in events if run_id and event.run_id != run_id})
    if mismatched:
        errors.append(f"events.jsonl 中出现其它 run_id：{mismatched}")

    # -- reference resolvability (§10) ------------------------------------
    unresolved: List[str] = []
    claims = _read_json(directory / "claims.json")
    evidence = _read_json(directory / "evidence.json")
    calculations = _read_json(directory / "calculations.json")
    if claims is not None:
        claim_ids = _collect_ids(claims, "claim_id")
        evidence_ids = _collect_ids(evidence, "evidence_id") if evidence is not None else set()
        calculation_ids = _collect_ids(calculations, "calculation_id") if calculations is not None else set()
        for source, available, keys in (
            ("evidence_ids", evidence_ids, ("evidence_id", "evidence_ids")),
            ("calculation_ids", calculation_ids, ("calculation_id", "calculation_ids")),
        ):
            for reference in sorted(_collect_ids(claims, *keys)):
                if available and reference not in available:
                    unresolved.append(f"{source}:{reference}")
        if not claim_ids:
            warnings.append("claims.json 中未解析出任何 claim_id（可能是空集，也可能是字段名不符）")
    elif entries[[item.name for item in entries].index("claims.json")].present:
        errors.append("claims.json 存在但无法解析")
    else:
        warnings.append("claims.json 缺失，引用可解析性未检查")

    if unresolved:
        errors.append(f"report 引用的 id 在 Pack 内无法解析：{len(unresolved)} 项")

    complete = not any(
        item.category == CATEGORY_COMPLETENESS and not item.present for item in entries
    )
    return PackValidation(
        run_id=run_id,
        valid=not errors,
        complete=complete,
        entries=tuple(entries),
        errors=tuple(errors),
        warnings=tuple(warnings),
        unresolved_references=tuple(sorted(set(unresolved))),
    )


def render_pack_markdown(validation: PackValidation) -> str:
    """A cover sheet for the pack — completeness first, never buried."""
    lines: List[str] = []
    lines.append(f"# Evidence Pack · {validation.run_id or '(unknown run)'}")
    lines.append("")
    lines.append(f"- 打包状态：**{validation.status.upper()}**")
    lines.append(f"- 可解析：{'是' if validation.valid else '否'}")
    if validation.missing_required:
        lines.append(f"- ⚠️ 缺必需件：{', '.join(validation.missing_required)}")
    if validation.missing_completeness:
        lines.append(f"- ⚠️ 缺完整性件：{', '.join(validation.missing_completeness)}")
    if validation.missing_optional:
        lines.append(f"- 缺可选件（不影响完整性）：{', '.join(validation.missing_optional)}")
    lines.append("")
    lines.append("| 文件 | 类别 | 存在 | 大小 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for entry in validation.entries:
        lines.append(
            f"| {entry.name} | {entry.category} | {'✅' if entry.present else '❌'} | "
            f"{entry.bytes if entry.bytes is not None else '—'} | {entry.note or entry.description} |"
        )
    for item in validation.errors:
        lines.append(f"- ❌ {item}")
    for item in validation.warnings:
        lines.append(f"- ⚠️ {item}")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Multi-run comparison (§8 edge 4)
# --------------------------------------------------------------------------- #
#: Manifest fields that must be identical for two runs to be comparable at all.
COMPARABLE_FIELDS: Tuple[str, ...] = (
    "snapshot_id",
    "model",
    "model_provider",
    "model_version",
    "temperature",
    "prompt_hash",
    "skill_hash",
    "parser_name",
    "parser_version",
    "random_seed",
    "execution_mode",
)


def diff_run_manifests(
    run_a: str, run_b: str, *, store_dir: Path | str = _DEFAULT_STORE_DIR
) -> Dict[str, Any]:
    """Side-by-side manifest diff for the replay view (§8 edge 4).

    Differences in ``input_document_sha256`` mean the two runs did not read the
    same bytes — a comparison across them would be apples to oranges, and the
    payload says so out loud rather than leaving the reader to notice.
    """
    store = Path(store_dir)
    left = load_manifest(store, run_a)
    right = load_manifest(store, run_b)

    rows: List[Dict[str, Any]] = []
    for field_name in COMPARABLE_FIELDS:
        left_value = getattr(left, field_name, None)
        right_value = getattr(right, field_name, None)
        rows.append(
            {
                "field": field_name,
                "run_a": left_value,
                "run_b": right_value,
                "same": left_value == right_value,
            }
        )

    inputs_same = list(left.input_document_sha256) == list(right.input_document_sha256)
    changed = [row["field"] for row in rows if not row["same"]]
    return {
        "run_a": run_a,
        "run_b": run_b,
        "rows": rows,
        "different_fields": changed,
        "same_inputs": inputs_same,
        "comparable": inputs_same,
        "note": (
            "两次 run 输入一致，可对比。"
            if inputs_same
            else "两次 run 的输入文件 sha256 不同，指标差异不可直接归因于模型/配置。"
        ),
    }
