# CARD-13: Source Conflict Resolver（来源冲突解决）

| 属性 | 值 |
|---|---|
| 优先级 | P1（基础区分 DATA CONFLICT / VERSION SUPERSESSION / NEW_INFO 为 P0 可选早段） |
| 竞赛阶段 | 初赛版（10/12–10/15） |
| 对应赛题 | 赛题2/5（重述与更正场景）；数据准确性；**并作为 ACME 跨源约束与 Temporal Revalidation 的裁决层** |
| 依赖 | CARD-01（多文档）、CARD-04（重述信号）、CARD-22（ACME 跨源结果四分类） |
| 实现复杂度 | 低中（约 2.5 人日，含三态区分与四分类接入） |
| 状态 | 🟡 代码+73 测试就绪；并列展示 UI 归 CARD-15 |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/conflict.py`：当同一指标同一期间出现多个来源值（原披露 vs 更正公告 vs 重述 vs 第三方数据源）时，**先分类变化类型，再按明确优先级规则裁决**，全程记录，永不静默覆盖；无法确定时交 Validator 阻断或降级。

## 2. 对应竞赛评分点

数据准确性、金融专业性（真实世界数据治理意识）、分析逻辑。

## 3. 为什么需要这个模块

真实金融材料必然冲突。没有裁决规则的系统要么用错值，要么随机用后到的值——两种都是事故。失败注入演示 #5 依赖本卡。

## 4. 输入 / 输出

- **输入**：同一 (company, metric, period) 的多个候选 NormalizedFact
- **输出**：选定值 + `ConflictRecord`（若存在冲突）

## 5. 数据模型

```python
@dataclass
class ConflictRecord:
    conflict_id: str
    change_kind: str        # DATA_CONFLICT | VERSION_SUPERSESSION | NEW_INFORMATION（三态区分，见 §7b）
    source_a: str           # document_id / snapshot_id
    source_b: str
    selected_source: str
    reason: str             # 优先级规则名 + 说明
    resolver: str           # 规则版本
    timestamp: str
```

## 6. API / Tool Contract

```python
def resolve_conflicts(facts: list[NormalizedFact]) -> tuple[list, list[ConflictRecord]]:
    """按优先级裁决：后发布且明确更正前值的监管披露 > 原监管披露
    > 官方公司公告 > 第三方数据源。
    同级冲突（如两家第三方源不一致）→ 不裁决，标记 unresolved。"""

def unresolved_report(conflicts) -> list[dict]: ...
```

## 7. 金融逻辑

优先级规则（集中常量 + 注明适用范围）：监管披露指交易所/巨潮披露的公司报告与更正公告；"明确更正前值"依据 CARD-04 的 prior_period_error_correction 信号；第三方数据源（如数据商用快照）永远低于官方披露。**数值不同但口径不同（scope/version）不构成冲突**——先由 CARD-02 区分，只有同口径同版本不同值才算冲突。

## 7b. 三种变化状态区分（不能全部叫 conflict，v3.2 强化）

**必须区分三种状态，传播规则不同（对接 CARD-25 Temporal Revalidation）**：

| 状态 | 判据 | 例子 | 处理 |
|---|---|---|---|
| DATA_CONFLICT | 同期间同口径、无版本关系、两个官方/来源值不一致 | 两家第三方源不一致；双官方源矛盾 | 裁决 > unresolved → 阻断该指标 |
| VERSION_SUPERSESSION | 新版本明确替代旧版本 | 原财报 → 更正公告（E37 被 E95 替代） | 旧值保留（superseded），用新值；下游 stale/blocked 传播 |
| NEW_INFORMATION | 新增数据，无替代关系 | 新季度数据补充 | 计算 stale（period 更新）→ claim needs_refresh，不覆盖历史 |

**ACME 跨源四分类（MATCH/ROUNDING_MATCH/CONFLICT/NOT_COMPARABLE）裁决责任**：ACME（CARD-22）只负责检测与分类；本卡对 CONFLICT 类执行裁决；NOT_COMPARABLE（口径未统一）由 CARD-02 先归一后再判。**处理输入必须带 change_kind 上下文。**

**正确用例示例**：
- "原财报值 10 亿 → 更正公告值 12 亿"：VERSION_SUPERSESSION（不是 data_conflict）
- "年报 PDF 营收 35.2 亿 vs 快照 provider 营收 35.2 亿"：MATCH（无冲突）
- "年报 PDF 营收 35.2 亿 vs 快照 provider 营收 36 亿（同期间同口径）"：DATA_CONFLICT → 提交裁决

## 8. Edge Cases

1. 更正公告本身被再更正 → 链式裁决，取最新一环（supersession 链）
2. 优先级相同且值不同（双官方源矛盾，罕见）→ unresolved + Validator 阻断该指标
3. 冲突值差异在舍入容差内 → 不视为冲突（容差=显示位数）
4. 第三方源与官方源一致 → 无冲突，正常输出
5. 更正公告与重述同时存在 → 分别建立 supersession 与 restatement 链，不混用

## 9. Failure Mode

unresolved 冲突 → 该指标在下游全部标记 blocked（宁可阻断不误选）。**静默覆盖是红线**。

## 10. Validator Rules

- 每个 selected_source 的 reason 可追溯到规则名
- unresolved 冲突的指标不得出现在任何 fact claim 中（validator 遍历检查）
- ConflictRecord 不可变（append-only）

## 11. Unit Tests

四级优先级正反例；链式更正；同级矛盾阻断；舍入容差不误报；scope 不同不误判冲突；**三态区分用例**（DATA_CONFLICT vs VERSION_SUPERSESSION vs NEW_INFORMATION 各构造正例）；ACME 四分类接入（CONFLICT 提交裁决、NOT_COMPARABLE 返回归一要求）。

## 12. Integration Tests

失败注入 #5（旧财报+更正公告）全链：值取更正后，报告显示 ConflictRecord 与理由。

## 13. Benchmark

Scope/Version 准确性间接覆盖；本卡无独立指标，由失败注入 #5 复现率保障。

## 14. Demo Method

演示 #5：上传旧年报 + 更正公告 → 系统展示冲突对（两个值并排 + 裁决理由）→ 报告使用更正值。

## 15. 验收标准

- [x] 四级优先级规则全部有测试（`tests/test_cn_conflict.py::test_regulatory_filing_outranks_*` 等 8 项，
      含「未知来源不得胜出」的反例；优先级表 `SOURCE_PRECEDENCE` 本身也有断言）
- [x] 无静默覆盖（代码路径断言）：`assert_no_silent_override` **在 `resolve_conflicts` 内部每次调用都执行**，
      非仅测试；记录按 candidate 粒度比对（同一文档可产出多个候选，按 source 比对会漏）
- [x] unresolved 阻断链路打通：`unresolved_report` / `unresolved_metrics` / `blocked_metric_keys`
      + `validate_conflicts(graph=...)` 遍历 fact claim 检查（无 graph 时报 skipped，不静默通过）
- [x] 失败注入 #5 可现场复现（`python -m src.cn.failure_injection` → 7/7 通过）
- [x] 全部测试离线通过 ← 待 Tushare 快照重建（23 项缺失快照失败集不变） → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **实现**：新建 `src/cn/conflict.py`（~1900 行）+ `src/validation/conflict_validator.py` + `ResolveCnSourceConflictsTool` + `tests/test_cn_conflict.py`（73 项）。 |
| 2026-09-19 | **两级分组（关键设计）**：外层 `(company, metric, scope, currency)`、内层 `period`。若把 period 放进分组键，`NEW_INFORMATION` 永远无法检出（同期才可比 ⇒ 判据恒为同期间），三态里有一态成为死代码。scope/currency 进分组键而非进比较，是因为 §7 明确「口径不同不构成冲突」——合并/母公司营收本就该并存。 |
| 2026-09-19 | **偏差 1：同文档内数值不一致交回 CARD-02**。`annual_01` 同一 (metric, period) 有 1234567（报表正文）与 2000000（主要会计数据）两条，若按「同级来源矛盾」判会误伤成 unresolved。改为 `source_id` 相同时委托 CARD-02 的 `primary_sort_key`，并新增规则 `rule.intra_document_table_precedence` 留痕。CARD-13 只裁决跨来源。 |
| 2026-09-19 | **偏差 2：更正公告的数值来自正文**。`announcement_04_correction.pdf` 经 CARD-02 抽取得到 **0 个** fact（更正对写在句子里，不在表格里），故新增 `correction_pairs()` 从句级解析「更正前 X 元…更正后 Y 元」。分句串接（同一句常被切成两个段落 fact）+ 按角色取值（as_reported 子句取**末**个数字，因期间「2024」本身也是数字；corrected 子句取**首**个）。产出带 `page` / `bbox` / `quote`，`fact=None` 并在 warnings 标 `value_from_prose`，**不伪装成表格事实**。 |
| 2026-09-19 | **偏差 3：口径由调用方声明，不从正文推断**。更正公告不写「合并/母公司」。`correction_pairs(scope=None)` 默认**不声明**——此时该候选独立分组，不会被悄悄并入已声明口径的组；失败注入 #5 显式声明 `合并` 并留痕 `scope_source=caller`。宁可少裁决，不替调用方猜口径。 |
| 2026-09-19 | **偏差 4：排序以版本优先、时间次之**。`_order_by_recency` 先比 `version_rank` 再比 `available_at`。若时间优先，一条没有时间戳的更正公告会被降级成「更旧」，进而把 VERSION_SUPERSESSION 误判成 DATA_CONFLICT——正是 §7b 警告的误读。另新增「更正公告自身发布日期」解析（`发布日期：` 行或 CARD-01 的 `publication_date`），缺失时如实告警，**绝不填当天日期**。 |
| 2026-09-19 | **偏差 5：ACME 四分类词表落在本卡**。CARD-22 尚未实现，而 §11 要求「CONFLICT 提交裁决 / NOT_COMPARABLE 返回归一要求」可测。故四分类常量与 `classify_cross_source` / `compare_pair` 定义在本卡，CARD-22 复用而不另立拼写（沿用「同一短语不能被一张卡认出、被另一张漏掉」的既有约定）。 |
| 2026-09-19 | **修 3 个真 bug**：① `年`/`年度` 正则重叠（`\s*年\s*` 先吃掉「年」，`年度` 只剩「度」可匹配）导致文档级追溯期间检不出；② 反向锚点序（「更正后…更正前…」）把期间数字当成金额；③ 静默覆盖断言按 source 粒度比对会漏（同文档多候选互相掩护），改为 candidate 粒度并在 `ConflictRecord` 加 `candidate_a/b`。三个都由新测试当场抓出。 |
| 2026-09-19 | **回归**：全量离线 **1013 passed / 23 failed / 2 skipped**，失败集合与 CARD-12 收尾时逐条一致（report 2 / cli 1 / workbench_service 14 / api 6）；冒烟 14/14 clean；`git diff --check` 无输出。 |
| 2026-09-19 | **未闭合**：`test_agent_research_api::test_agent_research_response_hides_secrets_and_paths` 偶发失败（`tmp/` 目录状态相关，单独运行必过；两轮全量中一轮 24 failed / 一轮 23 failed）——**与本卡无关**，但记在此处以免下次误判为回归。§14 并列展示 UI 归 CARD-15。 |
| 2026-09-19 | **下游接入（CARD-22 F5 已实装，验证 §11 的预留判断）**：`src/cn/acme/cross_source.py` 只构造 `ConflictCandidate` 后调 `compare_pair`，`VERDICT_STATUS` 仅做「四分类 → CARD-22 检查状态」的词表映射，**未另立拼写**。接入时暴露一条**配对键的使用约束**，记在此处以供后续消费者参考：**配对键若含 `scope`，`NOT_COMPARABLE` 就永远不会出现** —— 「合并营收 vs 母公司营收」会退化成双向 `skipped_missing_field`，既是对文档的假陈述（文档确实印了营收行），又把可比性阻断埋成数据缺口。正确用法是先按 `(metric, period, currency)` 找异口径对应项，再交给 `compare_pair`，由 `_comparability_blocker` 给出 `NOT_COMPARABLE: 口径不同`。 |
| 2026-09-19 | `compare_pair` 至此有了**第一个真实消费者**（此前只有测试与 `resolve_conflicts` 的分组逻辑在用）。§11 假设的「四分类词表只在本卡定义一次、CARD-22 复用」得到验证；若将来出现第二个消费者，应复用同一入口而不是再写一遍比较。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
