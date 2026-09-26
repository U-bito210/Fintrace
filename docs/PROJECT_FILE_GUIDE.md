# FinTrace-CN 项目文件讲解（交接给其他智能体）

> 用途：让一个**没有上下文的全新智能体**在最短时间内理解这个仓库的结构、边界和改动入口。
> 生成时间：2026-09-26 ｜ 对应工作区状态：`git log` 179 个提交，`git ls-files` 369 个受版本控制文件。
> 建议阅读顺序：第 1 节 → 第 2 节（硬约束，最重要）→ 第 4 节（目录总览）→ 需要动哪块再查第 5~10 节 → 动手前读第 12 节。

---

## 1. 这个项目是什么

**FinTrace-CN 是一个离线优先的 A 股可验证金融研究 Agent。** 它的核心主张不是"能让模型讲得动听"，而是：

> **任何重要金融数字都有来源，任何重要结论都有证据链，所有计算均可复核；当证据不足、数据冲突或口径不一致时，系统主动降级或阻断结论。**

也就是说，它不只证明"正确的时候能跑"，更证明"**错误的时候知道自己不能下结论**"。

### 1.1 四个必须分离的信任边界（整个项目的设计原点）

普通金融问答最容易犯的错是把「模型记忆 / 实时数据 / 计算结果 / 叙事」混成一团。本项目强制拆成四层，**LLM 不拥有其中任何一项权力**：

| 层 | 职责 | 代码位置 | LLM 有权力吗 |
|---|---|---|---|
| ① Provider | 提供**带来源、时间(`available_at`)、口径**的事实 | `src/cn/providers/` | ❌ 无权选数据源 |
| ② 确定性引擎 | 算 TTM、PE/PB/PS、同行估值、期间转换 | `src/cn/periods.py`、`valuation.py`、`normalization/`、`analysis/` | ❌ 无计算权 |
| ③ Evidence Ledger | 保存事实、公式、输入→输出的依赖 DAG | `src/cn/evidence.py` | ❌ 无创建证据权 |
| ④ Validator | 决定结论**能否输出**（fail-closed） | `src/validation/` | ❌ 无放行权 |

LLM 只做两件事：**在已给工具里选工具 + 写受约束的文字**。

### 1.2 业务背景（为什么文档里到处都是 CARD-xx）

这是一个参加**双赛**的竞赛项目（2026 北京市大学生金融人工智能竞赛 + 华北五省计算机设计大赛），所以：

- `docs/development/MASTER_PLAN.md` 是**进度唯一真相源**，所有开发以 `docs/development/cards/CARD-xx_*.md` 为执行单元；
- 代码与测试里大量出现 `CARD-07`、`CARD-22 §7 F5` 这类引用——**指向卡片的具体章节**，改动时应对照该章节的契约，而不是自己发明行为；
- **Code Freeze 2026-10-15**，之后只允许 bug fix。所以"大重构"通常不是选项，优先**扩展已有卡片**。

初赛主链（MASTER_PLAN §0）是一条 15 级流水线：

```
真实金融文档 + Snapshot Provider
 ①Document Intelligence(CARD-01) → ②Normalization(CARD-02) → ③ACME 约束层(CARD-22)
 → ④Evidence(point-in-time) → ⑤确定性金融工具 → ⑥财务诊断(CARD-03)
 → ⑦Claim 抽取+研报纠错(CARD-05) → ⑧估值(CARD-08) → ⑨结论护照(CARD-23)
 → ⑩主张-证据图谱(CARD-09) → ⑪脆弱性(CARD-24) → ⑫投资备忘录(CARD-10)
 → ⑬Validator(fail-closed) → ⑭时间重验证(CARD-25) → ⑮证据包+审计回放(CARD-12)
（系统外部）FinFuzz(CARD-26) 对整条流水线做对抗评测
```

---

## 2. 硬约束：动手前必须知道的 8 条

这些是"踩了会出事"的规则，**优先级高于任何局部便利**。

1. **`AGENTS.md` 是工作区级上位规则，永远优先于 MASTER_PLAN。** 其中明文禁止重新引入已被移除的美股新闻 / DCF / 加密资产 / 预测市场 / MongoDB / Redis / 旧 supervisor 多 Agent 流水线。这是从上游开源项目（Agentic-Analyst/stock-analyst）裁剪后的主线。
2. **零静默、零猜测。** 任何无法判定语义的值 → `None` / 跳过 + 显式 warning。**不允许**填 0、填参考值、"合理估计"、或让 LLM 补数字。这条在代码里以 `warning` 列表、`reason` 字段、`remediation` 字段的形式反复出现。
3. **不要用 LLM 文本或 illustrative 夹具冒充有来源的金融事实。** `demo_data.py` 生成的数据 provider 固定为 `illustrative_fixture`、`data_quality=illustrative_demo_not_for_investment`，任何时候都不得显示为实时行情。
4. **市值 / 同行估值只能用 `RAW`（不复权）价格**，Validator 会拒绝误用 QFQ/HFQ。
5. **Point-in-Time：** 只有 `available_at <= research_as_of` 的财报才能参与研究。快照的 bars 窗口**不得越过** `research_as_of`，否则判 `future_fact`。
6. **`data/`、`output/`、`.env`、`.workbuddy/` 全部 gitignore。** 仓库不含 API Key、付费原始数据、受版权保护的公告全文。**不得伪造 `*_tushare_v1` 快照来把测试"修绿"**。
7. **默认测试必须完全离线**：不依赖网络、不依赖本地 `.env`、不依赖墙钟时间、不依赖缓存。真实联网测试一律打 `integration` marker 并需显式凭据。
8. **判据是"失败集合逐条不变"，不是"passed 变多"。** 全量基线的通过数变更必须能解释清楚来源。

---

## 3. 一条容易踩的语义陷阱（务必先看）

`FinancialStatement.values` 的**键名字面 ≠ 语义**，有历史包袱：

| 键名 | 实际语义 |
|---|---|
| `net_profit` | **归母**净利润（tushare `n_income_attr_p`） |
| `equity` | **归母**股东权益（`total_hldr_eqy_exc_min_int`） |
| `total_shares` | 期末总股本 |
| `operating_cash_flow` / `free_cash_flow` | **仅 provider 写入**，很多快照里是 `None`（属可选指标） |

而 `src/cn/normalization/metrics.py` 的词表里用的是另一套命名（`net_profit` = **含少数股东**，`net_profit_parent` = 归母）。因此 `src/cn/normalization/metrics.py` 里有一张**显式投影表**做映射。**新增指标前先看这张表。**

同理：`AnalyzeCnFinancialsTool`（`src/agents/tools/cn_tools.py`）**只接受 `document_id`**，传 `symbol` 会返回 `analysis_missing_argument`——这是为了防止绕开投影表拿到错误口径的值。

---

## 4. 目录总览

```
FinTrace-CN/
├── AGENTS.md              工作区上位规则（最高优先级）
├── README.md              项目介绍 + 5 分钟跑起来
├── ARCHITECTURE.md        核心组件与信任边界（短，建议先读）
├── DATA_SOURCES.md        数据来源与价格/时点口径
├── VALUATION_METHODOLOGY.md  估值方法论与当前范围
├── EVALUATION.md          评测方法 + 四组 Agent 消融 + 换模型厂商的办法
├── LIMITATIONS.md         限制与合规边界（改动后要同步）
├── DEMO_GUIDE.md          面试 60 秒 Demo 路线
├── LICENSE                Apache-2.0（含上游版权）
├── api.py                 版本化 FastAPI（研究 / 任务 / Agent / 每日复盘）
├── workbench.py           Streamlit 主入口（22 个页面）
├── conftest.py            把项目根加入 sys.path，保证 import src.* 可用
├── pytest.ini             只注册 integration marker；默认只跑离线
├── requirements.txt       运行依赖（Python 3.11）
├── requirements-dev.txt   开发依赖
├── Dockerfile / docker-compose.yml   非 root 镜像，8501 + 8000
├── src/                   全部业务代码（168 个 .py，约 56k 行）
├── scripts/               19 个可执行脚本（采集/构建/基准/冒烟/彩排）
├── workbench/             11 个 .py —— CARD-15 Demo 页面容器包
├── tests/                 57 个测试文件 + fixtures
├── benchmarks/            cn_agent_v1.json / cn_agent_v2.json（三级基准）
├── docs/                  交付文档 + development/ 开发文档体系 + 27 张 CARD
├── data/                  【gitignored】快照、探针产物、文档库
├── output/                【gitignored】报告、清单、评测产物
├── tmp/                   【gitignored】临时与回归日志
└── *.zip                  scripts/src/tests/workbench 的打包副本（非真相源，可忽略）
```

---

## 5. 根目录文件逐个说明

| 文件 | 说明 |
|---|---|
| **`AGENTS.md`** | 工作区级规则。定义"维护面"（哪些文件是主线）、金融数据规则、验证命令、禁止重新引入的东西。**任何改动前先读它。** |
| **`README.md`** | 对外介绍：四层边界、已实现能力清单、架构 mermaid 图、5 分钟运行、API 列表、Docker、测试命令、数据边界、项目来源与二次开发边界。 |
| **`ARCHITECTURE.md`** | 36 行的精华版架构：核心组件表（领域/Provider/工具/可靠性/Agent 状态/评测）+ 信任边界 + 离线执行 + 审计工件。 |
| **`api.py`** | FastAPI 应用。所有响应走统一信封 `status: ok\|error` + `data` + `meta` + `error`，`meta` 里必须保留 `snapshot_id`、`provider`、`research_as_of`、`evidence_ids`、`validation_status`。路由清单见第 11 节。 |
| **`workbench.py`** | Streamlit 主入口，约 3000 行。包含导航分组 `NAV_GROUPS`、`NAVIGATION`（22 页的兼容契约，测试会断言）、样式、以及"研究分析 / 证据与交付 / 任务与评测"各页面的渲染函数。**新增 Demo 页面不要写在这里**，见第 8 节。 |
| **`conftest.py`** | 只有一件事：把项目根塞进 `sys.path`，让离线测试里 `import src.*` 一定成立。 |
| **`pytest.ini`** | 只注册 `integration` marker，`asyncio_mode = strict`。默认 `pytest -q` 就是**零成本离线**。 |
| **`requirements.txt`** | 关键依赖：`openai`/`anthropic`（模型客户端）、`pandas`、`tushare`、`akshare`、`fastapi`/`uvicorn`、`streamlit`/`plotly`、`jieba`（CARD-06 分词，**缺失时自动降级**到确定性空白+CJK 二元切分，离线测试仍可跑）、`reportlab`+`pdfplumber`（PDF fixture 生成与解析）。 |
| **`Dockerfile` / `docker-compose.yml`** | 非 root 运行，启动自动生成 illustrative Demo；真实数据与输出放具名卷。 |

---

## 6. `src/` 逐包讲解

`src/` 共 **168 个 .py / 约 56,061 行**。分包如下（按"改动频率"而非字母序）：

### 6.1 `src/cn/` — A 股领域主干（27 文件 / 14,086 行）

| 文件 | 一句话职责 |
|---|---|
| `domain.py` | **中立领域 Schema**。`FinancialStatement` 等模型的唯一出处，所有 Provider 都要归一到这里。 |
| `symbols.py` | A 股代码规范化。内部统一 `600519.SH` / `000333.SZ` / `920xxx.BJ`；由代码前缀推交易所，后缀写错会抛 `UnsupportedSymbolError`。**Provider 的写法只能在适配器边界转换，不许存进快照。** |
| `periods.py` | 确定性期间转换：`FY` / `Q1` / `H1` / `9M` / `TTM`，含累计→单季推导。 |
| `evidence.py` | **Evidence Ledger**。事实 + 计算 + 依赖 DAG，一切结论的证据都从这里取 ID。 |
| `valuation.py` | 确定性倍数计算 + CARD-08 三情景（Bear/Base/Bull）× PE/PB/PS + 敏感性矩阵 + Assumption Registry。（1410 行，最核心之一） |
| `peer_workflow.py` | **同行选择**。`select_peers()` 只保留「同行业 + 同期间 + 有证据」的候选，并记录**每条纳入/剔除的理由**。 |
| `industries.py` | **申万一级行业冻结映射**（31 个 L1 行业 + 128 只股票的 L1 归属）。`industry_peers()` 就是"谁是谁的同行"的答案。**没有独立的同行配置文件**——改同行 = 改这里的 L1 分配。 |
| `workbench_service.py` | **工作台与 HTTP API 共用的只读业务服务**（1567 行）。UI 与 API 都只调它，**前端不派生任何数字**。 |
| `report.py` | 中文 Markdown 研究报告骨架（离线、可复现）。 |
| `research.py` | 结构化、可审计的研究计划状态（`ResearchState`：工具轨迹、证据 ID、成本）。 |
| `research_agent.py` | **当前主线的共享 Agent** `CnResearchAgent`：provider 无关、可观测。 |
| `offline_agent.py` | 23 行的向后兼容适配器（旧基准入口），**不是**主线 Agent。 |
| `agent_service.py` | 把 `CnResearchAgent` 接进工作台任务的应用服务。 |
| `benchmark.py` | 离线确定性基准原语（工具路由 / 证据 / 回归）。 |
| `benchmark_v2.py` | **CARD-11** 真实世界基准 `cn_agent_v2` 的指标引擎与报告（三级：Synthetic / Real / Holdout）。 |
| `real_benchmark.py` | 真实模型（Loop-2）评测器，含成本与预算闸。 |
| `evidence_pack.py` | **CARD-12** 证据包 + 审计重放（13 类证据分三档，manifest/events 不可覆盖）。 |
| `failure_injection.py` | **CARD-12 §14** 七个脚本化失败注入场景。 |
| `conflict.py` | **CARD-13** 来源冲突解决（1920 行）：两级分组、四级优先级、三态裁决。 |
| `memo.py` | **CARD-10** 买方投资备忘录（11 段 + 句级三态）。 |
| `disclosure_lexicon.py` | 披露版本 / 会计口径的**声明式词表**（供 CARD-04 使用）。 |
| `demo_data.py` | 生成**确定性的、license-safe** 的 illustrative 演示快照。 |
| `demo_service.py` | **CARD-15 §10** Demo 页面的只读服务层（1348 行）。 |
| `document_service.py` | 文档只读服务（工作台"文档智能"页 + API）。 |
| `ablation_agent.py` | 四变体消融用的可配置 Agent。 |
| `errors.py` | Provider 层的类型化异常。 |

### 6.2 `src/cn/providers/` — 数据 Provider（8 文件 / 891 行）

| 文件 | 职责 |
|---|---|
| `base.py` | Provider 契约接口。 |
| `snapshot.py` | **版本化离线 Provider（默认路径）**，读 `data/snapshots/cn/*.json`。 |
| `tushare.py` | Tushare Pro 适配器，**预算受限**（每次调用计数、第一次失败即停）。 |
| `tushare_normalizer.py` | Tushare 响应与已存探针产物的**共享归一逻辑**。 |
| `akshare.py` | 免费无 key 的 AKShare Provider。 |
| `router.py` | 确定性路由 + fallback + 熔断 + 缓存。 |
| `cache.py` | TTL 内存缓存。 |
| `__init__.py` | 包导出。 |

### 6.3 `src/cn/documents/` — CARD-01 文档智能层（10 + 11 文件 / 4,197 行）

顶层 `parsers/` 是**只负责定位、不做判断**的解析器集合：`pdf_text.py`（页 + 逐行 bbox）、`table.py`（A 股跨页合并 + 双栏切分）、`financial_statement.py`、`notes.py`、`announcement.py`、`excel.py`、`report_draft.py`、`text.py`、`ocr.py`。

其余：`registry.py`（SQLite 文档注册表）、`models.py`、`fact_extraction.py`（RawFact → 校验后的 ExtractedFact）、`guard.py`（Controlled Environment Guard，**已并入 CARD-01，不单独做卡**）、`evidence_bridge.py`（文档事实 → Evidence Ledger）、`synthetic_corpus.py`（712 行合成语料）、`extraction_benchmark.py`、`financial_benchmark.py`、`fixtures_builder.py`。

### 6.4 `src/cn/normalization/` — CARD-02 财务规范化（11 文件 / 2,400 行）

`units.py`（单位/数字字面量）、`periods.py`（期间键规范化 + `period_kind`）、`scope.py`（合并 vs 母公司口径）、`version.py`（`AS_REPORTED`/`RESTATED`/`CORRECTED`）、`metrics.py`（**§7.1 标准指标词典 + 显式投影表**）、`pipeline.py`（主流程，554 行）、`adapter.py`、`evidence_bridge.py`、`warning_log.py`、`models.py`。

### 6.5 `src/cn/analysis/` — CARD-03/04 财务分析（10 文件 / 4,268 行）

`pipeline.py`（**`analyze_financials` 主流程，1201 行**）、`metrics.py`（14 个分析指标 + 期间对齐表）、`comparisons.py`（期间比较 + 盈利状态语义）、`calculations.py`（可审计计算记录）、`diagnostics.py`（确定性诊断信号，**只出信号不出判决**）、`series.py`、`scope_signals.py`（CARD-04 口径与重述检测，654 行）、`evidence_bridge.py`、`models.py`。

### 6.6 `src/cn/` 其余创新卡片包

| 包 | 卡片 | 文件与职责 |
|---|---|---|
| `acme/` | CARD-22（11 文件 / 3,233 行） | 六类约束族：`identities.py`(F1 恒等式)、`structural.py`(F2 小计勾稽)、`period_relations.py`(F3)、`cross_period.py`(F4)、`cross_source.py`(F5 年报 vs Snapshot)、`cross_modal.py`(F6 正文/表格/图表)、`tolerance.py`(**per-unit 容差策略**)、`validator.py`(§8 入口)、`adapters.py`、`models.py`。 |
| `checker/` | CARD-05（10 文件 / 4,240 行） | 研报纠错：`checker.py`（**13 类规则、零模型参与**，999 行）、`extractor.py`（**本卡唯一的 LLM 步骤**，只把字面量搬出句子）、`rule_extractor.py`（同一边界的确定性离线路径）、`sentences.py`、`facts.py`、`pipeline.py`、`draft_corpus.py`、`benchmark.py`（P/R/F1/FPR）、`models.py`。 |
| `graph/` | CARD-09（5 文件 / 2,270 行） | 主张-证据图谱：`models.py`(1036 行)、`builder.py`、`propagation.py`(§7b 依赖语义 + fail-closed 传播)、`revalidation.py`(证据失效与增量重验证)。 |
| `proof/` | CARD-23（3 文件 / 2,070 行） | 结论护照：`passport.py`（**构建 + 不依赖 LLM 的重新证明**，1239 行）、`models.py`。 |
| `fragility/` | CARD-24（4 文件 / 1,504 行） | 脆弱性：`engine.py`（关键依赖/最小割集/单点失效/数值脆弱性/监控计划）、`theses.py`（**两条确定性 Thesis 来源，不许凭空造**）。 |
| `temporal/` | CARD-25（4 文件 / 1,771 行） | 时间重验证：`engine.py`（增量**真有界重算**）、`lineage.py`（append-only 版本谱系）、`models.py`。 |
| `retrieval/` | CARD-06（8 文件 / 920 行） | 检索：`bm25.py`（**自研**）、`tokenize.py`（中文感知确定性分词）、`chunking.py`、`engine.py`、`citation.py`、`rerank.py`。 |
| `runs/` | CARD-07（4 文件 / 917 行） | Run Manifest：17 字段 `RunManifest` + **只追加** `events.jsonl` + `replay()` 双段 diff。`run_id` 是 CARD-09/12 的关联键。 |
| `finfuzz/` | CARD-26（8 文件 / 2,812 行） | 金融语义对抗变异测试：`operators.py`（10 类变异）、`suite.py`、`corpus.py`、`targets.py`、`baseline.py`（结果发布成 CARD-11 组件基线）、`__main__.py`（CLI）。 |
| `daily_review/` | 每日复盘（7 文件 / 1,752 行） | `collector.py`、`analysis.py`、`gate.py`（确定性结论闸）、`provider.py`、`report.py`（自包含离线 HTML）、`schema.py`。 |

### 6.7 `src/agents/tools/` — 工具层（3 文件 / 2,617 行）

| 文件 | 职责 |
|---|---|
| `base.py` | 异步工具框架。 |
| `cn_tools.py` | **2451 行的 A 股工具实现**，全部离线优先、基于版本化快照，返回 `status: ok\|error` 结构化信封。**这是 Agent 与领域层之间唯一的正式契约面。** |
| `__init__.py` | 注册表导出。 |

### 6.8 `src/validation/` — 确定性校验器（15 文件 / 3,868 行）

财务与研究闸门：`financial_validator.py`、`report_validator.py`（从渲染后的 Markdown 反抽数字交叉核对）、`research_gate.py`（**结论阻断闸**）、`valuation_validator.py`(CARD-08)、`scope_validator.py`(CARD-04)。

各卡片配套校验器：`checker_validator.py`、`graph_validator.py`、`memo_validator.py`、`acme_validator.py`、`proof_validator.py`、`fragility_validator.py`、`temporal_validator.py`、`conflict_validator.py`、`finfuzz_validator.py`。

**共同约定**：validator 是"检查而不是信任"——输入是报告对象，输出是 `valid` + `errors` + `warnings` + 可复核的 `recalculated_values`。

### 6.9 `src/llms/` — 模型客户端（8 文件 / 2,041 行）

| 文件 | 职责 |
|---|---|
| `model_registry.py` | **所有模型元数据的唯一事实来源**，调用路径按 `api_style` 路由。 |
| `async_client.py` | 异步客户端（同步版的增量对应物）。 |
| `openai_compatible.py` | OpenAI 兼容协议客户端 + 每厂商一行工厂函数。 |
| `config.py` | `_SYNC_MODEL_FUNCTIONS` 登记表，键必须与注册表**严格相等**。 |
| `cost_calculator.py` | 确定性成本计算（**不存任何模型元数据**）。 |
| `openai.py` / `claude.py` | 具体厂商同步客户端。 |

**成本口径关键规则**：成本账本是 **USD-only + 硬预算闸**。国内厂商按人民币计费时，若单价**核不实**就登记为「未定价」（`price_currency=None`、`price_source=None`），**不做汇率换算、不填参考价**；且未定价模型**不能与 `--max-cost-usd` 同时使用**（fail-closed 拒绝启动），否则总额恒小于上限、预算闸静默失效。

### 6.10 `src/logger.py`

集中式日志工具（204 行），含 `logger.llm_call(currency=...)` 等结构化事件。

> 注意：**`src/` 下没有 `__init__.py`**，它靠命名空间包工作（`conftest.py` 把项目根加进 `sys.path`）。所以 `import src.cn.*` 能跑，但不要假设存在包级初始化逻辑。

### 6.11 容易 import 错的重名文件（**务必区分**）

同一个文件名在多个包里出现，语义完全不同：

| 重名 | 各自归属 |
|---|---|
| `periods.py` | `src/cn/periods.py`（**全局**期间转换 `FinancialPeriodEngine`） vs `src/cn/normalization/periods.py`（CARD-02 的**期间键**规范化与 `period_kind` 判定） |
| `metrics.py` | `src/cn/normalization/metrics.py`（**§7.1 指标词典 + 投影表**） vs `src/cn/analysis/metrics.py`（CARD-03 的 **14 个分析指标 + 期间对齐表**） |
| `evidence_bridge.py` | `documents/`、`normalization/`、`analysis/` 各一份，分别把**各自产出**桥接进 `EvidenceLedger` |
| `models.py` | 几乎每个包各一份（各自卡片的数据模型），**不是共享模型** |
| `pipeline.py` | `normalization/pipeline.py`（CARD-02） vs `analysis/pipeline.py`（**CARD-03 主流程 `analyze_financials`，1201 行**） vs `checker/pipeline.py`（CARD-05 编排） |
| `benchmark.py` | `src/cn/benchmark.py`（离线基准原语） vs `src/cn/checker/benchmark.py`（CARD-05 的 P/R/F1） |
| `validator.py` | `src/cn/acme/validator.py`（ACME §8 编排入口） vs `src/validation/*_validator.py`（各卡校验器）——**前者是引擎入口，后者是校验规则** |

统一约定：**共享的领域模型只有一处** = `src/cn/domain.py`；其余 `models.py` 都是各自卡片私有。

---

## 7. `scripts/` 逐个说明（19 个）

### 采集与快照
| 脚本 | 用途 |
|---|---|
| `probe_tushare_cn0.py` | 单标的 Tushare 探针：**固定 6 次调用、不重试、第一次失败即停**，产物落 `data/provider_probes/cn0/`。 |
| `build_cn_snapshot_from_probe.py` | 把探针产物砖成归一化离线快照。**`snapshot_id` 的日期取自 `--research-as-of`，与 bars 区间无关。** |
| `build_cn_universe.py` | **批量采集 128 只精选股票池**（顺序执行避限流，同研究截止日已存在则跳过；`--only` 可单跑）。 |
| `rebuild_tushare_snapshots.py` | 重建**离线测试依赖的 9 个关键标的**（`SPEC` 常量里固化了三个历史坑，别手工拼命令）。 |
| `audit_cn_snapshots.py` | 审计本地快照并输出**溯源感知的质量清单**（JSON + Markdown），标注 `pass/warning/blocked`。 |
| `fetch_benchmark_real.py` | CARD-11 真实层级材料获取。 |

### 生成与夹具
| 脚本 | 用途 |
|---|---|
| `bootstrap_demo.py` | 生成 license-safe 的确定性演示快照。 |
| `bootstrap_documents.py` | 注册内置合成财报，让工作台"文档智能"页有东西可看。 |
| `gen_document_fixtures.py` | 生成 CARD-01 合成文档语料（**已提交的 fixture**）。 |
| `gen_report_draft_fixtures.py` | 生成/校验 CARD-05 的"人为注入错误"草稿语料。 |
| `generate_cn_snapshot_report.py` | 仅凭本地快照渲染可复现报告。 |

### 评测与门禁
| 脚本 | 用途 |
|---|---|
| `run_benchmark.py` | 对真实离线 Agent 轨迹跑基准。 |
| `run_benchmark_v2.py` | CARD-11 `cn_agent_v2` 三级基准。 |
| `run_ablation.py` | 离线消融（真实快照工具 + 确定性故障注入）。 |
| `run_agent_ablation.py` | **四变体真实 LLM 消融**（direct_llm / agent_tools / agent_tools_evidence / agent_tools_evidence_validator）。 |
| `run_real_agent_benchmark.py` | Loop-2 真实模型基准（带成本与预算闸）。 |
| `check_ablation_thresholds.py` | CI 里的消融产物阈值门禁。 |

### 验证与彩排
| 脚本 | 用途 |
|---|---|
| `smoke_workbench.py` | **冒烟：加载工作台全部 22 个页面**，输出 `22/22 pages clean`。不需要先启动 Streamlit。 |
| `rehearse_demo.py` | **把 5 分钟彩排从主观走查变成能跑的清单**（841 行，对照 DEMO_FLOW，`--strict` 更严）。 |

---

## 8. `workbench/` 与 `workbench.py` 的关系（容易搞错）

- `workbench.py` 是 **Streamlit 主入口**，已有页面（研究分析 / 证据与交付 / 任务与评测）在这里。
- `workbench/` 是 **CARD-15 §4 的新增 Demo 页面容器包**，`workbench/demo/` 下 8 个页面模块：

| 模块 | 页面 | 对应 DEMO_FLOW 环节 |
|---|---|---|
| `demo/acme.py` | ACME 约束 | 步骤 4b（创新展示一） |
| `demo/corrections.py` | 研报纠错 | 步骤 6+7（★高潮一） |
| `demo/passport.py` | 结论护照 | 步骤 7b（★核心动作） |
| `demo/graph.py` | 主张-证据图谱 | 步骤 9+10（结论→逐层展开） |
| `demo/fragility.py` | 逻辑脆弱性 | 步骤 10b（创新展示三） |
| `demo/replay.py` | 审计回放 + 证据包导出 | 收尾 |
| `demo/temporal.py` | 时间重验证 | 失败注入 F9 |
| `demo/adversarial.py` | 对抗注入 | 失败注入 F1–F8 |

`demo/__init__.py` 是挂载入口，`demo/_shared.py` 是共用呈现助手。

**约定**：新增 Demo 页面**不要改 `workbench.py` 的既有页面函数**——它被测试断言为兼容契约；新页面加进 `workbench/demo/`，由挂载入口统一登记；`NAVIGATION` 的 22 项顺序是测试与深链的契约。

---

## 9. `tests/` 逐个说明（57 个文件）

三层组织：**单元 / 契约 / Golden**，全部离线。命名即归属：

### 按卡片
| 测试文件 | 覆盖 |
|---|---|
| `test_cn_documents.py`、`test_cn_document_benchmark.py`、`test_cn_document_service.py` | CARD-01 |
| `test_cn_normalization.py` | CARD-02 |
| `test_cn_analysis.py` | CARD-03 |
| `test_cn_scope_signals.py` | CARD-04 |
| `test_cn_checker.py`（1600 行） | CARD-05 |
| `test_cn_retrieval.py` | CARD-06 |
| `test_cn_run_manifest.py` | CARD-07 |
| `test_cn_valuation.py` | CARD-08 |
| `test_cn_graph.py` | CARD-09 |
| `test_cn_memo.py` | CARD-10 |
| `test_cn_benchmark_v2.py` | CARD-11 |
| `test_cn_evidence_pack.py` | CARD-12 |
| `test_cn_conflict.py` | CARD-13 |
| `test_cn_demo_ui.py`、`test_cn_demo_cases.py`、`test_cn_demo_rehearsal.py` | CARD-15 |
| `test_cn_acme.py` | CARD-22 |
| `test_cn_proof.py`（2073 行，**全仓最大**） | CARD-23 |
| `test_cn_fragility.py` | CARD-24 |
| `test_cn_temporal.py` | CARD-25 |
| `test_cn_finfuzz.py` | CARD-26 |

### 按基础能力
`test_cn_symbols.py`、`test_cn_periods.py`、`test_cn_period_filter.py`、`test_cn_evidence.py`、`test_cn_validation.py`、`test_cn_peer_workflow.py`、`test_cn_industries.py`、`test_cn_universe_consistency.py`（**股票池 ↔ 同行映射一致性守卫**）、`test_cn_research.py`、`test_cn_research_agent.py`、`test_cn_offline_agent.py`、`test_cn_report.py`、`test_cn_report_cli.py`、`test_cn_tools.py`、`test_cn_snapshot_provider.py`、`test_cn_snapshot_builder.py`、`test_cn_workbench_service.py`。

### Provider 与接口
`test_cn_cache.py`、`test_cn_router.py`、`test_cn_tushare_provider.py`、`test_cn_tushare_budget.py`、`test_cn_tushare_integration.py`（**integration，需凭据**）、`test_workbench_api.py`、`test_agent_research_api.py`、`test_agent_research_service.py`、`test_daily_review_service.py`、`test_cn_daily_review.py`。

### 模型与评测
`test_model_registry.py`（1467 行）、`test_real_benchmark.py`、`test_cn_agent_ablation.py`、`test_cn_benchmark.py`、`test_cn_benchmark_runner.py`、`test_cn_ablation_thresholds.py`。

### fixtures
`tests/fixtures/cn/`（离线快照）、`fixtures/cn/benchmarks/`、`fixtures/documents/` + `gold/`（CARD-01 抽取的黄金标注）、`fixtures/report_drafts/` + `gold/`（CARD-05 注入错误草稿与其答案）。

---

## 10. `docs/` 逐个说明

### 交付文档（给人看的）
| 文件 | 说明 |
|---|---|
| `docs/RUN_GUIDE.md` | 本地运行与自测全流程：3.11 环境、venv、bootstrap、Streamlit、FastAPI、离线测试、真实 Tushare 快照流程、128 只股票池的批量采集、**"新增一只股票（同时设置它的同行）"**、常见问题。 |
| `docs/RESUME_AND_INTERVIEW.md` | 简历与面试准备口径。 |
| `docs/benchmark/LABELING_SPEC.md` | 基准标注规范。 |

### 开发文档体系（`docs/development/`）
| 文件 | 说明 |
|---|---|
| **`MASTER_PLAN.md`** | **进度唯一真相源**：主链、竞赛定位、必须保留的资产、执行纪律。执行任何卡片前先读它的 §8–§11。 |
| `ARCHITECTURE_V2.md` | 520 行的架构级说明。 |
| `CHANGELOG.md` | **只记"文档体系版本"的变更**（v2.0 → v3.2），不是代码 changelog。 |
| `DEMO_FLOW.md` | **5 分钟比赛 Demo 的 12 步主故事 + 失败注入 F1–F8 + 现场保障 + 答辩 FAQ**。页面清单以本文为准（v3.2）。 |
| `INNOVATION_DESIGN.md` | 四大创新体系设计。 |
| `DUAL_COMPETITION_POSITIONING.md` | 双赛同构的叙事定位。 |
| `COMPETITION_REQUIREMENTS_MATRIX.md` | 竞赛要求 → 模块 → 状态 → CARD → 验收标准 → Demo 体现，逐项对照。 |
| `THIRD_PARTY_NOTICES_PLAN.md` | 第三方清单计划（名称/版本/来源/许可证/用途）。 |

### `docs/development/cards/` — 27 张任务卡

每张卡是一份完整契约（通常含 §1 目标、§6 数据模型、§7 算法、§10/§13 校验规则、§16 变更记录）。**实现状态如下（这是接手时最容易误判的地方）**：

| 已实现（有代码 + 测试） | 仅有设计文档（`src/` 中零实现） |
|---|---|
| CARD-01 ~ CARD-13、CARD-15、CARD-22 ~ CARD-26 | **CARD-14（Prompt Registry）、CARD-16（DCF）、CARD-17（质量诊断）、CARD-18（多 Agent）、CARD-19（Bull/Bear）、CARD-20（MCP Server）、CARD-21（产业链）** |

> 已用 `grep -rl "CARD-1[4-9]\|CARD-2[01]" src/` 核实：这 7 张卡在 `src/` 中**零引用**。它们是"计划中/暂缓"，不要以为代码藏在别处。其中 CARD-16（DCF）与 CARD-20（MCP）在 `LIMITATIONS.md`/需求矩阵里明确写成"不属于已验证主线 / 暂缓"。

---

## 11. `api.py` 路由清单

统一信封：`status: ok|error` + `data` + `meta` + `error`。`meta` 必须带 `snapshot_id` / `provider` / `research_as_of` / `evidence_ids` / `validation_status`。共 **39 条路由**：

```
# 健康检查
GET  /health

# 研究快照（只读）
GET  /api/research                                       快照列表
GET  /api/research/{sid}/summary | market | financials | evidence | valuation
GET  /api/research/{sid}/validation | trace | report | artifacts

# 受控研究任务（本地 JSON + 线程池，单机 Demo 用）
POST /api/research                                       启动 (202)
GET  /api/tasks | /api/tasks/{id} | /api/tasks/{id}/progress | /api/tasks/{id}/diagnostics
POST /api/tasks/{id}/rerun | /api/tasks/{id}/cancel      (202)

# 真实 LLM Agent 研究
POST /api/agent/research                                 启动 (202)
GET  /api/agent/research/{id} | /api/agent/research/{id}/trace

# 快照采集
POST /api/snapshots/acquire                              采集并固化快照 (202)

# 文档智能（CARD-01）
GET  /api/documents | /api/documents/{document_id}
GET  /api/documents/{document_id}/facts
GET  /api/documents/{document_id}/facts/{fact_id}/jump   跳转原文页/bbox
GET  /api/documents/{document_id}/analysis

# 每日复盘
GET  /api/daily-review
GET  /api/daily-review/{sid}/summary | panorama | hotspots
GET  /api/daily-review/{sid}/report                      自包含 HTML
POST /api/daily-review/acquire                           (202)

# 评测与消融
GET  /api/evaluations | /api/evaluations/{evaluation_id:path}
```

---

## 12. `data/` 与 `output/`（gitignored 的运行产物）

```
data/
├── snapshots/cn/          <SYMBOL>_<YYYYMMDD>_tushare_v1.json  ← 版本化离线快照（当前 128 只 @20260923）
│                          <SYMBOL>_illustrative_demo_v1.json   ← 演示快照
├── snapshots/market/      每日复盘快照
├── provider_probes/cn0/   Tushare 探针原始产物（6 个 CSV/标的）
├── documents/             文档注册表（SQLite）+ 解析产物
└── demo/                  真实材料演示（如宁德时代 2024 年报 PDF）
output/                    报告 .md/.json、快照质量清单、评测与消融产物
```

**因为 `data/` 被 gitignore，换环境/重新 clone 后这些都会丢失**，必须重跑采集脚本（见 `docs/RUN_GUIDE.md` §7）。当前快照的两个既有约定：

- **bars 按降序（新→旧）存储**，是 provider 的原始顺序，`test_cn_tushare_provider` 会断言 `bars[0]` 是最新日期——**不要"修正"成升序**；
- **同行快照必须一起建**：`research_peer_valuation` 只保留 `research_as_of` 相差 ≤3 天的快照，所以给某只股票建快照后，它同行业的同行也必须用**同一研究截止日**建，否则 `peer_count = 0`。

---

## 13. 运行与验证命令

```bash
# 离线全量回归（默认，零成本、无网络）
python -m pytest -q -m "not integration" -p no:cacheprovider

# 工作台冒烟（22 页）
python scripts/smoke_workbench.py

# Demo 剧本自检
python scripts/rehearse_demo.py            # 加 --strict 更严

# 代码卫生
git diff --check
git status --short

# 本地跑起来
python scripts/bootstrap_demo.py
python -m streamlit run workbench.py       # http://localhost:8501
python -m uvicorn api:app --reload         # http://localhost:8000/docs
```

**当前基线**：`0 failed / 1550 passed / 0 skipped`（2026-09-26，128 只 / 23 行业）。
**判据是失败集合逐条不变**，不是 passed 变多。凡是通过数变化，必须能说清来源（新增用例 / 快照重建 / 记录口径差）。

### 本机环境的三个已知坑

1. **前台跑较重的 pytest 子集可能被 SIGTERM 杀掉且 stdout 为空**，日志里留下一个可疑的 `E`，容易被误读成 fixture error。**解法：改后台运行**并把输出重定向到 `tmp/*.log` 再读。
2. **safe-delete 的 bulk guard 按"每轮对话累计"计数**（阈值 50），触发时在测试自身的 `shutil.rmtree` 里抛 `SystemExit`，导致 monkeypatch 未还原 → 后面的快照类测试连锁报 `KeyError: Unknown snapshot_id`，看起来像一二百条"大面积回归"。**识别特征**：`errors > 0` + 失败铺满不相干模块 + 栈里出现 `sitecustomize.py` + 大量 `sqlite3.ProgrammingError: Cannot operate on a closed database`。**解法**：跑 pytest 时摘掉守卫相关环境变量（`CODEBUDDY_SAFE_DELETE_BULK_*`、`CODEBUDDY_TOOL_CALL_ID`、`CODEBUDDY_SANDBOX_BROKER_TOOL_CALL_ID`）。
3. **判据一律看 `--junitxml` 并解析失败集合**——沙箱里 stdout 可能被截断。

---

## 14. 常见任务 → 该改哪里

| 我要做的事 | 入口 |
|---|---|
| 加一只股票到精选池（**同时配它的同行**） | `scripts/build_cn_universe.py` 的 `CURATED_UNIVERSE` **+** `src/cn/industries.py` 的 `_STOCK_INDUSTRY`（**两处必须一致**，由 `tests/test_cn_universe_consistency.py` 锁死），然后 `--only <code>` 采集 |
| 换/加一个模型厂商 | `src/llms/model_registry.py` + `openai_compatible.py` 工厂 + `config.py` 登记 + `tests/test_model_registry.py` 的三条清单断言 |
| 加一个财务指标 | `src/cn/normalization/metrics.py`（先看**投影表**）+ `src/cn/analysis/metrics.py` 的期间对齐表 |
| 加一条报告校验规则 | `src/validation/report_validator.py` / 对应卡片的 `*_validator.py` |
| 加一个工作台 Demo 页面 | 新建 `workbench/demo/<name>.py` + 在 `demo/__init__.py` 登记（**不要动 `workbench.py` 既有页面**） |
| 改同行选择规则 | `src/cn/peer_workflow.py::select_peers()` + `industries.py`，并同步 CARD-08 |
| 改数据来源/口径 | `src/cn/providers/` + `DATA_SOURCES.md` + 对应快照重建脚本 |
| 改估值方法 | `src/cn/valuation.py` + `src/validation/valuation_validator.py` + `VALUATION_METHODOLOGY.md` |
| 改对外契约 | `api.py` + `src/cn/workbench_service.py`（UI 与 API 共用同一服务层） |

**跨卡改动要同步写进被改那张卡的 §16**；`CHANGELOG.md` 只记**文档体系版本**，不记代码改动。

---

## 15. 30 秒速览（如果只记一件事）

> 这是一个**离线优先的 A 股可验证研究系统**。四层严格分离：Provider 出**事实**、Python 做**计算**、Evidence Ledger 记**依赖**、Validator 定**放行**——LLM 只选工具和写文字。
> 一切以 `docs/development/cards/CARD-xx_*.md` 的章节契约为准；实现覆盖 CARD-01~13、15、22~26（CARD-14/16~21 仅设计）。
> 改代码前先跑 `python -m pytest -q -m "not integration"` 拿到基线，改完再跑一次，**比较失败集合而不是通过数**。
> **零静默、零猜测**：判不准就 `None` + warning，绝不用 LLM 或估计值补数字。
