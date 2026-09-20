"""CARD-15 §10 — workbench Demo 页面的**只读服务层**。

卡 §10 的原话是「Demo 页面不直接读数据库裸数据 —— 必须走服务层接口」。本模块
就是那个接口：它是 Demo 页面唯一允许触碰 CARD-01/05/09/12/22/23/24/25/26 引擎
的地方，页面自己只做渲染。

三条硬约束，都是验收项而不是风格偏好：

* **只读。** 不写文档注册表、不改图、不往工作区落产物（证据包导出写临时目录）。
  演示页面重放多少次，仓库状态都不变。
* **失败不抛。** 每个公开函数返回带 ``status`` 的信封；拿不到就 ``unavailable``
  并附 ``reason``。卡 §9「页面崩溃不影响后端任务」与「浏览器控制台零报错」是硬项，
  而"某份语料不存在"是完全正常的起点状态 —— 它应该显示成一句人话，不是 traceback。
* **不编数字。** 引擎说什么就返回什么。缺输入就报缺，绝不用替代值填补。

**零 LLM。** 本模块只调用确定性引擎；没有模型客户端，没有网络。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
DOCUMENTS_DIR = FIXTURES / "documents"
DRAFTS_DIR = FIXTURES / "report_drafts"

#: 演示 run 的落点。``output/`` 已在 .gitignore 内，重放不会污染仓库。
RUN_STORE_DIR = PROJECT_ROOT / "output" / "demo" / "runs"
#: 证据包导出的落点（同样是忽略目录）。
PACK_DIR = PROJECT_ROOT / "output" / "demo" / "packs"

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"


class DemoServiceError(RuntimeError):
    """内部错误类型；公开函数会把它转成 ``unavailable`` 信封，不外抛。"""


# --------------------------------------------------------------------------- #
# 演示案例目录
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DemoCase:
    """一份可用于演示的语料，以及它在剧本里承担的角色。"""

    key: str
    label: str
    filename: str
    declared_type: str
    purpose: str


#: 案卷类语料。选择依据是实测产出规模（见 CARD-15 §23），不是随手挑的。
FILING_CASES: Tuple[DemoCase, ...] = (
    DemoCase(
        "acme_break",
        "ACME 约束破坏（植入）",
        "annual_07_acme_constraint_break.pdf",
        "annual",
        "刻意写坏会计约束的年报，用于演示「不可能成立的解析结果被抓出来」",
    ),
    DemoCase(
        "equity",
        "股本/权益完整年报",
        "annual_06_equity_shares.pdf",
        "annual",
        "指标、计算与证据最齐全的一份，图谱/护照/脆弱性默认案例",
    ),
    DemoCase(
        "restatement",
        "重述对比列年报",
        "annual_04_restated_comparative.pdf",
        "annual",
        "带重述与调整后列，口径信号最多，用于演示可比性三态",
    ),
    DemoCase(
        "profit_continued",
        "本期/累计双列年报",
        "annual_01_profit_continued.pdf",
        "annual",
        "本期/累计双列，用于演示 YTD→单季换算的必要性",
    ),
)

DEFAULT_FILING_KEY = "equity"
DEFAULT_ACME_KEY = "acme_break"


def filing_cases() -> List[Dict[str, Any]]:
    return [
        {
            "key": case.key,
            "label": case.label,
            "filename": case.filename,
            "purpose": case.purpose,
            "present": (DOCUMENTS_DIR / case.filename).exists(),
        }
        for case in FILING_CASES
    ]


def draft_cases() -> List[Dict[str, Any]]:
    """CARD-05 的草稿语料：哪些是干净的、哪些是植入的。"""
    from .checker.draft_corpus import DRAFT_SPECS

    return [
        {
            "key": spec.doc_id,
            "label": spec.title or spec.filename,
            "filename": spec.filename,
            "planted": bool(spec.expected),
            "expected_findings": len(spec.expected),
            "as_of": spec.as_of,
            "present": (DRAFTS_DIR / spec.filename).exists(),
        }
        for spec in DRAFT_SPECS
    ]


# --------------------------------------------------------------------------- #
# 案例构建（懒加载 + 进程内缓存）
# --------------------------------------------------------------------------- #
@dataclass
class _FilingState:
    case: DemoCase
    registry: Any
    document_id: str
    document: Any
    normalization: Any
    analysis: Any
    ledger: Any
    graph: Any
    workdir: Path


_FILINGS: Dict[str, _FilingState] = {}
#: 草稿案例：注册表保持打开，跳转要能回查。键是 CARD-05 的 ``doc_id``。
_DRAFT_STATES: Dict[str, Dict[str, Any]] = {}
#: ``document_id -> 本地文件``。跳转要能回到**任何**被摄入过的文件（案卷与草稿
#: 都算），而证据索引里只有 document_id —— 没有这张表就只能放弃跳转。
_DOC_PATHS: Dict[str, Path] = {}
#: ``document_id -> 持有它的注册表``。同一份文件可能被多个案例重复摄入（每次是
#: 一个新的 document_id），所以这张表按 document_id 记，而不是按案例。
_DOC_REGISTRIES: Dict[str, Any] = {}


def reset() -> None:
    """丢弃缓存并关闭注册表（测试与显式重载）。"""
    for state in _FILINGS.values():
        try:
            state.registry.close()
        except Exception:  # noqa: BLE001 - 清理失败不该盖住真正的错误
            pass
        shutil.rmtree(state.workdir, ignore_errors=True)
    for draft in _DRAFT_STATES.values():
        try:
            draft["registry"].close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(draft["workdir"], ignore_errors=True)
    _FILINGS.clear()
    _DRAFT_STATES.clear()
    _DOC_PATHS.clear()
    _DOC_REGISTRIES.clear()


def _case_of(key: str) -> DemoCase:
    for case in FILING_CASES:
        if case.key == key:
            return case
    raise DemoServiceError(f"未知的演示案例: {key}")


def _build_filing(key: str) -> _FilingState:
    """CARD-01 → 02 → 03 → 09，一份文档一条链，全确定性。"""
    from .analysis import analyze_document
    from .documents import DocumentRegistry
    from .evidence import EvidenceLedger
    from .graph import build_graph_from_analysis
    from .normalization import normalize_financials
    from .normalization.evidence_bridge import normalization_jumps, register_normalized_evidence

    case = _case_of(key)
    path = DOCUMENTS_DIR / case.filename
    if not path.exists():
        raise DemoServiceError(f"演示语料不存在: {case.filename}")

    workdir = Path(tempfile.mkdtemp(prefix=f"cn_demo_{key}_"))
    registry = DocumentRegistry(root=workdir / "registry.db", allow_roots=[DOCUMENTS_DIR])
    try:
        ingested = registry.ingest(path, declared_type=case.declared_type, source="admin_import")
        document_id = ingested.document.document_id
        _DOC_PATHS[document_id] = path
        _DOC_REGISTRIES[document_id] = registry
        document = registry.get(document_id)
        normalization = normalize_financials(registry.get_facts(document_id), document)
        analysis = analyze_document(normalization, documents=document)
        ledger = EvidenceLedger(snapshot_id=document_id)
        register_normalized_evidence(document, normalization, ledger=ledger)
        jumps = normalization_jumps(document, normalization)
        graph = build_graph_from_analysis(
            analysis,
            run_id=f"demo-{key}",
            document_id=document_id,
            ledger=ledger,
            jumps=jumps,
        )
    except Exception:
        registry.close()
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return _FilingState(
        case=case, registry=registry, document_id=document_id, document=document,
        normalization=normalization, analysis=analysis, ledger=ledger, graph=graph,
        workdir=workdir,
    )


def _filing(key: str) -> _FilingState:
    state = _FILINGS.get(key)
    if state is None:
        state = _build_filing(key)
        _FILINGS[key] = state
    return state


def _unavailable(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"status": STATUS_UNAVAILABLE, "reason": reason, **extra}


def _case_brief(state: _FilingState) -> Dict[str, Any]:
    return {
        "key": state.case.key,
        "label": state.case.label,
        "filename": state.case.filename,
        "document_id": state.document_id,
        "company": getattr(state.document, "company", ""),
        "report_period": getattr(state.normalization, "report_period", ""),
        "sha256": getattr(state.document, "sha256", ""),
    }


# --------------------------------------------------------------------------- #
# ① 研报纠错（CARD-05）—— DEMO_FLOW 步骤 6+7，★高潮一
# --------------------------------------------------------------------------- #
def corrections(doc_id: Optional[str] = None) -> Dict[str, Any]:
    """对一份含错研报草稿跑确定性核查，返回可直接渲染的纠错报告。

    默认取**植入错误最多**的那份草稿：演示要看的正是"错误被逐条抓出来"。
    """
    from .checker.draft_corpus import DRAFT_SPECS

    try:
        specs = list(DRAFT_SPECS)
        if not specs:
            return _unavailable("语料目录里没有草稿规格。")
        if doc_id:
            spec = next((item for item in specs if item.doc_id == doc_id), None)
            if spec is None:
                return _unavailable(f"未知草稿: {doc_id}")
        else:
            spec = max(specs, key=lambda item: len(item.expected))
        state = _DRAFT_STATES.get(spec.doc_id)
        if state is None:
            state = _build_draft_state(spec)
            _DRAFT_STATES[spec.doc_id] = state
        cached = state["report"]
    except Exception as exc:  # noqa: BLE001 - 页面永不因语料而崩
        return _unavailable(f"{type(exc).__name__}: {exc}")

    findings = list(cached.get("findings") or ())
    by_type: Dict[str, int] = {}
    for item in findings:
        key = str(item.get("check_type") or "unknown")
        by_type[key] = by_type.get(key, 0) + 1

    return {
        "status": STATUS_OK,
        "doc_id": spec.doc_id,
        "filename": spec.filename,
        "title": spec.title,
        "as_of": spec.as_of,
        "planted": [item.to_dict() for item in spec.expected],
        "planted_count": len(spec.expected),
        "findings": findings,
        "finding_count": len(findings),
        "by_check_type": dict(sorted(by_type.items())),
        "claim_count": len(cached.get("claims") or ()),
        "summary": cached.get("summary") or {},
        "coverage": cached.get("coverage") or {},
        "evidence_index": cached.get("evidence_index") or {},
        "draft": cached.get("draft") or {},
        "uncertain": {
            "unextracted": len(cached.get("unextracted_sentences") or ()),
            "no_claim": len(cached.get("no_claim_sentences") or ()),
        },
        "uncertain_detail": list(cached.get("unextracted_sentences") or ()),
        # 草稿不是 FILING_CASES 的成员，跳转要走它自己的注册表与文件 —— 页面据此
        # 知道 evidence_id 该交给 document_id 而不是案例 key。
        "case_key": "",
        "draft_document_id": state["document_id"],
        "all_drafts": draft_cases(),
    }


def _build_draft_state(spec: Any) -> Dict[str, Any]:
    """把一份草稿连同它的参考披露装进临时注册表，并跑一次完整核查。

    注册表**保持打开**（不像 CARD-05 的批处理那样用完即关）：纠错页的每一条
    finding 都要能点回原文，而跳转需要 ``locate_fact`` 还活着。进程退出时由
    :func:`reset` 统一收尾。
    """
    from .checker.draft_corpus import market_documents, market_normalized_facts
    from .checker.pipeline import check_draft
    from .documents import DocumentRegistry

    workdir = Path(tempfile.mkdtemp(prefix=f"cn_demo_draft_{spec.doc_id}_"))
    registry = DocumentRegistry(root=workdir / "registry.db", allow_roots=[DOCUMENTS_DIR, DRAFTS_DIR])
    try:
        for filename in spec.references:
            path = DOCUMENTS_DIR / filename
            if not path.exists():
                continue
            declared = "annual" if str(filename).startswith("annual") else "announcement"
            ingested = registry.ingest(path, declared_type=declared, source="admin_import")
            _DOC_PATHS[ingested.document.document_id] = path
            _DOC_REGISTRIES[ingested.document.document_id] = registry
        draft_path = DRAFTS_DIR / spec.filename
        draft = registry.ingest(draft_path, declared_type="report_draft", source="admin_import")
        _DOC_PATHS[draft.document.document_id] = draft_path
        _DOC_REGISTRIES[draft.document.document_id] = registry
        report = check_draft(
            draft.document.document_id,
            registry=registry,
            as_of=spec.as_of,
            extra_facts=market_normalized_facts() if spec.uses_market_document else (),
            extra_documents=market_documents() if spec.uses_market_document else (),
        )
    except Exception:
        registry.close()
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return {"registry": registry, "document_id": draft.document.document_id,
            "report": report, "workdir": workdir}


# --------------------------------------------------------------------------- #
# ② 主张-证据图谱（CARD-09）—— DEMO_FLOW 步骤 9+10 的「点击结论→逐层展开」
# --------------------------------------------------------------------------- #
def graph_view(key: str = DEFAULT_FILING_KEY) -> Dict[str, Any]:
    """结论 → 计算 → 输入证据 → 原始页码，四个层次一次给全。"""
    from .graph.models import ROLE_RANK
    from .graph.propagation import claim_status, structural_status

    try:
        state = _filing(key)
        graph = state.graph
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    claim_rows: List[Dict[str, Any]] = []

    for claim_id, claim in sorted(graph.claims.items()):
        status, reasons, _blocked = claim_status(graph, claim)
        claim_rows.append(
            {
                "claim_id": claim_id,
                "claim_type": claim.claim_type,
                "text": claim.text,
                "metric": claim.metric,
                "period_key": claim.period_key,
                "scope": getattr(claim, "scope", None),
                "validation_status": claim.validation_status,
                "verification_status": claim.verification_status,
                "propagated_status": status,
                "structural_status": structural_status(graph, claim),
                "reasons": reasons,
                "evidence_ids": list(claim.evidence_ids),
                "calculation_ids": list(claim.calculation_ids),
                "derived_from": list(claim.derived_from),
            }
        )
        nodes.append(
            {
                "id": claim_id,
                "type": "claim",
                "subtype": claim.claim_type,
                "label": claim.metric or claim.text[:24],
                "status": status,
                "period": claim.period_key or "",
            }
        )
        for calc_id in claim.calculation_ids:
            edges.append({"source": claim_id, "target": calc_id, "kind": "uses_calculation", "role": "REQUIRED"})
        for evidence_id in claim.evidence_ids:
            edges.append({"source": claim_id, "target": evidence_id, "kind": "supported_by", "role": "REQUIRED"})
        for parent in claim.derived_from:
            edges.append({"source": parent, "target": claim_id, "kind": "derived_from", "role": "SUPPORTING"})

    for calc_id, node in sorted(graph.calculations.items()):
        nodes.append(
            {
                "id": calc_id,
                "type": "calculation",
                "subtype": node.operation,
                "label": node.metric or node.formula,
                "status": node.validation_status,
                "formula": node.formula,
            }
        )
        for operand in node.inputs:
            if operand.kind == "evidence":
                edges.append({"source": operand.ref, "target": calc_id, "kind": "input", "role": "REQUIRED"})
            elif operand.kind == "calculation":
                edges.append({"source": operand.ref, "target": calc_id, "kind": "input", "role": "REQUIRED"})

    for evidence_id, node in sorted(graph.evidence_index.items()):
        nodes.append(
            {
                "id": evidence_id,
                "type": "evidence",
                "subtype": node.kind,
                "label": node.metric or evidence_id,
                "status": "active" if node.active else "deactivated",
                "page": node.page,
                "document_id": node.document_id,
                "jumpable": bool(node.page),
            }
        )

    located = sum(1 for node in graph.evidence_index.values() if node.page)
    return {
        "status": STATUS_OK,
        "case": _case_brief(state),
        "run_id": graph.run_id,
        "claim_rows": claim_rows,
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "claims": len(graph.claims),
            "calculations": len(graph.calculations),
            "evidence": len(graph.evidence_index),
            "evidence_located": located,
            "theses": len(graph.theses),
        },
        "role_order": list(ROLE_RANK),
        "warnings": list(graph.warnings),
        "deactivations": list(graph.deactivations),
        "stale_claims": list(graph.stale_claims),
    }


# --------------------------------------------------------------------------- #
# ③ ACME 约束（CARD-22）—— DEMO_FLOW 步骤 4b，创新展示一
# --------------------------------------------------------------------------- #
def _envelope(payload: Dict[str, Any], engine_key: str) -> Dict[str, Any]:
    """把引擎载荷包进服务信封，**保留**引擎自己的 ``status``。

    信封需要一个"这次调用成不成功"的状态，而 CARD-22/24/25 的载荷里也有一个叫
    ``status`` 的字段，含义完全不同（约束族/脆弱性/重验证的结论状态）。直接覆盖
    会把"引擎说它没法判定"悄悄变成"一切正常" —— 所以先搬到 ``engine_key`` 再改。
    """
    engine_status = payload.pop("status", None)
    if engine_status is not None:
        payload[engine_key] = engine_status
    payload["status"] = STATUS_OK
    return payload


def acme_view(key: str = DEFAULT_ACME_KEY) -> Dict[str, Any]:
    from .acme import validate_multimodal_evidence

    try:
        state = _filing(key)
        raw_facts = state.registry.get_facts(state.document_id)
        report = validate_multimodal_evidence(
            state.normalization.facts,
            state.normalization.statements,
            raw_facts=raw_facts,
            text_facts=raw_facts,
            scope_signals=state.analysis.scope_signals,
            document_id=state.document_id,
            detected_at="",
        )
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    payload = report.to_dict()
    payload["case"] = _case_brief(state)
    return _envelope(payload, "acme_status")


# --------------------------------------------------------------------------- #
# ④ 结论护照 + VERIFY CLAIM（CARD-23）—— DEMO_FLOW 步骤 7b，★核心动作
# --------------------------------------------------------------------------- #
def _proof_context(state: _FilingState, claim_id: str = ""):
    from .proof import ProofContext

    def _loader(wanted: str) -> Optional[bytes]:
        """读回摄入时的字节，让 §7.1 的哈希被**重算**而不是被信任。"""
        record = state.registry.get(wanted)
        stored = getattr(record, "stored_path", None)
        if not stored:
            return None
        path = Path(stored)
        return path.read_bytes() if path.exists() else None

    declared_scope = ""
    for claim in state.graph.claims.values():
        if claim.claim_id == claim_id:
            declared_scope = str(getattr(claim, "scope", "") or "")
            break

    return ProofContext(
        graph=state.graph,
        calculation_registry=getattr(state.analysis, "registry", None),
        run_id=state.graph.run_id,
        document_hashes=(
            {state.document_id: state.document.sha256} if getattr(state.document, "sha256", "") else {}
        ),
        document_loader=_loader,
        declared_scopes={claim_id: declared_scope} if (claim_id and declared_scope) else {},
        detected_at="",
    )


def passport_view(key: str = DEFAULT_FILING_KEY, claim_id: str = "") -> Dict[str, Any]:
    """一张护照 + 一次 VERIFY CLAIM。``claim_id`` 留空时取图上第一条结论。"""
    from .proof import build_financial_proof_object, verify_claim

    try:
        state = _filing(key)
        graph = state.graph
        if not graph.claims:
            return _unavailable("该文档没有产出任何 claim，无法构建护照。")
        target = claim_id or sorted(graph.claims)[0]
        if target not in graph.claims:
            return _unavailable(f"未知 claim: {target}")
        context = _proof_context(state, target)
        passport = build_financial_proof_object(target, context)
        verification = verify_claim(target, context)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    claim = graph.claims[target]
    return {
        "status": STATUS_OK,
        "case": _case_brief(state),
        "claim_id": target,
        "claim": claim.to_dict(),
        "selectable": sorted(graph.claims),
        "passport": passport.to_dict(),
        "verification": verification.to_dict(),
        "overall_status": verification.overall_status,
        "status_reasons": list(verification.status_reasons),
        "checks": [item.to_dict() for item in verification.checks],
        "verified_at": verification.verified_at,
        "engine": "src.cn.proof（确定性重算与重查，模型不参与）",
    }


def verify_view(key: str = DEFAULT_FILING_KEY, claim_id: str = "") -> Dict[str, Any]:
    """只跑一次 **VERIFY CLAIM**（不构建护照）。

    与 :func:`passport_view` 分开，是为了让页面上的按钮**真的重跑一次证明**，
    而不是回放一遍页面加载时算过的结果 —— 剧本步骤 7b 要的正是"点击 → 系统
    重新执行"。验证路径纯确定性：重查文档哈希、重算公式、检查依赖状态，模型不参与。
    """
    from .proof import verify_claim

    try:
        state = _filing(key)
        graph = state.graph
        if not graph.claims:
            return _unavailable("该文档没有产出任何 claim，无从验证。")
        target = claim_id or sorted(graph.claims)[0]
        if target not in graph.claims:
            return _unavailable(f"未知 claim: {target}")
        verification = verify_claim(target, _proof_context(state, target))
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    return {
        "status": STATUS_OK,
        "claim_id": target,
        "overall_status": verification.overall_status,
        "status_reasons": list(verification.status_reasons),
        "checks": [item.to_dict() for item in verification.checks],
        "verified_at": verification.verified_at,
        "engine": "src.cn.proof（确定性重算与重查，模型不参与）",
    }


# --------------------------------------------------------------------------- #
# ⑤ 逻辑脆弱性（CARD-24）—— DEMO_FLOW 步骤 10b，创新展示三
# --------------------------------------------------------------------------- #
def fragility_view(key: str = DEFAULT_FILING_KEY) -> Dict[str, Any]:
    """关键依赖 / 单点失效 / 最小割集 / 监控计划。

    **Thesis 是声明式的，而且必须说清楚。** 现有语料解析出的 claim 全是
    ``fact``，没有一条 ``inference``（CARD-24 §23 已记录该语料缺口），所以
    推论式 Thesis 路径暂无材料。本页改为**显式声明**一条 Thesis：它依赖图上
    全部指标事实（REQUIRED），于是割集会沿 REQUIRED 边下钻到证据层。
    ``thesis_source`` 如实回报为 ``declared`` —— "分析对象是哪来的"不是隐含信息。
    """
    from .fragility import build_fragility_report, resolve_theses
    from .graph.models import ROLE_REQUIRED

    try:
        state = _filing(key)
        graph = state.graph
        claim_ids = [cid for cid, claim in sorted(graph.claims.items()) if claim.claim_type == "fact"]
        if not claim_ids:
            return _unavailable("该文档没有 fact 级 claim，没有可分析的逻辑。")
        declared = [
            {
                "thesis_id": f"thesis_demo_{state.case.key}",
                "statement": f"{state.document.company or state.case.filename} 当期财务健康度判断",
                "claim_ids": claim_ids,
                "dependency_roles": {cid: ROLE_REQUIRED for cid in claim_ids},
            }
        ]
        theses, source = resolve_theses(graph, declared)
        report = build_fragility_report(
            graph, [item.thesis_id for item in theses], None, thesis_source=source
        )
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    payload = report.to_dict()
    payload["case"] = _case_brief(state)
    payload["thesis_source"] = source
    payload["thesis_source_note"] = (
        "declared：本演示显式声明 Thesis（语料无 inference claim，推论式路径暂无材料）"
        if source == "declared"
        else source
    )
    return _envelope(payload, "fragility_status")


# --------------------------------------------------------------------------- #
# ⑥ 时间重验证（CARD-25）—— DEMO_FLOW 失败注入 F9
# --------------------------------------------------------------------------- #
def _supersession_pair(graph: Any) -> List[str]:
    """挑一对**同指标、不同期间**的证据节点作为（旧值, 新值）。

    任意两个证据节点凑不出"替代"这件事 —— 替代关系的语义是"同一条口径上，
    新的披露顶掉了旧的"。所以这里只在同 metric 的节点里按期间排序取相邻两期；
    找不到就返回空列表，让调用方如实报缺，而不是硬凑一对假血缘。
    """
    by_metric: Dict[str, List[Any]] = {}
    for node in graph.evidence_index.values():
        if node.metric:
            by_metric.setdefault(str(node.metric), []).append(node)
    for metric in sorted(by_metric):
        nodes = sorted(by_metric[metric], key=lambda item: str(item.period_key or ""))
        periods = [str(item.period_key or "") for item in nodes]
        if len(nodes) >= 2 and len(set(periods)) >= 2:
            return [nodes[0].evidence_id, nodes[-1].evidence_id]
    return []


def temporal_view(
    key: str = DEFAULT_FILING_KEY, old_id: str = "", new_id: str = ""
) -> Dict[str, Any]:
    """导入一次"世界变了"的事件，看它波及到哪些 Fact/Calculation/Claim。

    默认事件是**同一指标上跨期的一次替代**（例如 2024FY 披露被 2025FY 顶掉），
    因为这才是替代语义 —— 见 :func:`_supersession_pair`。
    """
    from .conflict import CHANGE_VERSION_SUPERSESSION
    from .temporal import ingest_temporal_event
    from .temporal.models import EVENT_CORRECTION, TemporalEvent

    try:
        state = _filing(key)
        graph = state.graph
        if not graph.claims:
            return _unavailable("该文档没有产出 claim，无从重验证。")
        if old_id and new_id:
            seeds = [old_id, new_id]
        else:
            seeds = _supersession_pair(graph)
        if len(seeds) < 2:
            return _unavailable(
                "图上找不到同指标跨期的两条证据，构成不了一次「替代」事件"
                "（替代的语义就是同口径上新的顶掉旧的）。"
            )
        # ``VERSION_SUPERSESSION`` 必须同时说明两件事：被替代的**旧**节点（``supersedes``）
        # 和顶替它的**新**节点（``changed_source_ids`` 里的另一个）。只给一边，引擎只能
        # 拒答 —— 它既无法知道哪条退出当前口径，也无法建立替代方向。
        event = TemporalEvent(
            event_id=f"demo-event-{state.case.key}",
            event_kind=CHANGE_VERSION_SUPERSESSION,
            event_type=EVENT_CORRECTION,
            description="演示：同一指标上新的披露进入，旧值退出当前口径。",
            changed_source_ids=tuple(seeds),
            supersedes=seeds[0],
        )
        report = ingest_temporal_event(graph, event, measure_latency=False)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    payload = report.to_dict()
    payload["case"] = _case_brief(state)
    payload["old_id"], payload["new_id"] = seeds[0], seeds[1]
    return _envelope(payload, "revalidation_status")


# --------------------------------------------------------------------------- #
# ⑦ 审计回放 + 证据包（CARD-12）
# --------------------------------------------------------------------------- #
def _recorder(store_dir: Optional[Path] = None):
    """录制器工厂。

    ``store_dir`` 默认值在**调用时**取模块全局，不写进函数签名 —— 默认参数在
    import 时求值一次，之后 monkeypatch ``RUN_STORE_DIR`` 就再也不会生效，
    测试无法把录制重定向到临时目录（会被迫往 ``output/`` 里写）。
    """
    from .runs.recorder import RunRecorder

    return RunRecorder(store_dir or RUN_STORE_DIR)


def runs_catalog() -> Dict[str, Any]:
    from .runs.recorder import list_runs, load_manifest

    try:
        run_ids = list(list_runs(RUN_STORE_DIR))
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")
    items: List[Dict[str, Any]] = []
    for run_id in run_ids:
        try:
            manifest = load_manifest(RUN_STORE_DIR, run_id)
        except Exception as exc:  # noqa: BLE001
            items.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        items.append(
            {
                "run_id": run_id,
                "started_at": manifest.started_at,
                "finished_at": manifest.finished_at,
                "execution_mode": manifest.execution_mode,
                "model": manifest.model or "（未使用模型）",
                "parser": f"{manifest.parser_name}@{manifest.parser_version}",
                "git_commit": manifest.git_commit,
                "snapshot_id": manifest.snapshot_id,
            }
        )
    return {"status": STATUS_OK, "store_dir": str(RUN_STORE_DIR), "items": items, "count": len(items)}


def record_demo_run(key: str = DEFAULT_FILING_KEY) -> Dict[str, Any]:
    """把一份文档的真实解析链录成一个可回放的 run。

    这不是"造一条演示数据"：跑的是同一个确定性管线，事件流是**真的发生过的**
    步骤（文件装载 → 解析 → 归一化 → 计算 → 结论 → 校验）。演示需要一条 run
    才回放得起来，而仓库里目前没有任何 run（``runs/`` 从未生成）。
    """
    from .runs.models import RunConfig, RunInputDoc

    try:
        state = _filing(key)
        path = DOCUMENTS_DIR / state.case.filename
        recorder = _recorder()
        run_id = recorder.start(
            RunConfig(
                input_documents=[RunInputDoc(path=str(path))],
                parser_name=getattr(state.document, "parser_name", "pdfplumber"),
                parser_version=getattr(state.document, "parser_version", "unknown"),
                execution_mode="offline",
                snapshot_id=None,
                random_seed=0,
            )
        )
        recorder.emit("file_load", "document_loaded", {"path": state.case.filename},
                      {"document_id": state.document_id, "page_count": getattr(state.document, "page_count", None)},
                      "ok")
        facts = state.registry.get_facts(state.document_id)
        recorder.emit("parse", "facts_extracted", {"document_id": state.document_id},
                      {"fact_count": len(facts)}, "ok")
        recorder.emit("normalize", "normalized", {"fact_count": len(facts)},
                      {"metric_count": len(state.analysis.metrics)}, "ok")
        for node in sorted(state.graph.calculations.values(), key=lambda item: item.calculation_id):
            recorder.emit(
                "calculation", "recomputed", {"calculation_id": node.calculation_id},
                {"value": node.output.get("value") if isinstance(node.output, dict) else None,
                 "status": node.validation_status},
                "ok" if node.validation_status == "ok" else "warning",
                calculation_id=node.calculation_id,
            )
        for claim_id in sorted(state.graph.claims):
            recorder.emit("claim", "claim_registered", {"claim_id": claim_id},
                          {"validation_status": state.graph.claims[claim_id].validation_status}, "ok")
        warnings = list(state.graph.warnings) + list(getattr(state.analysis, "warnings", []) or [])
        recorder.emit("validation", "validated", {"claim_count": len(state.graph.claims)},
                      {"warnings": len(warnings)}, "warning" if warnings else "ok")
        recorder.finish("ok")
        return {"status": STATUS_OK, "run_id": run_id, "case": _case_brief(state)}
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")


def replay_view(run_id: str = "") -> Dict[str, Any]:
    """十一阶段时间线。未记录到的阶段如实标 ``not_recorded``，不省略。"""
    from .evidence_pack import replay_timeline
    from .runs.recorder import load_events, list_runs

    try:
        available = list(list_runs(RUN_STORE_DIR))
        if not available:
            return _unavailable("尚无 run 记录。先在「审计回放」页点一次『录制一次真实 run』。")
        target = run_id or sorted(available)[-1]
        if target not in available:
            return _unavailable(f"未知 run_id: {target}")
        events = load_events(RUN_STORE_DIR, target)
        timeline = replay_timeline(events, page_size=0)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    timeline["status"] = STATUS_OK
    timeline["run_id"] = target
    timeline["available_runs"] = sorted(available)
    return timeline


def export_pack_view(run_id: str = "") -> Dict[str, Any]:
    """导出证据包并报告**缺哪些件** —— 残包不伪装成完整包。"""
    from .evidence_pack import export_evidence_pack, validate_pack, zip_path_for
    from .runs.recorder import list_runs, load_events, load_manifest

    try:
        available = list(list_runs(RUN_STORE_DIR))
        if not available:
            return _unavailable("尚无 run 记录，没有可打包的内容。")
        target = run_id or sorted(available)[-1]
        PACK_DIR.mkdir(parents=True, exist_ok=True)
        events = load_events(RUN_STORE_DIR, target)
        manifest = load_manifest(RUN_STORE_DIR, target)
        manifest_path = RUN_STORE_DIR / target / "manifest.json"
        events_path = RUN_STORE_DIR / target / "events.jsonl"
        pack_dir = export_evidence_pack(
            target,
            store_dir=RUN_STORE_DIR,
            artefacts={
                "manifest.json": manifest_path.read_text(encoding="utf-8"),
                "events.jsonl": events_path.read_text(encoding="utf-8"),
                "report.md": (
                    f"# FinTrace-CN 演示 run {target}\n\n"
                    f"- 输入文档数：{len(manifest.input_document_sha256)}\n"
                    f"- 解析器：{manifest.parser_name}@{manifest.parser_version}\n"
                    f"- 执行模式：{manifest.execution_mode}\n"
                    f"- git commit：{manifest.git_commit}\n"
                    f"- 事件数：{len(events)}\n"
                ),
                "claims.json": json.dumps(
                    {"run_id": target, "note": "本演示 run 未产生 claim 级产物"}, ensure_ascii=False, indent=2
                ),
            },
            out_dir=PACK_DIR / target,
            make_zip=True,
        )
        validation = validate_pack(pack_dir)
        zip_path = zip_path_for(pack_dir)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    payload = validation.to_dict()
    payload["run_id"] = target
    payload["pack_dir"] = str(pack_dir)
    payload["zip_path"] = str(zip_path) if zip_path and Path(zip_path).exists() else ""
    payload["zip_bytes"] = Path(zip_path).read_bytes() if zip_path and Path(zip_path).exists() else b""
    return _envelope(payload, "pack_status")


# --------------------------------------------------------------------------- #
# ⑧ 对抗注入（CARD-12 七场景 / CARD-25 / CARD-26 FinFuzz）
# --------------------------------------------------------------------------- #
def injections_catalog() -> Dict[str, Any]:
    from .failure_injection import SCENARIOS

    return {
        "status": STATUS_OK,
        "items": [
            {
                "scenario_id": spec.scenario_id,
                "slug": spec.slug,
                "title": spec.title,
                "injection": spec.injection,
                "expected_behavior": spec.expected_behavior,
                "deferred_to": spec.deferred_to,
            }
            for spec in SCENARIOS
        ],
    }


def run_injection(scenario_id: int) -> Dict[str, Any]:
    from .failure_injection import run_scenario

    try:
        result = run_scenario(int(scenario_id))
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")
    payload = result.to_dict()
    payload["status"] = STATUS_OK
    return payload


def adversarial_view(operator: Optional[str] = None) -> Dict[str, Any]:
    """FinFuzz 报告：按错误类型分层报检出率，并区分抽取器/核查器归因。"""
    from .finfuzz import load_corpus, run_corpus

    try:
        loaded = load_corpus()
        cases = list(loaded.cases)
        if not cases:
            reasons = "; ".join(f"{item['doc_id']}:{item['reason']}" for item in loaded.skipped)
            return _unavailable(f"FinFuzz 语料不可用（{reasons or '无合格案例'}）。")
        operators = [operator] if operator else None
        report, skipped = run_corpus(cases, operators, seed=42)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"{type(exc).__name__}: {exc}")

    payload = report.to_dict()
    payload["skipped_count"] = len(skipped)
    payload["cases"] = [case.doc_id for case in cases]
    # 语料侧被剔掉的 claim 也要露出来：否则"召回率很漂亮"可能只是语料变小了。
    payload["excluded_claims"] = sum(len(case.excluded) for case in cases)
    return _envelope(payload, "finfuzz_status")


# --------------------------------------------------------------------------- #
# 跳转（复用 CARD-01 的定位链路，页面不自己算坐标）
# --------------------------------------------------------------------------- #
def jump(
    target: str, evidence_id: str, *, index: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """反查一条证据的页码与坐标。``target`` 是 ``document_id`` 或演示案例 key。

    两条定位来源，优先级明确，**不混用**：

    1. ``index``（各引擎自带的 evidence index）—— 它知道证据属于哪份文档、在哪一页，
       而且覆盖**规范化**事实（``nf_…`` 这类 id 不在 CARD-01 的原始 fact 表里，
       只查注册表会查不到）；
    2. ``DocumentRegistry.locate_fact`` —— 它额外给出 bbox 与逐字 ``raw_text``，
       但只认**原始** fact id。

    所以先问 index 要"在哪一页"，再用注册表补"框在哪" —— 补不到就只给页码，
    绝不编一个 bbox。定位不到就报错原因，一个跳错地方的链接比没有链接更糟。
    """
    info = (index or {}).get(evidence_id) or {}
    document_id = str(info.get("document_id") or "") or target
    registry = _DOC_REGISTRIES.get(document_id)
    if registry is None:
        try:
            state = _filing(target)
        except Exception:  # noqa: BLE001
            state = None
        if state is not None:
            registry, document_id = state.registry, state.document_id

    located = None
    if registry is not None:
        try:
            located = registry.locate_fact(document_id, evidence_id)
        except Exception:  # noqa: BLE001 - 查不到就是查不到，不是错误
            located = None

    page = (located or {}).get("page") or info.get("page")
    if page is None:
        return _unavailable(
            f"证据 {evidence_id} 没有可定位的页码"
            + ("（未提供 evidence index）" if not info else "")
        )

    path = _DOC_PATHS.get(document_id)
    bbox = (located or {}).get("bbox") or info.get("bbox")
    payload: Dict[str, Any] = {
        "status": STATUS_OK,
        "evidence_id": evidence_id,
        "document_id": document_id,
        "filename": path.name if path else None,
        "page": int(page),
        "bbox": bbox,
        "raw_text": (located or {}).get("raw_text") or info.get("raw_text"),
        "locator": (located or {}).get("locator") or info.get("locator") or info.get("table") or "",
        # 只给页码、给不出 bbox 时明说，UI 据此不画高亮框 —— 框错位置比不画更糟。
        "highlight": bool(bbox),
    }
    return payload


def render_page(
    target: str, page: int, highlights: Sequence[Sequence[float]] = ()
) -> Tuple[Optional[bytes], str]:
    """渲染某页并高亮 bbox。``target`` 可以是演示案例 key 或 ``document_id``。

    返回 ``(PNG bytes, 失败原因)``，不抛。复用 CARD-01 的渲染器
    （``document_service.render_pdf_page``），绝不在 Demo 层另写一份 ——
    高亮框偏一格就是把错归到别的数字上。
    """
    from .document_service import render_pdf_page

    path = _DOC_PATHS.get(target)
    if path is None:
        try:
            path = DOCUMENTS_DIR / _case_of(target).filename
        except DemoServiceError:
            return None, f"未知的渲染目标：{target}"
    if not path.exists():
        return None, f"语料文件不存在：{path.name}"
    try:
        return render_pdf_page(path, page, highlights=highlights), ""
    except Exception as exc:  # noqa: BLE001 - 渲染失败只影响预览
        return None, f"{type(exc).__name__}: {exc}"


def catalog() -> Dict[str, Any]:
    """侧栏与首页用的总目录。"""
    return {
        "status": STATUS_OK,
        "filings": filing_cases(),
        "drafts": draft_cases(),
        "default_filing": DEFAULT_FILING_KEY,
        "default_acme": DEFAULT_ACME_KEY,
        "run_store": str(RUN_STORE_DIR),
        "pack_dir": str(PACK_DIR),
    }


__all__ = [
    "DEFAULT_ACME_KEY",
    "DEFAULT_FILING_KEY",
    "DOCUMENTS_DIR",
    "DRAFTS_DIR",
    "DemoCase",
    "DemoServiceError",
    "FILING_CASES",
    "PACK_DIR",
    "RUN_STORE_DIR",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "acme_view",
    "adversarial_view",
    "catalog",
    "corrections",
    "draft_cases",
    "export_pack_view",
    "filing_cases",
    "fragility_view",
    "graph_view",
    "injections_catalog",
    "jump",
    "passport_view",
    "record_demo_run",
    "render_page",
    "replay_view",
    "reset",
    "run_injection",
    "runs_catalog",
    "temporal_view",
    "verify_view",
]
