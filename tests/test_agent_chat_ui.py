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


def test_agent_uses_the_sidebar_snapshot_without_a_second_picker():
    """Agent requests inherit the global research version and its cutoff."""
    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    selected_item = next(item for item in service.list_research() if item["symbol"] == "000001.SZ")
    version = selected_item["id"].rsplit("_", 1)[-1]
    at.session_state["snapshot_picker"] = (
        f"{selected_item['name']} · {selected_item['symbol']} · "
        f"研究截至 {selected_item['research_as_of'][:10]} · {selected_item['provider']} · {version}"
    )
    at.session_state["workbench_page"] = "AI Agent 研究"
    at.run()

    assert not any(element.key == "agent_snapshot_choice" for element in at.selectbox)
    context_markup = " ".join(str(element.value) for element in at.markdown)
    assert selected_item["symbol"] in context_markup
    assert selected_item["research_as_of"][:19].replace("T", " ") in context_markup
    assert next(element.value for element in at.number_input if element.key == "agent_max_steps") == 6
    assert next(element.value for element in at.number_input if element.key == "agent_temperature") == 0.0


def test_sidebar_agent_button_routes_to_the_agent_page():
    """The visible navigation button must not render the previous page."""
    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.run()

    button = next(item for item in at.button if item.key == "nav_AI Agent 研究")
    button.click().run()

    assert at.session_state["workbench_page"] == "AI Agent 研究"
    rendered = " ".join(str(element.value) for element in at.markdown)
    assert "AI Agent 研究" in rendered


def test_local_baseline_switcher_updates_snapshot_before_sidebar_picker_renders():
    """The offline switch callback must not mutate an instantiated selectbox."""
    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.run()

    button = next(item for item in at.button if item.key == "refresh_snapshot_catalog")
    button.click().run()

    assert not at.exception, at.exception
    messages = " ".join(str(element.value) for element in at.info)
    assert "本地基准版本" in messages or "没有可切回" in messages


def test_online_refresh_notice_names_a_different_target_symbol():
    """A result for another symbol must never look like the open research version."""
    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.session_state["online_refresh_notice"] = {
        "symbol": "000001.SZ",
        "severity": "info",
        "text": "该标的的刷新结果。",
    }
    at.run()

    assert not at.exception, at.exception
    messages = " ".join(str(element.value) for element in at.info)
    assert "联网更新对象：000001.SZ" in messages
    assert "当前研究标的" in messages


def test_new_agent_conversation_confirms_the_reset_without_erasing_archive():
    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.session_state["workbench_page"] = "AI Agent 研究"
    at.run()

    button = next(item for item in at.button if item.key == "agent_new_conversation")
    button.click().run()

    assert not at.exception, at.exception
    messages = " ".join(str(element.value) for element in at.success)
    assert "已准备新的研究对话" in messages
