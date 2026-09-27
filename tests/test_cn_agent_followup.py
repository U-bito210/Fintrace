"""Multi-turn follow-up for the agent research conversation.

The property these tests exist to protect: a follow-up is *not* a shortcut
around the evidence gate. Earlier turns are replayed so the model can resolve
"它" / "那家公司", and nothing else — every figure a follow-up asserts still has
to come from a tool call made in that turn.

Service-level tests are offline (executor stubbed, no network). Agent-level
tests use the scripted fake provider from the shared research-agent suite.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.cn import agent_service, workbench_service
from src.cn.agent_service import AgentResearchError, AgentResearchRequest
from src.cn.demo_data import ensure_demo_snapshots
from src.cn.research_agent import MAX_FOLLOWUP_HISTORY, CnResearchAgent, _history_messages


_PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEMO_SNAPSHOT = "600519.SH_illustrative_demo_v1"
OTHER_SNAPSHOT = "000858.SZ_illustrative_demo_v1"
MODEL = "deepseek-v4-flash"

FIXTURE = Path(__file__).parent / "fixtures" / "cn" / "600519.SH_illustrative_v1.json"


class _StubExecutor:
    """Capture submitted work and never execute it (offline, no network)."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args))
        return None

    def shutdown(self, *args, **kwargs):
        return None


@pytest.fixture()
def isolated(monkeypatch):
    base = _PROJECT_ROOT / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="fu", dir=base))
    snapshot_dir = root / "snapshots"
    task_dir = root / "tasks"
    ensure_demo_snapshots(snapshot_dir)
    monkeypatch.setattr(workbench_service, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(workbench_service, "TASK_DIR", task_dir)
    monkeypatch.setattr(workbench_service, "TASK_EXECUTOR", _StubExecutor())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    yield
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    shutil.rmtree(root, ignore_errors=True)


def _request(**overrides) -> AgentResearchRequest:
    params = dict(
        query="分析贵州茅台的收入和净利润，并引用证据。",
        symbol="600519.SH",
        snapshot_id=DEMO_SNAPSHOT,
        research_as_of="2025-04-30T23:00:00+08:00",
        model_name=MODEL,
    )
    params.update(overrides)
    return AgentResearchRequest(**params)


def _finish(task_id: str, *, answer: str = "上一轮的结论。", valid: bool = True,
            facts=("fact-1",), tool_calls: int = 1) -> dict:
    """Persist a terminal task the way the worker would."""
    task = workbench_service.get_task(task_id)
    task["status"] = "succeeded" if valid else "blocked"
    task["answer"] = answer
    task["validation"] = {"valid": valid, "errors": [] if valid else ["no_evidence"]}
    task["research_state"] = {
        "facts": list(facts), "calculations": [],
        "tool_trace": [{"tool_name": "get_cn_financials"}] * tool_calls,
    }
    workbench_service._save_task(task)
    return task


# ---------------------------------------------------------------------------
# Service: linking, validation, depth
# ---------------------------------------------------------------------------
def test_a_question_without_a_parent_starts_turn_zero(isolated):
    task = agent_service.start_agent_research(_request())
    assert task["parent_task_id"] is None
    assert task["turn_index"] == 0


def test_a_followup_links_its_parent_and_is_numbered(isolated):
    root = agent_service.start_agent_research(_request())
    _finish(root["id"])
    followup = agent_service.start_agent_research(
        _request(query="那它的净利率是多少？", parent_task_id=root["id"]))
    assert followup["parent_task_id"] == root["id"]
    assert followup["turn_index"] == 1


def test_the_thread_reads_root_first_and_reports_each_turns_own_evidence(isolated):
    root = agent_service.start_agent_research(_request())
    _finish(root["id"], facts=("fact-1", "fact-2"), tool_calls=3)
    followup = agent_service.start_agent_research(
        _request(query="那它的净利率是多少？", parent_task_id=root["id"]))

    thread = agent_service.agent_conversation_thread(followup["id"])
    assert [turn["turn_index"] for turn in thread] == [0, 1]
    assert [turn["task_id"] for turn in thread] == [root["id"], followup["id"]]
    assert thread[0]["evidence_count"] == 2
    assert thread[0]["tool_calls"] == 3
    assert thread[0]["validation_valid"] is True
    assert thread[1]["evidence_count"] == 0  # not run yet


def test_a_followup_may_not_switch_snapshots(isolated):
    root = agent_service.start_agent_research(_request())
    _finish(root["id"])
    with pytest.raises(AgentResearchError) as exc:
        agent_service.start_agent_research(
            _request(query="换个快照继续问。", snapshot_id=OTHER_SNAPSHOT,
                     symbol="000858.SZ", parent_task_id=root["id"]))
    assert "同一份快照" in str(exc.value)


def test_a_followup_on_an_unknown_parent_is_rejected(isolated):
    with pytest.raises(AgentResearchError) as exc:
        agent_service.start_agent_research(_request(parent_task_id="no-such-task"))
    assert "Unknown parent task" in str(exc.value)


def test_a_followup_may_not_continue_a_non_agent_task(isolated):
    other = workbench_service._new_task("600519.SH", "offline_research")
    workbench_service._save_task(other)
    with pytest.raises(AgentResearchError) as exc:
        agent_service.start_agent_research(_request(parent_task_id=other["id"]))
    assert "agent_research" in str(exc.value)


def test_the_conversation_has_a_bounded_depth(isolated):
    parent_id = None
    for turn in range(agent_service.MAX_FOLLOWUP_DEPTH):
        task = agent_service.start_agent_research(
            _request(query=f"第 {turn} 轮的问题。", parent_task_id=parent_id))
        _finish(task["id"])
        parent_id = task["id"]
    with pytest.raises(AgentResearchError) as exc:
        agent_service.start_agent_research(
            _request(query="再来一轮。", parent_task_id=parent_id))
    assert "上限" in str(exc.value)


def test_the_parent_chain_survives_a_cycle(isolated):
    """A hand-edited parent pointer must not hang the walker."""
    first = agent_service.start_agent_research(_request())
    _finish(first["id"])
    second = agent_service.start_agent_research(
        _request(query="追问一次。", parent_task_id=first["id"]))
    task = workbench_service.get_task(first["id"])
    task["parent_task_id"] = second["id"]  # corrupt on purpose
    workbench_service._save_task(task)

    thread = agent_service.agent_conversation_thread(second["id"])
    assert len(thread) <= agent_service.MAX_FOLLOWUP_DEPTH + 1


# ---------------------------------------------------------------------------
# History: what gets replayed
# ---------------------------------------------------------------------------
def test_only_validated_turns_are_replayed(isolated):
    root = agent_service.start_agent_research(_request())
    _finish(root["id"], valid=False, answer="被阻断的结论，不应被继承。")
    followup = agent_service.start_agent_research(
        _request(query="继续。", parent_task_id=root["id"]))

    assert agent_service._followup_history(followup) == []


def test_a_first_question_replays_nothing(isolated):
    task = agent_service.start_agent_research(_request())
    assert agent_service._followup_history(task) == []


def test_replayed_messages_keep_the_question_and_mark_the_answer_unsourced():
    messages = _history_messages([
        {"query": "贵州茅台的净利润是多少？", "answer": "净利润 100 亿元。"},
    ])
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "贵州茅台的净利润是多少？"
    assert messages[1]["role"] == "assistant"
    assert "净利润 100 亿元。" in messages[1]["content"]
    assert "UNSOURCED IN THIS TURN" in messages[1]["content"]


def test_an_empty_turn_is_not_replayed():
    assert _history_messages([{"query": "", "answer": ""}]) == []


def test_the_replay_window_is_bounded():
    turns = [{"query": f"问题 {i}", "answer": f"答案 {i}"} for i in range(MAX_FOLLOWUP_HISTORY + 3)]
    history = turns[-MAX_FOLLOWUP_HISTORY:]
    messages = _history_messages(history)
    assert len(messages) == MAX_FOLLOWUP_HISTORY * 2
    assert f"问题 {MAX_FOLLOWUP_HISTORY}" in messages[0]["content"]


# ---------------------------------------------------------------------------
# Agent: the evidence gate still applies to a follow-up
# ---------------------------------------------------------------------------
class _ScriptedProvider:
    model_name = "gpt-4o-mini"
    api_style = "openai"

    def __init__(self, responses):
        self.responses = list(responses)
        self.messages_seen = []

    async def call_with_tools(self, messages, tools, temperature=0.0, **kwargs):
        self.messages_seen.append(messages)
        return self.responses.pop(0)


def _final(text="已完成。", cost=0.01):
    return SimpleNamespace(
        has_tool_calls=False, text=text, raw={"role": "assistant", "content": text},
        tool_calls=None, cost=cost,
    )


@pytest.mark.asyncio
async def test_a_followup_that_repeats_last_turn_without_refetching_is_blocked():
    """The whole point: inheriting an answer is not sourcing an answer."""
    provider = _ScriptedProvider([_final("还是 100 亿元。")])
    agent = CnResearchAgent(snapshot_path=FIXTURE, model_name="gpt-4o-mini",
                            provider=provider, max_steps=4)
    state = await agent.run(
        "那它的净利润呢？",
        history=[{"query": "贵州茅台的净利润是多少？", "answer": "净利润 100 亿元。"}],
    )
    assert state.facts == []
    assert state.calculations == []
    assert not state.tool_trace
    assert state.validation_result["valid"] is False
    assert "no_evidence" in state.validation_result["errors"]


@pytest.mark.asyncio
async def test_history_reaches_the_model_as_plain_text_before_the_new_question():
    provider = _ScriptedProvider([_final()])
    agent = CnResearchAgent(snapshot_path=FIXTURE, model_name="gpt-4o-mini",
                            provider=provider, max_steps=4)
    await agent.run("那净利率呢？", history=[{"query": "净利润多少？", "answer": "100 亿元。"}])

    sent = provider.messages_seen[0]
    roles = [message["role"] for message in sent]
    assert roles == ["system", "user", "assistant", "user"]
    assert sent[-1]["content"] == "那净利率呢？"
    assert "FOLLOW-UP TURN" in sent[0]["content"]


@pytest.mark.asyncio
async def test_a_first_question_gets_no_followup_note():
    provider = _ScriptedProvider([_final()])
    agent = CnResearchAgent(snapshot_path=FIXTURE, model_name="gpt-4o-mini",
                            provider=provider, max_steps=4)
    await agent.run("净利润多少？")

    sent = provider.messages_seen[0]
    assert [message["role"] for message in sent] == ["system", "user"]
    assert "FOLLOW-UP TURN" not in sent[0]["content"]
