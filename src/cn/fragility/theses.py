"""CARD-24 — Thesis 从哪来：两条**确定性**来源，都不许凭空造。

卡 §9 把"把研报叙事解析成 Thesis"划给了别处（那是 LLM 的活），但一张图里
如果**根本没有** Thesis，脆弱性引擎就没有分析对象。实测（开工前的可行性核查）
发现：全仓库只有 CARD-09 的测试手工构造过 ``Thesis``，真实管线里 ``theses`` 恒为空
—— 所以本模块必须回答"theses 是谁放进去的"，而且答案必须站得住：

* :func:`theses_from_declaration` —— **调用方声明**。声明里每个 claim_id 都必须在
  图上真的存在，否则抛错；声明不出来的东西不被猜出来。
* :func:`theses_from_inferences` —— **图自己的推论**。CARD-03 的诊断信号进图时
  就是 inference 且带 ``derived_from``（"这条结论是从哪几条指标推出来的"），
  这本来就是"投资逻辑 + 它的依赖"的现成结构，把它提升为 Thesis 是**重新贴标签**，
  不是新造判断。

:func:`resolve_theses` 是给工具用的选择器，并且**永远报告它选了哪一条**
（``THESIS_SOURCE_*``）—— "分析对象是哪来的"不能是隐含信息。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.cn.graph import (
    CLAIM_INFERENCE,
    ROLE_REQUIRED,
    ClaimGraph,
    Thesis,
)

from .models import (
    THESIS_SOURCE_DECLARED,
    THESIS_SOURCE_INFERENCES,
    THESIS_SOURCE_NONE,
    FragilityError,
)

#: 推论式 Thesis 的 id 前缀：一眼能看出它不是人工声明的。
INFERRED_THESIS_PREFIX = "thesis_inference_"


def theses_from_declaration(
    graph: ClaimGraph, specs: Sequence[Mapping[str, Any]]
) -> List[Thesis]:
    """把声明式规格变成 :class:`Thesis`，并逐条校验引用.

    校验（都抛 :class:`FragilityError`，不静默丢弃）：

    * ``thesis_id`` / ``statement`` / ``claim_ids`` 均不可为空；
    * 每个 ``claim_id`` 必须已注册在图上 —— 引用一条不存在的结论，等于宣称
      一条自己都点不出来的逻辑；
    * ``dependency_roles`` 的角色值必须合法（由 ``Thesis.__post_init__`` 兜底）。
    """
    out: List[Thesis] = []
    seen: set = set()
    for index, spec in enumerate(specs):
        thesis_id = str(spec.get("thesis_id") or "").strip()
        statement = str(spec.get("statement") or "").strip()
        claim_ids = [str(item) for item in (spec.get("claim_ids") or ())]
        if not thesis_id:
            raise FragilityError(f"第 {index + 1} 条声明缺少 thesis_id。")
        if thesis_id in seen:
            raise FragilityError(f"thesis_id 重复: {thesis_id}")
        if not statement:
            raise FragilityError(f"{thesis_id} 缺少 statement：没有陈述的 Thesis 不是逻辑。")
        if not claim_ids:
            raise FragilityError(f"{thesis_id} 没有 claim_ids：空 Thesis 无从分析。")
        missing = [cid for cid in claim_ids if cid not in graph.claims]
        if missing:
            raise FragilityError(
                f"{thesis_id} 引用了图上不存在的 claim: {', '.join(sorted(missing))}"
            )
        roles = {
            str(key): str(value)
            for key, value in dict(spec.get("dependency_roles") or {}).items()
        }
        unknown = sorted(set(roles) - set(claim_ids))
        if unknown:
            raise FragilityError(
                f"{thesis_id} 的 dependency_roles 含未声明的 claim: {', '.join(unknown)}"
            )
        out.append(
            Thesis(
                thesis_id=thesis_id,
                statement=statement,
                claim_ids=tuple(dict.fromkeys(claim_ids)),
                dependency_roles=roles,
            )
        )
        seen.add(thesis_id)
    return out


def theses_from_inferences(graph: ClaimGraph) -> List[Thesis]:
    """把图中每条 inference claim 提升为一条 Thesis（确定性，无模型）.

    结构：``claim_ids = (该 inference,)`` 且角色 REQUIRED —— 于是针对这条 Thesis
    的割集会**自动**沿它的 REQUIRED 边下钻到那些指标事实与证据，不需要在这里
    重复描述一遍依赖。id 用前缀 + claim_id，来源可回查。

    没有 ``derived_from`` 的 inference 也照样成图（它会被判为已 BLOCKED），
    只是如实报出来 —— "这条逻辑压根没接上来源"本身就是要给人看的信息。
    """
    out: List[Thesis] = []
    for claim in sorted(graph.claims.values(), key=lambda item: item.claim_id):
        if claim.claim_type != CLAIM_INFERENCE:
            continue
        out.append(
            Thesis(
                thesis_id=f"{INFERRED_THESIS_PREFIX}{claim.claim_id}",
                statement=claim.text or claim.rationale or claim.claim_id,
                claim_ids=(claim.claim_id,),
                dependency_roles={claim.claim_id: ROLE_REQUIRED},
            )
        )
    return out


def register_theses(graph: ClaimGraph, theses: Iterable[Thesis]) -> List[Thesis]:
    """把 Thesis 挂到图上（幂等）。同 id 不同内容 → 抛错，不覆盖."""
    registered: List[Thesis] = []
    for thesis in theses:
        existing = graph.theses.get(thesis.thesis_id)
        if existing is not None:
            if (
                tuple(existing.claim_ids) != tuple(thesis.claim_ids)
                or dict(existing.role_map) != dict(thesis.role_map)
            ):
                raise FragilityError(
                    f"thesis_id 冲突且内容不同: {thesis.thesis_id}（不覆盖既有声明）"
                )
            registered.append(existing)
            continue
        registered.append(graph.add_thesis(thesis))
    return registered


def resolve_theses(
    graph: ClaimGraph,
    declared: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    include_inferred: bool = True,
) -> Tuple[List[Thesis], str]:
    """选一组要分析的 Thesis，并返回**它是怎么来的**.

    优先级：显式声明 → 图上已注册 → 由推论提升。三条都不成立时返回空列表 +
    ``THESIS_SOURCE_NONE``，让调用方去报 ``no_theses_declared`` ——
    而不是返回一份"看起来干净"的空报告。
    """
    if declared:
        theses = theses_from_declaration(graph, declared)
        register_theses(graph, theses)
        return sorted(theses, key=lambda item: item.thesis_id), THESIS_SOURCE_DECLARED
    if graph.theses:
        return sorted(graph.theses.values(), key=lambda item: item.thesis_id), THESIS_SOURCE_DECLARED
    if include_inferred:
        theses = theses_from_inferences(graph)
        if theses:
            register_theses(graph, theses)
            return sorted(theses, key=lambda item: item.thesis_id), THESIS_SOURCE_INFERENCES
    return [], THESIS_SOURCE_NONE


__all__ = [
    "INFERRED_THESIS_PREFIX",
    "register_theses",
    "resolve_theses",
    "theses_from_declaration",
    "theses_from_inferences",
]
