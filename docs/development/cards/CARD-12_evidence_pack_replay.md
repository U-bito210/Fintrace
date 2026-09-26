# CARD-12: Evidence Pack & Audit Replay（证据包与审计回放）

| 属性 | 值 |
|---|---|
| 优先级 | P1 |
| 竞赛阶段 | 初赛版（10/12–10/15） |
| 对应赛题 | "来源可核验、执行过程可追溯、运行结果可复现"的最终交付形态 |
| 依赖 | CARD-07（events/manifest）、CARD-09（图） |
| 实现复杂度 | 中（约 3–4 人日） |
| 状态 | 🟡 代码+30 测试就绪；7/7 场景全部实现；§14 重放 UI 已随 CARD-15 落地（2026-09-20）；§13 人工验收截图待做 |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

实现每次任务的**标准产物目录**（Evidence Pack）与**历史任务时间线重放 UI**（Audit Replay），并内置 7 个失败注入现场演示场景。这是"可追溯/可验证/可复现"从能力变成评委可见成果的最后一公里。

## 2. 对应竞赛评分点

来源可核验、过程可追溯、结果可复现、现场展示。

## 3. 为什么需要这个模块

能力再强，评委看不到等于不存在。Evidence Pack 是可下载、可离线检查的交付物；Audit Replay 是"结果不是仅由 LLM 生成"的可视化证明。

## 4. 输入 / 输出

- **输入**：run 目录（manifest.json + events.jsonl + 各模块产物）
- **输出**：完整 Evidence Pack 目录 + UI 重放视图

## 5. 数据模型（Evidence Pack 目录结构，指令原文）

```text
runs/run_xxx/
├── report.md                # 人类可读报告（memo）
├── report.json              # 结构化报告
├── financials.xlsx          # 财务分析表
├── valuation.xlsx           # 估值与敏感性
├── corrections.xlsx         # 纠错清单（如有草稿核查）
├── claims.json              # 全部 Claim（含三级标注）
├── evidence.json            # 证据账本（含文档页码）
├── calculations.json        # 计算记录（公式+输入）
├── validation.json          # validator 结果
├── events.jsonl             # 审计事件流
├── manifest.json            # 运行清单
├── charts/                  # 图表（如有）
└── citations/               # 来源定位快照（如有）
```

## 6. API / Tool Contract

```python
def export_evidence_pack(run_id) -> Path: ...   # 打包 zip + 目录
# Audit Replay UI（workbench，走 CARD-15 页面骨架）：
# 选择 run_id → 按 events.jsonl 时间顺序展示：
# 文件加载 → hash → parser → 财务提取 → normalization → retrieval
#          → tool call → calculation → claim → validation → report
# 每步可展开：input / output / source / status / timestamp
```

## 7. 金融逻辑

本卡无金融计算；核心是**忠实呈现**：重放视图不得美化或跳过失败事件，blocked/failed 状态用醒目标记——失败注入演示正依赖于此。

## 8. Edge Cases

1. run 中断（无 report）→ Pack 仍导出，缺件标注 incomplete
2. events.jsonl 损坏行 → 跳过并标记，重放继续（不中断演示）
3. 大 run（千级事件）→ UI 分页/虚拟滚动，不牺牲完整记录
4. 多 run 对比 → 重放视图支持并排 diff 两次 run 的 manifest

## 9. Failure Mode

导出失败 → 明确报错与缺件清单。**禁止导出"看起来完整"的残包**。

## 10. Validator Rules

- Pack 完整性校验：13 类文件存在性检查（缺件降级导出需显著标注）
- manifest 与 events 的 run_id 一致性
- report 中引用的 claim/evidence id 在 Pack 文件内全部可解析（打包前校验）

## 11. Unit Tests

导出完整性；缺件降级标注；损坏 events 容错；zip 解包校验。

## 12. Integration Tests

一次含失败注入的 run → Pack 导出 → 重放 UI 展示失败事件 → 现场可复现。

## 13. Benchmark

Replay Success Rate 已由 CARD-07 测；本卡验收重放 UI 的呈现完整性（人工验收 + 截图入执行备注）。

## 14. Demo Method（失败注入演示剧本，7 场景）

| # | 注入 | 预期系统行为 |
|---|---|---|
| 1 | 缺失总股本 | 阻断每股估值，明确报缺 |
| 2 | 删除 PDF 某页 | citation incomplete 告警 |
| 3 | 单位万元→亿元 | unit checker 报错 |
| 4 | 草稿给错误同比 | report checker 定位错误并给正确值 |
| 5 | 旧财报+更正公告 | source conflict，按优先级取更正值 |
| 6 | 删除关键 Evidence | 对应 Claim 变 blocked |
| 7 | 改写无证据因果结论 | unsupported causal inference 标记 |

目标：证明 FinTrace"错误的时候知道自己不能下结论"。5 分钟 Demo 正片用 #6，其余备答辩追问。

## 15. 验收标准

- [x] 13 类产物文件导出与完整性校验 ← `PACK_FILES` 恰为 13 条（2 必需 + 8 完整性 + 3 可选）；`export_evidence_pack()` 产出目录 + `evidence_pack.zip` + `PACK_STATUS.json`（把缺件清单钉在包内，读者不会看漏）；`validate_pack()` 三重校验：13 类存在性、`manifest.run_id` ↔ `events.jsonl` 一致性、Pack 内引用可解析。`require_complete=True` 时**拒绝导出残包**
- [ ] 重放 UI 十一阶段时间线 + 步骤展开五要素齐全 ← **引擎已完成，UI 已接**（2026-09-20 随 CARD-15 的「审计回放」页，`workbench/demo/replay.py`；本行 2026-09-27 校正：原文写「UI 未接」已与 §16 记录矛盾）。**仍未勾的唯一原因是卡 §13 另有一条人工项**——「重放 UI 呈现完整性人工验收 + 截图入执行备注」，那是人工走查，不是代码缺口。引擎侧：`replay_timeline()` 产出 11 阶段（`file_load → hash → parse → extract → normalize → retrieval → tool_call → calculation → claim → validation → report`），每步含 `input/output/source/status/timestamp` 五要素 + 事件级明细；未记录步骤显式标 `not_recorded`（**不省略**）；失败状态按严重度冒泡到步骤（`failed > error > blocked > unsupported > warning > partial`）。**页面归属 CARD-15**（本卡 §6 原文即写「走 CARD-15 页面骨架」）
- [x] 7 个失败注入场景全部可现场复现（脚本化，一键触发） ← **7/7 通过**（CARD-13 落地后 #5 由 deferred 转为真实实现），`python -m src.cn.failure_injection` 一键跑全套：#1 阻断每股估值 ✅ / #2 citation incomplete ✅ / #3 unit_mismatch ✅ / #4 定位错误同比 ✅ / #6 下游 blocked ✅ / #7 unsupported causal ✅。**#5（旧财报+更正公告 → source conflict）已由 CARD-13 落地**：读更正公告正文的「更正前/更正后」对，按来源优先级取更正值并留 `ConflictRecord`
- [x] 全部测试离线通过 ← **原未勾**：CARD-12 新增 27 测试全绿（`tests/test_cn_evidence_pack.py` 27 passed），全量离线 **939 passed / 23 failed**，失败集合与基线逐条一致 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | 新建 `src/cn/evidence_pack.py`（13 类产物模型 + 导出/zip + 三重校验 + 11 阶段重放 + 多 run manifest diff）、`src/cn/failure_injection.py`（7 场景注册表 + 6 个真实实现 + 一键 CLI + markdown 渲染）、`tests/test_cn_evidence_pack.py`（**27 全绿**）。 |
| 2026-09-19 | **偏差 1（13 类分三档，不是「要么全有要么全无」）**：§5 只列了 13 个文件，没说哪些缺了算残包。按 §8 边缘 1（run 中断无 report 仍要导出）与 §9（禁止残包）的张力，分三档：**必需 2**（`manifest.json`/`events.jsonl`——缺了就不是 run 的记录）、**完整性 8**（`report.md`/`report.json`/`financials.xlsx`/`valuation.xlsx`/`claims.json`/`evidence.json`/`calculations.json`/`validation.json`——缺任一项 → 整包标 `incomplete` 并列出）、**可选 3**（`corrections.xlsx`/`charts/`/`citations/`——缺了只记 warning）。2+8+3=13。 |
| 2026-09-19 | **偏差 2（`manifest.json` 与 `events.jsonl` 不接受调用方覆盖）**：二者是 run **自身**的记录，不是调用方提供的产物。若允许 `artefacts` 覆盖，就能导出一个 manifest 与 events 互相矛盾的包——那正是 §10「run_id 一致性」要防的。故这两个名字被显式跳过，永远从 run store 复制。 |
| 2026-09-19 | **偏差 3（重放「阶段」与 CARD-07「事件阶段」不是一对一）**：CARD-07 的 `Stage` 有 9 个，§6 要求 11 个（多出 `hash` 与 `extract`）。不做「给 Stage 加两个枚举值」的侵入式改动（那会动 CARD-07 的契约），而是把重放做成**视图**：`hash` = 带 `sha256` 的 `file_load` 事件；`extract` = `parse` 阶段里的抽取类事件。`REPLAY_TIMELINE` 是唯一的映射表。 |
| 2026-09-19 | **偏差 4（「不可定位」要分开说，不能混为一谈）**：注入场景 #2 初版断言「页码移除后 trace 不再 locatable」——**实测失败**。原因是 `EvidenceNode.locatable = page 或 available_at 或 snapshot_id`，页码没了但**快照时点仍在**，故 locatable 依旧 True。这不是 bug，是设计：文档页定位与快照定位是两个不同的定位轴。改成断言「trace 不再落到任何 `page` 节点」（即 CARD-11 `page_attribution_accuracy` 度量的那件事），并在 `observed` 里**同时报出**「快照定位仍在，locatable 仍为 True 的 claim 有 N 条」。把一个「半真」的现象说成「全坏」才是真正的失真。 |
| 2026-09-19 | **偏差 5（`run_id` 一致性只做单向集合检查）**：`events.jsonl` 的 `run_id` 集合必须是 `{manifest.run_id}` 的子集——即不允许出现**别的** run 的事件；但不要求每条事件都被记录到 manifest（`events.jsonl` 本来就是只追加流，事件多于 manifest 字段是正常的）。 |
| 2026-09-19 | **偏差 6（引用可解析性只报「引用不存在」，不报「有未被引用的」）**：§10 要求「report 中引用的 claim/evidence id 在 Pack 文件内全部可解析」，方向是**单向**的：引用了 Pack 里没有的 → error；Pack 里有没被引用的 → 不报（`validation.json` 等文件本来就不引用 claim）。 |
| 2026-09-19 | **踩坑（值得记）**：`_collect_ids()` 初版只处理 `"evidence_id": "E1"`（单值）形态，遇到 CARD-09/10 实际输出的 `"evidence_ids": ["E1","E2"]`（**列表**）时**静默漏收**，于是「引用可解析性」这道校验对真实载荷**完全空转**（测试当场抓到）。修法：列表形态逐个收集。教训与 CARD-11 的子串隔离 bug 同源——**校验器对真实数据形态的假设若不写成测试，它就是在空转**。 |
| 2026-09-19 | **测试**：§11 全绿——13 类清单与导出/zip/状态单；缺件标 `incomplete` + 清单可见；`require_complete` 拒残包；未知名/无 manifest 的处理；调用方无法覆盖 manifest/events；run_id 不一致；引用不可解析（正反例）；目录被当文件提供时如实报 note；损坏 events 行「跳过 + 命名 + 不中断重放」；11 阶段顺序与五要素；未记录步骤显式 `not_recorded`；失败阶段冒泡；分页不丢事件（`窗口 + truncated == 总数`）；manifest diff（真差异 / 输入不同则标不可比）。§12：失败 run → Pack → 重放里失败可见；无 report 的中断 run 仍导出并标 `incomplete`。 |
| 2026-09-19 | 全量离线 **939 passed / 23 failed / 0 errors / 2 skipped**，失败集合与基线逐条一致（passed 净增 27）。冒烟 14/14 clean，`git diff --check` clean，新增文件行尾空白扫描无命中。 |
| 2026-09-19 | **未闭合**：§14 重放 UI（归 CARD-15）；场景 #5 source conflict（归 CARD-13）；§13「重放 UI 呈现完整性人工验收 + 截图入执行备注」需 UI 落地后才能做。 |
| 2026-09-19 | **补记**：场景 #5 已随 CARD-13 落地，`run_scenario(5)` 由 deferred 变为真实通过；原「deferred 要如实列出」的两项测试改为「不得有任何场景停在 deferred」，从负面断言升级为正面覆盖。 |
| 2026-09-20 | **§14 重放 UI 已落地（由 CARD-15 施工，本卡记账，卡 §6 原话即"走 CARD-15 页面骨架"）**：新增「审计回放」页（`workbench/demo/replay.py`）。① 仓库里原本**没有任何 run**（`output/demo/runs` 从未生成），所以页面第一次打开会**真的录一次 run**（`record_demo_run()` 走同一条确定性管线，事件是真实发生过的步骤，不是造数据）；② 11 阶段时间线由 `replay_timeline()` 产出，未记录到的阶段**显式标 `not_recorded`**，不省略；③ 证据包导出后**报缺哪些件**（`validate_pack` 的 incomplete 清单），残包不伪装成完整包，zip 可直接下载。录制/导出落 `output/`（已 gitignore），演示重放多少次仓库状态都不变。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
