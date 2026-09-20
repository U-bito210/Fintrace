# CARD-03: Financial Analysis Engine（财务分析引擎）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（9/23–9/29） |
| 对应赛题 | 赛题2 主方向 |
| 依赖 | CARD-02（NormalizedFact）、CARD-04（可比性信号，可后接） |
| 实现复杂度 | 中高（约 4–5 人日） |
| 状态 | 🟡 代码 + 74 测试就绪；§13 Calculation Accuracy 未接入 CARD-11 |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/analysis/`：在规范化数据上执行确定性财务分析——14 项指标 + 4 类诊断信号。修复 v2 方案的两处金融逻辑硬伤：**YTD 直接算 QoQ** 与 **亏损基期百分比同比**。

## 2. 对应竞赛评分点

计算准确性（同比/环比/单季化）、分析逻辑（诊断信号）、金融专业性（盈利状态语义）。

## 3. 为什么需要这个模块

赛题2 明确考察"同比、环比计算的准确性"与"利润与现金流背离等问题识别"。v2 的 `compute_qoq(current, prev)` 对 A 股 YTD 披露是错的；`compute_yoy` 对亏损基期输出误导性百分比。这两处会被专业评委当场击穿。

## 4. 输入 / 输出

- **输入**：`NormalizedFact[]`（同 scope/version 配对校验后的序列）
- **输出**：`AnalysisResult`：指标表（含计算过程）+ 诊断信号列表 + data_complete 标记

## 5. 数据模型

```python
@dataclass
class MetricResult:
    metric: str             # 见第 7 节 14 项
    value: float | None
    unit: str
    period_key: str
    comparison: str | None  # yoy | qoq | level
    comparison_semantic: str | None  # normal | turnaround(扭亏为盈) | to_loss(由盈转亏)
                                   # | loss_widening | loss_narrowing | not_computable
    delta_abs: float | None  # 状态迁移时输出绝对变动额，不输出百分比
    delta_pct: float | None  # 仅 normal 语义输出
    calculation_id: str      # 绑定 Calculation 记录

@dataclass
class DiagnosticSignal:
    signal_id: str          # earnings_quality_warning | receivable_risk | inventory_pressure
                           # | non_recurring_contribution
    periods: list[str]
    detail: str             # 确定性拼接
    metric_calculation_ids: list[str]
```

## 6. API / Tool Contract

```python
def compute_yoy(current, previous) -> MetricResult:
    """previous==0 → not_computable（禁止百分比）；
    previous<0<current → turnaround，返回绝对变动；
    previous>0>current → to_loss，绝对变动；
    同负 → loss_widening/loss_narrowing，绝对变动+方向；
    同正 → normal，百分比。"""

def compute_single_quarter_qoq(current_q, previous_q) -> MetricResult:
    """输入必须是 convert_ytd_to_single_quarter 之后的产品（CARD-02）。"""

def analyze_financials(facts: list[NormalizedFact]) -> AnalysisResult: ...

def run_diagnostics(metrics) -> list[DiagnosticSignal]: ...
```

Agent 工具 `AnalyzeCnFinancialsTool`：入参 `document_id 或 symbol`；出参 `status / metrics / signals / data_complete / missing[]`。

## 7. 金融逻辑

**14 项指标**（含期间对齐表，v3.0 原方案缺失——角色 M3 点）：

| 指标 | metric 键 | 计算期间 | 来源 | 备注 |
|---|---|---|---|---|
| Revenue YoY | revenue | FY/TTM | CARD-02 normalize | 同比 |
| Single-quarter Revenue QoQ | revenue | SINGLE_QUARTER | CARD-02 差分后 | 环比 |
| Net Profit YoY | net_profit_parent | FY/TTM | CARD-02 normalize | 归母净利润（非合并净利润） |
| Deducted Net Profit YoY | net_profit_deducted | FY/TTM | CARD-02 normalize | |
| Gross Margin | (revenue − operating_cost) / revenue | FY/TTM | 派生 | 分母必须是同期间 revenue |
| Net Margin | net_profit_parent / revenue | FY/TTM | 派生 | |
| ROE | net_profit_parent / average_equity_parent | FY/TTM | 派生 | 平均净资产=期初+期末/2 |
| Operating CFO / Net Profit | cfo / net_profit_parent | FY/TTM | 派生 | 盈利质量诊断信号分母 |
| Receivables / Revenue | receivables / revenue | Revenue=FY/TTM, Receivables=期末 | 派生 | 混合期间，标注口径 |
| Inventory / Revenue | inventory / revenue | Revenue=FY/TTM, Inventory=期末 | 派生 | 混合期间，标注口径 |
| Contract Liabilities | contract_liabilities | 期末（POINT_IN_TIME） | 原始 | 存量值，不做同比/环比 |
| CapEx | capex | FY/TTM | CARD-02 normalize | |
| FCF | cfo − capex | FY/TTM | 派生 | CARD-02 cfo_minus_capex |
| Non-recurring Profit / Net Profit | (net_profit_parent − net_profit_deducted) / net_profit_parent | FY/TTM | 派生 | |

**4 类确定性诊断规则**：

| 条件 | 信号 |
|---|---|
| 利润上涨 + CFO 下降 | earnings_quality_warning |
| 收入上涨 + 应收增速显著高于收入增速 | receivable_risk |
| 收入上涨 + 存货增速显著高于收入增速 | inventory_pressure |
| 净利润上涨 + 扣非净利润下降 | non_recurring_contribution |

"显著高于"阈值集中为模块常量并注释依据。**信号只是信号**：LLM/报告不得将 signal 直接写成确定因果（validator 强制：signal 引用必须保留 signal 语义措辞，因果表述必须有独立证据）。

## 8. Edge Cases

1. Q4 单季 = FY − Q3YTD（任一缺失 → Q4 指标 None，不插值）
2. 扣非净利润缺失 → 跳过该指标 + data_complete=False（不得用净利润替代）
3. 亏损基期全部走状态迁移语义（第 6 节）
4. ROE 用加权平均 ROE 披露值优先，缺失则期末净资产近似并标注口径
5. 跨 scope/version 配对 → CARD-02 校验拒绝，本卡不重复实现
6. 期间序列不连续 → 断点处禁止跨断点环比，明确标注

## 9. Failure Mode

任一必备输入缺失 → 对应指标 None + missing 登记，绝不编造。诊断信号在输入指标不完整时静默不产生该信号（宁缺勿假）。

## 10. Validator Rules

- 所有 MetricResult 的 calculation_id 可解析（公式+输入证据可复核）
- 状态迁移语义的输出禁止出现百分比（validator 断言）
- signal 引用措辞检查（不得出现"因为...所以导致"式确定因果，除非有独立因果证据）

## 11. Unit Tests

- 指令算例：Q1=100/H1=230/Q3YTD=390/FY=540 → 单季 100/130/160/150 → QoQ 正确值
- 同比五分支（normal/扭亏/转亏/亏损扩大/亏损收窄）+ 零基期 not_computable
- 4 类诊断信号的正例/反例（各 ≥2 用例）
- 缺扣非/缺 Q3YTD/序列断点的降级行为

## 12. Integration Tests

CARD-01→02→03 全链跑合成年报；信号进入 Claim 生成（fact 级，evidence 绑定）。

## 13. Benchmark

Calculation Accuracy 指标接入 CARD-11：合成集上 14 指标数值精确匹配；真实集上与人工标注对照。

## 14. Demo Method

财务分析页：指标表（每行可展开公式与输入证据）+ 信号徽章（点击看触发条件的数值依据）。

## 15. 验收标准

- [x] 第 11 节全部封闭算例通过（含指令原文的 YTD 算例）
- [x] 状态迁移语义全覆盖且无百分比泄漏
- [x] 全部指标带 calculation_id 可回溯
- [x] 全部测试离线通过
- [x] §6 工具契约 `AnalyzeCnFinancialsTool`（入参 document_id，出参 status/metrics/signals/data_complete/missing[]）
- [x] §14 工作台「财务分析」页（指标表 + 可展开公式/输入证据 + 信号徽章）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-17 | **实现完成**。新增 `src/cn/analysis/`：`models.py`（MetricResult / DiagnosticSignal / MissingMetric / AnalysisResult + ComparisonKind / ComparisonSemantic / Unit）、`calculations.py`（`Calculation` + `CalculationRegistry`，记录公式与操作数并可 `recalculate` 复核）、`comparisons.py`（`classify` / `compare` / `compute_yoy` / `compute_single_quarter_qoq` / `compute_ratio` / `average_operand`）、`metrics.py`（§7 的 14 项指标 + 期间对齐表 + 比率公式表）、`series.py`（`MetricSeries` / `PeriodValue` / 期间键算术）、`diagnostics.py`（4 类信号）、`pipeline.py`（`analyze_financials` / `analyze_document`）、`evidence_bridge.py`（§12 账本发布 + §14 公式回链）。新增 `tests/test_cn_analysis.py` **68 项**、`tests/test_cn_document_service.py` 新增 **6 项**，共 **74 项全绿**。 |
| 2026-09-17 | **指令算例实测（§11 第一条，走既有期间引擎）**：年报披露 YTD `Q1=100 / H1=230 / 9M=390 / FY=540` → 单季 `100 / 130 / 160 / 150`，环比 `+30.00% / +23.077% / −6.25%`；Q1 不再产出环比行（无前序期，不臆造）。v2 的 `H1/Q1 − 1` 路径由 `compute_single_quarter_qoq` 硬性拒绝 —— 它同时检查 `NormalizedFact.period_kind`、引擎的 `PeriodValue.basis` 与 `MetricResult.period_basis`，任一不是 `SINGLE_QUARTER` 即抛错，不存在「默认按单季处理」的后门。 |
| 2026-09-17 | **纠偏 1（跨卡缺陷，重要）：CARD-02 `derive_periods` 从未真正产出单季/TTM。** `adapter.py` 原按 `fiscal_period` 分组（每组只剩一期），期间引擎每次只收到一条记录 → `single_quarters` 与 `ttm` 恒为空，本卡的单季环比与 TTM 指标全部静默缺失。已改为按 `(statement_type, scope, version, currency)` 跨**全部期间**重新分组（`ALL_PERIODS = "*"` 标记），引擎才拿到完整的累计序列。CARD-01/02 的 102 项测试在修复后无回归；本卡的 `Q1=100…FY=540 → 100/130/160/150` 即修复后的实测结果。这是 CARD-03 对 CARD-02 的第一次真实回压。 |
| 2026-09-17 | **纠偏 2：水平值行不得声明 `normal` 比较语义。** `compute_ratio` 原先给水平比率（毛利率/净利率等）写 `comparison_semantic=NORMAL`，而 `MetricResult.comparable` 正是 `comparison_semantic is NORMAL` —— 于是一行**没有任何比较**的指标会被下游读成「可比较」，而它的 `delta_pct` 恒为 `None`，调用方拿不到「这行本来就不是比较」的信号。已改为水平值行 `comparison_semantic=None`（`comparison=LEVEL` 保持不变），并有测试断言。 |
| 2026-09-17 | **纠偏 3：`required_inputs` 的重复项。** `_emit_yoy` 原用 `tuple(key for key, found in ((metric_key, current), (metric_key, previous)) if found is None)` 拼装缺失输入；同比两端是**同一个**指标键，整段序列缺失时会输出 `net_profit_deducted、net_profit_deducted`。已改为去重后的 `(metric_key,)`，并把「本期缺」/「上期缺」/「两期都缺」写成三种不同措辞（原先两期都缺时只报「缺少本期」，低估了缺口）。 |
| 2026-09-17 | **设计取舍：百分比不进证据账本。** `EvidenceLedger.add_calculation` 要求 ≥2 个已有证据输入且只做单一算术，而比率类指标自带公式常量 `×100`，`is_ledger_publishable()` 判为不可入账。桥接层因此只发布**可表达的子集**，其余以带类型的原因（`ledger_cannot_express_formula_constant` / `operation_not_expressible_in_ledger` / `input_not_present_in_ledger` / `duplicate_evidence_id`）记入 `skipped`，**绝不伪造一条「100」的证据**来凑数。`analysis_jumps()` 则对全部计算（含中间量 `gross_profit`、非经常性损益）给出公式 + 操作数 + 重算值，供 §14 UI 与 CARD-09 结论图使用。 |
| 2026-09-17 | **设计取舍：`data_complete` 必须区分「缺数据」与「不适用」。** 年报没有季度累计序列，单季环比对它**结构性不适用**，不是数据缺失。`MissingMetric` 因此带 `blocking` 标志：`data_complete = not any(blocking)`。否则每份年报都会因为一个它本就不可能有的指标而被判「数据不完整」。实测 `annual_01`：`revenue_qoq_single_quarter` 记入 `blocking=False`，`capex` 等 11 项记入 `blocking=True`。 |
| 2026-09-17 | **集成实测（CARD-01→02→03）**：双列（本期金额/上期金额）合成利润表 → 同比 `revenue +20%`、`net_profit_parent +20%`、扣非 `−11.11%`，毛利率 `41.67%`，净利率 `10.00%`，非经常性占比 `33.33%`，并触发 `non_recurring_contribution` 信号（归母 +20% 而扣非 −11%）；`annual_02`（万元表 + 仅资产负债表）→ 应收 `83 000 000 元` 正确、流量类指标全部缺席而非填 0；`annual_01`（无上期列）→ 同比行缺席且原因写明「缺少上一可比期间（2024FY）」，`scan_sample.pdf`（纯扫描页）→ 0 指标、14 项 missing、工具 status=`blocked`、不抛异常。 |
| 2026-09-17 | **工具与 UI**：新增 Agent 工具 `AnalyzeCnFinancialsTool`（`cn_tools.py`，与 ingest/normalize 共用同一 registry），出参严格对齐 §6 的 `status / metrics / signals / data_complete / missing[]`，另附 `blocking_missing` / `not_applicable` / `validation_errors` 与可选的账本发布。工作台新增「财务分析」页（`workbench.py::financial_analysis`），指标表每行可展开**公式 + 输入证据 id + 引擎重算值**，信号以徽章展示并可展开触发阈值与观测值。服务端读模型 `document_service.document_analysis()` 由 UI 与工具共用，UI 不自算任何数值；`scripts/smoke_workbench.py` 14/14 页干净。 |
| 2026-09-17 | **边界（明确不做，而非半做）**：§6 写的入参是「`document_id` **或** `symbol`」，本卡只实现 `document_id`。snapshot 口径的 `FinancialStatement.values` 用的是 provider 键（其 `net_profit` 实为**归母**），与 CARD-02 §7.1 字典的语义并非同名同义；把它直接接进分析层会绕开 CARD-02 的投影表，静默产生错值。按「零静默、零猜测」约定，`symbol` 入参返回带类型的 `analysis_missing_argument` 错误并在消息中写明原因，而不是给一个看起来能跑的结果。若评委需要 symbol 口径，应另立一卡做「provider 报表 → NormalizedFact」的规范化（含证据 id 命名空间与口径映射），不在本卡顺手夹带。 |
| 2026-09-17 | **未完成（下一步）**：§13 的 Calculation Accuracy 指标尚未接入 CARD-11（合成集上 14 指标数值精确匹配目前由 `tests/test_cn_analysis.py` 的封闭算例覆盖，但未产出可入库的基准分数）；§12 的「信号进入 Claim 生成」需等 CARD-09 结论图落地后再接。 |
| 2026-09-18 | **补齐 CARD-11 基准入口**：财务层基准现在会对分析结果中的可回算指标逐行计数，输出 `calculation_accuracy`，并保留每个文档的错误明细；数值仍由确定性计算注册表验证，不由模型判断。 |
| 2026-09-17 | **跨卡改动（由 CARD-04 施工，本卡记账）**：① `models.py`：`AnalysisResult` 增加 `scope_signals` / `comparability` / `comparability_blocked`（"未评估"与"评估后干净"是两种状态，前者为空列表、后者为 `comparability=None`）。② `comparisons.py`：`compute_yoy(..., suppress_reason=)` —— 口径被阻断时返回**水平值** `MetricResult`（`calculation_id=None`、`warnings=("comparability_blocked",)`）。传原因而不是布尔，是为了审计链里不可能出现一个光秃秃的 "blocked"。③ `pipeline.py`：`analyze_financials(..., scope_signals=)`；`block_yoy` 时把原因透传给每一个 `_emit_yoy`；`analyze_document(normalization, documents=)` 在调用方未传时**自行**从 `normalization.source_facts` 派生信号，闸门不可能因为"忘了传"而失效。④ `series.py`：`PeriodValue` 增加 `version`（比较两端的版本必须留在操作数轨迹上，CARD-04 §10 第 3 条规则问的正是这件事）；`_prefer` 先比版本新旧（取代规则）、再比 disclosed/derived；新增 `merge_versions()` 与 `newest_version()`。⑤ `merge_versions` 只在 `use_adjusted` 下调用：详见 CARD-04 §16「本卡对 CARD-03 的真实回压」—— 默认路径（无口径决策）行为一字未改，`(metric, scope, version)` 分组仍强制 CARD-02 §7.3 rule 4。 |
| 2026-09-19 | **修本卡真 bug（由 CARD-23 施工，本卡记账）**：`metrics.py` 指标表里 `REVENUE_YOY` / `REVENUE_QOQ_SINGLE_QUARTER` / `NET_PROFIT_YOY` / `DEDUCTED_NET_PROFIT_YOY` 四条**声明时未写 `unit`**，于是落到 `MetricDefinition` 的默认真 `Unit.YUAN`；而 `comparisons.py` 的 YoY 分支实际产出的就是 `Unit.PERCENT`。表声明与产出**潜在不一致**，长期没被发现是因为此前没有任何消费者拿「指标表的规范单位」去核对 claim/证据的单位 —— 数值路径不读 `unit`，所以错也不显形。CARD-23 §7.4 的**独立单位核对**以 `metric_definition(metric).unit` 为真值来源，第一跑就把它照了出来（`gross_margin` 一路正常，唯 YoY 四兄弟不对）。已补 `unit=Unit.PERCENT`，`tests/test_cn_proof.py::test_ratio_unit_is_compared_only_with_the_same_quantity` 与 `test_the_passports_unit_is_an_echo_not_a_check` 守着。**教训**：指标表的 `unit` 是「规范单位」这一**语义声明**，不是可选的展示字段；漏写不等于「单位未知」，而是静默声明成了元。新增指标务必同时写 `unit`。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
