"""Versioned HTTP API for the local FinTrace-CN research workbench.

Run: .venv/Scripts/uvicorn api:app --reload
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.cn import workbench_service as service
from src.cn import document_service as documentservice
from src.cn import agent_service as agentservice
from src.cn.agent_service import AgentResearchError
from src.cn.errors import UnsupportedSymbolError


API_SCHEMA_VERSION = "workbench-api-1.0.0"
app = FastAPI(title="FinTrace-CN Workbench API", version="1.0.0")


class ResearchRequest(BaseModel):
    symbol: str = Field(description="Canonical A-share symbol, for example 600519.SH.")


class AgentResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=2000, description="Natural-language research question.")
    symbol: str = Field(description="Canonical A-share symbol, for example 600519.SH.")
    snapshot_id: str = Field(description="Versioned snapshot to run the Agent against.")
    research_as_of: str | None = Field(default=None, description="Complete ISO-8601 point-in-time cutoff.")
    model_name: str = Field(description="Registered model to invoke; must already be configured.")
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_steps: int = Field(default=6, ge=1, le=10)
    required_metrics: list[str] = Field(default_factory=list, description="Metrics the Validator must see.")


class SnapshotAcquisitionRequest(ResearchRequest):
    start_date: str = Field(pattern=r"^\d{8}$", description="Inclusive YYYYMMDD date.")
    end_date: str = Field(pattern=r"^\d{8}$", description="Inclusive YYYYMMDD date.")


class ApiError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ApiMeta(BaseModel):
    snapshot_id: str | None = None
    research_as_of: str | None = None
    provider: str | None = None
    currency: str | None = None
    unit: Any = None
    period_basis: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    validation_status: str | None = None
    data_coverage: Any = None
    freshness: Any = None


class ApiEnvelope(BaseModel):
    schema_version: str
    status: Literal["ok", "error"]
    data: Any = None
    meta: ApiMeta
    error: ApiError | None = None


@app.get("/health")
def health() -> dict[str, str]:
    """Unauthenticated liveness probe; never reads providers or credentials."""
    return {"status": "ok", "schema_version": API_SCHEMA_VERSION}


def _meta(**overrides: Any) -> dict[str, Any]:
    base = {
        "snapshot_id": None, "research_as_of": None, "provider": None, "currency": None,
        "unit": None, "period_basis": None, "evidence_ids": [],
        "validation_status": None, "data_coverage": None, "freshness": None,
    }
    base.update(overrides)
    return base


def _ok(data: Any, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema_version": API_SCHEMA_VERSION, "status": "ok", "data": data, "meta": meta or _meta(), "error": None}


def _error(code: str, message: str, *, retryable: bool = False, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "schema_version": API_SCHEMA_VERSION, "status": "error", "data": None, "meta": _meta(),
            "error": {"code": code, "message": message, "retryable": retryable},
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    return _error(detail.get("code", "HTTP_ERROR"), detail.get("message", "Request failed."), status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return _error("INVALID_REQUEST", "Request fields or parameters are invalid.", status_code=422)


def _research_meta(snapshot_id: str, data: dict[str, Any], *, unit: Any = None, period_basis: str | None = None) -> dict[str, Any]:
    payload = service.load_snapshot(snapshot_id)
    summary = service.research_summary(snapshot_id)
    validation = service.research_validation(snapshot_id)
    readiness = service.research_readiness(snapshot_id)
    evidence = data.get("evidence_ids", summary.get("evidence_ids", {}))
    if isinstance(evidence, dict):
        evidence = [value for value in evidence.values() if value]
    return _meta(
        snapshot_id=snapshot_id, research_as_of=payload["research_as_of"],
        provider=payload.get("provider", "snapshot"),
        currency=payload.get("data", {}).get("profile", {}).get("currency", "CNY"),
        unit=unit if unit is not None else data.get("unit"),
        period_basis=period_basis if period_basis is not None else data.get("period_basis"),
        evidence_ids=evidence, validation_status=validation["status"],
        data_coverage=readiness["checks"], freshness=service.snapshot_freshness(snapshot_id),
    )


def _research_result(fn: Callable[..., dict[str, Any]], snapshot_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        data = fn(snapshot_id, *args, **kwargs)
        return _ok(data, meta=_research_meta(snapshot_id, data))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "SNAPSHOT_NOT_FOUND", "message": "Research snapshot was not found."}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_REQUEST", "message": str(exc)}) from exc


def _public_task(data: dict[str, Any]) -> dict[str, Any]:
    """Remove server-local implementation details from browser-facing task data."""
    return {key: value for key, value in data.items()
            if key not in {"report_path", "metadata_path", "snapshot_path", "probe_dir"}}


def _task_result(data: dict[str, Any]) -> dict[str, Any]:
    """Keep task responses in the same envelope without leaking server paths."""
    data = _public_task(data)
    snapshot_id = data.get("snapshot_id")
    if snapshot_id:
        return _ok(data, meta=_research_meta(snapshot_id, {}))
    return _ok(data)


@app.get("/api/research")
def list_research() -> ApiEnvelope:
    return _ok({"items": service.list_research()})


@app.get("/api/evaluations")
def list_evaluations() -> ApiEnvelope:
    return _ok({"items": service.list_evaluations()})


@app.get("/api/evaluations/{evaluation_id:path}")
def evaluation_detail(evaluation_id: str) -> ApiEnvelope:
    try:
        return _ok(service.evaluation_detail(evaluation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "EVALUATION_NOT_FOUND", "message": "Evaluation artifact was not found."}) from exc


@app.post("/api/research", status_code=202)
def start_research(request: ResearchRequest) -> ApiEnvelope:
    try:
        return _task_result(service.start_research(request.symbol))
    except (ValueError, UnsupportedSymbolError) as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_SYMBOL", "message": str(exc)}) from exc


@app.get("/api/tasks")
def list_tasks() -> ApiEnvelope:
    return _ok({"items": [_public_task(task) for task in service.list_tasks()]})


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> ApiEnvelope:
    try:
        return _task_result(service.get_task(task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Research task was not found."}) from exc


@app.get("/api/tasks/{task_id}/progress")
def task_progress(task_id: str) -> ApiEnvelope:
    try:
        progress = service.get_task_progress(task_id)
        task = service.get_task(task_id)
        return _task_result({**progress, "snapshot_id": task.get("snapshot_id")})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Research task was not found."}) from exc


@app.post("/api/tasks/{task_id}/rerun", status_code=202)
def rerun_task(task_id: str) -> ApiEnvelope:
    try:
        return _task_result(service.rerun_task(task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Research task was not found."}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "TASK_RERUN_NOT_ALLOWED", "message": str(exc)}) from exc


@app.post("/api/tasks/{task_id}/cancel", status_code=202)
def cancel_task(task_id: str) -> ApiEnvelope:
    try:
        return _task_result(service.cancel_task(task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Research task was not found."}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "TASK_CANCEL_NOT_ALLOWED", "message": str(exc)}) from exc


@app.get("/api/tasks/{task_id}/diagnostics")
def task_diagnostics(task_id: str) -> ApiEnvelope:
    try:
        return _task_result(service.task_diagnostics(task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Research task was not found."}) from exc


# ---------------------------------------------------------------------------
# AI Agent Research: real LLM tool-calling agent on the shared CnResearchAgent
# ---------------------------------------------------------------------------
def _agent_error(exc: Exception) -> HTTPException:
    """Map application errors to typed API errors without leaking internals."""
    if isinstance(exc, KeyError):
        if "task_id" in str(exc):
            return HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Agent research task was not found."})
        return HTTPException(status_code=404, detail={"code": "UNKNOWN_SNAPSHOT", "message": "Research snapshot was not found."})
    if isinstance(exc, AgentResearchError):
        return HTTPException(status_code=422, detail={"code": "AGENT_RESEARCH_INVALID", "message": str(exc)})
    return HTTPException(status_code=500, detail={"code": "AGENT_RESEARCH_ERROR", "message": "Agent research failed; see server logs."})


@app.post("/api/agent/research", status_code=202)
def start_agent_research(request: AgentResearchRequest) -> ApiEnvelope:
    """Queue a real-model agent-research task on the shared CnResearchAgent."""
    try:
        task = agentservice.start_agent_research(
            agentservice.AgentResearchRequest(
                query=request.query,
                symbol=request.symbol,
                snapshot_id=request.snapshot_id,
                research_as_of=request.research_as_of,
                model_name=request.model_name,
                temperature=request.temperature,
                max_steps=request.max_steps,
                required_metrics=tuple(request.required_metrics or []),
            )
        )
    except (AgentResearchError, KeyError) as exc:
        raise _agent_error(exc) from exc
    return _ok(
        {"task_id": task["id"], "task_status": task["status"], "kind": task["kind"]},
        meta=_research_meta(task["snapshot_id"], {}),
    )


@app.get("/api/agent/research/{task_id}")
def agent_research_detail(task_id: str) -> ApiEnvelope:
    """Return the browser-safe public agent task (answer, state, validation, usage)."""
    try:
        data = agentservice.get_agent_task(task_id)
    except KeyError as exc:
        raise _agent_error(exc) from exc
    if data.get("snapshot_id"):
        return _ok(data, meta=_research_meta(data["snapshot_id"], {}))
    return _ok(data)


@app.get("/api/agent/research/{task_id}/trace")
def agent_research_trace(task_id: str) -> ApiEnvelope:
    """Return the credential-free event stream and tool trace for an agent task."""
    try:
        return _ok(agentservice.agent_task_events(task_id))
    except KeyError as exc:
        raise _agent_error(exc) from exc


@app.post("/api/snapshots/acquire", status_code=202)
def acquire_snapshot(request: SnapshotAcquisitionRequest) -> ApiEnvelope:
    try:
        return _ok(service.collect_snapshot(request.symbol, start_date=request.start_date, end_date=request.end_date))
    except (ValueError, UnsupportedSymbolError) as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_ACQUISITION_REQUEST", "message": str(exc)}) from exc


# ---------------------------------------------------------------------------
# Online snapshot refresh: pull the latest A-share close into a NEW snapshot.
#
# Versioned local snapshots remain the research input; this endpoint only adds
# one.  Existing snapshot files are never overwritten, the browser never reaches
# Tushare, and a failure is reported as a structured error -- never as stale
# data masquerading as a successful update.
# ---------------------------------------------------------------------------
class SnapshotRefreshRequest(BaseModel):
    symbol: str = Field(description="Canonical A-share symbol, for example 600519.SH.")
    bars_only: bool = Field(
        default=True,
        description="只刷新行情（RAW 日线收盘）。False 时同时重新拉取已披露财务数据。",
    )
    research_as_of: str | None = Field(
        default=None, description="可选 ISO-8601 研究截止时间；缺省为当前时间（Asia/Shanghai）。"
    )


#: reason_code -> HTTP status.  Request-shape problems stay 422; credential and
#: upstream problems are distinguished so a client can decide whether to retry.
_REFRESH_REASON_HTTP: dict[str, int] = {
    "credentials_missing": 503,
    "credentials_invalid": 503,
    "call_budget_exhausted": 503,
    "rate_limited": 429,
    "provider_timeout": 502,
    "network_error": 502,
    "provider_error": 502,
    "provider_no_data": 502,
    "no_bars_at_or_before_cutoff": 502,
    "base_snapshot_missing": 409,
    "base_snapshot_not_provider_backed": 409,
    "unsupported_symbol": 422,
}


def _refresh_meta(outcome: dict[str, Any]) -> dict[str, Any]:
    """Point meta at the new snapshot, or at the one still in use on failure."""
    current = outcome.get("current_snapshot") or {}
    new_id = outcome.get("snapshot_id")
    if new_id:
        return _meta(
            snapshot_id=new_id, research_as_of=outcome.get("research_as_of"),
            provider=outcome.get("provider"), freshness=outcome.get("freshness"),
        )
    return _meta(
        snapshot_id=current.get("snapshot_id"), research_as_of=current.get("research_as_of"),
        provider=current.get("provider") or outcome.get("provider"),
        freshness=outcome.get("freshness"),
    )


def _refresh_error(outcome: dict[str, Any]) -> JSONResponse:
    reason = str(outcome.get("reason_code") or "refresh_failed")
    return JSONResponse(
        status_code=_REFRESH_REASON_HTTP.get(reason, 502),
        content={
            "schema_version": API_SCHEMA_VERSION,
            "status": "error",
            "data": outcome,
            "meta": _refresh_meta(outcome),
            "error": {
                "code": reason.upper(),
                "message": outcome.get("message", "Snapshot refresh failed."),
                "retryable": bool(outcome.get("retryable")),
            },
        },
    )


@app.post("/api/research/snapshots/refresh")
def refresh_research_snapshot(request: SnapshotRefreshRequest) -> ApiEnvelope:
    """Pull the latest available A-share close and create a new snapshot version.

    Success returns an ``ok`` envelope whose ``meta`` points at the *new*
    snapshot.  A failure returns a structured ``error`` envelope that still
    carries, in ``meta`` and ``data.current_snapshot``, the cutoff of the
    snapshot the workbench keeps using.
    """
    try:
        outcome = service.refresh_snapshot(
            request.symbol, bars_only=request.bars_only, research_as_of=request.research_as_of,
        )
    except (ValueError, UnsupportedSymbolError) as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_REFRESH_REQUEST", "message": str(exc)}) from exc

    if outcome.get("refresh_status") == "succeeded":
        return _ok(outcome, meta=_research_meta(outcome["snapshot_id"], {}))
    if outcome.get("refresh_status") == "no_new_data":
        # Not an error: the local snapshot already holds the latest close.
        return _ok(outcome, meta=_refresh_meta(outcome))
    return _refresh_error(outcome)


@app.get("/api/research/{snapshot_id}/summary")
def summary(snapshot_id: str) -> ApiEnvelope:
    return _research_result(service.research_summary, snapshot_id)


@app.get("/api/research/{snapshot_id}/market")
def market(snapshot_id: str, adjustment: str = Query("RAW", pattern="^(RAW|QFQ|HFQ)$")) -> ApiEnvelope:
    return _research_result(service.research_market, snapshot_id, adjustment)


@app.get("/api/research/{snapshot_id}/financials")
def financials(snapshot_id: str, period_basis: str | None = Query(None, pattern="^(FY|Q1|H1|9M|TTM)$")) -> ApiEnvelope:
    fn = service.research_financial_trends if period_basis else service.research_financials
    return _research_result(fn, snapshot_id, *([period_basis] if period_basis else []))


@app.get("/api/research/{snapshot_id}/evidence")
def evidence(snapshot_id: str) -> ApiEnvelope:
    return _research_result(service.research_evidence, snapshot_id)


@app.get("/api/research/{snapshot_id}/valuation")
def valuation(snapshot_id: str) -> ApiEnvelope:
    return _research_result(service.research_valuation, snapshot_id)


@app.get("/api/research/{snapshot_id}/validation")
def validation(snapshot_id: str) -> ApiEnvelope:
    return _research_result(service.research_validation, snapshot_id)


@app.get("/api/research/{snapshot_id}/trace")
def trace(snapshot_id: str) -> ApiEnvelope:
    return _research_result(service.research_trace, snapshot_id)


@app.get("/api/research/{snapshot_id}/report")
def report(snapshot_id: str) -> ApiEnvelope:
    return _research_result(service.research_report, snapshot_id)


@app.get("/api/research/{snapshot_id}/artifacts")
def artifacts(snapshot_id: str) -> ApiEnvelope:
    return _research_result(service.research_artifacts, snapshot_id)


# ---------------------------------------------------------------------------
# Daily market review (新增；复用同一 ApiEnvelope 契约)
# ---------------------------------------------------------------------------
class DailyReviewRequest(BaseModel):
    review_date: str = Field(pattern=r"^\d{8}$", description="Inclusive YYYYMMDD date.")


def _daily_review_meta(snapshot_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    from src.cn.daily_review.provider import DailyReviewSnapshotProvider
    from src.cn.daily_review.gate import review_gate

    payload = service.load_market_review(snapshot_id)
    provider = DailyReviewSnapshotProvider.from_payload(payload)
    gate = review_gate(provider.snapshot)
    evidence = (data or {}).get("evidence_ids") or []
    return _meta(
        snapshot_id=snapshot_id, research_as_of=payload.get("research_as_of"),
        provider=payload.get("provider"), currency="CNY",
        evidence_ids=evidence, validation_status=gate.status,
        data_coverage={"synthetic_demo": provider.is_synthetic_demo()},
        freshness={"research_as_of": payload.get("research_as_of")},
    )


def _daily_review_result(fn: Callable[..., dict[str, Any]], snapshot_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        data = fn(snapshot_id, *args, **kwargs)
        return _ok(data, meta=_daily_review_meta(snapshot_id, data))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "MARKET_REVIEW_NOT_FOUND", "message": "Market review snapshot was not found."}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_REQUEST", "message": str(exc)}) from exc


@app.get("/api/daily-review")
def list_daily_reviews() -> ApiEnvelope:
    return _ok({"items": service.list_daily_reviews()})


@app.get("/api/daily-review/{snapshot_id}/summary")
def daily_review_summary(snapshot_id: str) -> ApiEnvelope:
    return _daily_review_result(service.daily_review_summary, snapshot_id)


@app.get("/api/daily-review/{snapshot_id}/panorama")
def daily_review_panorama(snapshot_id: str) -> ApiEnvelope:
    return _daily_review_result(service.daily_review_detail, snapshot_id, "panorama")


@app.get("/api/daily-review/{snapshot_id}/hotspots")
def daily_review_hotspots(snapshot_id: str) -> ApiEnvelope:
    return _daily_review_result(service.daily_review_detail, snapshot_id, "hotspots")


@app.get("/api/daily-review/{snapshot_id}/report", response_class=HTMLResponse)
def daily_review_report(snapshot_id: str) -> str:
    """Serve the standalone, CDN-free HTML market-review report for a snapshot.

    Renders whatever snapshot is selected (offline demo OR a freshly acquired
    live one) -- never fabricates.  Returns 404 if the snapshot is missing.
    """
    from src.cn.daily_review.report import render_report_html

    try:
        payload = service.load_market_review(snapshot_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "MARKET_REVIEW_NOT_FOUND", "message": "Market review snapshot was not found."},
        ) from exc
    return render_report_html(payload)


@app.post("/api/daily-review/acquire", status_code=202)
def acquire_daily_review(request: DailyReviewRequest) -> ApiEnvelope:
    try:
        return _ok(service.start_daily_review(request.review_date))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_REQUEST", "message": str(exc)}) from exc


# ---------------------------------------------------------------------------
# Document Intelligence — read side of CARD-01's 报告数字 → 原始 PDF 页
#
# Responses carry no server filesystem path: a document is addressed by
# ``document_id``, and its bytes are streamed through ``/source`` or rendered
# through ``/pages/{page}.png``. That is what makes the reverse jump clickable
# without exposing where the workspace keeps the file.
# ---------------------------------------------------------------------------
def _document_error(exc: Exception) -> HTTPException:
    if isinstance(exc, documentservice.FactNotFound):
        return HTTPException(status_code=404, detail={"code": "FACT_NOT_FOUND", "message": "Document fact was not found."})
    if isinstance(exc, documentservice.DocumentNotFound):
        return HTTPException(status_code=404, detail={"code": "DOCUMENT_NOT_FOUND", "message": "Registered document was not found."})
    return HTTPException(status_code=422, detail={"code": "DOCUMENT_NOT_LOCATABLE", "message": str(exc)})


def _document_meta(**overrides: Any) -> dict[str, Any]:
    return _meta(provider="document_registry", **overrides)


@app.get("/api/documents")
def list_documents(
    document_type: str | None = Query(None, description="annual|interim|quarterly|announcement|report_draft|other"),
    company: str | None = Query(None),
) -> ApiEnvelope:
    data = documentservice.list_documents(document_type=document_type, company=company)
    return _ok(data, meta=_document_meta())


@app.get("/api/documents/{document_id}")
def document_detail(document_id: str) -> ApiEnvelope:
    try:
        return _ok(documentservice.document_detail(document_id), meta=_document_meta())
    except KeyError as exc:
        raise _document_error(exc) from exc


@app.get("/api/documents/{document_id}/facts")
def document_facts(
    document_id: str,
    page: int | None = Query(None, ge=1),
    numeric_only: bool = Query(True, description="只返回可点击的数值单元格；行标签与表头仍参与标签重建。"),
) -> ApiEnvelope:
    try:
        data = documentservice.document_facts(document_id, page=page, numeric_only=numeric_only)
    except KeyError as exc:
        raise _document_error(exc) from exc
    return _ok(data, meta=_document_meta(unit=data["facts"][0]["unit"] if data["facts"] else None))


@app.get("/api/documents/{document_id}/facts/{fact_id}/jump")
def document_fact_jump(document_id: str, fact_id: str) -> ApiEnvelope:
    """Reverse jump: one disclosed number → its source page and bbox."""
    try:
        data = documentservice.jump_target(document_id, fact_id)
    except (KeyError, ValueError) as exc:
        raise _document_error(exc) from exc
    return _ok(data, meta=_document_meta(unit=data["unit"], currency=data["currency"]))


@app.get("/api/documents/{document_id}/analysis")
def document_analysis(document_id: str) -> ApiEnvelope:
    """Return the deterministic CARD-02/03/04 analysis for one document.

    This deliberately delegates to ``document_service`` so the API, Agent tool,
    and Streamlit workbench expose the same normalized facts and calculations.
    The endpoint never accepts caller-supplied numbers and never computes in the
    HTTP layer.
    """
    try:
        data = documentservice.document_analysis(document_id)
    except KeyError as exc:
        raise _document_error(exc) from exc
    return _ok(
        data,
        meta=_document_meta(
            currency=data.get("currency"),
            unit=data.get("unit"),
            validation_status=("error" if data.get("validation_errors") else "ok"),
            data_coverage={
                "data_complete": data.get("data_complete"),
                "metric_count": data.get("metric_count", 0),
                "missing_count": len(data.get("missing", [])),
            },
        ),
    )


@app.get("/api/documents/{document_id}/source")
def document_source(document_id: str) -> Response:
    """Stream the registered file for the embedded preview; never a filesystem path."""
    try:
        payload, media_type = documentservice.document_source(document_id)
    except (KeyError, ValueError) as exc:
        raise _document_error(exc) from exc
    return Response(content=payload, media_type=media_type)


@app.get("/api/documents/{document_id}/pages/{page}.png")
def document_page_image(
    document_id: str,
    page: int,
    fact_id: list[str] = Query(default_factory=list, description="要高亮 bbox 的事实 ID，可重复。"),
) -> Response:
    """Render one page as PNG with the given facts' bboxes outlined."""
    highlights = []
    try:
        for identifier in fact_id:
            target = documentservice.jump_target(document_id, identifier)
            if target["bbox"]:
                highlights.append(target["bbox"])
        payload = documentservice.page_image(document_id, page, highlights=highlights)
    except (KeyError, ValueError) as exc:
        raise _document_error(exc) from exc
    return Response(content=payload, media_type="image/png")
