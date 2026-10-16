# FinTrace-CN 本地运行与自测说明

这份说明面向第一次运行项目、想自己点一遍功能的使用者。推荐先跑离线演示：不需要 API Key，不访问网络，也不会把演示数字当成真实行情。

## 1. 运行前准备

需要准备：

- Python 3.11（建议使用 3.11，不要混用多个 Python 环境）；
- Git（如果项目已经下载好，可以跳过）；
- Windows PowerShell，或 macOS/Linux 终端；
- 浏览器。

在项目根目录执行：

~~~powershell
python --version
~~~

看到 `Python 3.11.x` 后再继续。若电脑同时安装了多个 Python，Windows 可以改用 `py -3.11` 替换下面命令中的 `python`。

## 2. 创建虚拟环境并安装依赖

Windows PowerShell：

~~~powershell
cd C:\path\to\FinTrace-CN
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
~~~

如果 PowerShell 阻止激活脚本，只需对当前用户放开一次权限，然后重新执行激活命令：

~~~powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
~~~

macOS/Linux：

~~~bash
cd /path/to/FinTrace-CN
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
~~~

看到命令行前出现 `(.venv)`，说明虚拟环境已启用。以后每次重新打开终端，都要先回到项目目录并重新激活它。

## 3. 生成离线演示数据

~~~powershell
python scripts\bootstrap_demo.py
~~~

正常情况下会看到“Created ... illustrative snapshots”，或提示演示快照已经存在。该脚本只生成本地、确定性的 illustrative 数据，不代表真实价格、财报或投资建议。

生成的两个核心案例是：

- `600519.SH_illustrative_demo_v1`：证据完整，可以看到估值和报告；
- `600519.SH_illustrative_missing_shares_v1`：故意缺少总股本，观察 Validator 如何阻断估值。

## 4. 启动 Streamlit 工作台（推荐）

在第一个终端执行：

~~~powershell
python -m streamlit run workbench.py
~~~

浏览器打开 [http://localhost:8501](http://localhost:8501)。如果没有自动打开，就手动复制这个地址。

建议按下面顺序点选：

1. **案例演示**：先看“成功研究：贵州茅台”；
2. **研究总览**：确认快照 ID、研究截止时间和 Provider；
3. **证据与校验**：查看价格、总股本到市值的证据链；
4. **同行估值**：查看 PE/PB/PS 及校验结果；
5. 再切换到 **Validator 阻断：缺少关键估值输入**，确认缺少总股本时不会凭空补数字；
6. 打开 **每日复盘**，注意页面会明确标出 `synthetic_demo`，它不是实时行情。

停止工作台时，在运行它的终端按 `Ctrl+C`。

## 5. 启动 FastAPI（可选）

如果还想测试 API，请新开一个终端，进入项目目录并激活同一个虚拟环境，然后执行：

~~~powershell
python -m uvicorn api:app --reload
~~~

打开 [http://localhost:8000/docs](http://localhost:8000/docs) 可以直接试请求。先访问 [http://localhost:8000/health](http://localhost:8000/health)，预期返回：

~~~json
{"status":"ok","schema_version":"workbench-api-1.0.0"}
~~~

也可以在 PowerShell 中运行：

~~~powershell
Invoke-RestMethod http://localhost:8000/api/research
Invoke-RestMethod http://localhost:8000/api/research/600519.SH_illustrative_demo_v1/summary
Invoke-RestMethod http://localhost:8000/api/research/600519.SH_illustrative_demo_v1/valuation
Invoke-RestMethod http://localhost:8000/api/research/600519.SH_illustrative_demo_v1/validation
~~~

返回结果统一包含 `status`、`data`、`meta` 和 `error`。重点看 `meta.snapshot_id`、`meta.provider`、`meta.evidence_ids` 和 `meta.validation_status`，不要只看叙述文字。

## 6. 运行离线测试

先安装开发依赖：

~~~powershell
python -m pip install -r requirements-dev.txt
~~~

运行默认离线测试：

~~~powershell
python -m pytest -q -m "not integration" -p no:cacheprovider
~~~

再运行工作台冒烟测试：

~~~powershell
python scripts\smoke_workbench.py
~~~

最后检查代码格式和工作区状态：

~~~powershell
git diff --check
git status --short
~~~

测试通过时，pytest 会显示通过数量，冒烟测试最后会显示类似 `N/N pages clean`。冒烟测试不需要先启动 Streamlit，它会自己加载工作台页面。

当前基线：`1753 passed, 3 deselected`（2026-10-08 复核，零失败）。

Windows 上默认测试会在仓库忽略的 `tmp/pytest-*` 下为**每次调用**创建独立临时目录。这避免系统临时目录 ACL 造成成百上千条 `PermissionError` 的 fixture 装配错误，也避免重复调用复用被 pytest 清理过的目录。评审方如需指定位置，仍可显式传入 `--basetemp=<目录>`；不要指向 `tmp/` 根目录，pytest 会清理其基目录。

## 7. 配置真实数据或模型（可选）

离线演示不需要密钥。若以后要测试真实 Provider 或 Agent：

1. 复制 `.env.example` 为 `.env`；
2. 只填写自己有权限使用的密钥，例如 `TUSHARE_TOKEN`、`OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`；
3. 真实联网测试要明确使用 integration 测试，并确认数据的来源、截止时间和授权范围。

不要把 `.env`、API Key、付费数据或真实输出提交到 Git。`--dry-run` 只验证工具契约和 Validator 机制，不能当成真实模型效果。

### 使用真实模型做研报 Claim 提取

竞赛主链中，模型只将草稿原句搬运为结构化 Claim；参考披露、数值计算、错误判定和证据定位均由确定性模块完成。准备好有授权的模型密钥和本地材料后，可显式运行：

~~~powershell
$env:DASHSCOPE_API_KEY = "<your-key>"
python scripts\run_llm_report_check.py `
  --model qwen-plus `
  --draft .\materials\report_draft.pdf `
  --annual .\materials\annual_report.pdf `
  --as-of 2026-10-18 `
  --out .\output\competition\llm_check.json
~~~

可重复传入 `--annual`，或使用 `--interim`、`--quarterly`、`--announcement` 增加参考披露。输出会记录模型名、Prompt 组合哈希、明确选中的参考文档 ID、结构化 Claim、规则覆盖范围、finding 和证据索引。未提供密钥、缺少参考披露或模型返回非文本时，命令会失败并说明原因；不会输出“核查通过”的替代结果。

### 真实 Tushare 快照流程

在普通 PowerShell 中执行，先确认 `.env` 里有自己的 `TUSHARE_TOKEN`：

~~~powershell
python scripts\probe_tushare_cn0.py --symbol 600519.SH --start-date 20260901 --end-date 20260923
python scripts\build_cn_snapshot_from_probe.py `
  "data\provider_probes\cn0\600519.SH_YYYYMMDD_HHMMSS" `
  --output "data\snapshots\cn\600519.SH_20260923_tushare_v1.json" `
  --research-as-of "2026-09-23T23:00:00+08:00"
python scripts\generate_cn_snapshot_report.py `
  --snapshot "data\snapshots\cn\600519.SH_20260923_tushare_v1.json" `
  --symbol 600519.SH `
  --research-as-of "2026-09-23T23:00:00+08:00" `
  --output "output\600519.SH_20260923_tushare_report.md"
~~~

批量重建清单中的股票时，建议保留每只股票的结果：

~~~powershell
python scripts\rebuild_tushare_snapshots.py --continue-on-error `
  --manifest output\tushare_rebuild_manifest.json
python scripts\audit_cn_snapshots.py `
  --output output\cn_snapshot_manifest.json `
  --markdown output\cn_snapshot_manifest.md
~~~

批量命令失败时会在 manifest 中记录 `failed`、错误信息、Provider、研究截止时间和输出路径；不会把失败的股票伪装成成功快照。工作台侧栏的“刷新本地快照”会清理缓存并重新读取清单。

### 扩展 128 只股票的 curated universe

项目内置了一份 128 只股票的分行业清单，覆盖食品饮料、家用电器、医药生物、银行、非银金融、房地产、电子、计算机、电力设备、汽车、有色金属、基础化工、石油石化、煤炭、钢铁、机械设备、建筑装饰、建筑材料、交通运输、国防军工、通信、公用事业和美容护理，共 23 个申万一级行业。**每个已覆盖行业都至少有 5 只股票**，因此任意一只股票都能拿到 4 家以上同行，PE/PB/PS 的 P25–P75 区间才有意义；行业样本不足 5 只时，同行估值会退化成单点，无法构成区间。采集按顺序执行，避免同时调用 Tushare 触发限流；已有同一研究截止日的快照会自动跳过。

**新增一个空白行业时必须一次加满 5 只。** 只往新行业里放 1 只股票，它的同行组就是空的（`industry_peers()` 返回 `[]`），估值页只会显示"没有满足要求的同行"——这正是 `tests/test_cn_universe_consistency.py` 要挡住的"看着正常、其实没用"的状态。所以扩行业是按整个行业成批加入的，而不是零散补单只。

### 新增一只股票（同时设置它的同行）

同行不是单独配置的：本项目按**申万一级行业**推导同行，`industry_peers()` 只保留同行业、同期间、且带证据的候选。所以"加股票"必须同时改两处，少改一处会让这只股票的同行静默为空：

1. `scripts/build_cn_universe.py` 的 `CURATED_UNIVERSE` —— 决定快照会被采集；
2. `src/cn/industries.py` 的 `_STOCK_INDUSTRY` —— 决定它属于哪个行业（= 哪些股票是它的同行）。

两处的股票代码集合必须完全一致，这一点由 `tests/test_cn_universe_consistency.py` 强制：它会检查一一对应、代码不重复、每个已覆盖行业至少 5 只、以及每只股票都能得到非空同行组。加完代码后按上面 `--only <代码>` 的方式单独采集，再跑一次 `audit_cn_snapshots.py` 确认新快照没有被 `blocked`。

同行估值对每只股票只采用一份快照：在研究截止日期相差不超过 3 天的候选中，优先选择截止时间最接近目标的一份；距离相同则依次优先选择截止时间较晚、采集时间较晚、快照 ID 字典序较大的一份。重复快照及最终采用的快照 ID 会记录在 `snapshot_exclusions`，避免重复计入同行数量或估值中位数。

`financial_periods` 分别披露 TTM、总股本和净资产的实际财报期间。对应期间缺失或与目标不一致的同行会被排除，并记录 `missing_financial_periods` 或 `financial_period_mismatch`。因此，行业配置了 5 只股票也不保证每个估值指标最终有 4 家有效同行，仍需检查实际样本数和置信度。

先用 dry-run 查看将要执行的股票，不会访问网络：

~~~powershell
python scripts\build_cn_universe.py `
  --dry-run `
  --research-as-of "2026-09-23T23:00:00+08:00" `
  --manifest output\cn_universe_rebuild_manifest.dryrun.json
~~~

确认清单后再执行真实采集：

~~~powershell
python scripts\build_cn_universe.py `
  --continue-on-error `
  --research-as-of "2026-09-23T23:00:00+08:00" `
  --manifest output\cn_universe_rebuild_manifest.json
python scripts\audit_cn_snapshots.py `
  --output output\cn_snapshot_manifest.json `
  --markdown output\cn_snapshot_manifest.md
~~~

也可以先只采集一只，例如：

~~~powershell
python scripts\build_cn_universe.py `
  --only 000651.SZ `
  --continue-on-error
~~~

真实运行需要 `.env` 中有 `TUSHARE_TOKEN`。如果某只股票失败，manifest 会保留失败原因；修复网络或权限后重新运行即可，成功的股票不会重复采集。

如果批处理已经完成大部分股票，只需重试失败项，可以使用重复的 `--only`：

~~~powershell
python scripts\build_cn_universe.py `
  --only 002601.SZ `
  --only 601006.SH `
  --only 002352.SZ `
  --only 600760.SH `
  --continue-on-error `
  --research-as-of "2026-09-23T23:00:00+08:00"
~~~

空的或只有 BOM 的 CSV 会被当作不完整探针，必须重新采集；不能直接用它构建快照。

## 8. 常见问题

### `No module named ...`

通常是虚拟环境没有激活，或依赖没有安装。重新执行：

~~~powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
~~~

### 端口已被占用

可以换端口：

~~~powershell
python -m streamlit run workbench.py --server.port 8502
python -m uvicorn api:app --reload --port 8001
~~~

对应访问 `http://localhost:8502` 和 `http://localhost:8001/docs`。

### 页面显示没有快照或“未覆盖”

先点击侧栏“刷新本地快照”。如果仍没有快照，停止工作台，重新执行 `python scripts\bootstrap_demo.py`，再启动工作台。确认当前终端目录是项目根目录，而不是 `scripts` 子目录。

### 页面报“组件版本不一致”

停止旧的 Streamlit 进程，确认代码已更新，并在项目根目录重新执行 `python -m streamlit run workbench.py`。

### pip 下载失败

项目依赖已配置官方 PyPI 备用源。仍失败时可以显式指定：

~~~powershell
python -m pip install -r requirements.txt -i https://pypi.org/simple
~~~

## 9. 最短启动流程

如果只想快速确认项目能跑起来，Windows 可以依次执行：

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts\bootstrap_demo.py
python -m streamlit run workbench.py
~~~
