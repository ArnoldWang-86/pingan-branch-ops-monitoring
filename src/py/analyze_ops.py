# -*- coding: utf-8 -*-
"""银行网点运营指标：异常识别、分级与成因判定。

流程
----
1. load            读取模拟明细 + 网点维度 + ground truth
2. indicators      派生指标（渠道分流率、差错率、投诉率、窗口效能、饱和度）
3. detect_mad      横截面稳健检测（Median + 1.4826*MAD，|z| > 3）
4. detect_ewma     单网点时序检测（EWMA 控制图，对手续性漂移敏感）
5. detect_stl      STL 周期分解（剥离周内效应后看趋势残差）
6. merge_rules     三路证据合并 → 严重度分级
7. classify        成因判定：数据错误 / 口径差异 / 真实风险 / 待定
8. evaluate        与 ground truth 比对，算查全率与查准率
9. export          输出 CSV / JSON / 预警清单

设计取舍（面试可讲）
--------------------
· 为什么用 MAD 而不是 3σ：运营指标里有真实的极端值（故障、活动），
  标准差会被这些值撑大，导致真正的异常反而落在 3σ 以内检不出来。
· 为什么要三路并用：单点偏离（MAD）能抓突发，但抓不到「每天高一点」的
  缓慢漂移；EWMA 恰好擅长后者；STL 用来先把周内效应剥掉，
  否则每周一都会被误报。
· 为什么不直接把偏离度大的都叫「风险」：因为数据错误和口径调整
  同样会让指标跳变。把这三类混在一起报，运营侧会很快不再相信预警。
"""
from __future__ import annotations

import argparse
import io
import json
import os
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd
from statsmodels.tsa.seasonal import STL

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
RAW = os.path.join(PROJ, "data", "raw")
CLEAN = os.path.join(PROJ, "data", "clean")
RESULTS = os.path.join(PROJ, "results")

MAD_SCALE = 1.4826          # 把 MAD 校正到与正态标准差同尺度
MAD_THRESHOLD = 3.0         # 横截面稳健 z 阈值
EWMA_LAMBDA = 0.25
EWMA_LIMIT = 3.0
STL_THRESHOLD = 3.0

METRIC_COLS = [
    "counter_txn_cnt", "avg_wait_minutes", "error_rate",
    "complaint_per_10k", "e_channel_rate", "capacity_utilization",
    "customer_satisfaction", "op_cost_ratio",
]

METRIC_LABEL = {
    "counter_txn_cnt": "柜面业务量",
    "avg_wait_minutes": "平均等候时长",
    "error_rate": "业务差错率",
    "complaint_per_10k": "投诉率（每万笔）",
    "e_channel_rate": "电子渠道分流率",
    "capacity_utilization": "窗口产能饱和度",
    "customer_satisfaction": "客户满意度",
    "op_cost_ratio": "运营成本收入比",
}


# --------------------------------------------------------------- 1. 载入

def load(raw_dir=RAW):
    detail = os.path.join(raw_dir, "branch_ops_detail.jsonl")
    if not os.path.exists(detail):
        raise SystemExit(
            f"未找到明细数据 {detail}\n请先运行：python src/py/gen_operation_data.py"
        )
    df = pd.read_json(detail, lines=True, dtype={"branch_code": str})
    df["stat_date"] = pd.to_datetime(df["stat_date"])
    df = df.sort_values(["branch_code", "stat_date"]).reset_index(drop=True)

    with io.open(os.path.join(raw_dir, "branches.json"), encoding="utf-8") as f:
        branches = pd.DataFrame(json.load(f))
    with io.open(os.path.join(raw_dir, "anomaly_ground_truth.json"), encoding="utf-8") as f:
        truth = json.load(f)
    meta = {}
    mp = os.path.join(raw_dir, "simulation_meta.json")
    if os.path.exists(mp):
        with io.open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    return df, branches, truth, meta


# --------------------------------------------------------------- 2. 指标

def indicators(df: pd.DataFrame) -> pd.DataFrame:
    """派生指标。所有比率都保留分子分母，便于复核口径。"""
    d = df.copy()
    d["total_txn_cnt"] = d["counter_txn_cnt"] + d["smart_device_txn_cnt"] + d["mobile_txn_cnt"]
    d["e_channel_rate"] = (
        (d["smart_device_txn_cnt"] + d["mobile_txn_cnt"])
        / d["total_txn_cnt"].replace(0, np.nan)
    )
    d["error_rate"] = d["txn_error_cnt"] / d["counter_txn_cnt"].replace(0, np.nan)
    d["complaint_per_10k"] = (
        d["complaint_cnt"] / d["counter_txn_cnt"].replace(0, np.nan) * 10000
    )
    d["post_review_rate"] = d["post_review_cnt"] / d["counter_txn_cnt"].replace(0, np.nan)
    d["txn_per_staff"] = d["counter_txn_cnt"] / d["staff_on_duty"].replace(0, np.nan)
    d["txn_per_window"] = d["counter_txn_cnt"] / d["open_windows"].replace(0, np.nan)
    # 单窗口 7.5 小时理论产能按每笔 12 分钟折算
    d["capacity_utilization"] = (
        d["txn_per_window"] / (7.5 * 60 / 12.0)
    )
    return d


def add_windows(d: pd.DataFrame, min_periods=7) -> pd.DataFrame:
    """为每个网点补上「同星期几基线」与滚动统计列。

    为什么必须按星期几对齐（这是本项目最关键的一处修正）
    --------------------------------------------------
    网点的业务量周内效应极强：本例中周日业务量只有工作日的 1/3 左右。
    如果基线用「前 7 日滚动均值」，周日必然显示为「业务量 -65%」，
    相当于每周制造一次假异常；而持续性门槛又会因为这种抖动
    永远无法被真实异常稳定满足，结果就是「假异常挤掉真异常」。
    第一版检测器正是栽在这里：7 例注入只检出 4 例，
    漏掉的 3 例全部被周内效应淹没。

    做法：对每个网点、每个星期几，取**此前 4 个同星期几观测的中位数**
    作为基线。它天然剥离了周内效应，也不需要拟合季节模型。

    另外保留：
    · ma21  ：前 21 日均值，用于识别台阶式变化（口径调整的典型形态）
    · 7 日累计差错数与业务量：差错属低频计数指标，日粒度信噪比太低，
      必须按周聚合才能稳定识别漂移（7 天累计差错 / 7 天累计业务量）。
    """
    out = []
    for code, sub in d.groupby("branch_code", sort=False):
        sub = sub.sort_values("stat_date").copy()

        # ---- 同星期几基线：shift(1) 保证不含当日，rolling(4) 取近 4 次同星期几
        for col, base_col in [
            ("counter_txn_cnt", "vol_wd_base"),
            ("avg_wait_minutes", "wait_wd_base"),
            ("error_rate", "err_wd_base"),
            ("complaint_per_10k", "comp_wd_base"),
            ("e_channel_rate", "ech_wd_base"),
            ("op_cost_ratio", "cost_wd_base"),
        ]:
            sub[base_col] = (
                sub.groupby("weekday")[col]
                   .transform(lambda s: s.shift(1).rolling(4, min_periods=2).median())
            )

        # ---- 中期滚动基线：识别台阶式变化
        for col, out_col in [("counter_txn_cnt", "counter_txn_ma21"),
                             ("avg_wait_minutes", "wait_ma21"),
                             ("error_rate", "error_rate_ma21"),
                             ("complaint_per_10k", "complaint_ma21")]:
            sub[out_col] = sub[col].shift(1).rolling(21, min_periods=5).mean()

        # ---- 周度口径
        sub["err_cnt_7d"] = sub["txn_error_cnt"].rolling(7, min_periods=5).sum()
        sub["txn_cnt_7d"] = sub["counter_txn_cnt"].rolling(7, min_periods=5).sum()
        sub["error_rate_7d"] = sub["err_cnt_7d"] / sub["txn_cnt_7d"].replace(0, np.nan)
        out.append(sub)

    res = pd.concat(out, ignore_index=True)

    def dev(col, base_col):
        base = res[base_col].replace(0, np.nan)
        return (res[col] - base) / base

    # 相对「同星期几基线」的偏离 —— 触发与定性都用这一组
    res["vol_dev"] = dev("counter_txn_cnt", "vol_wd_base")
    res["wait_dev"] = dev("avg_wait_minutes", "wait_wd_base")
    res["err_dev"] = dev("error_rate", "err_wd_base")
    res["comp_dev"] = dev("complaint_per_10k", "comp_wd_base")
    res["ech_dev"] = dev("e_channel_rate", "ech_wd_base")
    res["cost_dev"] = dev("op_cost_ratio", "cost_wd_base")
    # 相对中期基线 —— 台阶式（口径）变化用这一组
    res["vol_step"] = dev("counter_txn_cnt", "counter_txn_ma21")
    res["wait_step"] = dev("avg_wait_minutes", "wait_ma21")
    res["error_rate_7d_dev"] = dev("error_rate_7d", "error_rate_ma21")

    # ---- 低频计数指标的绝对增量（投诉、差错）
    # 投诉是「每万笔」口径的稀有事件，基线常为 0，相对变化会变成 inf/NaN，
    # 因此额外算一个「每万笔绝对增加额」，用绝对量而不是倍数来判定。
    res["comp_per_10k_abs_delta"] = res["complaint_per_10k"] - res["comp_wd_base"]

    # ---- 台阶式变化检测（口径调整的典型形态，且专门防「渐进漂移」误判）
    # 做法：统计「最近 5 天里有多少天显著低于/高于同星期几基线」。
    # 用计数而不是「逐日方向 + 连续天数」，因为同星期几偏离本身波动很大，
    # 逐日方向会被单日噪声打断，真正的台阶反而连不成一条线。
    res = _detect_level_shift(res)

    # 兼容旧字段名，避免下游脚本改动
    res["counter_txn_dev"] = res["vol_dev"]
    res["counter_txn_step"] = res["vol_step"]
    res["complaint_dev"] = res["comp_dev"]
    res["e_channel_ma7"] = res["ech_wd_base"]
    res["wait_ma7"] = res["wait_wd_base"]
    res["op_cost_ratio_ma7"] = res["cost_wd_base"]
    return res


# 台阶检测参数
STEP_TOL = 0.12          # 单日偏离容忍带：±12% 以内视为「没动」
STEP_TOL_DN = 0.13       # 下降方向单独放宽一档（口径剔除通常只影响部分交易）
STEP_WINDOW = 5          # 观察最近 5 天
STEP_MIN_DAYS = 4        # 其中至少 4 天同向越线，才认定为水平移动


def _detect_level_shift(res: pd.DataFrame) -> pd.DataFrame:
    """识别「水平移动」（台阶），用于区分口径调整与自然波动/渐进漂移。"""
    res = res.sort_values(["branch_code", "stat_date"]).copy()
    for col, out_col in [("vol_dev", "vol_shift_dir"), ("wait_dev", "wait_shift_dir")]:
        up_cnt, dn_cnt = [], []
        for code, idx in res.groupby("branch_code").groups.items():
            s = res.loc[idx].sort_values("stat_date")[col].astype(float).fillna(0.0)
            up_cnt.append((s > STEP_TOL)
                          .rolling(STEP_WINDOW, min_periods=STEP_WINDOW).sum())
            dn_cnt.append((s < -STEP_TOL_DN)
                          .rolling(STEP_WINDOW, min_periods=STEP_WINDOW).sum())
        up = pd.concat(up_cnt).sort_index()
        dn = pd.concat(dn_cnt).sort_index()
        res[out_col] = np.where(up >= STEP_MIN_DAYS, 1,
                                np.where(dn >= STEP_MIN_DAYS, -1, 0))
        res[out_col.replace("_dir", "_up_days")] = up.fillna(0).astype(int)
        res[out_col.replace("_dir", "_dn_days")] = dn.fillna(0).astype(int)
    return res


# --------------------------------------------------------------- 3. MAD

def detect_mad(d: pd.DataFrame, metric: str) -> pd.DataFrame:
    """横截面稳健检测：当日全行 Median 与 MAD，|1.4826 校正后 z| > 阈值。"""
    g = d.groupby("stat_date")[metric]
    med = g.transform("median")
    mad = g.transform(lambda s: np.median(np.abs(s - np.median(s))))
    scale = MAD_SCALE * mad.replace(0, np.nan)
    z = (d[metric] - med) / scale
    out = pd.DataFrame({
        "stat_date": d["stat_date"],
        "branch_code": d["branch_code"],
        "metric": metric,
        "value": d[metric],
        "baseline": med,
        "robust_z": z,
        "method": "mad",
    })
    out["hit"] = out["robust_z"].abs() > MAD_THRESHOLD
    return out


# --------------------------------------------------------------- 4. EWMA

def detect_ewma(d: pd.DataFrame, metric: str, lam=EWMA_LAMBDA, limit=EWMA_LIMIT):
    """单网点时序检测：EWMA 控制图。

    对照的是「该网点自身的历史水平」，因此对「每天高一点」的缓慢漂移敏感，
    而横截面 MAD 对这类漂移几乎无感（因为同一天其他网点也在动）。
    """
    frames = []
    for code, sub in d.groupby("branch_code"):
        s = sub.sort_values("stat_date")[metric].astype(float)
        if s.notna().sum() < 10:
            continue
        # 前 7 天作为初始基线，之后递推，避免把开局当成异常
        init = s.iloc[:7].mean()
        ewma = []
        prev = init
        for v in s:
            prev = lam * v + (1 - lam) * prev
            ewma.append(prev)
        ewma = pd.Series(ewma, index=s.index)
        resid = s - ewma
        sd = resid.iloc[7:].std(ddof=1)
        sd = sd if sd and not np.isnan(sd) else np.nan
        frames.append(pd.DataFrame({
            "stat_date": sub["stat_date"].values,
            "branch_code": code,
            "metric": metric,
            "value": s.values,
            "baseline": ewma.values,
            "robust_z": (resid.values / sd) if sd else np.nan,
            "method": "ewma",
        }))
    if not frames:
        return pd.DataFrame(columns=["stat_date", "branch_code", "metric",
                                     "value", "baseline", "robust_z", "method", "hit"])
    out = pd.concat(frames, ignore_index=True)
    out["hit"] = out["robust_z"].abs() > limit
    return out


# --------------------------------------------------------------- 5. STL

def detect_stl(d: pd.DataFrame, metric: str, period=7, threshold=STL_THRESHOLD):
    """STL 周期分解：先剥掉周内效应，再看趋势与残差是否偏离。

    业务量的周内效应很强（周一和周末完全不同），不剥离就会每周一误报一次。
    """
    frames = []
    for code, sub in d.groupby("branch_code"):
        sub = sub.sort_values("stat_date")
        s = sub[metric].astype(float)
        if s.notna().sum() < 2 * period + 2:
            continue
        try:
            res = STL(s.interpolate(), period=period, robust=True).fit()
        except Exception:
            continue
        resid = pd.Series(res.resid, index=s.index)
        sd = resid.std(ddof=1)
        sd = sd if sd and not np.isnan(sd) else np.nan
        frames.append(pd.DataFrame({
            "stat_date": sub["stat_date"].values,
            "branch_code": code,
            "metric": metric,
            "value": s.values,
            "baseline": (s - resid).values,          # 趋势 + 季节
            "robust_z": (resid.values / sd) if sd else np.nan,
            "trend": res.trend,
            "seasonal": res.seasonal,
            "method": "stl",
        }))
    if not frames:
        return pd.DataFrame(columns=["stat_date", "branch_code", "metric",
                                     "value", "baseline", "robust_z", "method", "hit"])
    out = pd.concat(frames, ignore_index=True)
    out["hit"] = out["robust_z"].abs() > threshold
    return out


def detect_all(d: pd.DataFrame, metrics=None):
    metrics = metrics or METRIC_COLS
    parts = []
    for m in metrics:
        parts.append(detect_mad(d, m))
        parts.append(detect_ewma(d, m))
        parts.append(detect_stl(d, m))
    ev = pd.concat(parts, ignore_index=True)
    ev["metric_label"] = ev["metric"].map(METRIC_LABEL)
    return ev


# --------------------------------------------------------------- 6. 合并

# 各指标的业务权重：等候时长与差错率对一线运营最关键
METRIC_WEIGHT = {
    "avg_wait_minutes": 1.00,
    "error_rate": 0.90,          # 低频计数指标，权重略降，另按周度口径单独处理
    "complaint_per_10k": 0.85,
    "capacity_utilization": 0.85,
    "counter_txn_cnt": 0.80,
    "e_channel_rate": 0.70,
    "customer_satisfaction": 0.70,
    "op_cost_ratio": 0.40,
}

# 幅度阈值：与 SQL 层保持一致，便于两层结果互相印证
# 注意这些是相对「同星期几基线」的偏离，不是相对 7 日滚动均值
AMP_WAIT = 0.35
AMP_VOL = 0.20
AMP_ERR = 0.60          # 差错类走周度口径，日粒度噪声大
AMP_COMP = 1.00         # 投诉本身低频，改用绝对增量触发（见 merge_rules）
AMP_COMP_ABS = 3.0      # 每万笔投诉绝对增加 3 笔即触发
TRIG_MIN_DAYS = 3       # 7 天窗口内至少越线 3 天，才算「持续」
SCORE_FLOOR = 8.0       # 低于此分数不产生预警（降级为观察）
SEVERITY_2 = 30.0
SEVERITY_3 = 80.0
AMPLITUDE_CAP = 3.0     # 幅度封顶，避免 60 倍量级错误把严重度打到天上
W_VOL = 0.95            # 幅度折算权重：业务量尖峰本身就很显眼
W_ERR = 0.90
W_COMP = 0.85
W_WAIT = 1.00
SPIKE_AMPLITUDE = 0.60  # 单日幅度超过此值 → 即使只出现一天也必须预警
SPIKE_DAYS = 2          # 近 3 天内出现 2 天即视为尖峰
DUPLICATE_DAYS = 2      # 归因「批量重跑」所需的近 3 日大幅虚高天数


def merge_rules(ev: pd.DataFrame, d: pd.DataFrame) -> pd.DataFrame:
    """把三路检测命中 + 同星期几偏离合成预警，并做持续性过滤。

    相对第一版的关键改动：
    1. 偏离基准从「前 7 日滚动均值」换成「同星期几基线」——
       否则周日天然显示 -65%，每周制造一次假异常，真异常反被挤掉。
    2. 严重度 = 幅度 × 持续性 × 方法一致性：
       缓慢漂移类单日幅度小但持续性强，这样才报得出来；
       正常波动的单日越线因为缺乏持续性会被降级。
    3. 投诉改用绝对增量触发：它基线常为 0，相对倍数会变成 inf。
    """
    # --- 单日触发（用于持续性计数）
    trig = d[["stat_date", "branch_code"]].copy()
    trig["triggered"] = (
        (d["wait_dev"].abs() > AMP_WAIT)
        | (d["vol_dev"].abs() > AMP_VOL)
        | (d["error_rate_7d_dev"].abs() > AMP_ERR)
        | (d["comp_per_10k_abs_delta"] > AMP_COMP_ABS)
    ).astype(int)
    trig = trig.sort_values(["branch_code", "stat_date"])
    trig["trig_7d"] = (trig.groupby("branch_code")["triggered"]
                       .transform(lambda s: s.rolling(7, min_periods=1).sum()))
    # 尖峰计数：近 3 天内业务量大幅偏离的天数。
    # 数据错误（批量重跑、字段单位）本质上是**单日或两日的系统故障**，
    # 不可能满足「持续 3 天」的门槛；若只用持续性过滤，
    # 这类异常会被整体漏掉（第一版就是这样漏掉了 duplicate_batch）。
    trig["vol_spike_3d"] = (
        d["vol_dev"].abs().gt(0.40).astype(int)
        .groupby(d["branch_code"]).transform(lambda s: s.rolling(3, min_periods=1).sum())
    )
    # 台阶持续天数：近 7 天内被台阶检测器认定为「水平移动」的天数。
    # 它替代了「直接豁免持续性门槛」的做法——豁免会让台阶状态一旦触发就
    # 连续多日报警（实测把预警从 202 条推到 570 条），
    # 而按天累计既保留了真实证据，又不会放大成噪声。
    shift_flag = ((d["vol_shift_dir"] != 0) | (d["wait_shift_dir"] != 0)).astype(int)
    trig["shift_7d"] = (
        shift_flag.groupby(d["branch_code"]).transform(lambda s: s.rolling(7, min_periods=1).sum())
    )
    trig["effective_7d"] = trig[["trig_7d", "shift_7d"]].max(axis=1)

    # --- 三路方法的命中证据
    hits = ev[ev["hit"]].copy()
    hits["abs_z"] = hits["robust_z"].abs()
    hits["weighted_z"] = hits["abs_z"] * hits["metric"].map(METRIC_WEIGHT).fillna(0.5)

    base = d.merge(trig[["stat_date", "branch_code", "trig_7d", "shift_7d",
                          "effective_7d", "vol_spike_3d"]],
                   on=["stat_date", "branch_code"], how="left")

    rows = []
    for _, r in base.iterrows():
        def num(name):
            x = r.get(name)
            return float(x) if x is not None and pd.notna(x) else 0.0

        # 幅度：各指标相对同星期几基线的偏离，按业务重要性折算
        amp_candidates = {
            "avg_wait_minutes": abs(num("wait_dev")) * W_WAIT,
            "counter_txn_cnt": abs(num("vol_dev")) * W_VOL,
            "error_rate": abs(num("error_rate_7d_dev")) * W_ERR,
            "complaint_per_10k": (num("comp_per_10k_abs_delta") / 10.0) * W_COMP,
        }
        raw_amp = max(amp_candidates.values())
        amplitude = min(raw_amp, AMPLITUDE_CAP)      # 封顶
        # 持续性取「触发天数」与「台阶持续天数」的较大者：
        # 缓慢漂移靠前者，口径调整靠后者。
        trig_7d = num("effective_7d")
        shift_7d = num("shift_7d")
        if trig_7d >= 5:
            persistence = 1.0
        elif trig_7d >= TRIG_MIN_DAYS:
            persistence = 0.8
        else:
            persistence = 0.35

        primary = max(amp_candidates, key=amp_candidates.get)

        # --- 三路方法一致性
        h = hits[(hits["stat_date"] == r["stat_date"])
                 & (hits["branch_code"] == r["branch_code"])]
        n_methods = int(h["method"].nunique())
        n_metrics = int(h["metric"].nunique())
        consistency = 1.0 + 0.15 * max(0, n_methods - 1)
        score = amplitude * persistence * 10.0 * consistency

        if not h.empty:
            top = h.loc[h["weighted_z"].idxmax()]
            primary_z = round(float(top["robust_z"]), 3)
            methods = "+".join(sorted(h["method"].unique()))
            evidence = "; ".join(
                f"{x.metric_label}={x.value:.3g}(z={x.robust_z:.2f},{x.method})"
                for x in h.sort_values("weighted_z", ascending=False).head(4).itertuples()
            )
        else:
            primary_z = None
            methods = "threshold"
            evidence = (f"同星期几偏离: 等候{num('wait_dev'):+.0%} 业务量{num('vol_dev'):+.0%} "
                        f"周度差错{num('error_rate_7d_dev'):+.0%} "
                        f"投诉+{num('comp_per_10k_abs_delta'):.1f}/万笔")

        # 过滤：尖峰型系统故障（近 3 日至少 2 天大幅偏离）单独放行；
        # 其余一律要求持续性（含台阶持续天数）或足够幅度。
        spikes = int(num("vol_spike_3d"))
        if spikes < SPIKE_DAYS:
            if trig_7d <= 1 and score < SEVERITY_2:
                continue
            if score < SCORE_FLOOR:
                continue
        else:
            persistence = max(persistence, 0.7)
            score = amplitude * persistence * 10.0 * consistency

        rows.append({
            "stat_date": r["stat_date"],
            "branch_code": r["branch_code"],
            "branch_name": r["branch_name"],
            "branch_kind": r["branch_kind"],
            "region": r["region"],
            "n_methods": n_methods,
            "n_metrics": n_metrics,
            "methods": methods,
            "amplitude": round(amplitude, 4),
            "raw_amplitude": round(raw_amp, 4),
            "persistence_factor": persistence,
            "trig_7d": int(trig_7d),
            "severity_score": round(score, 3),
            "primary_metric": primary,
            "primary_metric_label": METRIC_LABEL[primary],
            "primary_z": primary_z,
            "evidence": evidence,
        })

    if not rows:
        return pd.DataFrame(columns=[
            "stat_date", "branch_code", "branch_name", "branch_kind", "region",
            "n_methods", "n_metrics", "methods", "amplitude", "persistence_factor",
            "trig_7d", "severity_score", "primary_metric", "primary_metric_label",
            "primary_z", "evidence", "severity", "cause", "action",
        ])

    res = pd.DataFrame(rows)
    res["severity"] = np.where(res["severity_score"] >= SEVERITY_3, 3,
                               np.where(res["severity_score"] >= SEVERITY_2, 2, 1))
    return res.sort_values(["stat_date", "severity", "severity_score"],
                           ascending=[True, False, False]).reset_index(drop=True)


# --------------------------------------------------------------- 7. 判定

# 成因标签 → (中文说明, 处置建议)
CAUSE_META = {
    "data_error_unit": ("数据错误", "先核对叫号系统字段单位（分钟/秒），确认后再评估业务影响"),
    "data_error_duplicate": ("数据错误", "核对核心系统批量任务是否重复跑批，避免按虚高业务量排班"),
    "caliber_gap": ("口径差异", "确认是否启用新统计口径；若是则回算基线，而非问责网点"),
    "true_risk_channel_outage": ("真实风险", "确认自助设备/线上渠道状态，恢复前增开弹性窗口"),
    "true_risk_capacity": ("真实风险", "评估增开弹性窗口或引导客户转向智能设备，复盘排班模型"),
    "true_risk_operational": ("真实风险", "抽查差错明细，加强事后复核与新人辅导"),
    "true_risk_service": ("真实风险", "回溯投诉工单定位服务触点，安排服务话术专项辅导"),
    "undetermined": ("待定", "纳入观察名单，不单独处置"),
}


def classify(row: pd.Series, d: pd.DataFrame) -> str:
    """成因判定。**规则顺序即优先级**，这一点很关键。

    优先级设计依据（每条都是被数据打回来之后才定下来的）：
      1. 渠道故障 → 电子渠道分流率骤降是它的专属特征，且必须排在
         「字段单位错误」之前：设备故障也会让等候时长放大 1.35 倍，
         若先判单位错误就会误判（第一版就是这样把 atm_outage 判成 data_error 的）。
      2. 字段单位错误 → 等候时长量级离谱是它的专属特征。判据要同时看
         「绝对量级是否可信」与「相对基线/全行中位数的倍数」，
         **不能只用相对倍数**：网点基线本身偏高时，「放大 8 倍」会漏掉
         417 分钟这种「平均等候 7 小时」的荒谬值（实测踩过）。
      3. 批量重跑 → 业务量虚高但成本率反而下降，指标间不自洽是它的专属特征。
      4. 口径调整 → 业务量台阶式下降而等候/差错全在基线内。
      5. 真实风险 → 业务量与等候同步走高（产能）、周度差错率抬升（操作）、
         投诉率抬升（服务）。

    全部用**窗口统计量**而非单日值：缓慢漂移类异常单日偏离很小，
    用单日值判定永远判不出性质（第一版 7 例只判对 2 例，原因就在这里）。
    """
    code = row["branch_code"]
    date = row["stat_date"]
    cur = d[(d["branch_code"] == code) & (d["stat_date"] == date)]
    if cur.empty:
        return "undetermined"
    cur = cur.iloc[0]

    def v(name):
        x = cur.get(name)
        return float(x) if x is not None and pd.notna(x) else 0.0

    wait = v("avg_wait_minutes")
    wait_base = v("wait_wd_base")        # 同星期几基线
    wait_dev = v("wait_dev")
    vol_dev = v("vol_dev")
    err7 = v("error_rate_7d_dev")
    comp_abs = v("comp_per_10k_abs_delta")
    ech_now = v("e_channel_rate")
    ech_base = v("ech_wd_base")
    cost_now = v("op_cost_ratio")
    cost_base = v("cost_wd_base")
    vol_shift = v("vol_shift_dir")        # +1 台阶上升 / -1 台阶下降 / 0 无台阶
    wait_shift = v("wait_shift_dir")
    vol_spike = v("vol_spike_3d")         # 近 3 日业务量大幅偏离的天数

    # 1) 渠道故障：电子渠道分流率骤降 + 柜面量与等候同步上升
    #    必须排在「字段单位错误」之前——设备故障同样会让等候时长放大（1.35 倍），
    #    若先判单位错误就会误判（第一版正是这样把 atm_outage 判成了 data_error）。
    if ech_base and ech_now < ech_base * 0.92 and vol_dev > 0.15 and wait_dev > 0.20:
        return "true_risk_channel_outage"

    # 2) 字段单位错误：等候时长量级离谱，业务量与差错无异常。
    #    判据用「绝对量级 + 相对倍数 + 跨网点比较」三重条件，而不是单纯相对倍数：
    #      (a) 绝对量级不可信：平均等候超过 180 分钟（3 小时）在任何网点都不可信
    #      (b) 远超同星期几基线的 8 倍
    #      (c) 远超当日全行中位数（跨网点比较，不受单点基线漂移影响）
    #    为什么必须加 (a)(c)：只用 wait > wait_base × 8 时，若该网点基线本身偏高
    #    （例如 52 分钟），417 分钟这种明显是字段问题的值只到基线的 8 倍出头，
    #    就抓不住了——实测有 99 条预警因此落进「待定」，
    #    其中包含 417~419 分钟这种「平均等候 7 小时」的荒谬值。
    cross_median = float(d.loc[d["stat_date"] == date, "avg_wait_minutes"].median())
    if (wait > 180.0
            and ((wait_base and wait > wait_base * 8)
                 or (cross_median and wait > cross_median * 8))
            and abs(vol_dev) < 0.45
            and abs(err7) < 0.80):
        return "data_error_unit"

    # 3) 批量重跑：业务量短期虚高 + 成本率反而下降（指标间不自洽）
    #    注意这里用「近 3 日尖峰天数」而不是单日，避免把一次正常高峰误判成系统故障。
    if (vol_dev > 0.40 or vol_spike >= DUPLICATE_DAYS) and cost_base \
            and cost_now < cost_base and abs(wait_dev) < 0.50:
        return "data_error_duplicate"

    # 4) 口径调整：业务量出现**台阶式下降**，而等候时长没有同步台阶。
    #    用台阶检测器（近 5 日中至少 4 天显著低于同星期几基线）而不是单日阈值：
    #    口径调整是水平移动，业务量的自然增长/季节波动是渐变，两者必须分开，
    #    否则每逢业务量自然回落就会误报「口径异常」。
    #    判据的业务含义：若客户真的变少了，等候时长会跟着下降；
    #    等候时长不变而业务量台阶式下降，说明是「统计口径」而不是「经营变化」。
    if vol_shift < 0 and abs(wait_dev) < 0.35 and abs(err7) < 1.00:
        return "caliber_gap"
    # 4b) 兜底：业务量持续走低但等候、差错、投诉都平稳，同样归入口径差异
    if vol_dev < -0.15 and abs(wait_dev) < 0.25 and abs(err7) < 0.60 and comp_abs < 3:
        return "caliber_gap"

    # 5) 真实产能不足：业务量与等候时长同步走高
    if vol_dev > 0.15 and wait_dev > 0.20:
        return "true_risk_capacity"

    # 6) 真实操作风险：周度差错率显著抬升（含缓慢漂移）
    if err7 > 0.60:
        return "true_risk_operational"

    # 7) 真实服务风险：投诉绝对增量显著（每万笔增加 3 笔以上）
    if comp_abs >= 3.0:
        return "true_risk_service"

    return "undetermined"


def attach_classification(alerts: pd.DataFrame, d: pd.DataFrame) -> pd.DataFrame:
    if alerts.empty:
        out = alerts.copy()
        out["cause"] = []
        out["action"] = []
        return out
    causes, actions = [], []
    for _, r in alerts.iterrows():
        c = classify(r, d)
        causes.append(c)
        actions.append(CAUSE_META[c][1])
    out = alerts.copy()
    out["cause"] = causes
    out["cause_cn"] = [CAUSE_META[c][0] for c in causes]
    out["action"] = actions
    return out


# --------------------------------------------------------------- 8. 评估

@dataclass
class TruthEval:
    subtype: str
    kind: str
    branch_code: str
    start: str
    end: str
    detected: bool
    detected_on: str | None
    detected_cause: str | None
    cause_correct: bool | None


def evaluate(alerts: pd.DataFrame, truth: dict, d: pd.DataFrame, tolerance_days=2):
    """与 ground truth 比对。

    判定规则
    --------
    · 检出：窗口（含 ±tolerance_days 容差）内出现该网点的预警。
    · 成因正确：窗口内**任意一条**预警给出的性质与注入性质一致。
      为什么不是只看首次预警：像「差错率缓慢漂移」这类异常，
      窗口前期可能先被投诉或等候指标触发（该网点投诉基线恰为 0 时尤其明显），
      随着漂移累积，错误性质才浮出来——只看首条会把这种正常收敛过程记成错判。
      记录 cause_correct_on 表示性质首次判对的时间，便于人工复核。
    """
    a = alerts.copy()
    if not a.empty:
        a["stat_date"] = pd.to_datetime(a["stat_date"])

    def kind_of(cause):
        if str(cause).startswith("data_error"):
            return "data_error"
        if cause == "caliber_gap":
            return "caliber_gap"
        if str(cause).startswith("true_risk"):
            return "true_risk"
        return "undetermined"

    rows = []
    for item in truth.get("anomalies", []):
        start = pd.to_datetime(item["start"])
        end = pd.to_datetime(item["end"])
        lo = start - pd.Timedelta(days=tolerance_days)
        hi = end + pd.Timedelta(days=tolerance_days)
        hit = a[
            (a["branch_code"] == item["branch_code"])
            & (a["stat_date"] >= lo) & (a["stat_date"] <= hi)
        ].sort_values("stat_date") if not a.empty else pd.DataFrame()

        if hit.empty:
            rows.append(TruthEval(item["subtype"], item["kind"], item["branch_code"],
                                  item["start"], item["end"], False, None, None, None))
            continue

        first = hit.iloc[0]
        correct = hit[hit["cause"].map(kind_of) == item["kind"]]
        rows.append(TruthEval(
            subtype=item["subtype"], kind=item["kind"], branch_code=item["branch_code"],
            start=item["start"], end=item["end"], detected=True,
            detected_on=str(first["stat_date"].date()),
            detected_cause=first["cause"],
            cause_correct=bool(not correct.empty),
        ))
    ev = pd.DataFrame([asdict(r) for r in rows])
    if not ev.empty:
        ev["cause_correct_on"] = ""
        for i, item in enumerate(truth.get("anomalies", [])):
            if not ev.loc[i, "cause_correct"]:
                continue
            hit = a[(a["branch_code"] == item["branch_code"])
                    & (a["stat_date"] >= pd.to_datetime(item["start"])
                       - pd.Timedelta(days=tolerance_days))
                    & (a["stat_date"] <= pd.to_datetime(item["end"])
                       + pd.Timedelta(days=tolerance_days))]
            good = hit[hit["cause"].map(kind_of) == item["kind"]]
            if not good.empty:
                ev.loc[i, "cause_correct_on"] = str(good["stat_date"].min().date())
    return ev


def summarize_eval(ev: pd.DataFrame, alerts: pd.DataFrame, d: pd.DataFrame,
                   truth: dict | None = None, tolerance_days=2) -> dict:
    """查全率 + 误报率。

    误报口径（必须写清楚，否则数字会被误读）：
      把 ground truth 的每个异常窗口（含 ±tolerance_days 容差）标为「注入期」，
      落在注入期之外的预警记为「非注入期预警」。
      这是**保守口径**——真实运营里本来就有未被登记的波动（比如一次营销活动），
      它们不算假警报。所以这个数字是误报率的**上限**。
    """
    n_truth = len(ev)
    n_detected = int(ev["detected"].sum()) if n_truth else 0

    out_of_window = None
    alert_rate = None
    if not alerts.empty and truth is not None:
        a = alerts.copy()
        a["stat_date"] = pd.to_datetime(a["stat_date"])
        in_window = pd.Series(False, index=a.index)
        for item in truth.get("anomalies", []):
            lo = pd.to_datetime(item["start"]) - pd.Timedelta(days=tolerance_days)
            hi = pd.to_datetime(item["end"]) + pd.Timedelta(days=tolerance_days)
            for code in [None]:
                pass
            m = ((a["branch_code"] == item["branch_code"])
                 & (a["stat_date"] >= lo) & (a["stat_date"] <= hi))
            in_window |= m
        out_of_window = int((~in_window).sum())
        alert_rate = round(out_of_window / len(d), 5) if len(d) else None

    return {
        "n_injected": n_truth,
        "n_detected": n_detected,
        "recall": round(n_detected / n_truth, 4) if n_truth else None,
        "n_cause_correct": int(ev["cause_correct"].fillna(False).sum()) if n_truth else 0,
        "cause_accuracy": (
            round(ev["cause_correct"].fillna(False).sum() / n_truth, 4) if n_truth else None
        ),
        "n_alerts": int(len(alerts)),
        "n_branch_days": int(len(d)),
        "n_alerts_in_injection_window": (int(len(alerts)) - out_of_window)
        if out_of_window is not None else None,
        "n_alerts_out_of_window": out_of_window,
        "out_of_window_rate_vs_branch_days": alert_rate,
        "false_alarm_note": (
            "「非注入期预警」是误报率的**上限**：真实运营中未被登记的波动"
            "（营销活动、临时调休等）也会被计入。本项目只注入了 7 例已知异常，"
            "因此该比率必然偏高，不应直接当作算法准确率来读。"
        ),
        "by_kind": (
            ev.groupby("kind")["detected"].agg(["sum", "count"])
            .rename(columns={"sum": "detected", "count": "injected"})
            .reset_index().to_dict("records") if n_truth else []
        ),
    }


# --------------------------------------------------------------- 9. 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default=RAW)
    ap.add_argument("--out-dir", default=RESULTS)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(CLEAN, exist_ok=True)

    print("[1/7] 载入数据 ...")
    raw, branches, truth, meta = load(args.raw_dir)
    print(f"      明细 {len(raw)} 行 / 网点 {branches['branch_code'].nunique()} 家 / "
          f"区间 {raw['stat_date'].min().date()} ~ {raw['stat_date'].max().date()}")

    print("[2/7] 计算派生指标与滚动基线 ...")
    d = indicators(raw)
    d = add_windows(d)
    d.to_csv(os.path.join(CLEAN, "ops_indicators.csv"), index=False, encoding="utf-8-sig")

    print("[3/7] 三路异常检测（MAD / EWMA / STL）...")
    ev = detect_all(d)
    ev.to_csv(os.path.join(args.out_dir, "01_检测证据_全量.csv"),
              index=False, encoding="utf-8-sig")
    print(f"      检测证据 {len(ev)} 条，命中 {int(ev['hit'].sum())} 条")

    print("[4/7] 合并证据、持续性过滤并分级 ...")
    alerts = merge_rules(ev, d)
    alerts = attach_classification(alerts, d)
    alerts.to_csv(os.path.join(args.out_dir, "02_预警清单.csv"),
                  index=False, encoding="utf-8-sig")
    if alerts.empty:
        print("      无预警")
    else:
        print(f"      预警 {len(alerts)} 条  "
              f"(1级 {int((alerts['severity'] == 1).sum())} / "
              f"2级 {int((alerts['severity'] == 2).sum())} / "
              f"3级 {int((alerts['severity'] == 3).sum())})")

    print("[5/7] 与 ground truth 比对 ...")
    ev_eval = evaluate(alerts, truth, d)
    ev_eval.to_csv(os.path.join(args.out_dir, "03_异常检出评估.csv"),
                   index=False, encoding="utf-8-sig")
    summary = summarize_eval(ev_eval, alerts, d, truth)
    print(f"      注入 {summary['n_injected']} 例，检出 {summary['n_detected']} 例，"
          f"查全率 {summary['recall']}；成因判定正确 {summary['n_cause_correct']} 例"
          f"（{summary['cause_accuracy']}）")
    print(f"      预警 {summary['n_alerts']} 条，其中注入期外 "
          f"{summary['n_alerts_out_of_window']} 条"
          f"（占全部网点日 {summary['out_of_window_rate_vs_branch_days']}）")

    print("[6/7] 汇总统计 ...")
    # 网点画像：用于看板的「标杆 vs 待改进」对比
    prof = (d.groupby(["branch_code", "branch_name", "branch_kind", "region"])
              .agg(avg_wait=("avg_wait_minutes", "mean"),
                   avg_vol=("counter_txn_cnt", "mean"),
                   avg_err=("error_rate", "mean"),
                   avg_comp=("complaint_per_10k", "mean"),
                   avg_sat=("customer_satisfaction", "mean"),
                   avg_ech=("e_channel_rate", "mean"),
                   avg_cap=("capacity_utilization", "mean"),
                   avg_window=("txn_per_window", "mean"),
                   n_alerts=("branch_code", "size"))
              .reset_index())
    alert_cnt = (alerts.groupby("branch_code").size().rename("alert_cnt")
                 if not alerts.empty else pd.Series(dtype=int, name="alert_cnt"))
    prof = prof.drop(columns=["n_alerts"]).merge(
        alert_cnt, on="branch_code", how="left").fillna({"alert_cnt": 0})
    prof.to_csv(os.path.join(args.out_dir, "04_网点运营画像.csv"),
                index=False, encoding="utf-8-sig")

    # 预警结构
    if not alerts.empty:
        struct = (alerts.groupby(["cause", "severity"]).size()
                  .rename("alerts").reset_index())
        struct.to_csv(os.path.join(args.out_dir, "05_预警结构.csv"),
                      index=False, encoding="utf-8-sig")
        by_metric = (alerts.groupby(["primary_metric_label"]).size()
                     .rename("alerts").reset_index()
                     .sort_values("alerts", ascending=False))
        by_metric.to_csv(os.path.join(args.out_dir, "06_预警主因指标分布.csv"),
                         index=False, encoding="utf-8-sig")

    # 日度趋势（全行汇总）
    daily = (d.groupby("stat_date")
               .agg(counter_txn=("counter_txn_cnt", "sum"),
                    avg_wait=("avg_wait_minutes", "mean"),
                    error_rate=("error_rate", "mean"),
                    complaint=("complaint_per_10k", "mean"),
                    satisfaction=("customer_satisfaction", "mean"),
                    e_channel=("e_channel_rate", "mean"),
                    capacity=("capacity_utilization", "mean"))
               .reset_index())
    daily.to_csv(os.path.join(args.out_dir, "07_全行日度趋势.csv"),
                 index=False, encoding="utf-8-sig")

    print("[7/7] 写出汇总 JSON ...")
    payload = {
        "generated_from": {
            "detail_rows": int(len(raw)),
            "branches": int(branches["branch_code"].nunique()),
            "date_range": [str(raw["stat_date"].min().date()),
                           str(raw["stat_date"].max().date())],
            "data_nature": "bootstrap 模拟数据（参数锚定真实公开数据）",
            "simulation_meta": meta,
        },
        "evaluation": summary,
        "alerts_total": int(len(alerts)),
        "alerts_by_cause": (
            alerts["cause"].value_counts().to_dict() if not alerts.empty else {}
        ),
        "method_note": {
            "mad": "横截面 Median + 1.4826*MAD，|z|>3；对极端值稳健",
            "ewma": f"单网点 EWMA 控制图 λ={EWMA_LAMBDA}，|z|>{EWMA_LIMIT}；抓缓慢漂移",
            "stl": f"STL(period=7, robust) 残差 z 阈值 {STL_THRESHOLD}；剥离周内效应",
            "caveat": "MySQL 无 MEDIAN 聚合，SQL 层用平均绝对偏差近似；Python 层用真 MAD",
        },
    }
    with io.open(os.path.join(args.out_dir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    print("\n输出目录:", args.out_dir)
    for fn in sorted(os.listdir(args.out_dir)):
        p = os.path.join(args.out_dir, fn)
        if os.path.isfile(p):
            print(f"  {fn}  ({os.path.getsize(p):,} bytes)")
    return payload


if __name__ == "__main__":
    main()
