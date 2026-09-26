---
name: cn_claim_extractor_user
version: 1
variables: ["payload"]
created_at: "2026-09-27"
description: "CARD-05 提取器的用户消息：固定指令 + 该批句子的 JSON 载荷"
---

请逐句提取，返回 {"claims": [...]}：
{{payload}}
