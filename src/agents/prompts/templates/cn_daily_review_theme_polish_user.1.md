---
name: cn_daily_review_theme_polish_user
version: 1
variables: ["theme_name", "news_text"]
model: "deepseek-v4-flash"
temperature: 0.3
created_at: "2026-09-27"
description: "每日复盘题材润色的用户消息：题材 + 真实新闻摘录 + 只输出一句话的格式约束"
---

题材：{{theme_name}}
相关真实新闻：
{{news_text}}

请只输出一句不超过40字的中文主线说明（仅基于上述新闻，不添加未提及信息，不荐股，不提个股）。只输出这句话本身，不要解释、不要引号。
