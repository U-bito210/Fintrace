"""CARD-15 §6 —— Demo 页面挂载入口。

    def mount_demo_pages(): ...   # 在现有入口追加挂载，不修改既有页面函数

页面清单来自 ``DEMO_FLOW.md`` v3.2（唯一演示剧本），**不是**卡 §5 那张更早的表。
剧本 §7 写着"任何对本剧本的修改必须同步更新 CARD-15 页面清单"，而 §5 停在
CARD-21 之前的时代 —— ACME / Financial Claim Passport / Thesis Fragility 三个
创新展示步骤（4b / 7b / 10b）所需的页面当时还不存在。本模块按剧本校正，
卡 §5 已同步。

每个页面只做渲染：数据一律经 ``src.cn.demo_service``，页面自己不算任何数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Tuple


@dataclass(frozen=True)
class DemoPage:
    """一个 Demo 页面：导航标签 + 渲染函数 + 它在剧本里的位置。"""

    label: str
    render: Callable[[], None]
    card: str
    step: str


def _specs() -> Tuple[Tuple[str, str, str, str], ...]:
    """``(模块名, 页面标签, 支撑卡片, 剧本步骤)``，顺序即导航顺序。"""
    return (
        ("competition", "竞赛主线", "05/12", "总览：材料→核查→证据→审计"),
        ("corrections", "研报纠错", "05", "步骤 6+7 ★高潮一"),
        ("acme", "ACME 约束", "22", "步骤 4b 创新一"),
        ("passport", "结论护照", "23", "步骤 7b ★核心动作"),
        ("graph", "主张-证据图谱", "09", "步骤 9+10 结论回溯"),
        ("fragility", "逻辑脆弱性", "24", "步骤 10b 创新三"),
        ("temporal", "时间重验证", "25", "失败注入 F9"),
        ("replay", "审计回放", "12", "收尾：时间线 + 导出"),
        ("adversarial", "对抗注入", "26", "失败注入 F1–F8"),
    )


def mount_demo_pages() -> List[DemoPage]:
    """构造页面注册表。导入页面模块在这里发生，入口只拿到成品列表。

    用一个函数而不是模块级常量：``workbench.py`` 调一次即可，重复调用不会产生
    副作用（各页面模块自带 Streamlit 缓存），测试也能安全地反复构建。
    """
    import importlib

    pages: List[DemoPage] = []
    for module_name, label, card, step in _specs():
        module = importlib.import_module(f"{__name__}.{module_name}")
        render = getattr(module, "render", None)
        if not callable(render):  # pragma: no cover - 结构性守卫
            raise RuntimeError(f"Demo 页面模块 {module_name} 没有可调用的 render()")
        pages.append(DemoPage(label=label, render=render, card=card, step=step))
    return pages


def page_labels() -> List[str]:
    """只要标签 —— 入口构造 NAVIGATION 时用。"""
    return [page.label for page in mount_demo_pages()]


__all__ = ["DemoPage", "mount_demo_pages", "page_labels"]
