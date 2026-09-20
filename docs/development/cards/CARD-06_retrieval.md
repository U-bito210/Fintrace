# CARD-06: Retrieval Engine（检索引擎）

| 属性 | 值 |
|---|---|
| 优先级 | P0 |
| 竞赛阶段 | 初赛版（基础 9/16–9/22，随文档积累增强） |
| 对应赛题 | 竞赛任务链第一环"信息检索"；赛题6 引用素材 |
| 依赖 | CARD-01（文档 chunk 来源） |
| 实现复杂度 | 中（约 3–4 人日） |
| 状态 | 🟡 代码+19 测试就绪（全量离线 **710 passed / 23 failed**，失败集合与 CARD-05 时逐条一致，均为缺失的 Tushare 快照；冒烟 14/14 clean）；向量重排按「降级标注」交付；「全部测试离线通过」待 Tushare 快照重建 |
| Skill 要求 | 动手前加载 fintrace-card-flow |

## 1. 目标

新建 `src/cn/retrieval/`：对文档语料（年报 MD&A、公告、研报段落）建立证据化检索。BM25 **自研实现**（确定性优先），可选向量重排（显式 model-dependent）。修正 v2 的关键错误：**chunk ≠ evidence，只有被引用的 chunk 才入账**。

## 2. 对应竞赛评分点

信息检索（任务链第一环）、引用管理（quote/paraphrase/inference 三型）、结果稳定性（同查询同结果）。

## 3. 为什么需要这个模块

竞赛任务描述把"信息检索"列为智能体第一任务；备忘录的"业绩变动原因"需要检索 MD&A 段落做有据引用。市面方案是向量 RAG 黑盒，本项目用"确定性检索优先"对齐可复现主线。

## 4. 输入 / 输出

- **输入**：文档 chunk（CARD-01 解析产物，带 document_id/page/section）+ 查询字符串
- **输出**：`SearchHit[]`（带出处定位与 mode 标注）

## 5. 数据模型

```python
@dataclass
class CorpusChunk:
    chunk_id: str           # 与 evidence_id 严格分离
    document_id: str
    page: int | None
    section: str | None
    text: str

@dataclass
class SearchHit:
    chunk: CorpusChunk
    score: float
    mode: str               # bm25 | hybrid
    rank: int

class Citation(str, Enum):
    quote = "quote"          # 逐字一致，校验器强制
    paraphrase = "paraphrase"  # 允许重述，必须绑定原始 chunk_id
    inference = "inference"    # 允许组合多证据，必须标记
```

## 6. API / Tool Contract

```python
# bm25.py —— 自研，不引第三方 BM25 库
class Bm25Index:
    """k1=1.5, b=0.75（模块顶部常量+依据注释）。jieba 分词（MIT）。
    同索引同查询 → 结果逐项相等（确定性测试）。"""

# 正确的证据流（修正 v2）：
# Document → Chunk → Index → Retrieval → Selected Chunk → Evidence
# 只有真正用于支持 Claim 的 chunk 才调 ledger.register() 生成 evidence_id
def cite_chunk(chunk_id, citation_type, span) -> str: ...
```

Agent 工具 `SearchCnCorpusTool`：入参 `query / top_k`；出参 `status / hits[]`（每 hit 含 text/page/section/document_id，**不含 evidence_id——引用时才生成**）。

## 7. 金融逻辑

- 检索是"定位"不是"生成"：命中文本只能被逐字引用（quote 校验）或显式 paraphrase
- 检索文本中的数字不得直接进入结论——必须走数值校验路径（CARD-02/03）
- 向量重排（可选增强）：走 src/llms 国产 embedding 接口，SQLite 缓存 keyed by (model, text_hash)，未配置凭证自动降级 bm25 并标注 mode

## 8. Edge Cases

1. 空语料 → 空结果 + 明确提示（不报错）
2. 查询无命中 → 返回空列表而非强行近似
3. 同文档多页相似段落 → 各自独立 chunk，靠 page 区分
4. quote 校验失败（报告文本与 chunk 不逐字一致）→ validator 拒绝该引用，降级为需 paraphrase 声明
5. embedding 凭证缺失 → hybrid 自动降 bm25，结果标注 mode=bm25

## 9. Failure Mode

检索失败不影响主链其他环节（独立降级）。向量化失败 → 回退 BM25。**禁止**在降级时假装结果等价——mode 字段如实区分。

## 10. Validator Rules

- quote 型引用：报告文本与 chunk.text 逐字一致（字符级断言）
- paraphrase 型：必须携带 chunk_id 且 chunk 存在
- inference 型：必须标记且 derived_from 非空
- Evidence Ledger 中不存在未被任何 Claim 引用的检索型孤儿证据（账本卫生检查）

## 11. Unit Tests

- BM25 手工算例（5 文档小语料人工算分核对）
- 同查询两次结果逐项相等（确定性）
- quote/paraphrase/inference 校验三路
- 引用后才入账：检索 100 次不产生任何 evidence，引用 1 次产生 1 条

## 12. Integration Tests

CARD-01 文档 → 索引 → 检索 → CARD-10 备忘录引用 → ledger 只含被引用 chunk。

## 13. Benchmark

固定查询集 + 金标段落（合成语料）计算 recall@5；Citation Precision/Recall 接入 CARD-11。目标值待 baseline 后冻结。

## 14. Demo Method

检索页：输入查询 → 命中列表带页码出处；在备忘录中点击引用 → 跳转 chunk → PDF 页。

## 15. 验收标准

- [x] BM25 自研实现（k1=1.5, b=0.75，不引第三方 BM25 库）+ 手工算例通过（5 文档小语料人工算分核对，`test_bm25_hand_computed_scores` 断言 D1 分 = 1.215542）+ 确定性测试通过（`test_search_is_deterministic_for_same_query`）
- [x] chunk/evidence 分离机制有测试（检索不污染账本）：`test_searching_does_not_create_evidence` 检索 100 次产生 0 条 evidence、引用 1 次产生 1 条；命中出参不含 `evidence_id`
- [x] 引用三型校验全部有测试：quote 逐字一致强制（`test_quote_citation_requires_verbatim_text`）、paraphrase 必须绑定 chunk_id（`test_paraphrase_citation_binds_chunk_id`）、inference 必须 derived_from 非空（`test_inference_citation_requires_derived_from`）；孤儿检查 `test_citation_ledger_flags_orphan_when_chunk_removed`
- [x] 未配置 embedding 时自动降级且如实标注：`test_engine_degrades_to_bm25_without_embedding` 断言 `mode=bm25`；reranker 失败回退 BM25 并在 warning 标注
- [x] 空语料 / 无命中 / 同文档多页相似段落 等 edge case 有测试（`test_empty_corpus_returns_empty_with_warning` / `test_no_hit_query_returns_empty_list_not_approx` / `test_same_document_multi_page_similar_keeps_page_distinct`）
- [x] Agent 工具 `search_cn_corpus` 已接入 `build_cn_snapshot_tools`，出参 status/hits（每 hit 含 text/page/section/document_id，**不含 evidence_id**）+ mode + warnings
- [x] §13 基准指标已实现：`recall_at_k` / `citation_precision_recall`（目标值待 baseline 后冻结，未虚构）
- [x] **全部测试离线通过** —— （原）未闭合：剩余 23 项失败全部依赖 `data/snapshots/cn/` 下三个缺失的本地 Tushare 快照（见 §16），与 CARD-05 时失败集合逐条一致，非本卡引入 → **已于 2026-09-20 达成**（tushare 快照真实重建，全量离线 **0 failed / 1531 passed**，原 23 项阻塞解除）

## 16. 执行备注（agent 填写）

| 日期 | 记录 |
|---|---|
| 2026-09-19 | **CARD-06 落地（初赛版）**。新建 `src/cn/retrieval/`：`models.py`（CorpusChunk/SearchHit/Citation 枚举/CitationRecord/RetrievalResult）、`tokenize.py`（jieba 优先，缺失时确定性降级到空白+CJK 二元，离线可跑）、`bm25.py`（自研 BM25，k1=1.5/b=0.75，固定超参+依据注释）、`chunking.py`（CARD-01 的 ExtractedFact → CorpusChunk，按 page/section 定位，长文本重叠切窗）、`citation.py`（CitationLedger：cite_chunk 生成 evidence_id、quote/paraphrase/inference 三型校验、孤儿检查）、`rerank.py`（Reranker 协议 + Bm25OnlyReranker 默认降级）、`engine.py`（RetrievalEngine：from_registry、search 返回 mode、cite 唯一证据入口、recall_at_k/citation_precision_recall 基准）。 |
| 2026-09-19 | **核心纠偏（修正 v2）**：chunk ≠ evidence——`RetrievalEngine.search` 只定位，命中出参**不含 evidence_id**；只有 `cite()` 才把 chunk 提升为检索证据并写入 `CitationLedger`。检索 100 次 ≠ 产生任何 evidence 有测试锁死。引用账本与金融 `EvidenceLedger` 刻意分离（检索证据指向文本片段而非归一化数值），避免污染财务账本。 |
| 2026-09-19 | **工具接入**：`SearchCnCorpusTool` 入参 query/top_k（+ 可选 document_id/document_type 过滤），出参 status/hits/mode/warnings/corpus_size；注册进 `build_cn_snapshot_tools`。空查询返回 `error_code=retrieval_missing_query`。 |
| 2026-09-19 | **依赖**：卡设计指定 jieba（MIT）；已装入托管 venv 并写入 `requirements.txt`（`jieba==0.42.1`）。`tokenize.py` 对 jieba 缺失做 try/except 降级，保证无 jieba 环境离线测试仍绿。 |
| 2026-09-19 | **测试与基线**：新增 `tests/test_cn_retrieval.py` 19 项（BM25 手工算例/确定性/chunk-evidence 分离/三型校验/降级/空·无命中·多页/基准指标/工具集成）。全量离线 `710 passed / 23 failed`——失败集合与 CARD-05 时**逐条一致**（14 workbench_service + 6 workbench_api + 2 report + 1 report_cli），全部来自缺失的 Tushare 快照，**非本卡引入**；passed 净增 19 = 新测试数。冒烟 14/14 clean。 |
| 2026-09-19 | **已知未闭合**：向量重排按卡设计是「可选增强」，当前无 `src/llms` embedding 接口，故 `mode` 恒为 `bm25`，reranker 失败回退 BM25 并标注——符合「降级而非假装等价」。待 embedding provider 接入后，实现 `rerank.py` 中的真实 `Reranker`（走 `src/llms`）即可升级 hybrid，无需改动 BM25/索引/引用层。 |
| 2026-09-20 | **跨卡：tushare 快照真实重建**。用户填好 `.env` 的 `TUSHARE_TOKEN` 后，真实重建 9 个 `_tushare_v1` 快照（4 个标的 + 5 个白酒同业）→ 全量离线 **0 failed / 1531 passed / 0 skipped**。§15「全部测试离线通过」由 ☐ 转 ☑ —— 该条此前被 23 项快照缺失阻塞，与本卡代码无关。重建流程及三个坑（同业快照要一起建、bars 窗口不得越过研究时点、688836 上市日）见 `MEMORY.md`「快照重建」段。 |
