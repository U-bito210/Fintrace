# CARD-15: Workbench Demo UI 增量（演示界面）

| 属性 | 值 |
|---|---|
| 优先级 | P1（贯穿，随各业务卡逐页落地） |
| 竞赛阶段 | 初赛版 |
| 对应赛题 | 现场展示（评分项） |
| 依赖 | 各业务卡（页面随能力出现） |
| 实现复杂度 | 中（分散投入，合计约 4 人日） |
| 状态 | 🟡 8 个页面已挂载 + 52 测试全绿 + 冒烟 22/22 clean；CARD-02/13 的 §14 页面与真实演示材料未做（见 §16） |

## 1. 目标

在现有 Streamlit workbench 上做**最小增量**新增 Demo 页面，覆盖 5 分钟 Demo 主故事所需的全部界面。**禁止大规模重写**——现有 workbench.py 的既有页面不动，新页面以独立模块挂载。

## 2. 对应竞赛评分点

现场展示（Demo 流畅度直接决定评委第一印象）。

## 3. 为什么需要这个模块

Demo 是初赛视频与决赛现场的载体；但 v2 的"全量拆分 workbench"是工程洁癖，挤占竞赛窗口，本卡收敛为最小增量。

## 4. 输入 / 输出

- **输入**：各业务卡的确定性引擎（CARD-01/05/09/12/22/23/24/25/26），经 `src/cn/demo_service.py` 只读服务层接入
- **输出**：`workbench/demo/` 包 + 挂载入口（新增页面不动旧代码路径）

## 5. 页面清单（对应 Demo 主故事）

**本表已按 `DEMO_FLOW.md` v3.2 校正。** 剧本 §7 写明"任何对本剧本的修改必须同步更新
CARD-15 页面清单"，而旧的清单停在 CARD-21 之前的时代：它列的 11 个页面既不含
ACME / 结论护照 / Thesis Fragility 三个创新展示步骤（4b / 7b / 10b），又含若干
`workbench.py` 已有的页面（Upload / Execution Trace / Financial Analysis / Valuation /
Memo 在旧版工作台里早已存在，不需要本卡重复新增）。以剧本为准，与剧本冲突的地方
一律改这里。

| 页面 | 支撑卡片 | 剧本步骤 | 关键交互 |
|---|---|---|---|
| 研报纠错 | 05 | 步骤 6+7 ★高潮一 | 植入清单 ↔ 实际检出逐条对齐（漏检显形）+ 逐条跳回原始 PDF 页 + 覆盖范围表（哪些规则真的跑了） |
| ACME 约束 | 22 | 步骤 4b 创新一 | 六族约束逐族报告 + **未检查族**显式列出 + 违规项跳回原文 |
| 结论护照 | 23 | 步骤 7b ★核心动作 | 六证明块 + 点一次「VERIFY CLAIM」**真的重跑一次证明**（不是回放页面加载时算过的结果） |
| 主张-证据图谱 | 09 | 步骤 9+10 结论回溯 | 结论 → 计算 → 证据 → 原始页，四层逐层展开 + 状态传播原因 |
| 逻辑脆弱性 | 24 | 步骤 10b 创新三 | 关键依赖 / 单点失效 / 最小割集 / 监控计划；`thesis_source` 如实回报 `declared` |
| 时间重验证 | 25 | 失败注入 F9 | 同指标跨期替代事件 → 三态血缘 + 全量 vs 增量对账 |
| 审计回放 | 12 | 收尾：时间线 + 导出 | 录制一次**真实** run → 11 阶段时间线（未记录阶段标 `not_recorded`）→ 证据包导出并报缺件 |
| 对抗注入 | 26 | 失败注入 F1–F8 | 七场景逐个注入真实故障并断言系统反应 + FinFuzz 逐错误类型召回（`N/A` 行保留不删） |

**未做（剧本里也没有）**：CARD-02 的「原始披露值 → 规范化值」对照表、CARD-08 估值页、
CARD-10 备忘录页、CARD-13 并列展示页 —— 见 §16。
| Validator | 现有扩展 | 阻断/降级结果展示 |
| Audit Replay | 12 | 选 run_id → 时间线重放 |
| Export Evidence Pack | 12 | 一键导出 zip |

## 6. API / Tool Contract

```python
# workbench/demo/__init__.py —— 挂载入口
@dataclass(frozen=True)
class DemoPage:
    label: str        # 侧栏标签
    render: Callable[[], None]
    card: str         # 支撑卡片号
    step: str         # 剧本步骤（4b / 7b / 10b …）

def mount_demo_pages() -> List[DemoPage]: ...   # 在现有入口追加挂载，不修改既有页面函数
def page_labels() -> List[str]: ...
```

**页面不直接触碰引擎。** 唯一的碰点是 `src/cn/demo_service.py`（只读），它把
CARD-01/05/09/12/22/23/24/25/26 的输出包成带 `status` 的信封：

```python
catalog() / filing_cases() / draft_cases()          # 案例目录
corrections(doc_id=None)                            # CARD-05 纠错报告 + evidence_index
graph_view(key) / acme_view(key) / fragility_view(key)
passport_view(key, claim_id) / verify_view(key, claim_id)   # 护照与重跑分离
temporal_view(key, old_id, new_id)                  # 同指标跨期替代
record_demo_run(key) / runs_catalog() / replay_view(run_id) / export_pack_view(run_id)
injections_catalog() / run_injection(scenario_id) / adversarial_view(operator)
jump(target, evidence_id, *, index) / render_page(target, page, highlights)
```

三条硬约束（都是验收项，不是风格偏好）：

1. **只读** —— 不写文档注册表、不改图；演示重放多少次仓库状态都不变（导出写 `output/`）。
2. **失败不抛** —— 拿不到就返回 `{"status": "unavailable", "reason": ...}`。"语料里没有
   这份东西"是正常起点状态，应该显示成一句人话，不是 traceback（§9 页面崩溃是硬项）。
3. **不编数字** —— 引擎说什么返回什么，缺输入就报缺。

**信封的 `status` 不得吞掉引擎自己的 `status`。** CARD-22/24/25 载荷里也有一个叫 `status`
的字段，含义是结论状态而非调用成败；早期实现直接覆盖，于是"引擎说自己没法判定"被静默
改写成"一切正常"。现在引擎状态先搬到 `acme_status` / `fragility_status` /
`revalidation_status` / `pack_status` / `finfuzz_status`，信封 `status` 才另起。

实现约束：沿用现有 Streamlit 版本与组件风格；样式走现有设计约定；不引入新前端依赖。

## 7. 金融逻辑

无；但页面呈现必须如实：blocked/failed/warning 状态醒目，**不美化系统的不确定性**——这本身就是演示卖点。

## 8. Edge Cases

1. 大 PDF 预览卡顿 → 页级渲染 + 按需加载页图片，不为性能牺牲证据定位
2. 长任务（真实模型）→ 执行轨迹页轮询进度（沿用现有任务轮询机制，不引入 SSE 新依赖，除非时间允许）
3. 旧数据无新字段 → 页面兼容降级显示

## 9. Failure Mode

页面崩溃不影响后端任务（UI 与服务隔离，沿用现有服务层约定）；浏览器控制台零报错是验收硬项。

## 10. Validator Rules

- 每个新页面入口可在 smoke_workbench 中被发现（脚本扩展断言）
- Demo 页面不直接读数据库裸数据——必须走服务层接口（与现有架构一致）
- 公开视图函数**永不抛**：缺输入返回 `unavailable` 信封并带 `reason`（唯一例外是
  `mount_demo_pages`，模块缺 `render` 是结构性错误，必须响）
- 信封 `status` 与引擎 `status` 必须共存，不得互相覆盖
- 逐错误类型读数里空分母记 `N/A`（字符串），行保留不删

## 11. Unit Tests

`tests/test_cn_demo_ui.py`（52 项，全离线）：

- **挂载面**：8 个页面按剧本顺序挂载、标签与卡片号一一对应、4b/7b/10b 各有页面、
  与既有 14 个页面无重名；页面清单从**运行中的工作台**侧栏 radio 读出，不手抄第二份
- **服务层**：未知案例/草稿/run 一律返回 `unavailable` 且带原因，永不抛；11 个公开视图
  都返回带 `status` 的信封；信封不吞引擎 `status`（`revalidation_status == "partial"` 是活证）
- **零静默形态**：`temporal_view("profit_continued")` 挑不出替代对就报缺（不硬凑假血缘）；
  `ytd_vs_single` / `valuation` 召回为 `"N/A"` 且行保留；录制→回放→导出在临时目录跑通
- **呈现语气**：`blocked/violated` 红、`partial/stale` 黄、`N/A/not_recorded` 中性灰
- **渲染**：8 个页面逐个在 `AppTest` 下渲染无异常

冒烟：`scripts/smoke_workbench.py`（22/22 clean，页面清单同样从应用自身发现）。

## 12. Integration Tests

按 DEMO_FLOW.md 剧本人工全流程走查 + 截图；控制台零报错。

## 13. Benchmark

Latency（页面响应）粗粒度记录入执行备注即可，不设专项。

## 14. Demo Method

本卡即 Demo 的物质载体；每个页面完成后立即按 DEMO_FLOW 对应步骤验收。

## 15. 验收标准

- [x] **8 个页面**（按 DEMO_FLOW v3.2 校正）全部挂载且互不干扰既有功能；既有 14 页一个不少
- [x] smoke 通过（22/22 clean）+ 页面渲染无异常（`AppTest` 逐页跑）+ 对照原页面无回归
      （全量离线 **23 failed / 1497 passed / 2 skipped**，失败集合与 CARD-26 时**逐条一致**，
      均为缺失的 Tushare 快照工件，与本卡无关）
- [ ] 5 分钟 Demo 剧本可全程无卡点走完（彩排验证）← 真实材料**已就位**
      （`data/demo/annual_report.pdf` 229 页 + `report_draft.md` 含错草稿），见 §16
      现状已可**机器化核查**：`python scripts/rehearse_demo.py --strict` 逐条走剧本 13 步。
      当前 **1 步会卡**（6+7：植入 8 处经逐条对齐检出 **7/8**，唯一漏检 E4
      `valuation_multiple_error` —— CARD-05 §7.1 要求检「行业错配」但该分支未实现，见 CARD-05 §16）、
      3 步待人工走查 ——
      **按 §5 硬项「8/8 检出方可上台」，未解决前不满足上台条件**
- [x] 新增页面代码均在 `workbench/demo/` 内；`workbench.py` 只多了挂载行；
      `src/cn/document_service.py` 只把 `render_pdf_page` 抽出来给 Demo 复用（不另写一份渲染器）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **前置核查**：本卡是 26 张卡里唯一 `☐ 未开始` 的。卡 §5 的页面清单已过期（停在 CARD-21 之前，缺 4b/7b/10b 三个创新步骤的页面，又重复列出旧工作台已有的页面），按 `DEMO_FLOW.md` v3.2 §7「剧本改动必须同步 CARD-15 页面清单」校正为 8 页。 |
| 2026-09-19 | **实现**：`src/cn/demo_service.py`（只读服务层，三条硬约束：只读 / 失败不抛 / 不编数字）+ `workbench/demo/` 8 个页面 + `_shared.py` 呈现助手。`workbench.py` 仅加挂载 try/except 与 `NAVIGATION` 扩展，既有页面函数一行未动。 |
| 2026-09-19 | **踩坑**：① 信封 `status` 一度覆盖引擎 `status`，把"引擎说没法判定"静默改成"一切正常" —— 加 `_envelope(payload, engine_key)` 保留引擎状态（CARD-25 默认案例 `revalidation_status = partial` 是活证）。② `corrections.py` 循环变量 `index` 被 `evidence_index` 字典同名遮蔽，Streamlit 按钮 key 撞车（"multiple elements with the same key"）。③ FinFuzz 空分母召回是**字符串** `"N/A"`，塞进数值列会让整张表渲染失败（表消失比显示 N/A 更糟）。④ `VERSION_SUPERSESSION` 必须同时给旧节点（`supersedes`）和新节点，只给一边引擎只能拒答。⑤ `load_corpus()` 返回 `CorpusLoad` 不是 list。 |
| 2026-09-19 | **测试**：`tests/test_cn_demo_ui.py` 52 项全绿；`scripts/smoke_workbench.py` 重写为**从应用自身发现页面清单**（旧版手抄 `PAGES` 列表是第二份真相源），并显式断言 8 个 CARD-15 标签存在 —— 挂载静默失败会直接响，而不是让被测页面变少。 |
| 2026-09-19 | **未闭合**：① **演示材料未采集** —— 剧本 §1 要求真实 A 股年报 + 人为注入错误的真实研报草稿，仓库里 15 份全是合成件。8 个页面现在跑的是合成语料，**检出率不得表述为真实研报上的表现**（每页底部"本页的边界"已如实声明）。材料到位后**无需改 UI**。② CARD-02「原始披露值 → 规范化值」对照表、CARD-08 估值页、CARD-10 备忘录页、CARD-13 来源冲突并列展示页**未做**（剧本未要求，且这些卡另有 §14/§17 交接记录）。③ §15「5 分钟剧本彩排」待材料与人工走查。④ 「全部测试离线通过」受 Tushare 快照工件阻塞（23 项失败与本卡无关）。 |
| 2026-09-20 | **剧本自检脚本**（`scripts/rehearse_demo.py`）：§15「5 分钟剧本彩排」原来只能人工走查，现逐条跑 `DEMO_FLOW.md` 的 13 步（主故事 12 + F9 + F1–F8），判据只有一条——**每一步「评委看到什么」现在能不能真的取到数据**。四态：`ready` / `degraded`（可讲但话术必须按 ⚠ 行收窄）/ `blocked`（会卡）/ `manual`（服务层无接口）。**`manual` 是「本脚本查不了」，不是「查过没问题」**，报告里逐条点名。`--strict` 有 `blocked` 则退出 1。 |
| 2026-09-20 | **自检实测结果**：可讲 1 / 可讲需声明 7 / 会卡 2 / 待人工 3。**会卡**：步骤 1+2（真实材料缺失，违反 §0.1）、步骤 6+7（只植入 5 处错误，§5 硬项是 8/8 检出方可上台；E2/E4/E5/E6/E7/E8 六类未植入）。**待人工**：步骤 5 财务分析、步骤 8 估值、步骤 11 删证据阻断。**声明类**：4b 六族只跑 4 族（cross_period/cross_source 缺输入）、3+4 缺 parse 事件、9+10 有 2/14 证据未定位到页、10b Thesis 是显式声明而非语料推断、收尾证据包 `incomplete`（缺 6 项完整性文件）、F9 `partial`（4 条引擎告警）、F1–F8 七个场景全跑通但 FinFuzz 无 `numeric_scale` 行（E2 无评测数据）。 |
| 2026-09-20 | **踩坑（剧本自检）**：① ACME 载荷的 `violations` 是**全部已评估约束**（含 `passed`/`skipped_missing_field`），违反数在 `totals["violated"]` —— 实测 `len(violations)=7` 而 `violated=1`，拿列表长度当命中数会报错 7 倍。② 引擎没自带 `status` 时 `_envelope` 搬过来的 `acme_status` / `finfuzz_status` 是 `None`，展示处必须 `or payload["status"]` 兜底（`workbench/demo/acme.py` 页面已修）。③ 同一文件多处 Edit 放同一条消息会只落一处而回执都是"成功"（本轮又复现一次）。 |
| 2026-09-20 | **测试**：`tests/test_cn_demo_rehearsal.py` 9 项全绿。刻意**不**断言每步是 ready 还是 blocked —— 那会随材料到位而变化，且会逼人伪造材料去修绿。只防**漂移**：步骤表与剧本一一对应（含顺序）、状态不越界且每步都有输出、`--strict` 退出码 ≡ 存在 blocked、传错步骤名返回 2。全量离线 **23 failed / 1506 passed / 0 errors**，失败集合与 CARD-15 收尾时逐条一致（14/6/2/1）。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-09-26 | **真实材料彩排复测 + 抓到一个潜伏回归**。① 材料就位后完整彩排：**可讲 2 / 可讲需声明 7 / 会卡 1 / 待人工 3**（会卡由 2 降 1 —— 步骤 1+2 因 `data/demo/annual_report.pdf` + `report_draft.md` 就位而转 `[可讲]`）。步骤 6+7：植入 8 处**逐条对齐检出 7/8**；E3 改稿后**真的产生 `calculation_error`**；首次构建 **39s < 50s 窗口**，"现场必须预热"不再成立。② ⚠️ **潜伏回归**：`tests/test_cn_demo_ui.py::test_real_material_draft_end_to_end` 标了 `@pytest.mark.integration`，被默认套件 `-m "not integration"` **整条排除** —— 所以默认套件报"1568 passed 全绿"时它**实际是红的**：硬编码断言还停在"检出 3 处（E1/E2/E8）"，而 E3 改动后是 7 处。已按该测试**自己注释的要求**（"数字变了就必须重新核对"）先用 `scripts/rehearse_demo.py --step 6+7` 独立复核，再更新为 7 项，并**新增「未检出必须恰好是 E4」的断言**（该分支一旦落地会被顶红，逼人同步 DEMO_FLOW §1 + CARD-05 §16 + 本卡，而不是只改数字）。复跑 **1 passed**。**教训：`-m "not integration"` 全绿 ≠ 全绿；材料在的机器上必须显式跑 `-m integration`。** ③ 唯一会卡仍是 E4 `valuation_multiple_error`：CARD-05 §7.1 要求检「行业错配」，而 `_multiple_findings` 只实现了「可复算倍数」那一半 —— 已记入 CARD-05 §16，**§5 判据不下调**。 |
