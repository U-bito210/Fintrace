# CARD-02: Financial Normalization Engine（财务规范化引擎）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（9/23–9/29） |
| 对应赛题 | 赛题2/5 的数据基础（全主链） |
| 依赖 | CARD-01（ExtractedFact 输入）、**现有** `src/cn/periods.py`（FinancialPeriodEngine）、**现有** `src/cn/evidence.py`（Evidence Ledger 扩展）、**现有** `src/cn/domain.py`（FinancialStatement） |
| 实现复杂度 | 中高（约 4–5 人日） |
| 状态 | 🟡 实现完成 + 测试全绿（§15 最后一条受外部工件阻塞，见 §16） |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/normalization/`：把异构披露格式统一为规范金融数据。处理单位/币种/比率/期间/口径/版本六类规范化，**桥接而非重写**仓库现有 `FinancialPeriodEngine` 完成 YTD→单季和 TTM 推导，正确区分 flow 与 stock 语义，为主链所有计算提供干净输入。这是"数据准确性"评分项的地基。

**关键纠偏**：v3.0 原方案声称"v2 完全没有此层"且新建 `convert_ytd_to_single_quarter`——经审查仓库已存在 `FinancialPeriodEngine.derive_single_quarters()`（含 stock 禁差分抛错）和 `derive_ttm()`。本卡**不得重复造**：做薄适配器，复用现有引擎作为唯一单季化与 TTM 来源。

## 2. 对应竞赛评分点

数据准确性（单位/期间错误是研报最常见硬伤）、金融专业性（口径意识）、计算准确性（下游全部依赖）。

## 3. 为什么需要这个模块

真实 A 股披露的数值带各种单位（元/万元/百万元/亿元，甚至表头声明+正文混用）、期间（YTD 累计 vs 单季）、口径（合并/母公司）、版本（原披露/重述/更正后）。不规范化直接计算 = 必然出错。同时，必须把现有引擎的正确设计（stock 禁差分、TTM 推导、仅对 income/cashflow 生效）接入主链，不平行造轮子。

## 4. 输入 / 输出

- **输入**：`ExtractedFact[]`（CARD-01 产物，raw_value + unit + period + scope 原始字符串）
- **输出**：`NormalizedFact`：`{normalized_fact_id, metric, value(base 单位), currency, period_key, period_kind(ytd_cumulative | single_quarter | ttm | point_in_time), scope, version, available_at, source_fact_id, financial_statement_id}`；附加 `list[DerivedPeriod]`（单季化后，从现有引擎产出）

## 5. 数据模型

```python
class Unit(str, Enum):        # 元 M | 万元 W | 百万元 MM | 亿元 YI，基准=元
class Currency(str, Enum):    # CNY | USD | HKD | ...
class Ratio(str, Enum):       # PERCENT | BP | MULTIPLE
class Scope(str, Enum):       # CONSOLIDATED | PARENT
class Version(str, Enum):     # AS_REPORTED | RESTATED | CORRECTED
class PeriodKind(str, Enum):  # YTD_CUMULATIVE | SINGLE_QUARTER | TTM | POINT_IN_TIME

@dataclass
class NormalizedFact:
    normalized_fact_id: str
    metric: str               # 标准指标名（来自 §7.1 字典，不含层级歧义）
    value: float              # 基准元
    currency: Currency
    period_key: str           # 唯一期间键，复用现有 FinancialPeriodEngine 格式：YYYYQ1 / YYYYH1 / YYYY9M / YYYYFY
    period_kind: PeriodKind   # YTD_CUMULATIVE（披露值本身）| SINGLE_QUARTER（差分后）| TTM（推导后）| POINT_IN_TIME（stock）
    scope: Scope
    version: Version
    available_at: str
    source_fact_id: str       # 回链 CARD-01 的 ExtractedFact
    financial_statement_id: str | None  # 桥接到现有 FinancialStatement 的 id
```

**flow vs stock 语义**：flow（营业收入/营业利润/净利润/CFO/CapEx/费用）是期间流量，`period_kind` 可以是 `YTD_CUMULATIVE`（原始披露）或 `SINGLE_QUARTER`/`TTM`（引擎推导）；stock（现金/存货/应收账款/总资产/总负债）是时点存量，`period_kind` **只能是** `POINT_IN_TIME`，调用引擎会收到 `PeriodEngineError`（现有引擎已实现 stock 禁差分，本卡桥接时不吞此异常）。

## 6. API / Tool Contract

```python
# src/cn/normalization/units.py
def normalize_unit(raw_value: str | float, unit: str) -> float:
    """万元=10^4, 百万元=10^6, 亿元=10^8；基准单位统一为元。"""

# src/cn/normalization/periods.py（桥接现有引擎，不重写）
def canonical_period(raw_period: str, report_type: str) -> str:
    """返回现有引擎可接受的期间键：YYYYQ1 / YYYYH1 / YYYY9M / YYYYFY。
    错误键抛 ValueError，不猜测。"""

def detect_period_kind(source_table_name: str, column_header: str | None, report_type: str) -> PeriodKind:
    """YTD 判定规则（本卡核心，解决 v3.0 遗漏的判定盲区）——见 §7.2。
    无法判定 → 返回 None + warning，禁止猜测。"""

# src/cn/normalization/adapter.py（桥接现有 FinancialPeriodEngine）
def facts_to_financial_statements(normalized_facts: list[NormalizedFact]) -> list[FinancialStatement]:
    """把 NormalizedFact 投影到现有 src/cn/domain.FinancialStatement，
    scope→statement_type/scope, metric→value_dict, period_key→fiscal_period。
    单条失败则该 fact 不入投影，不中断其余。"""

def derive_single_quarters(statements: list[FinancialStatement]) -> list[DerivedPeriod]:
    """委托现有 FinancialPeriodEngine.derive_single_quarters()。
    输入校验：statement_type 必须是 income|cashflow；scope/version 同组内一致。"""

def derive_ttm(statements: list[FinancialStatement]) -> list[DerivedPeriod]:
    """委托现有 FinancialPeriodEngine.derive_ttm()（v3.0 原方案完全遗漏的 TTM 能力）。"""

# src/cn/normalization/scope.py / version.py
def detect_scope(table_header: str) -> Scope | None: ...
def detect_version(paragraph: str) -> Version: ...  # AS_REPORTED 默认；RESTATED/CORRECTED 信号来自 CARD-04

# src/cn/normalization/__init__.py 主入口
def normalize_document(facts: list[ExtractedFact], document) -> tuple[list[NormalizedFact], list[str]]:
    """返回 (NormalizedFact[], warnings[])。
    内部：单位换算 → canonical_period → detect_period_kind → detect_scope/version → 投影 FinancialStatement → derive_single_quarters → derive_ttm。"""
```

Agent 工具 `NormalizeCnFinancialsTool`：入参 `document_id`；出参 `status / facts / single_quarters[] / ttm[] / warnings[]`。**绝对禁止**自行实现单季化或 TTM——桥接层的存在就是为了强制走现有引擎。

## 7. 金融逻辑

### 7.1 标准指标字典（解决 v3.0 遗漏的"指标同义词+层级"问题）

每条显式绑定 A 股披露的**准确层级**，避免归母 vs 含少数股东混用、营业总收入 vs 营业收入混淆：

| metric 标准键 | 披露名称（同义词） | 财务报表位置 | 层级说明 |
|---|---|---|---|
| `revenue` | 营业收入 / 营业总收入 | 合并利润表 | 合并口径，不取母公司；"营业总收入"取合并表首行 |
| `operating_cost` | 营业成本 | 合并利润表 | |
| `gross_profit` | 营业收入 − 营业成本 | 派生 | 不得从"毛利"行取值（若有披露则取披露值） |
| `operating_profit` | 营业利润 | 合并利润表 | |
| `net_profit` | 净利润 | 合并利润表 | 取"合并净利润"行 |
| `net_profit_parent` | 归属于母公司股东的净利润 | 合并利润表 | **估值 EPS 用这个**（非净利润含少数股东） |
| `net_profit_deducted` | 扣除非经常性损益后归属于母公司股东的净利润 | 合并利润表 | CARD-03 的"扣非净利润"指标来源 |
| `cfo` | 经营活动产生的现金流量净额 | 合并现金流量表 | |
| `capex` | 购建固定资产、无形资产和其他长期资产支付的现金 | 合并现金流量表 | |
| `cfo_minus_capex` | CFO − CapEx | 派生 | FCF（简化版） |
| `total_assets` | 资产总计 | 合并资产负债表 | stock，禁止差分 |
| `total_liabilities` | 负债合计 | 合并资产负债表 | stock，禁止差分 |
| `equity_parent` | 归属于母公司股东权益合计 | 合并资产负债表 | stock；PB 的 BPS 来源 |
| `inventory` | 存货 | 合并资产负债表 | stock |
| `receivables` | 应收账款 | 合并资产负债表 | stock |
| `contract_liabilities` | 合同负债 | 合并资产负债表 | stock |
| `diluted_shares` | 期末总股本 / 稀释后股本 | 股本公告或资产负债表附注 | 时点；缺失 → 估值阻断 |
| `raw_price` | 最近交易日收盘价 | 快照提供者 | 行情链，**必须 RAW 价格** |
| `peers` | 可比公司集 | 快照提供者 | 用于 PE/PB/PS 同业估值 |

**规则**：字典外的 metric → `metric_unknown_xxx` 前缀 + warning；下游指标表不得引用未知 metric。

### 7.2 period_kind 判定规则表（解决 v3.0 遗漏的 YTD/单季判定盲区）

| 来源类型 | 识别条件 | period_kind | 期间键来源 |
|---|---|---|---|
| 合并利润表 / 合并现金流量表主体 | 表头/节名含"合并利润表"、"合并现金流量表" | **YTD_CUMULATIVE** | 文档声明的报告期（FY→YYYYFY，半年报→YYYYH1，三季报→YYYY9M，一季报→YYYYQ1） |
| 年报第三节"主要会计数据和财务指标"摘要表 | 表头含"本报告期/年初至本报告期末"双列 | 按列名："本报告期"→ **SINGLE_QUARTER**（不调用差分）；"年初至本报告期末"→ **YTD_CUMULATIVE** | 同报告期 |
| 季报"主要财务数据"表 | 表头同上双列 | 同上 | 同报告期 |
| 合并资产负债表 | 主体或附注 | **POINT_IN_TIME** | 报告期末日期 |
| 附注（含财务附注） | 段落文本提取的指标 | 按上上文判定；无法判定 → **None + warning** | |
| 公告 | 非报表段落 | **None + warning**（不猜） | |

**判定失败处理**：`period_kind=None` + `warnings.append(f"无法判定 {metric} 的期间语义，跳过单季化/TTM")`。**禁止**将无法判定的值默认当作 YTD 或单季——判错比不转换更糟。

### 7.3 其余规范化逻辑

1. **单位换算**：万元=10^4，百万元=10^6，亿元=10^8；基准单位统一为元
2. **表头优先**：表格数值单位以表头声明为准；正文与表头冲突 → 用表头 + 输出 warning，不静默
3. **期间键**：唯一格式 = `FinancialPeriodEngine` 接受的 `YYYYQ1/H1/9M/FY`；自定义 `period_key` 格式 → ValueError
4. **口径配对**：同比/环比计算（CARD-03）只能用同 scope 同 version 配对；跨 scope → 拒绝
5. **版本**：更正公告后的值标 CORRECTED；重述标 RESTATED（信号来自 CARD-04）

## 8. Edge Cases

1. 千分位逗号、括号负数（会计惯例 `(1,234)` = -1234）、"—"/"N/A" → 解析规则显式枚举
2. 表内混单位（某行万元某行元）→ 行级单位检测，无法判定 → 该行标 unparseable，不猜
3. 单位声明在页眉而非表头 → 向上搜索最近单位声明（限同页）
4. 币种为 USD/HKD 的财务附注 → 保留原币种，不换算，标注 currency
5. Q4 单季推导需要 FY 与 Q3YTD 同口径同版本，缺失 → Q4 为 None 并记录 missing（现有引擎自然处理）
6. 同期间同指标出现两个不同 scope 值 → 各自保留，靠 scope 字段区分
7. 季度报告披露的是 Q3YTD=390（9M 累计），不是 Q3 单季=160 → `detect_period_kind` 正确识别为 YTD_CUMULATIVE，差分引擎推导 Q3 单季

## 9. Failure Mode

任何无法确定语义的值 → `normalized_value=None` + warning，**绝不默认猜测**。规范化失败的 facts 汇总进 `warnings`，CARD-03 跳过对应指标并令 `data_complete=False`。

## 10. Validator Rules

- `normalize_unit` 输出必须与封闭算例一致（单位换算零容差）
- `canonical_period` 返回值必须匹配现有引擎正则 `^(?P<year>\d{4})(?P<kind>Q1|H1|9M|FY)$`，否则 ValueError
- `detect_period_kind` 返回 None 时必须有 warning，禁止返回猜测值
- 桥接层调用 `derive_single_quarters` 时若收到 `PeriodEngineError`（stock 输入）→ 重新抛出，不吞
- scope/version 配对检查供 CARD-03 调用（配对失败 → 禁止计算）

## 11. Unit Tests（封闭算例，手工可验）

- 单位换算全矩阵（元↔万元↔百万元↔亿元）
- **YTD 算例**（指令原文）：Q1=100, H1=230, Q3YTD=390, FY=540 → 单季 100/130/160/150（通过桥接层验证现有引擎输出）
- **TTM 算例**：FY2023=540, H12024=230, H12023=220 → H12024 TTM = 230 + 540 − 220 = 550（桥接现有 `derive_ttm`）
- 括号负数/千分位/N/A 解析
- stock 输入桥接后抛 `PeriodEngineError`（引擎侧）
- 表头正文单位冲突告警
- `detect_period_kind` 对摘要表双列正确分类
- `detect_period_kind` 对无法判定来源返回 None + warning
- 缺 Q3YTD → Q4=None（现有引擎自然处理）
- 混 scope 保留
- 未知 metric → `metric_unknown_xxx` + warning
- 错误期间键（`FY2024`, `2024Q1_YTD`, `Q1 2024`）→ ValueError

## 12. Integration Tests

CARD-01 合成年报 → 本卡 → NormalizedFact + Single Quarters + TTM 全部完整；接入扩展后的 Evidence Ledger（每 NormalizedFact 带回链 + document_id/page/bbox）。

## 13. Benchmark

Unit Accuracy / Period Accuracy / Scope Accuracy / **Period Kind Detection Accuracy** 四指标接入 CARD-11（合成集 + 真实集），目标待冻结。

## 14. Demo Method

UI 展示"原始披露值 → 规范化值"对照表（含单位换算、YTD→单季标注、TTM 推导），旁边列出 warnings——展示系统对脏数据的免疫力。

## 15. 验收标准

- [x] 第 11 节全部封闭算例通过（含指令原文的 YTD 算例和 v3.0 遗漏的 TTM 算例）
- [x] 桥接层代码路径存在（grep `derive_single_quarters|derive_ttm` 有调用），无自定义单季化/TTM 实现
- [x] flow/stock 语义分离有测试保护（stock 输入 → PeriodEngineError 重抛）
- [x] period_kind 判定失败 → None + warning，零猜测
- [x] metric 字典覆盖 §7.1 全部指标，未知 metric 走 `metric_unknown_xxx`
- [x] warnings 机制全覆盖（无法规范的值零静默）
- [x] 全部测试离线通过 —— **本卡新增 40 项 + CARD-01 59 项全绿**；套件另有 23 项失败，根因是 `data/` 下缺失的本地 tushare 快照（`data/` 整体 gitignore，仓库不含任何快照），与本卡无关，详见 §16「已知阻塞」 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-17 | **实现完成**。新增 `src/cn/normalization/`：`models.py`（Unit/Currency/Ratio/Scope/Version/PeriodKind + NormalizedFact，含 document_id/page/bbox/row/column 回链字段）、`units.py`（单位矩阵 + 会计惯例数值解析）、`metrics.py`（§7.1 字典 + 投影表）、`periods.py`（canonical_period + period_kind 判定）、`scope.py`、`version.py`、`adapter.py`（桥接 `FinancialPeriodEngine`）、`pipeline.py`（主流程）、`warning_log.py`（去重 + 计数告警）、`evidence_bridge.py`（§12 证据账本桥接）；新增 Agent 工具 `NormalizeCnFinancialsTool`（`cn_tools.py`，与 ingest 工具共用同一 registry）。新增 `tests/test_cn_normalization.py` **40 项测试全绿**。 |
| 2026-09-17 | **纠偏 1：`本报告期` 列的语义由报告类型决定。** §7.2 原表把年报摘要表的「本报告期」列直接定为 `SINGLE_QUARTER`。但「本报告期 = 本报告期覆盖的期间」：季报该列确为单季，**年报/半年报该列就是全年/半年累计**，标成单季即为错值（正是 §7.2 要防的「判错比不转换更糟」）。实现：`detect_period_kind(...)` 的 `report_type` 参数承担该区分 —— `quarterly` → `SINGLE_QUARTER`，`annual|interim` → `YTD_CUMULATIVE`。另将「年初至本报告期末」独立成 `ytd` 列角色（恒为 `YTD_CUMULATIVE`），不再与「本报告期」共用 `current`。 |
| 2026-09-17 | **纠偏 2：披露的单季值不入报表投影。** 单季的唯一来源是现有引擎的差分（`derive_single_quarters`）。摘要表「本报告期」列的单季值改为：保留为 `NormalizedFact`（`period_kind=SINGLE_QUARTER`）用于交叉校验，但**不投影为 `FinancialStatement`**，避免与引擎按 YTD 差分出的单季形成第二套口径。引擎无法解析的 `YYYYQ3` 之类期间键也因此不会进入报表（引擎正则只接受 Q1/H1/9M/FY）。 |
| 2026-09-17 | **纠偏 3（跨卡缺陷）：`单位：万元` 曾被识别为 `元`。** CARD-01 `TableParser._detect_money` 的正则用贪婪中文标签组吞掉了「万元」的「万」，只匹配到尾字「元」，即万元表被标为元 —— 10⁴ 量级错误，CARD-02 会忠实放大它。已改为单位分支**最长优先**并显式消费币种词（`table.py::_MONEY_RE`）；CARD-01 的 39 项测试与合成集基准无回归。这是「数值规范化」层对文档层的第一次真实回压。 |
| 2026-09-17 | **纠偏 4：`net_profit` 的语义陷阱。** 仓库既有 `FinancialStatement.values["net_profit"]` 实为**归母**净利润（`tushare.py` 映射 `n_income_attr_p`，`report.py` 标注「归母净利润」），而 §7.1 把 `net_profit` 定义为**含少数股东**、归母为 `net_profit_parent`。若直接同名投影，两者会静默混淆（§7.1 明令禁止）。实现：新增显式投影表 —— `net_profit_parent → values["net_profit"]`（沿用既有契约），`net_profit → values["net_profit_incl_minority"]`（新键，永不与归母合并）；`equity_parent → values["equity"]`、`diluted_shares → values["total_shares"]`、`cfo → values["operating_cash_flow"]`、`cfo_minus_capex → values["free_cash_flow"]` 同理。 |
| 2026-09-17 | **封闭算例实测**（经桥接层）：YTD `Q1=100/H1=230/9M=390/FY=540` → 单季 `100/130/160/150`；TTM `FY2023=540,H1 2024=230,H1 2023=220` → `H1 2024 = 550`；`9M` 缺失 → Q4 不产出（不猜）；`balance` → `PeriodEngineError` 重抛；`FY2024`/`2024Q1_YTD`/`Q1 2024`/`2024-01-31` → `ValueError`。 |
| 2026-09-17 | **集成实测**（CARD-01 合成语料 → 本卡）：`annual_01`（续表合并 + 双列）→ 13 条规范化事实、`income/2025FY` 报表含 `revenue=1 234 567`、`net_profit=330 000`（归母）、`net_profit_incl_minority=345 678`、派生 `gross_profit=434 567`，并对双列同期间冲突发出告警（交 CARD-13 裁决）；`annual_02`（万元 + 缺发布日期）→ `8 300 万元 = 83 000 000 元`，`期初余额` 确定性落到 `2023FY`；`annual_03` 扫描页不产出任何事实。 |
| 2026-09-17 | **已知阻塞（非本卡引入，范围已收窄）**：`python -m pytest -q -m "not integration"` = 394 passed / 23 failed。23 项**全部**依赖 `data/snapshots/cn/600519.SH_20260810_tushare_v1.json` 与 `300750.SZ_20260812_tushare_v1.json`。`data/` 被 `.gitignore` 整体忽略、全盘无副本，`data/provider_probes/cn0/`（重建快照所需的 probe CSV）亦不存在 → 需真实 Tushare token 跑 `probe_tushare_cn0.py` → `build_cn_snapshot_from_probe.py` 重建。按 `AGENTS.md`「不得把 fixture 当真实来源」的约束，未伪造快照来「修绿」。原 54 项中的另外 31 项（三份 benchmark 用例文件缺失）已通过把用例定义移入版本控制解决，见下条。 |
| 2026-09-17 | **修掉一个仓库级缺陷（原 31 项失败的根因）**：`data/benchmarks/cn_agent_v1.json`（20 例）、`cn_ablation_v2.json`、`cn_agent_ablation_v3.json`（7 例）是**手写的基准用例定义**（三份脚本的 `--cases` 输入），却放在被 `.gitignore` 整体忽略的 `data/` 下，从未进过 git、git 历史与全盘都无副本 → 全新 clone 必然红。已按测试给出的契约重建这三份用例并落到 `tests/fixtures/cn/benchmarks/`，同步更新 3 个测试与 3 个脚本的路径常量。**性质说明**：这是「按测试契约重建」而非「还原原文件」，用例内容按真实工具契约、真实快照日期与 §7.1 指标语义编写；已在文件内 `description` 中标注。 |
| 2026-09-17 | **未完成（下一步）**：§13 的 Unit / Period / Scope / Period-Kind Detection 四项准确率尚未接入 CARD-11 基准（`scripts/gen_document_fixtures.py --benchmark` 目前只跑抽取 P/R/F1，合成集 micro F1 = 1.0000）。§14 的「原始披露值 → 规范化值」UI 对照表随 CARD-15 一起做。 |
| 2026-09-18 | **补齐 CARD-11 基准入口**：新增 `src/cn/documents/financial_benchmark.py`，对合成语料逐条测量单位、期间语义、口径和计算回算准确性；`scripts/gen_document_fixtures.py --benchmark` 现在会额外写出 `financial_layer_baseline.json`。该报告明确标注 synthetic，不代表真实财报效果。 |
| 2026-09-17 | **增量（CARD-04 前置条件）：版本词表收归共享模块，本卡改为消费方。** 新建 `src/cn/disclosure_lexicon.py`，位于 `normalization` 与 `analysis` **之下**（只 import 标准库）—— CARD-03/04 已经 import CARD-02，词表若放进 `analysis` 会让 CARD-02 的 import 成环。`version.py` 原来的本地 `_CORRECTED_RE`/`_RESTATED_RE` 已删除，改为消费 `DISCLOSURE_CORRECTED`/`DISCLOSURE_RESTATED`；两个卡片从此不可能"一个认得出、另一个认不出"同一句措辞。措辞表的所有权在 CARD-04（口径语义属它），本卡只读。 |
| 2026-09-17 | **增量：列级版本优先于文档级版本。** 新增 `version_from_column(meta)`：读 CARD-01 写在单元格 `meta["version_label"]` 上的列版本 token → `Version`；**未标注时返回 `None` 而不是 `AS_REPORTED`** —— 返回 `AS_REPORTED` 会静默吃掉一张「调整后」列。`pipeline` 逐事实取 `column_version or document_version`，两者不一致时发告警。此前版本标注只到文档级，一份「调整前/调整后」双列表的两套 facts 会同标 `AS_REPORTED` 并在同一个 CARD-03 序列里撞车。 |
| 2026-09-17 | **增量：`NormalizationResult.source_facts`。** 规范化结果现在带回它在 CARD-01 侧的来源事实，使 CARD-04 可以在**不要求调用方重新传一遍抽取结果**的前提下检测口径信号（`analyze_document` 因此不可能"忘记传"而静默跳过可比性闸门）。 |
| 2026-09-17 | **增量纠偏：`_derive_metrics` 不得在期间语义未定的行上派生。** 「前期差错更正影响金额」这类附注表不在报表白名单内，其 `period_kind` 为 `None`，但报告期会被 `report_period` 盖成 `2025FY`，于是 `gross_profit = revenue - operating_cost` 会被派生出来并挂到一个语义不明的事实上（一个自信的错值）。已加 `if minuend.period_kind is None: continue` + `derive-undecided-period` 计数告警。 |
| 2026-09-19 | **修一处真 bug（CARD-22 发现，跨卡改动）：更窄的小计冒充其父汇总。** `resolve_metric` 的「最长同义词胜出」把「**流动资产合计**」映射到 `total_assets`（其同义词表里含「资产合计」这类更短字面），于是在按 `(statement, period)` 投影时顶掉了文档实际印刷的「资产总计」—— 报表上明明印了汇总行，系统却拿它的子项顶上，且下游（CARD-03 比率、CARD-22 F1 恒等式）不会收到任何提示。新增 `_is_narrower_aggregate()` 守卫：候选标签若被另一条已匹配的更宽标签**包含**且自身更窄，一律拒绝映射并记 unknown，**绝不静默出值**。新增 `tests/test_cn_normalization.py::test_narrower_subtotals_never_impersonate_their_parent_total`（三个更窄小计被拒 + 「资产总计」存活），既有规范化测试全部不变。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
