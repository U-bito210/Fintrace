# CARD-25: Temporal Revalidation Engine（时间重验证引擎）

| 属性 | 值 |
|---|---|
| 优先级 | P1（基础版 P0 可选早段：version lineage + 三态区分） |
| 竞赛阶段 | 初赛版（10/12–10/15 窗口 5；与 CARD-24 共用 Dependency Graph） |
| 对应赛题 | 北京赛：赛题2/5 时效性与版本处理（创新三）；华北五省：Incremental Computation |
| 依赖 | CARD-09（Dependency Graph）、CARD-13（三态区分：CONFLICT/SUPERSESSION/NEW_INFO）、CARD-24（Monitoring Plan 联动） |
| 实现复杂度 | 中高（约 5 人日） |
| 状态 | 🟡 初赛版已落地（2026-09-19）；§17 状态传播交互已随 CARD-15 落地（2026-09-20）；公告驱动的替代路径受语料限制不可用，见 §16 |
| Skill 要求 | 动手前加载 financial-data-provenance |

> 本卡前身：v3.1 的 "Temporal Revalidation"。v3.2 升级为 **Temporal Revalidation Engine**：明确 Version Lineage（AS_REPORTED/RESTATED/CORRECTED/SUPERSEDED），区分 DATA CONFLICT / VERSION SUPERSESSION / NEW INFORMATION 三种变化，实现 **Incremental Revalidation（affected-subgraph recomputation）**——新信息进入后，**不得默认全量重新执行所有 LLM workflow**。

## 1. Objective（目标）

新建 `src/cn/temporal/`：当新财报/新公告/更正公告/重新披露/新 Snapshot 进入系统后，做影响传播与**增量重验证**：

```
Source Changed ↓ Evidence superseded/conflict ↓ Calculation stale ↓ Claim stale/blocked ↓ Thesis affected ↓ Valuation affected ↓ Memo section affected
```

目标：自动计算 `affected_subgraph`，只重新执行受影响的 calculations / claims / theses / valuations / memo sections，并提供 impact analysis。

## 2. Competition relevance（双赛分别解决什么评审问题）

| 比赛 | 解决的评审问题 |
|---|---|
| 华北五省计算机设计大赛 | 展示 **Incremental Computation**：不重新跑整条 pipeline，而是仅重算受影响子图——性能与正确性可对比（full vs incremental → 相同结果 + 更低延迟） |
| 北京市大学生金融人工智能竞赛 | 回答"时效性/版本正确性"：更正公告或重述进入后，系统自动识别哪些结论过期/被阻断，而不是带着旧结论继续交付 |

## 3. Why this is not a gimmick（为什么不是点缀）

普通系统面对新公告的做法：要么"整份报告重新生成"（高成本、不稳定），要么忽略（错误保留旧结论）。本引擎的差异：

- **版本谱系**：旧值永不覆盖、可回放历史视图（supersedes / superseded_by）
- **三类变化分开传播**：DATA CONFLICT / VERSION SUPERSESSION / NEW INFORMATION **对应的传播规则不同**，不能全部叫 conflict
- **增量重验证**：changed_node_ids → affected_subgraph → 只重算受影响节点（可验证：与全量重计算同结果）
- 与 CARD-24 Monitoring Plan 联动：监测 KPI 触发 → 自动发起受影响的 claims 重验证

## 4. Inputs / 5. Outputs

- **输入**：changed_node_ids / TemporalEvent（新文件、更正、重述、新 snapshot）、Dependency Graph、Version Lineage
- **输出**：`RevalidationReport` + impact analysis（如：7 facts superseded / 4 calculations stale / 3 claims invalidated / 1 thesis weakened / 1 valuation stale / 2 memo sections affected）+ revalidated state diff

## 6. Data Models（数据模型）

```python
@dataclass
class VersionLineageEntry:
    fact_id: str
    version_status: str            # AS_REPORTED | RESTATED | CORRECTED | SUPERSEDED
    supersedes: str | None         # 被替代的旧 version_id
    superseded_by: str | None
    reason: str                    # restatement | correction | update | deprecation
    source_published_at: str
    ingested_at: str               # 显式区分：published vs ingested

@dataclass
class TemporalEvent:
    event_id: str
    event_kind: str                # DATA_CONFLICT | VERSION_SUPERSESSION | NEW_INFORMATION
    event_type: str                # new_filing | correction | restatement | new_snapshot
    event_date: str
    changed_source_ids: list[str]
    description: str

@dataclass
class RevalidationReport:
    event_id: str
    impact: dict                   # {facts_superseded, calculations_stale, claims_invalidated, theses_weakened, valuations_stale, memo_sections_affected}
    changed_nodes: list[str]
    affected_subgraph: list[str]
    state_diff: list[dict]         # [{node_id, before, after}]
    revalidation_actions: list[dict]  # [{action_type, target_id, priority}]
    full_vs_incremental: dict      # {nodes_recomputed, full_nodes, latency_ms, results_identical}
```

## 7. Algorithms（算法）

### 三态区分与传播规则（核心，与 CARD-13 联动）

| 事件 | 定义（不能全叫 conflict） | 传播行为 |
|---|---|---|
| DATA CONFLICT | 同期间同口径两个官方/来源值不一致，无版本关系 | 标记 CONFLICTED → CARD-13 裁决 > unresolved → 下游 BLOCKED |
| VERSION SUPERSESSION | 新版本明确替代旧版本（如更正公告 → E95 替代 E37） | 旧 evidence → SUPERSEDED；依赖计算 stale；依赖 claim stale/blocked |
| NEW INFORMATION | 新季度/新数据（无替代关系） | 计算 stale（period 更新）→ claim needs_refresh → memo section marked refresh |

### Incremental Revalidation（关键算法）
```
changed_node_ids
  → 在 Dependency Graph 上沿依赖方向计算 affected_subgraph（BFS/DFS 受限深度）
  → 只重算子图内 calculations / claims / theses / valuations / memo sections
  → 输出 state diff + 与"全量重算"结果一致性校验（测试断言，运行时可采样验证）
```
- **不得默认全量重新执行所有 LLM workflow**
- 非受影响节点保持原状态（快照视图）

## 8. API / Tool Contract

```python
# src/cn/temporal/engine.py
def ingest_temporal_event(event: TemporalEvent) -> RevalidationReport: ...
def compute_affected_subgraph(changed_node_ids, graph, max_depth=8) -> list[str]: ...
def propagate_status(affected_subgraph, graph) -> list[dict]: ...      # state_diff
def run_incremental_revalidation(report, env) -> RevalidationReport: ...
def impact_analysis(report) -> dict: ...
def compare_full_vs_incremental(run_id, changed_node_ids) -> dict: ...
```

Agent 工具 `RevalidateCnClaimsTool`：入参 `event 或 changed_node_ids`；出参 `status / revalidation_report / impact`。

## 9. Deterministic vs LLM boundary（确定性与 LLM 边界）

- 状态传播、affected_subgraph、重算**全确定性**
- LLM 不参与"是否受影响"判定；LLM 仅用于**受影响 memo 段落的改写**（发生在传播完成后，且只允许在 affected sections 内）

## 10. Dependencies（依赖）

CARD-09（Dependency Graph）、CARD-13（三态/裁决）、CARD-07（事件流审计）、CARD-04（重述信号）、CARD-24（monitoring 触发）。

## 11. Failure Modes

- 传播失败 → 该子图标记 `propagation_failed`，其余不受影响；报告如实列出
- 无法确定影响范围 → 标记 unknown 并人工复核（宁可漏报不误报——漏报让用户看见，误报破坏信任）
- 增量与全量结果不一致 → 断言失败，回落全量重算并记录

## 12. Edge Cases

1. 事实被删除无替代 → 依赖链全部 STALE/BLOCKED
2. 重述调整经济期间 → 重新映射 economic_period
3. 循环依赖 → 构建时拒绝（CARD-09 保证），本卡断言
4. 多事件并发 → 按时间顺序依次传播
5. 新 snapshot 更新同名指标 → NEW_INFORMATION → 计算 stale（不覆盖历史）

## 13. Validator Rules

- superseded 事实永不物理覆盖（append-only + lineage）
- 传播状态变化可回溯（event → state_diff 一一对应）
- affected_subgraph 是 state_diff 的超集（断言）

## 14. Unit Tests

- 三态区分（同一变化场景分别构造 CONFLICT / SUPERSESSION / NEW_INFO）传播正确
- version lineage：更正公告替代 → superseded_by 链正确
- affected_subgraph 计算（小图手工可验）
- **full vs incremental：人工构造图，两组结果完全一致断言**
- 无影响 → 空报告
- 循环 → 拒绝断言

## 15. Integration Tests

真实流程：旧年报 → 更正公告 → 输出 impact（facts/calcs/claims/theses/valuations/memo sections 数量与列表正确）；状态传播与 CARD-23 verify_claim STALE 联动。

## 16. Benchmark

**Ablation**：`full recomputation` vs `incremental affected-subgraph recomputation`，指标 = nodes recomputed / latency / **same deterministic result**（必须三个都有）。接入 CARD-11。

## 17. Demo Flow

Demo 步骤 11（DEMO_FLOW.md，时间允许时）：导入更正公告 → Temporal Revalidation 显示哪些 Fact / Calculation / Claim / Valuation / Memo 段落受影响 → VERIFIED→STALE/BLOCKED 传播可见。

## 18. Acceptance Criteria（验收标准）

- [x] 三态区分有测试（不全部叫 conflict）
- [x] version lineage 四态 + supersedes/superseded_by 关系测试通过
- [x] affected_subgraph 计算正确（小图手工验证）
- [x] full vs incremental 结果一致性断言通过
- [x] 传播/重算路径零 LLM（代码断言）
- [x] 全部测试离线通过 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

**收尾说明（2026-09-19）**：共 **84 个用例**（`tests/test_cn_temporal.py`，collect 实测），全绿。
逐条对账：

- **三态区分**：`test_three_event_kinds_produce_distinguishable_reports` 断言三者影响面**互不相同**
  （替代 → `facts_superseded=1`/不阻断；未裁决冲突 → `claims_invalidated=1`；新信息 → 既不替代也不阻断），
  另有各态独立用例。校验器还有一条 `event_effects_collapsed` 规则把"三态传播语义不得相同"钉住。
- **version lineage 四态**：`test_lineage_accepts_all_four_version_statuses` 覆盖四态；
  `supersedes`/`superseded_by` 双向关系、多前驱、链有序、环拒绝各有用例。
  注意第四个状态 `SUPERSEDED` **不在 CARD-02 的 `Version` 枚举里**（那只有三个成员），
  本卡另立四态而不去扩它 —— 理由见 §23 第 1 条。
- **affected_subgraph**：手验图（`E-OLD → C01`）断言方向性（改 C01 不会把 E-OLD 拉进来）、
  深度截断、种子自含；另有"被替代旧值必须在子图里"的校验器规则。
- **full vs incremental**：小图一致性 + 宽图（60 条链路）**实测节省**
  （`nodes_recomputed=1 < full_nodes=60` 且结果一致）+ **故意截断触发回落全量**的用例。
- **零 LLM**：源码级断言（扫 `src/cn/temporal/*.py` 与 `src/validation/temporal_validator.py`），
  复用 CARD-05 的 `check_source_is_model_free`；扫的是源码文本而非 import 图。
- **未勾的第六项**：与其余各卡同因 —— 离线套件里有 23 项失败源自缺失的本地 tushare 快照
  （`data/snapshots/cn/`），**不得伪造快照修绿**。本卡收尾游标 **1371 passed / 23 failed / 2 skipped**
  （junit `errors=0`），失败集合 14/6/2/1 与本轮之前逐条一致，passed 增量 1287 → 1371 = **+84** = 本卡用例数。

## 19. Priority

**P1**（基础版：version lineage + 三态 + 单向传播，10/12–10/15；incremental recomputation 为 P2/决赛增强，但 impact 统计初赛要出）。不裁剪 P0 主链。

## 20. Competition Stage

初赛：version lineage + 三态 + affected-subgraph 单向传播 + impact analysis；决赛：full incremental recomputation / automatic refresh / 高级可视化。

## 21. Estimated Complexity

约 5 人日（lineage+三态 2 + affected-subgraph 1.5 + impact/测试 1.5）。

## 22. Fallback / Degradation Strategy

- 时间不足：只做**单向** VERSION_SUPERSESSION 传播（更正公告是 demo 主场景），CONFLICT/NEW_INFO 降为"仅记录不阻断"
- incremental 不可用 → 声明回落全量重算（结果一致，代价是延迟）
- 断网不影响（全部本地确定性）

## 23. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **开工前可行性核查（技能第 2 步，别跳）**：实测 15 份语料 → 只有 5 份产出 claims（annual_01/04/05/06/07）。**`annual_04_restated_comparative.pdf` 是唯一的版本谱系语料**：它的 `revenue/2025FY` 同时存在 `AS_REPORTED 1100000`、`AS_REPORTED 900000`（同版本同口径不同值 ⇒ CARD-13 判 DATA_CONFLICT）与 `RESTATED 1000000`（判 VERSION_SUPERSESSION）；`operating_cost/2025FY` 同样成对。即**"更正/重述替代旧值"这个 demo 主场景在本语料里真实存在**，不需要造数。 |
| 2026-09-19 | **但也查明一条断头路**：卡 §17 的 demo 是"导入更正公告"，而 `announcement_04_correction.pdf` 解析出 **0 条 fact**，CARD-13 的正文型更正提取器 `correction_pairs()` 需要 fact 上的文本流才能跑，于是对代理语料**恒返回 `correction_no_text_facts`**（0 对）。即：**公告驱动的替代路径离线不可用**，可得的是**同一文档内部的重述血缘**。本卡据此把集成场景建在 `annual_04` 的真实重述对上，并把公告路径如实记为未闭合。 |
| 2026-09-19 | **卡 §6 的第四态 `SUPERSEDED` 在 CARD-02 里不存在（重要）**：`Version` 只有 `AS_REPORTED / RESTATED / CORRECTED` 三个成员。而且这不只是"少一个值"这么简单 —— `Version` 描述的是**这份披露以什么口径发布**，`SUPERSEDED` 描述的是**这个节点当前在谱系里还站不站得住**，是**两个维度**。因此本卡在 `temporal/models.py` 另立四态，**绝不去扩 `Version`**：扩了会让 `version_rank()` 直接 `KeyError`（替代态没有"新旧"可排，它根本不是一种发布口径）。映射只在 `version_status_from_fact()` 一处，未知 token 落 `AS_REPORTED`（与 CARD-02 自己的默认一致）。 |
| 2026-09-19 | **三态词表不在本卡重定义**：`DATA_CONFLICT / VERSION_SUPERSESSION / NEW_INFORMATION` 由 CARD-13 的 `CHANGE_KINDS` 拥有，本卡**导入并校验**，替代判定也直接调用 CARD-13 的 `classify_change()`，只消费 `VERSION_SUPERSESSION` 这一个结论。少一处判据就少一处日后走岔的地方。 |
| 2026-09-19 | **增量必须是"真的有界重算"，不能是"全量跑完再裁剪"（本卡最关键的做法）**：CARD-09 的 `mark_stale()` / `deactivate_evidence()` 都会**立刻跑一次全量传播**（它 §6 要求"撤销后马上读到新状态"）。若在这里调它们，"增量"就退化成"先全量一遍再增量一遍"，卡 §16 要对比的 `nodes_recomputed` 也就没有意义。CARD-09 的文档串明确写着**`stale_claims` 这个列表**才是留给 CARD-25 的接缝 —— 所以本卡**直接往列表追加标记、往字段写状态**，把传播交给随后**唯一一次**有界重算。有界重算与全量必然同结果的理由（闭包外的节点上游不变）写在 `recompute_within` 的文档串里，且**每次现场对账**。 |
| 2026-09-19 | **替代语义定为 STALE 而非 BLOCKED**：卡 §7 表写的是"依赖 claim stale/blocked"。本卡取 **stale**，因为替代与删除是两回事 —— 更正值**存在**，只是旧值退出了当前口径，所以依赖它的结论是"**需要按新值刷新**"，不是"没有来源"。真正的 BLOCKED 留给"证据被删除且无替代"（那走 CARD-09 的 `deactivate_evidence`，不是本卡的事件类型）。实测确认：`supported → stale`。 |
| 2026-09-19 | **受影响子图起初漏了 Thesis，被自家校验器当场抓住**：子图由 `claim → 依赖` 的邻接表做 BFS 得到，而 **Thesis 是 claim 的消费者、不在那张表里**，所以它永远走不到。但 `state_diff` 里会报 Thesis 的状态变化 ⇒ **违反卡 §13 的"`affected_subgraph` 是 `state_diff` 的超集"**。修法：报告里把被改动的 Thesis 并入 `affected_subgraph`。教训沿用 CARD-24：**校验器不是走过场，它是把"自己没想到的东西"逼出来的地方**。 |
| 2026-09-19 | **一个替代者会同时替代多条旧值，而 `supersedes` 是单值的**：`annual_04` 的 `operating_cost/2025FY` 就有两条旧值被同一个 RESTATED 值替代。初版实现让"最后写入者获胜"，于是**先建立的那条反向边被悄悄覆盖** —— 正是卡 §13 第一条禁止的物理覆盖，被集成测试抓到。定案：`superseded_by`（写在**旧值**上）是**权威且完整**的映射；`supersedes`（写在**新值**上）只记**主前驱**且**首写胜出、绝不覆盖**；要看全部前驱用新增的 `predecessors()`。字段保持卡的标量定义，信息不丢（完整映射在旧值那一侧）。 |
| 2026-09-19 | **`facts_superseded` 方向搞反（真实 bug）**：`impact_analysis` 里拿条目的 `superseded_by` 去比 `newly_superseded`（装的是被替代的**旧** id）—— 那个字段存的是替代者，方向正好相反，于是**一条都数不到，恒为 0**。改为比 `fact_id`。是探针（不是测试）先看见 `facts_superseded: 0` 才发现。 |
| 2026-09-19 | **`calculations_stale` 不能用"子图里可达的计算节点"数**：CARD-24 已经查明计算节点的**输入证据在传播上不承重**（`CalculationNode.validation_status` 由 CARD-03 在构图期定死），所以 `证据 → 计算` 这条边在图里**根本不存在**，按可达性数**恒为 0**。改为从 `claim → calculation_ids`（这条边真实存在）数"受影响 claim 所依赖的计算"。 |
| 2026-09-19 | **`event_date` 选择"允许留空 + warning"而不是"构造期必填"**：初版强制非空，结果由 `changed_node_ids` 合成事件时只能编一个日期 —— 而**编出来的日期会被下游当成排序依据**（卡 §12 边缘 4 要按时间排序）。改为允许留空，由引擎留 `event_date_missing` warning，让"没能按时间排序"**看得见**而不假装排过。 |
| 2026-09-19 | **"一件事都没变"时图外域报可证的 0**：空报告里 `valuations_stale` / `memo_sections_affected` 起初也报 `None`，让空报告变成 `partial` —— "什么都没发生"看起来像"算不出来"。改为：`state_diff` 为空且无替代发生时，外部域给 **0**（没有状态迁移 ⇒ 没有下游依赖被触碰，这是**可证**的 0），且不留 warning。有变化时仍严格 `None` + warning。 |
| 2026-09-19 | **图外域（估值/备忘段）不猜**：它们在图上没有依赖边（分别属 CARD-08 / CARD-10），本卡看不到那张映射表。做法：调用方给出**确定受影响**的 id 才计数并产出 `refresh_valuation` / `refresh_memo_section` 动作；不给就报 `None` + warning + `status=partial`。**报 0 会把"没有估值受影响"和"我们根本没看估值"说成同一件事。** |
| 2026-09-19 | **新增 `src/validation/temporal_validator.py`（11 条规则）**：① append-only（被替代事实的**替代前**状态必须仍在血缘历史里）② state_diff 可回溯且 **before ≠ after**（no-op 是噪音）③ `affected_subgraph ⊇ state_diff` 且含 `changed_nodes` 与**被替代旧 id** ④ **增量 == 全量**：不一致就必须已回落全量、必须标 `fallback`、必须留 warning；回落时 `nodes_recomputed == full_nodes`；回落过就不许报 `ok` ⑤ **`affected_subgraph` 可复现**（给图时按 `changed_nodes` + 被替代旧 id 现场重跑限深 BFS 比对，>400 claim 跳过并留 warning）⑥ 血缘引用完整 + 无环 ⑦ 三态未压成一个（`EVENT_EFFECTS` 三值互异）⑧ impact 六键齐全 ⑨ 动作词表合法（无自由文本）⑩ 状态自洽（`unavailable` 必带原因；`unavailable` 不得同时有 `state_diff`）⑪ 缺输入 → warning。**`unavailable` 报告不要求"增量对账"** —— 它本来就没做传播，要求它就是把自己的合法结果判成缺陷。 |
| 2026-09-19 | **工具 `revalidate_cn_claims`**（入参 `document_id` 必需 + `event` 或 `changed_node_ids` 二选一；可选 `max_depth` / `valuations` / `memo_sections` / `ingested_at` / `include_validation`）。非法事件 → `temporal_invalid_event`；缺参 → `temporal_missing_argument`；未知文档 → `document_not_found`。已注册进 `build_cn_snapshot_tools`（两个分支共用 `document_tools`）。`changed_node_ids` 走合成事件时 `event_id` 由 id 摘要得出（**确定性**，不掺墙钟）。 |
| 2026-09-19 | **卡要求加载的 `financial-data-provenance` 技能在本仓库不存在**（项目级只有 `fintrace-card-flow` / `fintrace-add-model-provider`，用户级为空，市场缓存里也没有）。未阻塞：本卡的出处纪律直接绑在仓库自己的契约上 —— CARD-04 的重述信号、CARD-13 的三态与裁决、CARD-09 的 append-only 撤销事件、以及本卡的血缘四态与 `superseded_by` 完整映射。**如实记录，不假装加载过。** |
| 2026-09-19 | **回归**：`tests/test_cn_temporal.py` **84 passed**（collect 实测）；全量离线 **1371 passed / 23 failed / 2 skipped**（junit `errors=0`），失败集合 **14/6/2/1** 与本轮之前逐条一致，passed 增量 1287 → 1371 = **+84** = 本卡用例数；冒烟 14/14 clean；`git diff --check` 无输出。 |
| 2026-09-19 | **未闭合**：① §16 ablation 的运行器与指标口径属 CARD-11（对比臂 `full_recomputation` vs `incremental_affected_subgraph` 的机制本卡已交付：`compare_full_vs_incremental` 三个数都在）；② §17「点更正公告 → 看状态传播」的工作台交互归 CARD-15；③ **公告驱动的替代路径**受语料限制不可用（见上文断头路），demo 用同文档重述血缘；④ 多事件的**并发/乱序**只做到"按调用顺序依次传播"（`event_date` 留空时有 warning），跨事件的全局时间线排序未做；⑤ 「全部测试离线通过」待 Tushare 快照重建。 |
| 2026-09-20 | **§17「点更正公告 → 看状态传播」已落地（由 CARD-15 施工，本卡记账）**：新增「时间重验证」页（`workbench/demo/temporal.py`）。两处按本卡语义实现：① 演示事件是**同指标跨期的一次替代**（`_supersession_pair()` 只在同一 metric 的节点里按期间排序取相邻两期）——任意两条证据凑不出"替代"，替代的语义就是同口径上新的顶掉旧的；挑不出对就**如实报缺**，不硬凑一对假血缘（`profit_continued` 这份语料上确实挑不出，页面显示原因）；② `VERSION_SUPERSESSION` 必须同时给旧节点（`supersedes`）和新节点，只给一边引擎只能拒答。三态血缘与「全量 vs 增量」对账照抄引擎输出。 || 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§18「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-09-27 | **§16 ablation 的口径缺口已定位（结论：缺定义，不缺运行器）**。机制本卡早已交出（`compare_full_vs_incremental` 同时给 `nodes_recomputed` / `full_nodes` / diff）。**但卡 §16 要求三个数都报**（`nodes recomputed` / `latency` / **`same deterministic result`**），而 `ABLATION_COMPARISONS` 只登记了 `nodes_recomputed` 一个 —— **只填它满足登记项却违反卡**。两条张力必须先决定再动手：① `latency` 是墙钟，而 CARD-11 §7b 规定消融无时钟以保证逐字节可复现（本项目有实测教训：`83s vs 40s` 实为冷机首建，四路对照 40.04 vs 41.29 无差异）——故 latency 要么作"单次机器相关值 + 显式注记、**不入冻结对照**"，要么如实不报；② `nodes_recomputed` 是**计数且越小越好**，与 CARD-11 那两条 recall 指标（比例、越大越好）形状不同，`_metric()` 的 recall 形状摘要器不能直接套。详见 CARD-11 §7b-2。 |
