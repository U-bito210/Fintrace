# CARD-09: Claim-Evidence Graph（主张-证据图谱）

| 属性 | 值 |
|---|---|
| 优先级 | P1（核心创新，初赛窗口内完成） |
| 竞赛阶段 | 初赛版（10/7–10/11） |
| 对应赛题 | 全链路的骨架；"事实、推论与观点区分"的直接实现 |
| 依赖 | CARD-01（证据定位）、CARD-03（计算）、CARD-07（run 关联） |
| 实现复杂度 | 高（约 5–6 人日） |
| 状态 | 🟡 初赛版已落地（图谱/传播/校验器/两工具/50 测试全绿；§14 工作台图谱展开页已随 CARD-15 落地，见 §16） |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/graph/`：把 Document → Source Fragment → Evidence → Calculation → Claim → Conclusion 组织为可序列化、可遍历、可在 UI 展开的图谱。实现孤儿 claim / 无支撑计算 / 缺失证据三类检测。这是**六大创新点之首**，也是"点击结论回溯到 PDF 页"的技术底座。

## 2. 对应竞赛评分点

分析逻辑严密性、证据支撑充分性、创新性、事实/推论/观点区分。

## 3. 为什么需要这个模块

"任何结论都有证据链"如果只是口号就是 PPT；做成图数据结构 + 遍历校验 + UI 展开，就是可验证的系统能力。示例（指令原文）：'盈利质量下降' → Claim C18 → CFO/NI=0.61 → Calculation CAL12 → CFO Evidence E51 + 净利润 Evidence E37 → 年报 P118 / P96。

## 4. 输入 / 输出

- **输入**：各模块产出的 Evidence / Calculation / Claim 记录（以 run_id 关联）
- **输出**：`ClaimGraph`（序列化 JSON、导出、遍历 API、检测报告）

## 5. 数据模型

```python
@dataclass
class Claim:
    claim_id: str
    claim_type: str          # fact | inference | opinion（认识论维度）
    temporal_status: str     # historical | current | forward_looking（时间维度，与 claim_type 分离）
    text: str
    evidence_ids: list[str]  # fact 必须非空
    calculation_ids: list[str]
    derived_from: list[str]  # inference 必须非空（指向 fact claim_id）
    dependency_metadata: list[dict]  # [{role: REQUIRED|SUPPORTING|OPTIONAL, target_id, target_type: claim|evidence|calculation|assumption}]
    verification_status: str # EXACT_MATCH | ROUNDING_MATCH | NORMALIZED_MATCH | PERIOD_INFERRED | CONFLICTED | UNVERIFIABLE（离散状态，无伪置信分数）
    validation_status: str   # pending | supported | unsupported | blocked | stale
    rationale: str | None    # 可选

@dataclass
class Thesis:
    """投资逻辑：由多个 REQUIRED/SUPPORTING Claim 构成的命题（CARD-24 Fragility 输入）"""
    thesis_id: str
    statement: str
    claim_ids: list[str]
    dependency_roles: dict[str, str]   # {claim_id: REQUIRED|SUPPORTING|OPTIONAL}
    status: str              # supported | weakened | blocked | stale（由传播规则得出）

@dataclass
class Calculation:
    calculation_id: str
    formula: str
    formula_version: str
    inputs: list[dict]       # 每项 {ref: evidence_id|calculation_id, value}
    output: dict             # {value, unit, period}
    code_version: str
    validation_status: str

@dataclass
class ClaimGraph:
    run_id: str
    claims: dict[str, Claim]
    theses: dict[str, Thesis]
    calculations: dict[str, Calculation]
    evidence_index: dict     # evidence_id → {document_id, page, bbox | snapshot}

    def trace(self, claim_id) -> TraceTree: ...        # 结论→证据→PDF页
    def detect_orphan_claims(self) -> list[str]: ...    # 无 evidence 的 fact
    def detect_unsupported_calculations(self) -> list[str]: ...  # 输入缺证据的计算
    def detect_missing_evidence(self) -> list[str]: ... # evidence_id 悬空
    def propagate_state(self, changed_ids=None) -> list[dict]: ...  # 见 §7b 传播规则
    def export_json(self) -> dict: ...
```

## 6. API / Tool Contract

```python
def build_graph(run_id) -> ClaimGraph: ...
def validate_graph(graph) -> ValidationReport: ...
    # fact 无 evidence → orphan；inference 无 derived_from → 结构错误；
    # opinion 无假设标注 → 结构错误；证据悬空 → missing

# 证据删除（Demo 高潮三的技术底座）——审计模式：标 invalid 而非物理删除
def deactivate_evidence(run_id, evidence_id, reason) -> str:
    """审计模式：EvidenceLedger 不物理删除，而是添加一条 'inactive' 状态记录
    （只追加原则的延伸：删除也是审计事件）。
    返回 revalidate 后的 impacted claim_id 列表。
    触发：revalidate(run_id) 增量重跑。"""

def revalidate(run_id, changed_evidence_ids=None) -> RevalidationReport:
    """全图增量重验证：
    - 受影响的 Claim validation_status 重计算（blocked/invalidated）
    - 下游 Claim 若所有 derived_from 的 fact 都 blocked → 级联 blocked
    - 返回受影响 Claim 清单 + 旧→新状态 diff
    UI 重渲染 memo 与图谱：blocked 徽章、memo 对应结论划灰加删除线。"""

Agent 工具 `TraceCnClaimTool`：入参 `claim_id`；出参 `status / trace_tree`（逐层到 PDF 页）。
Agent 工具 `DeactivateCnEvidenceTool`：入参 `evidence_id + reason`；出参 `status / impacted_claims / revalidation_report`。

## 7. 金融逻辑

三类绑定规则（强制）：
- **fact**：必须直接绑定 Evidence；数值型 fact 必须绑定 Calculation 或 Evidence 数值
- **inference**：必须能追踪 derived_from 的 fact（如诊断信号 → inference claim）
- **opinion**：必须显式标注假设来源（assumption_id 或"分析师主观判断"声明）

诊断信号（CARD-03）进图时是 inference，其 derived_from 指向指标 fact——**信号不得被当作确定因果写入 fact**。

## 7b. Dependency Semantics 与状态传播（fail-closed，v3.2 强化）

**依赖语义（dependency_metadata 的 role）**：REQUIRED / SUPPORTING / OPTIONAL。默认重要分析链路必须明确 REQUIRED。

**传播规则（propagate_state 实现，离散状态不引入伪置信分数）**：

| 上游状态 | REQUIRED | SUPPORTING | OPTIONAL |
|---|---|---|---|
| BLOCKED / CONFLICTED | 下游至少 STALE / BLOCKED | 覆盖度降级（support completeness↓），不自动 blocked | 不影响核心有效性 |
| STALE（superseded/过期） | 下游 STALE | 下游标注 stale 提醒 | 不影响 |
| 正常 | 正常 | 正常 | 不影响 |

- SUPPORTING Evidence 失效 **不一定阻断 Thesis**，但应降低 support completeness（记录 `coverage_dropped`）
- Thesis 状态由传播得出：任一 REQUIRED claim blocked → thesis blocked；所有 REQUIRED 正常但 SUPPORTING 缺失 → weakened；涉及过期 → stale
- **不得使用未经校准的 0.83 / 0.72 等伪置信分数**；一律用离散状态
- 时间维度（新公告/更正/重述）驱动同一传播，具体触发器与增量逻辑见 CARD-25

## 8. Edge Cases

1. 一条 inference 依赖多条 fact（多证据结论）→ derived_from 列表全量记录
2. 链上某证据被删除（失败注入 #6）→ 该 claim validation_status=blocked，下游全部标记
3. 环引用（A 依赖 B 依赖 A）→ 构建时报错拒绝
4. 证据版本更新（更正公告）→ 图保留新旧链，冲突处理记录引用 CARD-13 的 conflict_id

## 9. Failure Mode

图构建失败 → 该 run 标记 graph_incomplete，报告输出缺口，不产出半张图装完整。

## 10. Validator Rules

- 三类绑定规则全覆盖检测（第 7 节）
- trace() 必须能到达文档页或快照 available_at（可达性断言）
- 检测报告三项（orphan/unsupported/missing）每次 run 必产出

## 11. Unit Tests

- 指令原文示例图（C18→CAL12→E51/E37→P118/P96）构建与 trace
- 三类绑定规则正反例
- 证据删除 → blocked 传播
- 环引用拒绝；序列化/反序列化保真
- REQUIRED/SUPPORTING/OPTIONAL 传播规则表驱动测试（含 CONFLICTED → BLOCKED 路径）
- Thesis 状态推导（supported/weakened/blocked/stale）各一例

## 12. Integration Tests

全主链跑完后自动建图 → 检测报告零 orphan/unsupported/missing（好数据场景）；失败注入场景对应标记正确。

## 13. Benchmark

Evidence Coverage / Unsupported Claims 指标接入 CARD-11（由图检测自动计算）。

## 14. Demo Method

Demo 压轴：点击备忘录任一结论 → 图谱逐层展开 → 落到 PDF 页。再删除一个 Evidence → 重跑 → 结论变 blocked。**这一幕是整个 Demo 的灵魂**。

## 15. 验收标准

- [x] 图构建/trace/三类检测全部有测试
- [x] fact/inference/opinion 绑定规则 validator 强制
- [x] 序列化导出与 UI 展开链路打通
- [x] 失败注入 #6（删证据→claim blocked）可现场复现
- [x] 全部测试离线通过 ← **原未勾**：CARD-09 新增 50 测试全绿，但全量离线游标仍有 **23 项既有失败** → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）
      （全部来自缺失的 Tushare 快照，非本卡引入）。判据 = **失败集合逐条不变**，已核对与基线一致。
      ⚠️ 措辞修正：「序列化导出」已完成；**UI 展开链路（§14 工作台图谱页）未接**——见 §16 未闭合项。

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | 新建 `src/cn/graph/`：`models.py`（`Claim`/`Thesis`/`DependencyRef`/`CalculationNode`/`EvidenceNode`/`TraceNode`/`DetectionReport`/`RevalidationReport`/`ClaimGraph` + 全部词表常量）、`builder.py`（`build_graph`/`build_graph_from_analysis`/`claims_from_analysis`/`evidence_index_from_ledger`/`detect_cycles`/`assert_acyclic`）、`propagation.py`（§7b 表驱动传播 + `downstream_closure`）、`revalidation.py`（`deactivate_evidence`/`revalidate`/`mark_stale`）。校验器 `src/validation/graph_validator.py`（六类规则）。工具 `TraceCnClaimTool`（`trace_cn_claim`）与 `DeactivateCnEvidenceTool`（`deactivate_cn_evidence`）接入 `build_cn_snapshot_tools`。测试 `tests/test_cn_graph.py` **50 项全绿**。 |
| 2026-09-19 | **偏差 1（复用而非重造）**：§5 给了一张独立的 `Calculation` 模型（含 `formula_version`/`code_version`/`validation_status`）。按 MASTER_PLAN §8「不得重复造」+ 项目约定，**CARD-03 `CalculationRegistry` 仍是唯一计算器**；graph 里存的是 `CalculationNode`——由 CARD-03 记录构造的**可序列化视图**。`formula_version` = `"card03.v1"`（版本化 operand/operation **模式**，非 git commit）；`code_version` 缺省 `"unknown"`，**绝不编造 git hash**。 |
| 2026-09-19 | **偏差 2（derived fact 的证据展开）**：§7 要求「fact 必须直接绑定 Evidence」。CARD-03 的导出型指标（`free_cash_flow` 等）`MetricResult.inputs` 里只有**计算 id**、没有直接证据 id。若照字面判定，这些合法指标会被打成 orphan。做法：`claims_from_analysis` 把该指标所用计算的**证据操作数（传递闭包）读出来**回填进 `evidence_ids`（常量跳过、不可解析的计算跳过），于是「直接绑定证据」成立且不新增任何来源。`detect_orphan_claims` 因此仍是严格版（无 `evidence_ids` 即 orphan）。 |
| 2026-09-19 | **偏差 3（`MetricResult.inputs` 的二分）**：`inputs` 既可含 CARD-02 规范化事实 id、也可含 CARD-03 中间计算 id。构建时按**注册表实际认识谁**二分（`known_calculations`），否则一个计算 id 会伪装成悬空证据。有专门测试守此不变式。 |
| 2026-09-19 | **偏差 4（角色覆盖语义）**：`evidence_ids`/`calculation_ids`/`derived_from` **默认 REQUIRED**（它们就是 §7 绑定规则本身）；显式 `dependency_metadata` 条目**覆盖**该目标的角色（故 inference 可把某条 fact 声明为 SUPPORTING）；同一目标重复登记时取**最严角色**（防止靠重列降级）。 |
| 2026-09-19 | **偏差 5（stale 是外部触发）**：§7b 的 STALE 无法从图结构推出（来自更正公告/更新披露）。故新增**只追加**的 `ClaimGraph.stale_claims` 标记 + `mark_stale()`，传播读它。这是留给 **CARD-25 Temporal Revalidation** 的接缝；文档已注明。 |
| 2026-09-19 | **偏差 6（revalidate 的 diff 基线）**：§6 要 `deactivate_evidence` 返回「revalidate 后的 impacted 列表」，即撤销后立刻读到 blocked。故 `deactivate_evidence` 内部**当即传播**；为保证随后 `revalidate()` 仍能报出真实旧→新 diff，撤销记录携带 `status_before` 快照，`revalidate` 以**最早快照**为 diff 基线（否则第二次传播会「看起来没变」）。 |
| 2026-09-19 | **偏差 7（`deactivate_evidence` 形参）**：§6 写的是 `(run_id, evidence_id, reason)`。因 run 级图谱持久化归 CARD-12，本卡实现为 `(graph, evidence_id, reason)`——`run_id` 在 `ClaimGraph.run_id` 上。返回 impacted claim ids（下游闭包）。空 reason / 未知证据 id **抛 `GraphError`**，不静默。 |
| 2026-09-19 | **测试**：§11 全部覆盖——指令原文算例（C18→C11→CAL12→E51(P118)/E37(P96)）构建 + trace 到两页；三类绑定规则正反例；撤销→blocked 传播；环引用拒绝；JSON 往返保真；§7b 传播表（REQUIRED 阻断 / SUPPORTING 只降覆盖度 / OPTIONAL 完全不影响 / CONFLICTED→BLOCKED / STALE→下游 stale）；Thesis 四态各一例。§12：`annual_06_equity_shares.pdf` 上 CARD-01→02→03→09 全链 **detection 零 orphan/unsupported/missing 且 validator valid**；引擎三信号（盈利质量/非经常性/应收）进图是 **inference**；失败注入 #6 现场复现。工具用例覆盖 trace/未知 claim/撤回/空 reason。 |
| 2026-09-19 | 全量离线 **814 passed / 23 failed / 0 errors / 2 skipped**，失败集合与基线逐条一致（passed 净增 50）。冒烟 14/14 clean，`git diff --check` clean，新增文件行尾空白扫描无命中。 |
| 2026-09-19 | **未闭合**：§14 工作台「点击结论 → 图谱逐层展开 → 落到 PDF 页」**页面未接** `workbench.py`（服务/工具层 `trace_cn_claim` 已可直接产出 `trace_tree`，含 page+bbox）；§13 Evidence Coverage / Unsupported Claims 指标未接 CARD-11（`ClaimGraph.coverage()` 已就绪）。 |
| 2026-09-19 | **语义澄清（由 CARD-23 施工，本卡记账，无产品改动）**：一条 inference 若**只有 `dependency_metadata` 的 SUPPORTING claim 边、没有 `derived_from`**，它在 `structural_status` 阶段就是 `unsupported` —— 传播**根本没走到 §7b 的 SUPPORTING 那一行**。CARD-23 初版据此形状测试「SUPPORTING 上游失效只降覆盖、不阻断」，测到的其实是绑定规则而非 §7b（`verify_dependencies` 看边、结论是对的，但整条判决会先被 `unsupported` 拉成 BLOCKED）。代码行为经复核**正确、未改**；改的是测试构造（新增 `_supporting_lapse_graph()`：真带 REQUIRED 派生 + 另一条 SUPPORTING 边）并在测试里显式断言前提 `validation_status == supported`。**写下来是因为这个形状很容易被再写一次**：§7b 的 SUPPORTING 行只在 claim 自身**结构上成立**时才有观察意义。 |
| 2026-09-19 | **下游消费者登记**：CARD-23 Financial Claim Passport 是本卡 §7b 的第一个外部消费者 —— 它**不读** `claim.validation_status` 存储值，而是每轮 `verify_claim()` 用 `claim_status()`/`upstream_state()` **重算**，并与护照里记下的快照比对（`verify_validator_state` 的 `drifted` / `validate_proof` 的一致性规则）。含义有两条：① 本卡的传播结果必须是**纯函数**（同图同结果），任何缓存写回都会让护照的重算与原值打架；② 护照是**只读消费者**，不新增、不修改本卡任何状态。CARD-23 已用 `test_withdrawn_evidence_blocks_the_claim_through_the_engine` 与 `test_card09_claim_traces_and_verifies_from_the_same_id` 守这两条。 |
| 2026-09-19 | **语义澄清（由 CARD-24 施工，本卡记账，无产品改动）**：① **本卡传播表里没有 AND 门** —— `claim_status` 遇到**任一** REQUIRED 上游不可用即返回 BLOCKED，`thesis_status` 对 REQUIRED 成员同样是**任一倒下即倒**（合取项）。沿依赖边往上看，整棵树全是 OR 门，因此**不存在冗余路径**：任何单个位于必需祖先链上的节点失效都会一路传到 Thesis。CARD-24 §7B 的「最小割集」据此**恒为单元素集**（卡例 {E01,E02,C02} 成立但非极小），这是本卡 fail-closed 设计的必然推论，不是 CARD-24 的算法选择。CARD-24 已把这条写成不变式并用**完整性规则**（割集必须恰好等于"单独击穿集"，真值由本卡传播现场算出）守住。② **假设边永不阻断**：`upstream_state` 对 `TARGET_ASSUMPTION` 是 `UPSTREAM_OK if target_id else UPSTREAM_MISSING`，而 `resolve_targets` 只产出非空 id，故假设边对判决**恒为 OK**；同时 `detect_missing_*` 也不检查假设。后果是**解析不出的假设 id 会被静默判为 OK**——本卡没有假设索引，判不了"这份假设是否登记过"，所以这不是本卡能独立修的缺陷。CARD-24 因此把假设排除出割集，改由 `identify_fragile_assumptions` 作为"逻辑站不住"报告（不给 CARD-08 注册表时一律记 `unverified`）。若将来要把"未登记的 REQUIRED 假设"判成阻断，需先在本卡引入假设索引。③ **计算节点的输入证据在传播上不承重**：`CalculationNode.validation_status` 由 CARD-03 注册表在**构图时**定下，之后撤掉某个输入证据**不会**改变它，所以"计算 → 输入证据"这条边对 `upstream_state` 无影响。CARD-24 据此**不下钻计算节点**（否则会产出"纸面上能割、实际割不动"的假割集）。 |
| 2026-09-19 | **语义澄清（由 CARD-26 施工，本卡记账，无产品改动）**：CARD-26 把本卡当作**对抗变异的被测目标之一**（`GraphValidatorTarget`），读数位置的选择本身就是一条契约 —— 它读**节点上的 `validation_status`**，**不是**图报告对象上的 `.valid`：后者是整份图的汇总判决，对"**这一条** claim 有没有被拦下"没有分辨率，拿它当检测结果会让所有样本看起来同生共死。另一条同样重要的约束是**引用 → 支撑只映射 fact 的 `evidence_ids`，绝不映射 `derived_from`** —— `derived_from` 是 claim→claim 的推论边，拿它当"支撑"会让**每一条推论看起来都被支持**，于是引用类篡改在图上恒判通过（一个永远不报错的检测器）。实测该目标独立贡献 32 次命中，通路是活的。另：CARD-26 的引用类样本用的是 `annual_04_…pdf#p1` 这种**带 `#pN` 的引用串**，与本卡 `source_references` 的既有格式一致；比对文件名时须先剥掉 `#pN`（CARD-26 侧 `_reference_filename` 负责）。 |
| 2026-09-20 | **§14 工作台图谱展开页已落地（由 CARD-15 施工，本卡记账）**：新增「主张-证据图谱」页（`workbench/demo/graph.py`）。四层逐层展开：结论 → 依赖的计算 → 输入证据 → 原始页码。三个读数与本卡的传播语义保持一致：① claim 的 `propagated_status` 由 `claim_status()` **现场重算**，不是读存储值；② `reasons` 全列（为什么 BLOCKED / 哪条上游不可用）；③ 证据节点带 `page` 才标 `jumpable`，定位不到就显示"不可定位"而非猜页码。跳转所需的 `{id: {document_id, page}}` 索引由页面从图节点构造（规范化事实 `nf_…` id 不在 CARD-01 原始 fact 表里，只查注册表会查不到）。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
