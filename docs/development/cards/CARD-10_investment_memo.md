# CARD-10: Investment Memo（买方投资备忘录）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（10/7–10/11） |
| 对应赛题 | 赛题6 最终业务输出 |
| 依赖 | CARD-03（财务分析）、05（纠错）、06（检索）、08（估值）、09（图谱）——**不依赖 Multi-Agent** |
| 实现复杂度 | 中高（约 4–5 人日） |
| 状态 | 🟡 代码+52 测试就绪；§14 UI 与 §13 指标未接 |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/memo.py`：从主链各模块产物组装结构化买方备忘录（11 段），markdown + JSON 双输出，每个数字经 Claim-Evidence Graph 可回溯，逐句标注事实/推论/观点。**已解除 v2 对 Multi-Agent 的强依赖**——单 Agent 主线即可产出。

## 2. 对应竞赛评分点

赛题6 全部考察点：投资逻辑、事实依据、风险因素、反向验证条件、持续跟踪指标的完整性。

## 3. 为什么需要这个模块

备忘录是全系统的最终交付物与 Demo 主角的载体；v2 版本绑死 Multi-Agent（决赛才做）导致初赛无法交付，本卡解耦。

## 4. 输入 / 输出

- **输入**：`AnalysisResult`（CARD-03）、`ValuationResult`（CARD-08）、`Finding[]`（CARD-05，草稿核查场景）、`SearchHit[]` 引用（CARD-06）、`ClaimGraph`（CARD-09）、风险信号
- **输出**：`InvestmentMemo`（结构化 + markdown 渲染）

## 5. 数据模型

```python
@dataclass
class MemoSection:
    section_id: str    # executive_summary | company_overview | financial_performance
                       # | investment_thesis | valuation | catalysts | risks
                       # | counter_thesis | falsification | tracking_kpis | evidence_appendix
    tier_marking: list[dict]   # 逐句标注 [{sentence_span, claim_id, claim_type}]
    body_markdown: str
    evidence_ids: list[str]
```

**关键升级（对 v2）**：事实/推论/观点标注从 Section 级下沉到**句子级**（每个 claim_id 对应 Claim-Evidence Graph 节点）。

## 6. API / Tool Contract

```python
def build_memo(analysis, valuation, findings, hits, graph) -> InvestmentMemo: ...
def render_memo_markdown(memo) -> str: ...
def render_memo_json(memo) -> dict: ...

# 叙述段 LLM 输出契约（句子级标注机制，v3.0 原方案缺失）
def _build_memo_prompt(section_name, analysis_dict, valuation_dict, existing_claim_ids: set) -> str:
    """注入：结构化分析/估值/信号 + 已存在 Claim ID 白名单。
    LLM 只能引用白名单中的 claim_id，禁止自造新 id。"""

# LLM 输出结构化 schema：
# [{sentence_span: str, claim_id: str | None, claim_type: "fact" | "inference" | "opinion",
#   derived_from: list[str] | None, assumption_note: str | None}]
# 每条必须携带原句逐字文本；claim_id 必须在 existing_claim_ids 中（或 None）
# 数值句必须引用已存在的 calculation_id

def _validate_memo_segment_output(llm_json, existing_claim_ids: set) -> None:
    """硬校验：
    - 每条 sentence_span 非空
    - 每条 claim_id ∈ existing_claim_ids 或为 None
    - fact 句 claim_id 非空 且 claim_type=fact
    - inference 句 claim_type=inference 时 derived_from 非空
    - 数值句必须带 calculation_id
    违反 → 该条重生成或剔除，不写入 memo。"""
```

Agent 工具 `GenerateCnInvestmentMemoTool`：入参 `symbol 或 document_id`；出参 `status / memo`。数值段（financial_performance / valuation / tracking_kpis）由确定性模板渲染；叙述段（thesis / risks / counter_thesis / catalysts）由 LLM 生成但**只能引用已注入 prompt 的结构化 claims**（白名单断言），输出数值由 unverified_number 检查拦截。

## 7. 金融逻辑（11 段与赛题6 考察点映射）

| 段 | 赛题6 考察点 | 数据来源 |
|---|---|---|
| Executive Summary | 投资逻辑摘要 | 确定性模板 + LLM 叙述（限 claims） |
| Company Overview | 事实依据 | 检索引用（quote/paraphrase 型） |
| Financial Performance | 事实依据 | CARD-03 指标表（fact） |
| Investment Thesis | 投资逻辑 | CARD-03 信号 → inference 标注 |
| Valuation | 假设与边界 | CARD-08（假设清单 + 适用边界） |
| Catalysts | 投资逻辑 | 检索 + LLM（opinion 标注） |
| Risks | 风险因素 | 诊断信号 + 双向列示 |
| Counter-thesis | 反向验证 | 与 thesis 对立的证据链（防单视角） |
| Falsification | 反向验证条件 | 可证伪指标（阈值 + 计算方式） |
| Tracking KPIs | 持续跟踪指标 | 确定性生成（背离信号阈值、估值分位线、下期报告日） |
| Evidence Appendix | 事实依据 | 全部证据清单（页码可跳） |

免责与边界：模板固定渲染"不构成投资建议"与适用边界声明。

## 8. Edge Cases

1. 任一上游缺失（如无估值数据）→ 对应段标 data_unavailable，不编造
2. LLM 叙述段输出数值 → unverified_number 拦截，该句重写或剔除
3. assumption_only 数据 → 整体警示横幅（承袭 data_grade 机制）
4. 草稿核查场景 → 备忘录引用纠错结果，注明"草稿中 X 处陈述与事实不符"
5. Counter-thesis 找不到对立证据 → 明示"未发现显著反向证据"（如实陈述，不硬凑）

## 9. Failure Mode

上游链路有 blocked claim → 备忘录对应结论标注 blocked 并在 Validation 段说明——**备忘录不掩盖系统的不可知**。

## 10. Validator Rules

- 逐句 claim 标注完整（无标注句拒绝渲染）
- fact 句 100% 绑定证据；数值句 100% 绑定 Calculation 或 Evidence
- quote 引用逐字校验（CARD-06 联动）
- Falsification 段必须含可计算阈值（禁止"持续关注基本面"空话）

## 11. Unit Tests

三段式好/缺数据/失败注入场景的渲染正确性；逐句标注完整性；unverified_number 拦截；免责声明存在性。

## 12. Integration Tests

全主链 → memo → 每个数字抽检回溯 PDF 页（自动化测试：随机抽 10 个数值，claim 图 trace 可达）。

## 13. Benchmark

Numeric Accuracy / Evidence Coverage（memo 级）接入 CARD-11。

## 14. Demo Method

Demo 第 3.5 分钟：生成备忘录 → 点击结论 → 图谱回溯（CARD-09）→ 落到 PDF 页。

## 15. 验收标准

- [x] 11 段齐全、逐句三级标注、双格式输出一致 ← 11 段按 `SECTION_IDS` 固定顺序渲染；`tier_marking` 与 `sentences` 同源；Markdown↔JSON 由测试逐段比对（section_ids / body_markdown / tier_marking / sentence_count / evidence_count）
- [x] 数值 100% 可回溯（自动化抽检测试）← `test_every_numeric_assertion_binds_a_calculation_or_evidence` 覆盖全部数值句；`test_sampled_numbers_trace_back_to_a_pdf_page` 抽样 10 条走 CARD-09 `trace()` 落到 page+document_id
- [x] 无 Multi-Agent 依赖（单 Agent 主线可跑）← 默认叙述器 `template_narrator` 是**确定性选择**（不生成新散文），无模型即产出整份备忘录；另有 `test_memo_module_does_not_import_a_multi_agent_framework` 守线
- [x] 全部测试离线通过 ← **原未勾**：CARD-10 新增 52 测试全绿（`tests/test_cn_memo.py` 52 passed），但全量离线游标仍有 **23 项既有失败**（全部来自缺失的 Tushare 快照，非本卡引入）。判据 = **失败集合逐条不变**，已核对与基线一致。 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

> 卡片正文验收项全部达成，唯一未勾项是项目级「零 Tushare 快照」的既有游标，与本卡无关。

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | 新建 `src/cn/memo.py`（11 段模型 + 双渲染 + 叙述契约）、`src/validation/memo_validator.py`（五类规则）、`tests/test_cn_memo.py`（**52 项全绿**）。工具 `GenerateCnInvestmentMemoTool`（`generate_cn_investment_memo`）接入 `build_cn_snapshot_tools`；`_document_valuation_for_memo()` 让备忘录在给了 `raw_price` 时复用 CARD-08 引擎，未给时估值段标 `data_unavailable`。 |
| 2026-09-19 | **升级 1（句级标注的「第三态」）**：§5 的 `tier_marking` 只有 fact/inference/opinion 三态，但模板脚手架（「本备忘录引用 N 条证据」「共 3 处陈述与事实不符」）既非事实也非观点——它是**关于备忘录自身**的计数。若把它硬塞进三态，要么造假 claim，要么被数值绑定规则误杀。做法：`MemoSentence.structural: bool`，`structural=True` 且 `claim_type is None` 是唯一的豁免形态；`claim_type=None` **且不 structural** 即「未标注句」，**拒绝**。这正是句级标注要堵的缺口。 |
| 2026-09-19 | **升级 2（数值句的绑定对象）**：§10 要求「数值句 100% 绑定 Calculation 或 Evidence」。§7 又要求「fact 句 100% 绑定证据」。CARD-03 导出型指标（如 `free_cash_flow`）在 `MetricResult.inputs` 里只有计算 id，故 `_claim_for_metric` 必须走 `ClaimGraph.fact_claim_for(metric, period_key)`（CARD-09 已把计算的证据闭包回填到 fact 的 `evidence_ids`），否则数值句会拿不到证据而无法渲染。为此在 CARD-09 的 `Claim` 上补了 `metric`/`period_key` 两个字段与 `fact_claim_for()`／`claims_for_signal()` 两个查询。 |
| 2026-09-19 | **升级 3（推断句的绑定是「继承」而非「自有」）**：叙述段的 inference 句（信号→推论）自己没有 `evidence_ids`，只有 `derived_from`。`_inference_binding()` 因此同时收集**自身**的 `calculation_ids`/`evidence_ids` 与**上游**的，二者取并集——一条关于某 claim 的句子，绑定它自己的来源或它上游的来源都是合法的。修此之前，`blocked` 结论的状态陈述句（如「被判定为 blocked：CFO/归母净利润 = 0.61」）含数字却无来源，会被自家校验器拦下。 |
| 2026-09-19 | **偏差 1（拒绝 = 剔除 + 记录，不是整份失败）**：§6 原文是「违反 → 该条重生成或剔除，不写入 memo」。故 `_narrative_section` **逐句**校验：不合规句被丢弃并写入 `rejected_sentence[i]:<reason>` 警告；只有**叙述器本身抛异常**才升级为 `MemoError`（不许静默清空整节）。校验器 `_validate_memo_segment_output()` 对外仍**抛 `MemoSegmentError`**（这是 §6 的硬契约）。 |
| 2026-09-19 | **偏差 2（估值区间是 opinion 而非 fact）**：§7 表格把 Valuation 段归到「假设与边界」。故估值区间句标 `opinion` 并携带 `assumption_note`（目标倍数/情景增速均为登记假设），**不冒充 disclosed fact**；「不给出合成最终区间」由 CARD-08 的 `method_dispersion` 原样转述。`data_grade` 为 `assumption_only` 时整体加警示横幅。 |
| 2026-09-19 | **偏差 3（校验器要接受「另一个注册表」的计算 id）**：估值段引用的是 CARD-08 的 `calculation_id`，它们**不是** CARD-09 图的节点。若校验器只认图内计算，合法备忘录会被判 invalid。故 `validate_memo(..., calculation_ids=)` 允许并入外部注册表的 id；两者都缺时**报 warning**（`no_calculation_registry_reference_check_skipped`）而非静默通过。 |
| 2026-09-19 | **偏差 4（tool 只接 document_id）**：§6 写「入参 symbol 或 document_id」。与前序工具一致（CARD-03 `analyze_cn_financials`、CARD-08 `run_cn_relative_valuation`），只接 `document_id`——按 symbol 会绕开 CARD-02 规范化与证据链；单独传 `symbol` 返回 `memo_missing_argument` 并说明原因。 |
| 2026-09-19 | **偏差 5（catalyst 的 quote 需要 claim 才成句）**：公司概览段的检索引用若**没有** `claim_id`，句级标注无从绑定，按「零静默」该句**跳过**（不把检索命中伪装成事实句）；催化剂段本身是 `opinion`（分析师主观视角），绑定 chunk 作为证据，故 quote 逐字校验（`_check_quote_verbatim`）仍可执行。 |
| 2026-09-19 | **测试**：§11 全绿——三段式（好／缺数据／失败注入）；逐句标注完整性（结构句无 claim_type / 非结构句必有 tier / fact 必有 claim_id / inference 必有 derived_from / opinion 必有 assumption_note）；`unverified_number` 拦截（缺 calculation+evidence 的数值句被剔除并记录）；免责声明存在性与 Markdown 渲染；白名单拒绝自造 claim_id；prompt 携带白名单。§12：CARD-01→02→03→09→10 全链，抽样 10 个数值经 `trace()` 落到 PDF 页；失败注入 #6（撤回证据）后 blocked 结论在 Risks 段如实出镜且备忘录仍可序列化。§10 校验器九例。 |
| 2026-09-19 | 全量离线 **866 passed / 23 failed / 0 errors / 2 skipped**，失败集合与基线逐条一致（passed 净增 52）。冒烟 14/14 clean，`git diff --check` clean，新增文件行尾空白扫描无命中。 |
| 2026-09-19 | **未闭合**：§13 memo 级 Numeric Accuracy / Evidence Coverage 未接 CARD-11（`InvestmentMemo.evidence_ids` + 校验器已就绪，只差基准聚合）；§14 Demo「点击结论 → 图谱回溯」的 UI 侧仍未接 `workbench.py`（与 CARD-09 同一未闭合项）；LLM 叙述器只有契约与校验，**未接真实 provider**（属 CARD-18 Multi-Agent 的调用面）。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
