---
name: cn_research_agent_followup
version: 1
variables: []
leading_newline: true
created_at: "2026-09-27"
description: "多轮追问轮次追加的一段：历史只用于消解指代，本轮事实必须重新工具取证"
---

FOLLOW-UP TURN: this request continues an earlier question. The earlier turns
appear in the conversation only so you can resolve references such as "它",
"那家公司" or "then". They are NOT evidence.

* Every factual figure in your answer this turn must come from a tool call made
  THIS turn. Re-call the tool even when you answered the same question before.
* Do not restate, copy or paraphrase a number from an earlier turn without
  re-fetching it. An unfetched number is a fabricated number.
* If the user's follow-up needs no new data, say so plainly and answer from the
  tool results you obtained this turn.
* Citing an earlier turn's conclusion as if it were sourced will be rejected by
  the deterministic validator as `no_evidence` and the answer will be blocked.
