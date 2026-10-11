# FinTrace-CN 前端改进方案

方向：**米白编辑杂志风（方案 B）× Ghostfolio 排版骨架**，参照效果图 `design_mockups/D.png`。
范围：仅 `workbench.py` 展示层（`style()` / `header()` / 各页面 markdown 结构）与 `.streamlit/config.toml`。
不碰：`src/cn/` 服务层、数据/校验逻辑、API。

## 0. D 系列分页效果图（2026-09-30，纯参考件）

按 D 稿同一套设计语言补齐的分页静态效果图（HTML + 同名 PNG，数据均为示意），
入口索引 `D-index.html`；落地时仍以本文件的边界与验证清单为准：

| 文件 | 页面 | 覆盖的版式原型 |
|---|---|---|
| `D1-market.html` | 市场与行情 | 复权口径胶囊、行情 Hero（大数字+走势+区间列）、盘口九宫格、缺失警示条 |
| `D2-financials.html` | 财务表现 | 报告期胶囊、双图卡（柱图+增速线）、利润表密排表、部分披露警示 |
| `D3-valuation.html` | 同行估值 | PE 大数字+同业条形、PB 条形+气泡散点、估值公式卡、纳入/剔除记录 |
| `D4-evidence.html` | 证据与校验 | 筛选胶囊、统计磁贴、密排台账+详情面板（JSON 载荷）、门控清单 |
| `D5-agent.html` | AI Agent 研究 | 会话双栏（墨块气泡/AI 白卡/工具链胶囊）、任务-用量-证据侧栏 |
| `D6-trace.html` | Agent 执行轨迹 | 回放控制条、编号步骤卡（着色分类）、可展开载荷、统计磁贴 |
| `D7-report.html` | 研究报告 | 杂志式文档（目录+护照横幅+章节正文+复核结论框） |
| `D8-daily.html` | 每日复盘 | 指数磁贴（涨红跌绿）、广度表、行业涨跌条、事实/计算/叙事三分区 |


---

## 1. 问题诊断（为什么"乱"）

1. **三层主题叠加互覆盖**：Apple 基础层（`style()` 前半）→ v3 深蓝终端层 → Wise 绿层，后层用 `!important` 压前层，同一组件（侧栏、按钮、badge）有多套冲突规则。
2. **标题层级混用**：h1–h6 任意使用，眉题/副题没有统一节奏。
3. **卡片样式多套并存**：panel / warn / trace / badge / container(border) 各自为政，圆角 8/10/13/14/16/18px 混杂。
4. **数字排版无规范**：行情数字、证据 ID、表格数字没有统一的等宽/tabular 处理。

## 2. 设计规范（已定稿）

### 2.1 色板（token 已落地 `APPLE_TOKENS` + `--wise-*` 变量换值）

| 用途 | 值 | 说明 |
|---|---|---|
| 画布 canvas | `#FAF8F3` | 米白纸感 |
| 侧栏/次底 parchment | `#F6F2E9` | 羊皮纸 |
| 卡片 | `#FFFFFF` | 纯白浮于纸面 |
| 墨色 ink | `#191610` | 正文/标题/主按钮底 |
| 次级 muted | `#7A7264` / 弱 `#A49B8A` | |
| 发丝线 | `#E7E0D2`（divider `#EFE9DC`） | 唯一分隔线色 |
| 品牌藏蓝 accent | `#2B4A8B` | 链接/证据 ID/走势线 |
| 涨/跌（A股语义，专用） | `#C53A3A` / `#1F8A5D` | 只用于行情，与品牌色分离 |
| 状态 | ok `#1F8A5D` · warn `#9A6B14` · blocked `#B5544A` | 徽章用浅底深字 |

### 2.2 字体

- 展示字体（h1–h3、hero、大数字）：`Georgia, 'Source Han Serif SC', 'Noto Serif SC', 'SimSun', serif`
- 正文：`Segoe UI / Microsoft YaHei / PingFang SC`（现状保留）
- 等宽（证据 ID、代码、表格数字）：`ui-monospace, SFMono-Regular, Menlo, Consolas`
- 数字一律 `font-variant-numeric: tabular-nums`

### 2.3 几何

- 圆角统一 **10px**（按钮药丸形 999px 除外）
- 阴影全部去除（发丝线分隔）
- 区块节奏：眉题（0.68rem 大写字距 0.14em）→ 标题 → 细线 → 内容

### 2.4 组件规范

- **badge 五态**：`verified/warning/blocked/snapshot/neutral`，浅底深字药丸
- **主按钮**：墨底 `#191610` + 纸白字；次按钮白底墨字发丝线边
- **KPI 磁贴**（st.metric 自动继承）：白底发丝线、衬线大数字、小号大写标签
- **密排数据表**（st.dataframe 自动继承）：白底、表头细墨线、行发丝线、数字右对齐等宽
- **AI 叙事卡**（待做）：紫色 `#6B4FA8` 左边线 + 「AI 生成 · 需人工复核」标签
- **trace 时间线**（待做）：`01/02/03` 等宽编号 + 耗时右对齐

## 3. 已完成（2026-09-28，已验证）

| # | 内容 | 验证 |
|---|---|---|
| 1 | 三层旧主题**原位换肤**：Wise 绿 27 个色值 → 编辑米白色板（不新增覆盖层） | 视觉走查 |
| 2 | `APPLE_TOKENS` 全量替换为新 token；plotly 模板自动继承 | smoke |
| 3 | 追加 **v4 编辑排版层**（CSS 末段）：衬线标题、tabular 数字、纸感画布、10px 圆角、去阴影、墨色主按钮 | smoke |
| 4 | `.streamlit/config.toml` 主题同步（primary `#2B4A8B`、bg `#FAF8F3`） | — |
| 5 | `header()` 重构为 **D 式 hero**：衬线大标题 + RAW 收盘价衬线大数字（证据 ID 角标）+ 近 40 根 RAW 收盘 SVG 迷你走势线（纯可视化，不派生新数字；无涨跌幅字段故不放涨跌徽章） | ✅ smoke 22/22 |
| 6 | 全量回归 | ✅ pytest 1742 passed（本机须把 `TMPDIR` 指到可写目录绕开 pytest 临时目录 ACL 损坏；**不要用 `--basetemp`**，会引入 shim 的 `\\?\` errno 53 伪回归） |
| 7 | **v4b 组件层**（CSS 末段续写，对齐 D 稿）：`.eyebrow`/`.sechead`+`.tag`/`.factrow`/`.narr`+`.cite`/`.narrnote`/`.route`/`.tstep` | ✅ smoke 22/22 |
| 8 | `overview()` **事实与叙事分列**：左 `.factrow` 事实摘要、右 `.narr` 解读轨；标签为「服务层自动生成 · 需人工复核」（`research_overview_insights` 文档串自述 deterministic，**不得标成"模型叙事"**）；PE 句带真实证据 ID `.cite` | ✅ smoke 22/22 |
| 9 | **研究路径步骤条** `research_route()`：由 `RESEARCH_ROUTE` + `RESEARCH_ROUTE_STEP` 驱动，当前步用 `i` 高亮，不携带任何快照状态 | ✅ smoke 22/22 |
| 10 | 五页统一眉题（市场与行情/财务表现/同行估值×2/证据与校验/财务分析）；图表硬编码色全部换 token（`chart_bar`/`chart_line`/`primary_wash`，涨跌用 `quote_up`/`quote_down`）；`trace()` 改编号时间线（外部内容一律 `escape`）；证据台账补「截止」列 | ✅ smoke 22/22 |
| 11 | 第二批收尾：落地页 `.home-wordmark` 改衬线；每日复盘指数磁贴去内联字号改 `.dr-stat-num`/`.dr-stat-chg`（衬线大数字 + tabular）；证据台账状态列 `verified → ● 通过`（**只映射实测出现过的取值**，其余原样透传） | ✅ smoke 22/22 |
| 12 | `overview()` **完全按 D 重排**：eyebrow → hero → **估值磁贴**（关键估值读数）→ **研究路径** → 摘要｜AI 解读 → 关键数据图示 → 同行估值摘要 → **证据台账** → **AI 分析过程** → 分隔线 → 页尾（AI 研究质量 / 为什么相信这份报告 / 研究内容状态 / 数据覆盖与限制）→ `.pagenote` 页脚。`research_route()` 从 `main()` 移入 `overview()`（总览的路径栏排在磁贴之后，其它页仍然是首屏路径栏） | ✅ smoke 22/22 + CDP 实测 DOM 顺序与几何 |
| 13 | 顺带清理与去重：删掉死代码 `_trace_legacy`（全仓无调用点）→ 换成共享的 `_trace_view()` + `_trace_steps_html()`（总览摘要与轨迹页**同源**，不再各写一遍）；新增 `cached_research_trace` 缓存包装；`EVIDENCE_STATUS_LABELS` 提为模块常量，`evidence()` 与总览台账共用 | ✅ smoke 22/22 |
| 14 | **统一页头节奏**：新增 `page_eyebrow()` / `page_head()` + `.pagetitle` 样式（ecru 眉题 → 衬线标题 → 右对齐 tag + 发丝底线），**13 个页面全部接入**（市场与行情/财务表现/同行估值×2/证据与校验/文档智能/财务分析/评测与消融/研究任务/AI Agent 研究/每日复盘/研究报告；总览只用 `page_eyebrow()`，标题仍由 hero 承担）。替换掉原来的 `###`/`####` 裸标题，并补上此前缺眉题的轨迹页与报告页 | ✅ CDP 逐页实测：13/13 标题衬线 20.8px、tag 右对齐、`stException` 全为空 |
| 15 | 补齐 `.panel` / `.card-note` 缺失的 CSS 定义（总览「可信度状态」面板此前在标记里使用但无任何规则，裸样式渲染），按编辑风发丝线白卡补齐 | ✅ smoke 22/22 |
| 16 | 修正净利润线过期颜色词：图例与说明「绿色线」→「暖棕线」（`chart_line` 已是 #8A7A5C，文字未跟随） | ✅ smoke 22/22 |
| 17 | 修复 `test_workbench_api::test_refresh_endpoint_*` 墙钟敏感缺陷：刷新快照 ID 内嵌 cutoff（缺省取 now），跨日后断言必炸；测试内 monkeypatch `refresh._coerce_now` 钉到 2026-09-28 收盘后，恢复「离线且不依赖墙钟」契约 | ✅ 单测 + 全量 1742 passed（2026-09-29） |
| 18 | **总览证据台账换 D 式密排表**：新增 `_ledger_table_html()`（全单元格 escape；状态列只对已读作「通过」的值着色，未映射状态原样透传），替换只读 `st.dataframe`；Evidence ID 列等宽、行发丝线、表头小号大写 | ✅ smoke 22/22 + 全量 1742 passed |
| 19 | **v4c 组件铬统一层**（D 语法推广到 Streamlit 自身 chrome，各页不改结构自动继承）：`st.metric` 磁贴白卡化（原羊皮纸底）、主区 `stExpander` 白底发丝线 10px、`stInfo/stWarning/stError/stSuccess` 编辑风化、落地页 `home-*` 区块收编为白卡+发丝线+衬线 wordmark | ✅ smoke 22/22 + 全量 1742 passed（2026-09-29） |
| 20 | **v4d 深色块收编**：`st.code`/`st.json`（估值公式、财务分析计算式、任务详情、每日复盘事实/计算、诊断 JSON 共 8 处）由深色高亮块改为纸面等宽卡（ink 文字、token 色压平为墨色系）；`st.tabs` 改发丝线下划线 + 墨色激活页签。进度条自动继承 primary 藏蓝，无需处理 | ✅ smoke 22/22 + 全量 1742 passed（2026-09-29） |
| 21 | **总览 KPI 磁贴角标**：4 个 stretch 证据按钮（「收盘价证据」等）换成每贴一枚 `width="content"` 的证据 ID 小药丸（禁用态显示「证据未覆盖」），保留 `open_evidence` 一跳能力；删除角标化后冗余的「关键数值可追溯」caption。角标显示真实证据 ID（tushare 快照为 `fact_*` / `calc_*` 体系） | ✅ smoke 22/22 + AppTest 走查 + CDP 截图（2026-09-29） |
| 22 | **证据与校验台账 D 式密排**：`st.dataframe` 换 `_ledger_table_html()` 密排表（ID 列 mono、状态徽章列、全部 escape），行数截断 `EVIDENCE_TABLE_ROWS=60` 并明示「前 60 / 共 N 条」；行选择改「选择证据查看详情」选择器（跨页 `selected_evidence_id` 命中可见行时同步选择器，详情面板查 `filtered_records` 语义不变），筛选器与 JSON 导出保留 | ✅ smoke 22/22 + AppTest 断言（密排表/mono/选择器/截止列）+ CDP 实测 1175 条快照渲染（2026-09-29） |
| 23 | **财务分析区块标题**：「指标表（点击行查看计算过程）」「计算过程」两个 `#####` 裸标题换 `.sechead`（h4 + 右 tag）；指标表本体维持 `st.dataframe`（canvas 约束，见第二批第 9 条） | ✅ smoke 22/22 + AppTest 断言（2026-09-29） |
| 24 | **落地页价值药丸对齐 D 图 meta 行**：`.value-pills` 由药丸底（描边胶囊）改为 D `.meta` 式点分隔弱化文字行（`span + span::before` 打点，去底去边），base 层与 v4c 覆盖层同步改写 | ✅ smoke 22/22 + CDP 截图（2026-09-29） |
| 25 | **v4e 分页示例对齐层**（对照 D1–D8 示例，2026-09-30）：每日复盘「指数收盘」改 D8 式 `.idxtile` 磁贴（大写标签+证据角标+衬线大数字+涨红跌绿），新增**市场广度密排表**（直读 `market_characteristics`，不派生），该页 6 个 `####` 区块标题 `.sechead` 化（原「卡内标题过重」的保留意见随示例落地作废）；市场与行情新增 D1 式**当日盘口六格**（`latest` 行直读，收盘格证据角标）；同行估值公式由 expander 提为 D3 式 `.formula` 纸面卡（expander 只留证据输入），纳入/剔除 dataframe 换 `.tbl` 密排表（`● 纳入 / ○ 剔除`，新增 `td.st.drop`）；文档智能两处 `#####` 子标题 `.sechead` 化 | ✅ smoke 22/22 + 全量 1742 passed（2026-09-30） |
| 26 | **v4e 续：D5 会话页 + D7 报告页**（2026-09-30）。`agent_research()` 重排为**会话主栏 + 信息右栏**：右栏 = 新建对话按钮 + `.railcard` 研究上下文/会话与参数卡（原 `agent-context` 大卡与顶部按钮列退役）；用户消息改 `.bubble-user` 墨底气泡，成功轮次加 `.badge.ai`「模型生成 · 需人工复核」横幅；任务审计两个 `####` sechead 化。`report()` 改 D7 杂志式：Agent 报告与本地工件的 4 个 `st.metric` 换 `.statusbar` **护照徽章行**，正文进 `st.container(border=True)` 白卡，左侧 `.tocbox` 静态目录（解析报告自身 markdown 标题、封顶 24 条、装饰性无锚点），下载按钮收进目录栏。新增 `_railcard_html()` / `_report_toc_html()`（全 escape）。测试契约全保留：`st.chat_input[0]`、`agent_model/max_steps/temperature` 键值、上下文含 symbol+研究截止、`agent_new_conversation`/`download_agent_report_*` key | ✅ smoke 22/22 + chat UI 7/7 + 全量 1742 passed + diff-check（2026-09-30） |
| 27 | **v4e 收尾：全功能块覆盖**（2026-09-30）。8 个 Demo 页（研报纠错/ACME 约束/结论护照/主张-证据图谱/逻辑脆弱性/时间重验证/审计回放/对抗注入）：`st.subheader` 页标题换 `_shared.page_head()`（眉题「案例演示 · 离线演示材料」+ 衬线标题 + 英文 tag），22 个 `#### ①…` 裸标题换 `_shared.sechead()`（文本逐字保留，测试断言不受影响）——助手在 `_shared.py` 自包含（demo 反向 import workbench 会循环加载），CSS 复用全局类。研究任务页补「启动受控研究 / 任务记录」两个 sechead；评测与消融补「消融变体 / 案例级明细 / 评测指标摘要 / 案例结果」四个 sechead；登录/注册 popover、受控研究与采集表单由新增 `stForm` 白卡发丝线 chrome 统一。至此 14 个主导航页 + 8 个 Demo 页 + 落地页 + 账户区全部显式走 D 排版，无裸标题残留 | ✅ smoke 22/22 + diff-check（2026-09-30），全量 pytest **1742 passed / 3 deselected** + integration 2 passed / 1 skipped（2026-09-30 补录，方案 B 重构至此整体收口） |

## 4. 待完成（按优先级分三批）

### 第一批 · 研究总览页补完（P0）
1. ~~**AI 叙事卡隔离**~~ ✅ 已完成（见 §3 第 8 项）。原方案写的是「`AI 生成 · 需人工复核`」，落地改为「服务层自动生成 · 需人工复核」：该段文字来自 `research_overview_insights`（服务层确定性计算，非模型），按零猜测原则不能标成模型输出。
2. ~~**研究路径步骤条**~~ ✅ 已完成（见 §3 第 9 项）。
3. **同行估值摘要表**：✅ 位置已随重排落到「摘要｜AI 解读」之后（D 的顺序）。**未做**：目标公司行米色底高亮 —— `column_config` 不支持行级样式，要做得换 HTML 表格并丢掉 `on_select`，维持现状。
4. KPI 磁贴行：✅ 已提到页面的第二个区块（仅次 hero），D 稿里的四联磁贴位置。✅ 证据按钮已改磁贴角标（见 §3 第 21 项）。
5. ~~**总览补 D 稿的「证据台账」与「AI 分析过程」两段**~~ ✅ 已完成（见 §3 第 12 项）：都是**只读**复述（台账 `st.dataframe` 无 `on_select`、轨迹复用 `_trace_view()`），并明示截断（前 20 / 共 N 条；前 5 / 共 M 步），不与「证据与校验」「Agent 执行轨迹」两页抢交互。

### 第二批 · 各研究子页统一节奏（P1）
5. ~~**眉题统一**~~ ✅ 已完成（见 §3 第 10、14 项）：13 个页面已统一为「眉题 → 衬线标题 → 右 tag」的 `page_head()` 节奏，轨迹页与报告页的缺眉题问题一并解决。**未做**：`documents()` 页仍是 h4 级标题（已改为 h2，但与「文档智能」这类工具页的层级是否要再压一级待定）。
6. **页内区块标题 `.sechead` 化**：✅ 已换 6 处（同业参照 / 同行纳入与剔除 / 当前需要关注 / 计算依赖 / 数据检查详情 / 新公司数据采集）。**未做**：每日复盘卡片内标题（`#### 指数收盘` 等，属卡内小标题而非页级区块，换 `.sechead` 反而过重）与文档智能的 `#####` 子标题。
6. **证据台账密排化**（`evidence()` 页）：ID 列等宽、来源/期间/截止列、状态徽章列；与 D 图台账一致。
   → ✅ 已完成（见 §3 第 22 项）：整表换 D 式密排 HTML 表（ID 列等宽 + 状态徽章列 + 截止列）；行选择改「选择证据查看详情」选择器，详情面板入口与跨页 `selected_evidence_id` 语义保留。
7. ~~**Agent 执行轨迹**（`trace()`）~~ ✅ 已完成（见 §3 第 10 项）。**注意：原条目指向 `_trace_legacy`，但该函数全仓无调用点（死代码）**；真正落地的是实时事件流与 fallback 两条路径。
8. ~~**图卡硬编码色 token 化**~~ ✅ 已完成（见 §3 第 10 项）。
9. **财务分析页**指标表与"计算过程"：✅ 区块标题已 `.sechead` 化（见 §3 第 23 项）；指标表本体是 `st.dataframe`（canvas 渲染，CSS 无法逐列改字体，见第二批第 11 条注），等宽数字受此约束维持现状。

### 第三批 · 任务中心与落地页（P2）
10. ~~**每日复盘**~~ ✅ 部分完成（见 §3 第 11 项）：指数磁贴已换衬线大数字 + tabular。**未做**：把整个广度块改成 Dub 式统计表（现在是 `st.container(border=True)` + markdown 卡片，改结构收益有限）。
11. **评测与消融**：结果表密排化 —— **无需改动**：本就是 `st.dataframe`，已由 `.stDataFrame` 全局发丝线 + `tabular-nums` 覆盖（`st.dataframe` 走 canvas 渲染，CSS 无法逐列改字体）。
12. ~~**落地页**~~ ✅ 完成（见 §3 第 11、24 项）：`home-wordmark` 已改衬线；价值药丸已对齐 D 图 meta 行（点分隔弱化文字行，去药丸底）。
13. **侧栏**：分组标题已小号大写；快照选择区已卡片化。**「研究快照」选择器保持 `st.selectbox` 不换成胶囊组** —— 该控件是 AppTest/深链接的兼容面（与 §5「不动 `st.radio` 隐藏侧栏结构」同源风险），只做样式、不换控件。
14. **D 稿里拿不到的两块**（不做，理由是数据约束而非取舍）：hero 的「▲ +1.24% 涨跌 chip」需要当日涨跌幅、`donut` 同业市值占比需要派生比值 —— 快照服务层都不提供，按 §5 第一条不得在前端自行计算，故空置。

## 5. 明确不做（边界）

- 不在前端计算任何派生数字（涨跌幅、均值、分位一律服务层出，前端只渲染）。
- 不动 `NAVIGATION` 扁平列表契约（测试与深链接依赖）。
- 不动 `st.radio` 隐藏侧栏结构（自动化兼容）。
- 不引入前端框架/组件库（保持纯 Streamlit + CSS 注入）。
- 不动证据链展示义务：证据 ID、来源、期间、截止、币种/单位、校验状态、免责声明一个不能少。

## 6. 风险与回滚

- **Streamlit 升级风险**：CSS 依赖 1.61 真实类名（`.stSelectbox`、`[data-testid="stSelectboxVirtualDropdown"]` 等），升级前需重查（历史教训已写注释）。
- **回滚**：全部改动集中在 `workbench.py` + `config.toml`，`git checkout -- workbench.py .streamlit/config.toml` 即整体回滚；`design_mockups/` 为未跟踪参考件，可整目录删除。

## 7. 验证清单（每批完成后）

```
TMPDIR='C:\Users\LENOVO\AppData\Local\TempFT' \
  python -m pytest -q -m "not integration" -p no:cacheprovider   # 1742 passed / 3 deselected
python -m pytest -q -m integration -p no:cacheprovider           # 2 passed / 1 skipped（无 TUSHARE_TOKEN）
python scripts/smoke_workbench.py                                # 22/22 clean
git diff --check
```
（本机 pytest 临时目录 ACL 损坏，必须用 `TMPDIR` 绕过，**不要**用 `--basetemp`。）
人工走查：14 个主导航页 + 8 个 demo 页，重点核对深色文字对比度与证据徽章可读性。
