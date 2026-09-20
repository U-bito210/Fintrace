# CARD-11: Real-world Benchmark（真实基准评测）

| 属性 | 值 |
|---|---|
| 优先级 | P0（从第一个模块开发起持续建设，不是最后补） |
| 竞赛阶段 | 贯穿初赛（10/12–10/15 收口） |
| 对应赛题 | 赛题7 内部评价机制 + 全部评分项的量化证据；**FinFuzz 集成点（CARD-26）** |
| 依赖 | 无（与各业务卡互为配套，业务卡完成一块接一块）；FinFuzz 子集依赖 CARD-26 |
| 实现复杂度 | 高（约 8 人日+，分散投入；含 FinFuzz 集成 1–2 人日） |
| 状态 | 🟡 框架+46 测试就绪；Synthetic 已实测，Real/Holdout 材料待采集 |
| Skill 要求 | 动手前加载 financial-agent-evaluation |

## 1. 目标

在现有 benchmark/real_benchmark/ablation 基础上（`benchmarks/cn_agent_v1.json` 保持不动）新建 `cn_agent_v2` 三级评测体系：**Synthetic Test Set / Real Public A-share Test Set / Holdout Set**，覆盖 24 项指标 + **FinFuzz 对抗评测（CARD-26 集成）**。任何 LLM 新能力必须与 baseline 对比。评分项的量化证据全部出自本卡。**Benchmark 必须证明创新有效（ablation）——不能只报功能。**

## 2. 对应竞赛评分点

结果稳定性、数据准确性、计算准确性——以及"不编造 benchmark 结果"的红线。

## 3. 为什么需要这个模块

竞赛答辩必被追问"你怎么证明准确？"。没有真实材料基准就无法回答；v2 把 benchmark 排在阶段4（赛后）是重大排期错误，本卡纠偏为贯穿建设。

## 4. 输入 / 输出

- **输入**：三级测试集 + 各模块运行结果
- **输出**：`output/benchmark/cn_agent_v2/` 评测报告（markdown + json），含环境 fingerprint

## 5. 数据模型（三级测试集）

| 集合 | 内容 | 存储 |
|---|---|---|
| Synthetic | 合成年报/公告/研草稿 + planted 错误 + 封闭算例真值 | `tests/fixtures/`（入库，生成脚本随附） |
| Real | 公开披露的真实年报/半年报/季报/公告/研报草稿 + 人工标注真值 | `data/benchmark_real/`（**gitignore**；提交下载脚本 + sha256 清单；版权材料不进 Git） |
| Holdout | Real 的扣留子集，只在收口评测用 | 同上，目录隔离 |

真值标注格式：`{document_id, metric, period, expected_value, unit, scope, page}`——每条可回查原文页。

## 6. API / Tool Contract

```python
# 扩展 src/cn/benchmark.py（沿用现有任务注册格式，先读现状）
python -m src.cn.benchmark --suite cn_agent_v2 --set synthetic --out output/benchmark/
python -m src.cn.benchmark --suite cn_agent_v2 --set real --out output/benchmark/   # integration
```

## 7. 金融逻辑（24 项指标）

**提取组**：Extraction Precision / Recall / F1；Numeric Exact Match；Unit Accuracy；Period Accuracy；Scope Accuracy；Page Attribution Accuracy
**引用组**：Citation Precision；Citation Recall
**计算组**：Calculation Accuracy（含 YTD 单季化、同比状态迁移）
**核查组**：Checker Precision / Recall / F1；False Positive Rate
**智能体组**：Tool Selection Accuracy；Parameter Accuracy；Task Completion Rate；Evidence Coverage
**安全组**：Unsafe Conclusion Leakage（inference 冒充 fact 的泄漏率）
**复现组**：Replay Success Rate；Multi-run Consistency
**成本组**：Latency；Token Cost
**对抗组（FinFuzz 集成，CARD-26）**：Mutation Detection Precision / Recall / F1；per-error-type Recall（numeric/unit/period/scope/version/citation/causal…）；False Positive Rate；Holdout performance

阈值纪律：**所有目标值待各模块 baseline 测试后冻结**，评测报告里 baseline 数字如实呈现，禁止预填 95%/98% 类目标。

## 7b. Ablation（创新有效性必须证明，不能只报功能）

Benchmark 必须带 ablation 对照，每项创新要有"无创新 vs 有创新"的比较：

| 创新 | Baseline | 实验组 | 比较指标 |
|---|---|---|---|
| ACME（CARD-22） | Parser only | Parser + accounting constraints | financial extraction error detection / statement integrity error detection |
| Claim Passport（CARD-23） | citation only | FPO verifier | unsupported claim detection / stale claim detection / calculation reproducibility |
| Fragility（CARD-24） | —（非测准确率） | 人工构造 Thesis graph | critical dependency correctness / cut-set correctness / state propagation correctness |
| Temporal Revalidation（CARD-25） | full recomputation | incremental affected-subgraph recomputation | nodes recomputed / latency / same deterministic result |
| FinFuzz（CARD-26） | 无 FinFuzz 的常规测试 | 含 FinFuzz 对抗测试 | 错误检测覆盖率 / per-error-type recall |

ablation 结果进 `output/benchmark/cn_agent_v2/ablation/` 报告表，答辩材料直接可引用。**未运行前一律标 TARGET / TO BE MEASURED，禁止编造数字。**

## 8. Edge Cases

1. 真实材料无法下载（链接失效）→ 下载脚本多源（巨潮/交易所）+ sha256 校验失败报错
2. 人工标注量有限 → Real 集先覆盖 2–3 家公司全披露链，标注规范文档化，宁小勿错
3. LLM 采样波动 → multi-run 指标固定 n≥3，报告含方差
4. Holdout 泄漏 → 收口前 Holdout 不参与任何开发调试（目录权限+流程纪律）

## 9. Failure Mode

某模块指标无法计算（如无真实材料）→ 报告该指标标 N/A + 原因，不删除行——**评测报告的诚实性本身是被评审的对象**。

## 10. Validator Rules

- 真实/离线结果物理隔离：文件名 real 前缀 + 摘要页运行环境标注（模型/温度/日期/git commit），混排时报错
- 每份报告含环境 fingerprint（沿用现有机制）
- 评测代码不得为通过率特判任务

## 11. Unit Tests

指标计算器正确性（构造已知 P/R/F1 的假结果核对）；报告生成格式；fingerprint 稳定性。

## 12. Integration Tests

synthetic 集全链离线跑通；real 集 integration 标记（需凭证与授权）。

## 13. Benchmark

本卡自身即 benchmark 体系；对 LLM 新能力（如 CARD-05 提取器）强制跑 baseline 对比（无对比不合并）。

## 14. Demo Method

答辩材料附评测报告表：各模块 baseline 数字 + 环境指纹 + "真实/合成"分列。演示中可展示评测命令一键复跑。

## 15. 验收标准

- [ ] 三级测试集建立（Real ≥2 家公司全披露链 + 人工标注） ← **未勾**：三级**框架**已建（`benchmarks/cn_agent_v2.json` 注册三级 + 存储纪律 + 隔离闸门 + 真值 schema），Synthetic 级**已实测**；但 **Real/Holdout 级的真实材料未采集**——需要网络、多源门户（巨潮/交易所）与版权授权，本会话环境不具备。已交付采集脚本 `scripts/fetch_benchmark_real.py`（多源 + sha256 校验失败即报错、绝不写占位文件）与标注规范 `docs/benchmark/LABELING_SPEC.md`（含「什么情况下**不**标注」七条，宁小勿错）。**没有真实材料时报告如实标 N/A，不造数。**
- [x] 24 项指标计算器全部有单测 ← `tests/test_cn_benchmark_v2.py` 46 项；`BASE_METRICS` 恰为 24（断言 `len(BASE_METRICS) == 24` 且无重复），8 组逐组覆盖，每个计算器都用可手算的构造用例核对 P/R/F1/FPR
- [x] cn_agent_v1 零回归；v2 synthetic 全离线可跑 ← `benchmarks/cn_agent_v1.json` **未改动**；`scripts/run_benchmark_v2.py --set synthetic` 聚合既有组件基线（CARD-01 抽取 / CARD-05 核查）产出 `output/benchmark/cn_agent_v2/report.{json,md}`，已测 8 项 / 未测 20 项（未测原因逐行写出，不删行）
- [x] 真实/离线隔离与标注机制有测试 ← `isolate_payload()` 按**路径段**机械校验（`benchmark_real` 或 `real_` 前缀），real→synthetic 目录、synthetic→`benchmark_real` 目录两个方向都拒绝；`write_report()` 落盘前强制调用。另有 `test_non_synthetic_component_value_carries_a_warning_note` 守「非合成来源必须带标记」
- [x] 阈值冻结流程文档化（baseline → 冻结 → 版本化） ← `freeze_thresholds(baseline, margin=)` 从**实测** baseline 派生（高优指标下调、成本类上调），未测指标保持 `None`；`THRESHOLDS` 全为 `None`，报告 `threshold_status=UNFROZEN`，markdown 打印 `TARGET / TO BE MEASURED`。技能与规范均已记录该流程
- [x] 全部测试离线通过 ← **原未勾**：CARD-11 新增 46 测试全绿，全量离线 **912 passed / 23 failed**，失败集合与基线逐条一致（均为缺失的 Tushare 快照） → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | 新建 `src/cn/benchmark_v2.py`（24 项指标计算器 + 三级模型 + 组件归并 + 隔离闸门 + 指纹 + 报告渲染 + 阈值冻结）、`benchmarks/cn_agent_v2.json`（三级注册表，v1 未动）、`docs/benchmark/LABELING_SPEC.md`（真值规范）、`scripts/fetch_benchmark_real.py`（多源采集 + sha256 校验）、`scripts/run_benchmark_v2.py`（CLI）、`tests/test_cn_benchmark_v2.py`（**46 全绿**）。 |
| 2026-09-19 | **复用而非重造（本卡最重要的一条）**：CARD-01 已产出 `output/benchmark/cn_agent_v2/document_extraction_baseline.json`，CARD-05 已产出 `checker_baseline.json`（二者 `suite` 字段**本来就写着 `cn_agent_v2`**）。故本卡**不重新测量**抽取/核查指标，而是实现 `component_metrics()` 读取既有基线、`merge_components()` 归并进 synthetic 级。归并是**严格**的：两个组件声明同一指标 → 抛 `ValueError`（一个指标有两条定义等于没有定义）。这条决定了后续 CARD-22/23/24/25/26 只要按 `metrics: {<cn_agent_v2 metric id>: {value, unit, note}}` 发布，就能自动进报告。 |
| 2026-09-19 | **诚实性三条硬约束**：① 未测指标**保留为行**，`value=None` + `note` 说明原因，报告打 `N/A（原因）`（§9「评测报告的诚实性本身是被评审的对象」）；② `THRESHOLDS` 全 `None`，**不预填**任何 95%/98% 类目标，`freeze_thresholds()` 只从实测 baseline 派生；③ 未运行的 ablation 一律 `TARGET / TO BE MEASURED`（`NOT_MEASURED` 常量），`build_ablation_table()` 不编造。 |
| 2026-09-19 | **偏差 1（隔离检查按路径段而非子串）**：初版用 `"real_" in path` 判断，结果 pytest 的临时目录名（`test_real_tier_must_...0`）**天然含 `real_`**，闸门静默失效——测试当场抓到。改为按**路径段**判定：段名等于 `benchmark_real` 或以 `real_` 开头。这个 bug 值得记下来：*任何用子串做安全判断的代码，都会在某个调用者命名习惯下停止生效*。 |
| 2026-09-19 | **偏差 2（Real 级只能交付机制，不能交付材料）**：§15 要求 Real ≥2 家公司全披露链。真实年报需要网络 + 门户会话 + 版权授权，且 `data/benchmark_real/` 按 AGENTS.md 必须 gitignore，**无法在本会话内完成，也不该伪造**。故本卡交付：三级注册表、真值 schema、采集脚本（失败即非零退出，绝不写占位 PDF）、标注规范、以及报告侧的 N/A 诚实降级。§15 第一项如实保留未勾并在卡内写明原因。 |
| 2026-09-19 | **偏差 3（延迟/成本按「行」而非「逐次调用」）**：`cost_metrics` 对传入的 `cost_rows` 取均值 + p95，并**拒绝**静默合并多币种——`cost_currency` 不一致时 `unit="MIXED"` 明确暴露，避免把 CNY 与 USD 直接相加。 |
| 2026-09-19 | **偏差 4（multi-run 的样本纪律）**：§8 边缘 3 要求 n≥3。`replay_metrics` 在 n<3 时仍给出值，但把 `n=k_below_3` 写进 `note`——**给出数字的同时标注它还不够可信**，比直接隐藏更有信息量。 |
| 2026-09-19 | **测试**：§11 全绿——每个计算器一个可手算用例（含 `prf1(3,1,1)=(0.75,0.75,0.75)`、period 错配同时计 FP+FN、checker FPR=0.25、agent 四指标、leakage=2/3、replay 2/3 + consistency 0.5、adversarial per-error-type 明细）；`no_matched_labels` 等 N/A 语义；阈值冻结（高优下调 / 成本上调 / 未测保持 None）；ablation 未运行不编造 + 已运行如实取值；隔离两个方向 + 未知层级；指纹稳定性与 key 顺序无关；组件读取三种形态 + 定义冲突报错 + 非合成来源带标记；端到端 synthetic 离线跑通并落盘。 |
| 2026-09-19 | 全量离线 **912 passed / 23 failed / 0 errors / 2 skipped**，失败集合与基线逐条一致（passed 净增 46）。冒烟 14/14 clean，`git diff --check` clean，新增文件行尾空白扫描无命中。 |
| 2026-09-19 | **未闭合**：Real / Holdout 材料采集（需网络 + 授权）；FinFuzz 对抗组（`adversarial_metrics` 已就绪，等 CARD-26 的变异语料）；§7b 全部五条 ablation 未运行（表结构已就绪，等 CARD-22/23/24/25/26 各自发布 treatment 结果）；`--set real --verify-labels` 的标注格式校验器未实现（规范已写，校验器属增强项）。 |
| 2026-09-19 | **跨卡改动（由 CARD-26 §15 落地，本卡记账）**：CARD-26 的 FinFuzz 组件基线接通了本卡的 synthetic 聚合链。① **注册表加一行** `("card26_finfuzz", "finfuzz_baseline.json", None)` —— 组件名与文件名由 `src.cn.finfuzz.baseline` 的 `COMPONENT` / `BASELINE_FILENAME` 持有，注册表引用同一常量，改名不会静默把基线变成孤儿。② **`case_count` 语义收窄**：第三列原先必须填一个整数，现在允许 `None` = "由基线文件自报"；新增 `component_case_count()`，**载荷里的 `case_count` 优先于注册表兜底值**。原因：FinFuzz 的样本数随语料与 seed 变（现 128），在注册表里再写一个数就是**第二处真相**，会与紧挨着的基线文件漂移。card01(10) / card05(3) 的载荷里没有这个键，行为逐字不变。③ `tests/test_cn_benchmark_v2.py::test_report_over_the_committed_component_baselines` 的枚举加上 finfuzz —— 那条测试是"真聚合路径"的唯一覆盖，新组件不进去就等于没被聚合过。④ **本卡指标定义未动**：四项对抗指标仍由本卡 `adversarial_metrics()` 定义，CARD-26 只做搬运，符合本卡 §7 归并闸门（同一指标禁两定义）。实测 `--set synthetic` **已测 8 → 12 项**，`groups.adversarial` 四项 `source == card26_finfuzz`，tier `case_count = 141 = 10 + 3 + 128`。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
