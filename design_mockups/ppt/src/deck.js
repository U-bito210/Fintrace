// FinTrace-CN 4 分钟演示 PPT（8 页，米白编辑风，截图为高保真演示画面）
const pptxgen = require("pptxgenjs");
const path = require("path");

const IMG = "C:/Users/LENOVO/FinTrace-CN/design_mockups/ppt";
const OUT = path.join(IMG, "FinTrace-CN-4min-演示.pptx");

const BG = "FAF8F3", INK = "191610", MUTED = "7A7264", WEAK = "A49B8A";
const HAIR = "E7E0D2", ACCENT = "2B4A8B", WASH = "EFF2FA", PAPER = "FFFFFF";
const SANS = "Microsoft YaHei", SERIF = "Georgia", MONO = "Consolas";
const W = 13.33, H = 7.5, M = 0.5;

const p = new pptxgen();
p.layout = "LAYOUT_WIDE";
p.author = "FinTrace-CN";
p.title = "FinTrace-CN · A股可验证金融研究 Agent";

const shotW = 8.45, shotH = shotW * 810 / 1440; // 4.753

function footer(s, idx, dark) {
  const c = dark ? WEAK : WEAK;
  s.addShape(p.shapes.LINE, { x: M, y: 6.98, w: W - 2 * M, h: 0, line: { color: dark ? "3A352B" : HAIR, width: 0.75 } });
  s.addText("FINTRACE / CN · 可信 A 股研究", { x: M, y: 7.06, w: 5, h: 0.3, fontSize: 12, fontFace: MONO, color: c, margin: 0 });
  s.addText(`0${idx} / 08`, { x: W - M - 1.5, y: 7.06, w: 1.5, h: 0.3, fontSize: 12, fontFace: MONO, color: c, align: "right", margin: 0 });
}

function eyebrow(s, num, label, dark) {
  s.addText([
    { text: num + "  ", options: { color: ACCENT, bold: true } },
    { text: label, options: { color: dark ? WEAK : WEAK } },
  ], { x: M, y: 0.44, w: 9, h: 0.32, fontSize: 12.5, fontFace: SANS, charSpacing: 3, margin: 0 });
}

function slideTitle(s, t, dark) {
  s.addText(t, { x: M, y: 0.74, w: 11.5, h: 0.62, fontSize: 27, fontFace: SANS, bold: true, color: dark ? BG : INK, margin: 0 });
}

// 截图 + 发丝线边框 + 底部说明
function shot(s, file, x, y, w, cap, dark) {
  const h = w * 810 / 1440;
  s.addImage({ path: path.join(IMG, file), x, y, w, h });
  s.addShape(p.shapes.RECTANGLE, { x, y, w, h, fill: { color: INK, transparency: 100 }, line: { color: dark ? "5A5343" : HAIR, width: 1 } });
  if (cap) s.addText(cap, { x, y: y + h + 0.08, w, h: 0.3, fontSize: 12, fontFace: SANS, color: dark ? WEAK : WEAK, margin: 0 });
  return h;
}

// 右列要点行：mono 序号 + 加粗导语 + 弱化说明
function bullets(s, items, x, y, w, rowH) {
  items.forEach((it, i) => {
    const yy = y + i * rowH;
    s.addText(`0${i + 1}`, { x, y: yy, w: 0.5, h: 0.3, fontSize: 13, fontFace: MONO, color: ACCENT, bold: true, margin: 0 });
    s.addText(it.t, { x: x + 0.52, y: yy - 0.02, w: w - 0.52, h: 0.34, fontSize: 15.5, fontFace: SANS, bold: true, color: INK, margin: 0 });
    s.addText(it.d, { x: x + 0.52, y: yy + 0.34, w: w - 0.52, h: 0.62, fontSize: 12.5, fontFace: SANS, color: MUTED, margin: 0 });
  });
}

function chip(s, text, x, y, w) {
  s.addShape(p.shapes.ROUNDED_RECTANGLE, { x, y, w, h: 0.44, fill: { color: WASH }, line: { color: "DCE3F2", width: 0.75 }, rectRadius: 0.22 });
  s.addText(text, { x, y, w, h: 0.44, fontSize: 12.5, fontFace: MONO, color: ACCENT, align: "center", valign: "middle", margin: 0 });
}

/* ---------------- S1 封面（深色） ---------------- */
let s = p.addSlide();
s.background = { color: INK };
eyebrow(s, "◆", "FINTRACE / CN · A-SHARE RESEARCH OS", true);
s.addText("A股可验证\n金融研究 Agent", { x: M, y: 1.62, w: 5.6, h: 2.2, fontSize: 44, fontFace: SANS, bold: true, color: BG, lineSpacingMultiple: 1.15, margin: 0 });
s.addText("把数据、计算、证据与模型叙事拆开——\n让 AI 生成的每一个金融结论，都有可追溯的来源和验证过程。",
  { x: M, y: 4.05, w: 5.3, h: 1.1, fontSize: 15, fontFace: SANS, color: "C9C1B0", lineSpacingMultiple: 1.35, margin: 0 });
s.addText("4 分钟项目演示 · 2026-09 · 开源可复跑", { x: M, y: 5.62, w: 5.3, h: 0.35, fontSize: 13, fontFace: MONO, color: WEAK, margin: 0 });
shot(s, "cover_landing.png", 6.15, 1.5, 6.68, "研究工作台 · 落地页（高保真演示画面）", true);
footer(s, 1, true);
s.addNotes("【0:00-0:30】大家好，我演示的项目是 FinTrace-CN，一个 A 股的可验证金融研究 Agent。它要回答的问题是：当 AI 给出一条金融结论时，我们凭什么相信它？在 FinTrace 里，每一个结论都能追溯到数据来源和验证过程。");

/* ---------------- S2 四层边界（图示页） ---------------- */
s = p.addSlide();
s.background = { color: BG };
eyebrow(s, "01", "设计原则 · 为什么可信");
slideTitle(s, "拆开四层边界，模型只做叙事");
const cards = [
  { n: "①", t: "Provider", d: "只提供带来源、时点、币种口径的事实（tushare / 本地快照）" },
  { n: "②", t: "确定性引擎", d: "TTM、PE / PB / PS 等全部由 Python 计算，非模型生成" },
  { n: "③", t: "Evidence Ledger", d: "事实、公式与依赖关系全部留痕，可回放、可审计" },
  { n: "④", t: "Validator", d: "决定结论能否放行；关键证据缺失时直接阻断" },
];
const cw = 2.86, gap = 0.29, cy = 1.86, ch = 2.62;
cards.forEach((c, i) => {
  const x = M + i * (cw + gap);
  s.addShape(p.shapes.RECTANGLE, { x, y: cy, w: cw, h: ch, fill: { color: PAPER }, line: { color: HAIR, width: 1 } });
  s.addText(c.n, { x: x + 0.22, y: cy + 0.2, w: 1, h: 0.6, fontSize: 30, fontFace: SERIF, color: ACCENT, margin: 0 });
  s.addText(c.t, { x: x + 0.22, y: cy + 0.92, w: cw - 0.44, h: 0.4, fontSize: 17, fontFace: SANS, bold: true, color: INK, margin: 0 });
  s.addText(c.d, { x: x + 0.22, y: cy + 1.36, w: cw - 0.44, h: 1.1, fontSize: 12.5, fontFace: SANS, color: MUTED, lineSpacingMultiple: 1.25, margin: 0 });
  if (i < 3) s.addText("→", { x: x + cw - 0.06, y: cy + ch / 2 - 0.24, w: gap + 0.12, h: 0.4, fontSize: 18, fontFace: SANS, color: WEAK, align: "center", margin: 0 });
});
s.addShape(p.shapes.RECTANGLE, { x: M, y: 4.95, w: W - 2 * M, h: 1.42, fill: { color: PAPER }, line: { color: HAIR, width: 1 } });
s.addText([
  { text: "LLM 只负责：", options: { bold: true, color: INK, breakLine: true } },
  { text: "理解研究问题 · 选择工具 · 生成叙事（标注「需人工复核」）", options: { color: MUTED, breakLine: true } },
  { text: "", options: { breakLine: true } },
  { text: "LLM 不拥有：", options: { bold: true, color: "B5544A" } },
  { text: "数据源选择权 · 财务计算权 · 证据创建权 · Validator 放行权", options: { color: MUTED } },
], { x: M + 0.3, y: 5.12, w: W - 2 * M - 0.6, h: 1.1, fontSize: 14, fontFace: SANS, lineSpacingMultiple: 1.3, margin: 0 });
footer(s, 2, false);
s.addNotes("【0:30-1:10】普通金融问答最大的问题，是模型记忆、实时数据、计算结果和叙事混在一句话里，出了错不知道错在哪。所以 FinTrace 把系统拆成四层边界：Provider 只提供带来源、带时点的事实；财务计算全部由 Python 确定性引擎完成；每一步事实和依赖写进 Evidence Ledger 留痕；最后由 Validator 决定结论能不能放行。大模型在这里只做两件事：理解问题、生成叙事——它不拥有数据源选择权、计算权，也没有放行权。");

/* ---------------- S3 研究总览 ---------------- */
s = p.addSlide();
s.background = { color: BG };
eyebrow(s, "02", "核心功能 · 研究总览");
slideTitle(s, "总览一页，事实与 AI 叙事分列");
shot(s, "overview.png", M, 1.52, shotW, "研究总览 · 贵州茅台 600519.SH（快照 2026-09-28 · tushare）");
bullets(s, [
  { t: "事实摘要与 AI 解读分列", d: "叙事卡带「需人工复核」标签，不与事实混排" },
  { t: "研究路径六步可视", d: "选择标的 → 快照 → 校验 → 估值 → AI 解读 → 报告" },
  { t: "证据台账随身可查", d: "每个读数都挂着 Evidence ID，点开即达" },
], 9.28, 1.78, 3.55, 1.24);
chip(s, "EV-V-011 · PE 19.2x", 9.28, 5.62, 2.5);
footer(s, 3, false);
s.addNotes("【1:10-1:45】这是研究工作台的总览页。打开一个标的，能看到 RAW 收盘价、研究路径走到了哪一步、四个估值读数。注意中间：左边是事实摘要，右边是 AI 解读——右上角标着「模型叙事 · 需人工复核」，叙事和事实是分开呈现的。页面下方的证据台账里，每个数字都挂着 Evidence ID，随时可以点开溯源。");

/* ---------------- S4 市场与行情 ---------------- */
s = p.addSlide();
s.background = { color: BG };
eyebrow(s, "03", "核心功能 · 市场与行情");
slideTitle(s, "RAW 口径 + 版本化快照，缺口径不补值");
shot(s, "market.png", M, 1.52, shotW, "市场与行情 · 31 根日线 RAW（悬浮与表格均来自快照 bars[]）");
bullets(s, [
  { t: "只用 RAW 原始价格", d: "市值与同行估值不与复权价混用" },
  { t: "联网刷新生成新版本", d: "快照按时间版本化，旧版本可回放对比" },
  { t: "未覆盖就明说", d: "QFQ / HFQ 未覆盖时标注留空，绝不补值" },
], 9.28, 1.78, 3.55, 1.24);
chip(s, "EV-Q-0129 · 1,243.88 元", 9.28, 5.62, 2.75);
footer(s, 4, false);
s.addNotes("【1:45-2:15】行情页只认 RAW 原始价格，市值和估值都用它来算，不跟复权价混用。支持联网刷新，拉回最新收盘生成一个新的快照版本，旧版本全部保留、可以回放。如果某个复权口径没覆盖，页面就明确标注「未覆盖」，不会悄悄补值——宁可留空，也不编数。");

/* ---------------- S5 同行估值 ---------------- */
s = p.addSlide();
s.background = { color: BG };
eyebrow(s, "04", "核心功能 · 同行估值");
slideTitle(s, "同行业、同时点、同口径的确定性估值");
shot(s, "peers.png", M, 1.52, shotW, "同行估值 · 白酒行业 n=4 · IQR k=1.5（服务层计算）");
bullets(s, [
  { t: "估值由服务层算出", d: "PE / PB / PS 全部确定性计算，非模型生成" },
  { t: "异常值与口径处理", d: "IQR 剔异常；期间不匹配直接剔除，如古井贡酒" },
  { t: "适用性边界", d: "银行、保险不适用同业法，系统自动排除" },
], 9.28, 1.78, 3.55, 1.24);
chip(s, "同业中位 16.0x · 本标的 19.2x", 9.28, 5.62, 3.1);
footer(s, 5, false);
s.addNotes("【2:15-2:45】同行估值是服务层确定性算出来的：同一个行业、同一个时点、同一个口径去比较 PE 和 PB，用 IQR 剔除异常值，期间不匹配的直接剔除，银行保险这类不适用同业法的自动排除。这个例子里，茅台 PE 19.2 倍，高于同业中位 16 倍，但没触发异常剔除——结论是「估值不便宜」，并且注明依据是部分的，因为 PB 历史分位缺失。");

/* ---------------- S6 证据与校验 ---------------- */
s = p.addSlide();
s.background = { color: BG };
eyebrow(s, "05", "核心功能 · 证据与校验");
slideTitle(s, "证据链 + 闸门：缺证据就隐藏结论");
shot(s, "evidence.png", M, 1.52, shotW, "证据与校验 · Evidence DAG 与 Validator 闸门（含阻断案例）");
bullets(s, [
  { t: "事实带全溯源字段", d: "来源、期间、截止、币种 / 单位一个不少" },
  { t: "Evidence DAG 记录派生", d: "价格 × 总股本 → 市值 → PE，依赖可回放" },
  { t: "关键证据缺失即阻断", d: "估值与报告直接隐藏，模型不得自行补数" },
], 9.28, 1.78, 3.55, 1.24);
chip(s, "校验通过 12/12 · 1 项部分缺失", 9.28, 5.62, 3.1);
footer(s, 6, false);
s.addNotes("【2:45-3:20】这是整个项目最核心的一页：证据与校验。每条事实都带来源、期间、截止和币种单位；Evidence DAG 记录派生关系——收盘价乘以总股本得到市值，再除以 TTM 净利得到 PE。Validator 做五项闸门检查。最关键的是底部这个阻断案例：把总股本证据删掉，估值和报告会被直接隐藏。系统宁可不出结论，也不让模型自己补一个数。");

/* ---------------- S7 AI Agent ---------------- */
s = p.addSlide();
s.background = { color: BG };
eyebrow(s, "06", "核心功能 · AI Agent 研究");
slideTitle(s, "有界工具调用，AI 叙事全程留痕");
shot(s, "agent.png", M, 1.52, shotW, "AI Agent 研究 · 对话式研究 + 执行轨迹（可回放）");
bullets(s, [
  { t: "模型只写叙事", d: "数字来自工具与服务层，回答内嵌 Evidence ID" },
  { t: "有界重试与降级", d: "工具失败返回 fallback 状态，不编造结果" },
  { t: "轨迹与成本可审计", d: "每步调用、耗时、tokens、成本全部留痕" },
], 9.28, 1.78, 3.55, 1.24);
chip(s, "4 次工具调用 · 8.0s · 重试 0", 9.28, 5.62, 2.9);
footer(s, 7, false);
s.addNotes("【3:20-3:45】AI Agent 页面里，模型负责理解问题和生成回答，但所有数字都来自工具调用和服务层计算，回答里内嵌 Evidence ID 引用，缺数据的维度它就明说「不参与判断」。右边是执行轨迹：每一步工具调用、耗时、tokens 和成本全部留痕，可以回放审计。");

/* ---------------- S8 总结（深色） ---------------- */
s = p.addSlide();
s.background = { color: INK };
eyebrow(s, "◆", "总结 · FinTrace-CN", true);
s.addText("研究结果，\n应该经得起追问。", { x: M, y: 1.7, w: 6.2, h: 2.3, fontSize: 40, fontFace: SANS, bold: true, color: BG, lineSpacingMultiple: 1.2, margin: 0 });
s.addText([
  { text: "工程底座：离线测试 1700+ 全绿，CI 回归门禁", options: { bullet: { code: "25B8", indent: 12 }, color: "C9C1B0", breakLine: true } },
  { text: "诚实边界：演示数据带 synthetic_demo 标注，永不冒充实时行情", options: { bullet: { code: "25B8", indent: 12 }, color: "C9C1B0", breakLine: true } },
  { text: "开源可复跑 · 不构成投资建议 · 不连接券商账户", options: { bullet: { code: "25B8", indent: 12 }, color: "C9C1B0" } },
], { x: M + 0.05, y: 4.35, w: 6.1, h: 1.7, fontSize: 15, fontFace: SANS, paraSpaceAfter: 12, margin: 0 });
shot(s, "daily.png", 7.05, 1.62, 5.78, "每日复盘 · 演示数据页（synthetic_demo）", true);
footer(s, 8, true);
s.addNotes("【3:45-4:10】最后用每日复盘收尾：演示数据永远带 synthetic_demo 标注，不会冒充实时行情；整个项目有一千七百多个离线测试做回归门禁，开源可复跑。我们想传达的就一句话：研究结果，应该经得起追问。谢谢大家，欢迎提问。");

p.writeFile({ fileName: OUT }).then(() => console.log("written:", OUT));
