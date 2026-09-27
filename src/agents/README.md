# src/agents — 目录指路

> 打开这个目录想找「Agent 在哪」的人，通常会扑空：**这里只有提示词和工具，
> 没有 Agent 的执行逻辑。** 本文件说明实现在哪、以及为什么不放在一起。

## 这里放什么

| 子目录 | 内容 |
|---|---|
| `prompts/` | 提示词模板（唯一落点，受 CARD-14 Prompt Registry 约束） |
| `tools/` | Agent 可调用的工具（20 个，见 `tools/cn_tools.py`） |

本目录**没有 `__init__.py`**（与 `src/` 一样是命名空间包）—— 这是现状，不是疏漏。

## Agent 的执行逻辑在哪

在 **`src/cn/`**，不在本目录：

| 文件 | 职责 |
|---|---|
| `src/cn/research_agent.py` | **核心**：单轮 ReAct 轨迹（`CnResearchAgent`，最多 6 步） |
| `src/cn/agent_service.py` | 任务层：把一次研究请求编排成可持久化的 run |
| `src/cn/offline_agent.py` | 离线 provider（无凭据时走预生成路径） |
| `src/cn/ablation_agent.py` | 消融实验用的变体 agent |

校验闸门在 **`src/validation/research_gate.py`**（不在本目录，也不在 `src/cn/`）。

## 为什么不放在一起

`research_agent.py` 的导入方向决定了它必须在 `src/cn/` 这一侧：

```python
from src.agents.prompts import ...          # ← 依赖本目录
from src.agents.tools.cn_tools import ...   # ← 依赖本目录
from src.cn.providers.snapshot import ...   # ← 依赖领域包
from src.cn.research import ResearchState   # ← 依赖领域包
from src.validation.research_gate import ...# ← 依赖校验层
```

它**同时**吃 `src.agents.*` 和 `src.cn.*`。而 `tools/cn_tools.py` 反过来也要 import
`src.cn.*`。如果把 `research_agent.py` 挪进本目录，就会形成
`src.agents ↔ src.cn` 的**双向依赖**，循环导入的风险明显升高。

所以现状是**有代价但也有理由的分层**，不是随手乱放。
当前的处置是**加指路说明**而不是硬挪 —— 真要合并目录，必须先解开
`src.cn` 与 `src.agents` 的双向依赖，那是一次独立的重构。

## 速查

找东西时按这个顺序：

| 想找 | 去哪 |
|---|---|
| Agent 怎么跑 | `src/cn/research_agent.py` |
| 有哪些工具 | 本目录 `tools/cn_tools.py` |
| 提示词 | 本目录 `prompts/templates/` |
| 为什么被阻断 | `src/validation/research_gate.py` |
| 能力边界 / 错误码全表 | `docs/development/AGENT_CAPABILITY.md` |
