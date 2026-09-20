# CARD-23: Financial Claim Passport & Proof Verifier（金融AI结论护照 / 财务证明对象）

| 属性 | 值 |
|---|---|
| 优先级 | P0（第一冻结点前） |
| 竞赛阶段 | 初赛版（10/7–10/11） |
| 对应赛题 | 北京赛：全链路证据可核验（创新二）；华北五省：Trustworthy AI / Proof-Carrying Computation |
| 依赖 | CARD-09（Claim-Evidence Graph）、CARD-07（Run Manifest）、CARD-13（版本状态） |
| 实现复杂度 | 中（约 4 人日） |
| 状态 | 🟡 代码+111 测试就绪（2026-09-19 收尾，全量离线 1194 passed / 23 failed）；§17 护照 + VERIFY 交互已随 CARD-15 落地（2026-09-20）；§20 证明链图可视化未做，见 §16 |
| Skill 要求 | 动手前加载 financial-data-provenance |

> 本卡前身：v3.1 的 "Financial Proof Object / Verifier (PCIR)"。v3.2 正式命名为 **Financial Claim Passport（对外名称"金融AI结论护照"）**，底层技术对象仍为 Financial Proof Object（FPO），并**新增独立 `verify_claim()` 五态验证接口**——这不是普通 Citation，而是让一个金融 Claim 能够被追溯、重新计算、重新验证、判断是否仍然有效。

## 1. Objective（目标）

新建 `src/cn/proof/`：为每个重要金融 Claim 生成机器可验证的 **Financial Proof Object（FPO / 结论护照）**，并提供**独立于 LLM** 的 `verify_claim(claim_id)` 验证接口。一个 FPO 必须让一个金融 Claim 能够被：**追溯、重新计算、重新验证、判断是否仍然有效**。

## 2. Competition relevance（双赛分别解决什么评审问题）

| 比赛 | 解决的评审问题 |
|---|---|
| 华北五省计算机设计大赛 | 展示 **Proof-Carrying / Trustworthy AI**：点击一条结论 → 展示完整证明对象（Claim→Calculation→Evidence→PDF 页→假设→依赖）→ 点击 VERIFY → 系统**脱离 LLM 重新执行证明**，输出五态结果 |
| 北京市大学生金融人工智能竞赛 | 回答"证据可核验 / 结果可复现"：每条结论不是"LLM 说它来自第几页"，而是可独立重算、可判定有效性的机器证明对象 |

## 3. Why this is not a gimmick（为什么不是点缀）

普通 RAG / Chatbot / Citation 只能提供"引用来源"——"这句话来自第 45 页"，无法回答：数字对不对？公式是什么？依赖证据还有效吗？假设条件是什么？FPO 提供的是**完整证明链 + 可独立执行的验证器**：

- 普通 Citation：`claim → source page`（引用）
- FPO：`claim → source_proof + calculation_proof + assumption_proof + dependency_proof + validation_proof + integrity_proof`（证明），且 `verify_claim()` 能**重新检查全部依赖与重算结果**并给出离散状态（VERIFIED/DEGRADED/STALE/BLOCKED/CONFLICTED）

## 4. Inputs / 5. Outputs

- **输入**：`Claim` + `Calculation[]` + `Evidence[]`（CARD-09）、`Assumption[]`（CARD-08）、`RunManifest`（CARD-07）、版本谱系（CARD-13/25）
- **输出**：`FinancialProofObject`（护照）+ `VerificationResult`（验证结果，五态）

## 6. Data Models（数据模型——FPO 至少包含的部分）

```python
@dataclass
class FinancialProofObject:
    # --- Claim 本体 ---
    claim_id: str
    claim_type: str              # fact | inference | opinion
    claim_text: str
    economic_period: str         # 如 "2025Q2"
    scope: str                   # consolidated | parent
    version_status: str          # AS_REPORTED | RESTATED | CORRECTED | SUPERSEDED

    # --- Source Proof（来源证明）---
    source_proof: dict = {}      # {document_id, sha256, page, locator, evidence_ids, available_at}
    accounting_context: dict = {}  # {period, single_quarter|ytd|ttm, consolidated/parent, currency, unit, as_reported/restated/corrected}

    # --- Calculation Proof（计算证明）---
    calculation_proof: dict = {} # {calculation_id, formula, formula_version, inputs[{ref,value}], output, code_version}

    # --- Assumption Proof（假设证明）---
    assumption_proof: dict = {}  # {assumption_ids[], sources[], rationale}

    # --- Dependency Proof（依赖证明）---
    dependency_proof: dict = {}  # {upstream_claims[{role: REQUIRED|SUPPORTING|OPTIONAL, target_id}], dependencies[]}

    # --- Validation Proof（验证证明）---
    validation_proof: dict = {}  # {validator_rules[], result, blocked_reasons[]}

    # --- Integrity Proof（完整性证明）---
    integrity_proof: dict = {}   # {object_hash, audit_chain_ref, run_id, git_commit}

@dataclass
class VerificationResult:
    proof_id: str
    overall_status: str          # VERIFIED | DEGRADED | STALE | BLOCKED | CONFLICTED
    status_reasons: list[str]    # 逐条原因
    checks: list[dict]           # [{check: str, status: str, message: str}]
    verify_source_version: str   # 验证器版本（代码版本）
    verified_at: str
```

**五态定义（必须与依赖传播联动）**：

| 状态 | 含义 | 触发示例 |
|---|---|---|
| VERIFIED | 全部证明项通过 | 来源/期间/单位/公式/依赖全部有效 |
| DEGRADED | 有非阻断项失效 | SUPPORTING 证据缺失；页码模糊但可解析 |
| STALE | 数据过期待刷新 | 上游 evidence 被 superseded、period 更新、新 snapshot 进入 |
| BLOCKED | 关键证明项失败 | REQUIRED 依赖 blocked；document_hash 失效；公式重算不一致 |
| CONFLICTED | 证据冲突未决 | 同期间同口径出现冲突值且 CARD-13 未裁决 |

## 7. Algorithms（算法）

**verify_claim(claim_id) 至少重新检查**（全部确定性，不调用 LLM）：
1. document hash（重新计算输入文档 sha256 比对）+ evidence existence（evidence_id 是否仍存在且 active）
2. source version（版本状态是否 superseded / restated → STALE）
3. period 一致性（economic_period 与 inputs period 一致）
4. scope 一致性（consolidated/parent 一致）
5. unit 一致性（unit/currency 一致）
6. formula + calculation result（按公式用 inputs 重算，比对 deterministic_output，容差内视为通过）
7. required dependencies（上游 REQUIRED claim/evidence 状态 → 未通过则 BLOCKED）
8. assumptions 有效性（assumption 是否仍有效/过期）
9. validator state（当前 validator 规则下 claim 是否 supported）

**状态合并规则**：任一 REQUIRED 项 BLOCKED → 整体 BLOCKED；任一证据 superseded/stale → STALE；存在未决 conflict → CONFLICTED；非阻断 warning → DEGRADED；全部通过 → VERIFIED。

## 8. API / Tool Contract

```python
# src/cn/proof/passport.py
def build_financial_proof_object(claim_id, run_id) -> FinancialProofObject: ...
def verify_claim(claim_id) -> VerificationResult: ...   # ★ 核心：不依赖 LLM 重新回答
def verify_fpo(fpo: FinancialProofObject) -> VerificationResult: ...  # 复用 verify_claim 内部检查项
# 检查函数（verify_claim 内部逐项）
def verify_source(...) -> dict: ...
def verify_period(...) -> dict: ...
def verify_scope_unit(...) -> dict: ...
def verify_calculation(...) -> dict: ...
def verify_dependencies(...) -> dict: ...
def verify_assumptions(...) -> dict: ...
def verify_validator_state(...) -> dict: ...
```

Agent 工具 `VerifyCnClaimTool`：入参 `claim_id`；出参 `status / passport / verification_result`。

## 9. Deterministic vs LLM boundary（确定性与 LLM 边界）

- `verify_claim()` **零 LLM**——全部项为确定性检查与确定性重算
- LLM 曾做的：生成 Claim 的文本解释（passport 只引用 claim_text）
- 明确契约：验证结果**不得**被 LLM 修改；UI 可让 LLM 解释失败原因，但状态本身由验证器产生

## 10. Dependencies（依赖）

CARD-09（Claim/Calculation/Evidence + 依赖语义）、CARD-07（run manifest / audit chain 引用）、CARD-13 + CARD-25（版本谱系与超期状态 → STALE/CONFLICTED）、CARD-08（assumption registry）。

## 11. Failure Modes

- FPO 构建失败 → claim 标记 `proof_build_failed`，不阻断其余
- 验证失败 → 如实返回 BLOCKED，不降标通过
- 校验器自身 bug → 版本化 verify_source_version + 单测覆盖，不允许"验证器永远通过"

## 12. Edge Cases

1. 纯 fact 无 Calculation → calculation_proof 空，该检查 PASS（跳过）
2. 非 opinion 无 Assumption → assumption 检查 PASS
3. 孤儿 Claim（evidence 缺失）→ source 检查 BLOCKED
4. 计算输入缺失 → calculation 检查 BLOCKED
5. 上游 REQUIRED 依赖 blocked → dependency 检查 BLOCKED → 整体 BLOCKED
6. 新公告使 evidence superseded → STALE（不是 BLOCKED——还有历史价值）
7. proof_hash 重算失败 → integrity 标记 invalid

## 13. Validator Rules

- 每个 passport 的 proof_hash 可重算验证（断言）
- verification_result.overall_status 与各子项一致（合并规则有单测）
- BLOCKED/CONFLICTED 的 claim **不得进入最终 Memo 的结论段落**（Validator 遍历检查）；STALE 只允许带显式"待刷新"标注

## 14. Unit Tests

- passport 构建（fact/inference/opinion 各一例）
- verify_claim 五态各一例（VERIFIED/DEGRADED/STALE/BLOCKED/CONFLICTED）
- 各检查函数正反例（source/period/scope/unit/calculation/dependency/assumption/validator）
- proof_hash 稳定性；状态合并规则表驱动测试

## 15. Integration Tests

CARD-09 → CARD-23 全链；验证 supersession 后 verify_claim 返回 STALE（与 CARD-25 联动）；冲突未决返回 CONFLICTED（与 CARD-13 联动）。

## 16. Benchmark

**指标（接入 CARD-11，带 ablation）**：
- FPO Verification Pass Rate
- **Ablation**：`citation only` vs `FPO verifier`，比较 unsupported claim detection / stale claim detection / calculation reproducibility
- Orphan Claim Rate / Evidence Coverage
**测试集**：Synthetic + Real（人工标注 verification failures）。目标值待冻结，禁止编造。

## 17. Demo Flow

Demo 步骤 7b（DEMO_FLOW.md）：点击一条核心 Claim → 展示 Financial Claim Passport（完整证明链）→ 点击 **VERIFY CLAIM** → 系统脱离 LLM 重新验证 → 展示五态结果（VERIFIED 绿色通过，或注入 supersession 后变 STALE）。

## 18. Acceptance Criteria（验收标准）

- [x] **verify_claim() 零 LLM（代码断言：验证路径无模型调用）**：`tests/test_cn_proof.py` 的
      `test_no_proof_module_imports_a_model_client` 以 **AST 解析 import 节点**断言
      `src/cn/proof/*` 不导入 `src.llms`（不用子串匹配 —— 模块 docstring 本身就在**声明**
      「零 LLM」，子串扫描分不清「声称没有」与「有」）；`test_proof_validator_is_model_free_too`
      对 `src/validation/proof_validator.py` 同断言。`test_verify_tool_is_registered_and_never_writes_a_value`
      从工具侧再守一次。
- [x] **五态各有一例测试通过**：`test_state_verified_on_a_clean_claim` /
      `test_state_degraded_when_a_non_blocking_item_lapses` / `test_state_stale_after_a_supersession` /
      `test_state_blocked_when_a_required_proof_item_fails` /
      `test_state_conflicted_when_evidence_disagrees_and_card13_has_not_ruled`，
      另有 `test_every_state_in_the_vocabulary_is_reachable` 用**同一套图的受控变体**证明五态
      **都是从引擎真跑出来的**，不是手工改 `overall_status` 改出来的。
- [x] **proof_hash 稳定性与可重算性测试**：`test_proof_hash_is_stable_across_calls_and_constructions`、
      `test_seal_is_idempotent`、`test_hash_excludes_the_hash_it_stores`、
      `test_changing_any_proof_block_changes_the_hash`（六个证明块**逐个**扰动，逐块断言哈希改变 ——
      只测「改一处会变」会漏掉某个块根本没进哈希）。
- [x] **BLOCKED/CONFLICTED 不得进入 Memo 结论段（Validator 断言）**：
      `src/validation/proof_validator.py` 的 `CONCLUSION_SECTIONS` 白名单 + `REFRESH_MARKER`。
      `test_blocked_claim_in_a_conclusion_section_is_rejected`、`test_conflicted_claim_in_a_conclusion_section_is_rejected`、
      `test_blocked_claim_outside_the_conclusion_sections_is_allowed`（**事实陈述段不设限**，
      否则「记录一个已知被阻断的数」也会被拦）、`test_stale_claim_in_a_conclusion_must_carry_the_refresh_marker`、
      `test_conclusion_sections_exclude_the_fact_reporting_ones`。
- [x] **各检查正反例 + 合并规则表驱动**：九个检查函数各自正反例（见 §14 清单，共 47 项）；
      合并规则 `test_merge_takes_the_most_severe_impact`（参数化覆盖全部 impact 组合）、
      `test_merge_ignores_pass_and_skipped`（**pass/skipped 永不移动判决** —— 这是「跳过 ≠ 通过」
      能被信任的前提）、`test_a_raising_check_becomes_a_failure_not_a_pass`（检查自身抛异常记 fail，
      §11 禁止「验证器永远通过」）。
- [ ] **ablation「citation only vs FPO verifier」可运行出真实数字** ← 未完成，**交接 CARD-11 §7b**。
      对比臂已在 `benchmarks/cn_agent_v2.json::ablation_comparisons` 注册
      （`baseline: citation_only` → `treatment: fpo_verifier`，指标 `unsupported_claim_detection`），
      `src/cn/benchmark_v2.py` 只负责**声明与渲染**（`tests/test_cn_benchmark_v2.py` 覆盖渲染）。
      但**指标定义与运行器属 CARD-11**，本卡不得另立一份（CARD-11 组件归并规则：同一指标两条定义即报错）。
      本卡的检测能力已由语料闸门实测（见下条），ablation **表**待 CARD-11 §7b 运行器就位。
- [x] **语料闸门（本卡自己的实测，不是 ablation 的替代）**：
      `test_corpus_gate_clean_filings_verify_and_planted_defects_do_not` —— 两篇**干净**年报
      （`annual_06_equity_shares.pdf` / `annual_01_profit_continued.pdf`）走完 CARD-09 →
      CARD-23 全链后必须 `VERIFIED`；植入缺陷（篡改字节 / supersession / 未决冲突）必须分别落到
      `BLOCKED`/`STALE`/`CONFLICTED`。**一个只有作弊才检测得出来的验证器是没有价值的**；
      这条守的是「没错的时候也会闭嘴」。
- [x] **全部测试离线通过** ← 待 Tushare 快照重建（23 项缺失快照失败集逐条不变）。 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）
      本卡自身 `tests/test_cn_proof.py` **111 passed / 0 failed**（离线、无网络、无 .env）。

## 19. Priority

**P0**（第一冻结点前必须：完整 FPO + verify_claim + Validator integration）

## 20. Competition Stage

初赛基础版；决赛增强：advanced visualization（证明链图）、批量验证面板。

## 21. Estimated Complexity

约 4 人日。

## 22. Fallback / Degradation Strategy

- 时间不足：verify_claim 先实现最小集（source/calculation/required-dependency 三项），其余检查标记 `not_checked`
- demo 彩排若五态场景太多，保留 VERIFIED / BLOCKED / STALE 三态演示即可，CONFLICTED/DEGRADED 答辩补充
- 断网不影响（全部本地确定性）

## 23. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **实现**：新建 `src/cn/proof/`（`models.py` 六证明块 + 五态 + 合并规则 / `passport.py` 构建 + 九项检查 + `verify_fpo`/`verify_claim` / `__init__.py`）+ `src/validation/proof_validator.py`（§13 三条规则 + `CONCLUSION_SECTIONS`/`REFRESH_MARKER`）+ `VerifyCnClaimTool`（`name="verify_cn_claim"`，只收 `document_id` + `claim_id`）+ `tests/test_cn_proof.py`（105 个测试函数 / 111 个用例）。`ProofContext` 一律**注入**不全局：graph / conflicts / calculation_registry / assumption_registry / run_manifest / declared_versions / declared_scopes / document_hashes / document_loader / tolerance，缺哪一项就只让**对应的那一项**检查降为 `skipped` 并写明原因（§22）。 |
| 2026-09-19 | **偏差 1：§7.3 期间检查按字面实现会拦掉所有合法的跨期结论**。卡面写「`economic_period` 与 inputs period 一致」，但 `revenue_yoy`@2025FY 合法地同时依赖 2025FY 与 2024FY，平均净资产依赖两个资产负债表日 —— 要求全部输入同期间，等于对**本项目的核心比率**恒响。改为按字面背后的意图拆两段：①claim 期间必须等于**计算自己声明的输出期间**（CARD-03 已经决定过这份输出是关于哪一期，这是一次真核对）；②至少一项输入落在该期间。跨期输入如实报进 `cross_period_inputs` 但**不判失败** —— 两期输入是否合法是 CARD-02 期间引擎（与 CARD-22 F3/F4）的事，在这里重推一遍期间算术就是 §7.3 想避免的第二套实现。 |
| 2026-09-19 | **偏差 2：§7.4 单位检查按字面实现是范畴错误**。`gross_margin` 声明 `PERCENT`，它的输入（gross_profit / revenue）声明 `元` —— 拿 claim 的单位去比输入的单位，会把语料里**每一个比率**判失败。单位属于**量**，不属于 claim。改为：①claim 指标的单位比对 **CARD-03 指标表的规范单位**（`metric_definition(metric).unit`，独立来源）；②同名指标的多条证据必须互相一致（这才抓得住「同一行两个输入一个记元、一个记万元」）。 |
| 2026-09-19 | **偏差 3：拿派生值去比它的派生源不是核对，是回声** —— 同一个坑踩了两次。护照的 scope/currency/unit 是**派生**的（有计算项取自计算节点，无计算项取自首个证据）；拿它比回自己的来源**永远不可能不一致**，把它算进 `compared` 会让检查看起来比实际严。第一次修 `verify_scope_unit`；第二次发现 `_accounting_scope` 竟然用调用方声明**覆盖**了派生口径，于是 `verify_scope_unit` 里那一路径同样变成回声。最终形态：`AccountingContext` 把 `scope`（派生）+ `declared_scope`（调用方声明）+ `scope_basis` **分开存**（这样 §7.4 才可能抓到「结论说是母公司口径、数却是合并算出来的」）；派生的值如实列进 `derived` 并**排除在 `compared` 之外**；凡无法独立核对的项一律写进 `not_compared`，不装作查过。 |
| 2026-09-19 | **偏差 4：哈希没重算时不得记 passed**。`verify_source` 初版在「没有 loader、也没有 manifest 可交叉核对」时返回 `passed`（证据都在、都 active、可定位）。这等于让护照**宣称一个从未建立过的完整性** —— 正是本项目禁止的静默假设（§11「验证器永远通过」的软性版本）。改为 `warned` + `impact=DEGRADED` 并显式带上 `hash_state`：`recomputed` / `in_manifest` / `unreadable` / `unchecked` 四态各自可读，报告里「查过」与「没查」始终分得开。 |
| 2026-09-19 | **偏差 5：`verify_integrity` 的口径收窄**。§13 rule 1 只要求「proof_hash 可重算」，§12 edge 7 把失败定义成哈希失败 —— 那就只让这两件事移动判决。`run_id` 缺失 → `DEGRADED`（它是 CARD-09/12 的关联键，但没有 run_id 是**身份缺口**，不是假结论）；`audit_chain_ref` 与 `git_commit` 属 §4 列明的可选输入，**只记录进 `provenance_gaps` 不升级** —— 否则一篇干净年报永远到不了 `VERIFIED`，五态词表就退化成装饰，并训练用户忽略 `DEGRADED`。这里刻意分开的是两件事：「这条结论还站得住吗」与「这个构建可复现吗」。 |
| 2026-09-19 | **偏差 6：§12 edge 6「证据被撤销」的落点**。原测试名写成「撤销 → STALE」，实测是 `BLOCKED`：`deactivate_evidence` 是 CARD-09 的**追加式撤回**，撤回一条 REQUIRED 边上的证据 → 上游 `UPSTREAM_BLOCKED` → 下游阻断。`STALE` 是留给 CARD-13 **supersession**（有一份更新的文件取代了旧值，旧结论仍有历史价值）的。两者语义不同，测试改名 `test_withdrawn_evidence_blocks_the_claim_through_the_engine` 并断言 `BLOCKED`。 |
| 2026-09-19 | **偏差 7：§13 rule 2 的测试不能手工改 `overall_status`**。初版直接手改字段造出「BLOCKED 判决」喂给校验器，结果被校验器**正确地**拒了（改完的判决与各子项不一致，违反合并规则）。这不是校验器太严，是测试在伪造一个引擎产不出的对象。改用 `_blocked_verdict` / `_stale_verdict` / `_conflicted_verdict` 构造**真判决**。 |
| 2026-09-19 | **修 CARD-03 真 bug（跨卡改动）**：`src/cn/analysis/metrics.py` 里 `revenue_yoy` / `revenue_qoq_single_quarter` / `net_profit_yoy` / `deducted_net_profit_yoy` 声明时**漏了 `unit`**，于是默认成 `Unit.YUAN`；而 YoY 分支实际产出的就是 `PERCENT`。表声明与产出**潜在不一致**，一直没被照出来是因为没有消费者按规范单位去核对。CARD-23 §7.4 的独立单位核对以 `metric_definition(metric).unit` 为真值来源，第一跑就把它照出来了。已修并回填 CARD-03 §16。这正是「创新卡反过来校验基础卡」应有的样子。 |
| 2026-09-19 | **修一个测试构造错误（CARD-09 语义澄清，跨卡）**：测「SUPPORTING 上游失效只降覆盖、不阻断」时，claim 只写了 `dependency_metadata` 的 SUPPORTING 边、**没有 `derived_from`** —— 而 CARD-09 §7 的绑定规则要求 inference 必须有 `derived_from`，于是它在传播**还没走到 SUPPORTING 那一行**时就已是 `unsupported`。这个形状测到的是绑定规则，不是 §7b。改为真带 REQUIRED 派生（`_supporting_lapse_graph()`），并在测试里显式断言前提 `validation_status == supported`。已回填 CARD-09 §16 —— 否则下一个读到 §7b 表格的人会再写一遍同样的测试。 |
| 2026-09-19 | **回归**：全量离线 **1194 passed / 23 failed / 2 skipped**（junit 解析，`errors=0`），失败集合 **14 / 6 / 2 / 1** 与 CARD-22 收尾时**逐条一致**（`test_cn_workbench_service` / `test_workbench_api` / `test_cn_report` / `test_cn_report_cli`，全部为缺失 Tushare 快照）；passed 增量 1083 → 1194 = **+111**，恰好等于本卡用例数，逐项可归因。冒烟 14/14 clean；`git diff --check` 无输出。 |
| 2026-09-19 | **环境坑（写下来省下一次踩）**：全量回归必须**前台**跑。safe-delete 的 bulk guard 按「每轮对话」**累计**被删文件数（阈值 50），本轮先跑过一次子集后预算已耗尽，此时后台跑全量会把每个 teardown 的 `SystemExit: 1` 放大成 **220 errors / 984 passed 的假象** —— 看起来像产品崩了，其实 `errors` 全是 shim 在 teardown 拒删，`failed` 反而是准的。判据永远是 **junit 的失败集合**，不是 stdout 的计数。 |
| 2026-09-19 | **未闭合**：§18 的 ablation 表（对比臂已注册，指标定义与运行器属 CARD-11，本卡不重复定义）+「全部测试离线通过」（待 Tushare 快照重建）。§17 的「点击 Claim → 展示护照 → VERIFY CLAIM」交互归 CARD-15；§20 决赛增强的证明链图可视化同样归 CARD-15。 |
| 2026-09-20 | **§17「点击 Claim → 展示护照 → VERIFY CLAIM」已落地（由 CARD-15 施工，本卡记账）**：新增「结论护照」页（`workbench/demo/passport.py`）。关键设计：**"看护照"与"点 VERIFY"是两个分开的服务调用**——`passport_view()` 取护照，`verify_view()` 只跑一次验证。否则按钮只会回放页面加载时算过的结果，而剧本要的是"点击 → 系统重新执行"。验证路径纯确定性（重查文档哈希、重算公式、检查依赖状态，模型不参与）。§20 的证明链图可视化**未做**（决赛范围）。 || 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§18「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
