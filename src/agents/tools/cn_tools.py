"""Offline-first A-share tools backed by a versioned FinTrace-CN snapshot."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.cn.evidence import EvidenceLedger
from src.cn.providers.snapshot import SnapshotProvider
from src.cn.peer_workflow import PeerCandidate, result_to_dict, select_peers, value_with_peers
from src.cn.report import CnResearchReportBuilder
from src.cn.research import ResearchState
from src.cn.symbols import CanonicalSymbol, normalize_cn_symbol
from src.cn.valuation import (
    PEER_SOURCE,
    SOURCED_PARAMETERS,
    AssumptionError,
    AssumptionRegistry,
    PeerValuationInput,
    build_valuation,
    summarize_peer_multiple,
    valuation_inputs_from_normalization,
)
from src.cn.analysis import (
    COMPARABILITY_LABELS,
    SIGNAL_TYPE_LABELS,
    analyze_document,
    build_scope_report,
    publish_analysis_evidence,
)
from src.cn.acme import validate_multimodal_evidence
from src.cn.acme.adapters import facts_from_statements
from src.validation import validate_acme_report
from src.cn.checker.pipeline import DraftNotFound, check_draft
from src.cn.document_service import document_analysis
from src.cn.documents.registry import DocumentRegistry
from src.cn.graph import (
    build_graph_from_analysis,
    deactivate_evidence,
    revalidate,
)
from src.cn.conflict import (
    correction_candidates,
    correction_pairs,
    default_source_class,
    resolve_conflicts,
    summarize as summarize_conflicts,
    unresolved_report,
)
from src.cn.memo import (
    SECTION_IDS,
    InvestmentMemo,
    build_memo,
    render_memo_json,
    render_memo_markdown,
)
from src.cn.normalization import (
    BASE_UNIT,
    normalize_financials,
)
from src.cn.normalization.evidence_bridge import (
    normalization_jumps,
    register_normalized_evidence,
)
from src.cn.retrieval import RetrievalEngine
from src.cn.analysis.calculations import CalculationRegistry
from src.cn.proof import (
    ProofContext,
    build_financial_proof_object,
    verify_claim,
)
from src.cn.fragility import (
    THESIS_SOURCE_NONE,
    FragilityError,
    FragilityThreshold,
    build_fragility_report,
    resolve_theses,
    theses_from_declaration,
)
from src.cn.temporal import (
    CHANGE_KINDS,
    EVENT_TYPES,
    STATUS_OK,
    TemporalError,
    TemporalEvent,
    build_lineage,
    ingest_temporal_event,
)
from src.validation import (
    validate_conflicts,
    validate_fragility,
    validate_proof,
    validate_temporal,
    checker_module_source,
    validate_checker_report,
    validate_graph,
    validate_memo,
    validate_valuation,
)

from .base import Tool, tool_error, tool_ok


DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[3] / "data" / "snapshots" / "cn" / "600519.SH_20260810_tushare_v1.json"
_ALIASES = {"贵州茅台": "600519.SH", "茅台": "600519.SH", "KWEICHOW MOUTAI": "600519.SH"}


def _snapshot_path(path: Optional[Path | str] = None) -> Path:
    configured = os.getenv("FINTRACE_CN_SNAPSHOT", "").strip()
    return Path(path or configured or DEFAULT_SNAPSHOT)


class ResolveCnSymbolTool(Tool):
    name = "resolve_cn_symbol"
    description = "Resolve a Chinese A-share company name or code to its canonical symbol, e.g. 贵州茅台 → 600519.SH."
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Chinese company name or A-share code."}},
        "required": ["query"],
    }

    def __init__(self, snapshot: Optional[SnapshotProvider] = None):
        self.snapshot = snapshot

    async def execute(self, query: str) -> str:
        text = (query or "").strip()
        resolved = _ALIASES.get(text) or _ALIASES.get(text.upper())
        try:
            symbol = normalize_cn_symbol(resolved or text)
        except Exception as exc:
            return tool_error(str(exc), query=text)
        profile = None
        if self.snapshot:
            try:
                profile = self.snapshot.get_profile(symbol)
            except Exception:
                pass
        return tool_ok(
            query=text,
            symbol=str(symbol),
            asset_class="A_SHARE",
            company_name=profile.name if profile else None,
            snapshot_available=bool(profile),
        )


class _SnapshotTool(Tool):
    def __init__(self, snapshot: SnapshotProvider):
        self.snapshot = snapshot

    def _symbol(self, value: str) -> CanonicalSymbol:
        return normalize_cn_symbol(value)


class GetCnPricesTool(_SnapshotTool):
    name = "get_cn_prices"
    description = "Read saved A-share RAW price bars from the verified offline snapshot. This is historical snapshot data, not a live quote."
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Canonical A-share symbol, e.g. 600519.SH."},
            "start_date": {"type": "string", "description": "Optional YYYY-MM-DD start date."},
            "end_date": {"type": "string", "description": "Optional YYYY-MM-DD end date."},
        },
        "required": ["ticker"],
    }

    async def execute(self, ticker: str, start_date: str = "", end_date: str = "") -> str:
        try:
            symbol = self._symbol(ticker)
            bars = self.snapshot.get_daily_bars(symbol, start_date=start_date or None, end_date=end_date or None)
        except Exception as exc:
            return tool_error(str(exc), ticker=ticker)
        if not bars:
            return tool_error("No RAW bars in the configured snapshot for this range.", ticker=str(symbol))
        latest = max(bars, key=lambda item: item.trade_date)
        evidence_id = f"fact_{str(symbol).replace('.', '_')}_close_{latest.trade_date}_RAW"
        return tool_ok(
            ticker=str(symbol), snapshot_id=self.snapshot._payload["snapshot_id"], adjustment="RAW",
            latest={"trade_date": latest.trade_date, "close": latest.close, "volume": latest.volume, "amount": latest.amount},
            bars=[{"trade_date": item.trade_date, "open": item.open, "high": item.high, "low": item.low, "close": item.close} for item in bars],
            evidence_ids=[evidence_id],
            note="Offline snapshot data; do not describe it as a live quote.",
        )


class GetCnFinancialsTool(_SnapshotTool):
    name = "get_cn_financials"
    description = "Read A-share financial statements from the verified offline snapshot and return evidence IDs for each key value."
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Canonical A-share symbol, e.g. 600519.SH."},
            "research_as_of": {"type": "string", "description": "Optional ISO-8601 China-time research cutoff."},
        },
        "required": ["ticker"],
    }

    async def execute(self, ticker: str, research_as_of: str = "") -> str:
        try:
            symbol = self._symbol(ticker)
            cutoff = research_as_of or self.snapshot._payload["research_as_of"]
            statements = self.snapshot.get_financial_statements(symbol, research_as_of=cutoff)
        except Exception as exc:
            return tool_error(str(exc), ticker=ticker)
        ledger = EvidenceLedger(snapshot_id=self.snapshot._payload["snapshot_id"])
        facts = []
        for statement in statements:
            facts.extend(ledger.add_statement_facts(symbol=str(symbol), statement=statement, provider="snapshot"))
        return tool_ok(
            ticker=str(symbol), snapshot_id=self.snapshot._payload["snapshot_id"], research_as_of=cutoff,
            statements=[{
                "type": statement.statement_type, "fiscal_period": statement.fiscal_period,
                "period_basis": statement.period_basis, "published_at": statement.published_at,
                "available_at": statement.available_at, "values": statement.values,
            } for statement in statements],
            evidence_ids=[fact.evidence_id for fact in facts],
            validation=ledger.validate(research_as_of=cutoff).valid,
        )


class GenerateCnResearchReportTool(_SnapshotTool):
    name = "generate_cn_research_report"
    description = "Generate a Chinese Markdown A-share research report from the verified offline snapshot. Includes RAW price, TTM metrics, valuation, Evidence IDs, and Validator status; it never fetches live data or calls Tushare."
    parameters = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Canonical A-share symbol, e.g. 600519.SH."},
            "research_as_of": {"type": "string", "description": "Optional ISO-8601 China-time research cutoff."},
        },
        "required": ["ticker"],
    }

    async def execute(self, ticker: str, research_as_of: str = "") -> str:
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            symbol = self._symbol(ticker)
            cutoff = research_as_of or self.snapshot._payload["research_as_of"]
            state = ResearchState.create(query=f"生成 {symbol} A股研究报告", symbol=str(symbol), research_as_of=cutoff)
            report = CnResearchReportBuilder(self.snapshot).build(symbol, research_as_of=cutoff)
        except Exception as exc:
            return tool_error(str(exc), ticker=ticker)
        evidence_ids = [line.split("`")[1] for line in report.markdown.splitlines() if line.startswith("| `")]
        state.register_evidence(evidence_ids)
        validation = {"valid": report.validation.valid, "errors": report.validation.errors, "missing_evidence": []}
        state.finalize_validation(validation)
        state.report = report.markdown
        state.record_tool(tool_name=self.name, arguments={"ticker": str(symbol), "research_as_of": cutoff}, started_at=started_at,
                          result_status="ok", provider="snapshot", evidence_ids=evidence_ids)
        return tool_ok(
            ticker=str(symbol), snapshot_id=self.snapshot._payload["snapshot_id"],
            research_as_of=cutoff, validation=validation, research_state=state.to_dict(),
            report_markdown=report.markdown,
            note="Offline snapshot report; do not describe prices as live data.",
        )


class CreateCnResearchPlanTool(Tool):
    name = "create_cn_research_plan"
    description = "Create a structured, traceable offline A-share research plan with required evidence and bounded recovery state. Use after resolving an A-share symbol."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Original user research request."},
            "ticker": {"type": "string", "description": "Canonical A-share symbol, e.g. 600519.SH."},
            "research_as_of": {"type": "string", "description": "Research cutoff in ISO-8601 China time."},
        },
        "required": ["query", "ticker", "research_as_of"],
    }

    async def execute(self, query: str, ticker: str, research_as_of: str) -> str:
        try:
            symbol = normalize_cn_symbol(ticker)
            state = ResearchState.create(query=query, symbol=str(symbol), research_as_of=research_as_of)
        except Exception as exc:
            return tool_error(str(exc), ticker=ticker)
        return tool_ok(research_state=state.to_dict())


class ValueWithPeersTool(Tool):
    name = "value_with_peers"
    description = "Calculate deterministic A-share PE/PB/PS peer valuation ranges from supplied, evidence-backed offline inputs. Returns peer selection reasons, outlier exclusions, implied prices, and validation; no market-data provider is called."
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "object", "description": "Exactly one target OBJECT (never an array): symbol, raw_price, total_shares, net_profit, book_equity, revenue, period_basis, industry_code, evidence_ids object, and optional is_bank_or_insurer."},
            "peers": {"type": "array", "items": {"type": "object"}, "description": "Array of peer objects in the same shape as target; evidence_ids must be an object mapping field names to IDs."},
        },
        "required": ["target", "peers"],
    }

    @staticmethod
    def _candidate(raw: Dict[str, object]) -> PeerCandidate:
        if not isinstance(raw, dict):
            raise ValueError("Peer valuation target and every peer must be JSON objects, not arrays or scalars.")
        required = ("symbol", "raw_price", "total_shares", "period_basis", "industry_code")
        missing = [key for key in required if raw.get(key) is None]
        if missing:
            raise ValueError(f"Missing peer valuation fields: {', '.join(missing)}")
        valuation = PeerValuationInput(
            symbol=str(raw["symbol"]), raw_price=float(raw["raw_price"]), total_shares=float(raw["total_shares"]),
            net_profit=float(raw["net_profit"]) if raw.get("net_profit") is not None else None,
            book_equity=float(raw["book_equity"]) if raw.get("book_equity") is not None else None,
            revenue=float(raw["revenue"]) if raw.get("revenue") is not None else None,
            period_basis=str(raw["period_basis"]),
        )
        ids = raw.get("evidence_ids") or {}
        if not isinstance(ids, dict):
            raise ValueError("evidence_ids must be an object mapping fields to evidence IDs.")
        return PeerCandidate(valuation, str(raw["industry_code"]), {str(k): str(v) for k, v in ids.items()}, bool(raw.get("is_bank_or_insurer", False)))

    async def execute(self, target: Dict[str, object], peers: List[Dict[str, object]]) -> str:
        try:
            result = value_with_peers(self._candidate(target), [self._candidate(item) for item in peers])
        except (AttributeError, TypeError, ValueError) as exc:
            return tool_error(str(exc))
        return tool_ok(**result_to_dict(result))


class RunCnRelativeValuationTool(Tool):
    name = "run_cn_relative_valuation"
    description = (
        "在已摄入文档上执行确定性相对估值（CARD-08）：Bear/Base/Bull 三情景、"
        "EPS × 目标 PE 与 BPS × 目标 PB 敏感性矩阵、各方法离散度报告，"
        "并把每一项输入假设登记进 Assumption Registry（数据→公式→假设→输出四层可追溯）。"
        "每股驱动量取自 CARD-02 期间引擎的 TTM 输出（不用 FY 静态 EPS）；"
        "总股本缺失时整体阻断（fail-closed），未给同业或目标倍数时该方法标 not_applicable，绝不编造倍数。"
        "本工具只接受 document_id：按 symbol 取值会绕开可回溯的证据链。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册文档的 document_id（ingest_cn_document 返回）。"},
            "raw_price": {"type": "number", "description": "RAW 收盘价（元/股）。市值与倍数只允许用未复权原始价。"},
            "symbol": {"type": "string", "description": "仅为兼容 CARD-08 §6 的入参名而保留；单独传 symbol 会被显式拒绝（需要 document_id）。"},
            "peers": {"type": "array", "items": {"type": "object"}, "description": "可选同业可比集，形状同 value_with_peers 的 peers（须含 symbol/raw_price/total_shares/period_basis 与 evidence_ids）。period_basis 必须等于目标公司 TTM 期间键（见出参 peer_period_basis）才被纳入。"},
            "peer_industry_code": {"type": "string", "description": "提供 peers 时必填：目标公司的行业代码，同业须同行业同期间才被纳入。"},
            "is_bank_or_insurer": {"type": "boolean", "description": "目标公司是否银行/保险：为真时同业集只用 PE/PB。"},
            "target_multiples": {"type": "object", "description": "可选分析师目标倍数（基准值），如 {\"PE\": 20, \"PB\": 3}；仅登记的假设，不当作 sourced 事实。"},
            "valid_from": {"type": "string", "description": "假设生效时点（ISO-8601），缺省为空并如实留白。"},
        },
        "required": ["document_id", "raw_price"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        document_id: str = "",
        raw_price: Optional[float] = None,
        symbol: str = "",
        peers: Optional[List[Dict[str, object]]] = None,
        peer_industry_code: str = "",
        is_bank_or_insurer: bool = False,
        target_multiples: Optional[Dict[str, object]] = None,
        valid_from: str = "",
    ) -> str:
        if not document_id:
            reason = (
                "CARD-08 §6 的入参名是 symbol，但按 symbol 估值会绕开可回溯的证据链"
                "（CARD-02 规范化只接文档事实），故与 analyze_cn_financials 一致：本工具只接受 document_id。"
            )
            if symbol:
                reason = f"仅提供 symbol=『{symbol}』不足以估值。" + reason
            return tool_error(reason, error_code="valuation_missing_argument", symbol=symbol or None)
        document = self.registry.get(document_id)
        if document is None:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found", document_id=document_id,
            )
        if raw_price is None:
            return tool_error(
                "必须提供 RAW 收盘价：市值与倍数只允许建立在未复权原始价上。",
                error_code="valuation_missing_raw_price", document_id=document_id,
            )

        facts = self.registry.get_facts(document_id)
        if not facts:
            return tool_error(
                "该文档没有可规范化的事实（解析失败或全为扫描页），无法估值。",
                error_code="valuation_no_facts", document_id=document_id,
            )

        normalization = normalize_financials(facts, document)
        try:
            inputs = valuation_inputs_from_normalization(
                normalization,
                symbol=str(document.company or document_id),
                raw_price=float(raw_price),
                document_id=document_id,
            )
        except (TypeError, ValueError) as exc:
            # Fail-closed path: no shares → no per-share number at all.
            return tool_error(
                "缺少可用的期末总股本，每股估值被阻断（fail-closed）。"
                "CARD-08 §8 边缘 2：不允许用任何替代值填补股本。",
                error_code=str(exc) or "valuation_blocked", document_id=document_id,
                warnings=list(normalization.warnings),
            )

        # -- optional peer set: the *existing* engine decides which peers count --
        peer_ids: List[str] = []
        peer_conflicts: Dict[str, str] = {}
        peer_info: Dict[str, object] = {
            "provided": bool(peers),
            "period_basis_required": inputs.to_peer_input().period_basis,
            "included": [],
            "excluded": [],
        }
        if peers:
            if not str(peer_industry_code).strip():
                return tool_error(
                    "提供 peers 时必须同时给出 peer_industry_code：同业可比以同行业同期间为前提，"
                    "缺行业代码无法判定可比性（不猜测行业）。",
                    error_code="valuation_peer_industry_required", document_id=document_id,
                )
            try:
                target_candidate = PeerCandidate(
                    valuation=inputs.to_peer_input(),
                    industry_code=str(peer_industry_code),
                    evidence_ids={str(k): str(v) for k, v in inputs.evidence_ids.items()},
                    is_bank_or_insurer=bool(is_bank_or_insurer),
                )
                candidates = [ValueWithPeersTool._candidate(item) for item in peers]
            except (AttributeError, TypeError, ValueError) as exc:
                return tool_error(
                    f"同业输入无法解析：{exc}", error_code="valuation_peer_invalid",
                    document_id=document_id,
                )
            selected, decisions = select_peers(target_candidate, candidates)
            peer_info["included"] = [item.valuation.symbol for item in selected]
            peer_info["excluded"] = [
                {"symbol": item.symbol, "reason": item.reason}
                for item in decisions
                if not item.included
            ]
            for item in selected:
                for evidence_id in (item.evidence_ids or {}).values():
                    if evidence_id and evidence_id not in peer_ids:
                        peer_ids.append(str(evidence_id))
                for key, evidence_id in (item.evidence_ids or {}).items():
                    peer_conflicts.setdefault(str(key), str(evidence_id))

        # -- target multiples: explicit analyst input wins over peer medians ----
        resolved_targets: Dict[str, float] = {}
        target_source = None
        explicit: Dict[str, float] = {}
        for key, value in dict(target_multiples or {}).items():
            try:
                explicit[str(key).upper()] = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return tool_error(
                    f"target_multiples[{key}] 不是数值。", error_code="valuation_target_invalid",
                    document_id=document_id,
                )
        if explicit:
            resolved_targets.update(explicit)
            target_source = None  # register_scenario_assumptions 用默认的分析师来源
        if peers:
            for method in ("PE", "PB", "PS"):
                if method in resolved_targets:
                    continue
                summary = summarize_peer_multiple([item.valuation for item in selected], method)  # type: ignore[name-defined]
                if summary.median is not None:
                    resolved_targets[method] = float(summary.median)
                    target_source = PEER_SOURCE
            peer_info["medians"] = {
                method: summarize_peer_multiple(  # type: ignore[name-defined]
                    [item.valuation for item in selected], method
                ).median
                for method in ("PE", "PB", "PS")
            }
            if len(selected) < 3:
                peer_info["sample_warning"] = "peer_sample_below_3_no_iqr：同业纳入样本不足 3 家，未做 IQR 离群剔除。"

        registry = AssumptionRegistry(
            sourced={key: value for key, value in inputs.evidence_ids.items() if value},
            reserve=[name for name in SOURCED_PARAMETERS if name not in inputs.evidence_ids],
            evidence_ids=peer_ids,
        )
        calculations = CalculationRegistry()
        kwargs: Dict[str, object] = {
            "calculations": calculations,
            "valid_from": valid_from,
        }
        if resolved_targets:
            kwargs["target_multiples"] = resolved_targets
            if target_source is not None:
                kwargs["target_source"] = target_source
        try:
            result = build_valuation(inputs, registry, **kwargs)  # type: ignore[arg-type]
        except AssumptionError as exc:
            return tool_error(
                f"假设登记失败：{exc}", error_code="valuation_assumption_error", document_id=document_id,
            )

        validation = validate_valuation(result, registry)
        payload = result.to_dict()
        payload.update({
            "document_id": document_id,
            "validation": validation.to_dict(),
            "calculations_checked": calculations.validate(),
            "calculation_count": len(calculations.records()),
            "peer_selection": peer_info,
            "peer_evidence_ids": peer_ids,
            "engine": "src.cn.valuation（确定性计算，非模型输出）",
        })
        if inputs.warnings:
            payload["input_warnings"] = list(inputs.warnings)
        return json.dumps(payload, ensure_ascii=False, default=str)


def build_cn_snapshot_tools(snapshot_path: Optional[Path | str] = None) -> List[Tool]:
    """Always offer code resolution; only offer data tools when a local snapshot exists."""
    path = _snapshot_path(snapshot_path)
    # The document-layer tools share one registry so a document_id returned by
    # ingest resolves for normalization *and* for scope detection without a
    # second parse.
    registry = DocumentRegistry()
    document_tools: List[Tool] = [
        IngestCnDocumentTool(registry),
        NormalizeCnFinancialsTool(registry),
        DetectCnScopeSignalsTool(registry),
        CheckCnReportDraftTool(registry),
        SearchCnCorpusTool(registry),
        RunCnRelativeValuationTool(registry),
        TraceCnClaimTool(registry),
        DeactivateCnEvidenceTool(registry),
        GenerateCnInvestmentMemoTool(registry),
        ResolveCnSourceConflictsTool(registry),
        ValidateCnAcmeTool(registry),
        VerifyCnClaimTool(registry),
        AnalyzeCnThesisFragilityTool(registry),
        RevalidateCnClaimsTool(registry),
    ]
    if not path.exists():
        return [ResolveCnSymbolTool(), *document_tools]
    snapshot = SnapshotProvider(path)
    return [
        ResolveCnSymbolTool(snapshot), GetCnPricesTool(snapshot), GetCnFinancialsTool(snapshot),
        CreateCnResearchPlanTool(), GenerateCnResearchReportTool(snapshot), ValueWithPeersTool(),
        ValidateCnAcmeTool(registry, snapshot),
        *document_tools,
    ]


class IngestCnDocumentTool(Tool):
    name = "ingest_cn_document"
    description = (
        "摄入并解析真实 A 股金融材料（年报/公告/研报草稿 PDF，或 TXT/MD/Excel/CSV），"
        "返回带页码与坐标的结构化事实与解析摘要，支撑『报告数字 → 原始 PDF 页』反向跳转。"
        "解析失败返回 error 信封，绝不静默跳过。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区内文档路径（限 data/ output/ runs/ 之一），相对或绝对。"},
            "document_id": {"type": "string", "description": "已注册文档的 document_id；提供时直接返回其事实，不重复解析。"},
            "document_type": {"type": "string", "description": "可选声明类型：annual|interim|quarterly|announcement|report_draft|other"},
            "source": {"type": "string", "description": "来源：user_upload|data_dir|admin_import"},
            "register_evidence": {"type": "boolean", "description": "是否将可数值化事实登记到证据账本（用于反向追溯）。"},
        },
    }

    def __init__(self, registry: DocumentRegistry | None = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        path: str = "",
        document_id: str = "",
        document_type: str = "",
        source: str = "user_upload",
        register_evidence: bool = False,
    ) -> str:
        # -- cached lookup by document_id (no re-parse) --
        if not path and document_id:
            doc = self.registry.get(document_id)
            if doc is None:
                return tool_error(
                    f"未找到已注册文档: {document_id}",
                    error_code="document_not_found", document_id=document_id,
                )
            return tool_ok(**self._payload(
                doc, self.registry.get_facts(document_id), self.registry.get_events(document_id),
                warnings=[], register_evidence=register_evidence,
            ))

        if not path:
            return tool_error(
                "必须提供 path 或 document_id 之一。", error_code="document_missing_argument",
            )

        # -- ingest a new document --
        try:
            result = self.registry.ingest(path, declared_type=document_type or None, source=source)
        except Exception as exc:
            return tool_error(
                str(exc), error_code=_error_code(str(exc)), path=path,
            )
        doc = result.document
        if doc.status == "error":
            message = "; ".join(doc.notes) or "文档解析失败（加密/损坏/过大），未入库。"
            return tool_error(
                message, error_code=_error_code(message),
                document_id=doc.document_id, path=path,
            )
        return tool_ok(**self._payload(
            doc, result.facts, result.events, warnings=result.warnings,
            register_evidence=register_evidence,
        ))

    def _payload(self, doc, facts, events, *, warnings, register_evidence: bool) -> dict:
        """Uniform tool envelope: status / document / fact_count / parse_summary."""
        payload = {
            "status": doc.status if doc.status in ("ok", "partial", "degraded") else doc.status,
            "document": doc.to_dict(),
            "fact_count": len(facts),
            "parse_summary": {
                "parser": doc.parser_name,
                "page_count": doc.page_count,
                "events": [{"page": e.page, "status": e.status, "note": e.note} for e in events],
                "warnings": list(warnings),
                "suspected_injection": doc.suspected_injection,
                "injection_segments": doc.injection_segments,
                "notes": list(doc.notes),
            },
        }
        if register_evidence:
            try:
                from src.cn.documents.evidence_bridge import register_document_facts
                ledger = register_document_facts(doc, facts, snapshot_id=doc.document_id)
                payload["evidence_ids"] = [r.evidence_id for r in ledger.records() if r.kind == "fact"]
            except Exception as exc:  # best-effort; never block the ingest result
                payload["evidence_error"] = str(exc)
        return payload


# Failure classes surfaced to the agent as a stable ``error_code`` (CARD-01 §6:
# 解析失败 → status: error + error_code, 绝不静默跳过).
_ERROR_CODE_RULES = (
    ("文件过大", "document_too_large"),
    ("路径越权", "document_path_not_allowed"),
    ("无法解析路径", "document_path_invalid"),
    ("无法打开 PDF", "document_unreadable"),
    ("加密", "document_unreadable"),
    ("损坏", "document_unreadable"),
    ("未安装", "document_parser_unavailable"),
    ("不支持", "document_unsupported_input"),
)


def _error_code(message: str) -> str:
    for needle, code in _ERROR_CODE_RULES:
        if needle in message:
            return code
    return "document_parse_error"


class NormalizeCnFinancialsTool(Tool):
    name = "normalize_cn_financials"
    description = (
        "把已摄入文档（ingest_cn_document）的原始事实规范化：单位统一到元（元/千元/万元/百万元/亿元）、"
        "期间键规范化为 YYYYQ1/H1/9M/FY、标注 YTD/单季/TTM/时点语义与合并/母公司口径，"
        "并桥接现有 FinancialPeriodEngine 推导单季与 TTM。"
        "无法判定语义的值只记录 warnings，绝不猜测。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册文档的 document_id（ingest_cn_document 返回）。"},
            "register_evidence": {"type": "boolean", "description": "是否把规范化事实登记进证据账本（带回链 document_id/page/bbox）。"},
        },
        "required": ["document_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(self, document_id: str = "", register_evidence: bool = False) -> str:
        if not document_id:
            return tool_error("必须提供 document_id。", error_code="normalization_missing_argument")
        document = self.registry.get(document_id)
        if document is None:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found", document_id=document_id,
            )

        facts = self.registry.get_facts(document_id)
        if not facts:
            return tool_error(
                "该文档没有可规范化的事实（解析失败或全为扫描页），请先确认 ingest 结果。",
                error_code="normalization_no_facts", document_id=document_id,
            )

        result = normalize_financials(facts, document)

        if not result.facts:
            return tool_error(
                "所有事实都无法规范化（缺少单位声明、期间语义不可判定或不支持的期间键）。",
                error_code="normalization_empty", document_id=document_id,
                warnings=list(result.warnings),
            )

        status = "ok" if result.statements else "partial"
        statements = []
        for key in sorted(result.statement_groups, key=lambda item: item.describe()):
            for statement in result.statement_groups[key]:
                statements.append({
                    "statement_id": key.group_id,
                    "statement_type": statement.statement_type,
                    "fiscal_period": statement.fiscal_period,
                    "period_basis": statement.period_basis,
                    "currency": statement.currency,
                    "unit": statement.unit,
                    "available_at": statement.available_at,
                    "values": dict(statement.values),
                })

        payload: Dict[str, object] = {
            "status": status,
            "document_id": document_id,
            "report_period": result.report_period,
            "unit": BASE_UNIT,
            "fact_count": result.fact_count,
            "facts": [fact.to_dict() for fact in result.facts],
            "statements": statements,
            "single_quarters": [
                {
                    "statement_type": period.statement_type,
                    "fiscal_period": period.fiscal_period,
                    "values": dict(period.values),
                    "source_periods": list(period.source_periods),
                }
                for period in result.single_quarters
            ],
            "ttm": [
                {
                    "statement_type": period.statement_type,
                    "fiscal_period": period.fiscal_period,
                    "values": dict(period.values),
                    "source_periods": list(period.source_periods),
                }
                for period in result.ttm
            ],
            "warnings": list(result.warnings),
            "engine": "FinancialPeriodEngine (src/cn/periods.py)",
        }
        if status == "partial":
            payload["error"] = "规范化的值未形成可计算报表（期间语义不可判定或指标不在标准字典内）。"

        if register_evidence:
            try:
                payload.update(register_normalized_evidence(document, result))
            except Exception as exc:  # best-effort; never block the normalized result
                payload["evidence_error"] = str(exc)

        return json.dumps(payload, ensure_ascii=False, default=str)


class AnalyzeCnFinancialsTool(Tool):
    name = "analyze_cn_financials"
    description = (
        "在已摄入文档上执行确定性财务分析（CARD-03）：14 项指标（同比／环比／单季化／比率／水平值）"
        "+ 4 类诊断信号（盈利质量、应收、存货、非经常性损益）。"
        "所有指标都带可复核的 calculation_id（公式 + 输入证据 id + 重新计算值）；"
        "亏损基期的同比只给绝对变动，绝不给百分比；缺失输入登记在 missing 里，绝不编造。"
        "必须先 ingest_cn_document 摄入年报/季报，本工具只接受 document_id。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册文档的 document_id（ingest_cn_document 返回）。"},
            "register_evidence": {"type": "boolean", "description": "是否把可入账本的计算登记进证据账本（带回计算过程）。"},
            "research_as_of": {"type": "string", "description": "可选 ISO-8601 中国时区研究截止时点；提供时账本按该时点校验可用性。"},
        },
        "required": ["document_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self, document_id: str = "", register_evidence: bool = False, research_as_of: str = ""
    ) -> str:
        if not document_id:
            return tool_error(
                "必须提供 document_id。本工具只分析已摄入文档（CARD-01 → CARD-02 → CARD-03），"
                "不接受 symbol：快照口径尚未接入规范化层，按 symbol 分析会绕开可回溯的证据链。",
                error_code="analysis_missing_argument",
            )
        document = self.registry.get(document_id)
        if document is None:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found", document_id=document_id,
            )

        try:
            payload = document_analysis(document_id, registry=self.registry)
        except Exception as exc:
            return tool_error(
                f"财务分析失败（{type(exc).__name__}: {exc}）", error_code="analysis_failed",
                document_id=document_id,
            )

        metrics = payload["metrics"]
        if not metrics:
            status = "blocked"
            reason = (
                "该文档产不出任何指标：没有形成可计算的报表结构（缺单位声明、"
                "期间语义不可判定，或事实不在标准指标字典内）。"
            )
        elif payload["data_complete"]:
            status = "ok"
            reason = ""
        else:
            status = "partial"
            reason = "部分指标因缺失输入未产出；已产出的指标仍然可复核。"

        body: Dict[str, object] = {
            "status": status,
            "document_id": document_id,
            "report_period": payload["report_period"],
            "scope": payload["scope"],
            "version": payload["version"],
            "currency": payload["currency"],
            "unit": BASE_UNIT,
            "data_complete": payload["data_complete"],
            "metric_count": payload["metric_count"],
            "metrics": metrics,
            "signals": payload["signals"],
            "missing": payload["missing"],
            "blocking_missing": [row["metric"] for row in payload["missing"] if row["blocking"]],
            "not_applicable": [row["metric"] for row in payload["missing"] if not row["blocking"]],
            "warnings": payload["warnings"],
            "normalization_warnings": payload["normalization_warnings"],
            "validation_errors": payload["validation_errors"],
            "engine": "src.cn.analysis（确定性计算，非模型输出）",
        }
        if reason:
            body["error"] = reason

        if register_evidence:
            try:
                ledger = EvidenceLedger(snapshot_id=document_id)
                body["evidence"] = _publish_analysis_evidence(
                    document_id,
                    ledger=ledger,
                    registry=self.registry,
                    research_as_of=research_as_of,
                )
            except Exception as exc:  # best-effort; never block the analysis result
                body["evidence_error"] = str(exc)

        return json.dumps(body, ensure_ascii=False, default=str)


class DetectCnScopeSignalsTool(Tool):
    name = "detect_cn_scope_signals"
    description = (
        "在已摄入文档上检测会计口径与重述信号（CARD-04）：财务重述、会计政策变更、前期差错更正、"
        "合并范围变更、可比期间数据已调整，以及无法分类但影响报表的口径事项。"
        "每个信号绑定可回跳到原始页的证据 id，并给出可比性决策（阻断同比／改用调整后数据／提示）。"
        "检测为确定性文本规则 + 表格版本列，模型不参与判定；无信号 = 未命中规则，不等于不存在。"
        "本工具不改动任何数字，只回答『两端数据还能不能直接比』。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册文档的 document_id（ingest_cn_document 返回）。"},
            "detected_at": {
                "type": "string",
                "description": "可选 ISO-8601 检测时点；缺省取当前 UTC 时间（离线测试请显式传入以保持可复现）。",
            },
        },
        "required": ["document_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(self, document_id: str = "", detected_at: str = "") -> str:
        if not document_id:
            return tool_error(
                "必须提供 document_id。口径信号绑定在具体文档与其页面证据上，"
                "不接受 symbol：按 symbol 检测会脱离可回溯的证据链。",
                error_code="scope_missing_argument",
            )
        document = self.registry.get(document_id)
        if document is None:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found", document_id=document_id,
            )

        facts = self.registry.get_facts(document_id)
        if not facts:
            # No facts is a *parse* outcome, not a bad call: the document was read
            # and the parser located nothing (scan-only pages). Status ``blocked``
            # matches the analysis tool's vocabulary — 完全无产出 — and says
            # "detection was impossible", never "the filing is clean".
            return json.dumps(
                {
                    "status": "blocked",
                    "document_id": document_id,
                    "signal_count": 0,
                    "signals": [],
                    "comparability": [],
                    "blocked_documents": [],
                    "error": (
                        "该文档没有可用事实（解析失败或全为扫描页），无法检测口径信号；"
                        "本结果不表示不存在口径变化。"
                    ),
                    "error_code": "scope_no_facts",
                },
                ensure_ascii=False,
                default=str,
            )

        try:
            report = build_scope_report(facts, document, detected_at=detected_at or None)
        except Exception as exc:
            return tool_error(
                f"口径信号检测失败（{type(exc).__name__}: {exc}）", error_code="scope_failed",
                document_id=document_id,
            )

        signals = report["signals"]
        body: Dict[str, object] = {
            "status": "ok" if signals else "clean",
            "document_id": document_id,
            "company": document.company,
            "report_period": document.report_period,
            "document_type": document.document_type,
            "signal_count": report["signal_count"],
            "signals": signals,
            "comparability": report["comparability"],
            "blocked_documents": report["blocked_documents"],
            "coverage": report["coverage"],
            "labels": {
                "signal_types": SIGNAL_TYPE_LABELS,
                "comparability": COMPARABILITY_LABELS,
            },
            "engine": "src.cn.analysis.scope_signals（确定性规则，非模型输出）",
        }
        if not signals:
            body["note"] = (
                "未命中任何口径/重述规则。这**不等于**文档不存在口径变化，"
                "仅表示现有种子关键词表与表格版本列未匹配到；覆盖范围见 coverage。"
            )
        return json.dumps(body, ensure_ascii=False, default=str)


class CheckCnReportDraftTool(Tool):
    name = "check_cn_report_draft"
    description = (
        "核查一份已摄入的研报草稿（CARD-05）：把草稿句子中的数值/期间/口径/引用搬运为结构化 claim，"
        "再用确定性规则逐条对照已注册披露，输出 13 类错误的纠错报告（数值不符、单位错误、期间错配、"
        "口径错配、复算不一致、估值倍数错误、缺引用、引用错对象、引用不存在、页码错、引用过期数据、"
        "引用了被重述取代的旧值、无证据因果断言）。"
        "每条 finding 给出错误类型、原句、正确值、可执行修改建议，以及可回跳到原始 PDF 页的证据 id。"
        "判定完全由确定性规则完成，模型只负责搬运原文，不参与判错；无法核验与核实为错严格区分，"
        "宁可漏报也不误报。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册**草稿**文档的 document_id（ingest_cn_document 返回，document_type=report_draft）。"},
            "as_of": {
                "type": "string",
                "description": (
                    "可选草稿撰写时点（ISO-8601）。用于判定『引用在草稿时点是否已公开』；"
                    "缺省时该项规则不执行并计入覆盖范围，绝不按当前钟表时间猜测。"
                ),
            },
        },
        "required": ["document_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(self, document_id: str = "", as_of: str = "") -> str:
        if not document_id:
            return tool_error(
                "必须提供 document_id。核查对象是具体草稿文档的逐句原文与出处，"
                "不接受 symbol：按 symbol 核查会脱离可回跳的证据链。",
                error_code="checker_missing_argument",
            )
        document = self.registry.get(document_id)
        if document is None:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found",
                document_id=document_id,
            )

        try:
            report = check_draft(
                document_id, registry=self.registry, as_of=as_of or None,
            )
        except DraftNotFound:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found",
                document_id=document_id,
            )
        except Exception as exc:
            return tool_error(
                f"研报核查失败（{type(exc).__name__}: {exc}）", error_code="checker_failed",
                document_id=document_id,
            )

        summary = report["summary"]
        claims = report["claims"]
        findings = report["findings"]

        # Grading the *coverage* of the check, never the verdict (§9.3): a draft
        # full of errors that was fully read is ``ok``; a draft that read cleanly
        # except for three sentences the extractor could not process is ``partial``,
        # because "no finding" from a rule that never ran is not a clean bill.
        unextracted = summary["sentences_unextracted"]
        if not claims:
            status = "blocked"
        elif unextracted:
            status = "partial"
        else:
            status = "ok"

        body: Dict[str, object] = {
            "status": status,
            "document_id": document_id,
            "draft": report["draft"],
            "summary": summary,
            "findings": findings,
            "claims": claims,
            "findings_count": len(findings),
            "claims_count": len(claims),
            "view": _editorial_view(findings),
            "labels": report["labels"],
            "coverage": report["coverage"],
            "engine": report["engine"],
            "evidence_summary": report.get("evidence_summary", {}),
        }

        # §10 gate, run on the payload we are about to hand back. Its own errors are
        # reported beside the findings rather than raising: a validator failure is
        # news about the *tool*, and the reviewer needs to see it.
        validation = validate_checker_report(
            report,
            evidence_lookup=report.get("evidence_index") or None,
            checker_source=checker_module_source(),
        )
        body["validation"] = validation.to_dict()

        if status == "blocked":
            body["error"] = (
                "草稿中没有可核查的句子（解析为空或全部为无数字的叙述句），"
                "本次未产出任何 finding；这**不等于**草稿无误。"
            )
            body["error_code"] = "checker_no_claims"
        if unextracted:
            body["note"] = (
                f"有 {unextracted} 句未能提取为 claim，相关句子未被核查；"
                "未列出的句子不代表已通过。"
            )
        if not report["coverage"]["rules"]["stale_citation"]["ran"]:
            body.setdefault("note", "")
            body["note"] = (str(body["note"]) + " 未提供 as_of，『引用过期数据』规则未执行。").strip()

        return json.dumps(body, ensure_ascii=False, default=str)


#: The order the three severity groups are shown in — errors first, because that is
#: the order a reviewer fixes them in.
_VIEW_GROUPS: tuple = (("errors", "错误（必须修改）"), ("warnings", "提示（需确认）"))


def _editorial_view(findings: List[Dict[str, object]]) -> Dict[str, object]:
    """Findings grouped for the §14 correction list, in a fixed order.

    The UI and the Agent tool render the *same* grouping so the demo and the
    programmatic path cannot disagree about what a reviewer is looking at.
    """
    groups: Dict[str, List[Dict[str, object]]] = {name: [] for name, _ in _VIEW_GROUPS}
    for finding in findings:
        severity = str(finding.get("severity", ""))
        key = "errors" if severity == "error" else "warnings"
        groups[key].append({
            "claim_id": finding.get("claim_id"),
            "check_type": finding.get("check_type"),
            "check_label": finding.get("check_label"),
            "sentence": finding.get("sentence"),
            "claim_page": finding.get("claim_page"),
            "original": finding.get("original"),
            "expected": finding.get("expected"),
            "suggestion": finding.get("suggestion"),
            "confidence": finding.get("confidence"),
            "evidence": finding.get("evidence", []),
        })
    return {
        "groups": [
            {"key": key, "label": label, "count": len(groups[key]), "items": groups[key]}
            for key, label in _VIEW_GROUPS
        ],
        "total": len(findings),
    }


class SearchCnCorpusTool(Tool):
    name = "search_cn_corpus"
    description = (
        "在已摄入的真实金融文档语料（年报 MD&A、公告、研报段落，CARD-01 解析产物）上做"
        "证据化检索：返回带页码与章节出处的命中片段，支持 quote/paraphrase/inference 三型引用。"
        "检索本身只『定位』不『生成』，命中文本须逐字引用或显式 paraphrase；"
        "命中出参**不含 evidence_id**——只有在调用 cite（引用）时才会生成证据 id 并写入检索账本。"
        "未配置向量重排时自动降级 BM25 并在 mode 字段如实标注。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索查询字符串（自然语言或关键词）。"},
            "top_k": {"type": "integer", "description": "返回前 k 个命中，默认 5。"},
            "document_id": {"type": "string", "description": "可选：只在某一已注册文档内检索（ingest_cn_document 返回的 document_id）。"},
            "document_type": {"type": "string", "description": "可选：只检索某类文档（annual|interim|quarterly|announcement|report_draft|other）。"},
        },
        "required": ["query"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        query: str = "",
        top_k: int = 5,
        document_id: str = "",
        document_type: str = "",
    ) -> str:
        if not query or not query.strip():
            return tool_error("必须提供非空 query。", error_code="retrieval_missing_query")

        # -- resolve which documents to index --
        try:
            if document_id:
                doc = self.registry.get(document_id)
                if doc is None:
                    return tool_error(
                        f"未找到已注册文档: {document_id}", error_code="document_not_found",
                        document_id=document_id,
                    )
                ids = [document_id]
            elif document_type:
                ids = [d.document_id for d in self.registry.list(document_type=document_type)]
            else:
                ids = [d.document_id for d in self.registry.list()]
        except Exception as exc:
            return tool_error(f"检索文档解析失败: {exc}", error_code="retrieval_index_error")

        # -- build a deterministic, offline BM25 index over the located facts --
        try:
            engine = RetrievalEngine.from_registry(self.registry, document_ids=ids)
        except Exception as exc:
            return tool_error(f"检索索引构建失败: {exc}", error_code="retrieval_index_error")

        result = engine.search(query, top_k=int(top_k) if top_k and int(top_k) > 0 else 5)
        return json.dumps(
            {
                "status": "ok",
                "query": result.query,
                "mode": result.mode,
                "corpus_size": result.corpus_size,
                # 命中出参**不含 evidence_id**；引用时才由 CitationLedger 生成。
                "hits": [hit.to_dict() for hit in result.hits],
                "warnings": result.warnings,
                "engine": "src.cn.retrieval（自研 BM25，确定性优先）",
            },
            ensure_ascii=False, default=str,
        )


def _publish_analysis_evidence(
    document_id: str,
    *,
    ledger: EvidenceLedger,
    registry: Optional[DocumentRegistry] = None,
    research_as_of: str = "",
) -> Dict[str, object]:
    """Publish the ledger-shaped subset of a document's calculations.

    Re-derived from the registry rather than round-tripped through the service
    payload, so the ledger keeps receiving real :class:`Calculation` records.

    ``research_as_of`` is only used to *check* the ledger: with no cutoff the
    publish result is returned unchecked rather than validated against the wall
    clock, which would make the outcome depend on when the tool happened to run.
    """
    store = registry or DocumentRegistry()
    document = store.get(document_id)
    if document is None:
        raise KeyError(document_id)
    result = analyze_document(normalize_financials(store.get_facts(document_id), document))
    payload = publish_analysis_evidence(result, ledger=ledger, symbol=document_id)
    if research_as_of:
        payload["ledger_valid"] = ledger.validate(research_as_of=research_as_of).valid
    return payload


def _document_claim_graph(registry: DocumentRegistry, document_id: str, *, run_id: str = ""):
    """CARD-01 → CARD-02 → CARD-03 → CARD-09 for one ingested document.

    Fully deterministic and offline — the same document always yields the same
    graph, which is what makes ``trace_cn_claim`` reproducible in a demo. Returns
    ``(graph, analysis, document)`` or raises ``KeyError`` for an unknown id.
    """
    document = registry.get(document_id)
    if document is None:
        raise KeyError(document_id)
    normalization = normalize_financials(registry.get_facts(document_id), document)
    analysis = analyze_document(normalization)
    ledger = EvidenceLedger(snapshot_id=document_id)
    register_normalized_evidence(document, normalization, ledger=ledger)
    jumps = normalization_jumps(document, normalization)
    graph = build_graph_from_analysis(
        analysis,
        run_id=run_id or f"graph-{document_id}",
        document_id=document_id,
        ledger=ledger,
        jumps=jumps,
    )
    return graph, analysis, document


class TraceCnClaimTool(Tool):
    name = "trace_cn_claim"
    description = (
        "把一条结论（claim）逐层展开到原始 PDF 页：结论 → 依赖的计算 → 输入证据 → 文档页码/坐标。"
        "图谱区分 fact / inference / opinion（事实、推论、观点），诊断信号进图时是 inference 而非 fact；"
        "每条边带 REQUIRED/SUPPORTING/OPTIONAL 依赖语义。缺失或悬空的引用如实报出，绝不静默填充。"
        "本工具只读，数值全部来自确定性计算与已解析事实，模型不参与。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册文档的 document_id（ingest_cn_document 返回）。"},
            "claim_id": {"type": "string", "description": "要展开的 claim_id，取自 build 图谱的 claims 键。"},
            "max_depth": {"type": "integer", "description": "最大展开层数，默认 8。"},
        },
        "required": ["document_id", "claim_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(self, document_id: str = "", claim_id: str = "", max_depth: int = 8) -> str:
        if not document_id:
            return tool_error("必须提供 document_id。", error_code="graph_missing_argument")
        if not claim_id:
            return tool_error("必须提供 claim_id。", error_code="graph_missing_argument")
        try:
            graph, _analysis, document = _document_claim_graph(self.registry, document_id)
        except KeyError:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found", document_id=document_id,
            )
        except Exception as exc:
            return tool_error(
                f"图谱构建失败（{type(exc).__name__}: {exc}）", error_code="graph_build_failed",
                document_id=document_id,
            )

        claim = graph.claims.get(claim_id)
        if claim is None:
            return tool_error(
                f"未找到 claim: {claim_id}", error_code="claim_not_found",
                document_id=document_id, available=sorted(graph.claims)[:50],
            )

        tree = graph.trace(claim_id, max_depth=int(max_depth) if max_depth else 8)
        report = graph.detect().to_dict()
        return json.dumps(
            {
                "status": "ok",
                "document_id": document_id,
                "filename": document.filename,
                "run_id": graph.run_id,
                "claim": claim.to_dict(),
                "trace_tree": tree.to_dict(),
                "locatable": tree.locatable,
                "detection": report,
                "coverage": graph.coverage(),
                "engine": "src.cn.graph（确定性图遍历，非模型输出）",
            },
            ensure_ascii=False, default=str,
        )


class DeactivateCnEvidenceTool(Tool):
    name = "deactivate_cn_evidence"
    description = (
        "审计模式下『撤回』一条证据（失败注入演示 #6）：不物理删除，而是追加一条 inactive 记录，"
        "随后重跑状态传播——该证据支撑的 fact 及其下游 inference 变成 blocked，并返回旧→新状态 diff。"
        "用于演示『证据一撤，结论立刻不敢下』。必须给出 reason（删除也是审计事件）。"
        "注意：图谱按本次调用重新构建，撤销结果需由 CARD-12 证据包持久化。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册文档的 document_id。"},
            "evidence_id": {"type": "string", "description": "要撤回的证据 id（trace_cn_claim 的 trace_tree 或 normalization 证据 id）。"},
            "reason": {"type": "string", "description": "撤回原因，必填——审计要求。"},
        },
        "required": ["document_id", "evidence_id", "reason"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(self, document_id: str = "", evidence_id: str = "", reason: str = "") -> str:
        if not document_id or not evidence_id:
            return tool_error(
                "必须提供 document_id 与 evidence_id。", error_code="graph_missing_argument",
            )
        if not (reason or "").strip():
            return tool_error(
                "必须提供非空 reason：撤回证据是审计事件，不允许无理由删除。",
                error_code="deactivation_missing_reason",
            )
        try:
            graph, analysis, document = _document_claim_graph(self.registry, document_id)
        except KeyError:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found", document_id=document_id,
            )
        except Exception as exc:
            return tool_error(
                f"图谱构建失败（{type(exc).__name__}: {exc}）", error_code="graph_build_failed",
                document_id=document_id,
            )

        if evidence_id not in graph.evidence_index:
            return tool_error(
                f"未找到证据: {evidence_id}", error_code="evidence_not_found",
                document_id=document_id, note="本次构建的图谱中不存在该证据 id。",
            )

        try:
            impacted = deactivate_evidence(graph, evidence_id, reason)
            report = revalidate(graph, changed_evidence_ids=[evidence_id])
        except Exception as exc:
            return tool_error(
                f"撤回失败（{type(exc).__name__}: {exc}）", error_code="deactivation_failed",
                document_id=document_id,
            )

        status_after = {claim_id: claim.validation_status for claim_id, claim in graph.claims.items()}
        validation = validate_graph(graph, registry=analysis.registry)
        return json.dumps(
            {
                "status": "ok",
                "document_id": document_id,
                "filename": document.filename,
                "run_id": graph.run_id,
                "evidence_id": evidence_id,
                "reason": reason,
                "audit_mode": "append_only_no_physical_delete",
                "impacted_claims": impacted,
                "revalidation_report": report.to_dict(),
                "blocked_claims": list(report.blocked_claims),
                "status_after": status_after,
                "graph_valid": validation.valid,
                "detection": validation.detection,
                "engine": "src.cn.graph（确定性传播，非模型输出）",
            },
            ensure_ascii=False, default=str,
        )


def _document_valuation_for_memo(
    registry: DocumentRegistry,
    document_id: str,
    raw_price: Optional[float],
    target_multiples: Optional[Dict[str, object]] = None,
    valid_from: str = "",
):
    """Best-effort deterministic valuation for the memo (CARD-08 reuse).

    Returns ``(ValuationResult | None, note)``. ``note`` is empty on success and
    an honest reason otherwise — the memo must be able to say "no valuation"
    rather than reach for a placeholder, so a failure here is *reported*, never
    patched over.
    """
    document = registry.get(document_id)
    facts = registry.get_facts(document_id)
    if document is None or not facts:
        return None, "document_or_facts_missing"
    normalization = normalize_financials(facts, document)
    try:
        inputs = valuation_inputs_from_normalization(
            normalization,
            symbol=str(document.company or document_id),
            raw_price=float(raw_price),
            document_id=document_id,
        )
    except (TypeError, ValueError) as exc:
        return None, str(exc) or "valuation_blocked"

    clean_targets: Dict[str, float] = {}
    for key, value in dict(target_multiples or {}).items():
        try:
            clean_targets[str(key).upper()] = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None, f"valuation_target_invalid:{key}"

    assumptions = AssumptionRegistry(
        sourced={key: value for key, value in inputs.evidence_ids.items() if value},
        reserve=[name for name in SOURCED_PARAMETERS if name not in inputs.evidence_ids],
    )
    kwargs: Dict[str, object] = {
        "calculations": CalculationRegistry(),
        "valid_from": valid_from,
    }
    if clean_targets:
        kwargs["target_multiples"] = clean_targets
    try:
        return build_valuation(inputs, assumptions, **kwargs), ""
    except AssumptionError as exc:
        return None, f"valuation_assumption_error:{exc}"


class GenerateCnInvestmentMemoTool(Tool):
    name = "generate_cn_investment_memo"
    description = (
        "生成 11 段买方投资备忘录（CARD-10）：执行摘要 / 公司概览 / 财务表现 / 投资逻辑 / 估值 / "
        "催化剂 / 风险 / 反向论证 / 证伪条件 / 持续跟踪指标 / 证据附录。每个句子标注 fact/inference/opinion，"
        "并绑定 Claim-Evidence Graph 的 claim_id；数值句必须绑定确定性计算或证据，否则被硬校验拒绝。"
        "叙述段由确定性模板渲染（不依赖 Multi-Agent），也可注入 LLM 叙述器，但其输出只能引用白名单 claim_id。"
        "上游缺失（如未给 raw_price 则无估值）时对应段标 data_unavailable 并如实留白，不编造任何数字。"
        "本工具只接受 document_id：按 symbol 生成会绕开可回溯的证据链。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "已注册文档的 document_id（ingest_cn_document 返回）。"},
            "raw_price": {"type": "number", "description": "可选 RAW 收盘价（元/股）。提供时才生成估值段，否则估值段标 data_unavailable。"},
            "symbol": {"type": "string", "description": "仅为兼容 CARD-10 §6 的入参名而保留；单独传 symbol 会被显式拒绝（需要 document_id）。"},
            "target_multiples": {"type": "object", "description": "可选分析师目标倍数基准值，如 {\"PE\": 20}；仅登记为假设。"},
            "valid_from": {"type": "string", "description": "假设生效时点（ISO-8601），缺省留白。"},
            "as_of": {"type": "string", "description": "备忘录出具时点（ISO-8601），缺省留白。"},
        },
        "required": ["document_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        document_id: str = "",
        raw_price: Optional[float] = None,
        symbol: str = "",
        target_multiples: Optional[Dict[str, object]] = None,
        valid_from: str = "",
        as_of: str = "",
    ) -> str:
        if not document_id:
            reason = (
                "CARD-10 §6 的入参名是 symbol，但按 symbol 生成备忘录会绕开可回溯的证据链"
                "（CARD-02 规范化只接文档事实），故与前序工具一致：本工具只接受 document_id。"
            )
            if symbol:
                reason = f"仅提供 symbol=『{symbol}』不足以生成备忘录。" + reason
            return tool_error(reason, error_code="memo_missing_argument", symbol=symbol or None)

        try:
            graph, _analysis, document = _document_claim_graph(self.registry, document_id)
        except KeyError:
            return tool_error(
                f"未找到已注册文档: {document_id}", error_code="document_not_found",
                document_id=document_id,
            )
        except Exception as exc:
            return tool_error(
                f"图谱构建失败（{type(exc).__name__}: {exc}）", error_code="graph_build_failed",
                document_id=document_id,
            )

        analysis = _analysis
        valuation = None
        valuation_note = "raw_price_not_provided"
        if raw_price is not None:
            valuation, valuation_note = _document_valuation_for_memo(
                self.registry, document_id, raw_price,
                target_multiples=target_multiples, valid_from=valid_from,
            )

        try:
            memo: InvestmentMemo = build_memo(
                analysis,
                valuation=valuation,
                graph=graph,
                symbol=str(document.company or document_id),
                document_id=document_id,
                as_of=as_of,
            )
        except Exception as exc:
            return tool_error(
                f"备忘录生成失败（{type(exc).__name__}: {exc}）", error_code="memo_build_failed",
                document_id=document_id,
            )

        payload = render_memo_json(memo)
        validation = validate_memo(
            memo, graph=graph, calculation_ids=getattr(valuation, "calculation_ids", ()) or ()
        )
        section_status = {
            section.section_id: {
                "status": section.data_status,
                "sentence_count": len(section.sentences),
                "warnings": list(section.warnings),
            }
            for section in memo.sections
        }
        return json.dumps(
            {
                "status": "ok",
                "document_id": document_id,
                "filename": document.filename,
                "symbol": memo.symbol,
                "run_id": graph.run_id,
                "memo_id": memo.memo_id,
                "section_ids": list(SECTION_IDS),
                "memo": payload,
                "markdown": render_memo_markdown(memo),
                "data_grade": memo.data_grade,
                "validation": validation.to_dict(),
                "valuation_status": getattr(valuation, "status", None),
                "valuation_note": valuation_note,
                "section_status": section_status,
                "warnings": list(memo.warnings),
                "disclaimers": list(memo.disclaimers),
                "narrator": "template（确定性模板，非模型输出）",
                "engine": "src.cn.memo（确定性模板 + Claim-Evidence Graph 句级标注）",
            },
            ensure_ascii=False, default=str,
        )


class ResolveCnSourceConflictsTool(Tool):
    name = "resolve_cn_source_conflicts"
    description = (
        "裁决跨来源数值冲突（CARD-13）。输入若干已注册文档（+ 可选的更正公告），把同一指标同一期间的多个"
        "候选值先分类再按明确优先级裁决：后发布且明确更正前值的监管披露 > 原监管披露 > 官方公司公告 > 第三方数据源。"
        "区分三种变化状态：DATA_CONFLICT（同口径同版本值不一致）/ VERSION_SUPERSESSION（新版本替代旧值，"
        "旧值保留为 superseded）/ NEW_INFORMATION（新增期间，不覆盖历史）。"
        "同级来源矛盾时不裁决，标 unresolved 并阻断该指标（宁可阻断不误选）；任何被丢弃的候选都必须留下 "
        "ConflictRecord，静默覆盖会直接报错。更正公告正文里的『更正前/更正后』数值可被解析，但单位缺失、期间"
        "无法确定或指标无法识别时一律跳过并告警，不做任何推断。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参与裁决的已注册文档 document_id 列表（定期报告等）。",
            },
            "correction_document_id": {
                "type": "string",
                "description": "可选：更正公告的 document_id。提供时会从正文解析『更正前/更正后』数值对并作为最高优先级候选。",
            },
            "scope": {
                "type": "string",
                "description": "可选：为更正公告正文中的数值声明口径（如 合并）。不声明时该候选单独分组，不会被并入已声明口径的组。",
            },
            "company": {
                "type": "string",
                "description": "可选：本次裁决所属公司名。缺省时跨文档分组会被标记 company_unverified_across_documents 并如实告警。",
            },
        },
        "required": ["document_ids"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        document_ids: Optional[List[str]] = None,
        correction_document_id: str = "",
        scope: str = "",
        company: str = "",
    ) -> str:
        ids = [item for item in (document_ids or []) if item]
        if not ids:
            return tool_error(
                "CARD-13 §6 需要至少一个 document_id：按 symbol 无法定位到可比来源。",
                error_code="conflict_missing_argument",
            )

        facts: List[object] = []
        source_class_by_document: Dict[str, str] = {}
        warnings: List[str] = []
        documents: List[str] = []

        for document_id in ids:
            document = self.registry.get(document_id)
            if document is None:
                return tool_error(
                    f"未找到文档 {document_id}。", error_code="conflict_unknown_document"
                )
            normalization = normalize_financials(
                self.registry.get_facts(document_id), document
            )
            facts.extend(normalization.facts)
            source_class_by_document[document_id] = default_source_class(document.document_type)
            documents.append(document.filename)

        extra_candidates: List[object] = []
        pairs_payload: List[Dict[str, object]] = []
        if correction_document_id:
            correction_doc = self.registry.get(correction_document_id)
            if correction_doc is None:
                return tool_error(
                    f"未找到更正公告 {correction_document_id}。",
                    error_code="conflict_unknown_document",
                )
            pairs, pair_warnings = correction_pairs(
                self.registry.get_facts(correction_document_id),
                scope=scope or None,
                published_at=correction_doc.publication_date,
            )
            warnings.extend(pair_warnings)
            pairs_payload = [pair.to_dict() for pair in pairs]
            extra_candidates = correction_candidates(pairs, source_id=correction_document_id)
            source_class_by_document[correction_document_id] = default_source_class(
                correction_doc.document_type, is_correction=True
            )
            documents.append(correction_doc.filename)

        choices, records = resolve_conflicts(
            facts,
            source_class_by_document=source_class_by_document,
            extra_candidates=extra_candidates,
            company_of=(lambda _source: company) if company else None,
        )
        validation = validate_conflicts(records)

        choice_payload = []
        for choice in choices:
            item = choice.to_dict()
            item["key"] = choice.key
            item["selected_fact_id"] = (
                getattr(choice.selected.fact, "normalized_fact_id", None)
                if choice.selected is not None and choice.selected.fact is not None
                else None
            )
            item["value_from_prose"] = bool(
                choice.selected is not None and choice.selected.fact is None
            )
            choice_payload.append(item)

        return json.dumps(
            {
                "status": "ok" if validation.valid else "invalid",
                "documents": documents,
                "document_ids": list(ids),
                "correction_document_id": correction_document_id or None,
                "declared_scope": scope or None,
                "company": company or None,
                "change_kinds": ["DATA_CONFLICT", "VERSION_SUPERSESSION", "NEW_INFORMATION"],
                "choices": choice_payload,
                "conflicts": [record.to_dict() for record in records],
                "conflict_summary": summarize_conflicts(records),
                "unresolved": unresolved_report(records),
                "unresolved_metric_keys": sorted({choice.key for choice in choices if choice.blocked}),
                "correction_pairs": pairs_payload,
                "pair_warnings": warnings,
                "validation": validation.to_dict(),
                "engine": "src.cn.conflict（确定性优先级规则，非模型输出）",
                "resolver": "card13.v1",
                "note": (
                    "unresolved 的指标必须在下游全部阻断；ConflictRecord 为只追加记录，不得就地修改。"
                ),
            },
            ensure_ascii=False, default=str,
        )


class ValidateCnAcmeTool(Tool):
    name = "validate_cn_acme"
    description = (
        "对一份已注册文档跑 ACME 会计约束验证（CARD-22）：解析完不直接相信，而是用六类确定性约束核对——"
        "会计恒等式（资产≈负债+权益、毛利≈收入−成本）、表格小计勾稽、期间关系（只调 CARD-02 的期间引擎）、"
        "跨期连续性（期初=上期期末，需上一期文档）、跨来源一致性（需 Snapshot Provider，四分类 "
        "MATCH/ROUNDING_MATCH/CONFLICT/NOT_COMPARABLE，裁决交 CARD-13）、跨模态一致性（正文 vs 表格 vs 图表）。"
        "每个发现都带 expected/actual/residual/容差与可跳转来源（页码+坐标）；未能核对的约束会显式列出原因，"
        "绝不静默当成通过。全规则确定性、零 LLM，且没有任何自动改值路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "已注册文档的 document_id（必需）。按 symbol 无法定位到可验证的披露。",
            },
            "prior_document_id": {
                "type": "string",
                "description": "可选：上一期报告的 document_id，用于核对『本期期初 = 上期期末』的跨期连续性。",
            },
            "snapshot_id": {
                "type": "string",
                "description": "可选：本地行情快照 snapshot_id，用于跨来源一致性核对；必须与 symbol 同时提供。",
            },
            "symbol": {
                "type": "string",
                "description": "可选：快照对应的证券代码（如 600519.SH），与 snapshot_id 同时提供。",
            },
            "include_identities": {
                "type": "boolean",
                "description": "是否运行会计恒等式族（默认 true）。关闭时该族会在报告中显式标注未检查。",
            },
        },
        "required": ["document_id"],
    }

    def __init__(
        self,
        registry: Optional[DocumentRegistry] = None,
        snapshot: Optional[SnapshotProvider] = None,
    ):
        self.registry = registry or DocumentRegistry()
        self.snapshot = snapshot

    async def execute(
        self,
        document_id: str = "",
        prior_document_id: str = "",
        snapshot_id: str = "",
        symbol: str = "",
        include_identities: bool = True,
    ) -> str:
        if not document_id:
            return tool_error(
                "CARD-22 §8 需要 document_id：约束核对必须落在具体披露上，"
                "按 symbol 无法定位到可比来源。",
                error_code="acme_missing_argument",
            )
        document = self.registry.get(document_id)
        if document is None:
            return tool_error(
                f"未找到文档 {document_id}。", error_code="acme_unknown_document"
            )
        if snapshot_id and not symbol:
            return tool_error(
                "提供了 snapshot_id 但缺少 symbol：跨源核对需要快照自身的证券代码。",
                error_code="acme_missing_argument",
            )

        warnings: List[str] = []
        raw_facts = self.registry.get_facts(document_id)
        normalization = normalize_financials(raw_facts, document)
        analysis = analyze_document(normalization, documents=document)

        prior_facts: List[object] = []
        if prior_document_id:
            prior_document = self.registry.get(prior_document_id)
            if prior_document is None:
                return tool_error(
                    f"未找到上一期文档 {prior_document_id}。",
                    error_code="acme_unknown_document",
                )
            prior_facts = normalize_financials(
                self.registry.get_facts(prior_document_id), prior_document
            ).facts

        snapshot_facts: List[object] = []
        if snapshot_id:
            if self.snapshot is None:
                return tool_error(
                    "本次会话没有可用的本地快照提供方，跨源核对无法进行。",
                    error_code="acme_snapshot_unavailable",
                )
            try:
                canonical = normalize_cn_symbol(symbol)
                statements = self.snapshot.get_financial_statements(canonical)
            except Exception as exc:  # noqa: BLE001 - 转成 error 信封，绝不静默
                return tool_error(
                    f"快照 {snapshot_id} 无法读取：{type(exc).__name__}: {exc}",
                    error_code="acme_snapshot_unavailable",
                )
            snapshot_facts = facts_from_statements(statements, source_id=snapshot_id,
                                                   warnings=warnings)

        report = validate_multimodal_evidence(
            normalization.facts,
            normalization.statements,
            snapshot_facts or None,
            raw_facts=raw_facts,
            text_facts=raw_facts,
            prior_facts=prior_facts or None,
            scope_signals=analysis.scope_signals,
            snapshot_id=snapshot_id or None,
            document_id=document_id,
            detected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            include_identities=include_identities,
        )
        validation = validate_acme_report(report)
        payload = report.to_dict()
        payload.update(
            {
                "status": "ok" if validation.valid else "invalid",
                "filename": document.filename,
                "prior_document_id": prior_document_id or None,
                "snapshot_id": snapshot_id or None,
                "report_period": normalization.report_period,
                "missing": [item.to_dict() for item in analysis.missing],
                "validation": validation.to_dict(),
                "adapter_warnings": warnings,
                "engine": "src.cn.acme（确定性约束规则，非模型输出）",
                "note": (
                    "violated/warning/not_comparable 必须进人工复核队列；"
                    "families_not_checked 与 constraints_not_available 列出的项目本次未核对，"
                    "不等于通过。初赛无任何自动改值路径。"
                ),
            }
        )
        return json.dumps(payload, ensure_ascii=False, default=str)


class VerifyCnClaimTool(Tool):
    name = "verify_cn_claim"
    description = (
        "为一条结论生成 Financial Claim Passport（结论护照）并**脱离模型重新验证**："
        "六块证明（来源 / 期间口径 / 计算 / 假设 / 依赖 / 完整性）+ 五态判定"
        "（VERIFIED / DEGRADED / STALE / BLOCKED / CONFLICTED）。"
        "验证是**真的重算**——重新计算文档 sha256、按公式用 inputs 重算输出、沿 CARD-09 依赖语义重查上游状态、"
        "查 CARD-13 未决冲突与版本谱系——不是把原文再读一遍，也不是让模型复述。"
        "全程确定性、零 LLM；五态由验证器产生，模型不得改写。"
        "BLOCKED / CONFLICTED 的结论不得进入备忘录结论段，STALE 须带「待刷新」标注。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "已注册文档的 document_id（必需）。护照要落在具体披露与图谱上。",
            },
            "claim_id": {
                "type": "string",
                "description": "要验证的 claim_id，取自 trace_cn_claim 或图谱的 claims 键。",
            },
            "declared_scope": {
                "type": "string",
                "description": (
                    "可选：调用方声明的口径（如 consolidated / parent）。"
                    "必须由调用方给出才有意义——护照自身的口径取自计算节点，拿它跟同一节点比是循环论证。"
                ),
            },
            "declared_version": {
                "type": "string",
                "description": (
                    "可选：调用方声明的版本状态（AS_REPORTED / RESTATED / CORRECTED / SUPERSEDED）。"
                    "未声明时按 CARD-13 版本谱系与证据存活态推断，并在 proof 中记录推断依据。"
                ),
            },
            "include_passport": {
                "type": "boolean",
                "description": "是否返回完整护照（默认 true）。批量验证时可关掉，只取五态与逐项检查。",
            },
        },
        "required": ["document_id", "claim_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        document_id: str = "",
        claim_id: str = "",
        declared_scope: str = "",
        declared_version: str = "",
        include_passport: bool = True,
    ) -> str:
        if not document_id:
            return tool_error(
                "必须提供 document_id：护照要落在具体披露与图谱上。",
                error_code="proof_missing_argument",
            )
        if not claim_id:
            return tool_error(
                "必须提供 claim_id：验证的对象是某一条结论。",
                error_code="proof_missing_argument",
            )
        try:
            graph, analysis, document = _document_claim_graph(self.registry, document_id)
        except KeyError:
            return tool_error(
                f"未找到已注册文档: {document_id}",
                error_code="document_not_found", document_id=document_id,
            )
        except Exception as exc:
            return tool_error(
                f"图谱构建失败（{type(exc).__name__}: {exc}）",
                error_code="graph_build_failed", document_id=document_id,
            )

        claim = graph.claims.get(claim_id)
        if claim is None:
            return tool_error(
                f"未找到 claim: {claim_id}", error_code="claim_not_found",
                document_id=document_id, available=sorted(graph.claims)[:50],
            )

        def _loader(wanted: str) -> Optional[bytes]:
            """Read the ingested bytes so §7.1's hash is *re*computed, not trusted."""
            record = self.registry.get(wanted)
            stored = getattr(record, "stored_path", None)
            if not stored:
                return None
            path = Path(stored)
            return path.read_bytes() if path.exists() else None

        context = ProofContext(
            graph=graph,
            # CARD-03's registry is what makes §7.6 a real recomputation rather
            # than a note saying we could not check.
            calculation_registry=analysis.registry,
            run_id=graph.run_id,
            document_hashes={document_id: document.sha256} if document.sha256 else {},
            document_loader=_loader,
            declared_versions={claim_id: declared_version} if declared_version else {},
            declared_scopes={claim_id: declared_scope} if declared_scope else {},
            detected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            passport = build_financial_proof_object(claim_id, context)
            verification = verify_claim(claim_id, context)
        except ProofBuildError as exc:
            return tool_error(
                f"护照构建失败（claim 标记 proof_build_failed，其余结论不受影响）：{exc}",
                error_code="proof_build_failed",
                document_id=document_id, claim_id=claim_id,
            )
        validation = validate_proof(passport, verification=verification)

        payload: dict = {
            "status": "ok" if validation.valid else "invalid",
            "document_id": document_id,
            "filename": document.filename,
            "claim_id": claim_id,
            "overall_status": verification.overall_status,
            "status_reasons": list(verification.status_reasons),
            "checks": [item.to_dict() for item in verification.checks],
            "verify_source_version": verification.verify_source_version,
            "verified_at": verification.verified_at,
            "verification_result": verification.to_dict(),
            "validation": validation.to_dict(),
            "engine": "src.cn.proof（确定性重算与重查，非模型输出）",
            "note": (
                "五态由验证器产生，模型不得改写；checks 逐项留痕，跳过项（skipped）"
                "不等于通过。recomputed=False 的检查只做了交叉核对（缺输入字节），"
                "不得当作已重算。"
            ),
        }
        if include_passport:
            payload["passport"] = passport.to_dict()
        return json.dumps(payload, ensure_ascii=False, default=str)


#: 一次脆弱性分析最多为多少条结论取护照状态（取不到就如实标注截断）。
_FRAGILITY_VERIFY_CAP = 50


def _threshold_from_record(record: Dict) -> FragilityThreshold:
    """把工具入参里的一条阈值规格变成 :class:`FragilityThreshold`.

    缺字段直接抛 :class:`FragilityError`（由调用方转成 error 信封）：
    一条来源不明的阈值不该被"尽量解析"，它就是不该存在。
    """
    if not isinstance(record, dict):
        raise FragilityError(f"阈值必须是对象：{record!r}")
    node_id = str(record.get("node_id") or "").strip()
    source_ref = str(record.get("source_ref") or "").strip()
    if not node_id:
        raise FragilityError("阈值缺少 node_id：不知道该拿哪条结论/证据去比。")
    if not source_ref:
        raise FragilityError(f"阈值 {node_id} 缺少 source_ref：没有依据的阈值不是阈值。")
    raw_threshold = record.get("threshold")
    if raw_threshold is None:
        raise FragilityError(f"阈值 {node_id} 缺少 threshold。")
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        raise FragilityError(f"阈值 {node_id} 的 threshold 不是数值：{raw_threshold!r}")
    current = record.get("current_value")
    if current is not None and not isinstance(current, (int, float)):
        raise FragilityError(f"阈值 {node_id} 的 current_value 不是数值：{current!r}")
    return FragilityThreshold(
        node_id=node_id,
        metric=str(record.get("metric") or ""),
        comparison=str(record.get("comparison") or ">="),
        threshold=threshold,
        unit=str(record.get("unit") or ""),
        source_type=str(record.get("source_type") or ""),
        source_ref=source_ref,
        period_key=(str(record["period_key"]) if record.get("period_key") else None),
        label=str(record.get("label") or ""),
        rationale=str(record.get("rationale") or ""),
        current_value=(float(current) if current is not None else None),
        current_value_source=str(record.get("current_value_source") or ""),
    )


class AnalyzeCnThesisFragilityTool(Tool):
    name = "analyze_cn_thesis_fragility"
    description = (
        "对一条投资逻辑（Thesis）做**确定性**脆弱性分析：哪些 Claim/证据/计算位于它的 "
        "REQUIRED 路径上、哪一根是单点失效、**最少打断哪几个节点就能让这条逻辑倒下**"
        "（最小割集，且每个割集都经过反解验证），并输出 Monitoring Plan"
        "（依赖 → 可观察 KPI → 触发条件 → 受影响结论 → 重验证动作）。"
        "全部是图算法，零 LLM。数值阈值只接受白名单来源（public_assumption / analyst_input / "
        "model_constraint），来源解析不了就如实剔除并记账，绝不凭空造阈值。"
        "分析对象可以是声明的 Thesis，也可以是图里推论自身的结构；两者都会如实标注来源。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "已注册文档的 document_id（必需）。",
            },
            "thesis_id": {
                "type": "string",
                "description": "可选：只分析这一条 Thesis；省略则分析全部。",
            },
            "theses": {
                "type": "array",
                "description": (
                    "可选：声明式 Thesis 规格，元素形如 "
                    "{\"thesis_id\":\"T01\",\"statement\":\"盈利改善具有持续性\","
                    "\"claim_ids\":[...],\"dependency_roles\":{\"c1\":\"REQUIRED\"}}。"
                    "引用了图上不存在的 claim 会被拒绝，不会静默忽略。"
                ),
                "items": {"type": "object"},
            },
            "thresholds": {
                "type": "array",
                "description": (
                    "可选：数值失效条件，元素形如 {node_id, metric, comparison('>='|'>'|'<='|'<'), "
                    "threshold, unit, source_type, source_ref}。source_type 必须在白名单内且 "
                    "source_ref 必须真的解析得到，否则拒收。"
                ),
                "items": {"type": "object"},
            },
            "include_verification": {
                "type": "boolean",
                "description": (
                    "是否同时取 CARD-23 护照的五态状态并汇入报告（默认 true）。"
                    "取不到时报告会写明 verification_available=false，不会当成「都验过了」。"
                ),
            },
            "include_monitoring": {
                "type": "boolean",
                "description": "是否输出 Monitoring Plan（默认 true）。",
            },
        },
        "required": ["document_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        document_id: str = "",
        thesis_id: str = "",
        theses: Optional[List[Dict]] = None,
        thresholds: Optional[List[Dict]] = None,
        include_verification: bool = True,
        include_monitoring: bool = True,
    ) -> str:
        if not document_id:
            return tool_error(
                "必须提供 document_id：脆弱性要落在具体披露与图谱上。",
                error_code="fragility_missing_argument",
            )
        try:
            graph, analysis, document = _document_claim_graph(self.registry, document_id)
        except KeyError:
            return tool_error(
                f"未找到已注册文档: {document_id}",
                error_code="document_not_found", document_id=document_id,
            )
        except Exception as exc:
            return tool_error(
                f"图谱构建失败（{type(exc).__name__}: {exc}）",
                error_code="graph_build_failed", document_id=document_id,
            )

        try:
            declared = theses or None
            compiled_theses, thesis_source = resolve_theses(graph, declared)
        except FragilityError as exc:
            return tool_error(str(exc), error_code="fragility_invalid_thesis", document_id=document_id)

        if thesis_source == THESIS_SOURCE_NONE or not compiled_theses:
            return json.dumps(
                {
                    "status": "unavailable",
                    "document_id": document_id,
                    "filename": document.filename,
                    "thesis_source": thesis_source,
                    "unavailable_reason": "no_theses_declared",
                    "note": (
                        "图上没有任何 Thesis，也没有可提升为 Thesis 的推论（CARD-03 诊断信号）。"
                        "脆弱性分析无从下手时如实返回 unavailable，而不是给一份看起来干净的空报告。"
                    ),
                    "engine": "src.cn.fragility（确定性图算法，零 LLM）",
                },
                ensure_ascii=False,
                default=str,
            )

        if thesis_id:
            if thesis_id not in {item.thesis_id for item in compiled_theses}:
                return tool_error(
                    f"未找到 thesis: {thesis_id}",
                    error_code="thesis_not_found", document_id=document_id,
                    available=sorted(item.thesis_id for item in compiled_theses),
                )
            scope_ids = [thesis_id]
        else:
            scope_ids = [item.thesis_id for item in compiled_theses]

        try:
            compiled_thresholds = [_threshold_from_record(item) for item in (thresholds or [])]
        except FragilityError as exc:
            return tool_error(
                str(exc), error_code="fragility_invalid_threshold", document_id=document_id,
            )

        verification_statuses = None
        verification_note = "not_requested"
        if include_verification:
            verification_statuses, verification_note = self._passport_statuses(
                graph, analysis, document, document_id, scope_ids
            )

        try:
            report = build_fragility_report(
                graph,
                scope_ids,
                compiled_thresholds or None,
                verification_statuses=verification_statuses,
                thesis_source=thesis_source,
            )
        except FragilityError as exc:
            return tool_error(str(exc), error_code="fragility_build_failed", document_id=document_id)

        if not include_monitoring:
            report.monitoring_metrics = []
        validation = validate_fragility(graph, report)

        payload: dict = {
            "status": "ok" if validation.valid else "invalid",
            "document_id": document_id,
            "filename": document.filename,
            "run_id": graph.run_id,
            "thesis_source": thesis_source,
            "fragility_status": report.status,
            "fragility_unavailable_reason": report.unavailable_reason,
            "thesis_id": report.thesis_id,
            "thesis_ids": list(report.thesis_ids),
            "fragility_report": report.to_dict(),
            "validation": validation.to_dict(),
            "verification_note": verification_note,
            "engine": "src.cn.fragility（确定性图算法，零 LLM）",
            "note": (
                "关键依赖 / 单点失效 / 最小割集全部是图算法结果，每个割集都经过「移除后 Thesis 是否 BLOCKED」"
                "的反解验证；pruned 或 cut_sets_truncated 为真时结果不是全局最优，已如实标注。"
                "没有数值阈值时只做结构脆弱性，不会用编出来的数字补上。"
            ),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _passport_statuses(self, graph, analysis, document, document_id: str, scope_ids: List[str]):
        """取 CARD-23 五态（只对 scope 内 Thesis 的成员 claim，且有上限）."""
        claim_ids: List[str] = []
        for thesis_id in scope_ids:
            thesis = graph.theses.get(thesis_id)
            if thesis is None:
                continue
            for claim_id in thesis.claim_ids:
                if claim_id in graph.claims and claim_id not in claim_ids:
                    claim_ids.append(claim_id)
        if not claim_ids:
            return None, "no_claims_in_scope"
        capped = claim_ids[:_FRAGILITY_VERIFY_CAP]

        def _loader(wanted: str) -> Optional[bytes]:
            record = self.registry.get(wanted)
            stored = getattr(record, "stored_path", None)
            if not stored:
                return None
            path = Path(stored)
            return path.read_bytes() if path.exists() else None

        context = ProofContext(
            graph=graph,
            calculation_registry=getattr(analysis, "registry", None),
            run_id=graph.run_id,
            document_hashes={document_id: document.sha256} if getattr(document, "sha256", "") else {},
            document_loader=_loader,
            detected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        statuses: Dict[str, str] = {}
        failed: List[str] = []
        for claim_id in capped:
            try:
                statuses[claim_id] = verify_claim(claim_id, context).overall_status
            except Exception:  # pragma: no cover - defensive; a passport failure
                failed.append(claim_id)
        note = f"verified={len(statuses)}"
        if failed:
            note += f";failed={','.join(sorted(failed))}"
        if len(claim_ids) > len(capped):
            note += f";capped_at={_FRAGILITY_VERIFY_CAP}（共 {len(claim_ids)} 条）"
        return statuses or None, note


class RevalidateCnClaimsTool(Tool):
    name = "revalidate_cn_claims"
    description = (
        "当新财报/更正公告/重述/新快照进入后，做**确定性**的影响传播与**增量重验证**："
        "算出受影响子图（限深），只重算那一片，并给出 impact 统计"
        "（被替代事实 / 过期计算 / 失效结论 / 被削弱的逻辑 / 受影响估值 / 受影响备忘段）"
        "与重验证动作清单。"
        "三类变化分开处理、不混为一谈：VERSION_SUPERSESSION（新版替代旧值 → 旧值退出口径、"
        "依赖它的结论转 needs_refresh）、DATA_CONFLICT（同期间同口径冲突**未裁决**才 fail-closed 阻断）、"
        "NEW_INFORMATION（新期间数据 → 需刷新，不阻断）。"
        "增量结果每次都与全量重算现场对账，不一致就回落全量并如实记录。零 LLM。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "已注册文档的 document_id（必需）。",
            },
            "event": {
                "type": "object",
                "description": (
                    "时间事件，形如 {event_id, event_kind, event_type, event_date, "
                    "changed_source_ids:[...], description, supersedes, resolved_by}。"
                    "event_kind 必须是 DATA_CONFLICT / VERSION_SUPERSESSION / NEW_INFORMATION 之一；"
                    "event_type 必须是 new_filing / correction / restatement / new_snapshot 之一。"
                    "VERSION_SUPERSESSION 必须给 supersedes（被替代的旧 fact_id）。"
                    "DATA_CONFLICT 给了 resolved_by（CARD-13 已裁决）就不阻断，未裁决才阻断。"
                ),
            },
            "changed_node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选：直接给变更节点 id。未提供 event 时按 NEW_INFORMATION 处理。"
                    "与 event 二选一。"
                ),
            },
            "max_depth": {
                "type": "integer",
                "description": "受影响子图的限深 BFS 深度，默认 8。被截断会如实标注。",
            },
            "valuations": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "可选：**确定受影响的**估值 id（依赖映射属 CARD-08，本工具看不到）。"
                    "不给就报 null 而不是 0 —— 不把「没算」说成「没有」。"
                ),
            },
            "memo_sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：确定受影响的备忘段 id（依赖映射属 CARD-10）。同样不给就报 null。",
            },
            "ingested_at": {
                "type": "string",
                "description": "可选：入库时刻（ISO-8601）。省略则取文档记录里的入库时刻。",
            },
            "include_validation": {
                "type": "boolean",
                "description": "是否同时跑 CARD-25 校验器（默认 true）。",
            },
        },
        "required": ["document_id"],
    }

    def __init__(self, registry: Optional[DocumentRegistry] = None):
        self.registry = registry or DocumentRegistry()

    async def execute(
        self,
        document_id: str = "",
        event: Optional[Dict] = None,
        changed_node_ids: Optional[List[str]] = None,
        max_depth: int = 8,
        valuations: Optional[List[str]] = None,
        memo_sections: Optional[List[str]] = None,
        ingested_at: str = "",
        include_validation: bool = True,
    ) -> str:
        if not document_id:
            return tool_error(
                "必须提供 document_id：重验证要落在具体披露与图谱上。",
                error_code="temporal_missing_argument",
            )
        if not event and not changed_node_ids:
            return tool_error(
                "必须提供 event 或 changed_node_ids 之一：没有变更就无从重验证。",
                error_code="temporal_missing_argument",
            )

        try:
            graph, _analysis, document = _document_claim_graph(self.registry, document_id)
        except KeyError:
            return tool_error(
                f"未找到已注册文档: {document_id}",
                error_code="document_not_found", document_id=document_id,
            )
        except Exception as exc:
            return tool_error(
                f"图谱构建失败（{type(exc).__name__}: {exc}）",
                error_code="graph_build_failed", document_id=document_id,
            )

        normalization = normalize_financials(self.registry.get_facts(document_id), document)
        stamp = ingested_at or getattr(document, "ingested_at", "") or None
        try:
            lineage, lineage_warnings = build_lineage(
                normalization.facts,
                document_type=getattr(document, "document_type", None),
                ingested_at=stamp,
            )
        except TemporalError as exc:
            return tool_error(
                str(exc), error_code="temporal_invalid_lineage", document_id=document_id,
            )

        try:
            temporal_event = (
                _temporal_event_from_record(event)
                if event
                else _synthetic_change_event(changed_node_ids or ())
            )
        except TemporalError as exc:
            return tool_error(
                str(exc), error_code="temporal_invalid_event", document_id=document_id,
            )

        try:
            report = ingest_temporal_event(
                graph,
                temporal_event,
                lineage=lineage,
                max_depth=max_depth,
                valuations=valuations,
                memo_sections=memo_sections,
            )
        except TemporalError as exc:
            return tool_error(
                str(exc), error_code="temporal_revalidation_failed", document_id=document_id,
            )

        validation = (
            validate_temporal(report, lineage=lineage, graph=graph)
            if include_validation
            else None
        )

        payload: dict = {
            "status": report.status,
            "document_id": document_id,
            "filename": document.filename,
            "event_id": report.event_id,
            "event_kind": temporal_event.event_kind,
            "impact": dict(report.impact),
            "revalidation_report": report.to_dict(),
            "validation": validation.to_dict() if validation else None,
            "lineage_warnings": list(lineage_warnings),
            "engine": "src.cn.temporal（确定性增量重验证，零 LLM）",
            "note": (
                "受影响判定与状态传播全是确定性图算法，模型不参与「是否受影响」。"
                "增量只重算受影响子图，并与全量重算现场对账；不一致会回落全量并把 "
                "fallback 与 warning 一并写出。图外域（估值/备忘段）不猜：没给受影响清单就报 null。"
            ),
        }
        if report.status != STATUS_OK:
            payload["unavailable_reason"] = report.unavailable_reason
            payload["fallback"] = report.fallback
            payload["warnings"] = list(report.warnings)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _temporal_event_from_record(record: Dict) -> TemporalEvent:
    """把工具入参里的 dict 变成 :class:`TemporalEvent`（非法值由 dataclass 拒收）。"""
    if not isinstance(record, dict):
        raise TemporalError(f"event 必须是对象，收到 {type(record)!r}")
    kind = str(record.get("event_kind") or "")
    if kind not in CHANGE_KINDS:
        raise TemporalError(
            f"未知 event_kind: {kind!r}（合法值：{list(CHANGE_KINDS)}）"
        )
    event_type = str(record.get("event_type") or "")
    if event_type not in EVENT_TYPES:
        raise TemporalError(
            f"未知 event_type: {event_type!r}（合法值：{list(EVENT_TYPES)}）"
        )
    changed = record.get("changed_source_ids") or ()
    if isinstance(changed, str):
        changed = (changed,)
    return TemporalEvent(
        event_id=str(record.get("event_id") or ""),
        event_kind=kind,
        event_type=event_type,
        event_date=str(record.get("event_date") or ""),
        changed_source_ids=tuple(str(item) for item in changed),
        description=str(record.get("description") or ""),
        supersedes=record.get("supersedes"),
        resolved_by=record.get("resolved_by"),
    )


def _synthetic_change_event(changed_node_ids: Any) -> TemporalEvent:
    """未给 event 时，把 changed_node_ids 包成一个 NEW_INFORMATION 事件。

    event_id 由节点 id 摘要而来（确定性），不掺入墙钟。
    """
    nodes = tuple(sorted({str(item) for item in changed_node_ids}))
    digest = hashlib.sha256("|".join(nodes).encode("utf-8")).hexdigest()[:12]
    return TemporalEvent(
        event_id=f"EV-synth-{digest}",
        event_kind="NEW_INFORMATION",
        event_type="new_snapshot",
        event_date="",
        changed_source_ids=nodes,
        description="由 changed_node_ids 直接声明的变更（未提供显式事件）",
    )
