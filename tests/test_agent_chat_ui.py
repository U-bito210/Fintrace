"""Regression coverage for the chat-first Agent entry point."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.cn import workbench_service as service


WORKBENCH = Path(__file__).resolve().parents[1] / "workbench.py"


@pytest.mark.parametrize("prompt", ["你好啊", "你有什么功能"])
def test_onboarding_prompts_stay_in_the_chat_ui_without_starting_an_evidence_task(prompt):
    """Greetings and product-help questions are not financial-research trajectories."""
    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.session_state["workbench_page"] = "AI Agent 研究"
    at.run()

    at.chat_input[0].set_value(prompt).run()

    rendered = " ".join(str(element.value) for element in at.markdown)
    assert "A 股研究助手" in rendered
    assert "财务表现分析" in rendered
    assert "agent_task_id" not in at.session_state


def test_switching_snapshot_syncs_the_research_cutoff():
    """The date control must not retain the previous snapshot's cutoff."""
    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.session_state["workbench_page"] = "AI Agent 研究"
    at.run()

    picker = next(element for element in at.selectbox if element.key == "agent_snapshot_choice")
    assert len(picker.options) > 1
    replacement = next(option for option in picker.options if option != picker.value)
    picker.set_value(replacement).run()

    selected = next(element for element in at.selectbox if element.key == "agent_snapshot_choice")
    catalog = {f"{item['name']} · {item['symbol']}": item for item in service.list_research()}
    assert at.session_state["agent_cutoff"] == catalog[selected.value]["research_as_of"][:19]
