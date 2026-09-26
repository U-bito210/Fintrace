# CARD-05: Report Claim Extractor & Deterministic Checker（研报纠错系统）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（9/30–10/6） |
| 对应赛题 | 赛题5 第二核心方向 |
| 依赖 | CARD-02（规范化事实）、CARD-04（口径信号） |
| 实现复杂度 | 高（约 6–7 人日） |
| 状态 | 🟡 代码 + 135 测试就绪（全量离线 691 passed / 23 failed，失败集合与 CARD-04 时逐条一致；冒烟 14/14 clean）；§14 工作台纠错列表已随 CARD-15 落地（2026-09-20）；§13 四项指标仍只在合成 planted 集上产出 |
| Skill 要求 | 动手前加载 financial-agent-evaluation、financial-data-provenance |

## 1. 目标

新建 `src/cn/checker/`：从**自然语言研报草稿**中提取结构化 Claim（LLM 仅做搬运），再用**确定性规则引擎**对照事实核查，输出 13 类错误的完整纠错报告。真实比赛不会要求用户手输 DraftClaim JSON——必须支持 Raw Report 直接输入。

## 2. 对应竞赛评分点

错误识别准确性与覆盖率、误报控制、来源定位、修改建议可执行性（赛题5 全部考察点）。

## 3. 为什么需要这个模块

v2 的 checker 只接受预结构化 claims，是演示玩具。升级后这是第二核心赛题的完整答案，且是"LLM 提取 + 确定性判定"边界的最佳展示。

## 4. 输入 / 输出

- **输入**：研报草稿（PDF/TXT/MD，经 CARD-01 ReportDraftParser 解析为带页码段落）
- **输出**：`DraftClaim[]`（中间产物）→ `Finding[]` 纠错报告 + 汇总统计

## 5. 数据模型

```python
@dataclass
class DraftClaim:
    claim_id: str
    sentence: str           # 原报告句子（逐字保留，定位展示）
    page: int | None
    metric: str | None      # revenue / net_profit / pe / growth / ...
    period: str | None
    value: float | str | None
    unit: str | None
    growth: float | None
    valuation_multiple: str | None
    source_reference: str | None   # 草稿声称的引用
    claim_type: str          # factual | valuation | causal | opinion

@dataclass
class Finding:
    claim_id: str
    check_type: str          # 见第 7 节 13 类
    severity: str            # error | warning
    original: str            # 原始值
    expected: str            # 正确值（含单位/期间）
    evidence_ids: list[str]  # 来源 Evidence
    suggestion: str          # 可执行修改建议
    confidence: float        # 确定性规则给出（见 7.3），非 LLM 自评
```

**DraftClaim.claim_type → Claim.claim_type 映射**（v3.0 原方案两套枚举不一致，现已对齐）：

| DraftClaim.claim_type | Claim.claim_type | 附加约束 |
|---|---|---|
| factual | fact | 必须绑定数值类 Evidence |
| valuation | inference | 必须有 assumption_id（目标倍数登记）或行业 Gate 判定 |
| causal | inference | 双侧必须有独立 Evidence 支撑（否则 unsupported_causal_claim） |
| opinion | opinion | 必须显式标注假设/主观判断来源 |

LLM 不得直接决定映射——映射在 DraftClaim 通过 schema 校验后由确定性代码执行（checker.py 内完成）。

## 6. API / Tool Contract

```python
# claim_extractor.py（LLM，唯一允许的 LLM 环节）
def extract_claims(draft_paragraphs, llm_client) -> list[DraftClaim]:
    """LLM 提取 metric/period/value/unit/growth/valuation_multiple/
    source_reference/claim_type；输出过 schema 校验；
    每条必须携带原句 sentence（逐字），不携带原句的提取结果丢弃。"""

# checker.py（确定性规则引擎，零 LLM）
def check_report(claims, normalized_facts, scope_signals) -> list[Finding]: ...
def summarize(findings) -> dict: ...
```

Agent 工具 `CheckCnReportDraftTool`：入参 `document_id`（草稿）；出参 `status / findings / summary`。

示例（指令原文）：输入"公司2025年实现营业收入32.6亿元，同比增长18.7%。"→ 提取 `{metric: revenue, period: FY2025, value: 32.6, unit: 亿元, growth: 18.7%}` → 确定性核对快照/文档事实。

## 7. 金融逻辑

### 7.1 13 类错误

`numeric_error`（数值不符）、`unit_error`（单位错）、`period_error`（期间错配）、`scope_error`（口径错配——依赖 CARD-04 信号）、`calculation_error`（同比/环比复算不一致，复算走 CARD-03 同源函数）、`valuation_multiple_error`（**限界：仅检"引用的可比倍数与事实不符"和"行业错配"；纯分析师目标倍数跳过并计数为 opinion——把观点当事实判错是误报源头**）、`missing_citation`（无引用）、`wrong_citation`（引用错对象）、`unsupported_citation`（引用不存在）、`wrong_page`（页码错）、`stale_citation`（引用过期数据）、`revision_superseded`（引用了被更正/重述取代的旧值）、`unsupported_causal_claim`（无证据因果断言）。

### 7.2 单位换算纪律（承袭 v2 正确设计）

比对前按草稿声明单位换算到事实单位；数字量级恰好差 10^4/10^8 且单位声明不同 → 报 unit_error 而非 numeric_error（误报控制关键）。

### 7.3 confidence 语义

确定性规则给出：精确证据匹配=1.0；期间推断匹配=0.8；需单位换算后匹配=0.9 等。规则表集中常量。**不存在 LLM 自评置信度**。

### 7.4 causal 检查

claim_type=causal 的断言（"因 A 导致 B"）→ 检查因果两侧是否均有独立证据支撑；缺任一侧 → unsupported_causal_claim（warning 级，允许观点存在但必须标注）。

## 8. Edge Cases

1. LLM 提取失败/格式非法 → 该句标记 unextracted，计入 summary（不阻塞其余句子）
2. 草稿数字四舍五入显示（32.6 vs 32.58）→ 容差仅限显示位数舍入（亿级 1 位小数），禁止放宽
3. 草稿引用的指标系统事实库没有 → missing_citation 变体（标注"无法核验"而非"错误"）
4. 同一指标多个披露版本 → revision_superseded 优先于 numeric_error
5. 研报含预测值（"预计2026年营收…"）→ claim_type=forward Looking，跳过事实核查，单独计数

## 9. Failure Mode

提取环节失败 → 明确报告"X 句未能提取"而非装作没有；核查环节无对应事实 → "无法核验"状态，与"核实为错"严格区分。**误报比漏报更伤可信度**——规则倾向保守。

## 10. Validator Rules

- finding 的 evidence_ids 必须可解析（文档型带页码）
- numeric_error 的 original/expected 必须同单位口径展示
- 任何 finding 不得由 LLM 判定产生（代码审查断言：checker.py 无 LLM import）

## 11. Unit Tests

`tests/test_cn_checker.py` + `tests/fixtures/report_drafts/`（含 planted errors 的草稿集）：
- 全部 13 类错误各有 ≥1 用例
- 好草稿零误报（命中率 100% / 误报 0 的双指标测试）
- 单位量级错配判 unit_error 不判 numeric_error
- 舍入容差边界测试
- causal 单侧证据 → warning

## 12. Integration Tests

CARD-01 草稿解析 → 提取 → 核查 → finding 跳转原 PDF 页全链路；与 CARD-11 planted 集联合计算 Checker Precision/Recall/F1/FPR。

## 13. Benchmark

Checker Precision / Recall / F1 / False Positive Rate 四指标接入 CARD-11（合成 planted 集 + 真实研草稿改造集），目标值待 baseline 后冻结。

## 14. Demo Method

上传含错研报草稿 → 纠错列表（错误类型徽章 + 原句 + 正确值 + 建议）→ 点击任一 finding 跳转原始 PDF/事实来源页。**这是 5 分钟 Demo 的核心一幕**。

## 15. 验收标准

**两级验收，避免 LLM 波动卡死卡片关闭**：

**A. 核查器级（给定正确 DraftClaim 输入——纯确定性，可 100% 保证）**：
- [x] planted 集 13 类错误检出 100%（给定正确 DraftClaim）——7 份草稿 14 条期望全部命中，0 漏报；另有 `test_all_thirteen_are_reachable` 守"枚举有、规则无"
- [x] 好输入零误报（纯确定性，无 LLM 参与）——`Precision = 1.0000`、`False Positive Rate = 0.0000`（14 TP / 0 FP / 0 FN / 8 TN）
- [x] checker.py 零 LLM import（代码结构断言）——`src/validation/checker_validator.py` 扫源码文本，含"函数体内延迟 import"用例

**B. 端到端级（含 LLM Claim Extractor——自然语言草稿直接输入）**：
- [x] 自然语言输入端到端跑通（非预结构化 JSON）——PDF → CARD-01 事实 → 句切分 → 提取 → 核查 → finding 可回跳来源页与草稿页；生产走 `extract_claims`（LLM），离线走 `rule_extractor`（§偏差 2）
- [x] 指标只入 CARD-11 benchmark 报告（不作本卡验收门槛）：Checker Precision / Recall / F1 / FPR——已在合成 planted 集产出（1.0000 / 1.0000 / 1.0000 / 0.0000），真实研草稿改造集待接
- [x] DraftClaim.claim_type → Claim.claim_type 映射全路径验证通过——5 个取值逐一测试 + 未知值回退 `opinion`（绝不回退 `fact`）
- [x] 全部测试离线通过——**本卡相关测试 135 项全绿**，全量离线为 691 passed / 23 failed；23 项失败全部来自缺失的本地 `data/snapshots/cn/` tushare 快照（与 CARD-01/02/03/04 收尾时**逐条一致**），待快照重建 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-18 | **开工前可行性核查**：CARD-02 / CARD-04 均已就绪，本卡可开工。但实测发现 §4 的输入假设与实现不符，全部按下述偏差处理，不静默改规格。 |
| 2026-09-18 | **偏差 1（§4 输入粒度）**：§4 假设草稿经 `ReportDraftParser` 解析为「带页码**段落**」。实测 `ReportDraftParser` 按空行切段，而 reportlab 渲染的草稿没有空行，`pdfplumber.extract_text()` 返回的是**每页一个** text 事实。句子粒度因此由检查器在 sentence 层恢复（`src/cn/checker/sentences.py`），不改 CARD-01。**连带后果已记录**：草稿标题无终止标点，会被"未终止行与下一行合并"的换行规则吸收进首句（实测首条 claim 的原句是「…点评（草稿）公司当前市盈率为50倍。」）。无法分离标题——`extract_text()` 每个视觉行只出一个 `\n`，`Spacer` 的垂直间距被完全折叠（实测 8/18/28/40/60pt 输出逐字相同），而"某页首行""不含数字的行"都会误伤真正被换行的句子。选择保留并文档化：影响仅限原句展示多一段标题，且标题词同样被核查、不被豁免。 |
| 2026-09-18 | **偏差 2（§6 只有一个提取器 → 两个）**：§6 把 `extract_claims` 定为唯一允许 LLM 的环节，生产路径保持如此。但 §15B 要求"自然语言输入端到端跑通"、§11/§13 要求离线打分，只有 LLM 提取器时两者都无法离线完成。故新增 `rule_extractor.py`（正则，`extractor="rules"`）作为**兄弟实现**：它只产出 `DraftClaim`，从不读事实库、从不判定对错、并自我声明。§10「checker.py 零 LLM import」不受影响——两个提取器平级，只有一个连模型。 |
| 2026-09-18 | **偏差 3（§5 数据模型补 `scope` 字段）**：§5 的 `DraftClaim` 没有口径字段，而 §7.1 要求 `scope_error`。没有它，该规则只能靠 CARD-04 信号触发，一句"母公司营业收入为60万元"（引用的是合并数）会静默通过——这是 A 股最典型的口径错误。已加 `scope`，且**只从显式措辞填充**，两层窄口径：① 被限定为口径的词（「母公司报表」「合并口径」）；② 紧贴指标名之前的词（「母公司营业收入」）。因此「归属于母公司股东的净利润」判为 `None`——那里的"母公司"是指标名的一部分，读成口径就会凭空造出 scope_error。CARD-02 的 `Scope.coerce` 负责归一，模型不能自创口径。 |
| 2026-09-18 | **偏差 4（§7.4 causal 的双侧来源）**：§5 每条 claim 只有一个 `source_reference`，装不下"因果两侧各自取证"。已加 `source_references`（首个镜像回 `source_reference`，§5 字段语义不变）与 `causal_antecedent` / `causal_consequent`。独立性按**引用解析结果**判定：两个不同文档，或同一文档的两页；同一页引用两次仍算一个来源。两侧无法分离也报 warning——claim 无法被取证。 |
| 2026-09-18 | **偏差 5（§7.1 倍数规则的限界）**：只复算可验证倍数（PE = 价 × 股 ÷ 归母净利，分母口径与期间从事实库取），分析师目标倍数一律按 opinion 跳过**并计数**（`claims_skipped_opinion`）。§11 要的科目必须有输入才可测，而任何已入库年报都不含股价，故合成市价文档 `SYN-MARKET-01` 作为 `extra_facts` / `extra_documents` 注入，并在 `coverage.injected_facts` 明确标注 `synthetic: true`，不与解析出的年报事实混为一谈。 |
| 2026-09-18 | **偏差 6（§7.1 期间推断，唯一一处推断，已写明）**：CARD-02 的 `canonical_period` **刻意拒绝**裸年份（「2025年」可能是 FY/H1/Q4，不猜）——这条拒绝是对的，不得放宽。但草稿里「公司2025年实现营业收入…」读的就是年度，若留空则 `period=None`，检查器会接受任意期间下的该数值，**每一个期间错配用例都会漏报**。故在草稿层做一次、且只做最窄的一条：**标签必须恰好是四位年份 + 「年」**，才推为 `YYYYFY`，并把推断写进 `claim.reason`（不静默）。其余仍走 CARD-02 解析器，拒绝的一律留空。两个提取器共用 `resolve_period_label()`，两条路径不会对"期间是什么意思"产生分歧。 |
| 2026-09-18 | **开工中修掉的既有缺陷 1（误报，最重要）**：`FactIndex.match` 把 `period_key=None` 当成"期间是 None"，于是「营业成本为70万元」这种**未声明期间**的句子，在按值命中 2025FY 披露后仍被报 `period_error`——正是 §9 最忌讳的"建立在检查器自己假设之上的错误"。已改为 `period_key=None` 表示**不作约束**（与 `candidates` 的既有语义一致）：未声明期间就不构成期间错配。同时把 `period` 分支加上 `period_key is not None` 守卫。实测该缺陷让两份**好草稿**各产生 1–2 条 period_error 误报。 |
| 2026-09-18 | **开工中修掉的既有缺陷 2（引用错配）**：reportlab 把「（来源：…）」换到独立视觉行，句号切分后引用落到**下一句**。实测 5 条 claim 的引用全体错位、干净草稿被判 `missing_citation`。已在 `sentences.py` 加 `_pull_citations_back()`：把"跟在句号后、独立成行"的前导引用挂回上一句，只搬引用组、其余文字留在原位。 |
| 2026-09-18 | **开工中修掉的既有缺陷 3（replace 粒度）**：`annual_04` 的 `revenue 2025FY` 在第 3 页「前期差错更正影响金额」表的调整前/调整后列与合并利润表本期列被归成同一组，导致**未被取代**的合并利润表数值被判 `revision_superseded`。版本列是**表级**结构：取代关系由同一张表并排印出的调整前/调整后对确立。已把 `_group_of()` 加上 `document_id` + `source_table` 维度；跨表/跨文档的同值属重复或冲突，冲突归 CARD-13，不该由一条必须猜"谁赢"的规则处理。 |
| 2026-09-18 | **开工中修掉的既有缺陷 4（§6 逐字原句未被真正强制）**：`_match_sentence` 只要 `sentence_id` 命中就直接采信，从不核对模型抄回的 `sentence` 是否逐字。于是"报对了 id 但引了另一句话"的结果被接受，claim 带着**草稿里根本没有的引文**流出——而 `DraftClaim.sentence` 正是报告展示给审阅者的"原句"，等于造了一条伪引文。已改为：id 只是找句子的线索，引文必须与真实句子一致（仅容忍空白差异与截断前缀）；id 命中但没带句子、或句子对不上，一律按 §6 丢弃并计入 `unextracted`。 |
| 2026-09-18 | **开工中修掉的既有缺陷 5（提示词与解析器口径不一致）**：`SYSTEM_PROMPT` 要求模型按原文返回期间（例：「2025年」），而 `canonical_period` 拒绝裸年份，结果是模型**照做反而拿不到 period**、并在 `reason` 里记一条"不支持的写法"。已在 §偏差 6 的统一策略下解决。 |
| 2026-09-18 | **§10 Validator 的三条规则与两处判定收窄**：① 证据可解析——`validate_checker_report` 要求每条 `evidence_id` 解析到**文档 + 页码**；② 两侧同口径——`original`/`expected` 都必须写出单位，error 级必须能说出正确值；③ 源码级断言——扫 `checker.py` **源文本**而非 import 图，因为函数体内的延迟 `import` 对 `sys.modules` 检查是隐形的，而其破坏力与顶层 import 完全相同。判定收窄两处，都有实测依据：`wrong_citation` **移出**"必须有证据"清单（`draft_04` 实测被引公告不产出任何规范化事实，该 finding 的内容**就是**"这里没有披露"，硬要证据等于逼检查器编一条）；`scope_error` 若 `original` 是百分比则**不适用**"两侧同口径"（同一个 check_type 承载两件事：「数值属于另一口径」与「CARD-04 阻断后仍出百分比」，只有前者是金额比较，后者没有"正确金额"可给，强判会误伤一条正确 finding）。 |
| 2026-09-18 | **§10 Validator 抓出的真实缺陷（证据 id 空间不一致）**：`draft_05` 的 `scope_error`（CARD-04 `block_yoy`）转发的 `evidence_ids` 是 CARD-01 的 `fact_…`，而报告起初只发布 CARD-02 的 `nf_…` 索引，校验器**解析不到**——一条合法 finding 看起来无来源。已在 pipeline 增加 `_build_evidence_index()`，同时收录两层事实（`layer` 字段标明 `extracted` / `normalized`），报告因此**自校验**；这也正是 §14「点 finding 跳到来源页」需要的索引。 |
| 2026-09-18 | **§13 基准（合成 planted 集）**：`Precision / Recall / F1 = 1.0000`，`False Positive Rate = 0.0000`，`14 TP / 0 FP / 0 FN / 8 TN`，7 份草稿 22 条 claim。期望值全部由人工写在 `src/cn/checker/draft_corpus.py`，按 `(check_type, claim_id)` 配对，**不是**拿检查器自己的输出当真值。报告本身声明 `synthetic: true` / `real_model: false` / `extractor: "rules"`，并写明"不是真实研报现场准确率"。`scripts/gen_report_draft_fixtures.py --report` 可复现，`--check` 校验 sha256。 |
| 2026-09-18 | **游标（开工前后各一次，用于定位回归）**：CARD-04 收尾时全量离线为 532 passed / 23 failed；本次开工前实测为 **556 passed / 23 failed**（失败集合与分布完全一致：`test_cn_workbench_service` 14、`test_workbench_api` 6、`test_cn_report` 2、`test_cn_report_cli` 1，全部源于缺失的本地 tushare 快照）。收尾后 **691 passed / 23 failed**，增量 135 恰为本卡新增测试数，失败集合逐条未变。注：`556` 与 MASTER_PLAN 上记的 `532` 有差，差在 CARD-04 §16 的"补齐两项"落地，不属本卡改动。 |
| 2026-09-18 | **测试**：`tests/test_cn_checker.py` **135 项全绿**。覆盖 §11 全部要求（13 类各 ≥1 例 + `test_all_thirteen_are_reachable` 守"新枚举无规则可发"；好草稿零误报；单位量级判 `unit_error` 不判 `numeric_error`，含 千元/万元/亿元 三档；舍入容差边界含"尾随零不算有效位"；causal 单侧 → warning）与 §12 集成链（PDF 解析 → 提取 → 核查 → finding 证据回到来源页 + 草稿页码；工具三态；`acheck_draft` 与 `check_draft` 共用同一确定性核心）。另有两条防回归不变式：`checker.py` 零 LLM import（源码级）、模型不得自创口径。 |
| 2026-09-18 | **未完成**：① §14 的**工作台**纠错列表 UI（三态徽章 + 原句 + 正确值 + 建议 + 跳转）未做——服务层数据已齐（`_editorial_view` 的分组结构由工具与 UI 共用，工具侧已实现并测试），但 `workbench.py` 的页面与 `scripts/smoke_workbench.py` 的 `PAGES` 尚未新增；② §13 的四项指标只在合成 planted 集上产出，**真实研草稿改造集**未接入 CARD-11；③ 13 类中 `wrong_page` 只在单一文档内判定，跨文档"该指标其实在另一份材料里"仍归 CARD-13；④ `data_complete` 语义下 `blocked`/`partial` 的边界只由"有无 claim"与"有无未提取句"决定，"事实库为空"这一情形未单列；⑤ 演示库中 `blank` 类草稿（无任何数字）只能由测试构造。 |
| 2026-09-19 | **跨卡改动（CARD-26 发现，本卡 §10 源码断言的真 bug）**：`check_source_is_model_free` 用**子串**匹配 `BANNED_SYMBOLS`，于是 `from src.cn.checker.rule_extractor import extract_claims_rules` 被判成"引用了模型客户端（`extract_claims`）"——而 `extract_claims_rules` **正是本卡偏差 2 新增的确定性规则抽取器**，是模型无关模块**本来就该**导入的东西。改为**标识符边界**匹配（`(?<!\w)…(?!\w)`，编译成 `_BANNED_SYMBOL_PATTERNS`）；`BANNED_MODULE_FRAGMENTS` 仍是子串匹配（模块路径按片段命名是刻意的：`openai` 要能盖住 `openai_compatible`）。另新增 `label` 参数（默认 `"checker.py"`，**本卡行为与消息逐字不变**），供 CARD-24/25/26 复用时消息指向自己的文件。**本卡的测试永远发现不了这个误报**（本卡源码里没有 `extract_claims_rules` 这个名字），只有新的调用方撞得到——CARD-26 用它扫 `src/cn/finfuzz/*.py` 时当场报错。CARD-05 侧无行为变化，`tests/test_cn_checker.py` 135 项不受影响。 |
| 2026-09-20 | **§14 工作台 UI 已落地（由 CARD-15 施工，本卡记账）**：新增「研报纠错」页（`workbench/demo/corrections.py`），走 `src/cn/demo_service.corrections()` 只读服务层。**页面不做"检出几个错"的漂亮计数**，而是把语料规格里的**植入清单与实际检出逐条对齐**：0 条命中即"漏检"，用红条显形（不是从表里过滤掉）。另两项同属本卡的零静默口径：② 覆盖范围表逐条列出哪些规则**真的执行了**（`stale_citation` 未设 `as_of` 时如实标"未执行"——一条从未执行的规则给出的"干净"不是证据）；③ 抽取器没变成 claim 的句子单列（既没证实也没证伪）。跳转复用 CARD-01 的定位链路，引用类 finding 没有可解析数值证据时跳回**草稿原文页**。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-09-26 | **§7.1 限界的后半句未实现（真实材料彩排发现）**：§7.1 对 `valuation_multiple_error` 的限界原文是「仅检『引用的可比倍数与事实不符』**和『行业错配』**」，但 `checker.py::_multiple_findings` **只实现了前半句** —— 它只判可由「价 × 股本 ÷ 归母净利」复算的倍数（docstring 也这么写），对「把银行业的可比倍数套到电力设备公司」这类**行业错配没有任何判定**：`src/cn/checker/` 全目录检索 `industry`/`行业` 无相关逻辑，checker 上下文里也没有目标公司及其申万一级行业。后果：演示含错草稿的 E4 无法自动检出，步骤 6+7 卡在 **7/8**。**既没有下调 §5「8/8 检出方可上台」判据，也没有把它改判成"人工审查项"** —— 那两者都会与 §7.1 明文冲突。补齐需要新增数据通路：草稿 → 目标公司代码 → `src/cn/industries.py` 的申万一级行业名，再与该句声明的倍数来源行业比对；**草稿未声明来源行业时一律不判**（零猜测，避免"把观点当事实判错"这个 §7.1 点名的误报源头）。这是**未实现的设计**，不是可以绕过的边界。 |
