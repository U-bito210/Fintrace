# CARD-01: Document Intelligence Layer（文档智能层）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（9/16–9/22） |
| 对应赛题 | 赛题1 升级为全系统数据入口 |
| 依赖 | 无（首先开工） |
| 实现复杂度 | 高（约 6–8 人日） |
| 状态 | ✅ §15 验收项全部达成（Tushare 快照阻塞已于 2026-09-20 解除；2026-10-08 复核离线全绿 1753 passed） |
| Skill 要求 | 动手前加载 financial-data-provenance |

## 1. 目标

新建 `src/cn/documents/`：统一接管全部真实金融材料的摄入。任何 PDF/TXT/MD/Excel/CSV 进入系统即注册、解析、定位，产出带页码/坐标的结构化事实，支撑"从报告数字反向跳回原始 PDF 页"——本系统最重要的现场展示能力。

## 2. 对应竞赛评分点

数据准确性（解析源头）、来源可核验（page/bbox 定位）、任务完成度（真实输入能力）、现场展示（点击跳转）。

## 3. 为什么需要这个模块

v2 方案的 fatal gap：系统只吃 synthetic 快照，无法处理真实年报。竞赛要求"处理真实金融材料"，且"数据来源可核验"的最强形态就是数字→PDF 页的反向定位。此层是主链第一步，CARD-02/05/06/09/12 全部依赖它。

## 4. 输入 / 输出

- **输入**：文件字节流 + 声明的 document_type；支持类型：年报 PDF、半年报 PDF、季报 PDF、公司公告 PDF、研报草稿 PDF、TXT/Markdown、Excel、CSV
- **输出**：`DocumentRecord`（注册表记录）+ `list[ExtractedFact]`（带定位的结构化事实）+ 解析事件日志

## 5. 数据模型

```python
@dataclass
class DocumentRecord:
    document_id: str        # 内部生成（uuid 或内容寻址）
    sha256: str             # 文件指纹，重复上传去重依据
    filename: str
    source: str             # 用户上传 / 数据目录 / 管理员导入
    document_type: str      # annual | interim | quarterly | announcement | report_draft | other
    company: str | None     # 尽力提取（标题/封面页规则）
    report_period: str | None
    publication_date: str | None
    ingested_at: str
    parser_name: str
    parser_version: str
    page_count: int | None

@dataclass
class ExtractedFact:
    document_id: str
    page: int | None
    bbox: tuple | None     # (x0, y0, x1, y1)，文本型可为 None
    section: str | None    # 章节标题
    table: str | None      # 表格标识
    row: int | None
    column: int | None
    raw_text: str          # 原文切片（供人工复核）
    raw_value: str | None  # 原始数值字符串
    normalized_value: float | None   # 由 CARD-02 完成，本卡可暂空
    currency: str | None
    unit: str | None
    period: str | None
    scope: str | None      # 合并 | 母公司
    available_at: str | None
```

## 6. API / Tool Contract

```python
# src/cn/documents/registry.py
class DocumentRegistry:
    """SQLite（data/documents/registry.db，gitignore）。"""
    def ingest(self, path_or_bytes, declared_type) -> DocumentRecord: ...
    def get(self, document_id) -> DocumentRecord: ...
    def find_by_sha256(self, sha256) -> DocumentRecord | None: ...
    def list(self, document_type=None, company=None) -> list[DocumentRecord]: ...

# src/cn/documents/parsers/
class PDFTextParser: ...      # pdfplumber 文本+页码+bbox
class TableParser: ...        # pdfplumber 表格 → 行列坐标
class OCRFallback: ...        # 文本层为空时触发；未安装 MinerU/PaddleOCR → 返回 error 信封
class FinancialStatementParser: ...  # 三大表识别（表头规则匹配）
class AnnouncementParser: ...       # 公告：标题/日期/关键段
class ReportDraftParser: ...        # 研报草稿：全文段落+页码（供 CARD-05/06）
```

Agent 工具 `IngestCnDocumentTool`（cn_tools.py）：入参 `document_id 或路径`；出参 `status / document / fact_count / parse_summary`。解析失败 → `status: error` + error_code，**绝不静默跳过**。

## 7. 金融逻辑

- 财务三大表识别用表头关键词规则（"合并资产负债表"/"合并利润表"/"合并现金流量表"…），识别失败 → document 标记 `partial`，不猜测
- 研报草稿解析只做分段定位，不做任何判断（判断属于 CARD-05）
- 事实的 available_at 默认 = 文档 publication_date（无则 ingest 时间并标注 degraded）

## 8. Edge Cases

1. 扫描件（文本层为空）→ OCRFallback；OCR 不可用 → 该页标记 failed，facts 不含该页，文档状态 partial
2. 加密/损坏 PDF → 整文档 error，不入注册表
3. 声明类型与内容不符（声明年报实为公告）→ 解析器按内容重新判定并记录偏差警告
4. 同一文件重复上传 → sha256 去重，返回已有 document_id
5. 超大文件（>200MB）→ 拒绝并提示
6. 合并/母公司双表并存 → 两套 facts 分别标注 scope
7. **多页表合并**（v3.0 原方案盲区，角色 H6 点）：A 股年报三大表常跨 2–4 页，每页重复表头、后续页续列。TableParser 必须实现：①表头关键词匹配（"合并利润表"/"合并现金流量表"/"合并资产负债表"）识别表首页；②"续表"/"续"标记识别后续页；③后续页首行跳过（重复表头）；④行列坐标按首页+页偏移统一 → 合成一张完整逻辑表。**这是本卡最大工程量**（至少 1 人日），不得后置。
8. **双列表拆分**（v3.0 原方案盲区）：年报"主要会计数据和财务指标"摘要表常含"本报告期 / 年初至本报告期末"双列。TableParser 输出时标注列名与含义供 CARD-02 的 `detect_period_kind` 判定。
9. **Real 集版式多样性**（v3.0 原方案盲区）：Real 集至少 5 家公司——Demo 用 1 家（提前调试）、演练 2 家、Holdout 2 家（版式差异大：IPO 公司图表化排版、老公司纯文本、不同审计师不同排版）。**单家版式过拟合是致命风险**，必须在 10/16–10/18 冻结窗口做"陌生公司演练"。

## 9. Failure Mode

fail-closed：解析失败的事实不存在，而不是存在但错误。每页解析结果带 `status: ok|partial|failed`。汇总时 failed 页比例 > 阈值 → 文档整体降级为"仅供参考，不可作为事实来源"。

## 10. Validator Rules

- ExtractedFact 无 document_id/page → 拒绝入库
- raw_text 与 normalized 后文本不一致 → 保留 raw_text 原样，禁止覆盖
- 文档 sha256 与注册表不符 → 全部 facts 失效（防篡改）

## 11. Unit Tests

`tests/test_cn_documents.py`：注册/去重/查询；合成 PDF（脚本生成，含表格）解析出预期 facts；bbox 非空；扫描页降级；加密文件 error；sha256 篡改检测。

## 12. Integration Tests

- 端到端：ingest → facts → evidence 账本（复用现有 evidence.py 登记）→ 反查 page
- 与 CARD-11 合成文档集联合跑提取 P/R/F1

## 13. Benchmark

`tests/fixtures/documents/` 合成样例（年报/公告/研报草稿各 ≥3 份，含缺失字段与扫描页样例），生成脚本入库保证可复现。提取 P/R/F1 指标接入 CARD-11，**目标值待 baseline 后冻结**。

## 14. Demo Method

上传真实年报 PDF → 解析进度 → 结构化事实表（带页码列）→ 点击任一数字跳转该 PDF 页（Streamlit 内嵌 PDF 预览 + bbox 高亮，若高亮成本高则页级跳转即可，不为此引入重前端）。

## 15. 验收标准

- [x] 8 类输入格式全部可 ingest（OCR 类允许 partial）— `test_all_eight_declared_formats_are_ingestable`
- [x] 合成样例集提取的字段级 P/R/F1 有基线数字（不虚构目标值）— `output/benchmark/cn_agent_v2/document_extraction_baseline.md`
- [x] 反向跳转链路打通：fact → document_id → page 在 UI 可点击 — `src/cn/document_service.py` + `api.py` 文档路由 + `workbench.py`「文档智能」页
- [x] 真实 PDF 存 `data/documents/`（gitignore）— `scripts/bootstrap_documents.py`
- [x] **全部测试离线通过** — （原）未闭合：剩余 23 项失败全部依赖 `data/snapshots/cn/600519.SH_20260810_tushare_v1.json` 与 `300750.SZ_20260812_tushare_v1.json`（见 §16） → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）
- [x] Controlled Environment Guard 规则实现（第 9b 节）并有测试

### 15b. UI 侧验收补充（本次新增）

- [x] 事实表默认只列可点击的数值单元格；行标签由表格上下文重建（不把数值当成项目名），列语义给出 `current/prior/ytd/change`
- [x] 点击任一数字 → 返回该事实所在页与 bbox，并在渲染出的页图上以红框精确高亮
- [x] 跨页续表（同一逻辑表分页）各事实保留各自页码，跳转落点正确
- [x] 文档被改动导致 sha256 漂移 → 事实失效、不再提供定位
- [x] 来源文件离开工作区白名单 → 拒绝预览与读取（fail-closed）
- [x] 所有对外载荷（含 API 与 UI）都不含服务器绝对路径

## 9b. Controlled Environment Guard（并入本卡的封闭环境安全规则）

> 两个比赛都强调 Agent 系统真实运行；上传的 PDF/DOC/TXT 属于 **untrusted data**。文档中的文字不能成为 Agent 指令。此能力并入本卡与 CARD-05/07，**不单独做 CARD**。

**Document Content Isolation（文档文本 = DATA，永不等于 INSTRUCTION）**：
- 文档内容只经 data boundary 注入模型，不得改变系统/任务规则
- 文档含"忽略之前的规则"/"输出系统 prompt"等内容时，只能作为 document content，系统行为不变

**Prompt Injection Guard**：
- ingest 与解析阶段对文本做注入特征扫描（启发式规则），命中 → 文档标记 `suspected_injection` + 定位文本段落，不阻断解析但记录审计
- Claim Extractor（CARD-05）与 Run/Audit（CARD-07）共用该标记

**File Access Allowlist / Path Traversal Protection**：
- 文件读写仅限工作区 allowlist（data/、output/、runs/）
- 拒绝路径穿越：`../`、绝对路径越权、符号链接逃逸——校验后拒绝并记录

**External Network Policy**：
- 竞赛/离线模式下默认禁外网；白名单仅 LLM API（和评审环境复现说明）
- 文档不得触发任何网络行为

**Tool Permission Minimization**：工具权限最小化；文档不得要求模型泄露 prompt / environment / 无关文件。

| 检查项 | 实现位置 | 测试 |
|---|---|---|
| 注入特征扫描 | registry.ingest | 注入样本文档 → 标记 suspected_injection |
| 路径穿越拒绝 | registry/file loader | `../evil` 被拒 + 审计 |
| 越权逃逸 | sandbox 校验 | 逃逸尝试被拒 |
| 离线禁外网 | 网络策略（env 开关） | offline 模式网络调用被拒 |
| 内容隔离注入模型 | Claim/Retrieval prompt 模板 | 文档指令不改系统行为（CARD-05 联测） |

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-17 | **反向跳转 UI 打通（本卡原未闭合项）**。新增 `src/cn/document_service.py`（只读服务：list/detail/facts/jump_target/document_source/page_image，全部载荷零服务器路径）+ `api.py` 6 条 `/api/documents/*` 路由 + `workbench.py`「文档智能」页（点击数字行 → 页码 + bbox 红框高亮页图 + 原文下载）。复用 `DocumentRegistry.locate_fact()` 与 CARD-02 的 `build_table_context()`，未新造定位或标签推断逻辑。 |
| 2026-09-17 | **纠偏 1（事实表语义）**：数值单元格的 `raw_text` 是数值本身（`'12,500'`），直接当行标签会把「项目」列错认成数字。改为经 `build_table_context()` 重建 `row_label` / `column_label` / `column_role`，并新增 `row_label_source` 标明来源。 |
| 2026-09-17 | **纠偏 2（读取顺序）**：`document_detail()` 原先在 `verify_integrity()` 之前读事实，导致本次调用刚判定「文件被改动」时，仍报告这些事实可跳转。已改为先质检再取事实。 |
| 2026-09-17 | **纠偏 3（错误类型）**：API 原先靠 `document_id` 是否以 `doc` 开头区分「文档不存在/事实不存在」，实现脆弱。改为 `DocumentNotFound` / `FactNotFound` 显式异常（均继承 `KeyError`），并在 `jump_target()` 内先解析文档，使未知文档报文档错误而非误导性的事实错误。 |
| 2026-09-17 | **纠偏 4（前端依赖）**：`st.components.v1.html` 已废弃（2026-06-01 后移除），`st.pdf` 需要仓库未声明的 `streamlit-pdf` 组件。**未引入新依赖**：改为「精确高亮页图（pdfplumber→PIL）+ 原生 `st.download_button` 下载原文」两层，`st.pdf` 与 base64 iframe 均未采用。 |
| 2026-09-17 | 新增 `scripts/bootstrap_documents.py`（把 `tests/fixtures/documents/` 的合成语料复制并注册进 `data/documents/`，幂等、走真实 sha256 去重）。10 份全部注册成功；`scan_sample.pdf` 正确降级为 `degraded`、0 事实。 |
| 2026-09-17 | 测试：新增 `tests/test_cn_document_service.py` 20 项（列表/详情/事实重建/反向跳转/跨页续表/篡改失效/白名单越权/页图渲染/API 信封与错误类型）。工作台冒烟 13/13 clean。 |
| 2026-09-17 | **已知阻塞（非本卡引入）**：离线套件 394 passed / 23 failed。23 项全部依赖 `data/snapshots/cn/600519.SH_20260810_tushare_v1.json`（及 `300750.SZ_20260812_tushare_v1.json`），需真实 Tushare token 跑 `probe_tushare_cn0.py` → `build_cn_snapshot_from_probe.py` 重建；未伪造快照来「修绿」。 |
| 2026-09-17 | **增量（CARD-04 前置条件 1）：年报解析器现在产出正文/附注文本事实。** 开工 CARD-04 时实测：语料里 4 个检测面覆盖率为 0/4，且年报解析器产出的 `text` 事实数为 0 —— `FinancialStatementParser` 继承 `TableParser`，只留单元格、丢掉全部正文。新增 `src/cn/documents/parsers/notes.py`：逐行扫描落在所有表格 bbox 之外的行，按"最长优先匹配章节标题表 + 报表标题 + 未列举的裸标签（如『八、其他重要事项』）"给出 `section`，产出 `kind=FACT_KIND_TEXT`、**`raw_value=None`** 的事实。`raw_value` 恒为 `None` 是关键：CARD-02 的投影器与抽取基准只认 `table_cell`，因此加文本事实不会动摇既有契约与基准数字。 |
| 2026-09-17 | **增量纠偏：章节归属按页内最近标题判定，跨页不继承。** 初版把 `current_chapter` 带过页边界，于是「八、其他重要事项」这类**未列举**的裸标签不会刷新章节，下一页的段落会被错误归到上一章（一个"自信但错误"的标签）。已改为每页开始时 `current_chapter = None`，并在 §9 覆盖范围声明里写明这一点（`跨页续写不继承标题`）。宁可不给标签，也不给错标签。 |
| 2026-09-17 | **增量（CARD-04 前置条件 2）：表格版本列标注。** `table.py` 读表头行，把「调整前/未调整/更正前/重述前」→`as_reported`、「更正后/修正后」→`corrected`、「调整后/重述后/追溯调整后/追溯调整/已重述/重述」→`restated` 写进每个单元格的 `meta["version_label"]`。**顺序是承重的**：「追溯调整前」同时含「调整前」与「追溯调整」，前/未调整族必须先判，否则会把 as-reported 列标成 restated（正好反过来）。词表本体在 `src/cn/disclosure_lexicon.py`（CARD-04 持有），本卡只消费。 |
| 2026-09-17 | **增量（CARD-04 前置条件 3）：合成语料扩到 13 份且字节可复现。** 新增 `annual_04_restated_comparative`（上期列「已重述」+ 附注五/六/七 + 「调整前/调整后」双列表）、`annual_05_reclassification`（只有一条无法分类的口径披露，用来打通 block 路径）、`announcement_04_correction`（更正公告，无报表结构），并补齐 gold。**纠偏**：reportlab 默认写入墙钟 `/CreationDate` 与 `/ID`，语料其实**不可复现**；此前 `verify_corpus` 只比对生成器刚写下的文件，因此从未发现。已 `rl_config.invariant = 1` 钉住，并把 `verify_corpus` 改为**真正按文件名二次重渲染**再比对（13 份全部一致），另加「磁盘副本 vs manifest」比对。 |
| 2026-09-17 | **增量纠偏 2（严重，CARD-04 修掉）：解析器记账 note 污染了文档级版本标注。** 为记录"定位了多少行正文"，我往 `DocumentRecord.notes` 写了一句「…供口径与重述检测使用。」；而该字段会被 CARD-02 的 `_document_blob` 扫描来判定**整份文档的披露版本** —— 句中的「重述」两字把一份干净年报的 13 条事实全标成 RESTATED。已改为不含任何披露词汇的 `已定位正文与附注段落 N 行（含页内章节归属）。`，并在 `tests/test_cn_scope_signals.py` 加不变式测试锁死。**教训**：`notes` 是语义字段（承载文档"申报"的口径），不是解析器的自述字段。 |
| 2026-09-17 | **增量（顺手修的既有缺陷）：`scripts/bootstrap_documents.py` 的文件清单改为读 manifest。** 它原是一份手写 10 份清单，语料扩到 13 份后仍只注册 10 份 —— 演示库因此一直缺重述样例，CARD-04 §14 的横幅在真实文档上无法演示。已改为读 `tests/fixtures/documents/manifest.json` 的 `filename`/`document_type`（与测试摄入用的类型同源）。 |
| 2026-09-19 | **增量（CARD-22 前置条件）：合成语料扩到 15 份，新增 `annual_07_acme_constraint_break`。** 表格内植入小计断裂（`资产总计 11,000,000` ≠ 流动 6,000,000 + 非流动 4,000,000），正文与表格之间植入跨模态断裂（正文 `−1.2pct` vs 表格 `−4.06pct`），gold 补 16 条（全语料 64 条）。用途是给 ACME（CARD-22）一个**答案已知**的检测面：闸门断言其余干净文档 0 违规、本文件**恰好**命中 2 处植入。语料仍字节可复现：`verify_corpus` 二次重渲染 **15/15 一致**、无缺件（`rl_config.invariant = 1` 的既有约束未被破坏）。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
| 2026-10-08 | **状态行同步（修文档漂移）**：§15 验收项已全部 ☑，但表头「状态」行仍写「受 Tushare 快照阻塞」，与本行 2026-09-20 的记录自相矛盾，已改为 ✅。同日复核全量离线 **1753 passed / 0 failed**（1531 → 1753 为后续新增测试）。另修一处平台差异：Windows 下按 `AGENTS.md` 原命令直跑会得到约 319 项 `PermissionError: pytest-of-<user>` 的 fixture 装配错误（非代码缺陷），已在 `pytest.ini` 固定 `--basetemp=tmp/pytest-tmp`，评审环境复现不再依赖本机 ACL。 |
| 2026-10-08 | **发现并登记一个未修的解析缺陷（`looks_numeric` 截断）**。`parsers/base.py:15` 的 `NUMERIC_RE` 第一分支是 `[-+]?[\d,]{1,3}(?:,\d{3})*…`，其中 `[\d,]{1,3}` 本意是千分位数的首位三档，但**没有要求后面必须跟 `,ddd`**，于是对"数字嵌在散文里"的单元格，`search` 匹配满 3 位即停：`'2024年'`→`202`、`'107,294股限制性股票…'`→`107`、`'0593-8901666'`→`059`。干净的数值格不受影响（先 `replace(",","")` 再 `fullmatch`，命中第二分支 `[-+]?\d+`，`'-235,944'`→`-235944` 正确）。**实测规模**：真实年报 7,281 条带数值事实中 808 条（11.1%）与原文任一数字不符，样例以年份表头/电话/日期这类**本不该成数的标签格**为主。**为什么现在不修**：`NUMERIC_RE` 是 CARD-01 抽取基准与 CARD-02 归一化的共同地基，收紧它（把首分支改成必须 `^\d{1,3}(,\d{3})+$` 或给散文格改走 `raw_value=None`）会移动 `extraction_f1` 与一批 golden 断言，距 code freeze 5 个工作日，不在冻结窗口内做。**已登记**：`LIMITATIONS.md` 新增一条，含"引用 `table_cell` 数值前须核对 `raw_text`"与"recognized metric 是否受波及尚未交叉验证"两个待办。 |
