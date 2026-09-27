# FinTrace-CN 竞赛版开发总指挥（MASTER PLAN v3.0）

> 本文档是 FinTrace-CN 面向 2026 年北京市大学生金融人工智能竞赛的唯一进度真相源（single source of truth）。
> 所有模块开发以 `docs/development/cards/` 下的任务卡（CARD）为执行单元。执行任何卡片前，必须先阅读本文档第 8、9、10、11 节。
> 上位规则：`AGENTS.md`（工作区级，永远优先于本文档）。

- 版本：v3.2（编码前冻结版，全量替代 v3.1）
- 制定日期：2026-09-16
- 双赛：华北五省计算机设计大赛 + 北京市大学生金融人工智能竞赛（叙事定位见 DUAL_COMPETITION_POSITIONING.md）
- 初赛提交截止：2026-10-18（项目计划书 PDF + ≤5 分钟视频）
- **Code Freeze：2026-10-15 之后禁止加入任何重大新功能，只允许 bug fix 与材料准备**
- 决赛：2026-11 月底

**v3.2 修订说明**：把创新体系正式 CARD 化并融入现有架构。核心变更：①创新能力与现有 CARD 逐项重叠检查（优先扩展、禁止重复建设）；②创新一升级为 **ACME**（Accounting-Constrained Multimodal Evidence Engine，六类约束族，CARD-22）；③创新二改名 **Financial Claim Passport**（对外"金融AI结论护照"，新增 `verify_claim()` 五态验证，CARD-23）；④Thesis Fragility 升级为确定性图算法引擎（CARD-24）；⑤Temporal Revalidation 升级（version lineage + 三态区分 + 增量重算，CARD-25）；⑥FinFuzz 明确为测试框架并集成 Benchmark（CARD-26 + CARD-11）；⑦Controlled Environment Guard 并入 CARD-01（不单独做卡）；⑧Cross-source reconciliation 扩展 CARD-13、dependency semantics/Thesis 扩展 CARD-09；⑨新增 DUAL_COMPETITION_POSITIONING.md。全部创新卡升级为 22 字段。

---

## 0. 主链（整个文档体系的核心，先看这里）

```text
真实金融文档（年报 / 半年报 / 季报 / 公告 / 研报草稿）+ Snapshot Provider
  ↓ ① Document Intelligence     文档注册 / 解析 / 证据定位 / Controlled Environment Guard（CARD-01）
  ↓ ② Financial Normalization   单位 / 币种 / 期间 / 口径 / flow-stock 语义（CARD-02）
  ↓ ③ ACME Constraint Layer     六类约束族：检测+定位+阻断（CARD-22，创新一）
  ↓ ④ Evidence                  point-in-time，带文档页码 / bbox / available_at
  ↓ ⑤ Deterministic Financial Tools   确定性 Python 金融计算
  ↓ ⑥ Financial Diagnostics     YTD→单季化 / 盈利状态迁移 / 诊断信号
  ↓ ⑦ Claim Extraction + Report Checking   fact/inference/opinion 三级，13 类错误核查
  ↓ ⑧ Valuation                  相对估值 + Bear/Base/Bull 敏感性
  ↓ ⑨ Financial Claim Passport  关键结论 → FPO → verify_claim() 五态（CARD-23，创新二）
  ↓ ⑩ Claim-Evidence / Thesis Graph   结论 → 计算 → 证据 → PDF 页 + 依赖语义（CARD-09）
  ↓ ⑪ Fragility Engine          关键依赖 / 最小割集 / Monitoring Plan（CARD-24，创新三）
  ↓ ⑫ Investment Memo            买方投资备忘录
  ↓ ⑬ Validator（fail-closed）   证据不足 / 冲突 / 口径不一致 → 降级或阻断
  ↓ ⑭ Temporal Revalidation     version lineage + 三态 + 增量重算（CARD-25，创新三）
  ↓ ⑮ Evidence Pack + Audit Replay     可交付证据包 / 可重放审计
（系统外部）FinFuzz 对整个 Pipeline 对抗评测（CARD-26 + CARD-11）
```

**核心价值主张**：任何重要金融数字都有来源，任何重要结论都有证据链，所有计算均可复核；当证据不足、数据冲突或口径不一致时，系统主动降级或阻断结论。FinTrace 不只证明"正确的时候能跑"，更证明"**错误的时候知道自己不能下结论**"。

## 1. 竞赛定位（收敛，不再追求全覆盖）

> **双赛同构**：同一套代码同时参加华北五省计算机设计大赛与北京市大学生金融人工智能竞赛，叙事不同但功能同一。定位细节见 `DUAL_COMPETITION_POSITIONING.md`。本节按北京赛赛题映射（主基线），华北五省按"AI Agent + 多模态 + 约束推理 + 图算法 + 增量计算 + 对抗测试"主线包装。

| 角色 | 赛题 |
|---|---|
| 主方向 | 赛题2 上市公司财务报告分析 |
| 第二核心 | 赛题5 研究报告纠错核查 |
| 最终业务输出 | 赛题6 买方投资备忘录编制 |
| 重要金融工具 | 赛题4 自动化估值建模（初赛=相对估值+敏感性；DCF 决赛） |
| 内部评价机制 | 赛题7 研究报告质量评估（诊断维度制，非虚构权重评分） |
| 升级为系统能力 | 赛题1 → Document Intelligence Layer（全系统数据入口，不独立卖点） |
| 暂缓 | 赛题3 产业链 → P3 决赛可选，不得影响初赛核心链路 |

**禁止**：为了"多 Agent、MCP、Skill、DCF"等技术名词过度工程化；为了覆盖七个赛题堆功能。

## 2. 必须保留的现有资产（不得推翻）

1. Evidence Ledger（证据账本）
2. Validator 体系（financial / research gate / report）
3. Point-in-Time / available_at 设计
4. Snapshot Provider（版本化快照）
5. 确定性 Python 金融计算（相对估值 PE/PB/PS + IQR 离群剔除）
6. fail-closed 原则
7. 复现信息（prompt / model / git commit / snapshot 指纹）
8. Agent Tool Contract（status: ok|error 信封）
9. 测试体系（离线、不依赖 .env / 网络 / 墙钟）
10. `benchmarks/cn_agent_v1.json` + benchmark/real_benchmark/ablation 基础设施

## 3. LLM 边界契约（所有卡片共同遵守）

LLM **可以**：理解任务；制定研究计划；选择工具；从自然语言提取结构化 Claim；检索和综合证据；生成推论；组织报告。

LLM **不允许**：凭空创造金融数据；自己完成最终财务算术并作为事实来源；绕过 deterministic calculation；自行创建未经来源绑定的 Evidence；绕过 Validator；修改验证结果；将 inference 冒充 fact。

## 4. 优先级定义与竞赛阶段

- **P0**：直接影响竞赛核心闭环和现场真实输入能力 → 初赛版
- **P1**：显著提高金融专业度、技术深度和展示效果 → 初赛版（时间不足时按第 5 节顺序裁剪）
- **P2**：决赛增强项
- **P3**：锦上添花，不能影响主链交付

**初赛版锁定范围**（10-18 前必须稳定）：真实文档输入、财务解析、Evidence、财务分析、研报纠错、相对估值、Investment Memo、Validator、Audit Trail、Real Benchmark、稳定 Demo + **创新 P0**：ACME 基础约束族（CARD-22）、Financial Claim Passport + verify_claim（CARD-23）、REQUIRED dependency propagation（CARD-09）。

**决赛版增强**（11 月中旬后）：DCF、Multi-Agent、OCR 强化、复杂 Retrieval、MCP、Industry Chain、完整质量评估、Fragility/Temporal 完整实现、FinFuzz 完整 operator 集。

## 5. 开发时间表（严格执行，10-15 起 Code Freeze）

**排期原则**：全部 26 卡自估 88+ 人日（v3.2 核算，见下）；学生非全职 ≈ 21 个工作日到 10-15。以下排期是**砍超载窗口后的可行版本**——原窗口 5（8–10 人日）已按 4 条并行 Track 摊薄。

**v3.2 新增**：22/23 为 P0 创新核心卡，必须进入初赛关键路径；24/25/26 为 P1 创新增强卡，按并行工作流穿插；11 含 FinFuzz 集成与全创新 ablation。

| 窗口 | 内容 | 卡片 | 说明 |
|---|---|---|---|
| 9/16–9/22（7d） | Document Registry、PDF/表格解析、证据定位、Run Manifest、**Controlled Environment Guard** | **01, 07**（11 同步启动） | Guard 并入 01，不单独排卡 |
| 9/23–9/29（7d） | Financial Normalization（**桥接现有引擎，不重写**）、YTD→单季/TTM 推导、YoY/QoQ、诊断引擎、Accounting Scope、**ACME 基础约束族（F1–F4）** + **Retrieval 基础** | 02, 03, 04, **06**, **22（F1–F4）** | 22 只需 01/02；F5/F6（跨源/跨模态）放入 9/30–10/6 或与 13 联动 |
| 9/30–10/6（7d） | 自然语言研报 → Claim Extractor → Deterministic Checker → 纠错报告、**ACME F5/F6** | 05, **22（F5/F6 后半）** | 05 是主链核心，22 后半不占 05 关键路径 |
| 10/7–10/11（5d） | 相对估值 + 敏感性、Investment Memo、Claim-Evidence Graph、**Financial Claim Passport 基础版** | 08, 09, 10, **23** | 23 依赖 09，与 10 并行 |
| 10/12–10/15（4d） | Real benchmark 收口（**1 家 Demo 公司全链 + 2 家抽查 + 2 家 holdout**）、Failure Injection 场景脚本化、**Evidence Pack 导出（简化版）**、**Thesis Fragility 基础版**、**Temporal Revalidation 基础版**、**FinFuzz 基础版（集成 CARD-11）** | 11（含 ablation）, **12（简化版）**, **24**, **25**, **26** | **Conflict 完整实现 / Replay UI / 24·25·26 完整实现移入决赛缓冲**（若窗口超载） |
| 10/16–10/18（3d） | Bug fix、Demo freeze、陌生公司演练、计划书、5 分钟视频、安装复现测试 | — | 必须演练：陌生公司 PDF 解析 + 断网演示 |

贯穿性卡片：11（基准从第一个模块起持续建设）、15（UI 增量随各卡落地）、14（Prompt Registry 随 LLM 功能落地）。

### 5.1 并行工作流（v3.2 核算，4 条 Track）

**Track A：Document / Normalization / ACME**
- 卡片：01 → 02 → 22 → 04
- 预估人日：6 + 5 + 6 + 3 = 20 人日（22 升级 ACME 六类约束族 +3）
- 关键路径：01（7d）→ 02（7d）→ 22（7d）→ 04（7d）

**Track B：Financial Analysis / Checker / Valuation**
- 卡片：03 → 05 → 08
- 预估人日：5 + 7 + 4 = 16 人日
- 关键路径：03（7d）→ 05（7d）→ 08（10/7-10/11）

**Track C：Graph / Passport / Fragility / Temporal**
- 卡片：09 → 23 → 24 → 25
- 预估人日：6 + 4 + 4 + 5 = 19 人日（23 五态验证 +1，24 cut-set +2，25 增量重算 +3）
- 关键路径：09（10/7-10/11）→ 23（10/7-10/11）→ 24/25（10/12-10/15）

**Track D：Benchmark / FinFuzz / UI / Demo / Submission**
- 卡片：11（含 ablation）→ 12 → 26 → 15 → 14
- 预估人日：8 + 4 + 4 + 4 + 1.5 = 21.5 人日（11 含 FinFuzz 集成 2 + ablation 1）
- 关键路径：11（贯穿）→ 12（10/12-10/15）→ 26（10/12-10/15）→ 15（贯穿）→ 14（贯穿）

**如果实际团队只有 3 名核心开发者**：Track C 和 Track D 合并，24/25 降为决赛增强（或只做 24 critical dependency），26 简化为 5 个核心 operator。

### 5.2 Critical Path 与 Code Freeze

**Critical Path**：01 → 02 → 03 → 05 → 08 → 09 → 10 → 12 → Demo Freeze（创新支线 22/23 并行于主链，不延长主链）

**Code Freeze Date**：2026-10-15 23:59
- 此后禁止新功能
- 仅允许：bug fix、计划书、5 分钟视频、安装复现测试、Demo 演练

### 5.3 工作量核算（v3.2 Scope Check）

| 分组 | 卡片 | 人日 | 合计 |
|---|---|---|---|
| P0 主链 | 01(6) 02(5) 03(5) 04(3) 05(7) 06(3) 07(3) 08(4) 09(6) 10(4) 11(8*贯穿) 12(4) | — | ~58 |
| P0 创新 | 22(6) 23(4) | — | 10 |
| P1 创新增强 | 24(4) 25(5) 26(4) | — | 13 |
| P1 其余 | 13(2.5) 14(1.5) 15(4) | — | 8 |
| P2/P3 决赛 | 16(5) 17(3) 18(6) 19(2) 20(2) 21(5) | — | ~23 |

**初赛（P0+P1）合计 ≈ 89 人日**（含 11 贯穿分散）。21 个工作日 × ≤3 人并行 ≈ 可承载，但 **P1 增强项（24/25/26/13/06）按裁剪顺序执行**：若窗口超载，优先保证 P0（58+10=68 人日）铁定完成。核心业务闭环 → 创新 P0 嵌入 → Benchmark → Demo → 再增强 的顺序不可颠倒。

## 6. 卡片索引与进度台账

> 规则：完成一张卡 → "状态"列打 `[x]`，卡内"执行备注"写入 commit 范围与偏差记录。
> 状态取值：`☐` 未开始 / `🟡` 实现与卡内测试完成、仍有卡内 §16 记录的未闭合事项 / `[x]` 全部验收项通过。

| 编号 | 卡片 | 优先级 | 阶段 | 赛题 | 依赖 | 状态 |
|---|---|---|---|---|---|---|
| 01 | [Document Intelligence Layer](cards/CARD-01_document_intelligence.md) | P0 | 初赛 | 1(入口) | 无 | 🟡 代码+59 测试就绪；反向跳转 UI 已打通（document_service + `/api/documents/*` + 工作台「文档智能」页）；**已增量交付 CARD-04 前置条件**（正文/附注 `text` 事实、表格版本列 `meta["version_label"]`、语料扩到 13 份且字节可复现、`verify_corpus` 真重渲染比对）；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 02 | [Financial Normalization Engine](cards/CARD-02_financial_normalization.md) | P0 | 初赛 | 2 | 01 | 🟡 代码+40 测试就绪；**已增量交付 CARD-04 前置条件**（词表收归 `src/cn/disclosure_lexicon.py`、列级版本 `version_from_column`、`NormalizationResult.source_facts`、`_derive_metrics` 期间语义守卫）；§13 四项准确率未接入 CARD-11 |
| 03 | [Financial Analysis Engine](cards/CARD-03_financial_analysis.md) | P0 | 初赛 | 2 | 02（04 为可选增强信号） | 🟡 代码+74 测试就绪；工具 `analyze_cn_financials` 与工作台「财务分析」页已打通；**已接受 CARD-04 回压**（`AnalysisResult.comparability` / `compute_yoy(suppress_reason=)` / `series.merge_versions` 仅 use_adjusted 生效）；`symbol` 口径按「零静默」边界明确拒绝（待另立 provider→NormalizedFact 卡）；§13 计算准确率未接入 CARD-11 |
| 04 | [Accounting Scope & Restatement](cards/CARD-04_accounting_scope.md) | **P0** | 初赛 | 2 | 01, 02 | 🟡 代码+64 测试就绪（全量离线 532 passed / 23 failed，失败集合与开工前一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；6 类信号 × 3 态决策 × 3 条 Validator 规则全部就位，`use_adjusted` 真取调整后列、`block_yoy` 全文档零百分比；工具 `detect_cn_scope_signals` 与工作台「财务分析」页可比性横幅已打通；§13 Scope Accuracy 未接入 CARD-11；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 05 | [Report Claim Extractor & Checker](cards/CARD-05_report_checker.md) | P0 | 初赛 | 5 | 02, 04 | 🟡 代码+135 测试就绪（全量离线 **691 passed / 23 failed**，失败集合与 CARD-04 时逐条一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；13 类错误全部可达且 planted 集 **Precision/Recall/F1 = 1.0000、FPR = 0.0000**（14 TP / 0 FP / 0 FN / 8 TN）；`checker.py` 零 LLM import 由源码级断言守；`rule_extractor`（离线确定性）与 `extract_claims`（LLM）双提取器共用同一期间策略；工具 `check_cn_report_draft` 已接入；§14 工作台纠错列表已随 CARD-15 落地（2026-09-20，植入清单↔实际检出逐条对齐，漏检显形）；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**）；**§7.1 限界的后半句「行业错配」已于 2026-09-26 落地**（`_industry_multiple_findings`：句子自己声明唯一来源行业 + 草稿唯一目标公司代码 + 冻结申万映射，三者齐备且行业不同才判，缺一即静默；`coverage` 拆成两条分支各自申报 `ran`/`reason`），演示含错草稿逐条对齐由 7/8 升至 **8/8**，`rehearse_demo.py` 13 步「会卡」归零 → 状态仍为 🟡，剩余未闭合项见本卡 §16 |
| 06 | [Retrieval Engine](cards/CARD-06_retrieval.md) | P0 | 初赛 | 链路① | 01 | 🟡 代码+19 测试就绪（全量离线 710 passed / 23 failed，失败集合与 CARD-05 时逐条一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；自研 BM25 + chunk/evidence 分离 + 三型引用校验 + 降级标注全部就位；向量重排按「降级标注」交付（无 embedding provider）；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 07 | [Run Manifest & Audit Trail](cards/CARD-07_run_manifest.md) | P0 | 初赛 | 竞赛硬性 | 无 | 🟡 代码+15 测试就绪（全量离线 **725 passed / 23 failed**，失败集合与 CARD-06 时逐条一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；17 字段 manifest + 九类 stage 只追加 events.jsonl + `replay` 确定性层全等/采样漂移双段 diff 全部就位；CLI 支持 list/show/verify/replay（Demo 现场重放）；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 08 | [Relative Valuation & Sensitivity](cards/CARD-08_relative_valuation.md) | P0 | 初赛 | 4 | 02, 07 | 🟡 代码+42 测试就绪（全量离线 **764 passed / 23 failed**，失败集合与 CARD-07 时逐条一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；三情景（Bear/Base/Bull）× PE/PB/PS 分别展示 + `method_dispersion`（无合成区间）+ Assumption Registry（sourced vs `assumption:`，禁覆盖 sourced）+ `sensitivity_pe_eps`/`sensitivity_pb_bps` 逐格可重算矩阵 + fail-closed（缺总股本阻断）全部就位；§7.3 采选项①（PB 矩阵用 BPS×PB，ROE 不入轴）；校验器 `valuation_validator` 五类规则；工具 `run_cn_relative_valuation` 已接入；§14 工作台敏感性热力图未接（增强项）；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 09 | [Claim-Evidence Graph](cards/CARD-09_claim_evidence_graph.md) | P1 | 初赛 | 核心创新 | 01, 03, 07 | 🟡 代码+50 测试就绪（全量离线 **814 passed / 23 failed**，失败集合与 CARD-08 时逐条一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；`src/cn/graph/` 图谱（Claim/Thesis/Calculation/Evidence 四层 + trace 到 PDF 页）+ 三类检测（orphan/unsupported/missing）+ §7b REQUIRED/SUPPORTING/OPTIONAL fail-closed 传播 + 环引用拒绝 + 审计式证据撤销（只追加，非物理删除）全部就位；校验器 `graph_validator` 六类规则；工具 `trace_cn_claim` / `deactivate_cn_evidence` 已接入；失败注入 #6 离线可复现；§14 工作台图谱展开页未接；§13 Coverage 未接 CARD-11；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 10 | [Investment Memo](cards/CARD-10_investment_memo.md) | P0 | 初赛 | 6 | 03, 05, 06, 08, 09 | 🟡 代码+52 测试就绪（全量离线 **866 passed / 23 failed**，失败集合与 CARD-09 时逐条一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；`src/cn/memo.py` 11 段（执行摘要/公司概览/财务表现/投资逻辑/估值/催化剂/风险/反向论证/证伪条件/持续跟踪指标/证据附录）+ **句级** fact/inference/opinion 标注（`structural` 豁免脚手架，未标注句拒绝）+ Markdown↔JSON 同源渲染 + `template_narrator` 确定性叙述（**无 Multi-Agent 依赖**）+ LLM 叙述器契约（claim_id 白名单 + unverified_number 拦截 + 违规句剔除并记录）全部就位；校验器 `memo_validator` 五类规则（标注完整 / 数值绑定 / 引用可解析（含外部计算注册表）/ quote 逐字 / 证伪可计算）；工具 `generate_cn_investment_memo` 已接入；§14 工作台「点击结论→图谱回溯」UI 未接（与 CARD-09 同一未闭合项）；§13 memo 级指标未接 CARD-11；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 11 | [Real-world Benchmark](cards/CARD-11_real_benchmark.md) | P0 | 贯穿 | 7 | 无（持续） | 🟡 框架+46 测试就绪（全量离线 **912 passed / 23 failed**，失败集合与 CARD-10 时逐条一致；冒烟 14/14 clean）；`src/cn/benchmark_v2.py` **24 项指标计算器**（8 组：抽取/引用/计算/核查/智能体/安全/复现/成本）+ FinFuzz 对抗组 4 项 + 三级注册表（`benchmarks/cn_agent_v2.json`，**v1 未动**）+ **组件归并**（严格：同一指标两条定义即报错，CARD-01/05 既有基线直接复用，不重复测量）+ 真实/离线**路径段隔离闸门**（`isolate_payload`，落盘前强制）+ 环境指纹 + 阈值冻结流程（`THRESHOLDS` 全 `None`，绝不预填）+ ablation 表（未运行一律 `TARGET / TO BE MEASURED`）全部就位；Synthetic 已实测（8 项已测 / 20 项 N/A 逐行写明原因）；采集脚本 `scripts/fetch_benchmark_real.py`（多源 + sha256 失败即报错，绝不写占位文件）与真值规范 `docs/benchmark/LABELING_SPEC.md` 已交付；**Real/Holdout 真实材料未采集**（需网络+授权，且 `data/` 须 gitignore）；§7b 五条 ablation **两条已实测**（2026-09-27：ACME `0.4000 → 1.0000`、Passport `0.2500 → 1.0000`，误报 0；见 `src/cn/ablation_v2.py`），**另三条如实留白 `TARGET / TO BE MEASURED`** |
| 12 | [Evidence Pack & Audit Replay](cards/CARD-12_evidence_pack_replay.md) | P1 | 初赛 | 竞赛硬性 | 07, 09 | 🟡 代码+27 测试就绪（全量离线 **939 passed / 23 failed**，失败集合与 CARD-11 时逐条一致；冒烟 14/14 clean）；`src/cn/evidence_pack.py` **13 类产物**（2 必需 / 8 完整性 / 3 可选，缺件标 `incomplete` + `PACK_STATUS.json` 把缺件清单钉在包内）+ 目录与 zip 双导出 + `require_complete` 拒残包 + 三重校验（存在性 / `manifest.run_id` ↔ `events.jsonl` 一致 / Pack 内引用可解析）+ **11 阶段重放时间线**（五要素齐全，未记录步骤标 `not_recorded` 不省略，失败状态按严重度冒泡）+ 多 run manifest diff（输入不同则标不可比）全部就位；`src/cn/failure_injection.py` **7 场景注册、6 个真实实现**一键可跑（`python -m src.cn.failure_injection`，其中 #6 为 Demo 正片场景）；§14 重放 UI 已随 CARD-15 落地（2026-09-20，11 阶段时间线 + 证据包导出）、场景 #5 source conflict 归 CARD-13；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**）；**2026-09-27：§4「各模块产物」补齐** —— run 目录此前只有 `manifest.json`+`events.jsonl`（3 类输入只到了 2 类），pack 恒缺 8 件，而唯一名为 `claims.json` 的那件是**手写占位符**（断言"本 run 未产生 claim 级产物"，与它旁边那条注册了 5 条 claim 的 run 直接矛盾）。新增 `src/cn/run_artefacts.py`（7 个产出器，载荷取自 `analysis.to_dict()`/图谱/账本，不第二次序列化、不重算）+ `record_demo_run()` 落盘 + `export_pack_view()` 改为**只从 run 目录收集**；实测 `valid=True / errors=[]`，完整性缺件 **8 → 1**（只剩 `valuation.xlsx`；那是口径不是漏写 —— 估值要 symbol 级行情，文档级 run 的账本里没有市价证据，理由写进 `report.json` 的 `declared_absences`，由信封 `run_scope`/`declared_absences` 回读，`rehearse_demo.py` 据此分开报"真缺口"与"口径性缺失"）。顺带修两个真 bug：① `replay_view`/`export_pack_view` 的默认 run 取 `sorted(available)[-1]`，而 `run_id` 是随机 hex、字典序与时间无关 —— 装上产物后立刻显形（默认导出挑中一条从没写过产物的旧 run 并如实报"缺 8 件"）→ 改 `_latest_run()` 按 manifest mtime；② `financials.xlsx` 曾被 openpyxl 盖上"保存这一刻"的 mtime（`properties.modified` 设了也没用），同一 run 相隔 1.2 秒导出两次字节即不同 → 钉到 run 的 `started_at`（含 `core.xml` 两个时间戳）。新增 `tests/test_cn_run_artefacts.py` **23 条**。 |
| 13 | [Source Conflict Resolver](cards/CARD-13_source_conflict.md) | P1 | 初赛 | 2/5 | 01, 04| 🟡 代码+73 测试就绪（全量离线 **1013 passed / 23 failed / 2 skipped**，失败集合与 CARD-12 时逐条一致；冒烟 14/14 clean）；`src/cn/conflict.py` 四级来源优先级 + §7b 三态区分（DATA_CONFLICT / VERSION_SUPERSESSION / NEW_INFORMATION）+ **两级分组**（外层 `(company, metric, scope, currency)`、内层 `period` —— period 若进外层，NEW_INFORMATION 永远无法检出，三态之一成死代码）+ 同文档内数值不一致**交回 CARD-02**（`rule.intra_document_table_precedence` 留痕）+ 更正公告**句级**解析（`correction_pairs`，带 page/bbox/quote 且 `value_from_prose` 留痕，**不伪装成表格事实**）+ `assert_no_silent_override` **在 `resolve_conflicts` 内部每次调用都执行**（非仅测试；candidate 粒度，按 source 比对会漏）全部就位；校验器 `conflict_validator`；工具 `ResolveCnSourceConflictsTool` 已接入；失败注入 #5 可现场复现；**ACME 四分类词表（`classify_cross_source` / `compare_pair`）落在本卡，CARD-22 F5 复用不另立拼写**；§14 并列展示 UI 归 CARD-15；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 14 | [Prompt Registry](cards/CARD-14_prompt_registry.md) | P1 | 初赛起 | 竞赛模块清单 | 07 | `[x]` **全部验收项通过（2026-09-27）—— 台账里第一张走完 `[x]` 的卡**。全量离线 **0 failed / 1635 passed / 3 deselected**（1586 → +49 = 本卡新增用例数，逐项可归因）；冒烟 **22/22 clean**；`rehearse_demo.py --strict` 13 步四态与迁移前逐条一致。新建 `src/agents/prompts/`（`registry.py` + `templates/<name>.<version>.md` × 11 + `registry.lock.json` + `__main__.py`）。**11 段提示词**从 5 个模块**逐字**迁入，迁移前后 sha256 逐条相等（夹具 `tests/fixtures/cn/prompt_golden_hashes.json` 由迁移前代码生成，迁移后由接线后的代码重新推导比对）；占位符用 `{{var}}` 以保住备忘录里内嵌的 JSON schema；正文两侧空白是包装、真正的首尾换行由 frontmatter 显式声明。**`prompt_hash` 首次真正被赋值**：`RunConfig.prompt_names` → `RunRecorder.start()` 经注册表算出**组合哈希**写进 17 字段 manifest，旧的两处 ad-hoc 截断哈希（16 位 / 12 位）统一到同一来源；`config_fingerprint` 取值**未变**（提示词只换来源、字节未动）。§10 三条闸门全落地（零硬编码双判据扫描 / hash 入 manifest 四条用例 / `registry.lock.json` 冻结"版本只增不改"）；§14 注册表页落在「审计回放」页 **③**，且**没有 run 时也渲染**。§8 edge 3 已决策：**不建 Skill 空壳**（理由与"何时才值得建"写在卡 §16）。 |
| 15 | [Workbench Demo UI 增量](cards/CARD-15_demo_ui.md) | P1 | 贯穿 | 现场展示 | 各业务卡 | 🟡 8 个页面已挂载（研报纠错 05 / ACME 22 / 结论护照 23 / 图谱 09 / 脆弱性 24 / 时间重验证 25 / 审计回放 12 / 对抗注入 26），页面清单已按 `DEMO_FLOW.md` v3.2 校正；`src/cn/demo_service.py` **只读服务层**（只读 / 失败不抛 / 不编数字三条硬约束，信封 `status` 不吞引擎 `status`）；`workbench/demo/` 8 页 + `_shared.py`，`workbench.py` 只加挂载行；`tests/test_cn_demo_ui.py` **52 项全绿**，冒烟 **22/22 clean**，全量离线 **1497 passed / 23 failed / 2 skipped**（失败集合与 CARD-26 时逐条一致）；**未闭合**：真实演示材料未采集（现跑合成语料，每页已如实声明），CARD-02 对照表 / CARD-08 估值 / CARD-10 备忘录 / CARD-13 并列展示四页未做。彩排已**机器化**：`scripts/rehearse_demo.py`（13 步四态自检，可讲 1 / 需声明 7 / 会卡 2 / 待人工 3）+ `tests/test_cn_demo_rehearsal.py` **9 项全绿**，全量离线 **1506 passed / 23 failed**（失败集合逐条一致）；会卡的两步是真实材料缺失与只植入 5 处错误（§5 硬项 8/8） |
| 16 | [DCF 完整实现（修正版）](cards/CARD-16_dcf_valuation.md) | P2 | 决赛 | 4 | 08, 02 | ☐ |
| 17 | [Report Quality Diagnostics](cards/CARD-17_quality_diagnostics.md) | P2 | 决赛 | 7 | 05, 09, 12 | ☐ |
| 18 | [Multi-Agent 编排（含消融）](cards/CARD-18_multiagent.md) | P2 | 决赛 | 6 | 全主线 | ☐ |
| 19 | [Bull/Bear 结构化对照](cards/CARD-19_bull_bear.md) | P3 | 决赛 | 6 | 18 | ☐ |
| 20 | [MCP Server](cards/CARD-20_mcp_server.md) | P3 | 决赛 | — | 工具面稳定 | ☐ |
| 21 | [Industry Chain（重设数学）](cards/CARD-21_industry_chain.md) | P3 | 决赛 | 3 | 08 | ☐ |
| 22 | [ACME（会计约束驱动的多模态证据理解引擎）](cards/CARD-22_acme_multimodal_evidence.md) | **P0** | 初赛 | 2/5（创新一） | 01, 02, 13| 🟡 代码+69 测试就绪（全量离线 **1083 passed / 23 failed / 2 skipped**，失败集合与 CARD-13 时逐条一致（report 2 / cli 1 / workbench_service 14 / api 6）；冒烟 14/14 clean；passed 增量 1013→1083 = +70，逐项可归因）；`src/cn/acme/` 六类约束族 **F1–F6 全实现**：F1 会计恒等式（含少数股东权益 → `not_comparable` 不误报）、F2 表格结构（**声明式 `SUBTOTAL_SPECS`**，由邻接启发式改写 —— 旧版在 `annual_06` 上真误报）、F3 期间约束（**零重复实现**，只调 `FinancialPeriodEngine.derive_single_quarters`，引擎拒答记 `check_failed`）、F4 跨期连续性（消费 CARD-04 口径信号）、F5 跨源（**不持有任何比较算术**，交 CARD-13 `compare_pair`；两趟配对使「口径不同」正确落 `NOT_COMPARABLE` 而非假「单侧跳过」）、F6 跨模态（**子句级**解析，三方缺位记 `N/A`）；§7 容差策略 **per-unit**（元 1000 / PERCENT 0.05 / TABLE_UNIT 0.5 …）+ **三态**（passed/warning/violated）+ **无来源的豁免在构造期即拒绝**；`IntegrityReport` **零静默全覆盖**（`families_not_checked` + `constraints_not_available` 带 reason，每族永不返回空集）；校验器 `acme_validator` §13 四规则；工具 `ValidateCnAcmeTool` 已接入；**语料闸门实测**（15 篇：干净文档 0 违规、植入文件 `annual_07` 恰好命中 2 处）；**顺带修 CARD-02 真 bug**（更窄小计冒充父汇总，`_is_narrower_aggregate` 守卫）；§7b ablation **已于 2026-09-27 实测**（`parser_only 0.4000 (2/5) → parser_plus_accounting_constraints 1.0000 (5/5)`，干净对照误报 0/3；实验组取并集而非只取约束层，增益逐族可归因；F3/F4/F6 不在语料内，明写不冒充）、Real 材料未采集；§17 PDF 跳转 UI 已随 CARD-15 落地（2026-09-20）；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 23 | [Financial Claim Passport & Proof Verifier（金融AI结论护照）](cards/CARD-23_claim_passport_verifier.md) | **P0** | 初赛 | 全链路（创新二） | 09, 07, 13 | 🟡 代码+111 测试就绪（全量离线 **1194 passed / 23 failed / 2 skipped**，失败集合 14/6/2/1 与 CARD-22 时逐条一致；冒烟 14/14 clean；passed 增量 1083→1194 = **+111**，逐项可归因）；`src/cn/proof/` 六证明块（Source/Accounting/Calculation/Assumption/Dependency/Validation/Integrity）+ 九项检查 + **五态**（VERIFIED/DEGRADED/STALE/BLOCKED/CONFLICTED，合并取最严重**贡献**项，pass/skipped 永不移动判决）+ `verify_claim()` **零 LLM**（AST 断言不导入 `src.llms`）；`ProofContext` 全注入（graph/conflicts/注册表/manifest/声明版本与口径/文档哈希+loader/容差），缺项只让**对应那一项**检查 `skipped` 并写明原因；`verify_source` **未重算哈希时记 warned/DEGRADED 而非 passed**（四态 hash_state，杜绝"宣称未建立的完整性"）；`verify_calculation` 经 CARD-03 注册表真重算；`verify_validator_state` 用 CARD-09 重算并报 `drifted`；校验器 `proof_validator` §13 三规则 + `CONCLUSION_SECTIONS`/`REFRESH_MARKER`（BLOCKED/CONFLICTED 不得进结论段，STALE 须带"待刷新"）；工具 `VerifyCnClaimTool` 已接入；**语料闸门实测**（干净年报 VERIFIED、植入篡改/supersession/未决冲突分别 BLOCKED/STALE/CONFLICTED）；**顺带修 CARD-03 真 bug**（YoY 四指标漏声明 `unit`，静默落到"元"）；§16 ablation **已于 2026-09-27 实测**（`citation_only 0.2500 (1/4) → fpo_verifier 1.0000 (4/4)`，健康对照误报 0/1；增益全在"引用在、支撑不在"三类；`calculation reproducibility`/stale 不在语料内，不冒充）；另**同轮修 `verify_source` 真 bug**（悬空证据引用被误报为孤儿 claim，判决不变但理由误导；补两条互补回归，`test_cn_proof.py` 111 → 113 passed）；§17「点击 Claim → 展示护照 → VERIFY」交互已随 CARD-15 落地（2026-09-20，看护照与点 VERIFY 是两个调用，按钮真重跑）、§20 证明链图仍归 CARD-15（决赛范围）；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 24 | [Thesis Fragility Engine（投资逻辑脆弱性分析）](cards/CARD-24_thesis_fragility_engine.md) | **P1** | 初赛增强 | 6（创新三） | 09, 23 | 🟡 代码+93 测试就绪（全量离线 **1287 passed / 23 failed / 2 skipped**，失败集合 14/6/2/1 与 CARD-23 时逐条一致；冒烟 14/14 clean；passed 增量 1194→1287 = **+93** = 本卡用例数）；新建 `src/cn/fragility/`（`engine.py` 关键依赖/单点失效/**最小割集**/数值脆弱性/监控计划 + `theses.py` 两条确定性 Thesis 来源 + `models.py`）+ `src/validation/fragility_validator.py`（9 条规则：反解断言、极小性、**完整性**、阈值白名单+逐格可重算、单点兜底、状态自洽、缺输入 warning）+ 工具 `analyze_cn_thesis_fragility` 已注册；**零 LLM**（源码级断言）；阈值来源是**封闭白名单**（构造期即拒非法 `source_type`，解析不了则剔除进 `rejected_thresholds`），无阈值即"只做结构脆弱性"、`distance` 严格为 `None`；CARD-23 五态经 `verification_statuses` 流入（汇总取最严重，rank 0 = BLOCKED）。**两条最重要的偏差**：① 卡 §7B 的示例预设证据层有 OR 语义，而 CARD-09 是 fail-closed **没有 AND 门**，故**最小割集恒为单元素集**（卡例 {E01,E02,C02} 成立但非极小）——该结论是被校验器的"极小性"规则逼出来的；② **卡假设的 Thesis 在真实管线上不存在**（全仓库只有测试构造过），故新增声明式 + 推论式两条来源并永远报告 `thesis_source`。未闭合：§16 接入 CARD-11、语料缺口（现有语料无 CARD-03 诊断信号 → 推论式路径暂空，页面如实报 `thesis_source=declared`）、计算节点输入证据不承重故不下钻。 |
| 25 | [Temporal Revalidation Engine（时间重验证引擎）](cards/CARD-25_temporal_revalidation_engine.md) | **P1** | 初赛增强 | 2/5（创新三） | 09, 13, 24 | 🟡 代码+84 测试就绪（全量离线 **1371 passed / 23 failed / 2 skipped**，失败集合 14/6/2/1 与 CARD-24 时逐条一致；冒烟 14/14 clean；passed 增量 1287→1371 = **+84** = 本卡用例数）；新建 `src/cn/temporal/`（`models.py` 四态血缘 + `lineage.py` append-only 谱系 + `engine.py` 限深子图/**有界重算**/全量对账/影响与动作）+ `src/validation/temporal_validator.py`（11 条规则）+ 工具 `revalidate_cn_claims` 已注册；**零 LLM**（源码级断言）。**三态分开、不混为一谈**：替代 → 旧值退出口径、依赖结论转 **stale**（需按新值刷新，不是 blocked）；**未裁决**冲突 → fail-closed 阻断下游（已裁决不阻断）；新信息 → 需刷新不阻断。**增量是"真的有界重算"**：刻意**不调用** CARD-09 的 `mark_stale`/`deactivate_evidence`（它们会立刻全量传播，会让 `nodes_recomputed` 失去意义），改为直接往 `stale_claims` 追加标记 + **唯一一次**子图重算；与全量**每次现场对账**，不一致即**回落全量**并记录（卡 §11）；宽图实测 `nodes_recomputed=1 < full_nodes=60` 且结果一致。**三条最重要的偏差**：① 卡 §6 的第四态 `SUPERSEDED` **不在 CARD-02 `Version` 枚举里**（那只有三成员），且它是"谱系地位"而非"发布口径"，故另立四态而**不扩 `Version`**（扩了 `version_rank` 会 `KeyError`）；② `supersedes` 是**单值**却可能对应多条旧值 → 定案 `superseded_by`（写在旧值上）为**完整**映射、`supersedes` 只记**主前驱且首写胜出**（初版"最后写入者获胜"会静默覆盖反向边，被集成测试抓到）；③ 子图 BFS **走不到 Thesis**（Thesis 不在 claim→依赖 邻接表里），漏并进 `affected_subgraph` 会直接违反 §13 超集 —— 被自家校验器当场抓住。另修两处真 bug（`facts_superseded` 比对方向反了恒为 0；`calculations_stale` 按可达性数也是恒为 0，因计算节点输入证据不承重）。未闭合：§16 ablation 运行器属 CARD-11、**公告驱动的替代路径受语料限制不可用**（`announcement_04` 解析出 0 fact ⇒ CARD-13 正文型更正提取器恒返回 0 对），demo 用 `annual_04` 同文档重述血缘、多事件全局时间线排序未做、卡要求的 `financial-data-provenance` 技能在本仓库不存在。「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |
| 26 | [FinFuzz（金融语义对抗错误生成与压力测试）](cards/CARD-26_fin_fuzz.md) | **P1** | 初赛增强 | 7（评测创新） | 05, 09, 11 | 🟡 代码+74 测试就绪（全量离线 **1445 passed / 23 failed / 2 skipped**，失败集合 14/6/2/1 与 CARD-25 时逐条一致；冒烟 14/14 clean；passed 增量 1371→1445 = **+74** = 本卡用例数）；新建 `src/cn/finfuzz/`（`models.py` 10 算子枚举 + `EXPECTED_CHECK_TYPES` 多对多映射 + spec/result/report + `operators.py` 10 个确定性变异器 + `targets.py` 两个被测目标 + `suite.py` 生成/执行/指标/组合 + `corpus.py` 逐 claim 语料闸门 + `baseline.py` **CARD-11 组件基线** + `__main__.py` CLI）+ `src/validation/finfuzz_validator.py`（11 条规则）；**两通道分工**（`CLAIM` 直改字段→绕开抽取器，漏检只能归因核查器；`TEXT` 改句重抽→暴露抽取器失败），`failure_stage ∈ {extractor, checker}` 让"抽取器吃掉的错"不会被记成核查器盲点；**两个被测目标读数位置不同**：Checker 跑 CARD-05 `check_report`，Validator 读 CARD-09 **节点 `validation_status`** 而非报告 `.valid`，且**只把引用映射到 fact 的 `evidence_ids`、绝不映射 `derived_from`**（否则每条推论都"有支撑"、引用类篡改恒不报错）；**24 个伪变异（等值改写）是真正的压力源** —— `unit` 算子同时产出"110 万元→110 亿元"（量级错）与"110 万元 = 1,100,000 元"（等值），对前者沉默才是缺陷、对后者开口才是缺陷；**8/10 算子有样本**（`ytd_vs_single` / `valuation` 在当前合成语料上 `no_spec_generated`，**N/A 行保留不删**）；端到端 **P 1.0000 / R 0.9808 / F1 0.9903 / FPR 0.0000**（TP 102 / FP 0 / FN 2 / TN 24，specs=128），2 例漏检**全在抽取器阶段**、核查器阶段 0；**零 LLM**（源码级断言，变异与两个检测器全确定性，报告不携带墙钟）；CARD-11 集成 `adversarial_rows → adversarial_metrics()` 四项 + per-error-type recall 写进 note，无 rows 时四行保留 `None`；**并已注册进 `scripts/run_benchmark_v2.py` 的 `COMPONENT_BASELINES` 并由 `baseline.py` 产出 `finfuzz_baseline.json`**，实测 `--set synthetic` 由 **8 项已测变 12 项**、`source == card26_finfuzz`、`case_count = 10+3+128`（基线自报优先于注册表兜底，避免第二处真相）；**顺带修 CARD-05 真 bug**（`check_source_is_model_free` 子串匹配把确定性 `extract_claims_rules` 判成模型符号 ⇒ 改标识符边界 + 加 `label` 参数）；**holdout 未采集**（恒 `TARGET / TO BE MEASURED`，携带数字即判错）；§17 demo 交互已随 CARD-15 落地（2026-09-20，F1–F7 场景逐个注入真实故障 + FinFuzz 逐类型召回，`N/A` 行保留）；§16 ablation 未运行；卡 §15 写的 `--suite` 与实现 `--set` 名词不一致（按实现，已标注）；卡要求的 `financial-agent-evaluation` 技能在本仓库不存在；「全部测试离线通过」**已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**） |

**工程暂缓项**（赛后处理，不设卡）：workbench.py 全量拆分（仅允许 CARD-15 最小增量）、统一任务队列 SQLite 化（Run Manifest 已覆盖可复现需求）、快照 Catalog（Document Registry 优先）。

## 7. 依赖关系与执行顺序

```text
01 文档智能 ──→ 02 规范化 ──→ 04 口径 ──→ 03 财务分析 ──→ 10 备忘录
    │              │                         ↗ 08 相对估值 ↗
    │              └──→ 05 纠错 ──────────→ 10
    │              └──→ 22 ACME（六类约束）──→ 23 Claim Passport ──→ 24 脆弱性 ──→ 25 时间重验证
    ├──→ 06 检索 ─────────────────────────→ 10
07 Run Manifest ──→ 09 图谱 ──→ 12 证据包/回放
                 └──→ 23 Passport ──→ 24 脆弱性 ──→ 25 时间重验证
13 冲突/三态（依赖 01/04/22 跨源分类）──→ 25 时间重验证
11 基准（贯穿，从 01 起每卡配套；含 FinFuzz 集成与 ablation）
14 Prompt Registry（随 05/10 落地）  15 UI（随各业务卡）
26 FinFuzz（依赖 05/09/11，贯穿评测）
决赛链：08→16 DCF；09/12→17 诊断；主线→18 多智能体→19 辩论；21 产业链
```

单人串行推荐：**01 → 07 → 02 → 22 → 04 → 03 → 06 → 05 → 08 → 09 → 23 → 24 → 25 → 10 → 11 贯穿 → 12（简化版）→ 13 → 14/15 贯穿 → 26 贯穿**。
时间不足时裁剪顺序（从后往前砍）：13（Conflict 完整实现先砍，简化为"记录冲突+阻断"最小版）→ 25（Temporal 基础版先发电统一期还需 24）→ 24（Fragility 降决赛）→ 06（检索降级为标题关键字匹配）→ 26（FinFuzz 缩为 5 个核心 operator）。
**不可裁剪清单**：01/02/03/04/05/07/08/**09**/10/**22**/**23**。09 是"结论→证据→PDF 页回溯"的骨架，22 是 ACME 会计约束驱动的多模态质量控制（创新一），23 是 Financial Claim Passport（创新二），砍掉等于砍掉系统核心创新与差异化，**绝不可裁**。

决赛缓冲（初赛窗口 5 超载时的自动转移）：13 Conflict 完整实现、12 Audit Replay UI、CARD-20 全部能力、24/25 完整实现、26 完整 operator 集 → 决赛前 2 周内补。

## 8. 卡片执行协议（Agent 使用说明）

1. **一次只执行一张卡**。开工前按顺序读：`AGENTS.md` → 本文件第 9/10/11 节 → `ARCHITECTURE_V2.md` → 目标卡片全文。
2. 卡片是规格，不是死板脚本：与代码现状不符时以现状为准做最小合理调整，并在"执行备注"记录偏差。
3. 严格按卡内实施步骤推进；"验收标准"全项达成前不得声称完成。
4. **Skill 加载要求**：涉及 `src/cn/` 数据摄取/证据/估值/报告事实的卡片先加载 `financial-data-provenance` skill；涉及工具契约注册的先加载 `financial-tool-contract` skill；涉及评测设计与基准的先加载 `financial-agent-evaluation` skill。
5. 完成后：跑第 12 节验证命令 → 卡内状态打 `[x]` → 台账同步 → commit（遵循仓库既有风格）。
6. **禁止跨卡顺手改动**。发现别的问题记录到"执行备注"，留给对应卡片。
7. 每张卡完成后系统必须处于全测试通过的可运行状态。

## 9. 统一工程契约

### 9.1 Claim 契约（v3.1 修正：claim_type 与 temporal_status 分离、verification_status 取代 confidence 数值、依赖语义 REQUIRED/SUPPORTING/OPTIONAL）

```python
@dataclass
class Claim:
    claim_id: str
    claim_type: str              # fact | inference | opinion（认识论维度）
    temporal_status: str         # historical | current | forward_looking（时间维度）
    text: str
    evidence_ids: list[str]      # fact 必须直接绑定
    calculation_ids: list[str]   # 有计算的 claim 绑定 Calculation
    assumption_ids: list[str]    # opinion 必须标注假设来源
    derived_from: list[str]      # inference 必须能追踪依赖的 fact claim_id
    dependency_metadata: list[dict]  # [{role: REQUIRED|SUPPORTING|OPTIONAL, target_id}]
    verification_status: str     # EXACT_MATCH | ROUNDING_MATCH | NORMALIZED_MATCH | PERIOD_INFERRED | CONFLICTED | UNVERIFIABLE
    validation_status: str       # pending | supported | unsupported | blocked
    rationale: str | None        # 可选，推理依据说明
```

**v3.1 核心修正**：
- **claim_type 与 temporal_status 分离**：fact/inference/opinion 是认识论维度；historical/current/forward_looking 是时间维度。valuation 作为 claim subtype / domain，不与 fact/inference/opinion 混成同一 enum
- **verification_status 取代 confidence 数值**：删除没有校准依据的 0.8/0.9/1.0 confidence，改为离散状态。如果未来需要 numerical confidence，必须有 calibration 方法
- **依赖语义 REQUIRED/SUPPORTING/OPTIONAL**：REQUIRED 上游 blocked → 下游 blocked；SUPPORTING 失效 → coverage 降级但不自动 blocked；OPTIONAL 不影响核心有效性
- **因果 Claim 三级区分**：explicit_attribution（原文明确说明）/ analyst_inference（有证据但因果是推断）/ unsupported_causal（证据不足）

fact 必须直接绑定 Evidence；inference 必须能追踪其依赖的 Fact；opinion 必须明确假设或主观判断来源。

### 9.2 证据契约

- 事实只来自 Document Registry 的文档抽取或 Snapshot Provider；LLM 文本永不作为事实来源
- 文档型证据必须带 document_id + page（+ bbox）；快照型证据带 snapshot_id + available_at
- 市值与同业估值计算使用 RAW 价格
- 缺失/过期/混期间/混单位/口径冲突：拒绝、降级或显式标注，绝不静默填充

### 9.3 工具契约

新工具注册于 `src/agents/tools/cn_tools.py`，沿用现有信封：`status: ok | error`、error_code、事实字段带 evidence_id/as_of/unit/period。工具内不调用 LLM（编排层除外且留轨迹）。

**status 词表（v3.3 补充，与实现对齐）**：纯读取失败用 `error`（+ error_code）；成功路径按产出完整度分级 —— `ok`（完整）、`partial`（有产出但有阻断性缺口，如 `normalize_cn_financials` 未形成可计算报表、`analyze_cn_financials` 有指标未产出）、`blocked`（完全无产出，如 `analyze_cn_financials` 遇到纯扫描页）。分级表示**数据完整度，不表示结论可用性**；结论能否输出仍由 Validator 决定。

### 9.4 检索与引用契约（修正 v2 错误）

- chunk_id ≠ evidence_id：**只有真正用于支持 Claim 的 chunk 才进入 Evidence Ledger**
- 引用三型：quote（逐字一致，校验器强制）、paraphrase（允许重述但必须绑定原始 chunk）、inference（允许组合但必须标记）
- 检索命中文本只能被逐字引用；其中的数字不得直接进入结论，必须走数值校验路径

### 9.5 测试纪律

默认测试离线、不依赖 `.env`/网络/墙钟。新模块配套 `tests/test_cn_<module>.py`。确定性模块的测试用手工商可验的封闭数值。集成测试标 `@pytest.mark.integration`。

## 10. 红线（违反即回滚）

1. 不用 LLM 做可以由 Python 精确计算的金融算术
2. 不编造评分权重；不编造 benchmark 结果；不把 synthetic 结果描述成真实表现
3. 不直接对 YTD 财报数计算季度 QoQ（必须先 `convert_ytd_to_single_quarter`）
4. 不忽略单位和会计口径；不把 retrieval chunk 全部登记成 Evidence
5. 不把 inference 写成 fact；不无来源输出确定因果关系
6. 不因官方出现 Tool/Skill/MCP 字样就强制实现所有概念
7. 不大规模重写 UI（Streamlit + FastAPI 够用）；不迁移 React/微服务/K8s
8. 不破坏现有稳定能力；不引入 MongoDB/Redis/LangGraph
9. `data/`、`output/`、`.env`、付费数据、真实公告年报 PDF 原文不入 Git
10. 不复活已移除的美股/加密/预测市场/旧 supervisor 代码
11. 10-15 后禁止重大新功能（Code Freeze）

## 11. 反向检查（每张 P0 卡完成后、每次 Demo 冻结前逐项过）

| # | 问题 | 对应 |
|---|---|---|
| 1 | 能直接处理一份此前从未见过的真实 A 股年报？ | CARD-01/02 |
| 2 | 能处理自然语言研报草稿，而非仅预结构化 JSON？ | CARD-05 |
| 3 | 能正确处理 A 股累计季度数据？ | CARD-02/03 |
| 4 | 能区分合并口径和母公司口径？ | CARD-02/04 |
| 5 | 能检测更正/重述？ | CARD-04/13 |
| 6 | 能追踪一个数字到原始 PDF 页？ | CARD-01/09 |
| 7 | 能追踪一个结论到多个 Evidence？ | CARD-09 |
| 8 | 能追踪一个推论依赖哪些事实？ | CARD-09 |
| 9 | 能在 Evidence 缺失时阻断 Claim？ | CARD-09/Validator |
| 10 | 能重放一次历史任务？ | CARD-07/12 |
| 11 | 能证明最终结果不是仅由 LLM 生成？ | CARD-07/12 |
| 12 | 有真实数据 benchmark？ | CARD-11 |
| 13 | 同时测 Precision / Recall / False Positive？ | CARD-11 |
| 14 | 有完全离线或半离线可复现路径？ | 现有测试体系 |
| 15 | 能在 5 分钟内向评委证明核心价值？ | DEMO_FLOW.md |

任何一问答"不能"且属于 P0 → 回开发计划补齐。

## 12. 每卡通用验证命令

~~~powershell
python -m pytest -q -m "not integration" -p no:cacheprovider
python scripts/smoke_workbench.py
git diff --check
git status --short
~~~

涉及 UI 的卡片额外人工检查：运行 workbench，浏览器控制台无报错，对照原页面确认无回归。
