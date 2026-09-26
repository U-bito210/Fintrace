# THIRD_PARTY_NOTICES_PLAN.md — 第三方依赖合规计划

> 版本：v3.2 执行完成版 ｜ 2026-09-16 起草 / **2026-09-27 执行完毕**
> 本文是**计划**而非最终声明。竞赛提交（计划书、复现包、答辩材料）前，必须按 §3 核实流程逐项复核全部依赖的许可证与版本，生成最终 `THIRD_PARTY_NOTICES.md`。**在此之前，本文所有 License 栏均标注"待核实"，不以训练记忆或旧文档中的许可证描述为准。**
> v3.1 变更：MinerU 许可证从"AGPL-3.0"改为"待核实"（需以提交时官方源复核为准）。
> **v3.2（2026-09-27）：核实流程已执行完毕，最终声明见仓库根目录 [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md)。**
> 从那一天起，**许可证的权威取值以 `THIRD_PARTY_NOTICES.md` 为准**，本文的 License 栏降级为历史记录。
> 执行结果带来三处必须记住的更正：① **MinerU 不是 AGPL-3.0**，其现行许可是
> "MinerU Open Source License"（Apache-2.0 + 附加条款，见 §6）；② `pytest-asyncio` 的许可是
> **Apache-2.0** 而非 MIT；③ `pdfplumber` / `jieba` / `reportlab` 已从"计划引入"转为**实际直接依赖**，
> 本文 §4.1 已补齐。

---

## 1. 目的与红线

1. 竞赛要求作品可复现、来源可核验——依赖合规是"来源可核验"在工程侧的延伸。
2. 红线：
   - **任何第三方代码不得被描述为自主开发**（计划书、答辩、文档一律禁止）。
   - **不得复制旧文档中未经核实的许可证信息**；每项以提交时官方源（PyPI / 项目仓库 LICENSE 文件）为准。
   - **AGPL 类传染性许可必须隔离为可选依赖**，不进入默认安装路径。
   - 付费数据（tushare 积分接口等）不入提交包，仅本地 `.env` 凭证使用。

## 2. 登记模板（最终 NOTICES 逐项填写）

| 字段 | 说明 |
|---|---|
| 项目名 / 版本 | 锁定 requirements.txt 中精确版本 |
| 来源 | PyPI / 官方仓库 URL |
| License | **以官方源核实后填写**（含核实日期） |
| 是否修改 | 默认"否"；任何 patch 必须单独声明 |
| 具体用途 | 在 FinTrace-CN 中做什么 |
| 调用方式 | import 关系 / CLI / 可选 extras |

## 3. 核实流程（提交前一次性完成）

1. `pip freeze` 与 requirements.txt 比对，确认无未登记的传递依赖进入提交范围（仅登记直接依赖 + 明确影响运行的关键传递依赖）。
2. 逐项打开 PyPI 项目页 / 官方仓库 LICENSE 文件，抄录许可证名称与版本对应关系，记录核实日期与 URL。
3. 检查各许可证的归属声明要求（BSD/MIT 类需保留版权声明；Apache-2.0 需保留 NOTICE）。
4. 用 `python -m pytest -q -m "not integration" -p no:cacheprovider` 验证锁定版本下离线测试全绿。
5. 生成 `THIRD_PARTY_NOTICES.md` 随复现包提交。

## 4. 当前直接依赖登记表（License 待核实）

### 4.1 运行时依赖（requirements.txt）

**实际 14 项**（起草本计划时为 11 项；`jieba` / `reportlab` / `pdfplumber` 是后续按 §5 引入的）：

`openai 3.3.0` ｜ `anthropic 0.123.0` ｜ `pandas 3.0.5` ｜ `tushare 1.4.29` ｜ `akshare 1.18.92` ｜
`python-dotenv 1.2.2` ｜ `pydantic 2.13.4` ｜ `fastapi 0.141.1` ｜ `uvicorn 0.52.4` ｜ `streamlit 1.61.1` ｜
`plotly 6.9.0` ｜ `jieba 0.42.1` ｜ `reportlab 5.0.1` ｜ `pdfplumber 0.11.10`

每项的**许可证取值、核实 URL 与核实日期见** [`THIRD_PARTY_NOTICES.md` §1](../../THIRD_PARTY_NOTICES.md)；
本计划**不再重复登记**，以免两处漂移。

### 4.2 开发/测试依赖（requirements-dev.txt）

`pytest 9.1.1` ｜ `pytest-asyncio 1.4.0` ｜ `httpx 0.28.1` —— 许可证见 `THIRD_PARTY_NOTICES.md` §2
（其中 `pytest-asyncio` 为 **Apache-2.0**，与常见印象不同，以核实结果为准）。

### 4.3 标准库（非第三方，无需登记，列出以澄清边界）

`sqlite3`（runs/manifest 存储）、`hashlib`（document sha256）、`zipfile`（Evidence Pack 打包）、`json`/`jsonl`、`argparse`。

## 5. 计划引入依赖（引入前必须先核实并更新本表）—— 已全部结清

| 项目 | 计划用途 | 引入卡片 | 结果（2026-09-27） |
|---|---|---|---|
| pdfplumber | PDF 文本/表格解析（page + bbox 定位） | CARD-01 | **已引入** `pdfplumber==0.11.10`（MIT，随包 LICENSE.txt 已核） |
| jieba | 中文分词（检索用） | CARD-06 | **已引入** `jieba==0.42.1`（MIT）；缺失时确定性降级为空白+CJK 二元切分，离线测试不受影响。**不得宣传为自研算法。** |
| openpyxl 或 xlsxwriter | Evidence Pack 中 financials/valuation/corrections 的 `.xlsx` 导出 | CARD-12 | **已结清，结论是"不需要"**：CARD-12 的 xlsx 类产物是上游产出，本模块只把它们当**不透明字节**打包（缺件按 `CATEGORY_COMPLETENESS` 报 `incomplete`），仓库自身**只读** xlsx（`src/cn/documents/parsers/excel.py`）、**不写** xlsx。该解析路径需要的 `openpyxl` 由 `akshare` 传递引入（MIT），因此**既不新增直接依赖，也不引入 xlsxwriter**。 |

## 6. 可选依赖（默认不安装，明确隔离）

| 项目 | 计划用途 | 合规要点（2026-09-27 核实后） |
|---|---|---|
| MinerU | 复杂版式 PDF 结构化（可选 parser 后端） | **已核实：不是 AGPL-3.0。** 现行许可为 **"MinerU Open Source License" = Apache-2.0 + 附加条款**：商用免费，但**你与关联方合并计算**下 MAU > 1 亿、或月收入 > 2000 万美元时须另取商业许可；**基于它向第三方提供在线服务须显著署名 MinerU**；违反任一条则许可证自动终止。仍需惰性隔离（`src/cn/documents/parsers/ocr.py`），不进入默认路径，且不得链接其闭源在线服务。详见 `THIRD_PARTY_NOTICES.md` §4.1。 |
| PaddleOCR | 扫描件 OCR（可选） | **Apache-2.0**（LICENSE 全文 202 行已核）。同上，惰性探测隔离。 |

## 7. 非代码资产合规

| 资产 | 处理 |
|---|---|
| 真实年报/公告 PDF、研报 | 存 `data/`（gitignore）不入库不入提交包；演示与 benchmark 使用时在文档中注明出处与下载日期 |
| 行情 snapshot | 登记来源与 as-of 日期；第三方数据版权归数据源，报告中只作引用 |
| LLM 输出 | 报告中标注模型/版本/温度（Run Manifest 承载），不冒充人工分析 |

## 8. 验收

- [x] 提交前生成最终 `THIRD_PARTY_NOTICES.md`，每项含核实 URL 与日期 —— **2026-09-27 完成**（仓库根目录；每项含 PyPI JSON URL，模糊项另附随包分发许可证正文作证）
- [x] AGPL 类依赖（如保留 MinerU）仅存在于可选 extras，默认 `pip install -r requirements.txt` 不安装 —— **当前根本不存在 AGPL/GPL 类依赖**（MinerU 实为 Apache-2.0 + 附加条款，见 §6）；MinerU / PaddleOCR 仍以惰性探测隔离，默认安装不引入
- [x] 计划书技术描述中无任何第三方能力被表述为自主开发
- [x] `pip freeze` 与登记表交叉核对无遗漏 —— 实测版本与 pin **逐项一致**；17 项直接依赖 + 2 项关键传递依赖（`openpyxl` / `Pillow`，为本项目代码直接调用者）已登记，其余 74 项传递依赖按 §3.1 口径不逐项登记
