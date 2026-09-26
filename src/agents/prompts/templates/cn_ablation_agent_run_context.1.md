---
name: cn_ablation_agent_run_context
version: 1
variables: ["cutoff"]
created_at: "2026-09-27"
description: "消融 agent 的研究时点绑定段；与 research 版本同措辞、由各自的 _system_prompt 拼接"
---

RUN CONTEXT: The fixed research cutoff is {{cutoff}}. When any tool accepts research_as_of, use this exact value unless the user explicitly supplies a different cutoff.
