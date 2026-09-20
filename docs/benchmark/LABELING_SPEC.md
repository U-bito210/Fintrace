# CARD-11 真值标注规范（Real / Holdout 集）

> 本文件是 `benchmarks/cn_agent_v2.json` 中 `real` / `holdout` 两级的标注契约。
> 合成集的真值由 `scripts/gen_document_fixtures.py` 生成（`tests/fixtures/documents/gold/`），
> 不由本规范约束。

## 1. 为什么需要一份规范

CARD-11 §8 边缘 2 说得很直白：**人工标注量有限，宁小勿错**。
一条错的真值比一百条缺失的真值危害大得多——它会把一个正确的实现判成错的，
而且这个错误会随报告一起进答辩材料。所以本规范优先定义「什么时候**不**标注」。

## 2. 目录纪律（物理隔离）

```
data/benchmark_real/
  manifest.sha256                  # 文件 sha256 清单（入库的只有这个清单模板）
  real_acquisition.json            # 采集记录：来源 URL / 抓取时间 / sha256 / 授权说明
  real_<document_id>.pdf           # Real 集原文
  real_<document_id>.labels.json   # Real 集真值
  real_holdout_<document_id>.pdf   # Holdout 集原文（前缀强制 real_）
  real_holdout_<document_id>.labels.json
```

* 所有 Real / Holdout 文件名**必须**以 `real_` 开头。`benchmark_v2.isolate_payload()`
  在写报告时机械校验这一点——不是约定，是闸门。
* `data/` 整体在 `.gitignore` 中。**版权材料永不入 Git**，只入 sha256 清单与采集记录。
* Holdout 在收口评测前不参与任何开发调试。这是流程纪律，工具只能做到目录隔离；
  做不到的部分必须在答辩材料里如实说明。

## 3. 采集

```
$PY scripts/fetch_benchmark_real.py --symbol 600519.SH --document annual --year 2024
$PY scripts/fetch_benchmark_real.py --verify        # 只校验 sha256，不下载
```

* 多源：巨潮资讯 → 上交所/深交所 → 公司官网，逐一尝试。
* 任一下载后**必须**通过 sha256 校验写入 `real_acquisition.json`；校验失败**报错停止**，
  不写入、不降级（CARD-11 §8 边缘 1）。
* 无网络 / 无授权时脚本以非零码退出并打印原因，**绝不生成占位文件**。

## 4. 真值格式

```json
{
  "schema_version": 1,
  "document_id": "600519.SH_2024_annual",
  "source_kind": "real",
  "annotator": "<谁标的>",
  "annotated_on": "YYYY-MM-DD",
  "source_url": "https://...",
  "sha256": "<pdf 的 sha256>",
  "license_note": "公开披露文件，仅用于本评测，不再分发",
  "records": [
    {
      "document_id": "600519.SH_2024_annual",
      "metric": "营业收入",
      "period": "2024FY",
      "expected_value": 173834000000.0,
      "unit": "元",
      "scope": "合并",
      "page": 42,
      "expected_evidence_ids": []
    }
  ]
}
```

字段含义与 CARD-11 §5 一致：

| 字段 | 必填 | 说明 |
|---|---|---|
| `document_id` | ✅ | 与 PDF 文件名中的 `<document_id>` 一致 |
| `metric` | ✅ | 报告里的**原始行项名**（中文原文），不是规范化后的键名——标注对象是"文件里印了什么" |
| `period` | ✅ | `2024FY` / `2024H1` / `2024Q3` / `2024-12-31`（点时值用日期） |
| `expected_value` | ✅ | 原报表口径的数值，**不做单位换算**；元为基准 |
| `unit` | ✅ | 原文单位（元 / 万元 / 千元） |
| `scope` | ✅ | 合并 / 母公司 |
| `page` | ✅ | PDF 页码（1 起），必须能回查 |
| `expected_evidence_ids` | ❌ | 仅当该条同时进 Fig. evidence 评测时填写 |

## 5. 什么情况下**不**标注（宁小勿错）

1. **扫描页无文字层**：数字只能靠 OCR 猜 → 不标，并在 `notes` 说明页码。
2. **同一行项在同一页出现多次且口径不同**（如合并/母公司分栏）：分别标注，各带 `scope`；
   无法确定 scope → 不标。
3. **报表口径与附注口径不一致**：以报表为准；附注数不标。
4. **重述（restated）**：只标本期报告披露口径，并在 `notes` 注明存在重述；
   不要同时标上一期原披露值——那属于 CARD-04/25 的版本语义，不属于抽取真值。
5. **单位不明确**（表头写"单位：万元"但行内又有换算）：不标，直到能确定。
6. **数字带不确定性**（"约"、"不低于"）：不标。
7. **对赌/预测/管理层展望里的数字**：不标——那不是已披露事实。

## 6. 质量门槛

* Real 集先覆盖 **2–3 家公司全披露链**（年报 + 半年报 + 季报 + 关键公告 + 研报草稿），
  不追求广度。
* 每条记录必须有第二人复核；`annotator` 字段记录主标人。
* 标注完成后跑 `$PY scripts/run_benchmark_v2.py --set real --verify-labels`，
  校验格式与页码可回查性（不跑模型）。
* 争议记录进 `records` 之外的 `disputes` 数组，不进入指标计算。

## 7. 报告诚实性（CARD-11 §9）

* 指标无法计算（如无真实材料）→ 报告**保留该行**并标 `N/A（原因）`，不删除。
* 评测报告的诚实性本身是被评审的对象。
* Dry-run / mock / 合成结果**永不**以真实模型性能的名义呈现。
