# CARD-08: Relative Valuation & Sensitivity（相对估值与敏感性）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（10/7–10/11） |
| 对应赛题 | 赛题4 重要金融工具（初赛版） |
| 依赖 | CARD-02（规范化财务）、CARD-07（计算记录） |
| 实现复杂度 | 中（约 3–4 人日） |
| 状态 | 🟡 初赛版已落地（引擎/校验器/工具/测试全绿；仅 §14 工作台热力图页未接，见 §16） |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

扩展现有 `src/cn/valuation.py`（保留 PE/PB/PS + IQR 离群剔除 + RAW 价格纪律）：增加 Bear/Base/Bull 三情景、EPS×Target PE 与 ROE×Target PB 敏感性矩阵、Assumption Registry 登记全部输入假设，实现每个估值结果"数据→公式→假设→输出"四层可追溯。**不做**与 DCF 区间的交集/并集（无理论依据，v2 错误设计，已废弃）。

## 2. 对应竞赛评分点

自动化估值建模考察点：模型结构、假设依据、财务勾稽、结果合理性、敏感性分析完整性。

## 3. 为什么需要这个模块

赛题4 是初赛版估值主答案（DCF 降决赛）；现有能力缺敏感性分析（通知明确考察"敏感性分析完整性"）与假设登记（"假设依据"考察点）。

## 4. 输入 / 输出

- **输入**：规范化财务（EPS、ROE、BPS）、RAW 价格与股本、同业可比集、情景假设集
- **输出**：`ValuationResult`（各方法区间 + 敏感性矩阵 + dispersion 报告 + 假设清单）

## 5. 数据模型

```python
@dataclass
class Assumption:            # Assumption Registry 单条
    assumption_id: str
    value: float | str
    source: str             # evidence_id | "assumption: 理由"
    reason: str
    valid_from: str

@dataclass
class ScenarioValuation:    # 每方法每情景
    method: str             # pe | pb | ps
    scenario: str           # bear | base | bull
    value_low: float
    value_mid: float
    value_high: float
    assumption_ids: list[str]
    calculation_ids: list[str]

@dataclass
class SensitivityMatrix:
    x_name: str             # 如 Target PE
    y_name: str             # 如 EPS（或 ROE × Target PB 版本）
    x_values: list[float]
    y_values: list[float]
    matrix: list[list[float]]   # implied price
    assumption_ids: list[str]

@dataclass
class ValuationResult:
    scenarios: list[ScenarioValuation]     # 各方法分别展示，不合并
    sensitivity: list[SensitivityMatrix]
    method_dispersion: dict                 # 各方法区间离散度报告
    applicability_note: str                # 方法适用边界（确定性模板）
```

## 6. API / Tool Contract

```python
def build_scenarios(base_inputs, assumption_registry) -> list[ScenarioValuation]: ...
def sensitivity_pe_eps(eps_range, target_pe_range, shares) -> SensitivityMatrix: ...
def sensitivity_pb_roe(roe_range, target_pb_range, bps, shares) -> SensitivityMatrix: ...
def method_dispersion(scenarios) -> dict: ...
```

Agent 工具 `RunCnRelativeValuationTool`（扩展现有估值工具或新增）：入参 `symbol`；出参 `status / scenarios / sensitivity / dispersion / assumptions`。

## 7. 金融逻辑

1. **EPS/ROE 期间定义**（v3.0 原方案完全遗漏，这是角色 B 必查点）：EPS = `net_profit_parent_ttm / diluted_shares`（TTM，来自 CARD-02 的现有引擎）；**禁止**用 FY 静态 EPS 做估值（PE 估值隐含的是"未来 12 个月盈利"）。ROE = `net_profit_parent_ttm / average_equity_parent`（平均净资产，近似取期初+期末/2）。
2. **情景定义**：Bear/Base/Bull 的 EPS 增速与目标倍数各登记假设（如 base=分析师一致预期若有证据，否则 assumption 并标注）
3. **敏感性**：EPS × Target PE 矩阵（行=EPS 区间，列=目标 PE 区间，格子=EPS×PE，y 轴语义直接）；**PB 矩阵初赛简化**（v3.0 原方案"ROE × Target PB 矩阵"数学上 ROE 不进价格公式，格子会是 BPS×PB 与 y 轴 ROE 无关）：初赛 ROE × PB 矩阵 **二选一**——①初赛直接用 BPS × PB 矩阵（诚实，y 轴放 BPS 情景而非 ROE）+ PB 适用边界说明；②决赛做"PB = f(ROE, COE, g)"显式假设模型（类比 Gordon 增长模型），矩阵格子=BPS×PB 且 PB 由 ROE 推导，全部入 Assumption Registry。**初赛采用选项①**（简化版，决赛做②）。
4. 各方法（PE/PB/PS）区间**分别展示**，输出 method_dispersion（如各方法 mid 的极差/均值），不制造合成"最终区间"
5. 市值与倍数计算继续使用 RAW 价格（现有纪律）
6. 负 EPS / 负 BPS → 对应方法标注 not_applicable（不输出负 PE 伪区间）

## 8. Edge Cases

1. 同业可比不足 3 家 → IQR 不做，标注样本不足
2. 总股本缺失 → 阻断每股估值（fail-closed，对应失败注入演示 #1）
3. 假设覆盖 sourced 参数 → 拒绝（只允许登记 assumption 性参数）
4. EPS 为负 → PE 方法 not_applicable，PB/PS 继续
5. 分红导致 BPS 异常 → 数值如实，边界说明模板标注

## 9. Failure Mode

输入数据缺失 → 对应方法整体缺位 + missing 登记，不用别的方法的值填补。全部假设无 sourced 依据 → 结果整体降级标注"纯假设演示"。

## 10. Validator Rules

- 每个 ScenarioValuation 的 assumption_ids 全部可解析
- 敏感性矩阵单调性检查（PE↑→价格↑；EPS↑→价格↑）
- dispersion 报告必须存在（禁止只给单点值）

## 11. Unit Tests

- 矩阵手工算例（EPS 3 档 × PE 3 档，手算 9 格核对）
- 单调性断言；负 EPS 的 not_applicable；股本缺失阻断
- 假设登记完整性（无 assumption_id 的结果被拒）

## 12. Integration Tests

CARD-02→08→10 全链：估值结果进备忘录，点击任一单元格 → 假设 → 数据 → 证据。

## 13. Benchmark

Calculation Accuracy（估值数值精确匹配）接入 CARD-11。

## 14. Demo Method

估值页：三方法区间条 + 敏感性矩阵热力图 + 点击单元格展开假设来源。

## 15. 验收标准

- [x] 敏感性矩阵手工算例 + 单调性测试通过
- [x] 全部假设入 Registry 且可追溯（数据→公式→假设→输出链路测试）
- [x] 方法分别展示 + dispersion 报告（无合成区间）
- [x] 全部测试离线通过 ← **原未勾**：CARD-08 新增测试全绿，但全量离线游标仍有 **23 项既有失败** → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）
      （全部来自缺失的 Tushare 快照，分布于 `test_cn_workbench_service`/`test_workbench_api`/
      `test_cn_report`/`test_cn_report_cli`，非本卡引入）。判据 = **失败集合逐条不变**，已核对与基线一致。

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | 落地 CARD-08 初赛版。引擎 `src/cn/valuation.py` 扩充：`Assumption`/`AssumptionRegistry`（sourced vs `assumption:` 两类，禁止覆盖 sourced）、`ValuationInputs`（fail-closed：总股本缺失/非法 → `ValueError(SHARES_BLOCK_REASON)`）、`ScenarioValuation`、`SensitivityMatrix`、`ValuationResult`、`build_scenarios`/`sensitivity_pe_eps`/`sensitivity_pb_bps`/`method_dispersion`/`build_valuation`。校验器 `src/validation/valuation_validator.py`（假设可解析、矩阵单调且逐格可重算、dispersion 须存在且每方法 ≥2 点、价格基准须 RAW、blocked 须带原因）。工具 `RunCnRelativeValuationTool`（`run_cn_relative_valuation`）接入 `build_cn_snapshot_tools`。 |
| 2026-09-19 | **偏差 1**：§6 契约名 `sensitivity_pb_roe` 与 §7.3 冲突——ROE 不进价格公式，格子会是 `BPS×PB` 与 y 轴 ROE 无关。按 §7.3 **选项①** 实现为 `sensitivity_pb_bps`（y 轴 = BPS 情景，诚实版），PB 适用边界写入 `APPLICABILITY_NOTES`；决赛再做"PB=f(ROE,COE,g)"（选项②）。函数名变更已记于此，未回改 §6 契约（保持文档原貌）。 |
| 2026-09-19 | **偏差 2**：`ValuationResult.status` 枚举定为 `blocked` / `assumption_only` / `partial` / `ok` 四态（卡 §9 只描述"输入缺失→方法缺位""全无 sourced→纯假设演示"，未定名）。`ok` = 至少一方法 applicable 且该情景全部假设 sourced；`assumption_only` = 有值但无 sourced 依据（降级标注"纯假设演示"）。 |
| 2026-09-19 | **偏差 3**：§7.1 TTM——`valuation_inputs_from_normalization` 直接复用 CARD-02 `FinancialPeriodEngine` 的 `result.ttm`（`H1(本年)+FY(上年)-H1(上年)`），**不用** FY 静态 EPS。FY 边界期 TTM==FY 时出 `ttm_period_is_fy_boundary` warning **如实报告**（不隐瞒）。 |
| 2026-09-19 | **偏差 4**：工具仅接受 `document_id`；传入 `symbol`（无 document_id）→ 显式拒绝并提示改用 document_id（与 `AnalyzeCnFinancialsTool` 同纪律，避免绕开 CARD-02 投影表静默出错值）。同业可选：给了 `peers` 必须同时给 `peer_industry_code`，否则拒；同业中位数仅**播种**目标倍数，仍逐条登记进 Assumption Registry（source=peer 证据 id）。 |
| 2026-09-19 | **顺手补的跨卡前置**：原有合成语料 `SYN-ANNUAL-01..05` 均**不含总股本/归母权益**，CARD-02→08 无法离线全链。新增 `SYN-ANNUAL-06`（`annual_06_equity_shares.pdf`，示例医药 2025 年报：归母净利润 3,600,000/2,800,000、期末总股本 4,000,000/3,800,000、归母净资产 30,000,000/26,000,000），12 条 GoldRecord，`gen_document_fixtures.py --check` pass（sha256 `d86df2f65772`）。该 docs 文档库语料变动属合成 fixture 扩展，未触碰 CARD-01/02 契约语义。 |
| 2026-09-19 | 测试：`tests/test_cn_valuation.py` 重写 **42 项全绿**（含 §7.1 TTM 驱动、§12 CARD-02→08 全链 on `annual_06_equity_shares.pdf`、validator 反例、假设完整性、工具 fail-closed）。全量离线 **764 passed / 23 failed / 0 errors / 2 skipped**，失败集合与基线逐条一致（passed 净增 39）。冒烟 14/14 clean，`git diff --check` clean。 |
| 2026-09-19 | **未闭合**：§14 工作台估值页仍只读 `service.research_peer_valuation`（同业倍数表），**敏感性矩阵热力图 + 点击单元格展开假设来源未接** `workbench.py`（服务层已具备 `build_valuation` 输出）。§13 Calculation Accuracy 基准未接 CARD-11。二者不阻塞 §15 验收。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
