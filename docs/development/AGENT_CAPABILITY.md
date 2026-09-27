# AGENT_CAPABILITY.md — AI Agent 研究：能力边界与现状

> 本文回答三个问题：**它能做什么**、**它为什么拒绝回答**、**它还不是什么**。
> 全部结论来自代码，不是设计意图 —— 每条都标了出处，可逐条核对。
> 2026-09-27 首次成文（此前 `LIMITATIONS.md` 关于 Agent 只有一行，说不清边界）。

---

## 1. 它是什么形态

**ReAct 轨迹 + 可追问会话，不是对话机器人。**

| 属性 | 值 | 出处 |
|---|---|---|
| 执行方式 | ReAct（推理 → 调工具 → 看结果 → 再推理） | `src/cn/research_agent.py` 类文档字符串 |
| 最大步数 | **6**（`max_steps=6`） | `research_agent.py:68` |
| 采样温度 | **0.0**（默认，确定性优先） | `research_agent.py:67` |
| 绑定范围 | **一份 pinned snapshot**（一次运行只针对一个快照） | `research_agent.py:67` |
| 交互入口 | 聊天式输入与会话记录；可新建对话、在同一快照内追问 | `workbench.py` 的 Agent 研究页 |
| 追问 | 有：**每轮仍是独立 run**，上一轮结论仅作指代上下文重放 | `agent_service.parent_task_id`、§7 |

**「你好啊」和“你有什么功能”会得到本地的使用引导**，不会发起模型调用。
公司、行情、财务或估值问题才进入真实研究轨迹；当工具取不到所需证据时，
Validator 会阻断结论。这既避免把问候误报成失败，也避免把无证据内容包装成研究结论。

---

## 2. 它能调用什么（20 个工具）

出处：`src/agents/tools/cn_tools.py`（2451 行）

**快照与行情**
`resolve_cn_symbol` · `get_cn_prices` · `get_cn_financials` · `value_with_peers` ·
`run_cn_relative_valuation` · `generate_cn_research_report` · `create_cn_research_plan`

**文档解析与财务**
`ingest_cn_document` · `normalize_cn_financials` · `analyze_cn_financials` ·
`detect_cn_scope_signals` · `check_cn_report_draft` · `search_cn_corpus`

**证据、结论与校验**
`trace_cn_claim` · `verify_cn_claim` · `revalidate_cn_claims` ·
`deactivate_cn_evidence` · `resolve_cn_source_conflicts` · `generate_cn_investment_memo`

**约束与脆弱性**
`validate_cn_acme` · `analyze_cn_thesis_fragility`

覆盖 CARD-01/02/03/04/05/08/09/10/12/13/22/23/24/25/26 ——
也就是说演示主故事那套确定性引擎，Agent **全都能调到**。

---

## 3. 它凭什么说「我不回答」（阻断错误码全表）

出处：`src/validation/research_gate.py::ResearchGateValidator.validate`

判定是**纯确定性**的，不读模型措辞（模块 docstring 原话：*"It never relies on model prose."*）。

| 错误码 | 含义 |
|---|---|
| `tool_error:{tool_name}` | 有工具调用没成功 |
| `no_evidence` | 要求证据，但账本里 0 条 |
| `invalid_research_as_of:{...}` | 研究时点不是合法时间戳 |
| `availability_unknown:{evidence_id}` | 证据没有披露时点 |
| `invalid_availability_timestamp:{...}` | 披露时点格式非法 |
| `unavailable_before_cutoff:{metric}:{date}` | 该指标在研究时点**之后**才披露 → 防未来函数 |
| `requested_metric_missing:{metric}` | 要求的指标拿不到 |
| `industry_classification_unknown:{method}` | 行业分类查不到 |
| `industry_classification_conflict:{declared}:{canonical}` | 快照声明与申万映射矛盾 |
| `industry_valuation_inapplicable:{entity_type}:{method}` | 金融机构不给 PS / EV-EBITDA |

**一条关键设计（值得单独讲）：证据准入是「双重确认」**

`snapshot_evidence_records()`（`research_gate.py:168`）要求一个证据 ID
**同时**满足两个条件才采信：

1. 出现在 Agent 自己的 state（facts / calculations）里；
2. 出现在某次 **成功** 工具调用返回的 `evidence_ids` 里。

两者取交集。注释原话：*"This intentionally cannot manufacture provenance."*
—— **处（provenance）不能凭空造**。Agent 自己"记得"但工具没真正返回的东西，一律不算证据。

---

## 4. 运行过程可见吗

可见。事件流共 **12 种**，落盘进 run 的轨迹（`research_agent.py:57`）：

```
agent_started → llm_call_started → llm_call_completed
→ tool_call_started → tool_call_completed → evidence_registered
→ validation_started → validation_passed / validation_blocked
→ task_completed / task_cancelled / task_failed
```

在「Agent 执行轨迹」页可看。事件刻意**不含** prompt 原文与本地路径
（源码注释：*"without leaking prompts or paths"*），所以轨迹可以安全地随证据包交付。

---

## 5. 它还不是什么（已知缺口，诚实列）

| # | 缺口 | 性质 | 备注 |
|---|---|---|---|
| 1 | ~~无多轮 / 无会话上下文~~ | **已补（2026-09-27）** | 见 §7：追问沿 `parent_task_id` 串成会话，每轮仍是独立 run、独立取证 |
| 2 | ~~无对话式聊天框~~ | **已补（2026-09-27）** | 使用 `st.chat_input` 展示首问和追问；研究问题仍须重新执行工具与 Validator |
| 3 | 步数上限 6 | 约束 | 复杂多跳问题可能不够用 |
| 4 | ~~`src/agents/` 与 `src/cn/` 职责错位~~ | **已补** | 实测是双向依赖、有理由的分层；已在 `src/agents/README.md` 加指路说明 |
| 5 | ~~边界未成文~~ | **已补** | 本文 + `LIMITATIONS.md` 回链 |

---

## 6. 明确不做（已有决策，不要重复开工）

| 项 | 状态 | 依据 |
|---|---|---|
| Multi-Agent | **暂缓** | CARD-18：须先过「消融四问」，未过不上 |
| MCP Server | **暂缓** | CARD-20 |
| DCF 估值 | **暂缓** | CARD-16（`LIMITATIONS.md` 明写） |

---

## 7. 多轮追问：已落地（2026-09-27），且守住了「不能拿上一轮的话当依据」

**追问必须重新走工具 + 重新取证据，不能拿上一轮的话当依据。**

理由：现在每条结论都挂 Evidence ID、能跳回原文，这是最硬的卖点。
一旦第二轮引用第一轮的**中间结论**而非原始证据，可追溯链就断了，
第 3 节的「双重确认」也就形同虚设。

### 实现（一张表看完）

| 环节 | 落点 | 做法 |
|---|---|---|
| 会话串联 | `agent_service.AgentResearchRequest.parent_task_id` | 追问任务记 `parent_task_id` + `turn_index`，`_parent_chain()` 回溯出 root→当前 |
| 历史注入 | `research_agent._history_messages()` | 上一轮 Q/A 以**纯文本**重放，**不带任何 tool result** |
| 提示词 | `cn_research_agent_followup` + `cn_research_agent_history_turn`（CARD-14 模板） | 明写「历史只用于消解指代；上一轮答案 UNSOURCED IN THIS TURN」 |
| 取证 | 不变 | 历史**不进** `state.register_evidence()`，当轮数字只能来自当轮工具调用 |
| 兜底 | Validator | 追问若只复述上一轮数字 → `tool_trace` 为空 → `no_evidence` → **blocked** |
| 边界 | `MAX_FOLLOWUP_DEPTH=5`、`MAX_FOLLOWUP_HISTORY=3` | 会话轮数与重放窗口都有上限，账单和提示词长度都有界 |
| 快照 | `_resolve_parent()` | 追问**不许换快照**；跨快照接续会让上一轮数字的来源失效 |

### 三条硬约束的落地方式

1. **历史只消解指代** —— 注入的 assistant 消息自带 `(UNSOURCED IN THIS TURN: …)` 声明，
   且只重放 `status == "succeeded"` 的轮次；被阻断的上一轮结论**不进**历史，
   避免把未经验证的文本喂给下一轮。
2. **当轮事实当轮取证** —— 历史消息是 `user`/`assistant` 纯文本，不是 tool result，
   模型无法把它们当成刚取回的数据。
3. **跨轮引用判 `no_evidence`** —— 由证据账本天然保证：不调工具就没有 evidence ID。

### 实测（2026-09-27，DeepSeek `deepseek-v4-flash` + 真实快照 `600519.SH_20260923_tushare_v1`）

| 轮 | 问题 | 工具调用 | 证据 | Validator |
|---|---|---|---|---|
| 1 | 分析最新财报的营业收入和归母净利润 | `resolve_cn_symbol` → `get_cn_financials` | 已注册 | PASSED |
| 2 | 那它的净利率是多少？ | `get_cn_financials`（**重新取证**） | 已注册 | PASSED |

第二轮没有复述第一轮的表格，而是重新调用了财报工具，并在回答里明确写了
「由本期重新取证的原始值计算」。$\approx$ 0.000003 美元/轮量级。

### 已知取舍（别当成 bug 修）

- 追问会**完整重跑一次** Agent（新一轮 LLM + 工具），不是"接着上次继续生成"。
  代价高，但这是可追溯性的对价。
- 上一轮被 blocked 时 UI **不给追问按钮**：没有可继承的证据，追问只会再断一次。
- 追问沿用第 1 轮的快照/模型/cutoff，**界面上不可改**；要换就开新问题。

---

## 8. 一句话定位（答辩用）

> 普通 chatbot 答不出就说一个听起来像真的数字；
> FinTrace 的 Agent 答不出就**拒绝回答**并给出原因（错误码），
> 而它答得出的每一句，都能点开看到是哪份数据在哪个时点支持的。
