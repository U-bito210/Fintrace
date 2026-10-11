# -*- coding: utf-8 -*-
"""生成 PPT 用的理想化前端截图页面（B/D 米白编辑风，数字取自真实联网快照）。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SNAP = ROOT / "data/snapshots/cn/600519.SH_20260928_tushare_v2.json"
OUT = Path(__file__).resolve().parent

snap = json.loads(SNAP.read_text(encoding="utf-8"))
bars = sorted(snap["data"]["bars"], key=lambda b: b["trade_date"])
closes = [b["close"] for b in bars]
vols = [b["volume"] for b in bars]
dates = [b["trade_date"] for b in bars]
latest, prev = bars[-1], bars[-2]
day_chg = (latest["close"] / prev["close"] - 1) * 100
MARKET_CAP = 1.5623  # 万亿, 12.56 亿股 × 1243.88
TTM_NP = 814  # 亿
PE_TTM = MARKET_CAP * 10000 / TTM_NP  # 19.2
PB = 6.8

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{width:1440px;height:810px;overflow:hidden;background:#FAF8F3;color:#191610;
  font-family:'Segoe UI','Microsoft YaHei','PingFang SC',sans-serif;display:flex;font-size:12px}
.serif{font-family:Georgia,'Source Han Serif SC','SimSun',serif}
.mono{font-family:Consolas,'SFMono-Regular',Menlo,monospace}
.num{font-variant-numeric:tabular-nums}
/* ---------- 侧栏 ---------- */
.sidebar{width:318px;flex:none;background:#F6F2E9;border-right:1px solid #E7E0D2;
  padding:24px 20px 16px;display:flex;flex-direction:column;overflow:hidden}
.wordmark{font-family:Georgia,'SimSun',serif;font-size:16px;font-weight:700;letter-spacing:.06em}
.wordmark .dia{color:#2B4A8B;font-size:12px;vertical-align:2px}
.sub{font-size:8.5px;letter-spacing:.14em;color:#A49B8A;margin:5px 0 16px}
.snapcard{background:#fff;border:1px solid #E7E0D2;border-radius:10px;padding:13px 13px 11px;margin-bottom:14px}
.snapcard .ct{font-size:10.5px;color:#7A7264;margin-bottom:9px}
.flab{font-size:9px;letter-spacing:.05em;color:#A49B8A;margin-bottom:4px}
.fsel{background:#fff;border:1px solid #E7E0D2;border-radius:8px;padding:6px 9px;font-size:11.5px;
  display:flex;justify-content:space-between;align-items:center;font-weight:600}
.fsel .car{color:#A49B8A;font-size:9px}
.fcap{font-size:9.5px;color:#A49B8A;line-height:1.5;margin:7px 0 9px}
.sbtn{display:flex;align-items:center;justify-content:center;gap:5px;border:1px solid #E7E0D2;
  background:#fff;border-radius:999px;padding:5.5px 0;font-size:10.5px;color:#191610;margin-top:6px}
.sbtn.dark{background:#191610;color:#FAF8F3;border-color:#191610}
.navgrp{font-size:8.5px;letter-spacing:.13em;color:#A49B8A;margin:10px 2px 4px}
.navi{font-size:11.5px;color:#5C5546;padding:4.5px 9px;border-radius:8px;margin:1px 0}
.navi.on{background:#fff;color:#191610;font-weight:600;border-left:2px solid #2B4A8B;border-radius:4px 8px 8px 4px}
.navi.dim{color:#A49B8A}
/* ---------- 主区 ---------- */
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{height:50px;flex:none;display:flex;align-items:center;justify-content:flex-end;gap:13px;padding:0 22px}
.chip{background:#fff;border:1px solid #E7E0D2;border-radius:999px;padding:5px 13px;font-size:11px;font-weight:600}
.chip .dot{color:#1F8A5D;font-size:8px;vertical-align:1px;margin-right:4px}
.deploy{font-size:11px;color:#5C5546}
.dots{color:#A49B8A;font-size:13px;letter-spacing:1px}
.content{flex:1;padding:2px 26px 0;overflow:hidden}
.crumb{display:flex;justify-content:space-between;align-items:center;font-size:10.5px;color:#A49B8A;margin-bottom:7px}
.crumb .r{display:flex;gap:7px;align-items:center}
.minichip{border:1px solid #E7E0D2;background:#fff;border-radius:999px;padding:2.5px 9px;font-size:9.5px;color:#5C5546}
.minichip.ok{color:#1F8A5D;border-color:#CFE3D6;background:#F3F8F4}
.eyebrow{font-size:9.5px;letter-spacing:.16em;color:#A49B8A;text-transform:uppercase;margin:3px 0 5px}
.ptitlerow{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:1px solid #E7E0D2;padding-bottom:9px}
h1.pt{font-family:Georgia,'SimSun',serif;font-size:25px;font-weight:600;letter-spacing:.01em}
.ptag{font-size:10px;color:#A49B8A}
.sechead{display:flex;justify-content:space-between;align-items:baseline;margin:13px 0 7px}
.sechead .t{font-size:12.5px;font-weight:700}
.sechead .s{font-size:9.5px;color:#A49B8A;letter-spacing:.04em}
.badge{display:inline-block;border-radius:999px;padding:2px 9px;font-size:9.5px;font-weight:600}
.badge.ok{background:#E9F3EC;color:#1F8A5D}
.badge.warn{background:#F6EEDC;color:#9A6B14}
.badge.bad{background:#F8E9E7;color:#B5544A}
.badge.n{background:#F1EDE3;color:#5C5546}
.badge.v{background:#EDE9F6;color:#6B4FA8}
.tile{background:#fff;border:1px solid #E7E0D2;border-radius:10px;padding:10px 13px}
.tile .lb{font-size:9px;letter-spacing:.1em;color:#A49B8A;text-transform:uppercase;display:flex;justify-content:space-between}
.tile .lb .eid{font-family:Consolas,monospace;font-size:8.5px;letter-spacing:0;color:#B9B09E}
.tile .v{font-family:Georgia,'SimSun',serif;font-size:27px;font-weight:600;margin:4px 0 2px}
.tile .v small{font-size:13px;font-weight:400;color:#7A7264}
.tile .sub2{font-size:9.5px;color:#7A7264}
.up{color:#C53A3A}.dn{color:#1F8A5D}.mut{color:#7A7264}.blu{color:#2B4A8B}
.route{display:flex;align-items:center;gap:0;font-size:10.5px;color:#A49B8A;flex-wrap:nowrap}
.route .st{white-space:nowrap}
.route .st.cur{color:#2B4A8B;font-weight:700;border-bottom:2px solid #2B4A8B;padding-bottom:1px}
.route .st.done{color:#191610}
.route .ar{margin:0 7px;color:#D8D0BF;font-size:9px}
.wcard{background:#fff;border:1px solid #E7E0D2;border-radius:10px;padding:12px 15px}
table.tb{width:100%;border-collapse:collapse;font-size:11px}
table.tb th{font-size:9px;letter-spacing:.09em;color:#A49B8A;text-transform:uppercase;font-weight:600;
  text-align:left;padding:5px 9px;border-bottom:1px solid #191610}
table.tb td{padding:6.5px 9px;border-bottom:1px solid #EFE9DC;font-variant-numeric:tabular-nums}
table.tb tr.hl td{background:#F6F1E4}
table.tb .r{text-align:right}table.tb th.r{text-align:right}
.narr{border-left:3px solid #6B4FA8;background:#fff;border-top:1px solid #E7E0D2;border-right:1px solid #E7E0D2;
  border-bottom:1px solid #E7E0D2;border-radius:0 10px 10px 0;padding:11px 14px;font-size:11.5px;line-height:1.75}
.narr .nt{display:flex;justify-content:space-between;font-size:9px;color:#A49B8A;letter-spacing:.08em;margin-bottom:5px}
.narr .cite{font-family:Consolas,monospace;font-size:9.5px;color:#2B4A8B;background:#EFF2FA;border-radius:4px;padding:0 4px}
.pagenote{font-size:9.5px;color:#A49B8A;line-height:1.6}
.kv{display:flex;gap:22px;font-size:10.5px;color:#5C5546;margin-top:8px}
.kv b{color:#191610;font-weight:600}
.btn-dark{background:#191610;color:#FAF8F3;border-radius:999px;padding:7px 16px;font-size:11.5px;font-weight:600;display:inline-block}
.chatme{display:flex;justify-content:flex-end;margin:2px 0 10px}
.chatme .b{background:#EFF2FA;border:1px solid #DCE3F2;border-radius:12px 12px 2px 12px;padding:9px 13px;
  font-size:12px;max-width:430px;line-height:1.6}
.tstep{display:flex;align-items:center;gap:9px;padding:5.5px 2px;border-bottom:1px solid #EFE9DC;font-size:11px}
.tstep .no{font-family:Consolas,monospace;font-size:9px;color:#A49B8A;width:16px}
.tstep .kind{font-weight:700;font-size:10.5px}
.tstep .det{color:#7A7264;font-size:10.5px}
.tstep .ms{margin-left:auto;font-family:Consolas,monospace;font-size:9.5px;color:#A49B8A}
.node{border:1px solid #E7E0D2;background:#fff;border-radius:8px;padding:5px 9px;font-size:9.5px;text-align:center}
.node .eid{font-family:Consolas,monospace;font-size:8px;color:#2B4A8B;display:block}
"""

def sidebar(active: str) -> str:
    groups = [
        ("研究工作台", ["研究总览", "市场与行情", "财务表现", "财务分析", "同行估值"]),
        ("AI 研究", ["AI Agent 研究", "Agent 执行轨迹"]),
        ("证据与报告", ["证据与校验", "文档智能", "研究报告"]),
        ("任务中心", ["研究任务", "每日复盘", "评测与消融"]),
        ("演示与设置", ["案例演示"]),
    ]
    rows = []
    for g, items in groups:
        rows.append(f'<div class="navgrp">{g.upper()}</div>')
        for it in items:
            cls = "navi on" if it == active else "navi"
            rows.append(f'<div class="{cls}">{it}</div>')
    nav = "\n".join(rows)
    return f"""
<aside class="sidebar">
  <div class="wordmark"><span class="dia">◆</span> FINTRACE / CN</div>
  <div class="sub">A-SHARE RESEARCH OS · LOCAL SNAPSHOTS</div>
  <div class="snapcard">
    <div class="ct">快照管理</div>
    <div class="flab">当前研究版本</div>
    <div class="fsel">贵州茅台 · 600519.SH <span class="car">▼</span></div>
    <div class="fcap">当前：联网更新版本 · 收盘数据截至 2026-09-28 · 研究截至 2026-09-28</div>
    <div class="sbtn">↻&nbsp; 切回本地基准</div>
    <div class="sbtn dark">联网更新&nbsp;▼</div>
    <div class="sbtn">◎&nbsp; 查看研究总览</div>
  </div>
  {nav}
</aside>"""

TOPBAR = """
<header class="topbar">
  <div class="chip"><span class="dot">●</span>本地账号 · demo</div>
  <div class="deploy">Deploy</div>
  <div class="dots">⋮</div>
</header>"""

def crumb(path: str, right_extra: str = "") -> str:
    return f"""
<div class="crumb"><div>研究工作台 / {path}</div>
<div class="r"><span class="minichip ok num">● 校验通过 12/12</span>{right_extra}<span class="minichip num">快照 2026-09-28</span></div></div>"""

def page(active: str, crumb_path: str, body: str, right_extra: str = "") -> str:
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body>{sidebar(active)}<div class="main">{TOPBAR}<div class="content">{body}</div></div></body></html>"""

# ---------------------------------------------------------------- 总览
def spark_points(vals, w, h, pad=3):
    lo, hi = min(vals), max(vals)
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * (w - 2 * pad) / (n - 1)
        y = pad + (hi - v) / (hi - lo) * (h - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts), vals.index(min(vals)) * (w - 2 * pad) / (n - 1) + pad

sp_pts, sp_w = spark_points(closes, 470, 62)
peers_tbl = """
<div class="sechead"><div class="t">同行估值摘要</div><div class="s">仅现有本地图谱 · RAW 价格 · 口径 2025A / TTM</div></div>
<table class="tb"><tr><th>公司</th><th class="r">总市值</th><th class="r">PE · TTM</th><th class="r">PB</th><th class="r">口径</th></tr>
<tr class="hl"><td><b>贵州茅台（本标的）</b>&nbsp;<span class="mut mono">600519.SH</span></td><td class="r">1.56 万亿</td><td class="r num">19.2x</td><td class="r num">6.8x</td><td class="r">2026H1 / RAW</td></tr>
<tr><td>五粮液&nbsp;<span class="mut mono">000858.SZ</span></td><td class="r">4,620 亿</td><td class="r num">15.1x</td><td class="r num">3.6x</td><td class="r">2025A / RAW</td></tr>
<tr><td>山西汾酒&nbsp;<span class="mut mono">600809.SH</span></td><td class="r">2,010 亿</td><td class="r num">18.4x</td><td class="r num">4.6x</td><td class="r">2025A / RAW</td></tr>
<tr><td>泸州老窖&nbsp;<span class="mut mono">000568.SZ</span></td><td class="r">1,905 亿</td><td class="r num">13.2x</td><td class="r num">2.9x</td><td class="r">2025A / RAW</td></tr>
<tr><td>洋河股份&nbsp;<span class="mut mono">002304.SZ</span></td><td class="r">1,064 亿</td><td class="r num">16.9x</td><td class="r num">2.0x</td><td class="r">2025A / RAW</td></tr>
<tr><td class="mut">古井贡酒&nbsp;<span class="mono">000596.SZ</span></td><td class="r mut">—</td><td class="r mut">—</td><td class="r mut">—</td><td class="r"><span class="badge warn">已剔除 · 期间不匹配</span></td></tr>
</table>"""

ledger_tbl = """
<div class="sechead"><div class="t">证据台账</div><div class="s">EVIDENCE LEDGER · 12 条 · 显示前 5</div></div>
<table class="tb"><tr><th>证据 ID</th><th>类型</th><th>来源</th><th>期间</th><th>截止</th><th class="r">校验</th></tr>
<tr><td class="mono blu">EV-Q-0129</td><td>收盘价 · RAW</td><td>tushare</td><td>2026-09-28</td><td>09-28 15:00</td><td class="r"><span class="badge ok">● 通过</span></td></tr>
<tr><td class="mono blu">EV-F-007</td><td>营收 · 归母净利</td><td>2025 年报 + 2026 中报</td><td>2026H1</td><td>08-15</td><td class="r"><span class="badge ok">● 通过</span></td></tr>
<tr><td class="mono blu">EV-S-003</td><td>总股本</td><td>公司公告</td><td>2025A</td><td>03-28</td><td class="r"><span class="badge ok">● 通过</span></td></tr>
<tr><td class="mono blu">EV-V-011</td><td>PE · TTM 计算</td><td>服务层（非模型）</td><td>TTM</td><td>09-28</td><td class="r"><span class="badge ok">● 通过</span></td></tr>
<tr><td class="mono blu">EV-V-012</td><td>PB 历史分位</td><td>服务层</td><td>—</td><td>—</td><td class="r"><span class="badge warn">◑ 部分缺失</span></td></tr>
</table>"""

trace_rows = """
<div class="tstep"><span class="no">01</span><span class="kind">工具调用 · 行情获取</span><span class="det mono">quote.fetch(600519) → RAW</span><span class="ms">1.2s</span></div>
<div class="tstep"><span class="no">02</span><span class="kind">工具调用 · 财报提取</span><span class="det mono">filings.extract(2026H1) → 结构化事实</span><span class="ms">1.8s</span></div>
<div class="tstep"><span class="no">03</span><span class="kind">确定性计算 · 估值</span><span class="det mono">service.calc(PE/PB)（非模型生成）</span><span class="ms">0.3s</span></div>
<div class="tstep"><span class="no">04</span><span class="kind">证据校验</span><span class="det">gate.check — 12/12 通过 · 1 项部分缺失已标注</span><span class="ms">0.6s</span></div>
<div class="tstep"><span class="no">05</span><span class="kind">模型叙事生成</span><span class="det mono">narrative.draft → 待人工复核</span><span class="ms">4.1s</span></div>"""

overview_body = f"""
{crumb("研究总览")}
<div class="eyebrow">研究总览 — A 股研究快照</div>
<div class="ptitlerow"><h1 class="pt serif">贵州茅台 <span style="font-size:14px;color:#A49B8A" class="mono">600519.SH</span></h1>
<div style="font-size:10.5px;color:#7A7264">快照 2026-09-28 · 来源 tushare · 币种 CNY · 单位 元 · 期间 2026H1 / TTM · <span class="badge ok">● 校验通过 12/12</span></div></div>

<div class="wcard" style="margin-top:11px;display:flex;gap:22px;align-items:stretch">
  <div style="width:225px">
    <div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">收盘价 · RAW &nbsp;<span class="mono" style="color:#B9B09E">EV-Q-0129</span></div>
    <div class="serif num" style="font-size:44px;font-weight:600;margin:2px 0 1px">1,243<span style="font-size:20px;color:#7A7264">.88 元</span></div>
    <div class="num up" style="font-size:11px;font-weight:700">▲ +0.56% <span style="font-weight:400;color:#7A7264">当日 · 收盘对比前收</span></div>
    <div style="font-size:9.5px;color:#7A7264;margin-top:7px;line-height:1.6">总市值 1.56 万亿 · RAW 收盘 × 总股本<br>截至 2026-09-28 15:00 · tushare</div>
  </div>
  <div style="flex:1;border-left:1px solid #EFE9DC;padding:4px 0 0 20px">
    <div style="font-size:9px;letter-spacing:.1em;color:#A49B8A;margin-bottom:6px">近 30 个交易日 · 收盘价（快照内 bars[]）</div>
    <svg width="470" height="62" viewBox="0 0 470 62">
      <polyline points="{sp_pts}" fill="none" stroke="#2B4A8B" stroke-width="1.6"/>
      <line x1="{sp_w:.0f}" y1="4" x2="{sp_w:.0f}" y2="58" stroke="#A49B8A" stroke-dasharray="2 3" stroke-width="1"/>
    </svg>
    <div style="font-size:9px;color:#A49B8A;margin-top:4px">…… 09-28（虚线为研究时点）</div>
  </div>
  <div style="width:236px;border-left:1px solid #EFE9DC;padding-left:20px">
    <div style="font-size:9px;letter-spacing:.1em;color:#A49B8A;margin-bottom:8px">研究路径</div>
    <div class="route" style="flex-direction:column;align-items:flex-start;gap:7px;font-size:11px">
      <span class="st done">✓ 选择标的 → 拉取快照</span><span class="st done">✓ 校验证据 → 估值计算</span>
      <span class="st cur">AI 解读（当前）</span><span class="st">生成报告</span>
    </div>
  </div>
</div>

<div style="display:flex;gap:10px;margin-top:10px">
  <div class="tile" style="flex:1"><div class="lb"><span>PE · TTM</span><span class="eid">EV-V-011</span></div>
    <div class="v num">19.2<small>x</small></div><div class="sub2">同业 Q3 上方 · 未触发 IQR 剔除</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>PB · MRQ</span><span class="eid">EV-V-012</span></div>
    <div class="v num">6.8<small>x</small></div><div class="sub2">历史分位缺失 · 已标注</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>营收变化 · 2026H1</span><span class="eid">EV-F-007</span></div>
    <div class="v num">+1.5<small>%</small></div><div class="sub2"><span class="up">▲ 已披露 · 完整</span></div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>归母净利变化 · 2026H1</span><span class="eid">EV-F-008</span></div>
    <div class="v num">−2.0<small>%</small></div><div class="sub2"><span class="dn">▼ 已披露 · 完整</span></div></div>
</div>

<div style="display:flex;gap:14px;margin-top:12px">
  <div style="flex:1.05">
    <div class="sechead"><div class="t">关键研究摘要</div><div class="s">事实 · 规则可计算</div></div>
    <div class="wcard" style="padding:4px 15px">
      <table class="tb">
        <tr><td style="border-bottom:1px solid #EFE9DC">业绩状态（2026H1）</td><td class="r" style="border-bottom:1px solid #EFE9DC"><b>已披露 · 完整</b></td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">营收变化</td><td class="r up num" style="border-bottom:1px solid #EFE9DC">+1.5%</td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">归母净利变化</td><td class="r dn num" style="border-bottom:1px solid #EFE9DC">−2.0%</td></tr>
        <tr><td>下一次应验证</td><td class="r">2026 三季报 · 营收与净利是否企稳</td></tr>
      </table>
    </div>
  </div>
  <div style="flex:1">
    <div class="sechead"><div class="t">AI 研究解读</div><div class="s">模型叙事 · 需人工复核</div></div>
    <div class="narr">2026 年中报已出：营收微增、归母净利同比 −2.0%，增长阶段性承压。以 RAW 收盘价计算的 TTM 估值 <span class="cite">EV-V-011</span> 处于同业 Q3 上方，但 PB 历史分位数据缺失 <span class="cite">EV-V-012</span>，不能仅凭 PE 判断贵贱。
      <div style="margin-top:5px;color:#5C5546">下一步建议核对 2026 三季报的毛利率与一致性提验；若净利增速继续回落，应先完成成本结构再下结论……</div>
      <div class="nt" style="margin:6px 0 0"><span>叙事与事实分离 · 开启</span><span>不作为事实引用</span></div>
    </div>
  </div>
</div>

<div style="display:flex;gap:14px;margin-top:12px">
  <div style="flex:1.35">{peers_tbl}</div>
  <div style="flex:1">{ledger_tbl}</div>
</div>
<div class="pagenote" style="margin-top:8px">本页仅展示固定版本快照，非实时行情，不构成投资建议。所有计算与显示结论与状态都来自 Evidence Ledger 与研究校验。</div>
"""

# ---------------------------------------------------------------- 市场与行情
import math
def candles_svg():
    w, h = 1048, 330
    n = len(bars)
    lo = min(b["low"] if b["low"] else b["close"] for b in bars)
    hi = max(b["high"] if b["high"] else b["close"] for b in bars)
    pad_t, chart_h = 8, 240
    vmax = max(vols)
    cw = w / n
    grid = ""
    for gy in range(5):
        y = pad_t + gy * chart_h / 4
        price = hi - (hi - lo) * gy / 4
        grid += f'<line x1="0" y1="{y:.0f}" x2="{w}" y2="{y:.0f}" stroke="#EFE9DC" stroke-width="1"/>' \
                f'<text x="{w+6}" y="{y+3:.0f}" font-size="8.5" fill="#A49B8A" font-family="Consolas">{price:.0f}</text>'
    body = ""
    for i, b in enumerate(bars):
        x = i * cw + cw / 2
        yh = pad_t + (hi - b["high"]) / (hi - lo) * chart_h
        yl = pad_t + (hi - b["low"]) / (hi - lo) * chart_h
        yo = pad_t + (hi - b["open"]) / (hi - lo) * chart_h
        yc = pad_t + (hi - b["close"]) / (hi - lo) * chart_h
        col = "#C53A3A" if b["close"] >= b["open"] else "#1F8A5D"
        body += f'<line x1="{x:.1f}" y1="{yh:.1f}" x2="{x:.1f}" y2="{yl:.1f}" stroke="{col}" stroke-width="1"/>'
        top, bot = min(yo, yc), abs(yc - yo)
        if bot < 1: bot = 1
        body += f'<rect x="{x-cw*0.3:.1f}" y="{top:.1f}" width="{cw*0.6:.1f}" height="{bot:.1f}" fill="{col}"/>'
        vy = pad_t + chart_h + 14 + (1 - b["volume"] / vmax) * 62
        body += f'<rect x="{x-cw*0.3:.1f}" y="{vy:.1f}" width="{cw*0.6:.1f}" height="{pad_t+chart_h+14+76-vy:.1f}" fill="#D8D0BF"/>'
    xl = ""
    for i in range(0, n, 5):
        x = i * cw + cw / 2
        xl += f'<text x="{x:.0f}" y="{pad_t+chart_h+14+76+12}" font-size="8.5" fill="#A49B8A" text-anchor="middle" font-family="Consolas">{dates[i][5:]}</text>'
    yv = pad_t + chart_h + 14
    return (f'<svg width="{w+40}" height="{h}" viewBox="0 0 {w+40} {h}">{grid}{body}'
            f'<line x1="0" y1="{yv}" x2="{w}" y2="{yv}" stroke="#E7E0D2"/>'
            f'<line x1="{w-cw/2:.0f}" y1="6" x2="{w-cw/2:.0f}" y2="{pad_t+chart_h}" stroke="#A49B8A" stroke-dasharray="2 3"/>'
            f'<text x="{w-cw/2-4:.0f}" y="14" font-size="8.5" fill="#A49B8A" text-anchor="end">研究时点</text>{xl}</svg>')

market_rows = "".join(
    f'<tr><td class="mono num">{b["trade_date"]}</td><td class="r num">{b["open"]:,.2f}</td><td class="r num">{b["high"]:,.2f}</td>'
    f'<td class="r num">{b["low"]:,.2f}</td><td class="r num">{b["close"]:,.2f}</td><td class="r num">{b["volume"]:,.0f}</td></tr>'
    for b in sorted(bars[-8:], key=lambda x: x["trade_date"], reverse=True))

market_body = f"""
{crumb("市场与行情")}
<div class="eyebrow">市场与行情 · 固定快照价格与成交</div>
<div class="ptitlerow"><h1 class="pt">市场与行情</h1><div class="ptag">快照内 bars[] · 非实时</div></div>

<div style="display:flex;gap:8px;margin:10px 0 4px;align-items:center">
  <div style="display:flex;border:1px solid #E7E0D2;border-radius:999px;overflow:hidden;font-size:10.5px">
    <span style="padding:4px 14px;background:#191610;color:#FAF8F3;font-weight:600">RAW</span>
    <span style="padding:4px 14px;color:#A49B8A">QFQ</span><span style="padding:4px 14px;color:#A49B8A">HFQ</span>
  </div>
  <span style="font-size:9.5px;color:#A49B8A">RAW：已覆盖 · QFQ：未覆盖 · HFQ：未覆盖（未以任何方式补值）</span>
</div>

<div style="display:flex;gap:10px;margin-top:6px">
  <div class="tile" style="flex:1"><div class="lb"><span>最近收盘价</span></div><div class="v num">¥ 1,243.88</div><div class="sub2">RAW · tushare</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>最近交易日</span></div><div class="v num">09-28</div><div class="sub2">2026-09-28</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>行情记录</span></div><div class="v num">31 <small>条</small></div><div class="sub2">快照内 bars[]</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>研究截止日</span></div><div class="v num">09-28</div><div class="sub2">research_as_of</div></div>
</div>

<div class="wcard" style="margin-top:10px;padding:12px 14px 8px">
  <div style="font-size:9px;letter-spacing:.1em;color:#A49B8A;margin-bottom:4px">日线与成交量 / RAW · 单位 CNY · 成交量：手 · Provider：tushare</div>
  {candles_svg()}
</div>

<div style="display:flex;gap:14px;margin-top:10px">
  <div style="flex:1.6">
  <table class="tb"><tr><th>交易日</th><th class="r">开盘</th><th class="r">最高</th><th class="r">最低</th><th class="r">收盘价</th><th class="r">成交量（手）</th></tr>
  {market_rows}</table></div>
  <div style="flex:1">
    <div class="wcard" style="padding:12px 14px">
      <div style="font-size:10px;font-weight:700;margin-bottom:6px">口径说明</div>
      <div style="font-size:10.5px;color:#5C5546;line-height:1.8">
      · 展示口径 <b>RAW</b>：交易所原始价格，用于市值与同行估值计算；<br>
      · 前复权 / 后复权当前快照未覆盖，页面不补值、不混用；<br>
      · 悬浮与表格数据均来自快照 bars[]，每根 K 线携带 Evidence ID；<br>
      · 点击数据点后 Evidence ID 跨页面保留，可跳转证据与校验。</div>
      <div style="font-size:9.5px;color:#A49B8A;margin-top:9px">最近收盘价证据：<span class="mono blu">EV-Q-0129</span></div>
    </div>
  </div>
</div>
"""

# ---------------------------------------------------------------- 同行估值
peers_body = f"""
{crumb("同行估值")}
<div class="eyebrow">同行估值 · RAW 价格与统一口径</div>
<div class="ptitlerow"><h1 class="pt">同行估值</h1><div class="ptag">仅现有本地图谱 · RAW 价格</div></div>

<div style="display:flex;gap:10px;margin-top:10px">
  <div class="tile" style="flex:1"><div class="lb"><span>本标的 PE · TTM</span><span class="eid">EV-V-011</span></div><div class="v num">19.2<small>x</small></div><div class="sub2">同业中位 16.0x</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>同业 PE 中位</span><span class="eid">n = 4</span></div><div class="v num">16.0<small>x</small></div><div class="sub2">IQR [14.1x, 18.2x]</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>本标的 PB · MRQ</span><span class="eid">EV-V-012</span></div><div class="v num">6.8<small>x</small></div><div class="sub2">历史分位缺失 · 已标注</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>异常值处理</span></div><div class="v" style="font-size:17px;padding-top:6px">IQR k = 1.5</div><div class="sub2">古井贡酒期间不匹配 · 已剔除</div></div>
</div>

<div class="wcard" style="margin-top:10px;padding:6px 15px 2px">
{peers_tbl}
</div>

<div style="display:flex;gap:14px;margin-top:11px">
  <div style="flex:1.25">
    <div class="sechead"><div class="t">计算口径</div><div class="s">确定性计算 · 非模型生成</div></div>
    <div class="wcard" style="padding:12px 15px">
      <div class="mono" style="font-size:11px;background:#F6F1E4;border-radius:8px;padding:9px 12px;line-height:1.9">
      PE·TTM = RAW 收盘价 × 总股本 ÷ TTM 归母净利<br>
      &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;= 1,243.88 × 12.56 亿 ÷ 814 亿 ≈ 19.2x<br>
      PB·MRQ = 总市值 ÷ 最近披露归母净资产</div>
      <div style="font-size:10.5px;color:#5C5546;line-height:1.8;margin-top:9px">
      · 同一行业（白酒）· 同一时点（2026-09-28）· 同一口径（RAW / 2025A+TTM）；<br>
      · 市值与估值只用 RAW 价格，不使用前复权价；<br>
      · 银行 / 保险等金融机构不适用 PE·PB 同业法，自动排除。</div>
    </div>
  </div>
  <div style="flex:1">
    <div class="sechead"><div class="t">服务层结论</div><div class="s">规则可复核 · 需人工确认</div></div>
    <div class="narr" style="border-left-color:#2B4A8B">可比公司 n=4，本标的 PE·TTM 19.2x 位于同业 Q3 上方、未触发 IQR 异常剔除；PB 历史分位缺失，估值结论标注为<span class="cite">部分依据</span>。结合 2026H1 归母净利 −2.0%，同业比较结论为「估值不便宜、增长承压」。
    <div class="nt" style="margin:6px 0 0"><span>服务层自动生成</span><span>需人工复核</span></div></div>
  </div>
</div>
"""

# ---------------------------------------------------------------- 证据与校验
evidence_body = f"""
{crumb("证据与校验")}
<div class="eyebrow">证据与校验 · Evidence Ledger</div>
<div class="ptitlerow"><h1 class="pt">证据与校验</h1><div class="ptag">校验通过 12/12 · 1 项部分缺失</div></div>

<div style="display:flex;gap:7px;margin:10px 0 2px">
  <span class="badge ok">● 校验通过 12/12</span><span class="badge warn">◑ 部分缺失 1</span>
  <span class="badge n">快照 600519.SH_20260928_tushare_v2</span><span class="badge n">Provider tushare · RAW</span>
</div>

<div class="wcard" style="margin-top:10px;padding:4px 15px 2px">
{ledger_tbl.replace("显示前 5","显示前 5 / 共 12 条").replace("EVIDENCE LEDGER · 12 条","EVIDENCE LEDGER · 按时点排序")}
</div>

<div style="display:flex;gap:14px;margin-top:11px">
  <div style="flex:1">
    <div class="sechead"><div class="t">校验检查</div><div class="s">Validator 闸门 · 全部留痕</div></div>
    <div class="wcard" style="padding:8px 15px">
      <table class="tb">
        <tr><td style="border-bottom:1px solid #EFE9DC">RAW 价格口径</td><td class="r" style="border-bottom:1px solid #EFE9DC"><span class="badge ok">通过</span></td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">币种 · 单位一致（CNY · 元）</td><td class="r" style="border-bottom:1px solid #EFE9DC"><span class="badge ok">通过</span></td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">期间匹配（2026H1 / TTM）</td><td class="r" style="border-bottom:1px solid #EFE9DC"><span class="badge ok">通过</span></td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">Evidence DAG 依赖完整</td><td class="r" style="border-bottom:1px solid #EFE9DC"><span class="badge ok">通过</span></td></tr>
        <tr><td>PB 历史分位</td><td class="r"><span class="badge warn">◑ 部分缺失 · 已标注</span></td></tr>
      </table>
    </div>
  </div>
  <div style="flex:1">
    <div class="sechead"><div class="t">Evidence DAG · 市值派生链</div><div class="s">事实 → 计算 → 结论</div></div>
    <div class="wcard" style="padding:14px 16px">
      <div style="display:flex;align-items:center;gap:9px">
        <div class="node" style="flex:1"><span class="eid">EV-Q-0129</span>收盘价 · RAW<br><b>1,243.88 元</b></div>
        <span class="mut" style="font-size:13px">×</span>
        <div class="node" style="flex:1"><span class="eid">EV-S-003</span>总股本<br><b>12.56 亿股</b></div>
        <span class="mut" style="font-size:13px">→</span>
        <div class="node" style="flex:1;background:#F6F1E4"><span class="eid">EV-C-002</span>总市值（计算）<br><b>1.56 万亿</b></div>
      </div>
      <div style="display:flex;align-items:center;gap:9px;margin-top:9px">
        <div class="node" style="flex:1"><span class="eid">EV-F-007</span>TTM 归母净利<br><b>814 亿</b></div>
        <span class="mut" style="font-size:13px">→</span>
        <div class="node" style="flex:1;background:#EFF2FA"><span class="eid">EV-V-011</span>PE · TTM（计算）<br><b>19.2x</b></div>
        <span class="mut" style="font-size:13px">→</span>
        <div class="node" style="flex:1;border-style:dashed"><span class="eid">GATE</span>Validator<br><span class="badge ok">允许输出</span></div>
      </div>
      <div style="font-size:9.5px;color:#A49B8A;margin-top:9px">每条事实带来源、期间、截止与币种/单位；计算节点标注依赖，供回放与审计。</div>
    </div>
  </div>
</div>

<div class="wcard" style="margin-top:11px;border-left:3px solid #B5544A;background:#FBF3F2;display:flex;gap:12px;align-items:center;padding:10px 15px">
  <span class="badge bad">● 阻断案例</span>
  <div style="font-size:11px;color:#5C5546;line-height:1.6"><b class="mono">600519.SH_illustrative_missing_shares_v1</b>（本地基准）：总股本证据缺失 → Validator 阻断估值与报告，页面隐藏确定性结论，<b>模型不得自行补数</b>。</div>
</div>
"""

# ---------------------------------------------------------------- AI Agent 研究
agent_body = f"""
{crumb("AI Agent 研究")}
<div class="eyebrow">AI Agent 研究 · 有界工具调用</div>
<div class="ptitlerow"><h1 class="pt">AI Agent 研究</h1><div class="ptag">工具 6 项 · 有界重试 · 全程留痕</div></div>

<div style="display:flex;gap:16px;margin-top:11px">
  <div style="flex:1.35">
    <div class="chatme"><div class="b">茅台现在贵不贵？帮我和同业比一下估值，并给出你引用的证据。</div></div>
    <div class="wcard" style="padding:12px 15px">
      <div style="display:flex;gap:6px;margin-bottom:7px">
        <span class="badge n">任务 #A-118</span><span class="badge ok">● 校验通过 12/12</span><span class="badge v">AI 生成 · 需人工复核</span>
      </div>
      <div style="font-size:12px;line-height:1.85">
        按 2026-09-28 RAW 收盘价 <b class="num">1,243.88 元</b>（<span class="cite">EV-Q-0129</span>）与总股本 <span class="cite">EV-S-003</span> 计算，总市值约 <b class="num">1.56 万亿</b>（<span class="cite">EV-C-002</span>）；以 TTM 归母净利 814 亿计，<b>PE·TTM ≈ 19.2x</b>（<span class="cite">EV-V-011</span>），高于同业中位 16.0x，位于同业 Q3 上方，未触发 IQR 剔除。2026H1 归母净利同比 <b class="dn num">−2.0%</b>（<span class="cite">EV-F-008</span>），增长阶段性承压。
        <div style="margin-top:6px;color:#5C5546">结论：当前估值<b>不便宜</b>；PB 历史分位缺失（<span class="cite">EV-V-012</span>），该维度未参与判断。本回答由模型生成，数值全部来自 Evidence Ledger，<b>需人工复核</b>。</div>
      </div>
      <div style="display:flex;gap:6px;margin-top:9px">
        <span class="badge n">证据 12 条</span><span class="badge n">耗时 8.0s</span><span class="badge n">工具 4 次调用</span>
      </div>
    </div>
    <div class="chatme" style="margin-top:12px"><div class="b">PB 那一档为什么没给结论？</div></div>
    <div class="wcard" style="padding:11px 15px">
      <div style="font-size:12px;line-height:1.8">PB·MRQ 当前值 <b class="num">6.8x</b>（<span class="cite">EV-V-012</span>）可以给出，但它的<b>历史分位数据缺失</b>，没有历史锚点就不下「贵 / 便宜」的判断——这是 Validator 的边界，不是模型谦虚。
        <div style="display:flex;gap:5px;margin-top:8px;flex-wrap:wrap">
          <span class="mono" style="font-size:9.5px;color:#2B4A8B;background:#EFF2FA;border-radius:4px;padding:2px 7px">EV-Q-0129</span>
          <span class="mono" style="font-size:9.5px;color:#2B4A8B;background:#EFF2FA;border-radius:4px;padding:2px 7px">EV-S-003</span>
          <span class="mono" style="font-size:9.5px;color:#2B4A8B;background:#EFF2FA;border-radius:4px;padding:2px 7px">EV-F-008</span>
          <span class="mono" style="font-size:9.5px;color:#2B4A8B;background:#EFF2FA;border-radius:4px;padding:2px 7px">EV-C-002</span>
          <span class="mono" style="font-size:9.5px;color:#2B4A8B;background:#EFF2FA;border-radius:4px;padding:2px 7px">EV-V-011</span>
          <span class="mono" style="font-size:9.5px;color:#9A6B14;background:#F6EEDC;border-radius:4px;padding:2px 7px">EV-V-012</span>
        </div>
      </div>
    </div>
    <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
      <div style="flex:1;background:#fff;border:1px solid #E7E0D2;border-radius:999px;padding:8px 15px;font-size:11px;color:#A49B8A">输入研究问题，例如：五粮液的估值锚是多少？</div>
      <div class="btn-dark">发送 →</div>
    </div>
  </div>
  <div style="flex:1">
    <div class="sechead"><div class="t">Agent 执行轨迹</div><div class="s">TRACE · 可回放</div></div>
    <div class="wcard" style="padding:6px 14px 2px">
      {trace_rows}
      <div style="padding:8px 2px;font-size:9.5px;color:#A49B8A;line-height:1.7">模型只负责理解问题与生成叙事；<b>数据来自工具、计算来自服务层、放行来自 Validator</b>。每一步可在「Agent 执行轨迹」页回放。</div>
    </div>
    <div class="wcard" style="margin-top:10px;padding:10px 14px;display:flex;gap:14px">
      <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">输入 tokens</div><div class="serif num" style="font-size:19px;font-weight:600">6,412</div></div>
      <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">输出 tokens</div><div class="serif num" style="font-size:19px;font-weight:600">518</div></div>
      <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">成本（估）</div><div class="serif num" style="font-size:19px;font-weight:600">¥ 0.04</div></div>
      <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">重试</div><div class="serif num" style="font-size:19px;font-weight:600">0 次</div></div>
    </div>
    <div class="wcard" style="margin-top:10px;padding:11px 14px">
      <div style="font-size:10px;font-weight:700;margin-bottom:7px">边界与降级</div>
      <div style="font-size:10.5px;color:#5C5546;line-height:1.85">
      · 工具重试有界（≤2 次），超限返回 fallback 状态而非编造；<br>
      · 模型不拥有数据源选择权、计算权与 Validator 放行权；<br>
      · dry-run 评测结果单独标注，不冒充真实模型表现。</div>
    </div>
  </div>
</div>
"""

# ---------------------------------------------------------------- 每日复盘
daily_body = f"""
{crumb("每日复盘")}
<div class="eyebrow">每日复盘 · 明确标注的演示数据</div>
<div class="ptitlerow"><h1 class="pt">每日复盘</h1><div class="ptag">synthetic_demo · 非实时</div></div>

<div style="display:flex;gap:7px;margin:10px 0 2px">
  <span class="badge n">数据来源：synthetic_demo</span><span class="badge warn">永不冒充实时行情</span><span class="badge n">不构成投资建议</span>
</div>

<div style="display:flex;gap:10px;margin-top:8px">
  <div class="tile" style="flex:1"><div class="lb"><span>上证指数</span></div><div class="v num">3,342.20</div><div class="sub2 num up">▲ +0.42%</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>深证成指</span></div><div class="v num">10,845.13</div><div class="sub2 num up">▲ +0.21%</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>创业板指</span></div><div class="v num">2,286.45</div><div class="sub2 num dn">▼ −0.35%</div></div>
  <div class="tile" style="flex:1"><div class="lb"><span>沪深 300</span></div><div class="v num">3,978.51</div><div class="sub2 num up">▲ +0.18%</div></div>
</div>

<div style="display:flex;gap:14px;margin-top:12px">
  <div style="flex:1">
    <div class="sechead"><div class="t">市场广度</div><div class="s">synthetic_demo · 演示口径</div></div>
    <div class="wcard" style="padding:13px 16px">
      <div style="display:flex;gap:20px">
        <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">上涨家数</div><div class="serif num up" style="font-size:24px;font-weight:600">2,986</div></div>
        <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">下跌家数</div><div class="serif num dn" style="font-size:24px;font-weight:600">2,104</div></div>
        <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">平盘</div><div class="serif num" style="font-size:24px;font-weight:600">182</div></div>
        <div><div style="font-size:9px;letter-spacing:.1em;color:#A49B8A">涨停 / 跌停</div><div class="serif num" style="font-size:24px;font-weight:600">41 / 12</div></div>
      </div>
      <div style="display:flex;height:8px;border-radius:5px;overflow:hidden;margin-top:11px">
        <div style="width:56.8%;background:#C53A3A"></div><div style="width:40%;background:#1F8A5D"></div><div style="width:3.2%;background:#D8D0BF"></div>
      </div>
      <div style="font-size:9.5px;color:#A49B8A;margin-top:6px">涨跌家数比 1.42 · 中位数涨幅 +0.31%（synthetic_demo）</div>
    </div>
  </div>
  <div style="flex:1">
    <div class="sechead"><div class="t">行业涨跌榜</div><div class="s">演示数据 · 覆盖 31 个一级行业</div></div>
    <div class="wcard" style="padding:4px 15px 2px">
      <table class="tb">
        <tr><td style="border-bottom:1px solid #EFE9DC">领涨 · 电力设备</td><td class="r up num" style="border-bottom:1px solid #EFE9DC">+1.62%</td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">领涨 · 通信</td><td class="r up num" style="border-bottom:1px solid #EFE9DC">+1.18%</td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">领跌 · 煤炭</td><td class="r dn num" style="border-bottom:1px solid #EFE9DC">−1.12%</td></tr>
        <tr><td>领跌 · 白酒</td><td class="r dn num">−0.84%</td></tr>
      </table>
    </div>
    <div class="wcard" style="margin-top:10px;padding:10px 14px;font-size:10.5px;color:#5C5546;line-height:1.7">
      复盘事实与计算均由服务层生成；<b>演示数据永远带 synthetic_demo 标注</b>，不会混入联网快照，也不会被当作实时行情展示。
    </div>
  </div>
</div>

<div style="display:flex;gap:14px;margin-top:12px">
  <div style="flex:1.4">
    <div class="sechead"><div class="t">近 20 个交易日 · 上证指数收盘</div><div class="s">synthetic_demo · 仅演示复盘形态</div></div>
    <div class="wcard" style="padding:14px 16px 10px">
      <svg width="620" height="64" viewBox="0 0 620 64">
        <polyline points="3,46 35,42 67,44 99,39 131,41 163,36 195,38 227,33 259,35 291,30 323,32 355,27 387,29 419,24 451,26 483,21 515,23 547,18 579,20 611,15"
          fill="none" stroke="#2B4A8B" stroke-width="1.6"/>
        <line x1="3" y1="58" x2="611" y2="58" stroke="#EFE9DC"/>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:9px;color:#A49B8A;margin-top:3px"><span>08-31</span><span>09-28</span></div>
    </div>
  </div>
  <div style="flex:1">
    <div class="sechead"><div class="t">数据覆盖与限制</div><div class="s">服务层自述 · 不夸大覆盖</div></div>
    <div class="wcard" style="padding:12px 16px">
      <table class="tb">
        <tr><td style="border-bottom:1px solid #EFE9DC">覆盖范围</td><td class="r" style="border-bottom:1px solid #EFE9DC"><b>沪深北 A 股 · 31 个一级行业</b></td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">指数</td><td class="r num" style="border-bottom:1px solid #EFE9DC">4 只（synthetic_demo）</td></tr>
        <tr><td style="border-bottom:1px solid #EFE9DC">财报深度</td><td class="r" style="border-bottom:1px solid #EFE9DC">2026H1 · 覆盖不足处明确标注</td></tr>
        <tr><td>已知限制</td><td class="r"><span class="badge warn">◑ 演示数据 · 非实时</span></td></tr>
      </table>
    </div>
  </div>
</div>
"""

PAGES = {
    "overview.html": ("研究总览", overview_body),
    "market.html": ("市场与行情", market_body),
    "peers.html": ("同行估值", peers_body),
    "evidence.html": ("证据与校验", evidence_body),
    "agent.html": ("AI Agent 研究", agent_body),
    "daily.html": ("每日复盘", daily_body),
}

for fname, (active, body) in PAGES.items():
    (OUT / fname).write_text(page(active, active, body), encoding="utf-8")
    print("wrote", fname)
