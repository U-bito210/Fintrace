---
name: cn_research_agent_run_context
version: 1
variables: ["cutoff"]
created_at: "2026-09-27"
description: "把固定的研究时点钉进系统上下文，避免模型自行编造 research_as_of"
---

RUN CONTEXT: The fixed research cutoff is {{cutoff}}. When any tool accepts research_as_of, use this exact value unless the user explicitly supplies a different cutoff.
