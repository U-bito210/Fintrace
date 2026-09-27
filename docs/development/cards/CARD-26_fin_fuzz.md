# CARD-26: FinFuzz — Financial Semantic Adversarial Benchmark（金融语义对抗错误生成与压力测试框架）

| 属性 | 值 |
|---|---|
| 优先级 | P1（初赛增强；不阻塞主链） |
| 竞赛阶段 | 初赛版（10/12–10/15 窗口 5；贯穿评测） |
| 对应赛题 | 北京赛：赛题7 内部评价机制（评测创新）；华北五省：Adversarial Testing |
| 依赖 | CARD-05（Report Checker）、CARD-09（Claim 结构）、CARD-11（Benchmark 集成） |
| 实现复杂度 | 中（约 4 人日） |
| 状态 | 🟡 代码+74 测试就绪（2026-09-19 收尾，全量离线 1445 passed / 23 failed）；§17 demo 交互已随 CARD-15 落地（2026-09-20）；holdout 未采集，见 §16 |
| Skill 要求 | 动手前加载 financial-agent-evaluation |

> 本卡前身：v3.1 的 "FinFuzz"。v3.2 明确其定位：**FinFuzz 不是业务核心功能，它是验证 FinTrace 可信能力是否真的有效的测试框架**，必须集成到 Benchmark CARD（CARD-11）。

## 1. Objective（目标）

新建 `src/cn/finfuzz/`：金融语义 mutation suite——从正确事实/真实业务研报生成 adversarial variants，系统性地测试系统的错误检测能力。金融研报中的错误是**语义级别**的（单位、期间、口径、因果、引用），传统测试无法系统性覆盖。

## 2. Competition relevance（双赛分别解决什么评审问题）

| 比赛 | 解决的评审问题 |
|---|---|
| 华北五省计算机设计大赛 | 展示 **Adversarial Testing**：主动攻击自己的系统（mutation → 检测 → 分层指标），证明可信能力是真的而非演示脚本 |
| 北京市大学生金融人工智能竞赛 | 回答赛题7"内部评价机制"：per-error-type 的 Precision/Recall/F1/误报率，说明系统对每一类金融语义错误的具体检测能力 |

## 3. Why this is not a gimmick（为什么不是点缀）

普通评测只测"正确输入 → 正确输出"。FinFuzz 测"**篡改后的输入 → 系统能否识别篡改**"，并**按错误类型分层**报告（不能只统计总 F1）。它把"系统是否真的可信"从口头声明变成可复现的对抗测试结果。

## 4. Inputs / 5. Outputs

- **输入**：正确 Claim 集合 / 真实研报切片 + Mutation Operators + 被测系统（Checker/Validator）
- **输出**：`MutationSpec[]` + `MutationResult[]` + `FinFuzzReport`（Overall + per-error-type metrics）

## 6. Data Models（数据模型）

```python
class MutationOperators(str, Enum):
    NUMERIC = "numeric"                 # 小数点移动/量级：1.26 → 12.6
    UNIT = "unit"                       # 万元↔亿元, CNY↔USD, %↔bp
    SIGN = "sign"                       # -3.2% ↔ +3.2%
    PERIOD = "period"                   # FY↔H1, Q2↔Q3, 2024↔2025
    YTD_VS_SINGLE = "ytd_vs_single"     # 9M cumulative 冒充 Q3 single
    SCOPE = "scope"                     # consolidated ↔ parent
    VERSION = "version"                 # corrected ↔ as-reported old value
    VALUATION = "valuation"             # PE_TTM ↔ PE_NTM, PE ↔ PB
    CITATION = "citation"               # 正确数字 + 错误页码 / 错误 Evidence ID
    CAUSAL = "causal"                   # 无 management attribution 时把并列改为因果

@dataclass
class MutationSpec:
    mutation_id: str
    operator: MutationOperators
    source_claim: dict                  # 原始正确 claim
    mutated_claim: dict                 # 变异后 claim
    description: str
    expected_detection: bool            # 应被检出的真变异（部分用于误报测试的"伪变异"为 False）

@dataclass
class MutationResult:
    spec: MutationSpec
    detected_by: str | None             # checker | validator | none
    detected: bool
    detection_time_ms: int
    error_type_classified: str | None

@dataclass
class FinFuzzReport:
    suite_id: str
    overall: dict                       # {precision, recall, f1, false_positive_rate}
    per_error_type: dict                # {numeric: {recall, n}, unit: {...}, period: {...}, scope, version, citation, causal, sign, ytd_vs_single, valuation}
    baseline_holdout: dict              # holdout 结果（防止对测试集过拟合调优）
    extractor_vs_checker: dict          # 区分 Claim Extractor failure 与 Checker failure
```

## 7. Algorithms（算法）

### Mutation Operators（第一阶段至少 10 类，见 §6）
每个 operator 实现为 `<source> → <mutated>` 的确定性转换函数：
```python
def mutate_numeric(claim, factor=10) -> dict: ...      # 1.26 → 12.6
def mutate_unit(claim, from_unit, to_unit) -> dict: ... # 万元→亿元（数值同比例换算，形成单位语义错误）
def mutate_sign(claim) -> dict: ...
def mutate_period(claim, from_, to) -> dict: ...
def mutate_ytd_vs_single(claim) -> dict: ...
def mutate_scope(claim) -> dict: ...
def mutate_version(claim) -> dict: ...
def mutate_valuation_basis(claim) -> dict: ...
def mutate_citation(claim, wrong_page|wrong_evidence) -> dict: ...
def mutate_causal(claim) -> dict: ...
```

### 关键用例设计（与检测能力一一对应）
- `numeric`：1.26 → 12.6（数量级）
- `unit`：3.1 亿元 → 31,000 亿元（量级+单位混合场景按 unit operator 归类）——注：3.1 亿 = 31,000 万，**"3.1 亿元写成 31,000 万元"数值相等不是错误**，必须用 31,000 亿元这类真实量级错误
- `causal`："原材料上涨"+"毛利率下降"（原文并列）恶意改成"原材料上涨导致毛利率下降"——无 management attribution → 应识别为 unsupported causal claim

### 指标计算
- Overall Precision / Recall / F1 / False Positive Rate
- **按错误类型独立**：Numeric/Unit/Period/Scope/Version/Citation/Causal（+ Sign/YTD/Valuation）Error Recall
- 必须区分：Claim Extractor failure（提取器失败）vs Checker failure（核查器失败）

## 8. API / Tool Contract

```python
# src/cn/finfuzz/suite.py
def generate_mutation_specs(claims, operators, seed=42) -> list[MutationSpec]: ...
def run_suite(specs, targets) -> list[MutationResult]: ...   # targets = {checker, validator}
def compute_metrics(results) -> FinFuzzReport: ...
def run_finfuzz(suite_id, claims, operators, targets, holdout=None) -> FinFuzzReport: ...
```

CLI：`python -m src.cn.finfuzz --operator numeric,unit,... --set synthetic|real --out output/benchmark/finfuzz/`。集成进 CARD-11 的 `--suite cn_agent_v2` 体系。

## 9. Deterministic vs LLM boundary（确定性与 LLM 边界）

- mutation 生成是**确定性函数**（seed 固定，结果可复现）
- 被测对象（Checker/Validator）是确定性模块；对 LLM 提取器的评测以离线 mock 接口隔离
- 评测报告由确定性代码生成，**禁止编造结果**

## 10. Dependencies（依赖）

CARD-05（Checker 目标）、CARD-09（Claim schema 与 Validator 状态）、CARD-11（benchmark 集成与报告）。
协议：FinFuzz 只在 CARD-11 的评测流水线调用，不进入业务主链。

## 11. Failure Modes

- MutationSpec 生成失败 → 跳过并记录（不影响其余）
- 被测系统超时 → 标记 timeout，按 miss 处理（在报告中注明）
- 指标分母为零（某 error type 无样本）→ 该 type 记 `N/A`，不删除行

## 12. Edge Cases

1. 变异后仍正确（数值未真变）→ 应标记 `expected_detection=false` 作为误报压力样本
2. 多 operator 组合变异 → 记录组合标签
3. 同一 claim 重复变异 → 去重
4. holdout：FinFuzz 调优不得触碰 holdout 子集

## 13. Validator Rules

- 每个 MutationSpec 必须有 source_claim 与 mutated_claim 的 diff 描述
- 报告必须区分 extractor failure 与 checker failure
- 误报样本（expected_detection=false）计入 false_positive_rate，禁止从报告中剔除

## 14. Unit Tests

- 每个 operator ≥ 1 个确定性用例（1.26→12.6、31,000 亿 case、causal 合并 case 等手算可验）
- 指标计算器正确性（构造已知 P/R/F1 假结果核对）
- seed 固定 → 输出一致

## 15. Integration Tests

- 与 CARD-05 Checker 集成：注入 mutations → 检测结果符合预期（10 个 operator 全跑）
- 与 CARD-11 集成：`--suite cn_agent_v2 --set synthetic` 包含 finfuzz 报告

## 16. Benchmark

输出：Overall Precision/Recall/F1、False Positive Rate、per-error-type Recall、Holdout Performance。**Target / TO BE MEASURED**——未运行前只写目标不写假数字，接入 CARD-11 报告体系。Ablation 对照：`无 FinFuzz 常规测试` vs `含 FinFuzz` 的错误检测覆盖率。

## 17. Demo Flow

Demo 步骤 E2（DEMO_FLOW.md）：FinFuzz 注入一个单位/期间/因果错误到正确研报 → Report Checker/Validator 捕获 → 展示错误类型、原始值、正确值、证据、建议。答辩展示批量报表（per-error-type recall 表格）。

## 18. Acceptance Criteria（验收标准）

- [x] 10 个 Mutation Operator 全部实现并有确定性用例
- [x] 指标计算：Overall + per-error-type 全部有单测
- [x] extractor vs checker 区分有测试
- [x] 与 CARD-11 集成测试通过
- [x] 报告可复现（seed 固定）
- [x] 无编造数字（未运行时为 TARGET/TO BE MEASURED）

## 19. Priority

**P1**（初赛增强；核心 5 个 operator——numeric/unit/period/scope/citation——优先，其余跟进）；不阻塞主链。

## 20. Competition Stage

初赛：10 类 operator 基础版 + per-error-type 指标；决赛：更多 operator（汇率/股份拆分）、自适应变异、高级可视化。

## 21. Estimated Complexity

约 4 人日（operators 2 + suite/指标 1 + benchmark 集成 1）。

## 22. Fallback / Degradation Strategy

- 时间不足：5 个核心 operator + Overall 指标先行，per-error-type 完整化决赛补
- 无真实数据 → Synthetic 集先行，Real 集标注后接入
- 断网不影响（全部本地确定性）

## 23. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **开工前可行性核查（技能第 2 步，别跳）**：把 15 份语料逐份送进 CARD-05，确认 `SYN-DRAFT-CLEAN-01/02` 产出 claims **零 finding** —— 这是整套框架的前提，变异必须从**已被证实正确**的 claim 出发，否则"检测到了"分不清是变异被抓住还是原文本来就有错。再逐算子探针：10 个算子都有**可达的检测路径**；文本通道确实暴露**真实的抽取器失败**（标题锚定的期间篡改、全角数字）。 |
| 2026-09-19 | **两个通道是有意的分工，不是冗余**：`CLAIM` 通道直接改字段、**绕过抽取器**，因此一次漏检**只能**归因于核查器；`TEXT` 通道改句子再**重新抽取**，把抽取器的失败暴露出来。`failure_stage ∈ {extractor, checker}` 的判定规则是：文本通道且篡改**未通过抽取**（`_carried` 为假）⇒ extractor，否则 ⇒ checker。少了这条区分，抽取器吃掉的错会被记成核查器的盲点，评测结论直接反过来。 |
| 2026-09-19 | **十个算子实测 8 个有样本、2 个 N/A，且 N/A 行保留不删**：`ytd_vs_single`（语料里没有"只声明累计值、未声明单季口径"的 claim）与 `valuation`（没有"带可重算分母的 PE/PB/PS 声明"）在现行合成语料上**生不出 spec**。按卡 §11 记 `recall=N/A, n=0` 并把 `no_spec_generated` 写进 note。**删除这两行会把"没测"伪装成"全测且都过"** —— 报告里必须看得见缺口。 |
| 2026-09-19 | **伪变异（等值改写）是这套框架的真正压力源**：`unit` 算子同时产出**两个方向相反**的样本 —— 篡改版 `110 万元 → 110 亿元`（数值不动、量级错）与等值版 `110 万元 = 1,100,000 元`（**金额不变，只换写法**）。检测器对前者沉默才是缺陷，对后者**开口才是缺陷**。四个算子带等值变体，共 24 个真负例进 FPR 分母。**这 24 个 TN 比 102 个 TP 更能说明问题**：只报召回率的对抗测试可以靠"什么都报错"刷满分。 |
| 2026-09-19 | **两个被测目标，读数位置不同**：`CheckerTarget` 跑 CARD-05 的 `check_report`（固定事实基座）；`GraphValidatorTarget` 跑 CARD-09 的 `build_graph` 后读**节点上的 `validation_status`**，**不是**报告对象上的 `.valid`（后者是整份报告的汇总判决，对"这一条 claim 有没有被拦下"没有分辨率）。检测 = **任一目标命中**。 |
| 2026-09-19 | **Validator 目标只把引用映射到 fact 的 `evidence_ids`，绝不映射 `derived_from`**：`derived_from` 是 claim→claim 的推论边，拿它当"支撑"会让**每一条推论看起来都被支持**，于是引用类篡改在图里恒判通过 —— 一个永远不报错的检测器。实测 32 个样本由 validator 目标（而非 checker）抓住，说明这条通路是活的，不是摆设。 |
| 2026-09-19 | **零 LLM 边界用源码断言守，不用约定守**：`tests/test_cn_finfuzz.py` 用 CARD-05 的 `check_source_is_model_free` 逐文件扫 `src/cn/finfuzz/*.py`。变异生成与两个检测器全确定性；报告**不携带任何墙钟字段**（另有测试断言 `to_dict()` 里没有 `detection_time`/`elapsed`）—— 带时间戳的"可复现"是假的。 |
| 2026-09-19 | **`prf1` 刻意不复用 CARD-11 的那一个**：CARD-11 的 `prf1` 在分母为零时**塌成 0.0**（报告体系里 0 比 `None` 好画图），本卡的同名函数返回 **`None`**。"某 error type 一个样本都没有"与"某 error type 全部漏检"是两件事，压成 0 会让 N/A 行看起来像 0 召回。两处各留各的语义，名字一样但契约写在各自文档串里。 |
| 2026-09-19 | **`mutate_version` 两次真修**：① `Disclosure.value` 是**基本单位（元）**而 `claim.value` 是**声明单位（万元）**，直接比较永不相等 ⇒ 算子对每一条 claim 都"无从下手"，静默降级。加 `_to_base()` 换算。② 修完单位后它挑中了 **RESTATED 的 1,000,000（当前正确值）**，生成的句子**本来就是对的**（判序来自 `version_rank` 比较）⇒ 改用表格级的 `index.is_superseded(item)` 定位**真正被替代的 `AS_REPORTED 900,000`**。**"变异"没变异出错误，会静默在报告里记成一次漏检** —— 这类错误只有真读生成的句子才发现，测试通过不代表样本有效。 |
| 2026-09-19 | **`unit` 等值变体一度制造 4 个假误报**：初版只改 `value_literal` 与 `unit`、没改 `value`，于是"等值改写"实际把金额改了 10⁴ 倍 —— 一个**名副其实的错误**，却因为标着 `expected_detection=false` 而被记成误报。改为同步按单位比例缩放 `value`（`rescaled = claim.value * (old_scale / new_scale)`）。教训：**伪变异必须构造性地保证语义不变，不能靠"我改的是写法"这句话保证。** |
| 2026-09-19 | **三处算子锚点修错**：① `mutate_citation` 初版改的是 `claim.page`，而 CARD-05 **从引用串里的 `#pN` 读页码** —— 改了个检测器不看的地方；改为经 `_cited_page` 改引用串本身。② `mutate_causal` 把连接词插在句末标点之后，CARD-05 的 `_CAUSAL_RE` 要求动词后**至少两个字符**，于是匹配不上、回落成抽取器漏检；改为**锚在指标标签上**。③ `mutate_sign` 起初不检查前置条件：增长率重算需要 `metric` + `period` 同时存在，缺了就"不可能被避免地漏检" —— 一个**无法被核查器避免的样本**披着盲点的外衣。加前置条件，不满足即拒绝并把 `no_applicable_variant` 记进 skipped。 |
| 2026-09-19 | **`extractor_vs_checker` 起初把真负例算成核查器失败**：分母用了 `len(results)`，把 24 个**期望沉默**的等值样本一起算进去 ⇒ "核查器失败 24 次"，全部是它做对了的事。改为**只数真变异（planted）的漏检**。这与卡 §13 第三条（伪变异计入 FPR、禁止剔除）不矛盾：它们进 FPR 分母，但**不进"失败"计数**。 |
| 2026-09-19 | **语料清洗从"整篇干净"改成"逐 claim 过滤"**：初版要求整份草稿无 finding 才采用，结果 `draft_07` 因为别处有瑕疵被整篇丢弃，它那行**本来干净的 `scope=合并` 声明**也跟着没了 ⇒ `scope` 算子永远拿不到样本。改为逐 claim 采用、被 CARD-05 反对的 claim 逐条记进 `excluded` 并写明原因。**粒度选错会让"没有样本"看起来像"算子有问题"。** |
| 2026-09-19 | **修 CARD-05 一个真 bug（跨卡）**：`check_source_is_model_free` 用**子串**匹配禁用符号，于是 `from src.cn.checker.rule_extractor import extract_claims_rules` 被判定成"引用了模型客户端（extract_claims）" —— 而 `extract_claims_rules` 正是**确定性规则抽取器**，是模型无关模块**本来就该**导入的东西。改为标识符边界匹配（`(?<!\w)…(?!\w)`），并加 `label` 参数（默认 `"checker.py"` 保持 CARD-05 行为不变），让 CARD-24/25/26 复用时消息指向自己的文件。CARD-05 §16 已同步记录。**这是复用别人的断言时才会撞见的潜伏缺陷。** |
| 2026-09-19 | **校验器 11 条**：卡 §13 点名的 3 条（spec 必须有 diff / 必须区分 extractor 与 checker / 伪变异必须计入 FPR 且禁止剔除），加上 8 条交叉不变式 —— rows↔results 一一对应、**overall 逐格现场重算**（P/R/F1/FPR/TP/FP/FN/TN 全部由 rows 重算比对，不信任报告里的数）、per-error-type **十行齐全且 N/A 行的 `n` 必须为 0**、归因自洽（`detected` 与 `failure_stage` 互斥、检测器名在册）、确定性卫生（无墙钟、`real_model` 未声明即 False、**未采集的 holdout 不得带数字**）、`mutation_id` 唯一、组合标签自洽、**skipped 每项必须带 reason**。**报自相矛盾是这类评测最容易出的错**（一个 per-error-type 行算错，整体召回看上去照样合理）。 |
| 2026-09-19 | **CARD-11 集成**：`TierPayload.adversarial_rows` → `adversarial_metrics()` 产出 `mutation_detection_{precision,recall,f1,false_positive_rate}` 四项，并把 **per-error-type recall 写进 recall 那一行的 note**（`numeric=16/16 …`）—— 光看平均召回会掩盖单个算子的全盲。**无 rows 时四行保留为 `None` + `note="no_adversarial_rows"`，不删行**；`tier_report` 里 `groups["adversarial"]` 单独成组。本卡**不新增指标定义**，24 项指标的所有权仍在 CARD-11（⚠️ 本行只覆盖**消费端**：装配环节当时仍是空的，见下文本轮复查那两行）。 |
| 2026-09-19 | **端到端实测（现行合成语料）**：`python -m src.cn.finfuzz` → specs=128（真变异 104 / 等值 24），**Precision 1.0000 / Recall 0.9808 / F1 0.9903 / FPR 0.0000**，TP 102 / FP 0 / FN 2 / TN 24；漏检 2 例**全部落在抽取器阶段**（`period` 文本通道，8/10 召回），核查器阶段漏检 **0**；逐算子召回：numeric/unit/scope/version/citation/causal 1.0，period 0.8，ytd_vs_single 与 valuation `N/A`。validator 目标独立贡献 32 次命中。 |
| 2026-09-19 | **卡要求加载的 `financial-agent-evaluation` 技能在本仓库不存在**（项目级只有 `fintrace-card-flow` / `fintrace-add-model-provider`）。未阻塞：出处与判据纪律改绑仓库自己的契约（CARD-05 的 check_type 词表与 `EXPECTED_CHECK_TYPES` 映射、CARD-09 的节点状态、CARD-11 的指标所有权与阈值冻结）。**如实记录，不假装加载过。** |
| 2026-09-19 | **回归**：`tests/test_cn_finfuzz.py` **70 passed**（collect 实测）；全量离线 **1441 passed / 23 failed / 2 skipped**（junit `errors=0`），失败集合 **14/6/2/1** 与 CARD-25 收尾时**逐条一致**，passed 增量 1371 → 1441 = **+70** = 本卡用例数；冒烟 14/14 clean；`git diff --check` 无输出。 |
| 2026-09-19 | **复查发现一个真缺口（上一轮把它推给了 CARD-11，站不住）**：`--set synthetic` 聚合走的是 `scripts/run_benchmark_v2.py` 的 `COMPONENT_BASELINES` 注册表，而那里**只有 card01 / card05 两张卡、没有 FinFuzz 的条目**，也**没有任何脚本产出 `finfuzz_baseline.json`**。也就是说：消费端（`TierPayload.adversarial_rows` → `adversarial_metrics`）与 `tier_payload_from_dict` 的读取路径都齐了，但**装配环节是空的** ⇒ synthetic 档永远只报 `no_adversarial_rows`。卡 §15 原文就是「`--set synthetic` 包含 finfuzz 报告」，这属于本卡范围，不是"属 CARD-11 的运行器"。已补齐，见下两行。 |
| 2026-09-19 | **补齐：新增 `src/cn/finfuzz/baseline.py`（组件基线生产者）**。规矩有二：① **同一指标只有一处定义** —— 四项数值由 `baseline_metrics()` 调用 **CARD-11 自己的 `adversarial_metrics()`** 得到后原样搬运，本模块**不做任何计算**（组件用自己的算术重新发布一个已有指标，正是 CARD-11 归并闸门要拦的事）；② **文件名是契约的一部分** —— `COMPONENT="card26_finfuzz"` / `BASELINE_FILENAME="finfuzz_baseline.json"` 由注册表 import 同一常量，改名不会静默把基线变成孤儿（有测试守）。基线里**刻意不放逐算子明细**：聚合器只认四个指标名，明细已在 recall 那一行的 `note` 里，两处都放就会出现两份可能互相矛盾的逐算子数字。边界声明 `DISCLAIMER` 随数字一起落盘。CLI 增 `--baseline-dir`（不给该开关就不写基线，聚合器报"未测量"而不是 0）。 |
| 2026-09-19 | **`case_count` 拒绝硬编码**：注册表第三列原本要填一个数，但 FinFuzz 的样本数随语料与 seed 变（现为 128），硬编码会与**紧挨着的基线文件**静默漂移成两个真相。故改为该列填 `None`，并新增 `component_case_count()`：**基线文件自报的 `case_count` 优先于注册表的兜底值**（card01/card05 的载荷里没有这个键，仍用各自的 10 / 3）。实测 tier `case_count = 141 = 10 + 3 + 128`，三处来源一次对上。 |
| 2026-09-19 | **新代码自己踩的坑（测试当场抓住）**：`format_markdown` 与 `operators_not_applicable` 起初都只判 `recall is None`，而序列化后的 N/A 行里 `recall` 是**字符串 `"N/A"`** —— 于是 `operators_not_applicable` **静默返回空集**，与它旁边那份列了两个 N/A 算子的报告直接打架。改为统一的 `_is_not_applicable()`（`None` 或哨兵字符串都算）。又一次印证：**写"报告的报告"最容易出的错就是与源报告自相矛盾**。 |
| 2026-09-19 | **端到端验证（卡 §15 原句）**：`python -m src.cn.finfuzz --out output/benchmark/finfuzz --baseline-dir output/benchmark/cn_agent_v2` → `python scripts/run_benchmark_v2.py --set synthetic --out output/benchmark/cn_agent_v2` ⇒ **synthetic 由 8 项已测变 12 项已测**（+4 = 四项对抗指标），`groups.adversarial` 四项齐全且 `source == "card26_finfuzz"`（不是 `computed`），`notes` 含 `component:card26_finfuzz`，逐算子召回存活于 recall 行的 note（`causal=16/16 citation=36/36 … period=8/10`）。**数值与 FinFuzz 自己的报告逐格一致**（复用同一 calculator 的直接结果）。 |
| 2026-09-19 | **回归（补齐基线之后）**：`tests/test_cn_finfuzz.py` **74 passed**（+4 = 基线四例）；全量离线 **1445 passed / 23 failed / 2 skipped**（junit `errors=0`），失败集合 **14/6/2/1** 与上一轮逐条一致，passed 增量 1441 → 1445 = **+4**；冒烟 14/14 clean；`git diff --check` 无输出。 |
| 2026-09-19 | **未闭合**：① **holdout 未采集** ⇒ 报告恒为 `TARGET / TO BE MEASURED`，且携带数字会被校验器直接判错（§12.4 过拟合闸门已装好，但没有材料可跑）；② §16 的 ablation（`regular_tests_only` vs `regular_plus_finfuzz`，指标 `per_error_type_recall`）未运行 —— **已核实现状**：对照臂注册在 `ABLATION_COMPARISONS` 里，报告渲染为 `TARGET / TO BE MEASURED` 且两值均 `null`（五条创新全部如此）。缺的是**两处定义**而非运行器：基线臂"普通测试套件按 error type 的覆盖率"没有现成口径，且 `per_error_type_recall` 取什么统计量（最差算子 / 均值）未定，二者都属 CARD-11 的指标所有权；本卡侧只缺一个把治疗臂摘要喂进 `ablation_runs` 的调用；③ §17 的 demo 交互（注入 → 展示错误类型/原值/正确值/证据）归 CARD-15；④ Real 集未采集，所有数字均来自合成语料，**不得对外表述为真实研报上的检测率**；⑤ 汇率/股份拆分等新增算子与自适应变异属决赛范围（卡 §20），未做；⑥ 卡 §15 写的命令是 `--suite cn_agent_v2`，而运行器实际用的是 `--set`（`--set synthetic`）—— **卡文与实现的名词不一致**，按实现为准，已在 `contracts/CARD-26.md` 标注；⑦ 「全部测试离线通过」待 Tushare 快照重建。 |
| 2026-09-20 | **§17 demo 交互已落地（由 CARD-15 施工，本卡记账）**：新增「对抗注入」页（`workbench/demo/adversarial.py`），两个标签页。① **失败注入 F1–F7**：CARD-12 的七场景逐个**注入真实故障并断言系统自己的反应**（不是断言注入本身生效），`ok=False` 的场景画红条且不从列表消失，期望行为逐条印在页面上；② **FinFuzz 读数**：按错误类型分层报召回，并区分抽取器/核查器归因（`extractor_vs_checker`）。两条本卡形态在页面上被显式守住：空分母的召回是**字符串 `"N/A"`**（不是 0.0），行保留不删 —— 现有语料上 `ytd_vs_single` / `valuation` 正是 N/A，页面直接列出并警告"删掉空行等于假装这类错误不存在"；被剔除的 claim 条数与剔除理由也一并露出（否则"召回很漂亮"可能只是语料变小了）。另注：`"N/A"` 是字符串而非数值，直接塞进 dataframe 数值列会让整张表渲染失败（表消失比显示 N/A 更糟），页面先格式化成字符串。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§18「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-09-27 | **§16 ablation 的两个定义缺口复核确认（结论：缺定义，不缺运行器）**。① 基线臂「普通测试套件**按 error type** 的覆盖率」**没有现成口径**（手写测试不按 error type 组织，得先定义"哪些 error type 被既有测试显式断言过"）；② `per_error_type_recall` 取**哪个统计量**（最差算子 / 均值 / 加权）未定 —— 取最差会得到很低的数、取均值会好看，**先定统计量再算数**。二者都属 CARD-11 的指标所有权。另**明确区分**：本卡已有的 `P 1.0000 / R 0.9808 / F1 0.9903 / FPR 0.0000` 是**组件基线**（走 `scripts/run_benchmark_v2.py::COMPONENT_BASELINES`），**不是** §7b 的 `regular_tests_only vs regular_plus_finfuzz` 对照，两者不得互相顶替。详见 CARD-11 §7b-2。 |
