---
name: cn_research_agent_preflight
version: 1
variables: ["resolved_symbol"]
leading_newline: true
created_at: "2026-09-27"
description: "确定性代码已解析出规范代码时追加的一段；正文以换行开头，与拼接位一致"
---

PRE-FLIGHT: deterministic code has already resolved the request to {{resolved_symbol}}. Do not repeat symbol resolution; fetch the requested snapshot data using that canonical ticker.
