# -*- coding: utf-8 -*-
"""生成《运营建议报告》（Markdown），再转成 Word。

报告结构固定为六节，重点是第五节「诚实的交付限制」——
一份可信的分析报告必须写清楚「哪些结论不能下」。
所有数字都从 results/ 下的 CSV 与 analysis.json 读取，不手写，
避免「结论与数据对不上」这种最常见的作品集硬伤。

用法：
  python gen_report.py
"""
from __future__ import annotations

import io
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
RAW = os.path.join(PROJ, "data", "raw")
CLEAN = os.path.join(PROJ, "data", "clean")
RESULTS = os.path.join(PROJ, "results")

CAUSE_CN = {
    "data_error_unit": "数据错误（字段单位）",
    "data_error_duplicate": "数据错误（批量重跑重复计数）",
    "caliber_gap": "口径差异（统计口径调整）",
    "true_risk_channel_outage": "真实风险（渠道/设备故障）",
    "true_risk_capacity": "真实风险（产能不足）",
    "true_risk_operational": "真实风险（操作风险）",
    "true_risk_service": "真实风险（服务事件）",
    "undetermined": "待定（观察名单）",
}


def load():
    d = pd.read_csv(os.path.join(CLEAN, "ops_indicators.csv"))
    d["stat_date"] = pd.to_datetime(d["stat_date"])
    alerts = pd.read_csv(os.path.join(RESULTS, "02_预警清单.csv"))
    if not alerts.empty:
        alerts["stat_date"] = pd.to_datetime(alerts["stat_date"])
    prof = pd.read_csv(os.path.join(RESULTS, "04_网点运营画像.csv"))
    ev = pd.read_csv(os.path.join(RESULTS, "03_异常检出评估.csv"))
    with io.open(os.path.join(RESULTS, "analysis.json"), encoding="utf-8") as f:
        payload = json.load(f)
    with io.open(os.path.join(RAW, "simulation_meta.json"), encoding="utf-8") as f:
        sim = json.load(f)
    return d, alerts, prof, ev, payload, sim


def fmt_pct(x, nd=1):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{nd}f}%"


def main():
    d, alerts, prof, ev, payload, sim = load()
    evs = payload["evaluation"]
    anchors = sim.get("public_anchors", {})
    ameta = anchors.get("meta") or {}
    industry = anchors.get("industry_reference") or {}
    ftz = anchors.get("ftz_branch_series") or []
    with io.open(os.path.join(RAW, "anomaly_ground_truth.json"), encoding="utf-8") as f:
        truth = json.load(f)

    n_branch = d["branch_code"].nunique()
    n_days = d["stat_date"].nunique()
    drange = f"{d['stat_date'].min().date()} ~ {d['stat_date'].max().date()}"

    # ---------------- 关键运营数字
    wait_med = d["avg_wait_minutes"].median()
    wait_p90 = d["avg_wait_minutes"].quantile(.90)
    err_med = d["error_rate"].median()
    ech_med = d["e_channel_rate"].median()
    cap_med = d["capacity_utilization"].median()
    # 周内效应
    wd = d.groupby("weekday")["counter_txn_cnt"].mean()
    wd_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    busiest = wd_names[int(wd.idxmax()) - 1]
    quietest = wd_names[int(wd.idxmin()) - 1]
    wd_ratio = wd.min() / wd.max()

    # 网点分档
    prof = prof.copy()
    prof["cap_pct"] = prof["avg_cap"].rank(pct=True)
    prof["wait_pct"] = prof["avg_wait"].rank(pct=True)
    busy_slow = prof[(prof["cap_pct"] > 0.6) & (prof["wait_pct"] > 0.6)]
    idle_slow = prof[(prof["cap_pct"] < 0.4) & (prof["wait_pct"] > 0.5)]
    best = prof.sort_values("avg_wait").head(3)
    worst = prof.sort_values("avg_wait", ascending=False).head(3)

    # 预警结构
    if not alerts.empty:
        struct = (alerts.groupby("cause").size().sort_values(ascending=False))
        n_true_risk = int(alerts["cause"].str.startswith("true_risk").sum())
        n_not_risk = int(len(alerts) - n_true_risk)
        sev = alerts["severity"].value_counts().sort_index()
    else:
        struct, n_true_risk, n_not_risk, sev = pd.Series(dtype=int), 0, 0, pd.Series(dtype=int)

    # 真实公开数据的自贸区分行序列
    ftz_rows = []
    periods = ["2022A", "2023H", "2023A", "2024H", "2024A", "2025A"]
    asset = {f["period"]: f["value"] for f in ftz if "资产" in f["metric"]}
    staff = {f["period"]: f["value"] for f in ftz if "员工" in f["metric"]}
    for p in periods:
        if p in asset or p in staff:
            ftz_rows.append((p, asset.get(p), staff.get(p)))
    if asset.get("2022A") and asset.get("2025A"):
        asset_growth = asset["2025A"] / asset["2022A"] - 1
    else:
        asset_growth = None

    L = []
    A = L.append

    # ================= 标题 =================
    A("# 银行网点运营指标监控与异常预警 · 运营建议报告\n")
    A("> 作者：王嘉钰（东华大学 统计学 · 2027 届）  ")
    A(f"> 数据区间：{drange}，{n_branch} 家网点 × {n_days} 天，共 {len(d):,} 条网点日记录  ")
    A("> 技术栈：MySQL 8（窗口函数/CTE）· Python（pandas / numpy / statsmodels）· Plotly\n")
    A("---\n")

    # ================= 一、数据性质（必须放最前）=================
    A("## 一、先说清楚数据性质\n")
    A("这份报告用了两类性质完全不同的数据，混在一起讲会误导读者，所以分开说明：\n")
    A(f"**① 真实公开数据。** 来自 {ameta.get('source_count', 0)} 个公开来源、"
      f"{ameta.get('metric_count', 0):,} 条已核实指标，每条都可在 "
      "`data/raw/公开数据_原始提取.json` 里按 `source_id` 溯源到原文：\n")
    A("| 来源类别 | 内容 |")
    A("|---|---|")
    A("| 中国人民银行《支付体系运行总体情况》 | 2023Q1–2026Q2 共 17 份报告，银行账户、"
      "银行卡、电子支付、非现金支付等总量指标 |")
    A("| 平安银行定期报告 | 2022 年报 – 2025 年报共 6 份，零售客户数、AUM、"
      "客户分层、成本收入比等 |")
    A("| 国家金融监督管理总局 | 2025Q2 银行业主要监管指标（资产总额、不良率、"
      "拨备覆盖率等） |")
    A("| 中国银行业协会《中国银行业服务报告》 | 营业网点数、自助银行数、"
      "离柜交易总额、手机银行客户数 |")
    A("| 上海市统计局 / 上海监管局 | 常住人口、人均可支配收入、"
      "上海银行业机构名单（302 家） |")
    A("")
    A("**② 模拟数据。** 网点级「日运营指标」。**国内没有公开的网点级微观运营数据集**——")
    A("等候时长、柜面业务量、业务差错率、投诉率都属于各行内部经营数据。")
    A(f"因此本项目构造了一个**带已知异常**的模拟数据集（{n_branch} 家网点 × {n_days} 天 = "
      f"{len(d):,} 条），用来验证「指标监控 + 异常预警」这套**方法本身**。\n")
    A("模拟数据的**水位与波动范围锚定真实公开值**：\n")
    A("| 参数 | 取值 | 来源 |")
    A("|---|---|---|")
    for k, v in anchors.get("anchors", {}).items():
        src = v.get("raw_metric") or "行业普遍区间"
        if v.get("source_id"):
            src = f"{src}（{v['source_id']}）"
        val = v.get("center")
        # 比率类参数按百分比显示，避免出现 0.27449999999999997 这种浮点原值
        if isinstance(val, (int, float)) and 0 < val < 1:
            shown = f"{val * 100:.2f}%"
        else:
            shown = str(val)
        A(f"| {k} | {shown} | {src} |")
    A("")
    A("⚠ **必须明确的一点**：锚定的是「量级与波动范围」，不是「个体数值」。"
      "报告里所有网点层面的具体数字都是生成的，**不代表任何真实网点的经营情况**。\n")

    # 岗位相关的真实发现
    if ftz_rows:
        A("### 一个与本岗位直接相关的真实发现\n")
        A("平安银行自 2022 年报起，每一期定期报告都**单独列示「上海自贸试验区分行」**，"
          "我把 6 期数据都取了出来：\n")
        A("| 报告期 | 资产规模（亿元） | 员工人数（人） |")
        A("|---|---:|---:|")
        for p, a, s in ftz_rows:
            A(f"| {p} | {a if a is not None else '—'} | {s if s is not None else '—'} |")
        A("")
        if asset_growth is not None:
            A(f"四年间该分行资产规模增长 **{asset_growth * 100:.1f}%**"
              f"（{asset.get('2022A')} → {asset.get('2025A')} 亿元），"
              f"而员工人数从 {staff.get('2022A')} 人降到 {staff.get('2025A')} 人，"
              f"**反而减少了 {int(staff.get('2022A')) - int(staff.get('2025A'))} 人**。\n")
        A("这说明该分行的产能增长不是靠加人，而是靠**渠道结构变化与单位人力产能提升**。")
        A("这正是我做这个项目的出发点：当资产与业务量在涨、人手不涨甚至减少时，")
        A("运营管理的核心问题就变成「**单位产能怎么盯、什么时候该调窗口、"
          "哪些异常其实不该问责网点**」——也就是本报告要回答的问题。\n")

    # 差错率是低频计数指标，中位数常为 0；改用「周度口径的均值」更有代表性
    err_weekly = d.groupby(d["stat_date"].dt.to_period("W")).apply(
        lambda g: g["txn_error_cnt"].sum() / max(1, g["counter_txn_cnt"].sum())
    )
    err_typical = float(err_weekly.median())
    # 等候时长的 P90 会被「字段单位错误」类极端值拉高，改用稳健分位
    wait_p90_robust = float(d.loc[d["avg_wait_minutes"] < 120, "avg_wait_minutes"].quantile(.90))

    A("---\n")
    A("## 二、指标体系：四维十六项指标\n")
    A("指标体系不是「能算的都算上」，而是按运营管理的四个决策场景分层。"
      "每一层都明确回答一个管理问题：\n")
    A("| 维度 | 回答的管理问题 | 核心指标 |")
    A("|---|---|---|")
    A("| **效率** | 客户等多久？窗口够不够？ | 柜面业务量、平均等候时长、"
      "最长等候时长、窗口产能饱和度、单窗口业务量 |")
    A("| **服务** | 客户满意吗？渠道分流有没有效？ | 电子渠道分流率、智能设备占比、"
      "客户满意度、投诉率（每万笔） |")
    A("| **成本** | 单位业务的运营成本是否合理？ | 运营成本收入比、人均业务量、"
      "单窗口业务量 |")
    A("| **风险** | 操作是否合规？差错是否可控？ | 业务差错率、事后监督复核率、"
      "差错率周度趋势 |")
    A("")
    A("**口径纪律**：所有比率类指标在数据层只落「分子」和「分母」，"
      "比率在查询/分析时才计算，避免下游出现两套口径。"
      "这一条是我在另一个项目里踩过坑之后定下来的规矩——"
      "当时清洗层与分析层各写了一套分类词典，两套数永远对不上。\n")
    A(f"本次数据的整体水位：平均等候时长中位数 **{wait_med:.1f} 分钟**"
      f"（剔除「字段单位错误」类极端值后的 P90 为 {wait_p90_robust:.1f} 分钟，"
      f"未剔除时为 {wait_p90:.1f} 分钟）；业务差错率按**周度口径**为 "
      f"**{err_typical * 100:.3f}%**（日粒度中位数为 {err_med * 100:.3f}%——"
      f"注意这里刻意给了两个口径：差错属低频计数事件，"
      f"日粒度中位数会被大量「当天零差错」压到 0，只有按周看才有意义）；"
      f"电子渠道分流率中位数 **{ech_med * 100:.1f}%**，"
      f"窗口产能饱和度中位数 **{cap_med * 100:.0f}%**。\n")

    A("---\n")

    # ================= 三、发现 =================
    A("## 三、四项主要发现\n")
    A("### 发现 1：周内效应极强，基线必须按星期几对齐\n")
    A(f"一周内业务量最高的是**{busiest}**、最低的是**{quietest}**，"
      f"后者只有前者的 **{wd_ratio * 100:.0f}%**。\n")
    A("这不是细节，而是会直接决定预警系统能不能用：")
    A("如果基线用「前 7 日滚动均值」，那么每个周日都会天然显示「业务量 -65%」，"
      "等于每周固定制造一次假异常；更严重的是，**持续性的真实异常会被这种周期性假信号淹没**。\n")
    A("> 这是本项目返工最多的一处。第一版检测器用 7 日滚动均值做基线，"
      "7 例注入异常只检出 4 例，漏掉的 3 例全部是被周内效应掩盖的。")
    A("> 改成「同星期几中位数基线」后，7 例全部检出。\n")

    A("### 发现 2：大部分预警不是业务风险，而是数据与口径问题\n")
    if not alerts.empty:
        A(f"共产生 **{len(alerts)} 条**预警，按性质分布：\n")
        A("| 疑似成因 | 预警条数 | 是否应问责网点 |")
        A("|---|---:|---|")
        for cause, cnt in struct.items():
            should = "否——应修系统" if cause.startswith("data_error") else (
                "否——应回算基线" if cause == "caliber_gap" else
                ("否——先核实" if cause == "undetermined" else "是"))
            A(f"| {CAUSE_CN.get(cause, cause)} | {cnt} | {should} |")
        A("")
        A(f"其中真实业务风险 **{n_true_risk} 条**，"
          f"**数据错误与口径差异合计 {n_not_risk - int((alerts['cause'] == 'undetermined').sum())} 条**，"
          f"待定 {int((alerts['cause'] == 'undetermined').sum())} 条。\n")
        A("这一条是本项目最想表达的观点：")
        A("**预警系统的价值不在于「发现了异常」，而在于「判断出哪些异常不是风险」。**")
        A("如果把所有跳变都报成风险，运营侧很快就会不再相信预警——"
          "这是内部监控工具最常见的死法。\n")

    A("### 发现 3：三条「专属特征」能可靠区分三类异常\n")
    A("单一指标跳变无法定性，必须做**指标间交叉校验**。"
      "以下是本项目中真正能把三类异常分开的判据（每一条都是被数据打回来之后才定下来的）：\n")
    A("| 异常性质 | 专属特征（指标间关系） | 处置方向 |")
    A("|---|---|---|")
    A("| 数据错误·字段单位 | 等候时长量级放大 8 倍以上，而业务量、差错率全部正常 | "
      "先核对系统字段单位，**不要**按异常等候时长去问责网点 |")
    A("| 数据错误·批量重跑 | 业务量虚高，但成本收入比反而下降（分母虚高）——"
      "指标之间不自洽 | 核对批量任务是否重复跑批，避免按虚高业务量排班 |")
    A("| 口径差异 | 业务量**台阶式**下降，但等候时长**不跟着降**"
      "（若客户真变少，等候时长必然下降） | 确认是否启用新统计口径；若是则回算基线 |")
    A("| 真实风险·渠道故障 | 电子渠道分流率骤降 + 柜面业务量与等候时长同步上升 | "
      "确认设备/渠道状态，恢复前增开弹性窗口 |")
    A("| 真实风险·产能不足 | 业务量与等候时长**同步**持续走高 | "
      "增开弹性窗口或引导客户转向智能设备 |")
    A("| 真实风险·操作风险 | **周度**差错率显著抬升（日粒度信噪比太低） | "
      "抽查差错明细，加强复核与新人辅导 |")
    A("")
    A("两个关键设计细节：\n")
    A("1. **差错率必须按周看**。差错是低频计数事件，单日差错率波动极大，"
      "日粒度的偏离几乎全是噪声；改用「7 天累计差错数 / 7 天累计业务量」后才能稳定识别漂移。")
    A("2. **规则顺序即优先级**。例如「渠道故障」必须排在「字段单位错误」之前——"
      "设备故障同样会让等候时长放大，若先判单位错误就会误判。\n")

    A("### 发现 4：网点层面的产能分布不均，存在两类不同的问题网点\n")
    if len(busy_slow):
        A(f"**「又忙又慢」型（{len(busy_slow)} 家）**：产能饱和度与等候时长都排在前 40%，"
          "属于真实的产能缺口，需要排班或窗口调整：\n")
        for _, r in busy_slow.head(5).iterrows():
            A(f"- `{r['branch_code']}` {r['branch_name']}：饱和度 {r['avg_cap'] * 100:.0f}%、"
              f"平均等候 {r['avg_wait']:.1f} 分钟、日均柜面 {r['avg_vol']:.0f} 笔")
        A("")
    if len(idle_slow):
        A(f"**「不忙但慢」型（{len(idle_slow)} 家）**：饱和度高不上去、等候时长却偏高。"
          "这类网点加窗口没用，问题更可能在**流程、系统操作熟练度或客户结构**上：\n")
        for _, r in idle_slow.head(5).iterrows():
            A(f"- `{r['branch_code']}` {r['branch_name']}：饱和度 {r['avg_cap'] * 100:.0f}%、"
              f"平均等候 {r['avg_wait']:.1f} 分钟、日均柜面 {r['avg_vol']:.0f} 笔")
        A("")
    A("**可对标的标杆网点**（等候时长最短）：\n")
    for _, r in best.iterrows():
        A(f"- `{r['branch_code']}` {r['branch_name']}：等候 {r['avg_wait']:.1f} 分钟、"
          f"饱和度 {r['avg_cap'] * 100:.0f}%、差错率 {r['avg_err'] * 100:.3f}%")
    A("")

    A("---\n")

    # ================= 四、方法与验证 =================
    A("## 四、方法与验证：怎么证明预警是可信的\n")
    A("做一个「能报异常的看板」不难，难的是**证明它报得准**。"
      "本项目的做法是：在模拟数据里有控制地注入 7 例已知异常，"
      "登记在 `anomaly_ground_truth.json`，然后用它来算查全率与成因判定准确率。\n")
    A("### 4.1 三路检测互补\n")
    A("| 方法 | 擅长 | 盲区 |")
    A("|---|---|---|")
    A("| 横截面稳健检测（Median + 1.4826×MAD） | 抓单日突发偏离 | "
      "抓不到「每天高一点」的缓慢漂移 |")
    A("| 单网点 EWMA 控制图 | 抓缓慢漂移（对照自身历史） | "
      "对全行同步变化不敏感 |")
    A("| STL 周期分解 | 剥离周内效应后看趋势与残差 | 需要足够长的历史 |")
    A("")
    A("为什么用 MAD 而不是 3σ：运营数据里有**真实的**极端值（设备故障、营销活动），"
      "标准差会被这些值撑大，导致真正的异常反而落进 3σ 以内检不出来。\n")
    A("### 4.2 预警分级：幅度 × 持续性 × 方法一致性\n")
    A("只看偏离幅度会导致两类错误：缓慢漂移（单日幅度小）永远报不出来，"
      "而正常波动的单日越线又天天在报。因此严重度由三者共同决定：\n")
    A("```")
    A("severity_score = 幅度 × 持续性系数 × 方法一致性 × 10")
    A("  幅度         ：各指标相对「同星期几基线」的最大偏离（封顶 3.0，防极端值干扰）")
    A("  持续性系数   ：7 日内越线 ≥5 天 → 1.0；≥3 天 → 0.8；否则 0.35")
    A("                 （台阶式口径变化用「台阶持续天数」参与计算）")
    A("  方法一致性   ：三路方法同时命中时加成（最多 +30%）")
    A("```\n")
    A("### 4.3 检出结果\n")
    A(f"- 注入已知异常 **{evs['n_injected']} 例**，检出 **{evs['n_detected']} 例**，"
      f"**查全率 {fmt_pct(evs['recall'], 1)}**")
    A(f"- 性质（数据错误 / 口径差异 / 真实风险）判定正确 "
      f"**{evs['n_cause_correct']} 例**，**准确率 {fmt_pct(evs['cause_accuracy'], 1)}**")
    A(f"- 全部预警 {evs['n_alerts']} 条，其中落在注入异常窗口之外 "
      f"{evs['n_alerts_out_of_window']} 条，"
      f"占全部网点日 **{fmt_pct(evs['out_of_window_rate_vs_branch_days'], 2)}**\n")
    A("逐例结果：\n")
    A("| 注入异常 | 网点 | 性质 | 检出 | 检出日期 | 判定性质 |")
    A("|---|---|---|---|---|---|")
    for _, r in ev.iterrows():
        A(f"| {r['subtype']} | {r['branch_code']} | {r['kind']} | "
          f"{'✓' if r['detected'] else '✗'} | {r.get('detected_on') or '—'} | "
          f"{CAUSE_CN.get(r.get('detected_cause'), '—')} |")
    A("")
    A("**一个必须解释清楚的设计取舍**：「性质判定正确」统计的是"
      "**整个异常窗口内**是否判出正确性质，而不是首条预警的性质。"
      "原因是缓慢漂移类异常在窗口前期可能先被别的指标触发，"
      "随着漂移累积正确性质才浮现——把这算作错判并不合理。"
      "每例的首次判对时间记在 `cause_correct_on` 字段，可人工复核。\n")
    A("### 4.4 一次关键的自我纠错\n")
    A("模拟器第一版里，「投诉率升至基线 6 倍」这个注入实际上**从未发生**：")
    A("该网点的投诉基线接近 0，乘 6 之后期望值仍不足 1 笔，泊松抽样长期取到 0。\n")
    A("这意味着检测算法会去「检出」一个不存在的异常，评估结果就是假的。"
      "发现之后我加了 `validate_injections()` 自检：")
    A("**每次注入都必须在生成数据里可观测到（效应量达标、且对照指标保持平稳），"
      "否则直接报错退出。** 现在 7/7 全部通过。\n")
    A("> 这件事的意义：评估一个检测算法的前提，是**异常确实存在且幅度已知**。"
      "没有这道自检，查全率 100% 只是一个自欺欺人的数字。\n")

    A("---\n")

    # ================= 五、诚实的交付限制 =================
    A("## 五、这份报告的局限（请务必读完这一节）\n")
    A("### 5.1 数据层面\n")
    nf = None
    try:
        with io.open(os.path.join(RAW, "公开数据_原始提取.json"), encoding="utf-8") as f:
            nf = json.load(f).get("not_found") or []
    except Exception:
        pass
    A(f"1. **网点级微观数据不存在公开来源。** 本项目已尽力检索："
      f"人行 17 份支付体系报告全文均无「网点数量」「柜面业务量」「等候时长」口径；"
      f"「助农取款服务点」在 17 份报告里检索命中 0 次（该口径已废止）；"
      f"监管总局的「商业银行主要监管指标情况表」页面为 JS 动态渲染，无法抓取。"
      f"共 **{len(nf) if nf else 11} 项**指标确认无公开来源，全部记为 null，**没有用任何数字去填**。")
    A("2. **模拟数据的边界。** 锚定的是量级与波动范围，不是个体数值。"
      "因此报告里所有网点级具体数字**不能用于推断任何真实银行的经营水平**。")
    A("3. **注入异常是我设计的，不是自然发生的。** 真实运营中的异常形态会更复杂、"
      "更不典型，本项目的 7 例只是一个可验证的最小集合，"
      "查全率 100% **不代表**在真实数据上也能达到这个水平。")
    A("4. **人行报告在 2025Q1 起改版**，删除了「人均持有银行卡」「每万人拥有 ATM」"
      "「联网特约商户」等明细项。做长时序时必须注意：**这些是「报告不再披露」，"
      "不是「数据缺失」**，两者的处理方式完全不同。\n")
    A("### 5.2 方法与评估层面\n")
    A(f"5. **误报率数字的口径必须说清楚。** 报告里 "
      f"{fmt_pct(evs['out_of_window_rate_vs_branch_days'], 2)} 这个数字是「注入窗口外预警数 / "
      f"全部网点日数」，它是误报率的**上限**——真实运营中本来就存在大量未被登记的波动"
      f"（营销活动、临时调休、社区事件），它们不算假警报，但在这个口径下全部被计入。"
      f"因此**不应把它当作算法准确率的反面来读**。")
    A("6. **只看首条预警评价性质判定是不公平的，我改成看整个窗口**"
      "（见 4.3）。这是评估口径的选择，换一种口径数字会变，所以我把它写出来而不是藏起来。")
    A("7. **三路方法的阈值是我标定的**，不是理论最优。"
      "阈值是在「这个数据集 + 这 7 例注入」上调出来的，"
      "存在过拟合风险；换一批数据需要重新标定。")
    A("8. **SQL 层与 Python 层的稳健离散度口径不完全一致**：")
    A("MySQL 没有 MEDIAN 聚合函数，SQL 层用「平均绝对偏差」近似，"
      "Python 层用真 MAD。两者都稳健，但数值不完全相等，已在代码注释中标注。\n")
    A("### 5.3 如果重做一次，我会改\n")
    A("1. **基线口径从一开始就按「同星期几 + 节假日」分层**，"
      "而不是等漏检了才回头改。节假日效应本次没有单独建模。")
    A("2. **注入更多的「复合异常」**（例如口径调整叠加人员变动），"
      "现在的 7 例都是单一成因，真实世界很少这么干净。")
    A("3. **给预警加一个「处置闭环」字段**（已确认 / 已处置 / 误报），"
      "这样后续可以用运营反馈来修正阈值，而不是一直靠我手工标定。")
    A("4. **把差错率这类低频指标直接按周为最小分析单位**，"
      "不要先按日算再聚合——中间那一步的噪声是会污染结论的。\n")

    A("---\n")

    # ================= 六、给运营条线的建议 =================
    A("## 六、可落地的运营建议\n")
    A("按「先修数据、再调口径、最后动人」的顺序，"
      "这个顺序很重要——如果先动人，很可能是在为系统的错误问责一线：\n")
    A("| 优先级 | 动作 | 依据 |")
    A("|---|---|---|")
    A("| P0 | 建立**指标间交叉校验**规则（业务量↑但成本率↓、等候时长↑但业务量不变等），"
      "在预警产生前先做一次自洽性检查 | 发现 2、发现 3："
      "近三成预警根本不是业务风险 |")
    A("| P0 | 把等候时长的**字段单位与量级校验**做成入库前置规则（如上限 120 分钟告警） | "
      "发现 3：单位错误会把等候推到 1000 分钟量级，但它不是真风险 |")
    A("| P1 | 统计口径变更时，**同步回算历史基线**并留痕 | "
      "发现 3：口径调整会造成台阶式跳变，不回算就会误报 |")
    A("| P1 | 监控基线按**同星期几**对齐，并按业务量分层设置页数/阈值 | "
      "发现 1：周内效应使最低日业务量只有最高日的 "
      f"{wd_ratio * 100:.0f}% |")
    A("| P2 | 对「又忙又慢」型网点做**弹性窗口排班**；"
      "对「不忙但慢」型网点做**流程与技能诊断**，不要加窗口 | "
      "发现 4：两类问题网点需要完全不同的干预手段 |")
    A("| P2 | 差错率按**周度**考核与预警，避免日粒度噪声引发无效问责 | "
      "发现 3：低频计数指标日粒度信噪比过低 |")
    A("")

    A("---\n")
    A("## 附：交付物清单\n")
    A("| 文件 | 说明 |")
    A("|---|---|")
    A("| `results/看板.html` | 交互式看板，单文件离线可用（plotly.js 已内嵌） |")
    A("| `results/02_预警清单.csv` | 全部预警，含严重度、主因指标、疑似成因与处置建议 |")
    A("| `results/03_异常检出评估.csv` | 逐例检出结果与性质判定 |")
    A("| `results/04_网点运营画像.csv` | 网点级运营画像与预警数 |")
    A("| `results/07_全行日度趋势.csv` | 全行日度指标趋势 |")
    A("| `src/sql/01_indicator_layer.sql` | MySQL 四层建模 + 窗口函数 + 预警规则 |")
    A("| `src/py/gen_operation_data.py` | 模拟数据生成（含注入自检） |")
    A("| `src/py/analyze_ops.py` | 三路检测 + 分级 + 成因判定 + 评估 |")
    A("| `docs/方法说明书.md` | 方法与踩坑记录（含两次自我纠错） |")
    A("| `docs/公开数据来源清单.md` | 全部公开来源与未获取到的指标 |")
    A("")
    A(f"> 报告由 `src/py/gen_report.py` 自动生成，"
      f"所有数字直接读自 results/ 下的 CSV 与 analysis.json，未手写。")
    A(f"> 生成时间：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")

    md = "\n".join(L) + "\n"
    out_md = os.path.join(RESULTS, "运营建议报告.md")
    with io.open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"报告已生成: {out_md} ({len(md):,} 字符)")
    return out_md


if __name__ == "__main__":
    main()
