# CARD-04: Accounting Scope & Restatement Detection（会计口径与重述检测）

| 属性 | 值 |
|---|---|
| 优先级 | P1（初赛窗口内完成） |
| 竞赛阶段 | 初赛版（9/23–9/29） |
| 对应赛题 | 赛题2（会计口径变化识别）/ 赛题5（scope_error） |
| 依赖 | CARD-01（文档解析）、CARD-02（version 标注） |
| 实现复杂度 | 中（约 3 人日） |
| 状态 | 🟡 代码+63 测试就绪；信号检测／三态决策／Validator 三条规则／§14 横幅全部打通；「全部测试离线通过」受 CARD-01 的 Tushare 快照阻塞（见 §16） |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/analysis/scope_signals.py`：从文档中检测会计口径变化与重述信号，绑定原始 Evidence，并在存在口径变化时**阻断直接同比 / 使用调整后比较数据 / 输出可比性警告**。不再假设 periods.py 能解决口径问题。

## 2. 对应竞赛评分点

金融专业性（口径意识是专业评委必考）、数据准确性（重述数据误用是典型错误）、分析逻辑。

## 3. 为什么需要这个模块

真实披露中"原披露→更正公告→重述"很常见；v2 方案对此完全失明，会在旧值上计算同比并输出错误结论——这在赛题5 的 scope_error 检查中也会漏报。

## 4. 输入 / 输出

- **输入**：`ExtractedFact[]` + 文档段落（更正公告文本、年报附注"会计政策变更"章节）
- **输出**：`AccountingScopeSignal[]` + 可比性决策

## 5. 数据模型

```python
class ScopeSignalType(str, Enum):
    restated                       # 重述
    accounting_policy_change       # 会计政策变更
    prior_period_error_correction  # 前期差错更正
    consolidation_scope_change     # 合并范围变化
    comparable_period_adjusted     # 可比期间已调整
    unknown                        # 无法判定

@dataclass
class AccountingScopeSignal:
    signal_type: ScopeSignalType
    company: str
    periods_affected: list[str]
    evidence_ids: list[str]        # 必须绑定原始 Evidence（更正公告段落/附注页）
    detected_at: str
    detector_rule: str             # 触发规则说明
```

## 6. API / Tool Contract

```python
def detect_scope_signals(facts, documents) -> list[AccountingScopeSignal]: ...

def comparability_decision(signal: AccountingScopeSignal) -> str:
    """返回 'block_yoy' | 'use_adjusted' | 'warning'：
    - prior_period_error_correction + 有更正后数据 → use_adjusted
    - restated 且比较数据可得 → use_adjusted（用重述后比较值）
    - consolidation_scope_change → warning（可比性受损）
    - unknown + 影响重大 → block_yoy
    决策规则集中在模块常量表。"""
```

Agent 工具 `DetectCnScopeSignalsTool`：入参 `document_id`；出参 `status / signals / comparability[]`。

## 7. 金融逻辑

1. 检测规则（确定性文本匹配，先读真实样例再固化关键词表）：更正公告标题模式、年报附注"会计政策变更/前期差错更正"章节、表格"调整后/调整前"双列、重述声明段落
2. use_adjusted 时比较值必须取"调整后"列（CARD-02 的 Version.CORRECTED/RESTATED）
3. LLM 不参与信号判定；关键词表外的可疑段落 → unknown 信号 + 人工复核提示

## 8. Edge Cases

1. 更正公告与原报告分属两个文档 → 跨文档信号关联（依赖 CARD-13 冲突解决时按优先级取更正值）
2. 表格同时有"调整前/调整后"两列 → 两套 facts 分别标 AS_REPORTED / RESTATED
3. 附注影响金额不重大（披露了但金额小）→ 仍出信号，severity 字段区分
4. 检测到信号但比较数据不可得 → block_yoy

## 9. Failure Mode

检测不到 ≠ 不存在：本卡输出附带"检测覆盖范围声明"（哪些规则、哪些章节）；无法判定时给 unknown 信号并阻断相关同比——**宁可阻断也不误算**。

## 10. Validator Rules

- block_yoy 期间出现百分比同比 → validator 报错（这是本卡存在的意义）
- 每个 signal 的 evidence_ids 必须可解析到文档页
- use_adjusted 的计算必须引用 Version != AS_REPORTED 的比较值

## 11. Unit Tests

合成更正公告 + 双列重述表样例：信号类型正确；use_adjusted 取调整后列；block_yoy 生效；unknown 不放行。

## 12. Integration Tests

CARD-01→02→04→03：含更正的场景下同比走调整后或被阻断，全链不产出错误百分比。

## 13. Benchmark

Scope Accuracy 指标接入 CARD-11（合成集 planted 信号识别率）。

## 14. Demo Method

财务分析页顶部出现"可比性警告"横幅，点击展开：哪个公告、哪一页、影响哪些期间、系统做了什么决策（阻断/用调整值）。

## 15. 验收标准

- [x] 6 类信号检测各有测试用例 — `tests/test_cn_scope_signals.py`（每类一条封闭算例 + 规则 id 断言；`会计估计变更` 与 `重分类` 两条"同类型不同规则"的分支另有专测）
- [x] 三种决策路径（block/use_adjusted/warning）全部有测试 — 含"有调整后数据 → use_adjusted / 无 → block"的分支对照，以及多信号取最严的聚合测试
- [x] 信号 100% 绑定 Evidence — 每条信号 `evidence_ids` 均来自已定位事实，且 `validate_comparability` 第 2 条规则把它变成硬校验（`test_the_validator_passes_on_both_integration_filings`）
- [x] **全部测试离线通过** — 本卡新增 63 项全绿，全量离线 **468 passed / 23 failed**；23 项全部依赖 `data/snapshots/cn/600519.SH_20260810_tushare_v1.json` 与 `300750.SZ_20260812_tushare_v1.json`，与本卡无关（背景见 CARD-01 §16「已知阻塞」） → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

### 15b. 决策落地验收（本卡的核心，单列出来）

- [x] `use_adjusted` 时比较值真的取调整后列：`annual_04` 的上期取自「上期金额（已重述）」= 1,000,000，同比 +10%；若误用被取代的 900,000 会读成 +22.22%
- [x] `block_yoy` 时全文档零百分比：`annual_05` 的 `revenue_yoy` 只剩水平值 1,000,000，`delta_pct=None`、`delta_abs=None`、`calculation_id=None`，且计算注册表内不存在任何 `*_yoy` 记录
- [x] 无信号 ≠ 干净：`comparability=None` 与"命中 0 条规则"分别有测试；覆盖范围声明由 `detection_coverage()` 随报告发布
- [x] 干净文档零误报：8 份无口径披露的合成分件（年报/公告/研报草稿）检测结果全部为空
- [x] 更正公告（无报表结构、不产生任何规范化为事实的数值）同样能出信号并绑定到页

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-17 | **开工前先做了一次可行性核查，结论是本卡按原文无法开工**：合成语料里 4 个检测面（公告标题／附注章节／正文段落／表格版本列）覆盖率为 0/4，且年报解析器产出的 `text` 事实数为 0（`TableParser` 只留单元格）。三个决策由用户拍板：① 先扩 CARD-01 再开 CARD-04；② 扩合成语料而不是等真实 PDF；③ 词表统一由 CARD-04 持有。三者都已落地。 |
| 2026-09-17 | **新增 `src/cn/analysis/scope_signals.py`**：`ScopeSignalType`(6) / `SignalSeverity`(material·immaterial·undetermined) / `ComparabilityDecision`(3)；`DECISION_RULES` 为 §6 决策表的**数据**（逐信号 × 是否可得调整后数据）、`DECISION_PRECEDENCE` 为多信号取最严；`detect_scope_signals`（文本规则 + 表格版本列双通道）、`comparability_decision`、`aggregate_comparability`、`build_scope_report`、`detection_coverage`。检测全程为确定性正则，无 LLM、无网络。 |
| 2026-09-17 | **词表归属（用户决策③）**：新建 `src/cn/disclosure_lexicon.py`，位于 `normalization` 与 `analysis` **之下**（只 import 标准库），避免 CARD-02 反向依赖 CARD-03/04 形成环。CARD-02 的 `version.py` 改为消费 `DISCLOSURE_CORRECTED/DISCLOSURE_RESTATED`，表格版本列词表 `VERSION_COLUMN_KEYWORDS` 也收归到该表：一份词表，两个消费方。`SCOPE_WORDING` 的**顺序即优先级**（最具体优先），近失（pattern 命中但 context 不满足）直接不触发、不降级到更弱规则。 |
| 2026-09-17 | **偏差记录（相对 §5 数据模型）**：`AccountingScopeSignal` 除卡内四字段外，另加 `severity / surfaces / detail / document_id / adjusted_evidence_ids`，并暴露 `adjusted_data_available` / `comparability` 两个派生属性、`to_dict()`。原因：§8 edge case 3 要求区分"披露了但不重大"，§8 edge case 4 要求区分"有／无调整后数据"，这两件事必须落在数据上而不是散在判断逻辑里；`surfaces` 是 §9 覆盖范围声明的粒度依据。 |
| 2026-09-17 | **本卡对 CARD-03 的真实回压（重要）**：`use_adjusted` 一开始只是"打了个标签"——决策说用调整后数据，而 `analyze_financials` 仍按 `(metric, scope, version)` 分组，`_select_group` 取 `sorted(versions)[0]`。重述年报复述比较列的现实形态是：本期列未标注版本（AS_REPORTED）、上期列标注「已重述」（RESTATED），**没有任何一个版本分组同时含两端**，于是同比直接消失、决策对数字没有任何影响。已新增 `series.merge_versions()`：仅在 `use_adjusted` 下调用，按 `(metric, scope)` 合并版本维、每个期间取最新披露版本，key 用哨兵 `MERGED_VERSION` 使调用方不可能把它误认成声明版本；`_select_group/_series_in_group/_group_currency` 增加 `merged` 参数（此时版本只作为"本系列最新依据版本"的摘要，按 `version_rank` 取最新而不是按字母序取 `AS_REPORTED`）。默认路径（无 CARD-04 决策）行为一字未改，CARD-02 §7.3 rule 4「同一指标的两个版本不得配进同一比较」仍由 `build_series` 的分组键强制。 |
| 2026-09-17 | **纠偏（跨卡缺陷，Phase A 引入、本卡修掉）**：CARD-01 新增的"已定位正文/附注文本 N 行（含章节归属），供口径与重述检测使用"这句**解析器记账 note** 被写进了 `DocumentRecord.notes`，而该字段会被 CARD-02 的 `_document_blob` 扫来判定**文档级披露版本** —— 句子里的"重述"两字于是把一份干净年报的全部事实标成 RESTATED（实测 `annual_01`：13 条规范化事实全部 RESTATED）。已把 note 改写为不含任何披露词汇的措辞（`已定位正文与附注段落 N 行（含页内章节归属）。`），并加测试锁死该不变式。教训：`notes` 带语义（是文档"申报"的口径，不是解析器的自述）。 |
| 2026-09-17 | **顺手修的跨卡缺陷 2**：`_detect_table_signals` 对同一规则（`table_version_columns`）在不同表上的两条信号会被 `_dedupe` 合并，而合并时 `detail` 只保留第一条 —— 一份年报的「合并利润表」与「前期差错更正影响金额」两张表都有版本列时，读者只能看到一个表名。已改为 `detail` 去重拼接。 |
| 2026-09-17 | **测试**：`tests/test_cn_scope_signals.py` **63 项全绿**。另含两条"防回归不变式"：① 检测模块不得引入 `llms/requests/httpx/urllib/openai/anthropic`（§7.3 模型不参与判定，用源码级断言守）；② 解析器记账 note 不得污染文档版本标注。 |
| 2026-09-17 | **§14 UI**：`document_service.document_analysis()` 增加 `comparability / comparability_blocked / scope_signals / comparability_banner`，每条信号的每个 `evidence_id` 都解析到页码与 bbox（解析不到时 `locatable=False` 明示，而不是丢掉这条信号）。`workbench.py` 财务分析页顶部新增三态横幅：有决策→红/黄横幅 + 可展开（哪个文档、哪一页、影响哪些期间、系统做了什么决策、逐条信号与证据）；无信号→一条 caption 说明"未命中 ≠ 不存在"；未评估→不显示。用 `streamlit.testing.v1.AppTest` 实测三种状态均正确渲染（`annual_04` 黄幅、`annual_05` 红幅、`annual_01` caption）。 |
| 2026-09-17 | **Agent 工具**：新增 `DetectCnScopeSignalsTool`（入参 `document_id` + 可选 `detected_at`；出参严格对齐 §6 的 `status / signals / comparability[]`，另附 `blocked_documents / coverage / labels`），并接入 `build_cn_snapshot_tools()` 的文档层工具组（与 ingest/normalize 共用同一 registry）。无事实时返回 `status="blocked"`（"完全无产出"，与 CARD-03 的 status 词表一致）而不是 `error`，并在 `note` 里写明"未命中不等于不存在"。 |
| 2026-09-17 | **顺手修的既有缺陷 3**：`scripts/bootstrap_documents.py` 用一份手写文件清单，语料扩到 13 份后它仍只注册 10 份 —— 演示库因此一直没有重述样例，§14 横幅在真实文档上根本无法演示。已改为**读语料 manifest**（`tests/fixtures/documents/manifest.json`）取文件名与声明类型，与测试摄入用的类型同源，不会再漂移。同步重建了本地演示库（旧 `registry.db` 里存着语料字节可复现修复前留下的 10 条幽灵记录，已重命名为 `registry.db.stale-*` 保留，未删除）。 |
| 2026-09-17 | **Corpus 与 CARD-01 的同步改动**（本卡前置条件，详情见 CARD-01 §16）：合成语料新增 3 份（`annual_04_restated_comparative` / `annual_05_reclassification` / `announcement_04_correction`）与对应 gold；CARD-01 新增正文/附注文本事实（`kind="text"`,`raw_value=None`）与表格版本列标注（`meta["version_label"]`），`verify_corpus` 改为真正二次重渲染比对。 |
| 2026-09-17 | **未完成（下一步）**：① §13 的 Scope Accuracy（合成集 planted 信号识别率）未接入 CARD-11 —— 目前由本卡 64 项测试覆盖，但未产出可入库的基准分数；② §8 edge case 1 的**跨文档**关联（更正公告 ↔ 原报告）依赖 CARD-13，本卡只做到"公告自身能出信号且证据可定位"；③ 可比性决策目前只经 Agent 工具与工作台暴露，`api.py` 的 `/api/documents/*` 尚无 analysis 路由（该路由族属 CARD-01 的契约，未在本卡擅自扩面）；④ §9 的种子关键词表未在真实披露语料上拟合，表外措辞不产生信号。 |
| 2026-09-18 | **补齐两项**：① 财务层基准现包含合成集 scope signal detection accuracy；② 新增 `GET /api/documents/{document_id}/analysis`，复用 `document_service.document_analysis()`，统一返回 CARD-02/03/04 的分析、证据回链和校验状态。跨文档更正公告关联仍留给 CARD-13，真实语料词表拟合仍未完成。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
