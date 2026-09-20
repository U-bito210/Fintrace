# CARD-22: ACME — Accounting-Constrained Multimodal Evidence Engine（会计约束驱动的多模态金融证据理解引擎）

| 属性 | 值 |
|---|---|
| 优先级 | P0（第一冻结点前） |
| 竞赛阶段 | 初赛版（9/23–10/6 分两段：基础约束 9/23–9/29，跨源/跨模态 9/30–10/6） |
| 对应赛题 | 北京赛：赛题2/5（创新一）；华北五省：Constraint Reasoning / Multimodal Understanding |
| 依赖 | CARD-01（文档解析）、CARD-02（规范化/期间键）、CARD-13（cross-source 裁决）、现有 `src/cn/domain.py`（FinancialStatement） |
| 实现复杂度 | 高（约 6 人日：基础约束 3 + 跨源 1.5 + 跨模态 1.5） |
| 状态 | 🟡 代码+70 测试就绪（2026-09-19 收尾，全量离线 1083 passed / 23 failed）；§17 PDF 跳转 UI 已随 CARD-15 落地（2026-09-20）；§18 ablation 表与「全部测试离线通过」未闭合，见 §16 |
| Skill 要求 | 动手前加载 financial-data-provenance |

> 本卡前身：v3.1 的 "Accounting Integrity Validator (ACEE)"。v3.2 按最终创新体系升级为 ACME：在会计完整性约束之上，增加 **Table Structural / Period / Cross-period / Cross-source / Cross-modal** 六类约束族，成为"解析→约束验证→异常定位→降级/重新解析/人工复核"的完整质量控制层。

## 1. Objective（目标）

新建 `src/cn/acme/`：对多模态金融证据（财报正文、财务表格、图表、脚注、公告、Snapshot Provider 数据）的抽取结果做**主动约束验证**。系统**不能"解析完就相信"**，而必须执行：`解析 → 约束验证 → 异常定位 → 降级/重新解析/人工复核`。

初赛阶段以**检测 + 定位 + 阻断**为主；决赛增强阶段才考虑 OCR candidate search / constraint optimization / automatic candidate correction。**禁止在没有充分证据时静默修改原始财务数字。**

## 2. Competition relevance（双赛分别解决什么评审问题）

| 比赛 | 解决的评审问题 |
|---|---|
| 华北五省计算机设计大赛 | 展示 **Constraint Reasoning + Multimodal Understanding**：系统联合理解正文/表格/图表/脚注/公告，并用会计恒等关系与跨模态一致性主动验证抽取结果——这是普通 OCR/RAG 不具备的机器可验证推理能力 |
| 北京市大学生金融人工智能竞赛 | 回答"数据准确性/金融专业性"：单位混用、勾稽关系破坏、期间错配、跨源不一致等真实金融硬伤被系统**主动捕获并定位**，而不是静默用错值继续计算 |

## 3. Why this is not a gimmick（为什么不是点缀）

普通 OCR / PDF Parser / RAG 只做"提取"，没有能力回答"提取结果是否可能成立"。ACME 的核心差异：

- 普通 Pipeline：`parse → trust`（解析完就相信）
- 本系统：`parse → constraint-check → locate anomaly → degrade/re-parse/review`（解析完必须过验证）

约束验证全部是**确定性规则**（会计恒等式、小计勾稽、YTD 关系、跨期连续性、跨源一致），不依赖 LLM 判断，结果可复现、可测试、可向评委展示"系统发现自己错了"。

## 4. Inputs / 5. Outputs

- **输入**：`NormalizedFact[]`（CARD-02）、`FinancialStatement[]`（现有 domain.py 或 CARD-02 适配器投影）、多模态抽取载体（表格结构、图表数值/注释、正文段落）、Snapshot Provider facts
- **输出**：`ConstraintViolation[]` + `IntegrityReport` + `CrossModalInconsistency[]` + `review_queue[]`（人工复核队列）

## 6. Data Models（数据模型）

```python
@dataclass
class ConstraintViolation:
    violation_id: str
    constraint_family: str    # accounting_identity | table_structural | period | cross_period | cross_source | cross_modal
    constraint_id: str        # 如 "balance_sheet_equation" / "subtotal_reconciliation" / "ytd_single_quarter_relation"
    inputs: list[dict]        # [{ref: evidence_id, value, period, scope, version, unit, currency}]
    expected_value: float | None
    actual_value: float | None
    residual: float | None
    tolerance: float          # 已按 tolerance policy 归一
    status: str               # violated | warning | passed | not_comparable
    source_refs: list[dict]   # [{document_id, page, locator} | {snapshot_id, available_at}]
    severity: str             # error | warning
    resolution_status: str    # opened | re_parsed | reviewed | auto_corrected(决赛) | dismissed
    detected_at: str

@dataclass
class CrossModalInconsistency:
    """跨模态不一致（正文 / 表格 / 图表 三方不一致）"""
    inconsistency_id: str
    metric: str
    values: dict[str, float | None]   # {source_a(body): 33.4%, source_b(table): 31.2%, source_c(chart): -2.2pct}
    computed_value: float | None      # 由原始数值计算得到的基准值
    conflict_type: str                # value_mismatch | label_mismatch | unit_mismatch | period_mismatch
    resolution_status: str            # unresolved | resolved_which_source
    source_refs: list[dict]

@dataclass
class IntegrityReport:
    document_id: str
    constraint_families_checked: list[str]
    totals: dict                      # {checked, passed, warning, violated, not_comparable}
    violations: list[ConstraintViolation]
    cross_modal: list[CrossModalInconsistency]
    review_queue: list[str]
```

## 7. Algorithms（算法——约束族与容差策略）

必须覆盖 **六类 Constraint Family**：

### F1. Accounting Identity Constraints（会计恒等式）
- `Assets ≈ Liabilities + Equity`
- `Net Profit ≈ Profit Before Tax − Income Tax`（允许报表项目差异）
- `Closing Cash ≈ Opening Cash + Net Change in Cash`
- **不得机械要求绝对相等**：须按 tolerance policy 判定

### F2. Table Structural Constraints（表格结构）
- `Subtotal ≈ Sum(children)`、`Total ≈ Sum(line items)`
- 用于检测：OCR 错位、列错位、负号丢失、单位错误、单元格串行

### F3. Period Constraints（期间约束）
- `Q2 = H1 − Q1`、`Q3 = 9M − H1`、`Q4 = FY − 9M`
- **必须与现有 `FinancialPeriodEngine` 共用统一 period schema（YYYYQ1/YYYYH1/YYYY9M/YYYYFY）**，只调用 `derive_single_quarters()`，**不得重复实现另一套 period logic**

### F4. Cross-period Constraints（跨期约束）
- `current_year_opening_balance ≈ previous_year_closing_balance`
- 会计政策调整、重述、合并范围变化时**不能强制一致**——结合 CARD-04 的 Accounting Scope / Restatement Signal 决定是否跳过或降级为 warning

### F5. Cross-source Constraints（跨源约束）
- `Annual Report Revenue vs Snapshot Provider Revenue`
- 在 period / scope / version / currency / unit **统一后**比较
- 结果至少分四类：`MATCH | ROUNDING_MATCH | CONFLICT | NOT_COMPARABLE`
- **禁止直接"谁不一样就认为谁错"**：只标记结果，裁决交给 CARD-13（本卡不裁决）

### F6. Cross-modal Constraints（跨模态约束）
- 例：正文写"毛利率同比下降 2.1 个百分点"、表格计算 33.4%→31.2% = −2.2pct、图表标注 −2.2pct → 检出不一致
- **不能直接判断正文一定错误**：记录 source A/B/C、computed value、conflict type、resolution status

### Tolerance Policy（容差策略，必须显式定义，不能机械绝对相等）
- 绝对容差：1000 元（小额舍入差异）；相对容差：0.1%（大额报表）；取两者较大值
- 支持 statement-specific adjustment 白名单（登记后豁免），白名单必须来源可溯
- 容差内差异 → `warning`（不误报为 violated）；容差外 → `violated`

## 8. API / Tool Contract

```python
# src/cn/acme/validator.py
def validate_multimodal_evidence(facts, statements, snapshots=None, chart_annotations=None) -> IntegrityReport: ...
# 各约束族检查函数（每族一个模块级函数，返回 ConstraintViolation[]）
def check_accounting_identities(...) -> list[ConstraintViolation]: ...
def check_table_structural(...) -> list[ConstraintViolation]: ...
def check_period_relations(...) -> list[ConstraintViolation]: ...     # 内部只调 FinancialPeriodEngine
def check_cross_period_continuity(...) -> list[ConstraintViolation]: ...  # 结合 CARD-04 信号
def check_cross_source(...) -> list[ConstraintViolation]: ...         # 输出 MATCH/ROUNDING_MATCH/CONFLICT/NOT_COMPARABLE，裁决交 CARD-13
def check_cross_modal(...) -> list[CrossModalInconsistency]: ...
```

Agent 工具 `ValidateCnAcmeTool`：入参 `document_id`；出参 `status / integrity_report / violations[] / review_queue[]`。解析失败或勾稽失败 → 进入 Validator / review queue，**绝不静默跳过**。

## 9. Deterministic vs LLM boundary（确定性与 LLM 边界）

- 本卡**全部确定性规则，零 LLM**
- LLM 不参与任何约束判定；LLM 只可能在"异常定位后重新解释文档上下文"环节以只读辅助出现（可后续增强，不做初赛依赖）
- 修正值（决赛 P2）必须保留：`raw value / candidate values / constraint violated / selected candidate / selection reason / manual review status`

## 10. Dependencies（依赖）

CARD-01（ExtractedFact + SourceLocator）、CARD-02（NormalizedFact + 期间键）、CARD-04（重述信号，F4 用）、CARD-13（cross-source 裁决）。调用现有 `FinancialPeriodEngine.derive_single_quarters()` / `derive_ttm()`，**不重复实现 period 逻辑**。

## 11. Failure Modes

- 约束检查失败 → 该约束标记 `check_failed`，不影响其余约束；**不得**因检查失败静默跳过
- 字段缺失（如 Gross Profit 未披露）→ 跳过该约束并记录 `skipped_missing_field`，不报错不误报
- 解析失败或勾稽失败 → 进入 Validator / review queue

## 12. Edge Cases

1. 多口径并存（合并/母公司）→ 分别检查不混用
2. 重述/口径变化（CARD-04 信号）→ 使用调整后数据或标记 `restatement_context`
3. 跨期不连续（IPO 公司无上年数据）→ 跳过跨期连续性
4. 容差内差异 → warning 而非 violated
5. 跨源口径不同 → `NOT_COMPARABLE`，不判错
6. 跨模态只有两方（正文+表格，无图表）→ 以两者比对为准，缺失方记录 N/A

## 13. Validator Rules

- 每个 violation 的 source_refs 必须可解析
- residual 与 expected/actual 一致（代码审查断言）
- review_queue 非空（存在 violated/warning 时）
- 决赛自动纠正路径必须有 original/selected/reason/manual review 四字段，否则**禁止写入**该值

## 14. Unit Tests

- 六类约束族各含正例/反例
- 容差测试（容差内 warning / 容差外 violated）；容差策略函数单独测试
- 期间约束只调 FinancialPeriodEngine（接口 mock 断言，不允许自写差分）
- 跨源四分类（MATCH/ROUNDING_MATCH/CONFLICT/NOT_COMPARABLE）
- 跨模态三方不一致正反例（含"正文正确表格错误/img 标注错误"用例）
- 字段缺失跳过、多口径分检、重述上下文标记

## 15. Integration Tests

CARD-01 → CARD-02 → CARD-22 全链：解析真实 PDF → 规范化 → ACME 检查 → violation 报告；注入 cross-source 变化 → CARD-13 裁决后结果正确。

## 16. Benchmark

**指标（接入 CARD-11，带 ablation）**：
- Constraint Violation Detection Recall / False Alarm Rate
- Cross-source reconciliation 准确率（四分类）
- Cross-modal inconsistency detection recall

**Ablation（CARD-11 必须）**：`Parser only` vs `Parser + accounting constraints`，比较 financial extraction error detection / statement integrity error detection。
**测试集**：Synthetic（合成年报 + planted violations）+ Real（真实年报 + 人工标注 violations）。目标值待 baseline 冻结，**禁止编造数字**。

## 17. Demo Flow

Demo 步骤 4b（见 DEMO_FLOW.md）：上传真实年报 → 展示一个约束异常被捕获（如 Assets ≠ Liabilities + Equity 或"正文 vs 表格"毛利变动区间不一致）→ 点击异常 → 跳转原始 PDF 页 + 约束违反详情（含 violation ID、inputs、residual、容差）。

## 18. Acceptance Criteria（验收标准）

- [x] **六类约束族全部有测试**：`tests/test_cn_acme.py` 69 项，按族组织 —— F1 `test_balance_sheet_equation_*`
      / `test_gross_profit_identity_*` / `test_identities_never_mix_consolidated_and_parent_scope`；
      F2 `test_subtotal_*` / `test_label_column_is_not_mistaken_for_a_value_column`；
      F3 `test_disclosed_single_quarter_*` / `test_period_relations_never_reimplement_period_arithmetic`；
      F4 `test_opening_balance_*` / `test_card04_signal_is_consumed_by_attribute_not_reimplemented`；
      F5 `test_matching_sources_are_reported_as_matched` / `test_differing_scope_is_not_comparable_never_a_conflict`；
      F6 `test_body_change_that_contradicts_the_table_is_flagged` / `test_a_correct_body_claim_produces_no_finding`。
      每族均有**正例 + 反例**（探测器恒响 / 恒不响都在此失败）。
- [x] **容差策略函数有测试并通过**：`test_tolerance_is_the_larger_of_absolute_floor_and_relative_band`、
      `test_tolerance_is_per_unit_so_ratios_are_not_swallowed_by_a_yuan_floor`、
      `test_classify_distinguishes_passed_warning_and_violated`（三态而非两态）、
      `test_exemption_without_a_source_is_refused`（**无来源的白名单在构造期即拒绝**）、
      `test_exemption_widens_the_band_but_still_reports_the_residual`。
- [x] **期间约束零重复实现**：`test_period_relations_never_reimplement_period_arithmetic` 读取
      `period_relations.py` 源码断言其不含自写差分，且只经 `FinancialPeriodEngine.derive_single_quarters`；
      `test_mixed_scope_group_makes_the_engine_refuse_and_the_finding_says_so` 覆盖引擎拒答路径
      （拒答记 `check_failed`，不静默通过）。
- [x] **跨源四分类输出正确；裁决交接 CARD-13 链路打通**：`cross_source.py` 不持有任何比较算术，
      构造 `ConflictCandidate` 后调 CARD-13 `compare_pair`，`VERDICT_STATUS` 只做词表映射。
      `test_real_cross_source_difference_is_flagged_and_handed_to_card13` 断言 finding 里
      `expected=20,000,000 / actual=19,000,000` —— **两个值都留下，本卡不选边**。
- [x] **跨模态不一致检测有正反例测试**：正例 `test_body_change_that_contradicts_the_table_is_flagged`
      （正文 −1.2pct vs 表格 −4.0588pct）；反例 `test_a_correct_body_claim_produces_no_finding`。
      另含 §12 edge 6 的第三方缺位记 `N/A`（`test_chart_annotation_can_be_a_third_party_*`）与
      无基准时的如实告警（`test_body_claim_without_a_computable_benchmark_is_recorded_as_a_warning`）。
- [x] **初赛无任何自动静默修值路径（代码断言）**：`test_no_constraint_module_ever_writes_a_financial_value`
      以负向先行断言扫描 13 个模块源码的赋值模式（`\.value\s*=(?!=)` 等 —— 用 `!=` 排除
      `denominator.value == 0` 这类**比较**，否则探测器把 `=` 与 `==` 混为一谈）；
      `test_no_constraint_module_imports_an_llm_client` 以 AST 解析 import 节点断言零模型客户端
      （不用子串匹配：`__init__` 的文档字符串本身就在**声明**「不导入 src.llms」，
      子串扫描无法区分「声称没有」与「有」）。`CorrectionTrail.is_complete()` 四字段齐备是
      `auto_corrected` 的唯一放行条件，校验器 `_check_correction_trails` 强制。
- [ ] **ablation 至少"parser only vs parser+constraints"可运行出结果（不编数字）** ← 未完成，**交接 CARD-11 §7b**。
      对比臂已在 `benchmarks/cn_agent_v2.json` 注册（`baseline: parser_only` → `treatment:
      parser_plus_accounting_constraints`，指标 `financial_extraction_error_detection`），
      但**指标定义属 CARD-11**，本卡不得另立一份（CARD-11 组件归并规则：同一指标两条定义即报错）。
      本卡的检测能力已由语料闸门实测，见 §23「语料误报闸门」行；ablation **表**待 CARD-11 §7b 运行器就位。
- [x] **全部测试离线通过** ← 待 Tushare 快照重建（23 项缺失快照失败集逐条不变） → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 19. Priority

**P0**（第一冻结点前必须完成基础版：F1/F2/F3/F4 四族 + tolerance policy；F5/F6 为 P0 后半段或 P1 早段，但 demo 剧本用到 cross-modal）

## 20. Competition Stage

初赛基础版（六类约束检测+定位+阻断）；决赛增强：constraint-based candidate correction / CP-SAT / OR-Tools（保留 original/candidate/selected/reason/manual review）。

## 21. Estimated Complexity

约 6 人日（基础约束 3 + 跨源 1.5 + 跨模态 1.5）。

## 22. Fallback / Degradation Strategy

- 时间不足裁剪顺序：F6 跨模态（demo 彩排可改用演示点）→ F5 跨源（降为仅记录不阻断）→ F3 保留（直接复用引擎，成本低）
- 任何约束族缺失时该族返回 `not_checked`，IntegrityReport 显式列出未检查族，不假装全覆盖
- 断网/材料不足：全部本地规则，无外部依赖

## 23. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **实现**：新建 `src/cn/acme/`（`models.py` / `tolerance.py` / `identities.py` / `structural.py` / `period_relations.py` / `cross_period.py` / `cross_source.py` / `cross_modal.py` / `validator.py` / `adapters.py` / `__init__.py`）+ `src/validation/acme_validator.py` + `ValidateCnAcmeTool`（`name="validate_cn_acme"`，只收 `document_id`）+ `tests/test_cn_acme.py`（69 项）。六族全实现（F1–F6），非仅 P0 基础四族。 |
| 2026-09-19 | **符号惯例（全卡唯一）**：`expected` = **独立推导出的参考值**，`actual` = **文档印出来的值**，`residual = expected − actual`，写在 `models.py`；由 §13 rule 2 强制可重算。F1 用「另两行推出的值 vs 印刷值」（毛利：`revenue − operating_cost` 为 expected），F5 用「高优先级来源 vs 被比对来源」。同一惯例贯穿六族，避免「同一条式子在两族里正负相反」。 |
| 2026-09-19 | **三态而非两态**：`residual == 0 → passed`，容差内非零 → `warning`，容差外 → `violated`。§7 只写了「容差内 warning / 容差外 violated」，但若把 0 也归 warning，**每一条被满足的约束都会涌进 review_queue**，队列当场失去可读性。 |
| 2026-09-19 | **容差 per-unit**：元 1000 / PERCENT 0.05 / BP 5 / MULTIPLE 0.005 / TABLE_UNIT 0.5。单一 1000 元地板套到百分数上会把每一个比率比较整个吞掉（33.0% 与任意百分数之差恒 < 1000）。这是「单位不同则容差不同」而非「多处硬编码」。 |
| 2026-09-19 | **偏差 1：F2 从邻接启发式改为声明式 spec**。初版按「相邻行相减」推小计关系，在 `annual_06` 上把「资产总计 = 期末总股本 + 归母净资产」判成小计断裂 —— **真误报**。改为 `SUBTOTAL_SPECS` 显式声明（`资产总计 = 流动资产合计 + 非流动资产合计` 等，每条带 accounting source），并写明能看/不能看：**能**看「汇总之汇总」，**不能**看「科目明细之汇总」（metric 词典不是会计科目表，在此自造一张等于伪造被检的关系）。同时修掉两个真 bug：首列标签被当数值列读、子项跨列借用。 |
| 2026-09-19 | **偏差 2：少数股东权益 → `not_comparable` 而非 `violated`**。表上同时有归母权益与少数股东权益时，归母权益单独无法闭合 `资产 = 负债 + 权益`；此处报违规是**用自己的缺指标造出来的误报**。记 not_comparable 并说明原因，不沉默、也不冤枉文档。 |
| 2026-09-19 | **偏差 3：F3 只调引擎，拒答即 `check_failed`**。期间差分全部委托 `FinancialPeriodEngine.derive_single_quarters`，本卡零重复实现（测试直接读源码断言）。引擎拒答（如同一财政期重复出现）记 `check_failed`，**不静默通过**；单季缺端点记 `not_comparable`（不是跳过 —— 「查不了」与「没问题」必须分开）。 |
| 2026-09-19 | **偏差 4：F5 两趟配对（修正一处假陈述）**。初版把 scope 放进配对键，于是「合并营收 vs 母公司营收」退化成双向 `skipped_missing_field` —— 这是**对文档的假陈述**（文档确实印了营收行），且把真正的可比性阻断埋成数据缺口。改为：同键 → 交 CARD-13 比较；单侧键 → 找同 (metric, period, currency) 的**异口径对应项** → 交 CARD-13 判 `NOT_COMPARABLE`（理由串含「口径不同」，结论不落 CARD-22）；确无对应项才跳过。 |
| 2026-09-19 | **偏差 5：F5 刻意不复用 §7 的 0.1% 容差**。跨源相等 = 「同一个数字印了两次」（CARD-13 §8 edge 3），若放宽到 0.1%，真实的「厂商值 vs 财报值」差异会被降级成友好的 `ROUNDING_MATCH`，抢掉 CARD-13 本该裁决的案子。`cross_source.py` 模块 docstring 记录该决定，模块内**不持有任何比较算术**（否则就是对同一个数字持有第二种意见）。 |
| 2026-09-19 | **偏差 6：F6 句级 → 子句级解析**。「报告期内公司毛利率为 33.0%，较上年同期下降 1.2 个百分点」按整句取数会抓到 **33.0** 当变动值。改为子句切分后按角色取值，歧义的「下降 5%」（未说明是相对还是绝对）**如实跳过并留 note**。另：`ratio_series` 的 bucket key 必须含期间，否则一条跨年序列塌成单点，变动永远算不出来。 |
| 2026-09-19 | **修 CARD-02 真 bug（跨卡改动）**：`src/cn/normalization/metrics.py::resolve_metric` 的「最长同义词胜出」把「**流动资产合计**」映射到 `total_assets`，于是在投影里顶掉文档实际印刷的「资产总计」—— 一个更窄的小计冒充了它的父汇总。新增 `_is_narrower_aggregate()` 守卫：更窄的小计一律拒绝映射并记 unknown，绝不静默出值。新增 `tests/test_cn_normalization.py::test_narrower_subtotals_never_impersonate_their_parent_total`。已回填 CARD-02 §16。 |
| 2026-09-19 | **语料误报闸门（本质检点）**：`synthetic_corpus.py` 新增 `annual_07_acme_constraint_break.pdf`（植入 F2 小计断裂 `资产总计 11,000,000 vs 10,000,000` + F6 跨模态断裂 `正文 −1.2pct vs 表格 −4.06pct`），语料 14→15 篇。闸门断言：**6 篇干净文档 0 违规**，`annual_07` **恰好**命中 2 处植入且不多不少 —— 一个卖点是「系统发现自己错了」的层，只有在没错时也能闭嘴才可信；F2 初版正是死在这一条上才改成声明式。已回填 CARD-01 §16。 |
| 2026-09-19 | **零静默全覆盖**：每族**永不返回空集**。缺字段 → `skipped_missing_field`，未启用 → `families_not_checked` + `constraints_not_available`（带 reason），引擎拒答 → `check_failed`。`IntegrityReport` 上「已检查 ⊕ 未检查 = 全集」由 §13 `_check_family_coverage` 强制（重叠、未知族、无理由的未检查族均为 error）。 |
| 2026-09-19 | **回归**：全量离线 **1083 passed / 23 failed / 2 skipped**，失败集合与 CARD-13 收尾时**逐条一致**（report 2 / cli 1 / workbench_service 14 / api 6）；冒烟 14/14 clean；`git diff --check` 无输出。passed 增量 1013 → 1083 = **+70**，逐项可归因：本卡 69 项 + CARD-02 守卫 1 项。 |
| 2026-09-19 | **未闭合**：§18 末两项。① CARD-11 §7b 的 ablation **表**（对比臂已注册，指标定义属 CARD-11，本卡不重复定义）；② 「全部测试离线通过」待 Tushare 快照重建。§17 的 PDF 跳转 UI 归 CARD-15。 |
| 2026-09-20 | **§17 PDF 跳转 UI 已落地（由 CARD-15 施工，本卡记账）**：新增「ACME 约束」页（`workbench/demo/acme.py`）。页面按本卡的**零静默全覆盖**口径呈现：六族逐族报告，同时把 **`families_not_checked`** 与 `constraints_not_available`（带 reason）放在显眼位置——「已检查 ⊕ 未检查 = 全集」是 §13 的硬规则，页面上同样不允许只报检查过的那部分。违规项可跳回原始 PDF 页。另注：服务层信封把引擎自己的 `status` 搬到 `acme_status` 后再写自己的 `status`（CARD-15 侧通用修复），否则"引擎说它没法判定"会被改写成"一切正常"。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§18「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
