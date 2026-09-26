"""CARD-14 · the migration that moved five hard-coded prompts into the registry
is *verbatim* — proven, not asserted.

``tests/fixtures/cn/prompt_golden_hashes.json`` was captured from the code as it
existed **before** CARD-14 (command and evidence chain in the card's §16). It
pins two independent things:

* ``prompts`` — the *template body* of every registered prompt. For a prompt with
  placeholders the ``{{name}}`` form is pinned; ``rendered_with`` records the
  arguments to render with.
* ``composites`` — the full text the **call sites** emit, i.e. base + run context
  (+ pre-flight), the memo narrator with a real payload, the daily-review polish
  messages. These are the root of trust: every fragment is derivable from them
  (the derivation is asserted), so a leaf can never drift alone.

A "behaviour-preserving refactor" claim that nothing checks is exactly the kind
of claim this repo refuses to make, so the same file also guards the two CARD-14
gates that are not about text: no prompt literal may come back into a ``.py``
file (§10), and a run that sends prompts must record their covering hash in its
manifest (§10, CARD-07 联动).
"""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "cn" / "prompt_golden_hashes.json"
FIXTURE_SNAPSHOT = ROOT / "tests" / "fixtures" / "cn" / "600519.SH_illustrative_v1.json"

CUTOFF = "2026-08-12T10:45:00+08:00"
RESOLVED_SYMBOL = "600519.SH"

#: A prompt sentence is long and specific; a match this long cannot be a coincidence.
MIN_HARDCODE_LINE = 24

#: The sentences that used to live in Python. Kept as a readable, explicit list so
#: a reviewer can see what the gate is protecting without reading the scanner.
SIGNATURE_PHRASES = (
    "You are an offline A-share research agent.",
    "你是研报草稿的「信息搬运工」",
    "你是买方研究员的写作助手",
    "你是A股复盘助手",
    "RUN CONTEXT: The fixed research cutoff is",
    "PRE-FLIGHT: deterministic code has already resolved the request",
)


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class _StubTools:
    """Records the messages it was handed; never reaches a network."""

    model_name = "stub-model"
    api_style = "openai"

    def __init__(self) -> None:
        self.seen: list = []

    async def call_with_tools(self, messages, tools, temperature=0.0, **kwargs):
        self.seen.append(messages)
        return SimpleNamespace(
            text="", has_tool_calls=False, raw={"role": "assistant", "content": ""},
            cost=0.0, tool_calls=[],
        )

    async def __call__(self, messages, temperature=0.0):
        self.seen.append(messages)
        return "", 0.0


def _ablation(variant: str, provider=None):
    from src.cn.ablation_agent import AblationAgent

    return AblationAgent(
        variant=variant, snapshot_path=FIXTURE_SNAPSHOT, model_name="stub-model",
        provider=provider or _StubTools(), research_as_of=CUTOFF,
    )


def _state():
    from src.cn.research import ResearchState

    return ResearchState.create(
        query="查询 600519.SH 的价格", symbol=RESOLVED_SYMBOL,
        research_as_of=CUTOFF, intent="offline_ablation",
    )


def _memo_variables() -> dict:
    return {
        "section_name": "investment_thesis",
        "whitelist": json.dumps(sorted(["C11", "C18"]), ensure_ascii=False),
        "analysis": json.dumps({"a": 1}, ensure_ascii=False, default=str),
        "valuation": json.dumps({"b": 2}, ensure_ascii=False, default=str),
    }


def _composites() -> dict:
    """Re-derive, from the *wired* code, the text the golden file captured."""
    from src.cn.ablation_agent import SYSTEM_PROMPT as ABLATION_BASE
    from src.cn.memo import _build_memo_prompt
    from src.cn.research_agent import SYSTEM_PROMPT as RESEARCH_BASE
    from src.cn.research_agent import CnResearchAgent

    direct = _ablation("direct_llm")
    asyncio.run(direct._run_direct_llm("查询 600519.SH 的价格", _state()))

    return {
        "cn_research_agent_base": RESEARCH_BASE,
        "_research_system_no_preflight": CnResearchAgent._system_prompt(CUTOFF),
        "_research_system_preflight": CnResearchAgent._system_prompt(CUTOFF, RESOLVED_SYMBOL),
        "cn_ablation_agent_base": ABLATION_BASE,
        "_ablation_system": _ablation("agent_tools")._system_prompt(),
        "_ablation_direct_llm_system": direct.provider.seen[0][0]["content"],
        "_memo_narrator_simple": _build_memo_prompt(
            "investment_thesis", {"a": 1}, {"b": 2}, ["C11", "C18"]
        ),
        "_memo_narrator_realistic": _build_memo_prompt(
            "financial_performance",
            {"metrics": [{"metric": "revenue", "value": 362012554.0, "unit": "千元", "period": "2024FY"}]},
            {"pe": None, "note": "估值未提供"},
            ["CLM-0001", "CLM-0002", "CLM-0007"],
        ),
    }


def _render(name: str, **variables):
    from src.agents.prompts import render

    return render(name, **variables)


# --------------------------------------------------------------------------- #
# The prompts
# --------------------------------------------------------------------------- #
def test_every_registered_prompt_matches_the_pre_migration_bytes():
    """The template body is byte-identical to the string that used to be in Python."""
    golden = _golden()
    from src.agents.prompts import resolve

    mismatched = []
    for name, row in golden["prompts"].items():
        spec = resolve(name)
        if _sha(spec.template) != row["sha256"]:
            mismatched.append(f"{name}: template drifted (len {len(spec.template)} vs {row['length']})")
        if list(spec.variables) != list(row["placeholders"]):
            mismatched.append(f"{name}: variables {spec.variables} != {row['placeholders']}")
        if sorted(row["rendered_with"]) != sorted(row["placeholders"]):
            mismatched.append(f"{name}: golden fixture does not render every placeholder")
    assert not mismatched, "\n".join(mismatched)


def test_the_golden_fixture_covers_every_registered_prompt():
    """Both directions: a prompt may not be added without pinning its bytes."""
    golden = _golden()
    from src.agents.prompts import default_registry

    assert set(golden["prompts"]) == set(default_registry())


def test_every_composite_is_still_emitted_verbatim():
    """The end-to-end text the call sites produce, compared as a whole."""
    golden = _golden()["composites"]
    emitted = _composites()
    for name, row in sorted(golden.items()):
        assert name in emitted, f"{name} is no longer emitted by any call site"
        assert _sha(emitted[name]) == row["sha256"], f"{name} changed by {len(emitted[name]) - row['length']} chars"


def test_the_fragments_still_reconstruct_the_composites():
    """Why the fixture can hold per-prompt entries at all: they are the differences."""
    from src.cn.ablation_agent import SYSTEM_PROMPT as ABLATION_BASE
    from src.cn.research_agent import SYSTEM_PROMPT as RESEARCH_BASE
    from src.cn.research_agent import CnResearchAgent

    no_preflight = CnResearchAgent._system_prompt(CUTOFF)
    with_preflight = CnResearchAgent._system_prompt(CUTOFF, RESOLVED_SYMBOL)
    assert no_preflight == f"{RESEARCH_BASE}\n\n" + _render("cn_research_agent_run_context", cutoff=CUTOFF)
    assert with_preflight == no_preflight + _render(
        "cn_research_agent_preflight", resolved_symbol=RESOLVED_SYMBOL
    )
    assert _render("cn_research_agent_preflight", resolved_symbol=RESOLVED_SYMBOL).startswith("\nPRE-FLIGHT: ")
    ablation_system = _ablation("agent_tools")._system_prompt()
    assert ablation_system == f"{ABLATION_BASE}\n\n" + _render(
        "cn_ablation_agent_run_context", cutoff=CUTOFF
    )
    assert _render("cn_ablation_agent_no_tools").startswith("You do NOT have any tools available.")


def test_each_pipeline_declares_the_prompts_it_sends_to_the_model():
    """A run's covering hash is only meaningful if the membership is declared once."""
    from src.agents.prompts import RUN_PROMPT_BUNDLES, resolve
    from src.cn.checker import extractor
    from src.cn.daily_review import collector
    from src.cn.research_agent import RUN_PROMPTS

    assert tuple(RUN_PROMPTS) == RUN_PROMPT_BUNDLES["research"]
    assert tuple(extractor.RUN_PROMPTS) == RUN_PROMPT_BUNDLES["report_checker"]
    assert tuple(collector.RUN_PROMPTS) == RUN_PROMPT_BUNDLES["daily_review"]
    for name in (*RUN_PROMPTS, *extractor.RUN_PROMPTS, *collector.RUN_PROMPTS):
        resolve(name)  # fail-closed: a name that is not registered cannot be recorded


def test_the_extractor_renders_its_instruction_from_the_registry():
    from src.cn.checker import extractor
    from src.cn.checker.sentences import DraftSentence

    sample = [
        DraftSentence(sentence_id="S1", document_id="DOC-X", text="营业收入为 100 亿元。", page=1, index=0),
        DraftSentence(sentence_id="S2", document_id="DOC-X", text="归母净利润为 10 亿元。", page=2, index=1),
    ]
    payload = json.dumps(
        [
            {"sentence_id": "S1", "page": 1, "text": "营业收入为 100 亿元。"},
            {"sentence_id": "S2", "page": 2, "text": "归母净利润为 10 亿元。"},
        ],
        ensure_ascii=False, indent=2,
    )
    assert extractor.SYSTEM_PROMPT == _render("cn_claim_extractor_system")
    assert extractor._render_batch(sample) == _render("cn_claim_extractor_user", payload=payload)


def test_the_memo_narrator_prompt_is_the_registered_template():
    from src.cn.memo import _build_memo_prompt

    variables = _memo_variables()
    assert _build_memo_prompt("investment_thesis", {"a": 1}, {"b": 2}, ["C11", "C18"]) == _render(
        "cn_memo_section_narrator", **variables
    )


def test_the_daily_review_polish_messages_are_the_registered_templates(monkeypatch):
    from src.cn.daily_review import collector

    recorded: list = []

    def fake_polish(messages, temperature=0.3):
        recorded.append(messages)
        return "记录用输出", 0.0

    monkeypatch.setattr(collector, "_get_llm_polish", lambda: fake_polish)
    collector._polish_theme_with_llm("测试题材", [{"标题": "甲", "摘要": "乙"}])
    assert recorded, "the polish step did not build any messages"
    system, user = recorded[0][0]["content"], recorded[0][1]["content"]
    assert system == _render("cn_daily_review_theme_polish_system")
    assert user == _render(
        "cn_daily_review_theme_polish_user", theme_name="测试题材", news_text="- 甲：乙"
    )
    # The model binding is declared on the template, so recording needs no caller input.
    from src.agents.prompts import usage_log

    assert {row["model"] for row in usage_log()} == {"deepseek-v4-flash"}


# --------------------------------------------------------------------------- #
# §10 gate 1 — no LLM prompt literal may live in Python
# --------------------------------------------------------------------------- #
#: This module has to *name* the sentences it forbids, so it is the one file
#: excluded from its own scan. Everywhere else — including the rest of ``tests/`` —
#: is fair game.
_SELF = Path(__file__).resolve()


def _python_sources() -> list:
    roots = [ROOT / "src", ROOT / "scripts", ROOT / "workbench", ROOT / "tests"]
    files = [path for root in roots for path in root.rglob("*.py")]
    files += [ROOT / name for name in ("api.py", "workbench.py", "conftest.py")]
    return [path for path in files if path.is_file() and path.resolve() != _SELF]


def test_no_prompt_text_is_hard_coded_in_python():
    """Every prompt line must live in the registry, not in a `.py` file.

    Both the template form and the rendered form are scanned: the first catches an
    exact copy of the registry's bytes, the second catches someone re-typing the
    prompt with concrete values (which is how it was hard-coded before CARD-14).
    """
    from src.agents.prompts import known_prompts

    # The registry package itself only *addresses* prompts; it must also be clean,
    # so nothing is excluded from the scan.
    sources = {path: path.read_text(encoding="utf-8") for path in _python_sources()}
    offenders = []
    for spec in known_prompts():
        forms = [spec.template]
        row = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))["prompts"].get(spec.key)
        if row:
            forms.append(spec.render(**row["rendered_with"]))
        for form in forms:
            for line in form.split("\n"):
                line = line.strip()
                if len(line) < MIN_HARDCODE_LINE:
                    continue
                for path, text in sources.items():
                    if line in text:
                        offenders.append(f"{spec.key} :: {path.relative_to(ROOT)} :: {line[:48]!r}")
    assert not offenders, "prompt text found in Python source:\n" + "\n".join(sorted(set(offenders)))


def test_the_signature_sentences_that_used_to_be_hard_coded_are_gone():
    sources = {path: path.read_text(encoding="utf-8") for path in _python_sources()}
    offenders = [
        f"{phrase!r} still in {path.relative_to(ROOT)}"
        for phrase in SIGNATURE_PHRASES
        for path, text in sources.items()
        if phrase in text
    ]
    assert not offenders, "\n".join(offenders)


# --------------------------------------------------------------------------- #
# §10 gate 2 — the hash reaches RunManifest
# --------------------------------------------------------------------------- #
def _start_run(tmp_path, **overrides):
    from src.cn.runs.models import RunConfig, RunInputDoc
    from src.cn.runs.recorder import RunRecorder, load_manifest

    doc = tmp_path / "input.txt"
    doc.write_text("recorded input", encoding="utf-8")
    store = tmp_path / "runs"
    fields = dict(
        input_documents=[RunInputDoc(path=str(doc))],
        parser_name="test-parser",
        parser_version="1",
        execution_mode="offline",
    )
    fields.update(overrides)
    run_id = RunRecorder(store_dir=store).start(RunConfig(**fields))
    return load_manifest(store, run_id)


def test_a_run_that_sends_prompts_records_their_covering_hash(tmp_path):
    from src.agents.prompts import RUN_PROMPT_BUNDLES, prompt_bundle_hash, usage_log
    from src.cn.research_agent import RUN_PROMPTS

    manifest = _start_run(
        tmp_path, model="stub-model", temperature=0.0, prompt_names=list(RUN_PROMPTS)
    )
    assert manifest.prompt_hash == prompt_bundle_hash(list(RUN_PROMPTS))
    assert manifest.prompt_hash  # never the empty string
    assert {row["prompt_name"] for row in usage_log()} == set(RUN_PROMPT_BUNDLES["research"])


def test_a_run_that_sends_no_prompt_records_no_hash(tmp_path):
    """`record_demo_run` is such a run: parse → normalize → compute → validate, no LLM."""
    manifest = _start_run(tmp_path, execution_mode="offline")
    assert manifest.prompt_hash is None


def test_an_explicitly_supplied_hash_wins_over_the_bundle(tmp_path):
    manifest = _start_run(
        tmp_path,
        prompt_hash="f" * 64,
        prompt_names=["cn_research_agent_base"],
    )
    assert manifest.prompt_hash == "f" * 64


def test_a_run_naming_an_unregistered_prompt_fails_closed(tmp_path):
    from src.agents.prompts import PromptNotFound

    with pytest.raises(PromptNotFound):
        _start_run(tmp_path, prompt_names=["cn_prompt_that_never_existed"])
