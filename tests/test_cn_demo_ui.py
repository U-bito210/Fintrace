"""CARD-15 §15 — Demo UI 的离线回归。

三组断言，对应卡的三个硬项：

* **挂载面**：8 个 Demo 页面被挂到既有工作台上，且 14 个既有页面一个不少。
  卡 §6 要求"在现有入口追加挂载，不修改既有页面函数"；悄悄少掉几个页面
  比报错更糟，所以这里按标签逐一钉死，而不是只数个数。
* **服务层契约**：``src.cn.demo_service`` 的公开函数**永不抛**，缺输入返回
  ``unavailable`` 信封。卡 §9「页面崩溃不影响后端任务」与 §15「浏览器控制台零
  报错」都在这一条上 —— "语料里没有这份东西"是正常起点状态，不是 traceback。
* **渲染**：每个 Demo 页面在 streamlit 的 ``AppTest`` 下渲染无异常。

另有一条容易漏掉的锁：信封的 ``status`` **不得吞掉引擎自己的 ``status``**。
CARD-22/24/25 的载荷里也有一个叫 ``status`` 的字段（含义是结论状态，不是调用
成败），早期实现直接覆盖，于是"引擎说自己没法判定"被静默改写成"一切正常"。

全部离线：只跑确定性引擎，无模型、无网络、无 ``data/`` 快照依赖。既有 14 个页面
的逐页渲染由 ``scripts/smoke_workbench.py`` 覆盖，这里只验证"它们还在导航里"
（避免每次跑测试都额外起十几次 Streamlit 运行时）。

H 组是唯一的例外，且是**显式例外**：真实材料（``data/demo``）不进仓库，所以
"清单里的用例都能跑"「默认用例能跑」这些判据在本地/离线都成立，而真的跑一遍
真实年报（分钟级）标了 ``integration`` —— 默认套件不跑，材料在的机器上必须能跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cn import demo_service as service  # noqa: E402
from workbench.demo import mount_demo_pages, page_labels  # noqa: E402
from workbench.demo import _shared  # noqa: E402
from workbench.demo import graph as graph_page  # noqa: E402

WORKBENCH = ROOT / "workbench.py"

#: CARD-15 §6 —— 挂载前就存在的页面。挂载不得动它们。
LEGACY_PAGES = (
    "案例演示", "AI Agent 研究", "研究总览", "市场与行情", "财务表现", "财务分析",
    "同行估值", "证据与校验", "文档智能", "Agent 执行轨迹", "研究报告", "研究任务",
    "评测与消融", "每日复盘",
)

#: CARD-15 §5（已按 DEMO_FLOW.md v3.2 校正）—— 顺序即导航顺序。
DEMO_LABELS = (
    "研报纠错", "ACME 约束", "结论护照", "主张-证据图谱",
    "逻辑脆弱性", "时间重验证", "审计回放", "对抗注入",
)

#: 页面 → 支撑卡片。三个创新展示步骤（4b/7b/10b）必须各自有页面 ——
#: 这正是旧版 §5 页面清单缺掉、而 DEMO_FLOW v3.2 要求的部分。
DEMO_CARDS = {
    "研报纠错": "05",
    "ACME 约束": "22",
    "结论护照": "23",
    "主张-证据图谱": "09",
    "逻辑脆弱性": "24",
    "时间重验证": "25",
    "审计回放": "12",
    "对抗注入": "26",
}

STATUSES = {service.STATUS_OK, service.STATUS_UNAVAILABLE}


@pytest.fixture(autouse=True)
def _clean_service_cache():
    """每个用例前后都丢弃缓存，避免注册表句柄跨用例泄漏。"""
    service.reset()
    yield
    service.reset()


# --------------------------------------------------------------------------- #
# H. 真实材料草稿（DEMO_FLOW §1/§2）—— 清单里的用例必须真的能跑
# --------------------------------------------------------------------------- #
def test_every_listed_draft_case_resolves_to_a_spec():
    """列出来的草稿必须都有可跑的规格，规格指向的文件也必须真的在。

    这里钉的是一个真发生过的缺陷：真实材料案例只加进了清单，引擎侧没有对应规格，
    于是纠错页的默认选项静默变成 ``unavailable`` —— 页面不报错、只是没内容。
    这种「不崩但没内容」比崩溃难发现得多，所以它必须是一条会失败的测试。
    """
    for case in service.draft_cases():
        spec = service.draft_spec_for(case["key"])
        assert spec is not None, f"{case['key']} 列在清单里但没有可跑的规格"
        assert case.get("runnable", True), f"{case['key']} 自己标了不可跑却还在清单里"
        if case.get("source") == "data/demo":
            assert service.DEMO_DRAFT_PATH.exists() and service.DEMO_ANNUAL_PATH.exists()
        else:
            assert (service.DRAFTS_DIR / case["filename"]).exists(), case["filename"]


def test_default_draft_is_listed_and_runnable():
    """默认草稿必须是清单里的一员，且能跑 —— 页面默认值与自检值同源。"""
    default = service.default_draft_key()
    keys = [case["key"] for case in service.draft_cases()]
    assert default in keys, f"默认草稿 {default} 不在清单里"
    assert service.draft_spec_for(default) is not None
    assert service.corrections("__no_such_draft__")["status"] == service.STATUS_UNAVAILABLE


def test_real_material_case_is_omitted_when_files_are_missing(tmp_path, monkeypatch):
    """真实材料缺任一份就整案不下发，默认值退回合成件 —— 不假装有。"""
    monkeypatch.setattr(service, "DEMO_ANNUAL_PATH", tmp_path / "annual_report.pdf")
    monkeypatch.setattr(service, "DEMO_DRAFT_PATH", tmp_path / "report_draft.md")
    assert service.demo_material_ready() is False
    assert service.demo_draft_spec() is None
    keys = [case["key"] for case in service.draft_cases()]
    assert service.DEMO_DRAFT_ID not in keys
    assert service.default_draft_key() != service.DEMO_DRAFT_ID
    # 退回合成件之后，清单里每一个用例仍然可跑。
    for key in keys:
        assert service.draft_spec_for(key) is not None, key


def test_align_planted_matches_by_value_not_only_type():
    """同一 check_type 命中多条时，只按类型对齐会把命中数摊到每一处植入上。"""
    findings = [
        {"check_type": "numeric_error", "original": "352,012,554千元", "evidence_ids": ["ev_1"]},
        {"check_type": "numeric_error", "original": "31,000,000亿元", "evidence_ids": []},
        {"check_type": "missing_citation", "original": "11.58元", "evidence_ids": []},
    ]
    planted = [
        {"check_type": "numeric_error", "match_value": "352,012,554", "note": "E1"},
        {"check_type": "numeric_error", "match_value": "999,999", "note": "对不上的那处"},
    ]
    aligned = service.align_planted(planted, findings)
    assert [row["检出"] for row in aligned["rows"]] == [1, 0]
    assert aligned["rows"][0]["命中证据"] == "ev_1"
    assert aligned["rows"][1]["状态"] == "**漏检**"
    # 清单之外的检出不是噪音：必须原样出现在 unmatched 里，由人去判定真伪。
    assert [item["original"] for item in aligned["unmatched"]] == ["31,000,000亿元", "11.58元"]


def test_align_planted_falls_back_to_type_only_for_synthetic_specs():
    """合成规格没有 ``match_value``，对齐必须退化为按类型 —— 行为不得被改动。"""
    planted = [{"check_type": "unit_error", "note": "合成件"}]
    findings = [
        {"check_type": "unit_error", "original": "700,000万元"},
        {"check_type": "unit_error", "original": "另一个单位错误"},
    ]
    aligned = service.align_planted(planted, findings)
    assert aligned["rows"][0]["检出"] == 2
    assert aligned["rows"][0]["对齐口径"] == "仅按类型"
    assert aligned["unmatched"] == []


@pytest.mark.integration
@pytest.mark.skipif(
    not service.demo_material_ready(), reason="真实材料不在本机（data/ 不进仓库）"
)
def test_real_material_draft_end_to_end():
    """真实年报 + 含错草稿跑完整 CARD-05 链路，并把当前检出状态钉死。

    这条要解析 229 页年报（分钟级），所以标 ``integration``：默认套件不跑它，
    但**材料在的机器上必须能跑**，且数字变了就得重新核对 —— 不允许悄悄多检出
    两条，也不允许悄悄漏掉一条。
    """
    view = service.corrections()
    assert view["status"] == service.STATUS_OK, view.get("reason")
    assert view["doc_id"] == service.DEMO_DRAFT_ID
    assert view["source"] == "data/demo"
    assert view["reference_documents"] == ["annual_report.pdf"]
    assert view["as_of"] == service.DEMO_DRAFT_AS_OF
    assert view["build_seconds"] > 0
    assert view["draft_document_id"]

    rows = list(view["aligned"]["rows"])
    assert len(rows) == len(service.DEMO_PLANTED) == 8
    detected = [row for row in rows if row["检出"]]
    # 已记录的现状：8 处注入里检出 3 处（E1/E2/E8）。§5 的 8/8 硬项**未达标**，
    # 页面与剧本自检都照实说。数字变了就说明引擎或草稿变了 —— 必须重新核对。
    assert [row["植入类型"] for row in detected] == [
        "numeric_error", "numeric_error", "scope_error",
    ]
    # 清单之外的检出必须有出口，不能被吞掉（本材料当前 19 条）。
    assert len(view["aligned"]["unmatched"]) >= 1
    # 每条检出都要能指回证据或草稿页，否则「可回跳」不成立。
    for item in view["findings"]:
        assert item.get("evidence_ids") or item.get("claim_page") is not None
    # 第二次调用走缓存，不重复解析。
    again = service.corrections()
    assert again["build_seconds"] == view["build_seconds"]


# --------------------------------------------------------------------------- #
# A. 挂载面
# --------------------------------------------------------------------------- #
def test_workbench_stays_a_script_with_a_demo_package_beside_it():
    """``workbench.py`` 是 ``streamlit run`` 的脚本，旁边的 ``workbench/`` 只放新页面。

    两者同名不冲突的前提是**没人 import workbench 这个模块**（脚本按路径执行）。
    所以这里锁的是"文件仍在 + 包只含 Demo 页面"，不是"包不遮蔽文件"。
    """
    assert WORKBENCH.is_file()
    demo_pkg = ROOT / "workbench" / "demo"
    assert (demo_pkg / "__init__.py").is_file()
    submodules = {path.stem for path in demo_pkg.glob("*.py")}
    assert {"corrections", "acme", "passport", "graph", "fragility", "temporal",
            "replay", "adversarial"} <= submodules


def test_demo_pages_are_mounted_in_script_order():
    pages = mount_demo_pages()
    assert [page.label for page in pages] == list(DEMO_LABELS)
    assert all(callable(page.render) for page in pages)


def test_every_demo_page_declares_its_card_and_script_step():
    pages = {page.label: page for page in mount_demo_pages()}
    assert {label: page.card for label, page in pages.items()} == DEMO_CARDS
    for page in pages.values():
        assert page.step.strip(), f"{page.label} 没有标注剧本步骤"


def test_innovation_steps_have_their_own_pages():
    """DEMO_FLOW v3.2 声明的三个创新展示步骤（4b/7b/10b）必须各有页面。"""
    steps = {page.label: page.step for page in mount_demo_pages()}
    assert "4b" in steps["ACME 约束"]
    assert "7b" in steps["结论护照"]
    assert "10b" in steps["逻辑脆弱性"]


def test_page_labels_matches_mount_and_is_repeatable():
    first = page_labels()
    second = page_labels()
    assert first == second == list(DEMO_LABELS)


def test_demo_labels_do_not_collide_with_legacy_labels():
    """标签重复会让侧栏 radio 直接炸掉 —— 挂载时必须无重名。"""
    combined = list(LEGACY_PAGES) + list(DEMO_LABELS)
    assert len(combined) == len(set(combined))


# --------------------------------------------------------------------------- #
# B. 服务层契约
# --------------------------------------------------------------------------- #
def test_catalog_lists_filings_and_drafts():
    catalog = service.catalog()
    assert catalog["status"] == service.STATUS_OK
    keys = [item["key"] for item in catalog["filings"]]
    assert service.DEFAULT_FILING_KEY in keys
    assert service.DEFAULT_ACME_KEY in keys
    assert catalog["drafts"], "草稿语料清单不该为空"


def test_missing_corpus_returns_unavailable_instead_of_raising():
    """未知案例 / 未知草稿一律返回 ``unavailable`` 信封，绝不抛。"""
    cases = (
        ("corrections", lambda: service.corrections("__no_such_draft__")),
        ("graph_view", lambda: service.graph_view("__no_such_case__")),
        ("acme_view", lambda: service.acme_view("__no_such_case__")),
        ("passport_view", lambda: service.passport_view("__no_such_case__")),
        ("verify_view", lambda: service.verify_view("__no_such_case__")),
        ("fragility_view", lambda: service.fragility_view("__no_such_case__")),
        ("temporal_view", lambda: service.temporal_view("__no_such_case__")),
    )
    for name, call in cases:
        payload = call()  # 不得抛出
        assert payload["status"] == service.STATUS_UNAVAILABLE, name
        assert payload.get("reason"), f"{name} 报缺必须给出原因"


def test_unknown_injection_and_jump_targets_are_reported_not_raised():
    assert service.run_injection(9999)["status"] == service.STATUS_UNAVAILABLE
    assert service.jump("__no_such_case__", "ev_x")["status"] == service.STATUS_UNAVAILABLE
    image, reason = service.render_page("__no_such_case__", 1)
    assert image is None and reason, "渲染失败要给原因，不是空字符串"


@pytest.mark.parametrize(
    "call",
    [
        lambda: service.catalog(),
        lambda: service.corrections(),
        lambda: service.graph_view(),
        lambda: service.acme_view(),
        lambda: service.passport_view(),
        lambda: service.verify_view(),
        lambda: service.fragility_view(),
        lambda: service.temporal_view(),
        lambda: service.adversarial_view(),
        lambda: service.injections_catalog(),
        lambda: service.runs_catalog(),
    ],
)
def test_public_views_always_return_a_status_envelope(call):
    payload = call()
    assert isinstance(payload, dict)
    assert payload["status"] in STATUSES


def test_envelope_keeps_the_engine_status_separate_from_the_call_status():
    """信封 ``status`` 与引擎 ``status`` 必须共存。

    CARD-25 在默认案例上就返回 ``partial``（有一支证据没能重算）。如果信封把
    它覆盖成 ``ok``，"没算全"就变成了"算完了" —— 所以这里同时断言两者存在且
    不相同。
    """
    temporal = service.temporal_view()
    assert temporal["status"] == service.STATUS_OK
    assert "revalidation_status" in temporal
    assert temporal["revalidation_status"] != service.STATUS_OK

    fragility = service.fragility_view()
    assert fragility["status"] == service.STATUS_OK
    assert fragility.get("fragility_status")
    # 语料解析出的 claim 全是 fact，没有 inference —— 必须如实报 declared。
    assert fragility["thesis_source"] == "declared"


def test_temporal_requires_a_same_metric_cross_period_pair():
    """挑不出"同口径上新的顶掉旧的"，就如实报缺，而不是硬凑一对假血缘。"""
    ok = service.temporal_view("equity")
    assert ok["status"] == service.STATUS_OK
    assert ok["old_id"] and ok["new_id"] and ok["old_id"] != ok["new_id"]

    missing = service.temporal_view("profit_continued")
    assert missing["status"] == service.STATUS_UNAVAILABLE
    assert "替代" in missing["reason"]


def test_adversarial_keeps_no_sample_rows_as_na_string():
    """空分母的召回是字符串 ``"N/A"`` —— 不是 ``None``，也不折叠成 ``0.0``。

    折叠成 0.0 就分不清"这类错误查不出来"和"语料里根本没有这类错误"；删掉整行
    则等于假装这类错误不存在。
    """
    payload = service.adversarial_view()
    assert payload["status"] == service.STATUS_OK
    per_type = payload["per_error_type"]
    assert per_type, "逐错误类型读数不该为空"

    for name, row in per_type.items():
        recall, n = row.get("recall"), row.get("n")
        if n:
            assert isinstance(recall, float) and 0.0 <= recall <= 1.0, name
        else:
            assert recall == "N/A", f"{name} 空分母应为 'N/A'，实为 {recall!r}"

    # 现有语料上确实有两类没有合格声明 —— 钉住它们，防止"悄悄删行"。
    assert per_type["ytd_vs_single"]["recall"] == "N/A"
    assert per_type["valuation"]["recall"] == "N/A"


def test_adversarial_exposes_excluded_corpus_and_attribution():
    payload = service.adversarial_view()
    assert payload["excluded_claims"] >= 0
    assert payload["skipped_count"] >= 0
    attribution = payload["extractor_vs_checker"]
    assert "extractor_failures" in attribution and "checker_failures" in attribution


def test_injections_catalog_is_renderable():
    catalog = service.injections_catalog()
    assert catalog["status"] == service.STATUS_OK
    items = catalog["items"]
    assert len(items) >= 7, "CARD-12 的七场景必须都在册"
    for item in items:
        assert item["scenario_id"] and item["title"]
        assert item["injection"] and item["expected_behavior"]


def test_recorded_run_can_be_replayed_and_packed(tmp_path, monkeypatch):
    """录制 → 回放 → 导出，整条 CARD-12 链在临时目录里跑通（不碰 output/）。"""
    monkeypatch.setattr(service, "RUN_STORE_DIR", tmp_path / "runs")
    monkeypatch.setattr(service, "PACK_DIR", tmp_path / "packs")

    assert service.runs_catalog()["status"] == service.STATUS_OK

    recorded = service.record_demo_run()
    assert recorded["status"] == service.STATUS_OK, recorded.get("reason")
    run_id = recorded["run_id"]

    replay = service.replay_view(run_id)
    assert replay["status"] == service.STATUS_OK
    assert replay["run_id"] == run_id
    assert replay["available_runs"] == [run_id]
    # 未记录到的阶段要露出来，不能省略。
    assert "not_recorded_steps" in replay

    pack = service.export_pack_view(run_id)
    assert pack["status"] == service.STATUS_OK, pack.get("reason")
    assert pack["run_id"] == run_id
    assert pack["zip_bytes"], "导出必须产出 zip 字节"


def test_replay_reports_missing_runs_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "RUN_STORE_DIR", tmp_path / "empty_runs")
    payload = service.replay_view()
    assert payload["status"] == service.STATUS_UNAVAILABLE
    assert payload["reason"]


def test_unknown_run_id_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "RUN_STORE_DIR", tmp_path / "runs")
    monkeypatch.setattr(service, "PACK_DIR", tmp_path / "packs")
    service.record_demo_run()
    assert service.replay_view("__no_such_run__")["status"] == service.STATUS_UNAVAILABLE


def test_reset_drops_cached_filings():
    service.graph_view("equity")
    assert service._FILINGS, "前置条件：应已建立缓存"
    service.reset()
    assert not service._FILINGS and not service._DRAFT_STATES
    # reset 之后仍可重建 —— 缓存不是一次性状态。
    assert service.graph_view("equity")["status"] == service.STATUS_OK


def test_located_evidence_can_be_jumped_to_and_rendered():
    """剧本 §0 第 3 条：重要数字都能点回原始 PDF 页，且页面可高亮渲染。"""
    graph = service.graph_view("equity")
    located = [
        node for node in graph["nodes"]
        if node["type"] == "evidence" and node.get("jumpable")
    ]
    assert located, "股本/权益年报上应至少有可定位证据"

    node = located[0]
    # 跳转所需的位置信息由页面用同一份图节点构造（这里直接调页面那个助手，
    # 而不是在测试里重写一遍 —— 重写会让两边悄悄漂移）。
    index = graph_page._evidence_index(graph["nodes"])
    target = service.jump(node["document_id"], node["id"], index=index)
    assert target["status"] == service.STATUS_OK, target.get("reason")
    assert target["page"] >= 1

    image, reason = service.render_page(target["document_id"], target["page"])
    assert image, f"页面渲染失败：{reason}"
    assert image[:4] == b"\x89PNG"


# --------------------------------------------------------------------------- #
# C. 呈现语气（§7「不美化系统的不确定性」）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("violated", "error"),
        ("blocked", "error"),
        ("partial", "warning"),
        ("stale", "warning"),
        ("ok", "success"),
        ("verified", "success"),
        ("skipped", "info"),
        ("not_recorded", "info"),
        ("N/A", "info"),
        ("", "info"),
        (None, "info"),
    ],
)
def test_tone_maps_states_without_flattering_them(status, expected):
    assert _shared.tone(status) == expected


def test_unavailable_helper_lets_ok_payloads_through():
    """``unavailable`` 助手只接管 ``unavailable`` 信封，``ok`` 载荷原样放行。"""
    assert _shared.unavailable({"status": service.STATUS_OK}) is False


# --------------------------------------------------------------------------- #
# D. 渲染（AppTest）—— 页面抛异常 = 硬失败
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def workbench_boot():
    """启动一次工作台；页面清单从这里读（唯一来源），不手抄第二份。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.run()
    assert not at.exception, f"工作台启动即报错：{at.exception}"
    return at


@pytest.fixture(scope="module")
def workbench_radio_options(workbench_boot):
    for radio in workbench_boot.radio:
        if radio.key == "workbench_page":
            return [str(item) for item in radio.options]
    pytest.fail("侧栏的页面 radio 不存在")
    return []


def test_existing_pages_survive_the_mount(workbench_radio_options):
    options = workbench_radio_options
    assert len(options) == len(LEGACY_PAGES) + len(DEMO_LABELS)
    for label in LEGACY_PAGES + DEMO_LABELS:
        assert label in options, f"{label} 从导航里消失了"


def test_demo_mount_failure_is_not_swallowed(workbench_boot):
    """导入 Demo 页面失败会被降级成侧栏告警 —— 那是**缺陷**，不是可选状态。

    ``workbench.py`` 的 try/except 是为了不让整个工作台打不开；但挂载一旦失败，
    ``NAVIGATION`` 就会少掉那 8 个标签，且侧栏会亮出告警。这里把那条告警也钉住，
    免得"降级"悄悄变成常态。
    """
    warnings = [element.value for element in workbench_boot.warning]
    swallowed = [text for text in warnings if "Demo 页面未能挂载" in str(text)]
    assert not swallowed, f"Demo 页面挂载失败被降级成告警：{swallowed}"


@pytest.mark.parametrize("label", DEMO_LABELS)
def test_demo_page_renders_without_exception(label):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(WORKBENCH), default_timeout=120)
    at.session_state["workbench_page"] = label
    # 纠错页的默认草稿是 data/demo 的真实材料（解析 229 页年报，分钟级）。逐页渲染
    # 这条测的是「页面会不会炸」，不该把真实材料拖进来：显式钉住合成件，
    # 真实材料的端到端由上一条 integration 用案例覆盖。
    at.session_state["demo_corrections_draft"] = "SYN-DRAFT-FIGURES"
    at.run()
    if at.exception:
        err = at.exception[0]
        pytest.fail(f"{label} 渲染抛出 {type(err.value).__name__}: {err.value}")
