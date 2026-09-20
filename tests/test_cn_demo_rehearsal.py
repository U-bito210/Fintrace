"""剧本自检脚本（`scripts/rehearse_demo.py`）的自检。

这里防的是**漂移**：`DEMO_FLOW.md` 改了步骤、脚本没跟着改，彩排时才发现少查了一步。

刻意**不**断言每一步是 ready 还是 blocked —— 那会随材料到位而变化，而且会逼人为了
让测试变绿去伪造材料（本项目明令禁止）。只断言三件不会变的事：
步骤覆盖完整、状态只有那四种且每步都有话说、退出码语义正确。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rehearse_demo.py"
SPEC = importlib.util.spec_from_file_location("rehearse_demo", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
# dataclasses 解析注解时要按 `cls.__module__` 回查 sys.modules；不先登记会在
# exec_module 阶段炸 AttributeError。
sys.modules.setdefault("rehearse_demo", MODULE)
SPEC.loader.exec_module(MODULE)

# DEMO_FLOW.md §2 主故事 12 步 + §3 失败注入 F9 / F1–F8，按演示顺序。
SCRIPTED_STEPS = (
    "1+2", "3+4", "4b", "5", "6+7", "7b", "8", "9+10", "10b", "11", "收尾", "F9", "F1–F8",
)

# §2 的 8 处注入清单编号。
PLANTED_CODES = ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8")


@pytest.fixture(scope="module")
def results():
    return MODULE.run()


def test_every_scripted_step_is_covered():
    """剧本文档里的每一步都有对应自检——顺序也要一致（就是演示顺序）。"""
    assert tuple(step.step_id for step in MODULE.STEPS) == SCRIPTED_STEPS


def test_innovation_steps_are_flagged():
    """§4 要求四项创新全部可演示，它们必须在步骤表里能被认出来。"""
    step_ids = {step.step_id for step in MODULE.STEPS}
    for step_id in MODULE.INNOVATION_STEPS:
        assert step_id in step_ids, f"创新展示步骤 {step_id} 不在步骤表里"


def test_every_step_reports_a_known_state_and_says_something(results):
    """每一步都必须落到一个明确状态，且**有话说**——静默比报错更糟。"""
    known = (MODULE.READY, MODULE.DEGRADED, MODULE.BLOCKED, MODULE.MANUAL)
    assert len(results) == len(SCRIPTED_STEPS)
    for step, outcome in results:
        assert outcome.state in known, f"步骤 {step.step_id} 状态越界：{outcome.state}"
        assert outcome.lines, f"步骤 {step.step_id} 没有任何输出行"


def test_manual_steps_are_marked_as_unchecked(results, capsys):
    """MANUAL 是『本脚本查不了』，不是『查过没问题』——报告里必须写明白。"""
    MODULE.report(results)
    out = capsys.readouterr().out
    if any(outcome.state == MODULE.MANUAL for _, outcome in results):
        assert "需人工走查" in out
        assert "没查" in out or "查不了" in out


def test_strict_exit_code_matches_whether_anything_is_blocked(results):
    """`--strict` 的退出码必须严格等价于「有步骤会卡」。"""
    expected = 1 if any(o.state == MODULE.BLOCKED for _, o in results) else 0
    assert MODULE.report(results) == expected


def test_non_strict_run_never_fails_the_command():
    """默认只是打印报告，不该把 CI 跑挂——挂不挂由 `--strict` 决定。"""
    assert MODULE.main([]) == 0


def test_unknown_step_is_rejected_not_silently_ignored(capsys):
    """传错步骤名要报错退出，不能什么都不查还返回 0。"""
    assert MODULE.main(["--step", "nope"]) == 2
    assert "没有步骤" in capsys.readouterr().out


def test_planted_error_plan_matches_the_script():
    """§2 的 E1–E8 清单：编号齐全，且硬门槛就是 8 处。"""
    assert tuple(plan[0] for plan in MODULE.PLANTED_ERROR_PLAN) == PLANTED_CODES
    assert MODULE.REQUIRED_PLANTED == len(PLANTED_CODES) == 8


def test_e1_e2_carry_the_scripted_examples():
    """E2 在 v3.1 改成量级错误（不是单位错误）——别退回旧设计。"""
    by_code = {plan[0]: plan for plan in MODULE.PLANTED_ERROR_PLAN}
    assert "scale" in by_code["E2"][1], "E2 必须是量级错误"
    assert "31,000" in by_code["E2"][2] and "3.1" in by_code["E2"][2]
