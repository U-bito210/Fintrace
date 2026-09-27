"""CARD-12 §4 — the module artefacts inside a run directory.

§4 defines the Evidence Pack's input as *"run 目录（manifest.json + events.jsonl +
各模块产物）"*. The recorder has always written the first two; **nothing ever wrote
the third**. So the pack could only ever carry 4 of its 13 declared files, and the
one that did claim to carry Claims was a hand-written placeholder asserting the run
had produced no claim-level artefacts — while the run had in fact registered a
dozen of them. A placeholder that contradicts the run it sits next to is worse than
a missing file: it reads as a finding.

This module is the missing writer. Three rules it follows:

1. **It is not a second serializer.** Every payload is built from the same
   ``to_dict()`` / graph shapes the workbench already renders, so a reader
   comparing the pack against the UI sees one shape rather than two that disagree.
2. **It never invents an artefact the run did not produce.** A document-scoped run
   has no symbol and therefore no valuation; ``valuation.xlsx`` is simply absent,
   and :func:`~src.cn.evidence_pack.validate_pack` reports it. CARD-12 §8 edge 1
   makes an incomplete pack a legitimate state *provided it says so* — which
   ``PACK_STATUS.json`` does, on the pack's own cover sheet — and
   :func:`declared_absences` additionally writes the *reason* into ``report.json``,
   so "this run structurally cannot have it" never has to be re-derived by whoever
   reads the residual pack next.
3. **Every payload names the run it came from**, so the pack's ids stay
   resolvable against its own manifest (§10 rule 3) instead of forming a second,
   disconnected namespace.

What is deliberately *not* here: any recomputation. The artefacts record what the
run asserted; re-deriving values at export time would make the pack a claim about
the exporter rather than a record of the run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: The artefacts this module can write, in the order they land on disk. Names are
#: the pack's own file names so a mismatch cannot go unnoticed.
RUN_ARTEFACT_NAMES: Tuple[str, ...] = (
    "report.md",
    "report.json",
    "claims.json",
    "evidence.json",
    "calculations.json",
    "validation.json",
    "financials.xlsx",
)

#: Column order for ``financials.xlsx``. Declared once so the sheet and the
#: ``report.json`` it mirrors cannot drift apart.
_METRIC_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("metric", "指标键"),
    ("label", "指标"),
    ("value", "数值"),
    ("unit", "单位"),
    ("period_key", "期间"),
    ("period_basis", "期间口径"),
    ("comparison", "比较类型"),
    ("comparison_semantic", "比较语义"),
    ("delta_abs", "变动额"),
    ("delta_pct", "变动率(%)"),
    ("scope", "口径"),
    ("version", "版本"),
    ("currency", "币种"),
    ("calculation_id", "计算 id"),
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _manifest_brief(run_dir: Path) -> Dict[str, Any]:
    """The run header, read back from the manifest the recorder already wrote.

    Read from disk rather than passed in: it makes the artefacts a function of the
    run directory alone, so re-writing them later cannot produce a header that
    disagrees with the manifest sitting next to it.
    """
    path = run_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        "run_id": data.get("run_id"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "input_document_count": len(data.get("input_document_sha256") or ()),
        "parser_name": data.get("parser_name"),
        "parser_version": data.get("parser_version"),
        "execution_mode": data.get("execution_mode"),
        "snapshot_id": data.get("snapshot_id"),
        "git_commit": data.get("git_commit"),
        "random_seed": data.get("random_seed"),
        "model": data.get("model"),
    }


def _claims_payload(state: Any, run_id: str) -> Dict[str, Any]:
    """All claims, with the same fields the graph page shows one by one."""
    from .graph.propagation import claim_status, structural_status

    graph = state.graph
    rows: List[Dict[str, Any]] = []
    for claim_id, claim in sorted(graph.claims.items()):
        propagated, reasons, blocked = claim_status(graph, claim)
        rows.append(
            {
                "claim_id": claim_id,
                "claim_type": claim.claim_type,
                "text": claim.text,
                "metric": claim.metric,
                "period_key": claim.period_key,
                "scope": getattr(claim, "scope", None),
                "validation_status": claim.validation_status,
                "verification_status": claim.verification_status,
                "structural_status": structural_status(graph, claim),
                "propagated_status": propagated,
                "reasons": list(reasons),
                # ``claim_status`` returns the blocking trigger as a single
                # ``"target_type:target_id:state"`` string (or None), not a list —
                # ``list(blocked)`` would shred it into characters.
                "blocked_by": blocked,
                "evidence_ids": list(claim.evidence_ids),
                "calculation_ids": list(claim.calculation_ids),
                "derived_from": list(claim.derived_from),
                "assumption_ids": list(claim.assumption_ids),
            }
        )
    return {
        "run_id": run_id,
        "document_id": state.document_id,
        "claim_count": len(rows),
        "by_type": _count_by(rows, "claim_type"),
        "by_validation_status": _count_by(rows, "validation_status"),
        "claims": rows,
    }


def _count_by(rows: List[Mapping[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _evidence_payload(state: Any, run_id: str) -> Dict[str, Any]:
    """The evidence ledger, plus the page each id could be located to.

    The ledger's own records carry no page — a normalised fact knows its source
    cell (``field_path``) but not always the printed page. The graph does know,
    for the evidence claims actually lean on. So the pages travel **separately**
    and the ones we could not locate are listed by name: filling in a page number
    we do not have would turn "unknown" into a false citation.
    """
    graph = state.graph
    ledger = state.ledger.to_dict() if hasattr(state.ledger, "to_dict") else {}

    located: Dict[str, Dict[str, Any]] = {}
    unlocated: List[str] = []
    for evidence_id, node in sorted(graph.evidence_index.items()):
        if getattr(node, "page", None):
            located[evidence_id] = {
                "document_id": node.document_id,
                "page": node.page,
                "jumpable": True,
            }
        else:
            unlocated.append(evidence_id)
            located[evidence_id] = {
                "document_id": node.document_id,
                "page": None,
                "jumpable": False,
            }

    return {
        "run_id": run_id,
        "document_id": state.document_id,
        "snapshot_id": ledger.get("snapshot_id"),
        "record_count": len(ledger.get("records") or ()),
        "records": list(ledger.get("records") or ()),
        "page_locations": located,
        "page_unlocated": unlocated,
        "note": (
            "page_locations 只对有页面号的证据给 page；无页面号的进 page_unlocated，"
            "不补一个猜出来的页码。"
        ),
    }


def _calculations_payload(state: Any, run_id: str) -> Dict[str, Any]:
    """Formula plus inputs for every calculation — what the card asks for."""
    graph = state.graph
    rows: List[Dict[str, Any]] = []
    for calc_id, node in sorted(graph.calculations.items()):
        output = node.output if isinstance(node.output, dict) else {}
        rows.append(
            {
                "calculation_id": calc_id,
                "metric": node.metric,
                "formula": node.formula,
                "operation": node.operation,
                "scope": node.scope,
                "currency": node.currency,
                "validation_status": node.validation_status,
                "output": output,
                "inputs": [
                    {
                        "kind": operand.kind,
                        "ref": operand.ref,
                        "value": operand.value,
                        "label": operand.label,
                    }
                    for operand in node.inputs
                ],
            }
        )
    return {
        "run_id": run_id,
        "document_id": state.document_id,
        "calculation_count": len(rows),
        "by_validation_status": _count_by(rows, "validation_status"),
        "calculations": rows,
    }


def _validation_payload(state: Any, run_id: str) -> Dict[str, Any]:
    """What the validator concluded, including the parts it could not check."""
    graph = state.graph
    analysis = state.analysis
    claim_rows = _claims_payload(state, run_id)["claims"]

    return {
        "run_id": run_id,
        "document_id": state.document_id,
        "claim_count": len(claim_rows),
        "by_validation_status": _count_by(claim_rows, "validation_status"),
        "by_propagated_status": _count_by(claim_rows, "propagated_status"),
        "blocked_claims": [
            {"claim_id": row["claim_id"], "blocked_by": row["blocked_by"], "reasons": row["reasons"]}
            for row in claim_rows
            if row["propagated_status"] != "supported" or row["blocked_by"]
        ],
        "stale_claims": list(graph.stale_claims),
        "deactivations": list(graph.deactivations),
        "warnings": list(graph.warnings),
        "analysis_warnings": list(getattr(analysis, "warnings", ()) or ()),
        "analysis_missing": list(getattr(analysis, "missing", ()) or ()),
        "data_complete": getattr(analysis, "data_complete", None),
        "note": "blocked_claims 含被传播阻断与引用缺失两类，理由原样保留，不做归并。",
    }


def declared_absences(state: Any) -> Dict[str, str]:
    """哪些 pack 件是**这个 run 结构上产不出来**的，以及为什么。

    「缺件」有两种，混为一谈就会把口径说成缺陷：① 本该有、但这次没写出来 —— 那是
    真缺口；② 这个 run 根本没有产生它的输入 —— 那是口径。估值表属于②：CARD-08 的
    估值要 symbol 级行情，而本 run 的输入是一份文档。判据也取自同一处事实：账本里
    没有市价证据（``metric == "close"``），就没有估值可算。
    """
    records = state.ledger.records() if hasattr(state.ledger, "records") else []
    has_market = any(str(getattr(record, "metric", "")) == "close" for record in records)
    if has_market:
        return {}
    return {
        "valuation.xlsx": (
            "本 run 的输入是文档（无 symbol），账本里没有市价证据，因此没有估值可算；"
            "估值是 CARD-08 对 symbol 级 run 的产出，不该在这里补一个空表凑数。"
        )
    }


def run_scope(state: Any) -> str:
    """``"symbol"`` 或 ``"document"`` —— 由运行事实决定，不由调用方宣称。"""
    return "document" if declared_absences(state) else "symbol"


def _report_payload(state: Any, run_dir: Path, run_id: str) -> Dict[str, Any]:
    """The structured report: run header + analysis + graph roll-up."""
    analysis = state.analysis.to_dict()
    claims = _claims_payload(state, run_id)
    absences = declared_absences(state)
    return {
        "run_id": run_id,
        "scope": run_scope(state),
        "manifest": _manifest_brief(run_dir),
        "case": {
            "key": state.case.key,
            "label": state.case.label,
            "filename": state.case.filename,
            "purpose": state.case.purpose,
        },
        "document_id": state.document_id,
        "analysis": analysis,
        "graph": {
            "claim_count": claims["claim_count"],
            "calculation_count": len(state.graph.calculations),
            "evidence_count": len(state.graph.evidence_index),
            "thesis_count": len(state.graph.theses),
        },
        "warnings": list(state.graph.warnings) + list(getattr(state.analysis, "warnings", ()) or ()),
        #: 机器可读的缺件归因。pack 校验只报「缺 valuation.xlsx」，读到它才知道那是
        #: 口径而不是漏写 —— 否则每读一次残包都要重新推一遍为什么。
        "declared_absences": absences,
        "note": (
            "本 run 是文档级（解析 → 归一化 → 计算 → 结论 → 校验）；"
            "不含估值与备忘段，故 pack 里没有 valuation.xlsx。"
            if absences
            else "本 run 含 symbol 级证据，估值等件按 CARD-08 产出。"
        ),
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    """Human-readable twin of ``report.json``. Same numbers, not a re-derivation."""
    manifest = payload.get("manifest") or {}
    case = payload.get("case") or {}
    analysis = payload.get("analysis") or {}
    graph = payload.get("graph") or {}

    lines: List[str] = [
        f"# Run 报告 · {payload.get('run_id')}",
        "",
        f"- 语料：{case.get('label')}（`{case.get('filename')}`）",
        f"- 解析器：{manifest.get('parser_name')}@{manifest.get('parser_version')}",
        f"- 执行模式：{manifest.get('execution_mode')}",
        f"- git commit：{manifest.get('git_commit')}",
        f"- 输入文档数：{manifest.get('input_document_count')}",
        f"- document_id：`{payload.get('document_id')}`",
        "",
        "## 图谱规模",
        "",
        f"- Claim {graph.get('claim_count')} ｜ 计算 {graph.get('calculation_count')}"
        f" ｜ 证据 {graph.get('evidence_count')} ｜ Thesis {graph.get('thesis_count')}",
        "",
        "## 分析指标",
        "",
        "| 指标 | 数值 | 单位 | 期间 | 比较 | 变动额 | 变动率(%) | 计算 id |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in analysis.get("metrics") or ():
        lines.append(
            "| {label} | {value} | {unit} | {period} | {comparison} | {delta_abs} | "
            "{delta_pct} | {calc} |".format(
                label=row.get("label") or row.get("metric"),
                value=row.get("value"),
                unit=row.get("unit") or "",
                period=row.get("period_key") or "",
                comparison=row.get("comparison") or "",
                delta_abs=row.get("delta_abs"),
                delta_pct=row.get("delta_pct"),
                calc=row.get("calculation_id") or "",
            )
        )
    lines.extend(["", f"> 口径：{analysis.get('scope')} / {analysis.get('version')}"
                  f" / {analysis.get('currency')} ｜ 期间 {analysis.get('report_period')}"])

    warnings = list(payload.get("warnings") or ())
    lines.extend(["", "## 告警", ""])
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("- （无）")

    absences = payload.get("declared_absences") or {}
    if absences:
        lines.extend(["", "## 结构上不产出的件", ""])
        lines.extend(f"- `{name}`：{reason}" for name, reason in sorted(absences.items()))

    lines.extend(["", f"> {payload.get('note')}", ""])
    return "\n".join(lines)


def _workbook_stamp(started_at: Optional[str]) -> Optional[Any]:
    """把 run 的起始时刻转成 openpyxl 能写的**朴素 UTC** datetime。

    openpyxl 默认给工作簿盖「保存这一刻」的 ``created/modified``，于是同一个 run
    导出两次会得到两份字节不同的 ``financials.xlsx``（实测相隔 1.2 秒即不同）。
    这份表属于 run，不属于导出动作：用 run 自己的 ``started_at`` 覆盖，产物才对
    run 可复现。取不到就返回 None，宁可留 openpyxl 的默认值，也不编一个时间。
    """
    if not started_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(started_at))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


#: ``docProps/core.xml`` 里 openpyxl 自己盖章的两个时间戳。``properties.modified``
#: 设了也没用 —— openpyxl 3.1.5 在保存时把它重写成「此刻」（``created`` 倒是听话）。
#: 所以这里在冻结 zip 时把两个都改写成 run 的起始时刻。
_CORE_STAMP_FIELDS = (b"created", b"modified")


def _freeze_core(props: bytes, stamp: datetime) -> bytes:
    iso = stamp.strftime("%Y-%m-%dT%H:%M:%SZ").encode("ascii")
    for field in _CORE_STAMP_FIELDS:
        open_tag = b"<dcterms:" + field
        start = props.find(open_tag)
        if start < 0:
            continue
        body_start = props.find(b">", start) + 1
        body_end = props.find(b"</dcterms:" + field + b">", body_start)
        if body_end < 0:
            continue
        props = props[:body_start] + iso + props[body_end:]
    return props


def _freeze_zip(payload: bytes, stamp: datetime) -> bytes:
    """把 xlsx 里每个 zip 条目（及 core.xml 的时间戳）钉在 ``stamp`` 上。

    openpyxl 写盘时给每个条目盖「保存这一刻」的时间，于是**同一个 run** 导出两次
    会得到两份字节不同的 ``financials.xlsx``（实测相隔 1.2 秒即不同）。表属于 run
    而不是导出动作：条目时间取 run 起始时刻，产物才对该 run 可复现 —— 否则将来任
    何「同一 run 的 pack 应当逐字节一致」的校验都会莫名其妙地红。
    """
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

    frozen_stamp = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    buffer = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(buffer, "w", ZIP_DEFLATED, allowZip64=True) as target:
        for info in sorted(source.infolist(), key=lambda item: item.filename):
            data = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                data = _freeze_core(data, stamp)
            entry = ZipInfo(info.filename, date_time=frozen_stamp)
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = info.external_attr
            entry.create_system = info.create_system
            target.writestr(entry, data)
    return buffer.getvalue()


def _financials_workbook(analysis: Mapping[str, Any], *, created_at: Optional[str] = None) -> bytes:
    """``financials.xlsx`` — the analysis table, mirroring ``report.json``."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    stamp = _workbook_stamp(created_at)
    if stamp is not None:
        workbook.properties.created = stamp
        workbook.properties.modified = stamp
    sheet = workbook.active
    sheet.title = "分析指标"

    keys = [key for key, _label in _METRIC_COLUMNS]
    labels = [label for _key, label in _METRIC_COLUMNS]
    sheet.append(labels)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in analysis.get("metrics") or ():
        sheet.append([_cell_value(row.get(key)) for key in keys])

    # 「分析指标」这一张只放指标行：把口径/告警夹在同一张表里，读表的人就得靠
    # 「遇到空行就停」来切数据区 —— 那种约定没人会写在表头里。
    notes = workbook.create_sheet("口径与告警")
    notes.append(["类别", "内容"])
    for cell in notes[1]:
        cell.font = Font(bold=True)
    notes.append(["口径", _cell_value(analysis.get("scope"))])
    notes.append(["版本", _cell_value(analysis.get("version"))])
    notes.append(["币种", _cell_value(analysis.get("currency"))])
    notes.append(["报告期", _cell_value(analysis.get("report_period"))])
    notes.append(["数据完整", _cell_value(analysis.get("data_complete"))])
    for item in analysis.get("warnings") or ():
        notes.append(["warning", _cell_value(item)])
    for item in analysis.get("missing") or ():
        notes.append(["missing", _cell_value(item)])
    if not (analysis.get("warnings") or analysis.get("missing")):
        notes.append(["—", "（无告警、无缺项）"])

    buffer = BytesIO()
    workbook.save(buffer)
    payload = buffer.getvalue()
    return _freeze_zip(payload, stamp) if stamp is not None else payload


def _cell_value(value: Any) -> Any:
    """openpyxl writes numbers, not enums or tuples. Strings stay readable."""
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(getattr(value, "value", value))


def build_artefacts(state: Any, run_dir: Path, run_id: str) -> Dict[str, Any]:
    """Payload for each artefact, keyed by pack file name.

    Split out from :func:`write_run_artefacts` so tests can assert the *content*
    without touching disk.
    """
    run_dir = Path(run_dir)
    report = _report_payload(state, run_dir, run_id)
    analysis = report["analysis"]
    return {
        "report.md": _report_markdown(report),
        "report.json": report,
        "claims.json": _claims_payload(state, run_id),
        "evidence.json": _evidence_payload(state, run_id),
        "calculations.json": _calculations_payload(state, run_id),
        "validation.json": _validation_payload(state, run_id),
        "financials.xlsx": _financials_workbook(
            analysis, created_at=(report.get("manifest") or {}).get("started_at")
        ),
    }


def write_run_artefacts(
    run_dir: Path | str, *, state: Any, run_id: Optional[str] = None
) -> Dict[str, Path]:
    """Persist every artefact this run actually produced, and return their paths.

    ``valuation.xlsx`` is not among them and will not be synthesised: valuation is
    CARD-08's output for a symbol-scoped run, and this run is document-scoped. The
    pack reports its absence rather than the writer faking a workbook.
    """
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    artefacts = build_artefacts(state, directory, run_id or directory.name)

    written: Dict[str, Path] = {}
    for name in RUN_ARTEFACT_NAMES:
        payload = artefacts[name]
        target = directory / name
        if isinstance(payload, (bytes, bytearray)):
            target.write_bytes(bytes(payload))
        else:
            target.write_text(
                payload if isinstance(payload, str) else _dump(payload), encoding="utf-8"
            )
        written[name] = target
    return written
