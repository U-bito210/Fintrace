"""CARD-25 — 版本谱系（append-only）。

**判定"谁替代了谁"不在这里发明。** 卡 §7 的三态区分归 CARD-13，本模块直接调用它的
:func:`~src.cn.conflict.classify_change`，只消费 ``VERSION_SUPERSESSION`` 这一个结论。
少一处判据，就少一处日后走岔的地方。

**append-only 的形态**：一条 :class:`VersionLineageEntry` 是 frozen 的，改状态只能
**追加一条修订**（:meth:`VersionLineage.revise`）。因此"某个值原来的版本是什么"
在任何时刻都还能读出来 —— 这就是卡 §13 第一条要的可回放性。
物理覆盖在这里不是"不推荐"，是**做不到**。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.cn.conflict import (
    CHANGE_VERSION_SUPERSESSION,
    SOURCE_UNKNOWN,
    candidate_from_fact,
    classify_change,
    default_source_class,
)

from .models import (
    LINEAGE_AS_REPORTED,
    LINEAGE_CORRECTED,
    LINEAGE_RESTATED,
    LINEAGE_SUPERSEDED,
    LIVE_VERSION_STATUSES,
    REASON_CORRECTION,
    REASON_RESTATEMENT,
    TemporalError,
    VersionLineageEntry,
)

#: 披露版本 → 替代理由。
_REASON_BY_STATUS: Dict[str, str] = {
    LINEAGE_RESTATED: REASON_RESTATEMENT,
    LINEAGE_CORRECTED: REASON_CORRECTION,
}


def _token(value: Any) -> Optional[str]:
    """枚举 / 字符串统一取字面量。"""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def version_status_from_fact(fact: Any) -> str:
    """把 CARD-02 的 ``Version`` 映射到本卡四态。

    **永不产出 ``SUPERSEDED``** —— 那不是一个发布口径，而是谱系判定的结果，
    由 :meth:`VersionLineage.mark_superseded` 写入。若把"披露时就是这样写的"
    和"后来被替代了"混进同一个映射，一条被重述过的旧值会被当成它的初始状态就是
    "已被替代"，历史就此被抹掉。

    未知/缺失的 token 落到 ``AS_REPORTED``，与 CARD-02 ``Version`` 自己的默认值一致
    （它的文档串写明 "Default AS_REPORTED"），不是本卡另立一套默认。
    """
    raw = _token(getattr(fact, "version", None))
    token = (raw or "").strip().upper()
    if token in LIVE_VERSION_STATUSES:
        return token
    return LINEAGE_AS_REPORTED


def _scope_token(fact: Any) -> Optional[str]:
    scope = getattr(fact, "scope", None)
    token = _token(scope)
    return token.strip() if isinstance(token, str) and token.strip() else None


def entry_from_fact(fact: Any, *, ingested_at: Optional[str] = None) -> VersionLineageEntry:
    """由一个 CARD-02 事实造一条初始血缘条目。"""
    status = version_status_from_fact(fact)
    return VersionLineageEntry(
        fact_id=str(fact.normalized_fact_id),
        version_status=status,
        supersedes=None,
        superseded_by=None,
        reason=_REASON_BY_STATUS.get(status, ""),
        source_published_at=getattr(fact, "available_at", None),
        ingested_at=ingested_at,
        metric=getattr(fact, "metric", None),
        period_key=getattr(fact, "period_key", None),
        scope=_scope_token(fact),
        value=getattr(fact, "value", None),
    )


class VersionLineage:
    """一个 run 内所有事实的版本谱系（append-only 记录 + 末条生效视图）。"""

    def __init__(self, records: Optional[Iterable[VersionLineageEntry]] = None) -> None:
        self._records: List[VersionLineageEntry] = []
        self._effective: Dict[str, VersionLineageEntry] = {}
        for record in records or ():
            self.register(record)

    # -- 写入 -------------------------------------------------------------
    def register(self, entry: VersionLineageEntry) -> VersionLineageEntry:
        """追加一条血缘记录（永不覆盖历史）。"""
        if not isinstance(entry, VersionLineageEntry):
            raise TemporalError(f"register 需要一个 VersionLineageEntry，收到 {type(entry)!r}")
        self._records.append(entry)
        self._effective[entry.fact_id] = entry
        return entry

    def revise(self, fact_id: str, **changes: Any) -> VersionLineageEntry:
        """基于当前生效条目**追加**一条修订。

        历史条目原样留在 ``records`` 里，"它被替代之前是什么版本"任何时候都读得出来。
        """
        current = self._effective.get(fact_id)
        if current is None:
            raise TemporalError(f"revise 的目标不存在：{fact_id}")
        entry = dataclasses.replace(current, **changes)
        return self.register(entry)

    def mark_superseded(
        self,
        fact_id: str,
        *,
        superseded_by: str,
        reason: str,
    ) -> VersionLineageEntry:
        """把 ``fact_id`` 标为已被 ``superseded_by`` 替代，并写齐血缘边。

        **一个替代者可以同时替代多条旧值**（一份更正公告常常一次改好几行），
        而 :attr:`VersionLineageEntry.supersedes` 是**单值**的（卡 §6 就是这样定义的）。
        所以这里的约定是：

        * ``superseded_by``（写在**旧值**上）是**权威且完整**的映射 —— 每条旧值都记下
          自己被谁替代了，这是"这个值被谁取代"的完整答案；
        * ``supersedes``（写在**新值**上）只记**第一个**被替代者，作为"主前驱"指针，
          **不覆盖**。若在这里让它"最后写入者获胜"，先建立的那条反向边就会被悄悄抹掉 ——
          那正是卡 §13 第一条禁止的物理覆盖（施工时被集成测试抓到）。

        要看全部前驱用 :meth:`predecessors`；:meth:`chain` 走的是主前驱那条线。
        """
        if fact_id not in self._effective:
            raise TemporalError(f"mark_superseded 的目标不存在：{fact_id}")
        if superseded_by not in self._effective:
            raise TemporalError(
                f"mark_superseded 的替代者不存在：{superseded_by}（血缘不能指向图外的 id）"
            )
        old = self.revise(
            fact_id,
            version_status=LINEAGE_SUPERSEDED,
            superseded_by=superseded_by,
            reason=reason,
        )
        # 反向边只在**还没有**主前驱时写一次：首写胜出，绝不用后来的替代者覆盖先前的。
        newer = self._effective[superseded_by]
        if newer.supersedes is None:
            self.revise(superseded_by, supersedes=fact_id)
        return old

    def predecessors(self, fact_id: str) -> Tuple[str, ...]:
        """**全部**直接前驱（可能不止一个）。

        权威来源是各旧值上的 ``superseded_by`` —— 单值的 ``supersedes`` 装不下多条旧值。
        """
        return tuple(
            record
            for record in sorted(self._effective)
            if self._effective[record].superseded_by == fact_id
        )

    # -- 读取 -------------------------------------------------------------
    def get(self, fact_id: str) -> Optional[VersionLineageEntry]:
        return self._effective.get(fact_id)

    def require(self, fact_id: str) -> VersionLineageEntry:
        entry = self._effective.get(fact_id)
        if entry is None:
            raise TemporalError(f"未知血缘节点：{fact_id}")
        return entry

    def __contains__(self, fact_id: object) -> bool:
        return fact_id in self._effective

    def __len__(self) -> int:
        return len(self._effective)

    @property
    def records(self) -> Tuple[VersionLineageEntry, ...]:
        """全部历史记录（含被修订过的旧条目）。"""
        return tuple(self._records)

    def entries(self) -> Tuple[VersionLineageEntry, ...]:
        """每个 fact_id 的当前生效条目。"""
        return tuple(self._effective[key] for key in sorted(self._effective))

    def history(self, fact_id: str) -> Tuple[VersionLineageEntry, ...]:
        return tuple(record for record in self._records if record.fact_id == fact_id)

    def superseded_ids(self) -> Tuple[str, ...]:
        return tuple(
            key
            for key in sorted(self._effective)
            if self._effective[key].version_status == LINEAGE_SUPERSEDED
        )

    def live_ids(self) -> Tuple[str, ...]:
        return tuple(
            key for key in sorted(self._effective) if self._effective[key].is_live
        )

    def chain(self, fact_id: str) -> Tuple[str, ...]:
        """从最老的版本到最新的版本，串成一条链（含 ``fact_id``）。"""
        if fact_id not in self._effective:
            raise TemporalError(f"未知血缘节点：{fact_id}")

        oldest = fact_id
        seen = {oldest}
        while True:
            entry = self._effective.get(oldest)
            if entry is None or not entry.supersedes:
                break
            nxt = entry.supersedes
            if nxt in seen:
                raise TemporalError(f"血缘链出现环：{fact_id} → … → {nxt}")
            seen.add(nxt)
            oldest = nxt

        ordered: List[str] = [oldest]
        cursor = oldest
        # 步数上限 = 节点数，环会在到达上限前被判出。
        for _ in range(len(self._effective) + 1):
            entry = self._effective.get(cursor)
            if entry is None or not entry.superseded_by:
                return tuple(ordered)
            if entry.superseded_by in seen and entry.superseded_by in ordered:
                raise TemporalError(f"血缘链出现环：{fact_id} → … → {entry.superseded_by}")
            cursor = entry.superseded_by
            ordered.append(cursor)
        raise TemporalError(f"血缘链未终止（疑似环）：{fact_id}")

    def assert_acyclic(self) -> None:
        """每条链都必须终止；任一条不终止就是血缘成环。"""
        for fact_id in sorted(self._effective):
            self.chain(fact_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries()],
            "records": [record.to_dict() for record in self._records],
            "superseded_ids": list(self.superseded_ids()),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "VersionLineage":
        """按**记录顺序**重建，使修订历史原样恢复（不能只读生效视图）。"""
        records = [
            VersionLineageEntry(
                fact_id=item["fact_id"],
                version_status=item.get("version_status", LINEAGE_AS_REPORTED),
                supersedes=item.get("supersedes"),
                superseded_by=item.get("superseded_by"),
                reason=item.get("reason", ""),
                source_published_at=item.get("source_published_at"),
                ingested_at=item.get("ingested_at"),
                metric=item.get("metric"),
                period_key=item.get("period_key"),
                scope=item.get("scope"),
                value=item.get("value"),
            )
            for item in payload.get("records", payload.get("entries", []))
        ]
        return cls(records)


# ---------------------------------------------------------------------------
# 由 CARD-02 事实构建
# ---------------------------------------------------------------------------
def build_lineage(
    facts: Sequence[Any],
    *,
    document_type: Optional[str] = None,
    ingested_at: Optional[str] = None,
    tolerance: Optional[float] = None,
) -> Tuple[VersionLineage, List[str]]:
    """由一批 CARD-02 事实构建版本谱系，返回 ``(lineage, warnings)``。

    两步，都是确定性的：

    1. 每个事实按自己的披露版本登记一条初始条目；
    2. **同 ``(metric, period_key, scope)``** 的事实两两送进 CARD-13 的
       :func:`~src.cn.conflict.classify_change`，只采纳 ``VERSION_SUPERSESSION``
       的判决，双向写齐 ``supersedes`` / ``superseded_by``。

    第 2 步的键**含 scope**：口径不同的两行是两件事，把它们判成"新版替代旧版"
    正是 CARD-22 §23 记过的那个坑（配对键不含 scope 会让"口径不同"退化成假的
    "对方没有这一行"）。
    """
    fact_list = list(facts)
    lineage = VersionLineage()
    warnings: List[str] = []

    for fact in fact_list:
        entry = entry_from_fact(fact, ingested_at=ingested_at)
        lineage.register(entry)
        token = (_token(getattr(fact, "version", None)) or "").strip()
        if token and token.upper() not in LIVE_VERSION_STATUSES:
            warnings.append(
                f"lineage_unknown_version_token:{entry.fact_id}:{token}"
                f"（按 AS_REPORTED 处理，与 CARD-02 默认一致）"
            )

    source_class = default_source_class(document_type)
    if source_class == SOURCE_UNKNOWN:
        warnings.append(
            "lineage_source_class_unknown：未提供 document_type，"
            "候选按未知来源登记（只影响 CARD-13 的仲裁，不影响本卡的版本替代判定）"
        )

    grouped: Dict[Tuple[str, str, Optional[str]], List[Any]] = {}
    for fact in fact_list:
        key = (
            str(getattr(fact, "metric", "") or ""),
            str(getattr(fact, "period_key", "") or ""),
            _scope_token(fact),
        )
        grouped.setdefault(key, []).append(fact)

    for key in sorted(grouped, key=lambda item: (item[0], item[1], item[2] or "")):
        group = grouped[key]
        if len(group) < 2:
            continue
        # 组内按 (available_at, fact_id) 定序，保证判定与输入顺序无关。
        ordered = sorted(
            group,
            key=lambda f: (
                str(getattr(f, "available_at", "") or ""),
                str(getattr(f, "normalized_fact_id", "")),
            ),
        )
        candidates = [
            candidate_from_fact(fact, source_class=source_class) for fact in ordered
        ]
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                left, right = candidates[i], candidates[j]
                kind, _reason = classify_change(left, right, tolerance=tolerance)
                if kind != CHANGE_VERSION_SUPERSESSION:
                    continue
                older, newer = _older_newer(left, right)
                old_entry = lineage.get(older.candidate_id)
                new_entry = lineage.get(newer.candidate_id)
                if old_entry is None or new_entry is None:
                    continue
                if old_entry.version_status == LINEAGE_SUPERSEDED:
                    continue
                reason = _REASON_BY_STATUS.get(new_entry.version_status, REASON_RESTATEMENT)
                lineage.mark_superseded(
                    older.candidate_id, superseded_by=newer.candidate_id, reason=reason
                )

    lineage.assert_acyclic()
    return lineage, warnings


def _older_newer(a: Any, b: Any):
    """与 CARD-13 ``_order_by_recency`` 同序：(较旧, 较新)。"""
    from src.cn.conflict import version_rank

    key_a = (str(getattr(a, "available_at", "") or ""), version_rank(a.version), a.candidate_id)
    key_b = (str(getattr(b, "available_at", "") or ""), version_rank(b.version), b.candidate_id)
    return (a, b) if key_a <= key_b else (b, a)


__all__ = [
    "VersionLineage",
    "build_lineage",
    "entry_from_fact",
    "version_status_from_fact",
]