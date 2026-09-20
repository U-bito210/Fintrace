"""CARD-26 §15 — 把 FinFuzz 结果发布成 CARD-11 的**组件基线**。

CARD-11 的 synthetic 档聚合每个组件各自发布的基线文件
（``output/benchmark/cn_agent_v2/<component>_baseline.json``）。FinFuzz 在这个意义上是
一个组件：它用自己的语料量自己的四项指标，聚合器**不需要知道任何变异算子**就能读它。

两条规矩塑造了本模块：

* **同一指标只有一处定义。** 数值来自
  :func:`src.cn.benchmark_v2.adversarial_metrics` —— CARD-11 自己的计算器，而不是这里
  再写一遍。组件用自己的算术重新发布一个已有指标，正是 CARD-11 的归并闸门要拦的事
  （同一指标两条定义即报错）。所以本模块只做**搬运与声明**，不做计算。
* **文件名是契约的一部分。** ``COMPONENT`` / ``BASELINE_FILENAME`` 由
  ``scripts/run_benchmark_v2.py`` 的注册表导入，改名不会静默把基线变成孤儿。

一个刻意的取舍：**基线里不放 per-error-type recall**。聚合器只认四项指标名，逐算子明细
已经写在 recall 那一行的 ``note`` 里（``numeric=16/16 …``）—— 两处都放就会出现两份可能
互相矛盾的逐算子数字。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping

from .models import NOT_APPLICABLE, FinFuzzReport

#: CARD-11 ``COMPONENT_BASELINES`` 注册表里的组件键。
COMPONENT = "card26_finfuzz"
#: CARD-11 聚合器在组件目录下寻找的文件名。
BASELINE_FILENAME = "finfuzz_baseline.json"
#: 报告体系标识（与 CARD-11 的 ``build_report`` 一致）。
SUITE = "cn_agent_v2"
SET_NAME = "synthetic"

#: 随基线一起落盘的边界声明。**每次都要写**：这四个数字来自合成对抗语料，
#: 把它当成"真实研报上的现场检测率"是最容易犯也最严重的误读。
DISCLAIMER = (
    "合成对抗语料上的变异检测率；不是真实研报上的现场准确率。"
    "holdout 子集未采集，holdout 指标恒为 TARGET / TO BE MEASURED（卡 §12.4）。"
)


def _is_not_applicable(row: Mapping[str, Any]) -> bool:
    """A per-error-type row whose recall was never measurable.

    The sentinel is a **string** (``"N/A"``) in the serialised row, not ``None`` —
    checking only for ``None`` misses every one of them and makes
    ``operators_not_applicable`` silently come back empty while the report it sits
    next to lists two such operators.
    """
    recall = row.get("recall")
    return recall is None or str(recall) == NOT_APPLICABLE


def baseline_metrics(report: FinFuzzReport) -> Dict[str, Dict[str, Any]]:
    """四项对抗指标，由 CARD-11 的 calculator 算出后原样搬运。"""
    from src.cn.benchmark_v2 import adversarial_metrics

    return {
        name: metric.to_dict()
        for name, metric in adversarial_metrics(report.adversarial_rows()).items()
    }


def baseline_payload(report: FinFuzzReport) -> Dict[str, Any]:
    """``finfuzz_baseline.json`` 的内容 —— CARD-11 原生读法（``metrics`` 映射）的形状。"""
    return {
        "suite": SUITE,
        "set": SET_NAME,
        "component": COMPONENT,
        "synthetic": bool(report.synthetic),
        "real_model": bool(report.real_model),
        "disclaimer": DISCLAIMER,
        # 样本数而非"文档数"：这个组件的计量单位就是一次变异，卡 §16 的 ablation
        # 也是按样本数对比覆盖率。与 ``report.counts`` 对不上时以 rows 为准。
        "case_count": len(report.rows),
        "operators_run": list(report.operators_run),
        "operators_not_applicable": sorted(
            name for name, row in report.per_error_type.items() if _is_not_applicable(row)
        ),
        "seed": report.seed,
        "metrics": baseline_metrics(report),
    }


def format_markdown(report: FinFuzzReport) -> str:
    """人读版：边界声明 + 四项指标 + 逐算子召回 + 未覆盖的算子。"""
    payload = baseline_payload(report)
    lines = [
        f"# FinFuzz 组件基线 — {COMPONENT}",
        "",
        f"> {DISCLAIMER}",
        "",
        "## 四项指标（CARD-11 定义）",
        "",
        "| metric | value | unit | note |",
        "|---|---|---|---|",
    ]
    for name, entry in payload["metrics"].items():
        value = entry.get("value")
        shown = NOT_APPLICABLE if value is None else f"{float(value):.4f}"
        lines.append(f"| {name} | {shown} | {entry.get('unit', '')} | {entry.get('note', '')} |")

    lines += ["", "## 逐算子召回", "", "| operator | recall | detected / n | equivalent |", "|---|---|---|---|"]
    for name, row in report.per_error_type.items():
        recall = row.get("recall")
        shown = NOT_APPLICABLE if _is_not_applicable(row) else f"{float(recall):.4f}"
        lines.append(
            f"| {name} | {shown} | {row.get('detected', 0)} / {row.get('n', 0)} | "
            f"{row.get('equivalent_n', 0)} |"
        )

    na = payload["operators_not_applicable"]
    if na:
        lines += [
            "",
            "## 未获得样本的算子（N/A，不是 0 召回）",
            "",
            *[f"- `{name}`：现行语料上没有可变异的目标声明（`no_spec_generated`）" for name in na],
        ]
    lines += [
        "",
        f"seed={payload['seed']} ｜ specs={payload['case_count']} ｜ "
        f"operators_run={','.join(payload['operators_run'])}",
        "",
    ]
    return "\n".join(lines)


def write_baseline(report: FinFuzzReport, out_dir: str | Path) -> Dict[str, str]:
    """把组件基线写进 CARD-11 的组件目录（``<out_dir>/finfuzz_baseline.*``）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / BASELINE_FILENAME
    md_path = out / BASELINE_FILENAME.replace(".json", ".md")
    json_path.write_text(
        json.dumps(baseline_payload(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(format_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


__all__ = [
    "BASELINE_FILENAME",
    "COMPONENT",
    "DISCLAIMER",
    "SET_NAME",
    "SUITE",
    "baseline_metrics",
    "baseline_payload",
    "format_markdown",
    "write_baseline",
]
