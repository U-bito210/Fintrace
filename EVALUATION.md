# FinTrace-CN 评测说明

## 评测原则

事实、公式和证据使用确定性断言；模型文案不作为金融正确性的裁判。默认测试只读取固定夹具或本地生成的 illustrative 快照，不访问网络。

## 测试层级

1. 单元测试：代码标准化、期间转换、估值与证据重算；
2. Provider 契约：标准 Schema、错误分类、缓存、重试、fallback 与熔断；
3. Golden 回归：固定快照、Evidence ID、Validator 结果与报告边界；
4. Agent 评测：工具选择、参数、证据覆盖、失败恢复与结论泄漏；
5. Integration：真实 Tushare 或 LLM，仅在显式提供凭据时运行。

## 四组 Agent 消融

| 变体 | 工具 | Evidence | Validator |
|---|---|---|---|
| direct_llm | 否 | 否 | 否 |
| agent_tools | 是 | 剥离 | 否 |
| agent_tools_evidence | 是 | 是 | 否 |
| agent_tools_evidence_validator | 是 | 是 | 是 |

固定指标包括 tool F1、参数正确率、证据覆盖率、端到端成功率、Validator 拦截率、错误结论泄漏、延迟与成本。每个指标都由固定案例和明确分母计算。

## 离线机制验证

~~~powershell
python -m pytest -q -m "not integration" -p no:cacheprovider
python scripts/run_agent_ablation.py --dry-run --output output/agent_ablation
python scripts/check_ablation_thresholds.py output/agent_ablation/ablation_results.json
~~~

dry-run 使用 mock provider，证明评测器、工具契约和 Validator 的确定性行为。它不代表真实模型能力，简历和 README 不得把 dry-run 分数描述为真实 LLM 指标。

## 真实模型评测

~~~powershell
python scripts/run_agent_ablation.py --model deepseek-v4-flash --output output/real_agent_ablation
~~~

真实运行必须记录模型标识、Prompt/案例版本、温度、最大步数、快照 ID、Git commit、每案工具轨迹、错误、延迟与成本。连接失败的运行应保留为失败诊断，但不能作为模型效果结论。

### 换用其他模型服务（例：阿里云百炼）

任何 OpenAI 兼容协议的服务都可以接进来：在 `src/llms/model_registry.py` 登记一条 `ModelInfo`（`provider`、`api_style="openai"`、`api_key_env`、`base_url_env`、`default_base_url`），再在 `openai_compatible.py` 加一行工厂函数、在 `config.py` 登记。路由按 `api_style` 判定，不需要改任何调用路径。

已经登记好的第三方厂商：

| 模型 | `provider` | 环境变量 | 端点 |
|---|---|---|---|
| `qwen3-max` / `qwen-plus` / `qwen-flash` | `dashscope` | `DASHSCOPE_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |

~~~powershell
# .env 里填好 DASHSCOPE_API_KEY 之后
python scripts/run_agent_ablation.py --model qwen-plus --output output/real_agent_ablation_qwen
~~~

**成本口径（务必先读）**：百炼按人民币计费，而本仓库的成本账本是 USD。核对过阿里云官方页面后发现同一模型 ID 的人民币单价在多个官方页面之间并不一致，因此这些模型**登记为「未定价」**，不做汇率换算、也不填参考价：

- 成本数字仍会出现，但 `cost_currency` 为 `null`，报告与工作台都会显示「未定价（币种未知）」，**不得当金额使用**；
- `--max-cost-usd` 与未定价模型**不能同时使用**：未定价模型计入 `0.0`，总额会恒小于上限而永不触发，等于一个失效的预算闸。此时运行会**直接拒绝启动**（fail-closed），请去掉上限或改用已定价模型；
- 把 `DEEPSEEK_BASE_URL` 指向百炼（同一模型、另一家转售）会让已登记的 USD 单价同样失效，成本口径按未定价处理，预算闸同样拒绝启动。

要启用真实成本核算，就在注册表里为这些模型填入可核实的单价与 `price_source`；登记表本身就是唯一事实来源，改数据即可，不必改代码。

## 当前边界

评测重点是工具路由、证据和 fail-closed 行为，不等价于完整投资研究质量，也不衡量收益预测能力。真实模型输出仍可能随服务端版本变化。
