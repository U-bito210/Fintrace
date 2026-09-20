"""CARD-25 — 时间重验证的数据模型与词表.

**这里不定义"三种变化"。** CARD-13 已经拥有 `DATA_CONFLICT` /
`VERSION_SUPERSESSION` / `NEW_INFORMATION` 这套词表（`CHANGE_KINDS`），
本模块**导入并校验**，绝不重定义一遍 —— 两处同名常量日后必然走岔。

本模块自己拥有的只有两类词表，因为 CARD-02 / CARD-09 都没有：

1. **版本谱系四态**（``version_status``）。CARD-02 的 :class:`~src.cn.normalization.models.Version`
   只有三个成员（``AS_REPORTED`` / ``RESTATED`` / ``CORRECTED``），而且它描述的是
   *"这份披露是以什么口径发布的"*；卡 §6 要的第四个 ``SUPERSEDED`` 描述的是
   *"这个节点当前在谱系里还站不站得住"*。
   这是**两个维度**，不是枚举漏了一个值 —— 所以这里另立四态，
   **不去扩 CARD-02 的 ``Version``**（扩了会让 ``version_rank`` 直接 ``KeyError``，
   因为替代态没有"新旧"可言，它根本不是一种发布口径）。

2. **事件类型与重验证动作词表**（``event_type`` / ``action_type``），卡 §6 与 §8 自定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# CARD-13 的三态 —— 只导入，不重定义。
from src.cn.conflict import (
    CHANGE_DATA_CONFLICT,
    CHANGE_KINDS,
    CHANGE_NEW_INFORMATION,
    CHANGE_VERSION_SUPERSESSION,
)

# ---------------------------------------------------------------------------
# 版本谱系（卡 §6 VersionLineageEntry.version_status）
# ---------------------------------------------------------------------------
LINEAGE_AS_REPORTED = "AS_REPORTED"
LINEAGE_RESTATED = "RESTATED"
LINEAGE_CORRECTED = "CORRECTED"
LINEAGE_SUPERSEDED = "SUPERSEDED"

#: 四态。前三个与 CARD-02 的 ``Version`` 同名同义（便于对齐），第四个是本卡新增的"已被替代"。
VERSION_STATUSES: Tuple[str, ...] = (
    LINEAGE_AS_REPORTED,
    LINEAGE_RESTATED,
    LINEAGE_CORRECTED,
    LINEAGE_SUPERSEDED,
)

#: 仍有资格作为"当前有效披露"的三态（即未被替代的）。
LIVE_VERSION_STATUSES: Tuple[str, ...] = (
    LINEAGE_AS_REPORTED,
    LINEAGE_RESTATED,
    LINEAGE_CORRECTED,
)

VERSION_STATUS_LABELS: Dict[str, str] = {
    LINEAGE_AS_REPORTED: "原始披露",
    LINEAGE_RESTATED: "重述",
    LINEAGE_CORRECTED: "更正",
    LINEAGE_SUPERSEDED: "已被替代",
}

#: 替代理由（卡 §6 ``reason``）。空串只对 AS_REPORTED 合法 —— 原始披露没有"理由"。
REASON_RESTATEMENT = "restatement"
REASON_CORRECTION = "correction"
REASON_UPDATE = "update"
REASON_DEPRECATION = "deprecation"
LINEAGE_REASONS: Tuple[str, ...] = (
    REASON_RESTATEMENT,
    REASON_CORRECTION,
    REASON_UPDATE,
    REASON_DEPRECATION,
)

# ---------------------------------------------------------------------------
# 事件类型（卡 §6 TemporalEvent.event_type）
# ---------------------------------------------------------------------------
EVENT_NEW_FILING = "new_filing"
EVENT_CORRECTION = "correction"
EVENT_RESTATEMENT = "restatement"
EVENT_NEW_SNAPSHOT = "new_snapshot"
EVENT_TYPES: Tuple[str, ...] = (
    EVENT_NEW_FILING,
    EVENT_CORRECTION,
    EVENT_RESTATEMENT,
    EVENT_NEW_SNAPSHOT,
)

EVENT_TYPE_LABELS: Dict[str, str] = {
    EVENT_NEW_FILING: "新财报",
    EVENT_CORRECTION: "更正公告",
    EVENT_RESTATEMENT: "重述",
    EVENT_NEW_SNAPSHOT: "新快照",
}

# ---------------------------------------------------------------------------
# 报告状态 / 动作词表
# ---------------------------------------------------------------------------
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
REPORT_STATUSES: Tuple[str, ...] = (STATUS_OK, STATUS_PARTIAL, STATUS_UNAVAILABLE)

ACTION_REVALIDATE_CLAIM = "revalidate_claim"
ACTION_REFRESH_CALCULATION = "refresh_calculation"
ACTION_REVIEW_SUPERSESSION = "review_supersession"
ACTION_ARBITRATE_CONFLICT = "arbitrate_conflict"
ACTION_RECHECK_THESIS = "recheck_thesis"
ACTION_REFRESH_VALUATION = "refresh_valuation"
ACTION_REFRESH_MEMO_SECTION = "refresh_memo_section"
ACTION_TYPES: Tuple[str, ...] = (
    ACTION_REVALIDATE_CLAIM,
    ACTION_REFRESH_CALCULATION,
    ACTION_REVIEW_SUPERSESSION,
    ACTION_ARBITRATE_CONFLICT,
    ACTION_RECHECK_THESIS,
    ACTION_REFRESH_VALUATION,
    ACTION_REFRESH_MEMO_SECTION,
)

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITIES: Tuple[str, ...] = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)

#: 卡 §4 的 impact 六个字段。**键必须齐全**：缺一个就等于报告没回答那个问题。
IMPACT_KEYS: Tuple[str, ...] = (
    "facts_superseded",
    "calculations_stale",
    "claims_invalidated",
    "theses_weakened",
    "valuations_stale",
    "memo_sections_affected",
)

#: 图之外的域（估值属 CARD-08、备忘段属 CARD-10）。调用方不给句柄时**不猜 0**，
#: 报 ``None`` + warning，理由见报告 ``unavailable_reason``。
EXTERNAL_IMPACT_KEYS: Tuple[str, ...] = ("valuations_stale", "memo_sections_affected")

DEFAULT_MAX_DEPTH = 8

#: ``unavailable_reason`` 取值。**没有** "changed 为空"这一项：卡 §14 要求"无影响 → 空报告"，
#: 那是一份**有效**的空报告，不是不可用 —— 两者混为一谈会让"什么都没发生"看起来像"算不出来"。
UNAVAILABLE_UNKNOWN_NODES = "unknown_changed_nodes"
UNAVAILABLE_NO_CLAIMS = "graph_has_no_claims"


class TemporalError(RuntimeError):
    """CARD-25 的调用方错误：非法事件、非法血缘条目、未知节点。"""


# ---------------------------------------------------------------------------
# 版本谱系条目
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VersionLineageEntry:
    """一个事实在版本谱系里的位置（卡 §6）。

    **frozen**：卡 §13 第一条就是"superseded 事实永不物理覆盖"。
    做成不可变对象，这条不变式就是**结构上**成立的，而不只是靠校验器事后发现；
    要改状态就**追加一条新修订**（见 :meth:`VersionLineage.revise`），历史一条不丢。

    ``supersedes`` / ``superseded_by`` 是**有向**的：新的那条写 ``supersedes=旧``，
    旧的那条写 ``superseded_by=新``。两条边都由 :mod:`src.cn.temporal.lineage`
    一次性写齐，不允许调用方只写一半 —— 单向的血缘无法回答"这个值被谁替代了"。

    ``source_published_at`` 与 ``ingested_at`` **刻意分开**（卡 §6 明确要求）：
    公告的发布日期与它进入系统的时刻常常差几天，混用会让"按时间顺序依次传播"
    （§12 边缘 4）排错顺序。
    """

    fact_id: str
    version_status: str = LINEAGE_AS_REPORTED
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    reason: str = ""
    source_published_at: Optional[str] = None
    ingested_at: Optional[str] = None
    #: 便于审计的上下文，不参与判定。
    metric: Optional[str] = None
    period_key: Optional[str] = None
    scope: Optional[str] = None
    value: Optional[float] = None

    def __post_init__(self) -> None:
        if not str(self.fact_id or "").strip():
            raise TemporalError("VersionLineageEntry.fact_id 不能为空")
        if self.version_status not in VERSION_STATUSES:
            raise TemporalError(
                f"未知 version_status: {self.version_status!r}（合法值：{list(VERSION_STATUSES)}）"
            )
        if self.reason and self.reason not in LINEAGE_REASONS:
            raise TemporalError(
                f"未知 reason: {self.reason!r}（合法值：{list(LINEAGE_REASONS)}）"
            )
        if self.reason and self.version_status == LINEAGE_AS_REPORTED:
            raise TemporalError(
                "AS_REPORTED 是首次披露，不该带替代理由 —— "
                "带理由说明它其实是一次重述/更正，version_status 应同步改"
            )
        if self.supersedes is not None and not str(self.supersedes).strip():
            raise TemporalError("supersedes 给了空串；要么给具体的旧 fact_id，要么给 None")
        if self.superseded_by is not None and not str(self.superseded_by).strip():
            raise TemporalError("superseded_by 给了空串；要么给具体的新 fact_id，要么给 None")
        for edge in (self.supersedes, self.superseded_by):
            if edge is not None and edge == self.fact_id:
                raise TemporalError(f"血缘自指：{self.fact_id} 不能替代自己")
        # "已被替代"与"还在线上"是互斥的：一条被替代的记录却仍报 RESTATED，
        # 会让下游读到"它还是当前有效值"。
        if self.superseded_by and self.version_status != LINEAGE_SUPERSEDED:
            raise TemporalError(
                f"节点 {self.fact_id} 已声明 superseded_by={self.superseded_by!r}，"
                f"version_status 必须为 {LINEAGE_SUPERSEDED}，而非 {self.version_status!r}"
            )
        if self.version_status == LINEAGE_SUPERSEDED and not self.superseded_by:
            raise TemporalError(
                f"节点 {self.fact_id} 标为 SUPERSEDED 却没说被谁替代（superseded_by 为空）—— "
                "被替代却没有替代者，是血缘断链"
            )

    @property
    def is_live(self) -> bool:
        """这条披露当前还算不算数。"""
        return self.version_status in LIVE_VERSION_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "version_status": self.version_status,
            "version_status_label": VERSION_STATUS_LABELS.get(
                self.version_status, self.version_status
            ),
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "reason": self.reason,
            "source_published_at": self.source_published_at,
            "ingested_at": self.ingested_at,
            "metric": self.metric,
            "period_key": self.period_key,
            "scope": self.scope,
            "value": self.value,
            "is_live": self.is_live,
        }


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TemporalEvent:
    """一次"世界变了"的声明（卡 §6）。

    ``event_kind`` 必须是 CARD-13 的三态之一。事件**不自己判断**自己属于哪种变化 ——
    调用方（或 :mod:`src.cn.temporal.engine` 的构造器）用 CARD-13 的 ``classify_change``
    判定后填进来，避免本卡另养一套判据。
    """

    event_id: str
    event_kind: str
    event_type: str
    #: 允许留空（见 ``__post_init__`` 的说明）；留空时引擎会给 ``event_date_missing`` warning。
    event_date: str = ""
    changed_source_ids: Tuple[str, ...] = ()
    description: str = ""
    #: 事件涉及的具体版本边（新→旧），供 VERSION_SUPERSESSION 使用。
    supersedes: Optional[str] = None
    #: 仅 DATA_CONFLICT 用：CARD-13 已裁决出的胜出来源 id。
    #: **给了它就不阻断**（卡 §7 表：「标记 CONFLICTED → CARD-13 裁决 > **unresolved** → 下游 BLOCKED」，
    #: 阻断的前提是"未裁决"）。没给 = 未裁决 → 下游 BLOCKED，这是 fail-closed 的默认。
    resolved_by: Optional[str] = None

    def __post_init__(self) -> None:
        if not str(self.event_id or "").strip():
            raise TemporalError("TemporalEvent.event_id 不能为空")
        if self.event_kind not in CHANGE_KINDS:
            raise TemporalError(
                f"未知 event_kind: {self.event_kind!r}（合法值：{list(CHANGE_KINDS)}）"
            )
        if self.event_type not in EVENT_TYPES:
            raise TemporalError(
                f"未知 event_type: {self.event_type!r}（合法值：{list(EVENT_TYPES)}）"
            )
        # ``event_date`` **不强制**：卡 §12 边缘 4 要按时间顺序传播，但"日期未知"是一种
        # 真实状态。强制非空等于逼调用方编一个日期出来 —— 编出来的日期会被下游当成
        # 排序依据。这里允许留空，由引擎在报告里留 ``event_date_missing`` warning，
        # 让"没能按时间排序"这件事**看得见**，而不是假装排过。
        if self.supersedes is not None and not str(self.supersedes).strip():
            raise TemporalError("supersedes 给了空串；要么给具体 fact_id，要么给 None")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "event_type": self.event_type,
            "event_date": self.event_date,
            "changed_source_ids": list(self.changed_source_ids),
            "description": self.description,
            "supersedes": self.supersedes,
            "resolved_by": self.resolved_by,
        }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
@dataclass
class RevalidationReport:
    """一次时间重验证的完整结果（卡 §6）。

    与 CARD-09 的 :class:`~src.cn.graph.models.RevalidationReport` **不是同一个东西**：
    那个是"我改了证据，图的状态变成什么样"，只覆盖 claim；这个是"外面发生了一件事，
    我该重算什么"，覆盖血缘 + 子图 + 外部域 + 全量/增量对比。两者刻意分开命名，
    因为它们回答的问题不同，混用会让"这件事影响了什么"退化成"哪些 claim 变了"。
    """

    event_id: str
    impact: Dict[str, Any] = field(default_factory=dict)
    changed_nodes: Tuple[str, ...] = ()
    affected_subgraph: Tuple[str, ...] = ()
    state_diff: Tuple[Dict[str, Any], ...] = ()
    revalidation_actions: Tuple[Dict[str, Any], ...] = ()
    full_vs_incremental: Dict[str, Any] = field(default_factory=dict)
    lineage: Tuple[Dict[str, Any], ...] = ()
    status: str = STATUS_OK
    unavailable_reason: Optional[str] = None
    #: 卡 §11：「增量与全量结果不一致 → 断言失败，回落全量重算并记录」。
    #: 取值为 ``"full_recomputation"`` 或 ``None``。**不静默**：回落了就要写在这里。
    fallback: Optional[str] = None
    warnings: Tuple[str, ...] = ()
    #: 事件本身，便于报告自解释、可回放。
    event: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event": self.event,
            "status": self.status,
            "unavailable_reason": self.unavailable_reason,
            "impact": dict(self.impact),
            "changed_nodes": list(self.changed_nodes),
            "affected_subgraph": list(self.affected_subgraph),
            "state_diff": [dict(item) for item in self.state_diff],
            "revalidation_actions": [dict(item) for item in self.revalidation_actions],
            "full_vs_incremental": dict(self.full_vs_incremental),
            "lineage": [dict(item) for item in self.lineage],
            "fallback": self.fallback,
            "warnings": list(self.warnings),
        }


__all__ = [
    "ACTION_ARBITRATE_CONFLICT",
    "ACTION_RECHECK_THESIS",
    "ACTION_REFRESH_CALCULATION",
    "ACTION_REFRESH_MEMO_SECTION",
    "ACTION_REFRESH_VALUATION",
    "ACTION_REVALIDATE_CLAIM",
    "ACTION_REVIEW_SUPERSESSION",
    "ACTION_TYPES",
    "CHANGE_DATA_CONFLICT",
    "CHANGE_KINDS",
    "CHANGE_NEW_INFORMATION",
    "CHANGE_VERSION_SUPERSESSION",
    "DEFAULT_MAX_DEPTH",
    "EVENT_NEW_FILING",
    "EVENT_NEW_SNAPSHOT",
    "EVENT_CORRECTION",
    "EVENT_RESTATEMENT",
    "EVENT_TYPE_LABELS",
    "EVENT_TYPES",
    "EXTERNAL_IMPACT_KEYS",
    "IMPACT_KEYS",
    "LINEAGE_AS_REPORTED",
    "LINEAGE_CORRECTED",
    "LINEAGE_REASONS",
    "LINEAGE_RESTATED",
    "LINEAGE_SUPERSEDED",
    "LIVE_VERSION_STATUSES",
    "PRIORITIES",
    "PRIORITY_HIGH",
    "PRIORITY_LOW",
    "PRIORITY_MEDIUM",
    "REASON_CORRECTION",
    "REASON_DEPRECATION",
    "REASON_RESTATEMENT",
    "REASON_UPDATE",
    "REPORT_STATUSES",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_UNAVAILABLE",
    "TemporalError",
    "TemporalEvent",
    "UNAVAILABLE_NO_CLAIMS",
    "UNAVAILABLE_UNKNOWN_NODES",
    "VERSION_STATUSES",
    "VERSION_STATUS_LABELS",
    "VersionLineageEntry",
    "RevalidationReport",
]
