# CARD-14: Prompt Registry（提示词注册表）

| 属性 | 值 |
|---|---|
| 优先级 | P1 |
| 竞赛阶段 | 初赛起随 LLM 功能落地（CARD-05/10 时同步迁移），10-15 前完成核心 prompt |
| 对应赛题 | 竞赛源代码模块清单点名的 "Prompt" |
| 依赖 | CARD-07（prompt_hash 入 manifest） |
| 实现复杂度 | 低（约 1.5 人日） |
| 状态 | `[x]` 全部验收项通过（2026-09-27）。11 段提示词迁入 `src/agents/prompts/`，§10 三条闸门落地，§14 注册表页落在「审计回放」页 ③ |

## 1. 目标

新建 `src/agents/prompts/`：版本化提示词注册表，记录 prompt_name/version/hash/model/temperature/created_at，替换散落在 `research_agent.py` 等处的硬编码字符串。**Skill 不做空壳**：只有存在真实功能价值（如稳定的多步流程约束）才建，否则只交付 Prompt Registry。

## 2. 对应竞赛评分点

技术要求点名模块、结果可复现（prompt_hash 纳入 manifest）。

## 3. 为什么需要这个模块

源代码清单里 "Prompt" 是点名模块；且 prompt 变更无版本 = 复现性破坏。v2 的 Skill 库设计有"为名词而建"的倾向，本卡收敛。

## 4. 输入 / 输出

- **输入**：现有代码中的 prompt 字符串 + 新卡片（05/10）的 prompt 需求
- **输出**：注册表 + 模板文件 + hash 服务

## 5. 数据模型

```python
@dataclass
class PromptSpec:
    prompt_name: str
    prompt_version: int
    template: str
    variables: list[str]
    prompt_hash: str          # sha256
    model: str | None          # 使用时记录
    temperature: float | None
    created_at: str
```

## 6. API / Tool Contract

```python
def load_registry(templates_dir) -> dict[str, PromptSpec]: ...
def render(prompt_name, **variables) -> str: ...   # 缺/多变量抛错
def record_usage(prompt_name, model, temperature) -> None: ...
```

模板文件：`src/agents/prompts/templates/*.md`，frontmatter 声明 name/version/variables。

## 7. 金融逻辑

无直接金融逻辑；但 prompt 内容迁移**语义必须保持**：先迁 1 个跑 golden 回归，再批量。CARD-05 提取器与 CARD-10 叙述段 prompt 是本卡首批登记对象。

## 8. Edge Cases

1. 同名不同版本并存 → 注册表按 (name, version) 寻址，运行记录实际使用版本
2. 模板变量缺失 → 渲染抛错（禁止半渲染）
3. Skill 目录决策 → 若无真实价值，在 THIRD_PARTY/答辩材料中如实说明"未采用 Skill 机制及其原因"

## 9. Failure Mode

注册表加载失败 → 任务 fail-closed（不回退硬编码字符串——回退等于绕过版本化）。

## 10. Validator Rules

- 代码库 LLM prompt 零硬编码（grep 断言测试）
- prompt_hash 必须进入 RunManifest（CARD-07 联动断言）
- 版本只增不改（同版本内容变化测试报错）

## 11. Unit Tests

渲染变量校验；hash 稳定性；版本不变性；零硬编码 grep 断言。

## 12. Integration Tests

迁移后现有 agent 测试与 golden 用例零回归；manifest 含 prompt_hash。

## 13. Benchmark

Multi-run Consistency 间接覆盖（prompt 版本化是稳定前提）。

## 14. Demo Method

展示注册表页（name/version/hash 列表）+ manifest 中的 prompt_hash——证明 prompt 可审计。

## 15. 验收标准

- [x] 零硬编码断言通过；golden 零回归
- [x] (name, version) 寻址与 hash 入 manifest 打通
- [x] Skill 取舍决策有明确记录（做或不做均有理由文档化）

**逐条证据**（2026-09-27）

1. **零硬编码**：`tests/test_cn_prompt_migration.py::test_no_prompt_text_is_hard_coded_in_python` 把
   `src/ scripts/ workbench/ tests/ api.py workbench.py conftest.py` 全扫一遍，两条独立判据 ——
   ① 每段模板的**每一行**（≥24 字符）不得出现在任何 `.py`；② 把模板按夹具给的变量**渲染后**再扫一遍
   （防的是"照抄一遍带具体值的提示词"，也就是迁移前那五处的形态）。外加一份 6 条签名句清单。
   **golden 零回归**由同一文件的三条断言保证：模板体逐字比 sha256、11 个端到端 composite 逐字比
   sha256、片段必须能重新拼回 composite。全量离线 **0 failed / 1635 passed / 3 deselected**，
   与迁移前 1586 相比 **+49 = 本卡新增用例数**，逐项可归因；冒烟 **22/22 clean**；
   `rehearse_demo.py --strict` 13 步四态与迁移前逐条一致。
2. **(name, version) 寻址与 hash 入 manifest**：注册表键就是 `name@version`（`resolve("p")` 取最新，
   `resolve("p@1")` 取指定版本，两个版本可并存）；`RunConfig.prompt_names` → `RunRecorder.start()` 经
   注册表算出**覆盖整组提示词的组合哈希**写入 17 字段 manifest 的 `prompt_hash`。旧的两处 ad-hoc
   截断哈希（`real_benchmark.config_fingerprint` 取 16 位、`scripts/run_benchmark.py` 取 12 位）统一到
   这一个来源。`tests/test_cn_prompt_migration.py` 的 §10 gate 2 四条用例覆盖"有提示词→有哈希"
   "无提示词→None""显式哈希优先""名字未登记→fail-closed"。
3. **Skill 取舍**：**决定不建 Prompt Skill 目录**，理由与"什么条件下值得建"写在 §16。

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
|  |  |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-09-27 | **开工前可行性核查（卡片流程第 2 步，别跳）**：全仓 grep 提示词命中 8 个文件，逐个看原文后确认**只有 5 处是真提示词**，另 3 处是假阳性，**不要迁**：`src/cn/offline_agent.py`（只是 re-export）、`src/cn/real_benchmark.py`（import）、`src/cn/documents/guard.py:138`（注入检测正则 `en_system_prompt`，它是安全守卫，把它"注册"进提示词库会张冠李戴）。真提示词是：research agent 的基座 + RUN CONTEXT + PRE-FLIGHT 段、ablation agent 的另一套基座 + RUN CONTEXT + no-tools 段、CARD-05 提取器的 system 与 user 两条、CARD-10 备忘录叙述、每日复盘题材润色的 system 与 user 两条 —— 共 **11 段**（原估 5 处，是因为 system/user 与拼接段当时还算在一块）。 |
| 2026-09-27 | **实现**。新建 `src/agents/prompts/`：`registry.py`（`PromptSpec` + `load_registry` / `render` / `record_usage` / `prompt_bundle_hash` / lock 服务 / `registry_table`）+ `templates/<name>.<version>.md` + `registry.lock.json` + `__main__.py`（`python -m src.agents.prompts --write-lock --check`）。三条设计选择值得记：<br>① **占位符用 `{{var}}` 而非 `str.format`**：备忘录叙述提示词里原样嵌着 JSON schema（`{"sentence_span": str, …}`），`str.format` 会把那些单括号当字段而直接报错。<br>② **`leading_newline` / `trailing_newline` 是显式声明**：`cn_claim_extractor_system` 本身以换行结尾、`cn_research_agent_preflight` 以换行开头，靠"文件末尾多一个空行"表达会让 `git diff --check` 报 `blank-at-eof`，且任何人顺手整理文件都会静默改掉提示词。所以正文两侧的空白一律当**包装**剥掉，真正的换行由 frontmatter 声明。<br>③ **变量在加载期就对齐**：声明的变量集合必须等于正文里出现的占位符集合，两处不一致直接拒载 —— 半渲染的提示词比没有提示词更坏。 |
| 2026-09-27 | **迁移是逐字迁移，且可被证明**。先跑 `tmp/probe_prompt_golden.py` 抓迁移前每段提示词的 sha256（含 8 个端到端 composite），提交为 `tests/fixtures/cn/prompt_golden_hashes.json`；迁移后再由 `tests/test_cn_prompt_migration.py` 从**接线后的代码**重新推导并逐字比对。夹具里带变量的条目记的是**模板体**（占位符保留 `{{name}}`）并附 `rendered_with`，片段条目可由 composite 精确切分还原（切分本身也被断言），所以"叶子"不可能单独漂移。核查期用 `tmp/check_prompt_templates.py` 逐条比对，11 段全部一次对上。 |
| 2026-09-27 | **§8 edge 3 的 Skill 取舍：不建**。卡 §1 的原话是"Skill 不做空壳：只有存在真实功能价值才建，否则只交付 Prompt Registry"。判断依据：本仓库的"技能"是**给 agent 用的工作流知识**（`.workbuddy/skills/` 下那几份），而提示词注册表是**运行时被代码调用的数据**——`render()` 在每一个 agent step 上被调用，装进 Skill 目录只会让它离开 import 路径、把 fail-closed 变成"找不到就静默降级"。两者形态不同，硬凑成一个目录是**为名词而建**。**什么条件下值得建**：若将来出现"需要按任务类型挑选/组合提示词"的编排需求（例如同一段服务两种口径策略），那时 Skill 才是正确的抽象层，届时另立卡。此结论同步写进 `COMPETITION_REQUIREMENTS_MATRIX.md` 的 Skill 行。 |
| 2026-09-27 | **§10 三条闸门落地方式**。① 零硬编码 → 见 §15 证据 1；② hash 入 manifest → `RunConfig.prompt_names`（新增可选字段，`None` 时行为与从前完全一致）+ `RunRecorder.start()` 经 `prompt_bundle_hash` 计算，**显式传入的 `prompt_hash` 仍然优先**；`real_benchmark` 的元数据与 `scripts/run_benchmark.py` 的 `prompt_version/prompt_versions/prompt_hash` 都改为同一来源；③ 版本只增不改 → `registry.lock.json` 冻结每个已发布版本的 `prompt_hash` 与变量集，`verify_lock()` 在测试里跑；改法只有一条 —— **加新版本文件**，`main()` 的 `--check` 会退回非零码并打印漂移项。 |
| 2026-09-27 | **诚实点（写进页面而不是藏起来）**：演示 run 的 manifest `prompt_hash` 是 `null`，**因为这条 run 只做解析/归一化/计算/校验，一个字都没发给模型** —— 空值是对该 run 的真实陈述，不是缺件。同理，注册表页的"本次进程使用记录"在只跑确定性页面的进程里就是 0 条。生产路径里真的发送提示词的 run 是**真实模型基准**（`RealBenchmarkRunner`，需授权与预算才能跑），其元数据已带 `prompt_names` 与组合 `prompt_hash`。`config_fingerprint` 的取值**不因本卡改变**（`SYSTEM_PROMPT` 只是换了来源，字节未变）。 |
