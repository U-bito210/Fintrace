---
name: cn_research_agent_history_turn
version: 1
variables: ["answer"]
created_at: "2026-09-27"
description: "把上一轮的结论作为 assistant 历史消息重放；自带「未经本轮取证」声明，防止模型当事实复用"
---

{{answer}}

(UNSOURCED IN THIS TURN: the text above is last turn's conclusion, replayed only
so you can follow the conversation. It carries no evidence ID and must not be
quoted as a figure in this turn's answer.)
