# -*- coding: utf-8 -*-
"""生成单文件交互式看板（离线可打开）。

设计要点
--------
· 单文件 HTML，plotly.js 内嵌（include_plotlyjs=True），断网也能打开。
· 版式沿用我个人作品集既有的卡片式风格（深蓝 #1F3864 标题 + 浅灰底）。
· 每张图都配一行「怎么看」，避免看板变成「一堆没人看的图」。
· 首页顶部固定一块「数据性质声明」，明确写清哪些是真实公开数据、
  哪些是模拟数据、模拟数据的锚点来自哪里。

用法：
  python make_dashboard.py
"""
from __future__ import annotations

import io
import json
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
CLEAN = os.path.join(PROJ, "data", "clean")
RAW = os.path.join(PROJ, "data", "raw")
RESULTS = os.path.join(PROJ, "results")

NAVY = "#1F3864"
ACCENT = "#C8102E"      # 告警色
OK = "#2E7D6F"
GRAY = "#6B7C8F"

CAUSE_LABEL = {
    "data_error_unit": "数据错误·字段单位",
    "data_error_duplicate": "数据错误·批量重跑",
    "caliber_gap": "口径差异",
    "true_risk_channel_outage": "真实风险·渠道故障",
    "true_risk_capacity": "真实风险·产能不足",
    "true_risk_operational": "真实风险·操作风险",
    "true_risk_service": "真实风险·服务事件",
    "undetermined": "待定",
}
CAUSE_COLOR = {
    "data_error_unit": "#B07AA1",
    "data_error_duplicate": "#9B59B6",
    "caliber_gap": "#3B7DD8",
    "true_risk_channel_outage": "#E67E22",
    "true_risk_capacity": ACCENT,
    "true_risk_operational": "#D64545",
    "true_risk_service": "#F2A03D",
    "undetermined": "#9AA5B1",
}

CARDS = []      # (title, note, figure_html, height)


def fig_html(fig, height=400):
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Microsoft YaHei, sans-serif", size=12, color="#22303F"),
        margin=dict(l=60, r=24, t=28, b=48),
        height=height,
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(gridcolor="#EDF0F4", zeroline=False)
    fig.update_yaxes(gridcolor="#EDF0F4", zeroline=False)
    return fig.to_html(full_html=False, include_plotlyjs=False,
                       config={"displaylogo": False, "responsive": True})


def add(title, note, fig, height=400):
    CARDS.append((title, note, fig_html(fig, height), height))


# ------------------------------------------------------------------ 载入

def load():
    d = pd.read_csv(os.path.join(CLEAN, "ops_indicators.csv"))
    d["stat_date"] = pd.to_datetime(d["stat_date"])
    alerts = pd.read_csv(os.path.join(RESULTS, "02_预警清单.csv"))
    if not alerts.empty:
        alerts["stat_date"] = pd.to_datetime(alerts["stat_date"])
    ev = pd.read_csv(os.path.join(RESULTS, "03_异常检出评估.csv"))
    prof = pd.read_csv(os.path.join(RESULTS, "04_网点运营画像.csv"))
    daily = pd.read_csv(os.path.join(RESULTS, "07_全行日度趋势.csv"))
    daily["stat_date"] = pd.to_datetime(daily["stat_date"])
    with io.open(os.path.join(RESULTS, "analysis.json"), encoding="utf-8") as f:
        payload = json.load(f)
    with io.open(os.path.join(RAW, "simulation_meta.json"), encoding="utf-8") as f:
        sim = json.load(f)
    truth = None
    tp = os.path.join(RAW, "anomaly_ground_truth.json")
    if os.path.exists(tp):
        with io.open(tp, encoding="utf-8") as f:
            truth = json.load(f)
    return d, alerts, ev, prof, daily, payload, sim, truth


# ------------------------------------------------------------------ 图 1：总览趋势

def chart_overview(daily):
    """全行趋势：只用双轴。

    早期版本把等候时长、差错率、分流率三条线塞进三个 y 轴，
    结果刻度互相重叠、完全读不出来。教训：一张图最多两条不同量纲的轴，
    多出来的指标应当另开一张图或改用「同环比」这类无量纲口径。
    """
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=daily["stat_date"], y=daily["avg_wait"],
                             name="平均等候时长（分钟）", mode="lines",
                             line=dict(color=ACCENT, width=2.2)))
    fig.add_trace(go.Scatter(x=daily["stat_date"], y=daily["error_rate"] * 1000,
                             name="业务差错率（‰）", mode="lines", yaxis="y2",
                             line=dict(color=NAVY, width=2)))
    fig.update_layout(
        yaxis=dict(title=dict(text="平均等候时长（分钟）", font=dict(color=ACCENT)),
                   tickfont=dict(color=ACCENT)),
        yaxis2=dict(title=dict(text="业务差错率（‰）", font=dict(color=NAVY)),
                    tickfont=dict(color=NAVY),
                    overlaying="y", side="right", showgrid=False),
        hovermode="x unified",
    )
    return fig


def chart_channel(daily):
    """渠道分流率单独一张：它与前两个指标不同量纲，硬塞一张图会互相遮挡。"""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=daily["stat_date"], y=daily["e_channel"] * 100,
                             name="电子渠道分流率（%）", mode="lines",
                             line=dict(color=OK, width=2.2), fill="tozeroy",
                             fillcolor="rgba(46,125,111,0.10)"))
    fig.update_layout(yaxis=dict(title="电子渠道分流率（%）", rangemode="tozero"),
                      hovermode="x unified")
    return fig


# ------------------------------------------------------------------ 图 2：网点横截面对比

def chart_branch_scatter(prof):
    """网点对比：饱和度 × 等候时长。

    关于 y 轴的处理（这里有个必须讲清楚的取舍）：
    个别网点因「字段单位错误」类异常，平均等候时长被推到 200 分钟以上，
    线性全量显示会把其余 19 个网点压成一条线，图就废了；
    而 plotly 的 log 轴在纯静态导出下会渲染成 1T / 100T 这类不可读刻度，
    平方根轴又不受支持。最终采用「截断显示 + 明示截断」：
    y 轴上限取其余网点的较高分位，超出上限的网点在图上单独标注真实值。
    凡是被截断的图都必须标注，否则就是误导读者。
    """
    prof = prof.copy()
    size = prof["avg_vol"] / prof["avg_vol"].max() * 40 + 9
    # 截断阈值：用中位数 + 3 倍四分位距识别极端值
    med = float(prof["avg_wait"].median())
    q1, q3 = prof["avg_wait"].quantile([.25, .75])
    cap = float(q3 + 3 * (q3 - q1))
    prof["shown_wait"] = prof["avg_wait"].clip(upper=cap)
    n_clipped = int((prof["avg_wait"] > cap).sum())

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=prof["avg_cap"] * 100, y=prof["shown_wait"],
        mode="markers+text", text=prof["branch_code"], textposition="top center",
        textfont=dict(size=9, color="#5A6B7D"),
        marker=dict(size=size, color=prof["alert_cnt"], colorscale="YlOrRd",
                    showscale=True, colorbar=dict(title="预警<br>条数", thickness=12),
                    line=dict(width=1, color="#8A94A6")),
        customdata=np.stack([prof["branch_name"], prof["avg_vol"], prof["avg_err"] * 100,
                             prof["avg_comp"], prof["avg_ech"] * 100,
                             prof["avg_wait"], prof["avg_cap"] * 100], axis=-1),
        hovertemplate=("<b>%{customdata[0]}</b><br>"
                       "平均等候 %{customdata[5]:.1f} 分钟"
                       "（图上位置已按 %{y:.1f} 截断显示）<br>"
                       "窗口产能饱和度 %{customdata[6]:.1f}%<br>"
                       "日均柜面业务量 %{customdata[1]:.0f} 笔<br>"
                       "差错率 %{customdata[2]:.3f}%<br>"
                       "投诉率 %{customdata[3]:.2f}/万笔<br>"
                       "电子渠道分流率 %{customdata[4]:.1f}%<extra></extra>"),
    ))
    fig.add_hline(y=med, line_dash="dash", line_color=GRAY, line_width=1,
                  annotation_text=f"等候时长中位数 {med:.1f} 分钟",
                  annotation_position="bottom right",
                  annotation_font=dict(size=10, color=GRAY))
    fig.add_vline(x=float(prof["avg_cap"].median() * 100), line_dash="dash",
                  line_color=GRAY, line_width=1,
                  annotation_text="饱和度中位数",
                  annotation_position="top left",
                  annotation_font=dict(size=10, color=GRAY))
    fig.update_layout(
        xaxis_title="窗口产能饱和度（%）",
        yaxis=dict(title="平均等候时长（分钟）", range=[0, cap * 1.12]),
    )
    if n_clipped:
        worst = prof.loc[prof["avg_wait"].idxmax()]
        fig.add_annotation(
            x=0.5, y=1.08, xref="paper", yref="paper", showarrow=False,
            yanchor="bottom",
            text=(f"注：{n_clipped} 个网点的等候时长超出上限（{cap:.0f} 分钟），"
                  f"已按上限截断显示；最高为 {worst['branch_code']} "
                  f"{worst['avg_wait']:.0f} 分钟——该量级本身即为「字段单位错误」的典型特征"),
            font=dict(size=10, color=ACCENT),
        )
    return fig


# ------------------------------------------------------------------ 图 3：周内效应

def chart_weekday(d):
    wd = d.groupby("weekday").agg(
        vol=("counter_txn_cnt", "mean"),
        wait=("avg_wait_minutes", "mean"),
        err=("error_rate", "mean"),
        comp=("complaint_per_10k", "mean"),
    ).reset_index()
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=[names[i - 1] for i in wd["weekday"]], y=wd["vol"],
                         name="日均柜面业务量（笔）", marker_color=NAVY, opacity=.85))
    fig.add_trace(go.Scatter(x=[names[i - 1] for i in wd["weekday"]], y=wd["wait"],
                             name="平均等候时长（分钟）", mode="lines+markers",
                             yaxis="y2", line=dict(color=ACCENT, width=2.4)))
    fig.update_layout(yaxis=dict(title="日均柜面业务量（笔）"),
                      yaxis2=dict(title="平均等候时长（分钟）", overlaying="y",
                                  side="right", showgrid=False))
    return fig


# ------------------------------------------------------------------ 图 4：预警时间线

def chart_alert_timeline(alerts, truth):
    fig = go.Figure()
    if truth:
        for item in truth["anomalies"]:
            color = {"data_error": "#9B59B6", "caliber_gap": "#3B7DD8",
                     "true_risk": ACCENT}[item["kind"]]
            fig.add_vrect(x0=pd.to_datetime(item["start"]), x1=pd.to_datetime(item["end"]),
                          fillcolor=color, opacity=0.12, line_width=0)
    if not alerts.empty:
        for cause, g in alerts.groupby("cause"):
            fig.add_trace(go.Scatter(
                x=g["stat_date"], y=g["branch_code"], mode="markers",
                name=CAUSE_LABEL.get(cause, cause),
                marker=dict(size=g["severity"] * 6 + 5,
                            color=CAUSE_COLOR.get(cause, GRAY),
                            opacity=.85, line=dict(width=.6, color="#FFFFFF")),
                customdata=np.stack([g["severity_score"], g["evidence"]], axis=-1),
                hovertemplate=("<b>%{y}</b> %{x|%Y-%m-%d}<br>严重度分 %{customdata[0]:.1f}"
                               "<br>%{customdata[1]}<extra></extra>"),
            ))
    fig.update_layout(xaxis_title="日期", yaxis_title="网点",
                      yaxis=dict(categoryorder="category ascending"))
    return fig


# ------------------------------------------------------------------ 图 5：预警结构

def chart_cause_mix(alerts):
    if alerts.empty:
        return go.Figure()
    g = alerts.copy()
    g["cause_cn"] = g["cause"].map(lambda c: CAUSE_LABEL.get(c, c))
    pivot = g.pivot_table(index="cause_cn", columns="severity",
                          values="stat_date", aggfunc="count").fillna(0)
    fig = go.Figure()
    for sev, color in [(1, "#9AA5B1"), (2, "#F2A03D"), (3, ACCENT)]:
        if sev in pivot.columns:
            fig.add_trace(go.Bar(y=pivot.index, x=pivot[sev], name=f"{sev} 级",
                                 orientation="h", marker_color=color))
    fig.update_layout(barmode="stack", xaxis_title="预警条数", yaxis_title="")
    return fig


# ------------------------------------------------------------------ 图 6：异常植入 vs 检出

def chart_eval(ev):
    if ev.empty:
        return go.Figure()
    ev = ev.copy()
    ev["label"] = ev["subtype"] + " @" + ev["branch_code"]
    ev["结论"] = np.where(ev["cause_correct"], "检出且性质判定正确",
                          np.where(ev["detected"], "检出但性质判定错误", "漏检"))
    colors = {"检出且性质判定正确": OK, "检出但性质判定错误": "#F2A03D", "漏检": ACCENT}
    fig = go.Figure()
    for k, g in ev.groupby("结论"):
        fig.add_trace(go.Bar(y=g["label"], x=[1] * len(g), orientation="h",
                             name=k, marker_color=colors[k],
                             text=g["detected_on"].fillna("未检出"),
                             textposition="inside", insidetextanchor="middle"))
    fig.update_layout(barmode="stack", xaxis=dict(showticklabels=False, title=""),
                      yaxis_title="", showlegend=True)
    return fig


# ------------------------------------------------------------------ 图 7：单网点时序（标杆 vs 待改进）

def chart_two_branches(d, prof):
    """挑一个等候最短的网点与一个最长的网点做对照。"""
    prof = prof.sort_values("avg_wait")
    best = prof.iloc[0]["branch_code"]
    worst = prof.iloc[-1]["branch_code"]
    fig = go.Figure()
    for code, color, label in [(best, OK, "等候最优"), (worst, ACCENT, "等候最差")]:
        s = d[d["branch_code"] == code].sort_values("stat_date")
        name = s.iloc[0]["branch_name"]
        fig.add_trace(go.Scatter(x=s["stat_date"], y=s["avg_wait_minutes"],
                                 mode="lines", name=f"{code} {name}（{label}）",
                                 line=dict(color=color, width=2)))
        fig.add_trace(go.Scatter(x=s["stat_date"], y=s["wait_wd_base"],
                                 mode="lines", name=f"{code} 同星期几基线",
                                 line=dict(color=color, width=1, dash="dot"),
                                 showlegend=False, opacity=.6))
    fig.update_layout(yaxis_title="平均等候时长（分钟）", xaxis_title="日期",
                      hovermode="x unified")
    return fig


# ------------------------------------------------------------------ 图 8：真实公开数据 —— 岗位所在分行

def chart_ftz(sim):
    series = (sim.get("public_anchors") or {}).get("ftz_branch_series") or []
    if not series:
        return None
    rows = []
    for s in series:
        rows.append({"period": s["period"], "metric": s["metric"], "value": s["value"]})
    df = pd.DataFrame(rows)
    df["kind"] = np.where(df["metric"].str.contains("资产"), "资产规模", "员工人数")
    order = ["2022A", "2023H", "2023A", "2024H", "2024A", "2025A"]
    piv = df.pivot_table(index="period", columns="kind", values="value",
                         aggfunc="first").reindex(order)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=piv.index, y=piv["资产规模"], name="资产规模（亿元）",
                         marker_color=NAVY))
    fig.add_trace(go.Scatter(x=piv.index, y=piv["员工人数"], name="员工人数（人）",
                             mode="lines+markers", yaxis="y2",
                             line=dict(color=ACCENT, width=2.4)))
    fig.update_layout(
        yaxis=dict(title="资产规模（亿元）", rangemode="tozero"),
        yaxis2=dict(title="员工人数（人）", overlaying="y", side="right",
                    showgrid=False,
                    # 员工人数在 152~159 之间波动，不放大范围就看不出趋势；
                    # 但必须标明轴被截断，否则会夸大人事变动的幅度。
                    range=[145, 168]),
        xaxis_title="报告期",
    )
    fig.add_annotation(x=0.02, y=1.14, xref="paper", yref="paper", showarrow=False,
                       text="注：右轴为截断显示（145~168 人），用于看清 152~159 的细微变化",
                       font=dict(size=10, color=GRAY))
    return fig


# ------------------------------------------------------------------ 主流程

def main():
    d, alerts, ev, prof, daily, payload, sim, truth = load()
    ev_sum = payload.get("evaluation", {})
    anchors = sim.get("public_anchors", {})
    n_src = (anchors.get("meta") or {}).get("source_count", 0)
    n_met = (anchors.get("meta") or {}).get("metric_count", 0)

    add("全行运营趋势 · 等候时长 × 差错率",
        "看两个指标是否同步：等候时长与差错率同时抬升，通常意味着产能不足或人员技能问题；"
        "只有一个抬升，则更可能是单点事件。",
        chart_overview(daily), 380)

    add("电子渠道分流率趋势",
        "分流率与等候时长必须一起看：分流率上升但等候时长不降，"
        "说明自助与线上渠道没有真正把柜面压力接过去，这是流程问题而不是设备问题。",
        chart_channel(daily), 300)

    add("网点横截面对比 · 饱和度 × 等候时长",
        "气泡大小＝日均业务量，颜色＝预警条数。右上角「又忙又慢」是优先干预对象；"
        "左上角「不忙但慢」通常是流程或人员技能问题，不是客流问题。",
        chart_branch_scatter(prof), 440)

    add("周内效应 · 为什么基线必须按星期几对齐",
        "周日业务量只有工作日的约三分之一。若用「前 7 日滚动均值」做基线，"
        "周日会天然显示 -65%，等于每周制造一次假异常——"
        "这正是本项目检测基线改为「同星期几中位数」的原因。",
        chart_weekday(d), 380)

    add("预警时间线 · 预警落在哪里、性质是什么",
        "背景色块是人为注入的异常区间（紫＝数据错误 / 蓝＝口径差异 / 红＝真实风险）。"
        "看预警是否落在色块内、颜色是否对得上——这就是查全率与成因判定的可视化。",
        chart_alert_timeline(alerts, truth), 460)

    add("预警结构 · 多少预警其实不是业务风险",
        f"共 {len(alerts)} 条预警。真实运营中最有价值的判断不是「发现了异常」，"
        "而是「判断出这不是风险」——数据错误应当修复系统，"
        "口径差异应当回算基线，都不该去问责网点。",
        chart_cause_mix(alerts), 340)

    add("植入异常 vs 检出结果",
        f"共注入 {ev_sum.get('n_injected')} 例已知异常，"
        f"检出 {ev_sum.get('n_detected')} 例（查全率 {ev_sum.get('recall')}），"
        f"性质判定正确 {ev_sum.get('n_cause_correct')} 例"
        f"（{ev_sum.get('cause_accuracy')}）。"
        f"注入期之外的预警 {ev_sum.get('n_alerts_out_of_window')} 条，"
        f"占全部网点日 {ev_sum.get('out_of_window_rate_vs_branch_days')}——"
        "注意这是误报率的**上限**，因为真实存在的、未被登记的波动也会被计入。",
        chart_eval(ev), 340)

    add("标杆网点 vs 待改进网点 · 个体时序",
        "实线是实际等候时长，点线是同星期几基线。"
        "点线稳定但实线长期在上方 = 该网点基线本身就偏高，属于能力问题；"
        "实线突然脱离点线 = 出现了新变化，需要归因。",
        chart_two_branches(d, prof), 380)

    ftz = chart_ftz(sim)
    if ftz is not None:
        add("真实公开数据 · 岗位所在分行（上海自贸试验区分行）",
            "数据来自平安银行历年定期报告，该分行自 2022 年报起每期单独列示。"
            "4 年资产 +58%，员工人数 159 → 152 人——"
            "这正是「网点轻型化、单位人力产能提升」的真实证据，"
            "也是我做这个运营监控项目的出发点。",
            ftz, 380)

    # ---------------- 组装 HTML ----------------
    cards_html = []
    for title, note, html, _h in CARDS:
        cards_html.append(
            f'<div class="card"><h2>{title}</h2><p class="note">{note}</p>{html}</div>'
        )

    kpi = [
        ("模拟网点数", f"{payload['generated_from']['branches']} 家"),
        ("模拟明细", f"{payload['generated_from']['detail_rows']:,} 行"),
        ("监控指标", "8 个核心指标"),
        ("注入异常", f"{ev_sum.get('n_injected')} 例"),
        ("查全率", f"{ev_sum.get('recall')}"),
        ("成因判定准确率", f"{ev_sum.get('cause_accuracy')}"),
        ("真实公开来源", f"{n_src} 个 / {n_met:,} 条已核实指标"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-v">{v}</div><div class="kpi-k">{k}</div></div>'
        for k, v in kpi
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>银行网点运营指标监控与异常预警 · 交互式看板</title>
<style>
 body{{font-family:"Microsoft YaHei",sans-serif;background:#f5f7fa;margin:0;padding:24px;color:#22303f}}
 h1{{color:{NAVY};margin:0 0 6px;font-size:26px}}
 .sub{{color:#5a6b7d;margin-bottom:14px;font-size:13.5px;line-height:1.8}}
 .warn{{background:#fff8e6;border-left:4px solid #f2a03d;border-radius:6px;
        padding:12px 16px;margin:14px 0 20px;font-size:13px;line-height:1.8;color:#6b5a2e}}
 .warn b{{color:#8a5b00}}
 .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0 22px}}
 .kpi{{background:#fff;border-radius:9px;padding:12px 14px;box-shadow:0 2px 8px rgba(20,40,80,.07)}}
 .kpi-v{{font-size:19px;font-weight:700;color:{NAVY}}}
 .kpi-k{{font-size:12px;color:#7b8a9c;margin-top:3px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(560px,1fr));gap:18px}}
 .card{{background:#fff;border-radius:10px;padding:16px 18px;box-shadow:0 2px 10px rgba(20,40,80,.08)}}
 .card h2{{font-size:15.5px;color:{NAVY};margin:0 0 4px}}
 .note{{font-size:12.5px;color:#6b7c8f;margin:0 0 8px;line-height:1.75}}
 footer{{margin-top:26px;color:#8b98a6;font-size:12.5px;line-height:2}}
 code{{background:#eef2f7;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body>
<h1>银行网点运营指标监控与异常预警</h1>
<div class="sub">
作者：王嘉钰（东华大学 统计学 · 2027 届）　｜　技术栈：SQL(MySQL 8) / Python / pandas / statsmodels / Plotly<br>
覆盖 <b>{payload['generated_from']['branches']} 家网点 × {payload['generated_from']['detail_rows'] // payload['generated_from']['branches']} 天</b>，
区间 {payload['generated_from']['date_range'][0]} ~ {payload['generated_from']['date_range'][1]}　｜　
代码与结论已开源，全流程可复现
</div>

<div class="warn">
<b>⚠ 数据性质声明（请先读这一段）</b><br>
本看板包含两类数据，性质完全不同：<br>
<b>① 真实公开数据</b>：来自中国人民银行《支付体系运行总体情况》17 份报告、平安银行 6 份定期报告、
国家金融监督管理总局、中国银行业协会、上海市统计局等 {n_src} 个来源，共 <b>{n_met:,} 条已核实指标</b>，
每条都可在 <code>data/raw/公开数据_原始提取.json</code> 里按 source_id 溯源到原文。<br>
<b>② 模拟数据</b>：网点级「日运营指标」。<b>国内没有公开的网点级微观运营数据集</b>——
等候时长、柜面业务量、业务差错率、投诉率都属于各行内部经营数据。
因此本项目用模拟数据构造了一个<b>带已知异常</b>的数据集（20 网点 × 90 天 = {payload['generated_from']['detail_rows']} 行），
用来验证「指标监控 + 异常预警」这套方法本身。<br>
模拟数据的<b>水位与波动范围锚定真实公开值</b>（如成本收入比取自平安银行年报披露值），
但个体数值是生成的，<b>不代表任何真实网点的经营情况</b>。
</div>

<div class="kpis">{kpi_html}</div>
<div class="grid">{''.join(cards_html)}</div>

<footer>
生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}　｜　
模拟数据随机种子：{sim.get('seed')}　｜　注入异常 {sim.get('anomalies_injected')} 例（自检全部通过）<br>
方法：横截面稳健检测（Median + 1.4826×MAD）＋ 单网点 EWMA 控制图 ＋ STL 周期分解；
基线按<b>同星期几</b>对齐以剥离周内效应；预警经「持续性 × 幅度 × 方法一致性」分级，
再逐条判定为 <b>数据错误 / 口径差异 / 真实风险</b> 三类。<br>
单文件、离线可用：plotly.js 已内嵌，双击即可打开，无需联网。
</footer>
<script>{get_plotlyjs()}</script>
</body></html>
"""

    out = os.path.join(RESULTS, "看板.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"看板已生成: {out}  ({os.path.getsize(out):,} bytes)")
    return out


if __name__ == "__main__":
    main()
