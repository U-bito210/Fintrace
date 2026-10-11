from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


OUT = Path("docs/FinTrace-CN_研报纠错核查智能体_竞赛实施计划.docx")


def set_cell_shading(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_cell_margins(cell, top=100, start=120, bottom=100, end=120):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for side, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{side}"))
        if node is None:
            node = OxmlElement(f"w:{side}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_repeat_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def set_font(run, size=10.5, bold=False, color=None):
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    run._element.rPr.rFonts.set(qn("w:ascii"), "Aptos")
    run._element.rPr.rFonts.set(qn("w:hAnsi"), "Aptos")
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def add_text(doc, text, style=None, bold_lead=None):
    p = doc.add_paragraph(style=style)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.35
    if bold_lead and text.startswith(bold_lead):
        set_font(p.add_run(bold_lead), 10.5, True)
        set_font(p.add_run(text[len(bold_lead):]), 10.5)
    else:
        set_font(p.add_run(text), 10.5)
    return p


def add_bullet(doc, text, level=0):
    p = doc.add_paragraph(style="List Bullet" if level == 0 else "List Bullet 2")
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.25
    set_font(p.add_run(text), 10.5)
    return p


def add_number(doc, text):
    p = doc.add_paragraph(style="List Number")
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.25
    set_font(p.add_run(text), 10.5)
    return p


def add_table(doc, headers, rows, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = False
    header = table.rows[0]
    set_repeat_table_header(header)
    for i, value in enumerate(headers):
        cell = header.cells[i]
        set_cell_shading(cell, "17365D")
        set_cell_margins(cell)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_font(p.add_run(value), 9.5, True, "FFFFFF")
        if widths:
            cell.width = Cm(widths[i])
    for r_idx, row_values in enumerate(rows):
        cells = table.add_row().cells
        for i, value in enumerate(row_values):
            cell = cells[i]
            if r_idx % 2:
                set_cell_shading(cell, "F2F6FA")
            set_cell_margins(cell)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.15
            set_font(p.add_run(str(value)), 8.8)
            if widths:
                cell.width = Cm(widths[i])
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return table


def add_heading(doc, text, level=1):
    p = doc.add_paragraph(style=f"Heading {level}")
    p.paragraph_format.space_before = Pt(12 if level == 1 else 8)
    p.paragraph_format.space_after = Pt(5)
    run = p.add_run(text)
    set_font(run, 15 if level == 1 else 12, True, "000000")
    return p


def add_code(doc, lines):
    table = doc.add_table(rows=1, cols=1)
    table.autofit = False
    cell = table.cell(0, 0)
    set_cell_shading(cell, "F2F4F7")
    set_cell_margins(cell, 160, 180, 160, 180)
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    for idx, line in enumerate(lines):
        if idx:
            p.add_run("\n")
        run = p.add_run(line)
        run.font.name = "Consolas"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        run.font.size = Pt(9)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def add_footer(section):
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("FinTrace-CN 竞赛实施计划")
    set_font(run, 8, False, "666666")


def build():
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.15)
    section.right_margin = Cm(2.15)
    add_footer(section)

    styles = doc.styles
    styles["Normal"].font.name = "Microsoft YaHei"
    styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    for name in ("Title", "Heading 1", "Heading 2"):
        styles[name].font.color.rgb = RGBColor(0, 0, 0)

    title = doc.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(10)
    set_font(title.add_run("FinTrace-CN 研究报告纠错核查智能体竞赛实施计划"), 18, True, "000000")
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_after = Pt(18)
    set_font(subtitle.add_run("北京市大学生金融人工智能竞赛  初赛收敛方案"), 11, False, "555555")

    add_text(doc, "本计划用于将现有 FinTrace-CN 项目收敛为一套可展示、可核验、可复现的研究报告纠错核查 Agent。项目主选赛题为研究报告纠错核查，并以财务报告分析为事实和计算基础，以估值复算作为可选加分能力。", bold_lead="本计划用于")
    add_text(doc, "核心结论：不重写已有的文档解析、财务规范化、证据链和确定性校验器；将它们收敛为由 Agent 调用的受控工具，并以一条完整的研报核查流程作为唯一主展示。", bold_lead="核心结论：")

    add_heading(doc, "一 项目定位与范围")
    add_heading(doc, "1 项目定位", 2)
    add_text(doc, "项目名称：FinTrace-CN 面向 A 股研究报告的可追溯智能核查 Agent。")
    add_text(doc, "项目定义：系统读取原始财务材料和研究报告草稿，由大语言模型驱动的 Agent 识别核查任务并调用受控工具，完成事实提取、口径统一、财务与估值复算、引用定位和结果生成。每一条确认错误均可回到原文、数据、公式和执行记录。")
    add_heading(doc, "2 选题范围", 2)
    add_table(doc, ["层级", "竞赛选题", "在系统中的作用", "处理原则"], [
        ["主线", "5 研究报告纠错核查", "最终业务闭环与展示重点", "必须完成并稳定演示"],
        ["支撑", "2 上市公司财务报告分析", "提供收入、利润、现金流、期间和口径事实", "必须保留为核查依据"],
        ["加分", "4 自动化估值建模", "复算 PE PB PS 与可验证目标价输入", "仅做核查 不做完整 DCF"],
        ["暂缓", "6 7 3 等其他方向", "备忘录 质量评估 产业链等独立产品方向", "不进入初赛主流程"],
    ], [2.0, 3.3, 7.0, 4.0])
    add_heading(doc, "3 明确不纳入初赛主线的内容", 2)
    for item in ["每日复盘与行情产品", "买方投资备忘录", "产业链影响分析", "多智能体和 MCP 服务化", "完整 DCF 流水线", "投资逻辑脆弱性和时间重验证", "对抗评测的产品化展示"]:
        add_bullet(doc, item)

    add_heading(doc, "二 业务流程与 Agent 边界")
    add_code(doc, [
        "原始年报或公告  +  研究报告草稿",
        "                ↓",
        "文档解析  →  财务事实规范化  →  Agent 任务规划",
        "                ↓",
        "主张提取  →  工具调用  →  确定性核验与复算",
        "                ↓",
        "错误清单  →  证据定位  →  修改建议  →  审计记录",
    ])
    add_heading(doc, "1 Agent 做什么", 2)
    for item in ["识别研报中收入、利润、同比、估值、引用、因果陈述等待核查主张。", "将主张拆成可验证任务，并在允许的工具集合中选择下一步。", "材料缺失、期间不明确或证据不足时，输出无法核验或待人工复核。", "将已经验证的结果组织为结构化核查报告和简明修改建议。"]:
        add_bullet(doc, item)
    add_heading(doc, "2 Agent 不做什么", 2)
    for item in ["不自行创造财务数字、来源、页码或证据。", "不自行计算同比、环比、PE、PB、PS 或单位换算。", "不自行决定事实是否成立，或绕过 Validator 输出结论。", "不将预测、分析师观点和可验证历史事实混为一谈。"]:
        add_bullet(doc, item)
    add_heading(doc, "3 执行控制", 2)
    add_text(doc, "采用单 Agent、受控工具调用和有限步数执行。建议每个任务最多 8 至 12 次工具调用，默认在本地材料和版本化快照中运行，不依赖互联网。模型负责任务理解与编排，确定性代码负责事实、计算、证据和最终放行。")

    add_heading(doc, "三 核心工具与输出")
    add_table(doc, ["工具", "职责", "优先级"], [
        ["register_documents", "登记文件 哈希 类型 来源和研究截止日", "P0"],
        ["parse_document", "解析 PDF 表格 扫描件和研报草稿", "P0"],
        ["extract_financial_facts", "抽取收入 利润 现金流 股本等事实", "P0"],
        ["normalize_financial_facts", "统一单位 期间 币种和统计口径", "P0"],
        ["extract_report_claims", "从研报原句提取结构化待核查主张", "P0"],
        ["check_report_claims", "确定性规则核验并生成 finding", "P0"],
        ["locate_evidence", "定位原文页码 表格位置和 Evidence ID", "P0"],
        ["recalculate_valuation", "复算可验证估值倍数和输入关系", "P1"],
        ["generate_checker_report", "生成 JSON Markdown 和导出结果", "P0"],
    ], [5.0, 10.0, 2.0])
    add_heading(doc, "1 每条核查结果的必要字段", 2)
    add_table(doc, ["字段", "说明"], [
        ["finding_id 与 check_type", "唯一编号和错误类型，例如 unit_error 或 period_error"],
        ["draft_claim 与 draft_location", "研报原句及其页码或段落位置"],
        ["status 与 severity", "确认错误 警告 无法核验 观点预测 核验通过及严重等级"],
        ["expected_value", "正确数值 单位 期间和口径"],
        ["evidence", "原始材料 document_id 页码和 Evidence ID"],
        ["calculation", "公式 输入与程序化复算结果"],
        ["suggestion", "可直接执行的修改建议"],
    ], [5.4, 11.6])
    add_heading(doc, "2 首批稳定覆盖的错误类型", 2)
    add_text(doc, "初赛主展示优先覆盖八类：数值错误、单位错误、期间错误、统计口径错误、同比或环比计算错误、估值倍数复算错误、缺失或错误引用、错误页码或来源定位失败。")
    add_text(doc, "后续加分类型包括：过期引用、重述后仍使用旧值、因果断言证据不足、预测值与历史事实混淆、金融机构估值方法不适用。")

    add_heading(doc, "四 产品界面与演示设计")
    add_table(doc, ["页面", "评委可见内容", "主要目标"], [
        ["案例与材料", "原始材料 草稿 文件哈希 数据版本 研究截止日", "明确受控数据来源"],
        ["智能核查", "任务计划 工具调用 进度 成功或警告状态", "证明系统是 Agent 而非普通 OCR"],
        ["核查结果", "错误类型 原句 正确值 原因 修改建议", "形成业务交付物"],
        ["证据与复算", "PDF 页码 Evidence ID 规范化过程 公式", "证明结论可回溯"],
        ["审计轨迹与导出", "文件访问 工具记录 计算记录 Prompt 版本 输出哈希", "证明过程可复现"],
    ], [3.2, 10.5, 3.3])
    add_text(doc, "竞赛版应隐藏研究总览、每日复盘、市场行情、独立同行估值、任务中心、评测与消融及实验 Demo 页面。它们可以保留代码兼容性，但不应分散评委注意力。")

    add_heading(doc, "五 现有代码的收敛策略")
    add_table(doc, ["模块", "建议", "竞赛中的角色"], [
        ["src/cn/checker", "保留并优先打磨", "研报核查的确定性核心"],
        ["src/cn/documents", "保留", "原始材料和草稿解析"],
        ["src/cn/normalization 与 analysis", "保留", "单位 期间 口径和财务复算"],
        ["evidence validation runs", "保留", "证据链 校验和审计复现"],
        ["valuation 与 peer_workflow", "保留为可选工具", "估值核验加分项"],
        ["daily_review memo industries", "从比赛入口隐藏", "后续产品方向"],
        ["acme graph proof temporal fragility finfuzz", "保留为内部或答辩备选", "不单独作为初赛模块"],
    ], [5.2, 5.4, 6.4])
    add_text(doc, "对外统一使用“证据链与复核记录”这一术语。Evidence Ledger、Claim Evidence Graph 和 Financial Claim Passport 可以保留内部实现，但不宜在首轮演示中以三套并列概念出现。")

    add_heading(doc, "六 测试与演示数据")
    add_text(doc, "准备一套完全离线的固定演示材料，并为每个案例人工维护金标预期结果。合成植入错误集只能说明合成测试表现，不得表述为真实研报准确率。")
    add_table(doc, ["金标案例", "重点验证"], [
        ["正确研报", "零误报 不为展示效果而乱报错"],
        ["单位错误", "元 万元 亿元的换算与正确修改建议"],
        ["期间错误", "年报 半年报 单季 TTM 混用识别"],
        ["口径错误", "合并 母公司 归母 扣非等统计口径识别"],
        ["计算错误", "同比 环比或 PE PB 等程序化复算"],
        ["引用错误或数据过期", "来源 页码 重述和无法核验状态"],
    ], [5.3, 11.7])

    add_heading(doc, "七 十天实施排期")
    add_table(doc, ["日期", "重点工作", "当日交付物"], [
        ["10 月 8 日", "冻结范围 保护现有改动 确定演示材料", "范围说明与材料清单"],
        ["10 月 9 日", "固定端到端主流程和金标结果", "一套可复跑案例"],
        ["10 月 10 日", "收敛 Agent 工具链与运行记录", "工具列表和执行轨迹"],
        ["10 月 11 日", "完成结构化 CheckerReport", "错误清单和 JSON 输出"],
        ["10 月 12 日", "完成证据与复算跳转", "原句 页码 公式展示"],
        ["10 月 13 日", "收敛为五页竞赛 UI", "竞赛版工作台"],
        ["10 月 14 日", "完善审计记录与导出", "Manifest 与 Markdown 导出"],
        ["10 月 15 日", "离线测试 演示彩排 代码冻结", "测试记录和录屏候选版"],
        ["10 月 16 日", "完成计划书和五分钟视频", "PDF 与视频初版"],
        ["10 月 17 日", "模拟答辩与提交检查", "最终源码包和材料"],
        ["10 月 18 日", "提交", "提交清单确认"],
    ], [2.7, 8.3, 6.0])

    add_heading(doc, "八 团队分工")
    add_table(doc, ["角色", "主要职责"], [
        ["Agent 与后端负责人", "任务编排 工具契约 API 运行日志"],
        ["金融规则负责人", "指标口径 错误规则 修改建议与适用边界"],
        ["文档智能负责人", "PDF 表格解析 主张提取 原文定位"],
        ["前端与演示负责人", "五页工作台 结果可视化 视频与答辩"],
        ["测试与材料负责人", "金标案例 离线测试 计划书 第三方清单"],
    ], [4.7, 12.3])

    add_heading(doc, "九 五分钟视频建议")
    add_table(doc, ["时间", "内容"], [
        ["0:00 至 0:25", "说明研报数字 口径和引用难以人工高效复核的问题"],
        ["0:25 至 0:50", "展示 Agent 工具 证据 Validator 的整体架构"],
        ["0:50 至 1:25", "展示受控环境中的年报 公告与含错草稿"],
        ["1:25 至 2:10", "展示 Agent 任务计划和工具调用"],
        ["2:10 至 3:20", "展示数值 单位 期间 引用和估值等纠错结果"],
        ["3:20 至 4:05", "点击一条错误 回到 PDF 页码并展示公式复算"],
        ["4:05 至 4:35", "展示日志 哈希 Validator 和离线复跑"],
        ["4:35 至 4:50", "总结系统用工具和证据核查 而非凭模型猜测"],
    ], [3.1, 13.9])

    add_heading(doc, "十 提交前验收清单")
    for item in [
        "从自然语言研报草稿中提取可核查主张，并保留逐字原句与页码。",
        "使用至少一个大语言模型作为 Agent 的任务理解和工具编排引擎。",
        "稳定覆盖数值、单位、期间、口径、计算、估值和引用等核心核查。",
        "每条确认错误均能定位研报原句和原始财务材料。",
        "每条数值类错误均能展示程序化计算过程和输入证据。",
        "证据不足时输出无法核验或待人工复核，而不是编造结论。",
        "在本地封闭或半封闭环境中完整运行，并保存文件、工具、计算和结果日志。",
        "默认测试离线运行，不依赖网络、密钥、缓存或实时行情。",
        "不将合成演示数据、干跑结果或模型叙述表述为真实金融事实或真实模型效果。",
        "提交包不含密钥、.env、受版权限制原始材料、虚拟环境或 node_modules。",
    ]:
        add_bullet(doc, "□ " + item)

    add_heading(doc, "十一 第一周优先级")
    add_number(doc, "选择并冻结一组原始材料与含错研报草稿，建立人工金标。")
    add_number(doc, "将现有 checker 流程接入受控 Agent 工具调用，保留确定性核验边界。")
    add_number(doc, "将工作台收敛为案例材料、智能核查、核查结果、证据复算、审计导出五页。")
    add_number(doc, "完成至少六个离线金标案例，并将结果接入测试。")
    add_number(doc, "录制完整端到端流程，先保证稳定性，再优化界面和叙事。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(OUT.resolve())


if __name__ == "__main__":
    build()
