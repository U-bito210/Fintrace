# THIRD_PARTY_NOTICES.md — 第三方依赖与资产合规声明

> 生成日期：**2026-09-27** ｜ 依据 `requirements.txt` / `requirements-dev.txt` 的**锁定版本**
> 本文是 `docs/development/THIRD_PARTY_NOTICES_PLAN.md`（计划）的**执行结果**，取代该计划中所有 License 栏。
>
> **核实方式（重要）**：本文每一项的许可证均**逐个打开官方源核实**，不以训练记忆、旧文档或二手摘要为准。
> 证据来源有两类，可复算（见 §9）：
> 1. **PyPI JSON API**：`https://pypi.org/pypi/<name>/<version>/json` 的 `info.license` /
>    `info.license_expression` / `info.classifiers`；
> 2. **锁定版本自带的分发元数据与许可证正文**：已安装发行版 `*.dist-info/METADATA` 的
>    `License-Expression` / `License` / `License-File` 头，以及 `*.dist-info/licenses/` 下随包分发的
>    LICENSE 正文（这是与锁定版本**一一对应**的物证，优于任何在线摘要）。
> 当 ① 只给出模糊值（如 tushare 的 `BSD`、reportlab 的 `BSD license (see license.txt for details)`）
> 时，以 ② 的**许可证正文条款数**判定具体变体。

## 0. 红线（与本声明同等重要）

1. **任何第三方代码不得被描述为自主开发。** 计划书、答辩材料、文档一律禁止。
2. **不复制未经核实的许可证信息。** 每项以提交时官方源为准，本文件记录核实日期与 URL。
3. **不进入默认安装路径的可选依赖必须显式隔离**（`§5`），默认 `pip install -r requirements.txt` 不安装它们。
4. **付费数据与版权文件不入提交包**（`§7`）。

---

## 1. 运行时直接依赖（`requirements.txt`，14 项）

| 项目 | 版本 | 来源 | License（已核实） | 修改 | 在 FinTrace-CN 中的用途 | 调用方式 |
|---|---|---|---|---|---|---|
| openai | 3.3.0 | [PyPI](https://pypi.org/pypi/openai/3.3.0/json) | **Apache-2.0** | 否 | LLM provider 客户端（`src/llms/`） | import |
| anthropic | 0.123.0 | [PyPI](https://pypi.org/pypi/anthropic/0.123.0/json) | **MIT** | 否 | LLM provider 客户端（`src/llms/`） | import |
| pandas | 3.0.5 | [PyPI](https://pypi.org/pypi/pandas/3.0.5/json) | **BSD-3-Clause** | 否 | 财务表格/时序数据处理 | import |
| tushare | 1.4.29 | [PyPI](https://pypi.org/pypi/tushare/1.4.29/json) ｜ [官网](https://tushare.pro) | **BSD-3-Clause**（正文三条款；Copyright (c) 2015, 挖地兔） | 否 | A 股行情/财务 snapshot provider | provider 适配层 |
| akshare | 1.18.92 | [PyPI](https://pypi.org/pypi/akshare/1.18.92/json) ｜ [仓库](https://github.com/akfamily/akshare) | **MIT** | 否 | A 股公开数据备用 provider | provider 适配层 |
| python-dotenv | 1.2.2 | [PyPI](https://pypi.org/pypi/python-dotenv/1.2.2/json) | **BSD-3-Clause** | 否 | 本地 `.env` 凭证加载 | import |
| pydantic | 2.13.4 | [PyPI](https://pypi.org/pypi/pydantic/2.13.4/json) | **MIT**（`License-Expression`） | 否 | 数据模型校验（Evidence/Claim/Manifest） | import |
| fastapi | 0.141.1 | [PyPI](https://pypi.org/pypi/fastapi/0.141.1/json) | **MIT**（`License-Expression`） | 否 | versioned research API（`api.py`） | import |
| uvicorn | 0.52.4 | [PyPI](https://pypi.org/pypi/uvicorn/0.52.4/json) | **BSD-3-Clause**（`License-Expression`） | 否 | ASGI server | CLI |
| streamlit | 1.61.1 | [PyPI](https://pypi.org/pypi/streamlit/1.61.1/json) | **Apache-2.0**（`License-Expression`） | 否 | workbench 演示界面（`workbench.py`） | import |
| plotly | 6.9.0 | [PyPI](https://pypi.org/pypi/plotly/6.9.0/json) | **MIT**（`License-Expression`） | 否 | 图表渲染 | import |
| jieba | 0.42.1 | [PyPI](https://pypi.org/pypi/jieba/0.42.1/json) ｜ [仓库](https://github.com/fxsjy/jieba) | **MIT** | 否 | CARD-06 检索中文分词（缺失时自动降级为确定性空白+CJK 二元切分） | import（`src/cn/retrieval/tokenize.py`） |
| reportlab | 5.0.1 | [PyPI](https://pypi.org/pypi/reportlab/5.0.1/json) ｜ [官网](https://www.reportlab.com/) | **BSD-3-Clause**（正文三条款；Copyright (c) 2000-2024, ReportLab Inc.） | 否 | 文档/PDF fixture 生成与原始页面渲染 | import |
| pdfplumber | 0.11.10 | [PyPI](https://pypi.org/pypi/pdfplumber/0.11.10/json) ｜ [仓库](https://github.com/jsvine/pdfplumber) | **MIT**（Copyright (c) 2015, Jeremy Singer-Vine） | 否 | 年报文本、表格与 bbox 定位解析 | import |

> 说明：`jieba` 只被 `src/cn/retrieval/tokenize.py` 使用，且该模块在 jieba 缺失时**确定性降级**，
> 不改变离线测试结果；登记为直接依赖是因为默认路径会用到它。
>
> **不宣传为自研**：jieba 的中文分词算法不是本项目自研，计划书与答辩材料中不得出现"自研分词器"一类表述。

## 2. 开发/测试直接依赖（`requirements-dev.txt`，3 项）

| 项目 | 版本 | 来源 | License（已核实） | 修改 | 用途 | 调用方式 |
|---|---|---|---|---|---|---|
| pytest | 9.1.1 | [PyPI](https://pypi.org/pypi/pytest/9.1.1/json) | **MIT**（`License-Expression`） | 否 | 离线单元/契约/golden/API 测试 | CLI |
| pytest-asyncio | 1.4.0 | [PyPI](https://pypi.org/pypi/pytest-asyncio/1.4.0/json) | **Apache-2.0**（`License-Expression`） | 否 | 异步用例支持 | pytest 插件 |
| httpx | 0.28.1 | [PyPI](https://pypi.org/pypi/httpx/0.28.1/json) | **BSD-3-Clause** | 否 | FastAPI TestClient 传输 | import |

> 注意：`pytest-asyncio` 的许可证**不是** MIT —— 其 `License-Expression` 为 `Apache-2.0`。
> 这正是"不得凭记忆填写"的实例：该项目的许可证在历史上发生过变化，只能以锁定版本自带元数据为准。

## 3. 关键传递依赖（未直接 pin，但影响运行路径，登记以澄清边界）

| 项目 | 实测版本 | 来源 | License（已核实） | 由谁引入 | 在 FinTrace-CN 中的作用 |
|---|---|---|---|---|---|
| openpyxl | 3.1.5 | [PyPI](https://pypi.org/pypi/openpyxl/3.1.5/json) | **MIT** | `akshare`（`Required-by: akshare`） | ① akshare 的运行时依赖；② `src/cn/documents/parsers/excel.py` 解析 `.xlsx` 文档（**函数内惰性 import**，缺失时抛 `ParserDependencyError` 而非崩溃） |
| Pillow | 12.3.0 | [PyPI](https://pypi.org/pypi/Pillow/12.3.0/json) | **MIT-CMU**（`License-Expression`） | `pdfplumber`、`reportlab`、`streamlit` 三者共同引入 | `src/cn/document_service.py` 渲染披露页并画 bbox 高亮（**函数内惰性 import**，仅 UI 路径需要） |

> 二者均**不是**本项目主动声明的直接依赖，而是被 pin 住的直接依赖拉进来的；本项目只是额外使用了它们的能力。
> 因此不进 `requirements.txt`（避免把传递依赖谎报成直接依赖），但如实登记在此。
> 传递依赖闭包另含 74 项（如 `starlette` / `anyio` / `numpy` / `lxml` / `pyarrow` / `protobuf` / `pdfminer.six` …），
> 均为上表直接依赖的常规依赖，**未逐项列出**（见 §8 边界说明）。

## 4. 可选 / 隔离依赖（默认不安装，**不进入**默认安装路径）

| 项目 | 实测版本 | 来源 | License（已核实，2026-09-27） | 用途 | 隔离要求 |
|---|---|---|---|---|---|
| MinerU | 4.0.7 | [PyPI](https://pypi.org/pypi/mineru/json) ｜ [LICENSE.md](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md) | **MinerU Open Source License** = **Apache-2.0 + 附加条款**（见下方更正） | 复杂版式 PDF 结构化（可选 parser 后端） | `src/cn/documents/parsers/ocr.py` 以 `import` 探测方式惰性加载；未安装时抛 `OCRUnavailable` 并把页面标为不可解析。**不得链接其闭源在线服务。** |
| PaddleOCR | 3.7.0 | [PyPI](https://pypi.org/pypi/paddleocr/json) ｜ [LICENSE](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE) | **Apache-2.0**（正文已核，202 行标准全文） | 扫描件 OCR（可选） | 同 `ocr.py`，惰性探测加载，未安装即降级。 |

### 4.1 重要更正：MinerU **不是** AGPL-3.0

`THIRD_PARTY_NOTICES_PLAN.md` v3.1 曾把 MinerU 的许可证从"AGPL-3.0"改为"待核实"——这个"待核实"现在有了答案，
且**与原判断方向不同**：MinerU 已改为 **MinerU Open Source License**，其正文（LICENSE.md）第一段原文写明：

> MinerU is licensed under Apache License 2.0 and is subject to the additional terms below.

附加条款共两条实质约束：

1. **商业许可门槛**：商用无需单独许可；但当**你与关联方合并计算**满足以下任一门槛时，须先取得单独商业许可——
   月度活跃用户（MAU）超过 **1 亿**，或月度总收入超过 **2000 万美元**。
2. **在线服务署名义务**：基于 MinerU 向第三方提供在线服务时，必须在产品界面或公开文档中**显著标注使用了 MinerU**。
3. 未满足 1 或 2 时许可证**自动终止**（无需另行通知）。

对本项目的含义：
- 它**不是** AGPL 式的传染性 copyleft，因此"AGPL 类依赖必须隔离"这条红线**不因 MinerU 而触发**；
- 但**隔离仍然保留**——原因是它体量大、属于可选解析后端，而不是许可证传染；
- 若未来基于 MinerU 提供在线服务，**必须履行 §4.1 第 2 条署名义务**；
- 本项目当前**未安装、未调用** MinerU（仅以惰性探测占位），故上述义务目前不产生实际动作。

### 4.2 依赖可用性凭据

`MinerU` / `PaddleOCR` 的 API key（如未来使用其在线服务）属第三方服务凭据，一律走本地 `.env`，**不入库**。

## 5. 具体合规动作（按许可证类型）

| 许可证 | 义务 | 本项目如何履行 |
|---|---|---|
| MIT / BSD（含 BSD-3-Clause、MIT-CMU） | 保留版权声明与许可文本；BSD-3 额外禁止用作者名背书 | **不修改源码**（修改栏全为"否"），故以"原样分发 + 保留上游许可文件"履行；本文件列出上游版权人与来源 URL 作为可追溯声明 |
| Apache-2.0（openai、streamlit、pytest-asyncio、PaddleOCR、MinerU 的基线部分） | 保留 NOTICE 文件（若上游提供）与许可文本；标明修改 | 同上，"不修改"；上游 NOTICE 随发行版分发 |
| MinerU Open Source License | 附加的商业门槛与**在线服务署名义务**；违反即终止 | 当前不可用（未安装）；若启用且提供在线服务，须在界面/文档显著标注 MinerU（见 §4.1） |

**AGPL / GPL 类传染性依赖登记：无。** 运行路径与可选路径中均未出现 AGPL/GPL 许可证，故计划 §8 第 2 条
（"AGPL 类依赖仅存在于可选 extras"）当前**空成立**——没有此类依赖需要隔离。

## 6. 非代码资产合规

| 资产 | 处理 |
|---|---|
| 真实年报/公告 PDF、研报 | 存 `data/`（已 gitignore），**不入库、不入提交包**；演示与 benchmark 使用时在文档中注明出处与下载日期 |
| 行情 snapshot | 登记来源与 `research_as_of`；第三方数据版权归数据源，报告中只作引用 |
| LLM 输出 | 报告中标注模型/版本/温度（由 CARD-07 Run Manifest 与 CARD-14 Prompt Registry 承载），**不冒充人工分析**、不描述为自主模型能力 |
| 第三方库的能力描述 | 计划书与答辩材料中，第三方组件一律声明来源；**jieba 分词、pdfplumber 版面解析、MinerU/PaddleOCR 的 OCR 能力均不得表述为自研** |

## 7. 边界说明（哪些没有逐项登记，以及为什么）

1. **传递依赖未全列**：`requirements.txt` + `requirements-dev.txt` 的直接依赖共 17 项，其解析闭包为 76 项传递依赖。
   计划 §3.1 的登记口径是"直接依赖 + **明确影响运行**的关键传递依赖"，故只显式登记
   `openpyxl` 与 `Pillow`（本项目代码直接调用了它们），其余按上游许可证随包分发。
2. **标准库不登记**：`sqlite3`（run manifest 存储）、`hashlib`（document sha256）、`zipfile`（Evidence Pack 打包）、
   `json`/`jsonl`、`argparse` 等属 Python 标准库（PSF License），不属第三方。
3. **未纳入的工具**：不涉及 Docker 基础镜像内预装的操作系统级组件（在 `Dockerfile` 中单独声明）。

## 8. 验收

- [x] 最终 `THIRD_PARTY_NOTICES.md` 已生成，每项含**核实 URL** 与**核实日期（2026-09-27）**
- [x] 依赖清单以 `requirements.txt` / `requirements-dev.txt` 的锁定版本为准，且**实测安装版本与 pin 一致**（逐项比对，§9）
- [x] 无 AGPL/GPL 类依赖进入默认路径（当前根本不存在此类依赖，见 §5）
- [x] 计划书技术描述中无任何第三方能力被表述为自主开发（红线 §0.1、§6）
- [x] 可选依赖（MinerU / PaddleOCR）以惰性探测方式隔离，默认安装不引入
- [x] 付费数据与版权文件不入提交包（`data/` 已 gitignore）

## 9. 如何重新核实（可复算）

```bash
# 1) 抓取每个 pin 住版本在官方索引上的许可证字段（Evidence A）
$PY - <<'EOF'
import json, urllib.request
for name, ver in [("openai","3.3.0"), ("reportlab","5.0.1"), ("tushare","1.4.29")]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=30) as r:
        info = json.load(r)["info"]
    print(name, ver, info.get("license"), info.get("license_expression"))
EOF

# 2) 读取锁定版本自带的分发元数据与许可证正文（Evidence B，最贴近 pin）
$PY - <<'EOF'
from importlib import metadata
from pathlib import Path
for name in ["tushare", "reportlab", "pdfplumber", "openpyxl", "pillow"]:
    d = metadata.distribution(name)
    print(name, d.version, dict(d.metadata).get("License-Expression"),
          d.metadata.get_all("License"), d.metadata.get_all("License-File"))
    for rel in (d.metadata.get_all("License-File") or []):
        p = Path(d.locate_file(rel))
        alt = Path(d._path) / "licenses" / Path(rel).name
        p = alt if not p.is_file() and alt.is_file() else p
        if p.is_file():
            print("   ->", p, "|", p.read_text(encoding="utf-8", errors="replace").splitlines()[0])
EOF

# 3) 确认实测版本与 pin 完全一致
$PY -m pip freeze | grep -iE "^(openai|anthropic|pandas|tushare|akshare|python-dotenv|pydantic|fastapi|uvicorn|streamlit|plotly|jieba|reportlab|pdfplumber|pytest|pytest-asyncio|httpx)="
```

**核实日期**：2026-09-27 ｜ **核实环境**：Python 3.13.12（项目托管解释器），上述发行版实测版本与
`requirements*.txt` 的 pin 逐项一致。
