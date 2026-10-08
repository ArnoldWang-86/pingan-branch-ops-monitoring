# -*- coding: utf-8 -*-
"""把网页 Agent 需要的数据打包成一个紧凑 JSON，供内嵌进 HTML。

为什么要打包而不是让网页读 CSV
------------------------------
交付物是一个**单文件、离线可用**的网页 Agent：双击就能打开，
不需要任何后端、不联网也能跑完整个工作流程。
浏览器无法读取本地文件系统，所以数据必须内嵌。

打包策略（体积与表达力的权衡）
------------------------------
原始 ops_indicators.csv 有 1.0 MB / 1800 行 / 40 列，直接内嵌太大。
做法：
  · 只保留分析真正会用到的列
  · 数字做定点舍入（比率 4 位、时长 1 位），字符串列做字典编码
  · 网点明细行按「列式」存储（每列一个数组），比行式 JSON 小约 40%
  · 另外预计算几张小的聚合表，减少浏览器端重复计算

用法： python build_web_data.py
"""
from __future__ import annotations

import io
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
CLEAN = os.path.join(PROJ, "data", "clean")
RAW = os.path.join(PROJ, "data", "raw")
RESULTS = os.path.join(PROJ, "results")
OUT = os.path.join(PROJ, "tools", "web_data.json")
JS_OUT = os.path.join(PROJ, "tools", "web_data.js")

# 内嵌到网页的列（只留分析要用的，控制体积）
COLS = [
    "stat_date", "weekday", "branch_code", "counter_txn_cnt",
    "avg_wait_minutes", "error_rate", "complaint_per_10k",
    "e_channel_rate", "capacity_utilization", "customer_satisfaction",
    "op_cost_ratio", "vol_dev", "wait_dev", "error_rate_7d",
]


def r(x, n):
    """安全定点舍入：None/NaN 保持为 None。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if np.isnan(v) or np.isinf(v):
        return None
    return round(v, n)


def main():
    d = pd.read_csv(os.path.join(CLEAN, "ops_indicators.csv"))
    d["stat_date"] = pd.to_datetime(d["stat_date"])

    # ---------------- 网点维度
    br = (d.groupby(["branch_code", "branch_name", "branch_kind", "region"])
            .agg(open_windows=("open_windows", "first"),
                 staff=("staff_on_duty", "mean"),
                 avg_vol=("counter_txn_cnt", "mean"),
                 avg_wait=("avg_wait_minutes", "mean"),
                 avg_err=("error_rate", "mean"),
                 avg_comp=("complaint_per_10k", "mean"),
                 avg_ech=("e_channel_rate", "mean"),
                 avg_cap=("capacity_utilization", "mean"),
                 avg_sat=("customer_satisfaction", "mean"),
                 avg_cost=("op_cost_ratio", "mean"))
            .reset_index())
    branches = [{
        "code": row.branch_code,
        "name": row.branch_name,
        "kind": row.branch_kind,
        "region": row.region,
        "windows": int(row.open_windows),
        "staff": round(float(row.staff), 1),
        "avg_vol": r(row.avg_vol, 1),
        "avg_wait": r(row.avg_wait, 2),
        "avg_err": r(row.avg_err, 6),
        "avg_comp": r(row.avg_comp, 3),
        "avg_ech": r(row.avg_ech, 4),
        "avg_cap": r(row.avg_cap, 4),
        "avg_sat": r(row.avg_sat, 3),
        "avg_cost": r(row.avg_cost, 5),
    } for row in br.itertuples()]

    # ---------------- 明细（列式存储）
    dd = d[COLS].copy()
    dd = dd.sort_values(["stat_date", "branch_code"])
    dates = sorted(dd["stat_date"].dt.strftime("%Y-%m-%d").unique().tolist())
    date_idx = {s: i for i, s in enumerate(dates)}
    codes = [b["code"] for b in branches]
    code_idx = {c: i for i, c in enumerate(codes)}

    detail_cols = {
        "date_i": [date_idx[s] for s in dd["stat_date"].dt.strftime("%Y-%m-%d")],
        "branch_i": [code_idx[c] for c in dd["branch_code"]],
        "weekday": [int(x) for x in dd["weekday"]],
        "vol": [int(x) for x in dd["counter_txn_cnt"]],
        "wait": [r(x, 1) for x in dd["avg_wait_minutes"]],
        "err": [r(x, 6) for x in dd["error_rate"]],
        "comp": [r(x, 3) for x in dd["complaint_per_10k"]],
        "ech": [r(x, 4) for x in dd["e_channel_rate"]],
        "cap": [r(x, 4) for x in dd["capacity_utilization"]],
        "sat": [r(x, 3) for x in dd["customer_satisfaction"]],
        "cost": [r(x, 5) for x in dd["op_cost_ratio"]],
        "vol_dev": [r(x, 3) for x in dd["vol_dev"]],
        "wait_dev": [r(x, 3) for x in dd["wait_dev"]],
        "err7": [r(x, 6) for x in dd["error_rate_7d"]],
    }

    # ---------------- 同星期几基线（按 网点 × 星期几）
    wd_base = []
    for code, sub in d.groupby("branch_code"):
        for wd, g in sub.groupby("weekday"):
            wd_base.append({
                "b": code_idx[code], "w": int(wd),
                "vol": r(g["counter_txn_cnt"].median(), 1),
                "wait": r(g["avg_wait_minutes"].median(), 2),
                "err": r(g["error_rate"].median(), 6),
            })

    # ---------------- 预警清单（含成因与处置建议）
    al = pd.read_csv(os.path.join(RESULTS, "02_预警清单.csv"))
    if not al.empty:
        al["stat_date"] = pd.to_datetime(al["stat_date"])
        alerts = [{
            "date": s.strftime("%Y-%m-%d"),
            "b": code_idx.get(bc, -1),
            "code": bc,
            "sev": int(sv),
            "score": r(sc, 1),
            "metric": pm,
            "metricLabel": ml,
            "method": mt,
            "cause": cz,
            "causeCn": ccn if isinstance(ccn, str) else "",
            "action": ac,
            "evidence": ev,
        } for s, bc, sv, sc, pm, ml, mt, cz, ccn, ac, ev in zip(
            al["stat_date"], al["branch_code"], al["severity"], al["severity_score"],
            al["primary_metric"], al["primary_metric_label"], al["methods"],
            al["cause"], al.get("cause_cn", pd.Series([""] * len(al))),
            al["action"], al["evidence"])]
    else:
        alerts = []

    # ---------------- 检出评估
    ev = pd.read_csv(os.path.join(RESULTS, "03_异常检出评估.csv"))
    if "cause_correct_on" not in ev.columns:
        ev["cause_correct_on"] = None

    def _s(v):
        """把可能为 NaN 的字段安全转成 str 或 None。"""
        return None if v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v) else str(v)

    evaluation = [{
        "subtype": x.subtype, "kind": x.kind, "branch": x.branch_code,
        "start": _s(x.start), "end": _s(x.end), "detected": bool(x.detected),
        "on": _s(x.detected_on),
        "cause": _s(x.detected_cause),
        "correct": bool(x.cause_correct) if not pd.isna(x.cause_correct) else False,
        "correctOn": _s(x.cause_correct_on),
    } for x in ev.itertuples()]

    # ---------------- 汇总
    with io.open(os.path.join(RESULTS, "analysis.json"), encoding="utf-8") as f:
        analysis = json.load(f)
    with io.open(os.path.join(RAW, "simulation_meta.json"), encoding="utf-8") as f:
        sim = json.load(f)

    payload = {
        "meta": {
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            "date_range": [dates[0], dates[-1]],
            "n_branches": len(branches),
            "n_rows": len(dd),
            "n_dates": len(dates),
            "simulation": {
                "seed": sim.get("seed"),
                "anomalies_injected": sim.get("anomalies_injected"),
                "used_fallback": (sim.get("public_anchors") or {}).get("used_fallback"),
                "source_count": ((sim.get("public_anchors") or {}).get("meta") or {}).get("source_count"),
                "metric_count": ((sim.get("public_anchors") or {}).get("meta") or {}).get("metric_count"),
                "anchors": {k: v.get("center") for k, v in
                            ((sim.get("public_anchors") or {}).get("anchors") or {}).items()},
            },
            "evaluation": analysis.get("evaluation", {}),
            "alerts_total": analysis.get("alerts_total"),
        },
        "branches": branches,
        "dates": dates,
        "detail_cols": detail_cols,
        "wd_base": wd_base,
        "alerts": alerts,
        "evaluation": evaluation,
        "top_issues": sorted(
            [b for b in branches], key=lambda x: -(x["avg_wait"] or 0))[:5],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    size_json = os.path.getsize(OUT)

    # 同时产出一个 .js（网页用 <script src> 时可用；单文件版会内联它）
    with io.open(JS_OUT, "w", encoding="utf-8") as f:
        f.write("window.OPS_DATA=")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";")
    size_js = os.path.getsize(JS_OUT)

    print("=" * 62)
    print(f" 网点 {len(branches)} 家 / 明细 {len(dd)} 行 / 日期 {len(dates)} 天")
    print(f" 预警 {len(alerts)} 条 / 检出评估 {len(evaluation)} 例")
    print(f" JSON 未压缩: {size_json/1024:.0f} KB")
    print(f" 若 gzip 后约: {_gzip_size(OUT)/1024:.0f} KB（内嵌进 HTML 时用 base64+gzip 更小）")
    print(f" 输出: {os.path.relpath(OUT, PROJ)}")
    return 0


def _gzip_size(path):
    import gzip
    with open(path, "rb") as f:
        return len(gzip.compress(f.read(), 9))


if __name__ == "__main__":
    raise SystemExit(main())
