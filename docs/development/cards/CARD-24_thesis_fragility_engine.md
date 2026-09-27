# CARD-24: Thesis Fragility Engine（投资逻辑脆弱性分析）

| 属性 | 值 |
|---|---|
| 优先级 | P1（初赛增强；critical dependency detection 为 P0 可选早段） |
| 竞赛阶段 | 初赛版（10/12–10/15 穿插；或提前与 CARD-09 联动） |
| 对应赛题 | 北京赛：赛题6 反向验证增强（创新三）；华北五省：Graph Algorithms |
| 依赖 | CARD-09（Claim-Evidence Graph + dependency semantics，必须）、CARD-23（护照/验证状态）、CARD-08（假设） |
| 实现复杂度 | 中（约 4 人日） |
| 状态 | 🟡 代码+93 测试就绪（2026-09-19 收尾，全量离线 1287 passed / 23 failed）；§17 Fragility Map 交互已随 CARD-15 落地（2026-09-20）；推论式 Thesis 受语料缺口限制（现用声明式），见 §16 |
| Skill 要求 | 动手前加载 financial-data-provenance |

> 本卡前身：v3.1 的 "Thesis Fragility Map"。v3.2 升级为 **Thesis Fragility Engine**：建立在 Claim-Evidence Graph 之上，实现 Thesis 模型、REQUIRED/SUPPORTING/OPTIONAL 依赖语义、Critical Dependency Detection、Minimal Cut Set、Numeric Fragility（可选）、Monitoring Metrics。**不是 Bull/Bear 打分**。

## 1. Objective（目标）

新建 `src/cn/fragility/`：基于 Claim-Evidence Graph 分析投资逻辑（Thesis）的**脆弱性**——哪些 Claim/Evidence/Assumption 位于多条核心 Thesis 的 REQUIRED 路径上；最少破坏哪些节点会让 Thesis 失效；哪些假设是脆弱的；输出 Monitoring Plan 供后续 Temporal Revalidation（CARD-25）与备忘录刷新使用。

## 2. Competition relevance（双赛分别解决什么评审问题）

| 比赛 | 解决的评审问题 |
|---|---|
| 华北五省计算机设计大赛 | 展示 **Graph Algorithms**：REQUIRED 路径分析、minimal cut set（图论/可靠性分析），非 ML 依赖的确定性图算法 |
| 北京市大学生金融人工智能竞赛 | 回答"分析逻辑/反向验证"：投资逻辑**为什么成立、依赖哪些条件、哪个节点最脆弱、什么变化会使它失效**——把"逻辑严谨性"量化为可展示的图结构 |

## 3. Why this is not a gimmick（为什么不是点缀）

普通研报只有结论，无法回答"你的投资逻辑最依赖什么？"。多 Agent / Bull-Bear 打分只是观点的多空罗列，**不是结构分析**。本引擎的差异：

- 建立在 **Claim-Evidence Graph**（已有依赖语义）之上，输出的是图论意义上可验证的结构结论（关键依赖、最小割集）
- 输出可转为 **Monitoring Plan**（dependency → observable KPI → trigger condition → affected claims → revalidation action），直接驱动 CARD-25 / 备忘录断言更新
- 全部**确定性图算法**，可人工验证正确性

## 4. Inputs / 5. Outputs

- **输入**：`ClaimGraph`（含 Claim/Evidence/Calculation/Thesis 节点 + dependency_metadata）、Assumption Registry
- **输出**：`FragilityReport`（critical_dependencies / minimal_cut_sets / numeric_thresholds(可选) / distance_to_invalidation(可选) / unresolved_assumptions / monitoring_metrics）+ UI 的 **Thesis Fragility Map**

## 6. Data Models（数据模型）

```python
@dataclass
class Thesis:
    thesis_id: str
    statement: str                # 如 "盈利改善具有持续性"
    claim_ids: list[str]          # 构成该 thesis 的 Claims
    dependency_roles: dict[str, str]  # {claim_id: REQUIRED | SUPPORTING | OPTIONAL}
    # 例：T01 依赖 C01 毛利率改善(REQUIRED) + C02 收入增长(REQUIRED) + C03 原材料成本下降(SUPPORTING)

@dataclass
class FragilityReport:
    thesis_id: str
    critical_dependencies: list[dict]   # [{node_id, node_type: claim|evidence|assumption, thesis_ids[], is_single_point}]
    required_dependencies: list[dict]
    minimal_cut_sets: list[list[str]]   # 每个割集为节点 id 列表
    numeric_thresholds: list[dict]      # [{node_id, threshold, current_value, distance_to_invalidation}]  （可选，仅可靠阈值来源）
    unresolved_assumptions: list[dict]
    monitoring_metrics: list[dict]      # [{dependency, observable_kpi_event, trigger_condition, affected_claims[], revalidation_action}]

@dataclass
class MonitoringPlan:
    dependency: str
    observable_kpi_event: str
    trigger_condition: str
    affected_claims: list[str]
    revalidation_action: str
```

## 7. Algorithms（算法——确定性图算法，阶段一不要求复杂 ML）

### A. Critical Dependency Detection（关键依赖检测）
- 遍历所有 Thesis，统计每个 Claim/Evidence/Assumption 出现在多少条 **REQUIRED 路径**上
- 单点依赖（single point of failure）：某节点在所有可达的 Thesis 的 REQUIRED 路径上且无替代路径 → 标记 `is_single_point`

### B. Minimal Cut Set（最小割集）
- 给定 Thesis，求**使其失效所需破坏的最小 REQUIRED dependency 集合**
- 例：T01 依赖 C01 AND C02，其中 C01 依赖 E01 OR E02 → 割集有 {C01, C02}、{E01, E02, C02} 等
- 实现：DAG traversal + cut-set enumeration + graph reliability style analysis
- **初赛限制**：仅 DAG、有限深度（可配置，默认 < 8 层）、小规模 Thesis graph，保证可解释性。**不做 NP-hard 通用求解器**

### C. Numeric Fragility（数值脆弱性，可选）
- 当 Thesis 依赖明确数值条件（如 Gross Margin ≥ 29.4%，当前 31.2%）→ `distance_to_invalidation = 1.8 pct`
- **阈值必须来自**：公开假设 / 分析师明确输入 / 模型或估值约束。**严禁 LLM 凭空创造阈值**
- 无可靠阈值 → 只做 structural fragility，**不伪造 quantitative fragility**

### D. Monitoring Plan 生成
- 每个关键依赖 → 映射到可观察 KPI/事件与触发条件 → 生成 affected claims / revalidation action

## 8. API / Tool Contract

```python
# src/cn/fragility/engine.py
def identify_critical_nodes(graph, thesis_ids) -> list[dict]: ...
def detect_single_points_of_failure(critical_nodes) -> list[dict]: ...
def compute_minimal_cut_sets(graph, thesis_id, max_depth=8, max_sets=20) -> list[list[str]]: ...
def identify_fragile_assumptions(graph, assumptions) -> list[dict]: ...
def compute_numeric_fragility(graph, thresholds) -> list[dict]: ...   # thresholds 来自白名单来源
def generate_monitoring_plans(graph, report) -> list[MonitoringPlan]: ...
def build_fragility_report(graph, thesis_ids, thresholds=None) -> FragilityReport: ...
```

Agent 工具 `AnalyzeCnThesisFragilityTool`：入参 `thesis_id 或 run_id`；出参 `status / fragility_report`。

## 9. Deterministic vs LLM boundary（确定性与 LLM 边界）

- 本卡**全部确定性；零 LLM**
- LLM 可能的角色（不在本卡）：将研报叙事解析为 Thesis 陈述与依赖声明（但依赖角色 REQUIRED/SUPPORTING 必须在 CARD-09 构建时确定并有明确依据）
- 禁止 LLM 生成数值阈值（numeric_fragility 的 thresholds 只接受白名单来源）

## 10. Dependencies（依赖）

CARD-09（前提：Claim Graph + dependency_metadata 已具备）、CARD-23（护照验证状态流入 fragility，refresh 后重算）、CARD-24 → CARD-25（监测计划驱动时间重验证）。

## 11. Failure Modes

- 图规模过大/深度超限 → 返回部分结果并标注 `pruned`，不假装全局最优
- 阈值为空 → `numeric_thresholds = []`（如实为空，不编阈值）
- 割集枚举超预算 → 截断 + 标注 `cut_sets_truncated`

## 12. Edge Cases

1. Thesis 只有 SUPPORTING 依赖、无 REQUIRED → 无割集（无单点），报告如实说明
2. 图含环 → CARD-09 构建时已拒绝，本卡断言无环
3. 同一节点属于多 Thesis → critical 计数累加
4. 数值条件当前处于边界（等于阈值）→ distance_to_invalidation = 0 且标记 `at_threshold`

## 13. Validator Rules

- 每个 minimal cut set：移除集合内节点后 thesis 必须 BLOCKED（**反解断言测试**）
- numeric_thresholds 每项必须有来源类型标注（public_assumption | analyst_input | model_constraint）
- FragilityReport 必须包含 unresolved_assumptions（允许为空数组）

## 14. Unit Tests

- Critical dependency 计数正确性（人工构造小图）
- Minimal cut set：C01 AND C02 / E01 OR E02 反解断言；深度限制；环拒绝
- 单点依赖（唯一路径）检测
- Numeric fragility：阈值来源白名单；无阈值 → 空数组；at_threshold 边界
- Monitoring plan 生成（字段完整）

## 15. Integration Tests

CARD-09 build_graph → CARD-24 分析 → 输出与人工预期一致；异常后 refresh → fragility 报告更新。

## 16. Benchmark

**不是测"准确率"**。至少提供：人工构造 Thesis graph 验证 **critical dependency correctness / cut-set correctness / state propagation correctness**（正确性断言回归集）。接入 CARD-11。

## 17. Demo Flow

Demo 步骤 10b（DEMO_FLOW.md）：打开 Thesis Fragility Map → 展示关键 REQUIRED dependency（如"盈利改善持续性"依赖毛利率改善）→ 点击关键节点展示 Monitoring Plan → 人为删除/替换关键 Evidence → Thesis 状态从 VERIFIED 传播为 STALE/BLOCKED。

## 18. Acceptance Criteria（验收标准）

- [x] critical_dependencies / minimal_cut_sets / monitoring_metrics 全部有测试
- [x] cut-set 反解断言全通过
- [x] 零 LLM（代码断言）
- [x] 阈值来源白名单机制有测试
- [x] 与 CARD-23 VERIFIED→STALE/BLOCKED 传播联调通过
- [x] 全部测试离线通过 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

**收尾说明（2026-09-19）**：共 93 个用例（`tests/test_cn_fragility.py`），全绿。
逐条对账：

- **critical_dependencies / minimal_cut_sets / monitoring_metrics**：三者各有单测，
  且 critical 的递归性有专项（`test_critical_dependencies_list_every_required_ancestor`、
  `..._records_which_theses_need_it`、`..._shared_by_two_theses_counts_both`）。
- **cut-set 反解断言**：`test_every_cut_set_passes_the_reverse_assertion` 对**每一个**
  割集跑一次传播反解；校验器里还有独立实现（`_check_cut_sets`，不看引擎任何字段）；
  另有**完整性**规则把"单独击穿集"的真值用引擎现场算出来比对（见 §23 第 3 条）。
- **零 LLM**：源码级断言（扫 `src/cn/fragility/*.py` 与
  `src/validation/fragility_validator.py`，复用 `check_source_is_model_free`），
  不是查 import 图 —— 函数体内的延迟 import 对 `sys.modules` 是隐形的。
- **阈值来源白名单**：白名单在**构造期**生效（非法 `source_type` 直接抛
  `FragilityError`），数据层面的"来源 id 解析不了"则剔除并记账
  （`rejected_thresholds`），两条路径都有测试。
- **与 CARD-23 联调**：护照五态经 `verification_statuses` 流入报告，
  三态（VERIFIED / STALE / BLOCKED）各有测试；汇总用 CARD-23 的
  `STATUS_PRECEDENCE` 取**最严重**（rank 0 = BLOCKED），不重定义一套序。
  施工中此处真出过一次错（取反成"最宽松"，会把 BLOCKED 汇总成 VERIFIED），
  已在 `_worst_status` 的文档串里写明原因。
- **未勾的第六项**：与其余各卡同因 —— 离线套件里有 23 项失败源自缺失的本地
  tushare 快照（`data/snapshots/cn/`），**不得伪造快照修绿**。本卡收尾游标为
  **1287 passed / 23 failed / 2 skipped**，失败集合 14/6/2/1 与本轮之前逐条一致
  （passed 增量 1194 → 1287 = +93 = 本卡用例数）。

## 19. Priority

**P1**（critical dependency + 单点检测可在 10/7 前备好；minimal cut set 为 10/12–10/15 段）；不裁剪 P0 主链。

## 20. Competition Stage

初赛：structural fragility（critical dependency / cut-set / monitoring plan）；决赛：numerical fragility optimization、minimal cut set 复杂度增强、advanced visualization。

## 21. Estimated Complexity

约 4 人日（critical dependency 1.5 + cut-set 1.5 + numeric/monitoring 1）。

## 22. Fallback / Degradation Strategy

- 时间不足裁剪：minimal cut set 降为"单层割集"（只对 Thesis 的直接 REQUIRED 依赖枚举），标注范围
- 没跑出结果 → 报告标注 `fragility_unavailable`，保证不阻断主链
- 断网不影响（全部本地确定性）

## 23. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **开工前可行性核查（技能第 2 步，别跳）**：实测 13 份已注册语料 → 只有 3 份产出 claims（各 2 条 fact），**推论数 0**（CARD-03 诊断信号需要应收/存货/非经常性损益，现有语料没有），`Thesis` 更是**全仓库只有 CARD-09 的测试手工构造过**，真实管线恒为空。即：**卡假设的输入（Thesis）在管线上不存在**。因此本卡必须自己回答"theses 谁放进去" —— 见下条。 |
| 2026-09-19 | **新增 `src/cn/fragility/theses.py`（卡未要求，但不开工不下去）**：① `theses_from_declaration` 声明式，逐条校验 claim 存在性/角色/陈述非空，**任何一处不成立即抛**；② `theses_from_inferences` 把图里每条 inference 提升为 Thesis（`claim_ids=(该 inference,)` + REQUIRED）—— CARD-03 的诊断信号进图时本来就带 `derived_from`，那是"结论 + 它的推导来源"，**重新贴标签而非新造判断**；③ `resolve_theses` 选择器，**永远报告选了哪条路**（`thesis_source`：declared / derived_from_inferences / none）。三条都没有时工具返回 `status="unavailable"` + `unavailable_reason="no_theses_declared"`，不给一份"看起来干净"的空报告。 |
| 2026-09-19 | **§7B 的最小割集与 CARD-09 传播表冲突（本卡最重要的偏差）**：卡 §7B 的示例是「C01 依赖 E01 **OR** E02 → 割集 {E01,E02,C02}」，预设证据层有 OR 语义。但 CARD-09 是 **fail-closed**：任一 REQUIRED 上游不可用即阻断，**没有 AND 门也就没有冗余路径**；且 `thesis_status` 对 REQUIRED 成员是**任一倒下即倒**（合取）。于是"使其失效所需破坏的最小集合"恒为**单元素集** —— 必需祖先链上任何一处被打掉，逻辑即失效。实现按真实语义返回单元素割集，卡里那个 {E01,E02,C02} 是**成立但非极小**的（其真子集已可割断）。**该偏差由校验器逼出来**：第一版按"每个成员都要倒下"去枚举极小命中集，产出一堆两元素割集，校验器的极小性规则当场判它们非极小 —— 是校验器先发现规则错了，不是测试。 |
| 2026-09-19 | **因此 `max_sets` 的语义变了**：卡给 `max_sets=20` 是按指数级枚举估的预算；割集恒为单元素后条数 = 必需祖先节点数，真实文档轻松超过 20，会让每份报告都落进 `cut_sets_truncated`。`DEFAULT_MAX_SETS` 放宽到 200，**截断标注照旧保留**（截断本身如实上报，只是不再天天触发）。`DEFAULT_MAX_CANDIDATES` 随枚举一起删除，不留死参数。 |
| 2026-09-19 | **`is_single_point` 的口径（卡 §7A 原话：「在所有可达的 Thesis 的 REQUIRED 路径上且无替代路径」）**：实装为「该节点落在**每一个** REQUIRED 成员的必需祖先集交集里」，即**一击同时打断全部必需依赖**。它是最小割集的**真子集**，也是"最省力的那一击"：`minimal_cut_sets` 给"哪些节点单独就能击穿它"，`is_single_point` 给"哪些一击能让所有依赖同时断链"。两者对单成员 Thesis 重合（链上处处是单点，如实反映"无冗余"），对多成员 Thesis 则不重合 —— 这个差距就是逻辑稳健度的度量。已 BLOCKED 的 Thesis 不再标单点（把"当前的脆弱点"和"昨天的事故"混为一谈是错的）。 |
| 2026-09-19 | **§12 边缘 4（当前值恰在阈值上）**：`abs(distance) <= 1e-9 * max(1, |threshold|)` 视作 0.0 并置 `at_threshold=True`，距离精确写 0.0 而不是 1e-16。卡 §7C 的算例（毛利率 ≥ 29.4%、当前 31.2% → 1.8 pct）有专项测试逐字复现。 |
| 2026-09-19 | **§7C「严禁 LLM 凭空创造阈值」做成两层**：① `FragilityThreshold.__post_init__` 对非白名单 `source_type`、空 `source_ref`、非法 `comparison`、以及"给了 `current_value` 却不给 `current_value_source`"**直接抛错**（类型问题根本构造不出来）；② `source_ref` 解析不了（图上/假设注册表里都没有）则**剔除并记账**到 `rejected_thresholds`，同时 `status="partial"`。两条路径都有测试。另有"单位真不匹配 → 不评估"（`unit_mismatch`）与"单位有一边没声明 → 照算但 `unit_verified=False`"的不对称：**只在能证明不匹配时拒算，不在无法核对时假装核对过**。 |
| 2026-09-19 | **可以拿来说明「没有阈值就不造数字」的细节**：`evaluable=False` 时 `distance_to_invalidation` 严格为 `None`，没有"默认 0"也没有"用 0 表示未知"。Monitoring Plan 的触发条件因此分 `numeric` / `structural` 两类且**不混用**：没有白名单阈值就只给结构触发（"被撤回/被取代/无法解析 → CARD-09 判 BLOCKED 或 STALE"），不编一条触发线。未映射 KPI（节点自己没声明 metric）如实写 `mapped=False` + `unmapped_reason`，并进 warnings。 |
| 2026-09-19 | **假设不走割集（CARD-09 语义澄清）**：§7b 里 `TARGET_ASSUMPTION` 恒返回 OK，**假设边永远不阻断**，所以假设不进 `minimal_cut_sets`，而是走 `identify_fragile_assumptions` 作为"逻辑站不住"报告。不给注册表时每条假设都是 `status="unverified"` + `reason="no_assumption_registry"` —— **"没核实"不等于"没问题"**。已回填 CARD-09 §16。 |
| 2026-09-19 | **构造期真错一次（记下来当教训）**：`_worst_status` 用 CARD-23 的 `STATUS_RANK` 汇总时写了 `sorted(..., reverse=True)` 取最大 rank —— 而 `STATUS_PRECEDENCE` 是**从严重到宽松**排的（rank 0 = BLOCKED），于是 BLOCKED 会被汇总成 VERIFIED。这正是"护照状态流入"最危险的错法（把已阻断渲染成已通过）。已改为 `min(...)` 并在文档串里写明"rank 0 才是最严重"。单测 `test_verification_view_rolls_up_with_card23_precedence` 覆盖。 |
| 2026-09-19 | **新增 `src/validation/fragility_validator.py`（§13 + 不外露的交叉不变式）**：① 反解断言；② **极小性**（任何真子集都不得同样是割集）；③ **完整性**：割集必须**恰好**是"单独移除即可击穿该 Thesis"的全部节点，真值由引擎现场跑传播算出、不看报告任何字段（这条把上面那个"单元素割集"结论钉成不变式，也是抓出第一版枚举错误的规则）；④ 阈值来源白名单 + 来源解析 + **距离逐格可重算**（仿 CARD-08 的敏感性矩阵做法）；⑤ `unresolved_assumptions` 键必须存在（允许空数组）；⑥ 单点必须真有一个单元素割集兜底；⑦ 割集节点必须可解析；⑧ `unavailable` 必带原因、截断时状态不得是 `ok`；⑨ 缺输入 → warning（`numeric_fragility_absent`、`assumption_registry_absent`），绝不静默通过。**完整性规则有规模闸门**（>80 节点跳过并留 warning），避免大图上 O(N) 次传播。 |
| 2026-09-19 | **工具 `analyze_cn_thesis_fragility`**（入参 `document_id` 必需；`thesis_id` / `theses` / `thresholds` 可选；`include_verification` 默认 true，会把 CARD-23 五态汇入报告；`include_monitoring` 默认 true）。已注册进 `build_cn_snapshot_tools` 两个分支。阈值非法 → `fragility_invalid_threshold`；thesis 引用悬空 → `fragility_invalid_thesis`；无 Thesis 可分析 → `status="unavailable"`。护照状态有 50 条上限并如实标注 `capped_at`。 |
| 2026-09-19 | **回归**：`tests/test_cn_fragility.py` **93 passed**；全量离线 **1287 passed / 23 failed / 2 skipped**（junit `errors=0`），失败集合 **14/6/2/1** 与本轮之前逐条一致，passed 增量 1194 → 1287 = **+93** = 本卡用例数；冒烟 14/14 clean；`git diff --check` 无输出。 |
| 2026-09-19 | **未闭合**：① §17 的 Thesis Fragility Map 交互（点击关键节点展示 Monitoring Plan、删证据看状态传播）**归 CARD-15**；② §16 Benchmark 接入 CARD-11（正确性断言回归集已备，指标口径属 CARD-11）；③ **语料缺口**：现有 3 份产出 claims 的语料**没有任何 CARD-03 诊断信号**（缺应收/存货/非经常性损益），所以"推论式 Thesis"这条路在真实语料上暂时是空的 —— 声明式路径可用且已端到端验证。要在 Demo 里展示"图自己长出 Thesis"，需要先补一份带这类附注的年报语料（属 CARD-01/02 语料工作）；④ 计算节点**输入证据**的失效不改变该节点的 `validation_status`（CARD-03 在构图时定下、之后不再校验），因此"计算 → 输入证据"这条边在传播上不承重，本卡据此**不下钻计算节点**——这不是绕过，是拒绝把"纸面上能割、实际割不动"的边算进割集。此口径已写进 `engine._required_targets` 的文档串，若 CARD-09/CARD-03 将来让计算节点可重校验，这里要同步改。 |
| 2026-09-20 | **§17 Thesis Fragility Map 交互已落地（由 CARD-15 施工，本卡记账）**：新增「逻辑脆弱性」页（`workbench/demo/fragility.py`）。**Thesis 来源如实回报**：上文 ③ 记的语料缺口（现有语料没有 CARD-03 诊断信号 ⇒ 解析出的 claim 全是 `fact`、没有 `inference`）意味着推论式路径暂无材料，所以页面**显式声明**一条依赖全部指标事实（REQUIRED）的 Thesis，并把 `thesis_source` 报为 `declared`（不是 `inferred`），页面上直接写明"declared 是什么意思"。割集因此沿 REQUIRED 边下钻到证据层——这与 §7B「最小割集恒为单元素集」一致（CARD-09 无 AND 门）。关键依赖 / 单点失效 / 监控计划三块照抄引擎输出，不二次加工。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§18「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-09-27 | **§16 那行不在卡里 —— 先把口径定清楚再谈数字**。**卡内全文没有 ablation 章节**；`ABLATION_COMPARISONS` 里的 `no_fragility_engine → fragility_engine` 是**占位行**，而卡 §7b 的原话是「Baseline = —（**非测准确率**），实验组 = 人工构造 Thesis graph」——一个"没有引擎"的基线报不出 `critical_dependency_correctness`。本卡 §23 上文的自我定位也与之一致：这是**正确性断言回归集**（93 项 `test_cn_fragility.py` + 校验器 9 条规则，其中**完整性**规则现场跑传播反解真值），不是一个"有/无"对照。**故本条留白是结论**：要先决定它是**正确性核对**（对人工构造的 Thesis 图逐项核最小割集/单点/状态传播）还是**对照实验**，才谈得上填数；**硬塞一个 0 或自造基线比留白更糟**（CARD-11 §7b「禁止编造数字」）。另：`minimal_cut_sets` 恒为**单元素集**（CARD-09 无 AND 门）这条如果要做成指标，形状是"割集规模的分布"而非 recall。详见 CARD-11 §7b-2。 |
