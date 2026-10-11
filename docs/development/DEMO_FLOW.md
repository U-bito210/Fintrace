# DEMO_FLOW.md — 5 分钟比赛 Demo 主故事

> 版本：v3.2 编码前冻结版 ｜ 2026-09-16
> 本文是 FinTrace-CN 初赛视频与决赛现场的**唯一**演示剧本。CARD-15 的每个页面、CARD-11 的每项指标、CARD-12 的 Evidence Pack，最终都以"能否支撑本剧本"为验收依据。
> v3.2 变更：创新命名冻结（ACME / Financial Claim Passport）；步骤 7b 增加 **VERIFY CLAIM**（脱离 LLM 重新执行证明）；双赛叙事见 DUAL_COMPETITION_POSITIONING.md（同流程切换话术）。

---

## 0. 演示总原则：演示即证据

1. **只用真实材料**：演示输入必须是真实 A 股年报 PDF + 真实研报草稿（人为注入错误），不得用 synthetic fixtures 演示。
2. **不美化失败**：blocked / failed / warning 状态原样展示。Demo 要证明的不是"系统永远正确"，而是——**"错误的时候，系统知道自己不能下结论"**。
3. **每个数字可回跳**：演示中出现的每一个重要数字，都能点击跳回原始 PDF 页码/表格位置。
4. **不预演 LLM 输出**：解析、提取、计算、核查全部现场执行；LLM 叙事部分允许预先缓存，但 Validator 结果必须现场产生。
5. **离线可复现**：演示环境不依赖外网（数据 snapshot + 本地文档），网络故障不影响剧本走完。
6. **创新来自痛点**：每个创新展示必须对应一个真实金融痛点，不堆砌技术名词。

---

## 1. 演示资产准备（Demo 前置条件）

| 资产 | 要求 | 治理 |
|---|---|---|
| `data/demo/annual_report.pdf` | 一家真实 A 股上市公司年度报告（建议制造业，避免金融行业触发 ValuationApplicabilityGate 降级，除非刻意展示） | 存 `data/`（gitignore），登记 sha256 + 下载来源 + 下载日期；**必须文本型 PDF**（扫描版 OCR 不可用→Demo 降级，预检） |
| `data/demo/report_draft.md` | 同一公司的"研报草稿"，**人为注入至少 8 处错误**（见 §2 错误清单） | 自建内容可入 Git；引用的年报数字必须与 PDF 一致 |
| snapshot | 演示公司对应的行情/股本 snapshot | 入 `tests/fixtures/` 或 `data/`，run manifest 记录 snapshot_id |
| 备份 run | 提前一天完成的完整彩排 run（含全部产物） | 本地保存 Evidence Pack zip + 录屏视频，**现场故障时切换展示** |

**含错研报草稿的 8 处注入清单**（编写草稿时逐项登记，彩排时逐项核对系统是否检出）：

| # | 错误类型 | 示例设计 | 预期系统行为 |
|---|---|---|---|
| E1 | numeric_error | 营业收入写 352,012,554 千元（年报为 362,012,554 千元） | checker 定位 + 给出正确值 + 跳原文页 |
| E2 | numeric_scale_error（设计名；checker 实际为 numeric_error） | 归母净利润写 31,000,000 亿元（年报为 50,744,682 千元，量级错误） | checker 报错 + 跳原文页对比 |
| E3 | calculation_error | 2024 年营业收入同比写成增长 15%（年报 2024/2023 年复算约下降 9.70%） | 展示两期原值、证据页和同比复算过程 |
| E4 | valuation_multiple_error | 目标 PE 用了银行业可比值 | §7.1 限界含「**行业错配**」，分支已实现（声明来源行业 + 草稿唯一目标代码 + 冻结申万映射，三者齐备且行业不同才判） |
| E5 | wrong_citation（设计名；checker 实际为 wrong_page） | 营业收入引用"年报第 45 页"（对应页由证据定位） | wrong_page 检出 + 跳正确页 |
| E6 | period_error | 把 2023 年研发费用约 183.56 亿元写成 2024 年数值 | 核对 2024/2023 两列及原文页，给出本期值 |
| E7 | unsupported_causal_claim | "利润增长主要因海外订单放量"（年报无此表述，证据不足） | 标记 unsupported，要求 evidence 绑定 |
| E8 | scope_error | 母公司口径数据当成合并口径使用 | scope checker 报错 |

> **2026-09-26 真实材料彩排（当前）**：逐条对齐 **8/8** —— **E1–E8 全部检出**，§5「8/8 检出方可上台」**已达标**，步骤 6+7 由「会卡」转为「可讲」。E3 已改为「2024 年营业收入同比写成增长 15%，年报 2024/2023 两期复算约下降 9.70%」，现在**真的产生 `calculation_error`**（旧稿的 166,766,834 千元不在这份年报中，已废弃）。E6 的期间错误给出两期证据及本期值；参考年报缺可确认披露日时 `stale_citation` 明确不运行，导入时间不能代替披露时间。首次构建 **37–40s**（步骤窗口 50s）——**这一次已由侧栏「演示预热」从现场路径上移除**（见 §5），预热后同一进程内 `corrections()` 实测 **0.00s**。但 37–40s 是**系统文件缓存已热**的数；在没读过这份 PDF 的机器上首建约 **85s**，会直接超窗 —— 所以「现场不用等」只在预热确实执行过时才成立，预热不是可选的美化项。
>
> **E4 是怎么补上的 —— 实现的是 §7.1 明文，不是放宽判据**：CARD-05 §7.1 对该类型的限界原文是「仅检『引用的可比倍数与事实不符』**和『行业错配』**」，原实现只做了前半句（`_multiple_findings` 只判可由「价 × 股本 ÷ 归母净利」复算的倍数）。现新增 `_industry_multiple_findings` 作为**同一个 `check_type` 的第二条独立依据**，三个条件同时成立才判：① 句子**自己声明**了倍数取自哪个行业，且只声明一个（行业词取自 `L1_INDUSTRIES` 的 31 个申万一级名 + 可选的「业/行业」后缀，且必须**紧邻**倍数表达式，随口提一句行业不算）；② 草稿**唯一**给出目标公司代码（只认带交易所后缀的写法，如 `300750.SZ`；裸六位数一律不当股票代码）；③ 声明行业与目标公司申万一级行业**不同**。任一条件不满足即**静默** —— 这样才不会把「分析师目标倍数」当成事实来判错。
>
> 因此 E4 的检出**不是**靠匹配条件放宽得来的：命中它的那条检出 `original` 就是草稿原句里的「银行业 6 倍 PE」，`expected` 是「电力设备可比公司的目标倍数」，`detail` 里写明 `L1_04` vs `L1_11`。E4 **按设计没有 evidence ID**（依据是冻结的行业映射与草稿措辞，不是事实库披露），页面只能靠 `claim_page` 回跳草稿 —— 这是有意的，不补假 ID。
>
> ⚠️ 另注：`rehearse_demo.py` 报的「E1–E8 类型覆盖 命中 6/8」**不是漏检**，而是剧本 `PLANTED_ERROR_PLAN` 里 E2/E5 写的仍是**旧设计名**（`numeric_scale_error`/`wrong_citation`），而 `demo_service.py` 现值是 `numeric_error`/`wrong_page`。**不得**把清单类型名改成实现名来"凑"这个分数 —— 那正是"为了凑 8/8 改数据"。判据只有一个：`rehearse_demo.py --step 6+7` 的「逐条对齐」行。
>
> **E1–E8 逐条核查（真值 / 检出 / 证据 / 页码）**，2026-09-26 实测：
>
> | 植入 | 实现类型 | 检出 | 事实库证据 | 证据所在年报页 |
> |---|---|---|---|---|
> | E1 | numeric_error | 1 条 | 1 条 | **p9**（营业收入，合并口径） |
> | E2 | numeric_error | 1 条 | 1 条 | **p9**（归母净利润） |
> | E3 | calculation_error | 1 条 | 2 条 | **p9 + p119**（2024FY 与 2023FY 营业收入） |
> | E4 | valuation_multiple_error | 1 条 | 0 条（按设计） | 无；回跳草稿页（`claim_page`） |
> | E5 | wrong_page | 1 条 | 3 条 | **p9**（草稿误引"第 45 页"，正确页为 9） |
> | E6 | period_error | 1 条 | 2 条 | **p119**（2024FY 与 2023FY 研发费用） |
> | E7 | unsupported_causal_claim | 1 条 | 0 条（按设计） | 无；回跳草稿页（`claim_page`） |
> | E8 | scope_error | 1 条 | 2 条 | **p121 母公司 + p119 合并** |
>
> 八处**全部有正文页可回跳**：六处落在年报真实页，E4/E7 两处按设计无事实库证据（E4 依据冻结行业映射，E7 是"证据不足"本身），回跳落草稿页 —— 现场被问到时照实说。
>
> **E2 的类型核对（"量级错误"≠ 单位错误）**：剧本把 E2 记作 `numeric_scale_error`，checker 实际报 **`numeric_error`，这是对的**。CARD-05 §7.2 明确"单位纪律先于数值判断"：`unit_error` **只在数字与披露完全一致、仅单位声明错**时触发（`MatchKind.UNIT`，建议文案是"不要改动数字"）。E2 是 `31,000,000 亿元`（≈3.1×10¹³ 元）对披露的 `50,744,682 千元`（≈5.07×10¹⁰ 元），**数字和单位都不对，且相差约 611 倍 —— 不是元/千元/万元/百万元/亿元的整档**，所以既不是 `unit_error` 也不是"整档量级错"。判成 `numeric_error` 才是正确口径；把它算作 `unit_error` 反而会把"抄错数"说成"选错单位"。本材料 `by_check_type` 里 `unit_error = 0`，与这条判断一致。

> **历史设计说明**：E2 曾设计为"3.1 亿元写成 31,000 万元"，但两者数值相等，不构成错误。当前真实材料采用上述量级错误；FinFuzz 的 `numeric_scale` 是扰动类型，不等于 checker 的 `check_type`。

### 1b. 清单外 8 条检出的逐条判定（2026-10-08 实测）

> 纠错页的「清单外检出」一格写着「须逐条判定真伪」（`workbench/demo/corrections.py:70`），但此前**没有人判定过**。现补齐。
> 数据来源：`demo_service.corrections()` 对真实材料的一次完整跑出 —— **16 条检出 = 8 条植入 + 8 条连带真检出，误报 0 条**。

| # | 检出 | 落点（草稿行号） | 判定 | 依据 |
|---|---|---|---|---|
| 1 | `missing_citation` `352,012,554千元` | L5（E1 同句） | 真 | 全文 33 行只有 L21 带「（来源：…）」，其余带数字句子一律无出处 |
| 2 | `missing_citation` `31,000,000亿元` | L7（E2 同句） | 真 | 同上 |
| 3 | `missing_citation` `11.58元` | L9 EPS | 真 | 同上；另见下方边界 |
| 4 | `missing_citation` `+15.01%` | L9 归母净利同比 | 真 | 同上 |
| 5 | `missing_citation` `+15.00%` | L13（E3 同句） | 真 | 同上 |
| 6 | `missing_citation` `183.56亿元` | L23（E6 同句） | 真 | 同上 |
| 7 | `missing_citation` `191,320,942千元，-14.00%` | L31（E8 同句） | 真 | 同上 |
| 8 | **`calculation_error` `-14.00%` → 应为 `-9.70%`** | L31（E8 同句） | 真 | 该句**同时**犯两个独立缺陷：口径错（母公司数当合并数）+ 同比算错。两条检出并存、互不遮蔽，正是 CARD-05 §10「一处的两个缺陷任一都不许关掉另一个」所要求的 |

> **一个要照实说的边界**：第 3 条 `11.58 元`（基本每股收益）的检出 `metric=None` —— 事实库里没有可绑定的 EPS 指标，所以系统**只判了「没给出处」，没有判「数值对不对」**。这不是漏检（无源可核时无从判数值），但现场若被追问"EPS 对不对"，答案是"本材料不校验 EPS 数值，只要求它给出出处"。
>
> **已查实的原因**（2026-10-08 跑 `corrections()` 统计 24,715 条事实）：EPS 在归一化字典里**没有登记**，年报里的「基本每股收益」5 条与「稀释每股收益」5 条全部落在 `metric_unknown_基本每股收益` / `metric_unknown_稀释每股收益`。同类的还有「股本」2 条、「股份总数」4 条 —— `normalization/metrics.py:146` 的别名表写的是「总股本/股本总数/期末股本」，而宁德时代这份年报的股份变动表印的是「股本」「股份总数」，**逐字不匹配所以没映射上**。这意味着：①EPS 类声明目前一律不校验数值；②若要走 §1 的市值方案，股本别名得先补。

> **话术（步骤 6+7 收尾，约 10 秒）**：「植入的 8 处全部检出。系统另外还报了 8 处——这 8 处**不是误报**，是这份草稿真实的毛病：全文只有一句话标了来源，其余带数字的句子一律没有出处；而第 6 节那一句话同时犯了口径错和同比算错两个毛病，系统分开报了两条。**误报 0 条。**」

> **诚实性限定**：草稿由本团队自建，植入项与未引用句子的真值**由构造已知**，所以「误报 0」是**在这份受控材料上**的结论，不等于在任意真实研报语料上的误报率——后者归 CARD-11 的 Real / Holdout 集，材料尚未采集。答辩时不得把前者说成后者。

---

## 2. 5 分钟主故事（12 步，含创新展示）

> 总时长 300 秒。列"评委看到什么"与"话术要点"。所有页面见 CARD-15 页面清单。
> v3.2 新增：步骤 4b（ACME 创新展示）、步骤 7b（Financial Claim Passport + VERIFY CLAIM）、步骤 10b（Thesis Fragility 创新展示）。

### 延迟预案（必彩排实测，解决 H7 点）

| 步骤 | 真实耗时 | 展示策略 | 话术 |
|---|---|---|---|
| ①② 上传 | ≈30s | **预 ingest 缓存命中**（DocumentRegistry 按 sha256 秒级返回已注册文档） | "这个 PDF 我昨天已经上传过，Registry 记录了 sha256 和解析结果——同输入同配置不会重解析，这就是可复现性的第一步。" |
| ③④ 解析+提取 | 200页PDF≈1–3min / 长草稿LLM≈30–90s | **快进展示缓存**（Events 页展示缓存 run 的事件流；若必须现场跑 LLM，只展示草稿提取） | "解析用 pdfplumber 定位，每个事实带 page/bbox——今天这份文档我昨天已经完整 ingest 过，秒级出结果。" |
| ④b ACME | ≈5s | 实时展示约束违反检测 | "系统不只提取数字，还用会计约束自检——比如资产≠负债+权益，立即报警。" |
| ⑤ 财务分析 | ≈10s | 实时 | 快 |
| ⑥⑦ 研报纠错 | 预热后 **0.00s**；首次解析 229 页年报 **37–42s**（冷机 ≈85s） | **侧栏「演示预热」先建好**，现场只演确定性核查与逐条跳转 | "LLM 只做一件事：把自然语言句子结构化成 Claim。之后全部由确定性 Checker 判断。" |
| ⑦b Passport | ≈10s | 实时展示护照 + VERIFY CLAIM | "每个关键 Claim 生成一张 Financial Claim Passport——点击 VERIFY CLAIM，系统脱离 LLM 重新执行证明。" |
| ⑧ 估值 | ≈10s | 实时 | 快 |
| ⑨⑩ 备忘录 | ≈30–60s LLM / ≈5s 模板 | 缓存命中；或展示预生成版本 | "备忘录 11 段，每一句都有三级标注。" |
| ⑩b Fragility | ≈10s | 实时展示脆弱性分析 | "系统还能分析投资逻辑的脆弱节点——哪些假设一旦不成立，整个结论就翻转。" |
| ⑪ 删证据阻断 | ≈15s | 实时 | 快 |

**彩排验收**：全剧本（含预 ingest 命中）必须实测 < 5 分钟（含话术）。任何超 5 分钟的步骤必须精简——**可以剪掉⑧估值环节（已在纠错环节展示了 CARD-02/03），保留①②③④④b⑤⑥⑦⑦b⑨⑩⑩b⑪**，把估值挪到答辩追问环节。

**逐段耗时实测（2026-09-26，本机，真实材料 DEMO-CATL-2024）**——「录下逐步耗时」的落点：

| 动作 | 实测 | 说明 |
|---|---|---|
| 预热（解析 229 页年报 → 归一化 → 跑 13 类核查） | **37.4 / 37.8 / 39.3 / 40.0 / 41.3 / 41.5s** | 六次独立测量。`prewarm()` 与 `corrections()` 走同一条构建路径 |
| 同一次预热在**冷机**上（系统文件缓存未热、首扫） | **≈83–85s** | 会**超窗**。这是"预热不可省"的唯一理由 |
| 预热后 `corrections()`（步骤 6+7 现场那一次） | **0.00s** | 页面的 `build_seconds` 仍如实报首次的 37–42s |
| 同进程复跑（不预热，靠进程内 `_DRAFT_STATES`） | **1.18–1.22s** | 省下 84.20s 中的绝大部分 |
| 注册表落点：系统临时目录 vs 项目内 `output/` | **40.04s vs 41.29s** | **无差异**（噪声内）。"挪到系统临时目录更快"是伪优化，已实测排除 |
| 证据 `jump`（只回答"在第几页"） | **0.12–0.15ms** | sqlite 点查 |
| 证据 `render_page`（把那一页栅格化成 PNG，**观众真正感知的那一步**） | **100–300ms**（首张 300ms，稳定后 ≈100–115ms；载荷 27–36KB） | 别把它和 `jump` 混成一个数 |
| 失败注入 `adversarial_view()` 整页 / `run_injection(1)` 单场景 | **0.49s / 0.23s** | 实时演得起 |

> **跳转的两个数字不是同一件事**：`jump` 给页码（0.1ms 级），`render_page` 给图（0.1s 级）。现场观众感知的是后者，所以"跳转很快"这句话只在你说的是页码时成立。

### 开场（0:00–0:25）｜Upload Task 页

**步骤 1+2**：上传真实年报 PDF；上传含错研报草稿。

- 话术："FinTrace-CN 是面向 A 股投研的**可验证金融智能体**。今天演示两个输入：一份真实年报，一份故意写了 8 处错误的研报草稿。"
- 评委看到：DocumentRegistry 登记 document_id / sha256 / parser 版本（上传即入账，执行轨迹页可查）。

### 解析与提取（0:25–0:55）｜Execution Trace 页（快进展示）

**步骤 3+4**：系统解析 PDF 与财务表格，自动提取收入、利润、现金流指标。

- 话术："解析用 pdfplumber 定位到页和表格坐标，每个事实记录 page/bbox/单位/期间/口径，不是一段无来源的文本。"
- 评委看到：事件流按时间顺序滚出 parse → extract → normalize，每条事件可展开 input/output。

### ★创新展示一：ACME（会计约束驱动的多模态证据理解）（0:55–1:00）｜ACME Constraint 页

**步骤 4b**：展示 ACME 六类约束族检测结果。

- 话术（创新亮点 5 秒）："传统 OCR 提取完就结束了。FinTrace 多做一步——**用会计约束自检解析质量**：资产=负债+权益、YTD 与单季可推导、跨期数据连续、正文和表格矛盾能发现。任何'不可能成立'的解析结果，系统立即定位报警，而不是默默信任。"
- 现场操作：点击一个约束违反项 → 展示违反详情（如"正文 vs 表格"毛利变动区间不一致，或 Assets vs Liabilities+Equity 差额）→ 跳转原始 PDF 页。
- 评委看到：约束违反列表（含 constraint_id / inputs / residual / 容差）+ 违反详情 + PDF 定位。
- **对应创新**：ACME（Accounting-Constrained Multimodal Evidence Engine）/ CARD-22。

### 财务分析（1:00–1:35）｜Financial Analysis 页

**步骤 5**：计算 YoY、**单季度 QoQ**、盈利质量指标。

- 话术（核心金融专业性 40 秒）："A 股利润表披露的是 YTD 累计值。Q1 100、H1 230、Q3 累计 390、全年 540——系统先做确定性单季换算得到 100/130/160/150，再算 QoQ。直接对累计值算环比是常见错误。"
- 评委看到：指标表可展开公式与 Evidence；若有扣非/口径问题，可比性横幅（CARD-04）原样显示。

### 研报纠错（1:35–2:25）｜Report Corrections 页 ★高潮一

**步骤 6+7**：检测草稿中的错误数字、错误单位、错误同比、错误估值倍数、错误引用、无证据因果推断；点击错误跳转原始 PDF 页。

- 话术："LLM 只做一件事：把自然语言句子结构化成 Claim。之后全部由确定性 Checker 判断对错——错误类型、原始值、正确值、证据、建议修改。"
- 现场操作：逐条点开 2–3 处错误（优先 E1 数字错、E2 量级错、E3 同比复算错、E7 无证据因果），**点击跳转 PDF 第 N 页**。
- 评委看到：8 处错误全部检出，每处给出 Evidence ID 与修改建议。**例外的只有 E4**：它的依据是冻结的申万行业映射与草稿自身措辞，不引用事实库披露，所以按设计**没有 Evidence ID**，回跳落在草稿页（`claim_page`）而不是年报页 —— 被问到时照实说，不要含糊成"证据也在年报里"。
- **页面上还会显示 8 条「清单外检出」，别当场卡住**：它们是 `7 × missing_citation + 1 × calculation_error`，已逐条判定为**真检出，误报 0 条**，收尾话术与判定依据见 §1b。若被追问"多报的算不算误报"，答："这份草稿全文只有一句话标了来源，另外 8 处是它真实的毛病，不是系统编的。"

### ★创新展示二：Financial Claim Passport（金融AI结论护照）（2:25–2:35）｜Claim Passport 页 ★核心动作

**步骤 7b**：展示一个关键 Claim 的 Financial Claim Passport（Financial Proof Object），并执行 **VERIFY CLAIM**。

- 话术（创新亮点 10 秒）："普通 RAG 只能告诉你'这句话来自第几页'——无法回答数字对不对、依赖还有效吗。FinTrace 把关键结论做成**金融AI结论护照（Financial Claim Passport）**：完整证明链（Source / Accounting Context / Calculation / Assumption / Dependency / Validation / Integrity）。现在点击 VERIFY CLAIM——**系统脱离 LLM，重新执行整套证明**：重查文档哈希、重算公式、检查依赖状态，输出五态结果。"
- 现场操作：点击一个关键 Claim → 展示 Passport 全貌 → 点击 **VERIFY CLAIM** → 展示验证结果（VERIFIED 状态 + 逐检查项明细；可预先准备一个"更正公告进入后变 STALE"的对照）。
- 评委看到：Passport 详情（六个 proof）+ VERIFY CLAIM 执行中 → 五态结果（VERIFIED / DEGRADED / STALE / BLOCKED / CONFLICTED）+ 原因列表。
- **对应创新**：Financial Claim Passport / FPO / CARD-23。这是 Demo 的核心技术动作：**点击结论 → Verify Claim → 系统重新执行证明**。

### 估值（2:35–3:00）｜Valuation 页

**步骤 8**：生成相对估值与敏感性分析。

- 话术："Bear/Base/Bull 三档假设全部进 Assumption Registry，EPS×PE 敏感性矩阵每个格子可追溯到数据→公式→假设→输出。DCF 区间不做交并集拼凑，分别展示并报告方法间 dispersion。"
- 评委看到：区间条 + 矩阵 + 假设展开（含来源与理由）。

### 投资备忘录（3:00–3:35）｜Investment Memo 页 ★高潮二

**步骤 9+10**：生成 Investment Memo；点击任一结论展示 Claim→Calculation→Evidence→PDF Page。

- 话术："备忘录每一句话都有三级标注：fact 绑定证据，inference 标注依赖哪些 fact，opinion 标注假设来源。系统不把推论冒充事实。"
- 现场操作：点击"盈利质量下降"结论 → 图谱逐层展开：Claim C18 → CFO/NI 计算 CAL12 → Evidence E51/E37 → 年报 P118/P96（与 CARD-09 示例一致）。
- 评委看到：Claim-Evidence Graph 页的结论回溯链。

### ★创新展示三：Thesis Fragility（投资逻辑脆弱性）（3:35–3:45）｜Thesis Fragility 页

**步骤 10b**：展示 Thesis Fragility Map。

- 话术（创新亮点 10 秒）："FinTrace 还能回答一个传统研报无法回答的问题——**你的投资逻辑最依赖什么？最少破坏哪些节点会让它失效？** 系统自动识别关键 REQUIRED 依赖、单点依赖、脆弱假设，并生成 Monitoring Plan：当某个 KPI 触发阈值时，自动标记受影响的 Claim 需要重验证。"
- 现场操作：展示 Fragility Map → 点击一个关键节点 → 展示 Monitoring Plan（dependency / observable KPI / trigger condition / affected claims / revalidation action）。
- 评委看到：Fragility Map 可视化 + Monitoring Plan 详情。
- **对应创新**：Thesis Fragility / CARD-24。

### 终极证据（3:45–4:15）｜Validator 页 ★高潮三

**步骤 11（剧本终点）**：人为删除一个关键 Evidence → 系统 Validator 阻断对应结论。

- 话术："最后 30 秒是 FinTrace 的核心承诺。我现在删掉支撑'盈利质量下降'的关键证据——Validator 立即把该 Claim 置为 invalid，备忘录中对应结论被阻断而不是悄悄保留。**错误的时候知道自己不能下结论，这才是可验证。**"

### 收尾（4:15–5:00）｜Audit Replay + Export 页

- 话术："整个 run 的 manifest 记录了模型、prompt hash、parser 版本、git commit、snapshot——同一输入同一配置可重放。最后导出 Evidence Pack：报告、claims、evidence、calculations、events 全量打包，评委可离线复核。这是文档级 run，所以包里没有估值表——估值要 symbol 级行情，这条 run 的输入是一份年报；包自己的 `PACK_STATUS.json` 会把'缺 valuation.xlsx'和为什么缺一并写出来，而不是补一个空表凑满 13 件。"
- 现场操作：Audit Replay 选当前 run_id 展示时间线 → 点击 Export Evidence Pack 导出 zip → **停在 ③「Prompt 注册表」**（CARD-14）。
- 兜底问法（若评委追问"证据包是不是只有 4 个文件"）：现在是**必装件 + 完整性件共 10 类里进了 9 类**，zip 内 16 KB 量级，只有估值表按口径不在；改口径要换一条 **symbol 级** run（CARD-08），不是导出器补件。
- ③ 这一节回答"prompt 凭什么叫可复现"：11 段提示词各带 `name@version` 与 `sha256`，5 条流程各自的**组合哈希**就是写进 manifest 的那个值；`lock 同步 = 是` 表示没有任何已发布的提示词被就地改过（同版本只增不改）。**没有 run 时这一节照样渲染** —— 注册表能不能审计，和 run 目录里有没有东西无关。若 manifest 那一格显示"未调用 LLM prompt"，那是真话不是缺件：这个演示 run 只做解析/归一化/计算/校验，一个字都没发给模型。

---

## 3. 失败注入场景（决赛现场 / 答辩加分环节）

> 初赛视频结尾可放 1–2 个；决赛现场作为"评委挑战环节"主动邀请评委选题。目标：证明系统在异常输入下 fail-closed，而非静默出错。剧本见 CARD-12 §4。

| # | 注入操作 | 预期行为 |
|---|---|---|
| F1 | 删除总股本数据 | 阻断每股估值（block，不猜股本） |
| F2 | 删除 PDF 某页 | citation incomplete 警告，受影响 Claim 降级 |
| F3 | 单位从万元改成亿元 | unit checker 报错，计算中止 |
| F4 | 研报给出错误同比 | report checker 定位到句 + 给出正确值 |
| F5 | 旧财报 + 更正公告同时在场 | Source Conflict Resolver 展示冲突记录，不静默覆盖 |
| F6 | 删除某关键 Evidence | Claim invalid，Validator 阻断（即主故事步骤 11） |
| F7 | 改写为无证据因果结论 | 标记 unsupported causal inference，要求绑定证据 |
| F8 | FinFuzz 注入量级错误 | numeric_scale checker 报错 + 跳原文页（对应 E2） |
| F9 | 导入更正公告 | Temporal Revalidation 显示受影响 Fact/Calculation/Claim/Valuation/Memo 段落 + 增量重算 impact |

**挑战环节话术**："各位评委可以任选一个场景，或者现场给我一个更刁难的输入——如果系统不知道自己错了还硬给结论，那才是失败。"

---

## 4. 创新展示总结

| 创新 | 展示步骤 | 对应 CARD | 痛点 | 技术机制 |
|---|---|---|---|---|
| ACME（会计约束驱动的多模态证据理解） | 步骤 4b | CARD-22 | 传统 OCR 无法发现"不可能成立"的解析结果 | 六类约束族验证 + 异常定位 + 人工复核队列 |
| Financial Claim Passport（金融AI结论护照） | 步骤 7b（VERIFY CLAIM） | CARD-23 | 传统研报结论无法独立验证 | 完整证明链 + verify_claim() 五态纯确定性验证 |
| Thesis Fragility（投资逻辑脆弱性） | 步骤 10b | CARD-24 | 传统研报无法回答"投资逻辑最依赖什么" | critical dependency + minimal cut set + Monitoring Plan |
| Temporal Revalidation（时间重验证） | 失败注入 F9 | CARD-25 | 新信息进入后旧结论如何失效 | version lineage + 三态区分 + 增量重算 |
| FinFuzz（金融语义对抗攻击器） | E2 错误检出 | CARD-26（集成 11） | 传统测试无法系统性检测金融语义错误 | 10 类 mutation + per-error-type P/R/F1 |

**创新声明边界**：
- ✅ "将会计一致性约束前移到财务文档解析质量控制环节"
- ✅ "将关键金融 Claim 封装为可独立验证的 Financial Proof Object"
- ✅ "在 Claim-Evidence Graph 上进一步分析投资逻辑的脆弱节点"
- ✅ "使用金融语义 mutation 对系统进行持续对抗评测"
- ❌ 禁止使用"全球首创 / 国内首创 / 唯一 / 第一"

---

## 5. 现场保障清单

- [ ] 演示机离线冒烟通过（snapshot + 本地文档 + 缓存 LLM 叙事路径）
- [ ] 备份：昨日彩排 run 的 Evidence Pack zip + 录屏视频（极端故障时播放）
- [ ] `python scripts/smoke_workbench.py` 演示前当天运行通过
- [ ] `python scripts/rehearse_demo.py --strict` 演示前当天运行：无「会卡」步骤方可上台
      （逐条走本剧本 13 步，输出「可讲 / 可讲需声明 / 会卡 / 待人工」；
      **「待人工」= 脚本查不了 ≠ 通过**，那几步仍需人工走查。见 CARD-15 §16）
- [ ] 含错草稿 8 处错误检出率彩排核对（8/8 检出方可上台）
- [ ] **已预热真实材料**：打开工作台后点一次侧栏「演示预热」（或改用
      `FINTRACE_DEMO_PREWARM=1 streamlit run workbench.py` 启动自动预热）。
      步骤 6+7 首建 37–42s、冷机 ≈85s，都超掉剧本给的 50s 窗口；预热后 `corrections()`
      实测 0.00s。预热只写**本进程内存**，不落仓库、不改任何产物
- [ ] 确认预热在**即将用于演示的那个进程**里完成 —— 预热的是进程内存，另起一个脚本
      解析一遍对正在服务的进程毫无帮助（这也是它没有做成命令行脚本的原因）
- [ ] 浏览器控制台零报错（CARD-15 验收硬项）
- [ ] PDF 页码跳转逐条核对（E1/E2/E3/E5/E7 五处必点）
- [ ] 四个创新展示步骤（4b/7b/10b/E2）彩排流畅

**断网演示预案**（专项7 场景）：现场网络故障时切换——①展示"离线模式"徽章（manifest 的 `execution_mode="offline"`）；②所有确定性环节（解析/归一化/分析/核查/约束验证/FPO验证/脆弱性分析）走本地缓存与预 ingest 结果；③LLM 环节（提取/叙述段）若不可用则展示预生成版本并显式标注"cached offline run"；④话术："FinTrace 的确定性层全部本地可跑；LLM 环节断网时用预缓存——评审环境装好后可以从零 ingest 一份新 PDF 体验完整链路。"

## 6. 答辩高频问题预演

| 评委问题 | 回答要点 |
|---|---|
| 和普通 RAG 投研助手区别？ | chunk≠evidence；LLM 不做财务算术；fact/inference/opinion 三级区分；fail-closed Validator；ACME 六类约束自检；Claim Passport 完整证明链 + VERIFY CLAIM |
| 数字错了怎么办？ | 现场演示 F6：删证据→阻断。错误自知是设计目标不是缺陷。 |
| 创新点在哪？ | 四大创新：ACME（多模态证据理解 + 约束推理）/ Financial Claim Passport（金融AI结论护照）/ Thesis Fragility + Temporal Revalidation（投资逻辑可证伪、信息变化可重验证）/ FinFuzz（金融语义对抗评测）。每个创新解决一个真实金融痛点，不是技术名词堆砌。 |
| VERIFY CLAIM 真的不经过 LLM 吗？ | 是的。verify_claim() 是纯确定性：重查文档哈希、重算公式、检查依赖状态；LLM 不参与验证路径（CARD-23 有代码断言测试）。 |
| 为什么不用多 Agent？ | 主链 Planner→Tools→Evidence→Validator→Memo 已闭环；Multi-Agent 是 P2 且必须过消融四问（CARD-18）。 |
| 结果可复现吗？ | Run Manifest 17 字段 + Audit Replay + Evidence Pack 离线重放。 |
| 数据准确性怎么保证？ | benchmark 三级测试集 + 24 指标 + 全创新 ablation（CARD-11），阈值待 baseline 冻结，不编数字。 |
| FinFuzz 是什么？ | 金融语义对抗测试框架，10 类 mutation（数值/单位/期间/口径/版本/估值/引用/因果等），按错误类型分层输出 Precision/Recall/F1/误报率，区分提取器失败与核查器失败。 |

## 7. 验收

- [ ] 全剧本 5±0.5 分钟彩排 ≥3 次零卡点
- [ ] 初赛视频按本剧本拍摄（可剪辑加速解析过程，不得剪掉错误检出与 Validator 阻断）
- [ ] 任何对本剧本的修改必须同步更新 CARD-15 页面清单
- [ ] 四个创新展示步骤（4b/7b/10b/E2）全部可演示
- [ ] E2 示例修正：31,000 亿元 vs 3.1 亿元（量级错误，非单位错误）
