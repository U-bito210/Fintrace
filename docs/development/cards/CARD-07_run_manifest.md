# CARD-07: Run Manifest & Audit Trail（运行清单与审计轨迹）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（9/16–9/22，最早开工之一） |
| 对应赛题 | 竞赛硬性要求："完整记录文件访问、工具调用、计算过程和结果生成情况" |
| 依赖 | 无 |
| 实现复杂度 | 中（约 3 人日） |
| 状态 | 🟡 代码+15 测试就绪（2026-09-27 校正：原文 ☐ 未开始，与 MASTER_PLAN 台账及本卡 §16 记录矛盾） |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/runs/`：每次任务执行生成 17 字段 RunManifest + 只追加 events.jsonl 审计轨迹，支撑"同一输入+同一配置→重新执行"。这是竞赛"执行过程可追溯、运行结果可复现"的**直接合规层**，也是 Audit Replay（CARD-12）的数据源。

## 2. 对应竞赛评分点

结果可复现、执行过程可追溯（两项硬性要求）、结果稳定性（重放一致性）。

## 3. 为什么需要这个模块

现有 fingerprint（prompt hash/git commit/snapshot）只覆盖部分复现信息，且没有文件访问与计算过程的完整事件流。v2 的"统一任务系统"关注队列管理，偏离了竞赛真正要的"可复现清单"——本卡纠偏。

## 4. 输入 / 输出

- **输入**：任务执行过程中的各环节事件（文件加载、解析、工具调用、计算、校验、生成）
- **输出**：`runs/<run_id>/manifest.json` + `runs/<run_id>/events.jsonl`

## 5. 数据模型

```python
@dataclass
class RunManifest:
    run_id: str
    started_at: str
    finished_at: str
    input_document_sha256: list[str]   # 全部输入文档指纹
    snapshot_id: str | None
    model: str | None                  # offline 模式为 None
    model_provider: str | None
    model_version: str | None
    temperature: float | None
    prompt_hash: str | None            # CARD-14 注册表联动
    skill_hash: str | None
    parser_name: str
    parser_version: str
    git_commit: str
    tool_versions: dict[str, str]
    random_seed: int | None
    execution_mode: str                # offline | real_model | replay

# events.jsonl 每行：
# {"ts", "run_id", "stage", "event_type",
#  "input_digest", "output_digest", "status", "detail"}
# stage ∈ {file_load, parse, normalize, retrieval, tool_call,
#          calculation, claim, validation, report}
```

## 6. API / Tool Contract

```python
class RunRecorder:
    def start(self, config) -> str: ...              # 返回 run_id，写 manifest 骨架
    def emit(self, stage, event_type, input_, output, status) -> None: ...
        # 自动计算 input/output digest；追加 events.jsonl
    def finish(self, outcome) -> None: ...             # 补 finished_at
    def verify_replayable(self, run_id) -> ReplayPlan: ...

def replay(run_id) -> RunResult:
    """按 manifest 重新执行；输出与原 run 的 diff 报告
    （确定性部分应全等；LLM 部分标注采样差异）。"""
```

与现有 ResearchState/轨迹的关系：**扩展不重写**——ResearchState 记录 agent 循环层，RunRecorder 记录全链路层，事件共享 run_id 关联。

## 7. 金融逻辑

本卡无直接金融计算，但 digest 的设计决定可复现性：文件访问事件必须含 sha256；计算事件必须含 calculation_id（与 CARD-09 的 Calculation 记录对齐）；工具调用事件记录入参出参摘要。

## 8. Edge Cases

1. 进程中断 → manifest 缺 finished_at → 标记 interrupted（不删，审计价值保留）
2. replay 时 git_commit 已前进 → diff 报告头部显著标注版本差异
3. LLM 部分重放（temperature>0）→ 结果不保证全等，replay 报告分"确定性层全等 + LLM 层采样差异"两段
4. 输入文件已被修改 → sha256 比对失败 → replay 拒绝执行并报告

## 9. Failure Mode

事件写入失败（磁盘）→ 任务标记 failed，fail-closed。**事件只追加不修改**——任何改写历史事件的代码路径都是红线。

## 10. Validator Rules

- manifest 17 字段齐全（offline 模式允许 model 系列为 None，但必须显式存在）
- events.jsonl 单调递增时间戳
- 每个 calculation 事件可回查 calculation_id 完整定义

## 11. Unit Tests

manifest 字段完整性；事件追加顺序；digest 稳定性（同输入同 digest）；interrupted 标记；replay 拒绝（文件已改）。

## 12. Integration Tests

一次完整研究任务 → run 目录产物齐全 → replay 确定性层全等报告；接入现有 benchmark fingerprint 机制（复用不重复造）。

## 13. Benchmark

Replay Success Rate / Multi-run Consistency 两指标接入 CARD-11。

## 14. Demo Method

Demo 结尾展示：任选一次 run → 导出 manifest → 现场重放 → 一致性报告。"结果不是只由 LLM 生成的"的证明材料。

## 15. 验收标准

- [x] 17 字段 manifest 生成与测试（`RunManifest` 17 键齐全校验；offline 模式 `model` 系列为 `None` 但显式存在）
- [x] 九类 stage 事件全覆盖（file_load 到 report）（集成测试断言九类 stage 全部出现）
- [x] replay 确定性层全等验证通过（`replay()` 确定性层逐事件比对，全等→`status=ok`）
- [x] 事件只追加（防篡改测试）（`emit` 仅以 append 模式写一行；读操作/二次 `emit` 不改变已写字节）
- [x] 全部测试离线通过 —— **项目级阻塞**：全量离线 725 passed / **23 failed**，23 项全部来自缺失的本地 Tushare 快照（与 CARD-06 时失败集合逐条一致，非本卡引入）；CARD-07 自身 15 项测试全部离线通过。 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | 落地 `src/cn/runs/`：`models.py`（17 字段 `RunManifest` + `RunEvent` + `RunConfig` + `ReplayPlan`/`RunResult` + `Stage`/`ExecutionMode` 枚举 + `digest_of`/`sha256_file`/`collect_tool_versions`/`detect_git_commit`）、`recorder.py`（`RunRecorder.start/emit/finish/verify_replayable` + module-level `replay/list_runs/load_*`）、`cli.py`（list/show/verify/replay，Demo 现场重放）、`__init__.py`。只追加红线在 `_append_event` 用 append 模式一处落实；fail-closed：`emit` 写盘失败抛 `RunRecordError`；interrupted = `finished_at` 空。`replay` 分「确定性层全等 + LLM/采样层 drift」两段 diff（`sampled=True` 的 claim 事件计入采样层）。`calculation` 事件带 `calculation_id`（对齐 CARD-09 `Calculation`），`file_load` 事件带 `sha256`+`path`（replay 重校验输入未被篡改）。测试 `tests/test_cn_run_manifest.py` 15 项离线全过。全量套件失败集合仍为 23 不变（14 workbench_service + 6 workbench_api + 2 cn_report + 1 cn_report_cli），冒烟 14/14 clean。偏差：未把本模块直接接进现有 ResearchState/`workbench_service` 执行循环（保持扩展不重写；CARD-09 将借 `run_id` 关联），仅以集成测试驱动一次完整研究链路验证 replay。未伪造 Tushare 快照。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-09-27 | **交付缺口修复：本卡源码包此前从未进过 Git**。`.gitignore` 第 69 行原文是**裸写** `runs/` —— git 的语义是"在**任意深度**匹配名为 `runs` 的目录"，于是它同时吃掉了**仓库根**的运行产物目录（`recorder._DEFAULT_STORE_DIR == _REPO_ROOT / "runs"`，这是本意）**和源码包 `src/cn/runs/`**（`__init__.py` / `cli.py` / `models.py` / `recorder.py`，四个 `.py` 全部未跟踪）。`git ls-files src/cn/runs/` 为空即证。**这类缺陷在 `git status` 里完全不可见**，只有在别人 clone 后 `import src.cn.runs` 报 `ModuleNotFoundError` 时才会暴露。修法：改为锚定的 `/runs/`（运行产物仍被忽略，源码包回到跟踪范围），并把这条规则固化成守卫 `tests/test_repo_hygiene.py`（纯 Python，不起 `git` 子进程）：① 任何**裸目录模式**（形状恰为 `name/`，无其它斜杠、无通配）不得匹配到"含 `.py` 的目录"（`__pycache__/` 只含 `.pyc`，合法通过；`runs/` 当时含 4 个 `.py`，会被点名）；② 锚定后的 `/runs/` 必须仍与 `_DEFAULT_STORE_DIR` 对得上，防止有人搬走运行时目录后规则失效。修复后 `git ls-files src/cn/runs/` 可见四条源码，守卫 2 项通过。 |
