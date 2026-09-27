"""Provider-agnostic, observable A-share research agent.

Shared by the product path (FastAPI / Streamlit workbench) and the real-model
benchmark so both exercise the *same* tool-selection and execution logic.
Extracted from the offline benchmark runner; ``OfflineCnAgent`` now exists as a
thin, backward-compatible adapter in ``offline_agent.py``.

The agent executes one real model-and-tool trajectory against one pinned
snapshot.  It exposes only the snapshot-backed ``cn_tools`` so a run measures
the model's actual tool selection and arguments.  It reports progress through
an optional event callback and cooperatively stops when a cancellation check
returns ``True`` between steps.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

from src.agents.prompts import RUN_PROMPT_BUNDLES, record_usage, render
from src.agents.tools.base import ToolRegistry
from src.agents.tools.cn_tools import build_cn_snapshot_tools
from src.cn.providers.snapshot import SnapshotProvider
from src.cn.research import ResearchState
from src.validation.research_gate import (
    ResearchGateRequirements,
    ResearchGateValidator,
    SnapshotEvidenceLedger,
    snapshot_evidence_records,
)


#: CARD-14 §10: the text lives in ``src/agents/prompts/templates/``, not here.
#: The module-level name is kept because callers import it and the real-model
#: benchmark fingerprints it — only its *source* moved.
SYSTEM_PROMPT = render("cn_research_agent_base")

#: The prompts one research trajectory sends to the model. CARD-07's manifest
#: records their covering hash, so a run states exactly which prompt bytes it used.
RUN_PROMPTS = RUN_PROMPT_BUNDLES["research"]

#: How many earlier turns a follow-up run replays to the model. Bounded so a long
#: session cannot grow the prompt — and the bill — without limit. Older turns are
#: dropped oldest-first; the run still records that it did so.
MAX_FOLLOWUP_HISTORY = 3


class ToolCallingProvider(Protocol):
    model_name: str
    api_style: str

    async def call_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]],
                              temperature: float = 0.0, **kwargs: Any) -> Any: ...


# Events mirror the product API so the task layer can persist a deterministic,
# credential-free trail that describes the run without leaking prompts or paths.
AGENT_EVENTS = (
    "agent_started", "llm_call_started", "llm_call_completed",
    "tool_call_started", "tool_call_completed", "evidence_registered",
    "validation_started", "validation_passed", "validation_blocked",
    "task_completed", "task_cancelled", "task_failed",
)


class CnResearchAgent:
    """Execute one observable ReAct trajectory over one pinned snapshot."""

    def __init__(self, *, snapshot_path: Path | str, model_name: str,
                 temperature: float = 0.0, max_steps: int = 6,
                 provider: ToolCallingProvider | None = None,
                 required_metrics: Sequence[str] = (),
                 requires_evidence: Optional[bool] = None,
                 requested_valuation_method: Optional[str] = None) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.snapshot = SnapshotProvider(self.snapshot_path)
        self.model_name = model_name
        self.temperature = temperature
        self.max_steps = max_steps
        gate_requires_evidence = True if requires_evidence is None else requires_evidence
        self.gate_requirements = ResearchGateRequirements(
            required_metrics=tuple(str(metric).lower() for metric in required_metrics),
            requires_evidence=gate_requires_evidence,
            requested_valuation_method=requested_valuation_method,
        )
        if provider is None:
            # The application imports its LLM package from ``src``. Keep that
            # optional dependency out of fixture-only test collection.
            from src.llms.async_client import AsyncLLMProvider
            provider = AsyncLLMProvider(model_name)
        self.provider = provider
        self.registry = ToolRegistry().register_all(build_cn_snapshot_tools(self.snapshot_path))
        self.answer = ""
        self.total_cost = 0.0
        self.llm_calls = 0

    async def run(
        self,
        query: str,
        *,
        research_as_of: Optional[str] = None,
        event_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        history: Sequence[Mapping[str, Any]] = (),
    ) -> ResearchState:
        """Run the loop. Returns the ``ResearchState`` and leaves ``answer``,
        ``total_cost`` and ``llm_calls`` on the instance for callers to read.

        ``history`` replays earlier turns of the same conversation, oldest first.
        It is *reference context only*: it is never registered as evidence, so a
        follow-up answer that merely repeats last turn's figures still fails the
        Validator with ``no_evidence``. That is the point — a follow-up must
        re-derive what it asserts.
        """
        symbol = str(self.snapshot._symbol)
        cutoff = research_as_of or str(self.snapshot._payload["research_as_of"])
        state = ResearchState.create(
            query=query, symbol=symbol, research_as_of=cutoff, intent="agent_research",
        )
        self._emit(event_handler, "agent_started",
                   detail=f"model={self.model_name} cutoff={cutoff}")

        # Symbol parsing is a deterministic boundary, not an LLM judgement.
        resolved_symbol = await self._resolve_symbol_preflight(
            query, state, event_handler=event_handler, cancellation_check=cancellation_check,
        )
        if cancellation_check and cancellation_check():
            return self._cancel(state, event_handler)

        prior_turns = [dict(turn) for turn in (history or ())][-MAX_FOLLOWUP_HISTORY:]

        is_openai_style = getattr(self.provider, "api_style", "openai") == "openai"
        tool_defs = self.registry.openai_defs() if is_openai_style else self.registry.anthropic_defs()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(
                cutoff, resolved_symbol, has_history=bool(prior_turns))},
        ]
        # Earlier turns go in as plain text, never as tool results: replaying a
        # tool payload would let the model treat a previous run's numbers as if
        # it had just fetched them.
        messages.extend(_history_messages(prior_turns))
        messages.append({"role": "user", "content": query})
        # CARD-14: declare the prompt versions this trajectory used so the run can
        # be pointed back at exact prompt bytes. Auditing only — it never edits a
        # prompt, and it records the pre-flight note only when it was really sent.
        for prompt_name in RUN_PROMPTS:
            if prompt_name == "cn_research_agent_preflight" and not resolved_symbol:
                continue
            if prompt_name in ("cn_research_agent_followup", "cn_research_agent_history_turn") and not prior_turns:
                continue
            record_usage(prompt_name, self.model_name, self.temperature)

        for step in range(self.max_steps):
            if cancellation_check and cancellation_check():
                return self._cancel(state, event_handler)
            self.llm_calls += 1
            self._emit(event_handler, "llm_call_started", detail=f"step={step + 1}")
            response = await self.provider.call_with_tools(
                messages, tool_defs, temperature=self.temperature,
            )
            self.total_cost += float(getattr(response, "cost", 0.0) or 0.0)
            self._emit(event_handler, "llm_call_completed",
                       detail=f"step={step + 1} cost={self.total_cost:.6f}")
            if not response.has_tool_calls:
                self.answer = str(response.text or "")
                state.report = self.answer
                break
            messages.append(response.raw)
            anthropic_results: List[Dict[str, Any]] = []
            for call in response.tool_calls:
                started_at = datetime.now(timezone.utc).isoformat()
                self._emit(event_handler, "tool_call_started",
                           meta={"tool_name": call.name, "step": step + 1})
                if call.parse_error:
                    result_json = json.dumps(
                        {"status": "error", "error": call.parse_error}, ensure_ascii=False,
                    )
                else:
                    result_json = await self.registry.execute(call.name, call.arguments)
                payload = _decode_result(result_json)
                evidence_ids = _evidence_ids(payload)
                state.register_evidence(evidence_ids)
                state.record_tool(
                    tool_name=call.name, arguments=call.arguments, started_at=started_at,
                    result_status=str(payload.get("status", "error")), provider="snapshot",
                    evidence_ids=evidence_ids,
                )
                self._emit(
                    event_handler, "tool_call_completed",
                    detail=f"{call.name} returned {len(evidence_ids)} evidence IDs",
                    meta={"tool_name": call.name, "step": step + 1,
                          "evidence_count": len(evidence_ids)},
                )
                if evidence_ids:
                    self._emit(
                        event_handler, "evidence_registered",
                        detail=f"{len(evidence_ids)} IDs",
                        meta={"tool_name": call.name, "step": step + 1,
                              "evidence_count": len(evidence_ids)},
                    )
                validation = payload.get("validation")
                if isinstance(validation, dict):
                    state.finalize_validation(validation)
                if is_openai_style:
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result_json})
                else:
                    anthropic_results.append({"type": "tool_result", "tool_use_id": call.id, "content": result_json})
            if not is_openai_style and anthropic_results:
                messages.append({"role": "user", "content": anthropic_results})
        else:
            # The model kept requesting tools up to max_steps; force a final turn
            # with no tools so we always return a visible answer.
            if cancellation_check and cancellation_check():
                return self._cancel(state, event_handler)
            self.llm_calls += 1
            self._emit(event_handler, "llm_call_started", detail="final")
            response = await self.provider.call_with_tools(messages, [], temperature=self.temperature)
            self.total_cost += float(getattr(response, "cost", 0.0) or 0.0)
            self._emit(event_handler, "llm_call_completed",
                       detail=f"final cost={self.total_cost:.6f}")
            self.answer = str(response.text or "")
            state.report = self.answer

        self._apply_validator_gate(state, event_handler=event_handler)
        state.report = self.answer
        state.cost_trace.append({
            "model": self.model_name,
            "cost_usd": self.total_cost,
            "llm_calls": self.llm_calls,
            "steps": len(state.tool_trace),
        })
        valid = bool((state.validation_result or {}).get("valid"))
        self._emit(event_handler, "task_completed",
                   detail=f"steps={len(state.tool_trace)} cost={self.total_cost:.6f}",
                   meta={"status": "succeeded" if valid else "blocked"})
        return state

    @staticmethod
    def _emit(event_handler: Optional[Callable[[Dict[str, Any]], None]],
              event: str, *, status: str = "ok", detail: str = "",
              meta: Optional[Dict[str, Any]] = None) -> None:
        if event_handler:
            event_handler({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event, "status": status, "detail": detail,
                "metadata": meta or {},
            })

    def _cancel(self, state: ResearchState, event_handler) -> ResearchState:
        self._emit(event_handler, "task_cancelled", status="cancelled", detail="cooperative stop")
        self.answer = "研究任务已取消。"
        state.report = self.answer
        state.cost_trace.append({
            "model": self.model_name, "cost_usd": self.total_cost,
            "llm_calls": self.llm_calls, "steps": len(state.tool_trace),
        })
        state.finalize_validation({
            "valid": False, "errors": ["cancelled"], "missing_evidence": state.missing_evidence,
        })
        return state

    @staticmethod
    def _system_prompt(cutoff: str, resolved_symbol: Optional[str] = None,
                       *, has_history: bool = False) -> str:
        """Compose the system prompt from registered fragments (CARD-14).

        The pieces are separate prompts on purpose: the base never changes, the
        run context binds the cutoff, the pre-flight note appears only when
        deterministic code already resolved the symbol, and the follow-up note
        appears only on a continuation turn. Concatenation is assembly, not
        prompt content, so it stays in Python.
        """
        preflight = ""
        if resolved_symbol:
            preflight = render("cn_research_agent_preflight", resolved_symbol=resolved_symbol)
        followup = ""
        if has_history:
            followup = render("cn_research_agent_followup")
        context = render("cn_research_agent_run_context", cutoff=cutoff)
        return f"{SYSTEM_PROMPT}\n\n{context}{preflight}{followup}"

    async def _resolve_symbol_preflight(
        self, query: str, state: ResearchState,
        *, event_handler=None, cancellation_check=None,
    ) -> Optional[str]:
        """Record deterministic canonical-code parsing when the query names this snapshot."""
        symbol = str(self.snapshot._symbol)
        if not re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", query.upper()):
            return None
        if cancellation_check and cancellation_check():
            return None
        started_at = datetime.now(timezone.utc).isoformat()
        self._emit(event_handler, "tool_call_started", meta={"tool_name": "resolve_cn_symbol", "step": 0})
        result_json = await self.registry.execute("resolve_cn_symbol", {"query": symbol})
        payload = _decode_result(result_json)
        state.record_tool(
            tool_name="resolve_cn_symbol", arguments={"query": symbol}, started_at=started_at,
            result_status=str(payload.get("status", "error")), provider="snapshot",
            evidence_ids=_evidence_ids(payload),
        )
        self._emit(event_handler, "tool_call_completed", meta={"tool_name": "resolve_cn_symbol", "step": 0})
        return symbol if payload.get("status") == "ok" else None

    def _apply_validator_gate(self, state: ResearchState, *, event_handler=None) -> None:
        """Block deterministic prose unless captured execution is admissible."""
        self._emit(event_handler, "validation_started")
        records = snapshot_evidence_records(self.snapshot._payload, state=state)
        result = ResearchGateValidator(max_data_age_days=365).validate(
            SnapshotEvidenceLedger(records), state=state,
            snapshot_payload=self.snapshot._payload,
            research_as_of=state.research_as_of,
            requirements=self.gate_requirements,
        )
        if result.valid:
            state.finalize_validation({
                "valid": True, "errors": [], "missing_evidence": state.missing_evidence,
            })
            self._emit(event_handler, "validation_passed")
            return

        self.answer = (
            "无法验证/拒绝结论：Validator 检测到执行或数据问题。\n"
            f"错误详情：{', '.join(result.errors)}\n"
            "请补齐有效工具结果、所需指标和证据，并确认截止时间与行业估值口径。"
        )
        state.finalize_validation({
            "valid": False, "errors": result.errors,
            "missing_evidence": state.missing_evidence,
        })
        self._emit(event_handler, "validation_blocked", status="blocked",
                   detail=", ".join(result.errors))


def _history_messages(turns: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Turn earlier conversation turns into replay messages.

    Each turn becomes a ``user`` (the question as asked) / ``assistant`` (the
    prior answer, wrapped in the registered unsourced-notice) pair so anaphora
    such as "那它的净利率呢" still resolves. Nothing here is registered as
    evidence — see :meth:`CnResearchAgent.run`.
    """
    messages: List[Dict[str, Any]] = []
    for turn in turns:
        question = str(turn.get("query") or "").strip()
        answer = str(turn.get("answer") or "").strip()
        if not question and not answer:
            continue
        if question:
            messages.append({"role": "user", "content": question})
        if answer:
            messages.append({
                "role": "assistant",
                "content": render("cn_research_agent_history_turn", answer=answer),
            })
    return messages


def _decode_result(result_json: str) -> Dict[str, Any]:
    try:
        value = json.loads(result_json)
    except json.JSONDecodeError:
        return {"status": "error", "error": "Tool returned invalid JSON."}
    return value if isinstance(value, dict) else {"status": "error", "error": "Tool returned non-object JSON."}


def _evidence_ids(payload: Dict[str, Any]) -> List[str]:
    ids = payload.get("evidence_ids", [])
    if not isinstance(ids, list):
        ids = []
    nested = payload.get("research_state")
    if isinstance(nested, dict):
        ids = [*ids, *(nested.get("facts") or []), *(nested.get("calculations") or [])]
    return [str(item) for item in ids]