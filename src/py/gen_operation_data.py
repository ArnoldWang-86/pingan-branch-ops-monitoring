# -*- coding: utf-8 -*-
"""银行网点日运营数据模拟器（bootstrap-simulation）。

**这是模拟数据，不是真实经营数据。**
生成参数锚定真实公开指标（央行支付体系运行报告、上市银行年报等），
锚点见 data/raw/公开数据_原始提取.json；README 与报告里都明确标注。

为什么需要模拟数据
------------------
「网点级日运营指标」（等候时长、差错率、投诉率）在国内没有公开的
微观数据集，公开渠道只能拿到总量与行业口径。因此本项目的做法是：
用真实公开宏观数据设定水位与量级，再用模拟数据构造一个**带已知异常**
的运营数据集，用来验证「指标监控 + 异常预警」这套方法本身。

关键设计：异常是登记在案的（ground truth）
------------------------------------------
每次注入都在 anomaly_ground_truth.json 里登记网点、日期、类型与真实成因，
并且生成后用 validate_injections() 自检「该异常确实在数据里可观测到」，
自检不通过直接报错。这样后续才能算查全率与查准率，
而不是只给一张"看起来有异常"的图。

三种异常性质（对应简历里「数据错误 / 口径差异 / 真实风险」的区分）
------------------------------------------------------------------
data_error   数据错误 —— 批量重跑、字段单位错，应修正或剔除，不是业务风险
caliber_gap  口径差异 —— 统计口径调整导致台阶式跳变，不应问责网点
true_risk    真实风险 —— 指标真实劣化，需要运营干预

用法：
  python gen_operation_data.py                  # 20 网点 x 90 天
  python gen_operation_data.py --seed 7
  python gen_operation_data.py --no-validate    # 跳过自检（不推荐）
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))

ANCHORS_FILE = os.path.join(PROJ, "data", "raw", "公开数据_原始提取.json")

# ---------------------------------------------------------------- 模型常数
# 全部集中在这里，方便复核与调参；每个常数都在注释里说明来源或推导。

SERVICE_MIN_PER_TXN = 8.5      # 单笔柜面业务平均耗时（分钟），行业普遍区间 6~12
EFFECTIVE_HOURS_PER_DAY = 7.5  # 单窗口日有效服务时长（小时）
BASE_WAIT_COEF = 12.0          # 等候时长系数：wait = 系数 * u/(1-u)^0.85
BASE_ERROR_RATE = 0.0045       # 业务差错率基线（笔/笔）
BASE_COMPLAINT_PER_10K = 0.65  # 投诉率基线（笔/万笔柜面业务）
BASE_COST_RATIO = 0.28         # 运营成本收入比基线
BASE_SATISFACTION = 4.62       # 客户满意度基线（0~5）

# 兜底锚点：采集脚本未产出时使用，并在 simulation_meta 里标明 used_fallback
FALLBACK_ANCHORS = {
    "used_fallback": True,
    "note": "未找到公开数据提取文件，使用行业普遍公开区间作为模拟参数",
    "anchors": {
        "电子渠道分流率": {"center": 0.72, "range": (0.60, 0.86)},
        "业务差错率": {"center": BASE_ERROR_RATE, "range": (0.0018, 0.0090)},
        "成本收入比": {"center": BASE_COST_RATIO, "range": (0.20, 0.38)},
        "人均等候时长_分钟": {"center": 12.0, "range": (4.0, 28.0)},
    },
}


# ---------------------------------------------------------------- 网点

@dataclass
class Branch:
    code: str
    name: str
    kind: str
    region: str
    age_years: float
    base_total_volume: float     # 日均总业务量（笔，含各渠道）
    e_channel: float             # 电子渠道分流率
    windows: int
    staff: int
    is_ftz: int = 0
    # 隐含质量（不进明细表）
    latency_skill: float = 0.0
    error_skill: float = 0.0
    complaint_skill: float = 0.0


BRANCH_PLAN = [
    ("浦东新区", 4, "综合支行"),
    ("黄浦区", 3, "综合支行"),
    ("静安区", 2, "综合支行"),
    ("自贸区临港新片区", 2, "自贸区分行营业部"),
    ("闵行区", 3, "综合支行"),
    ("宝山区", 2, "社区支行"),
    ("松江区", 2, "社区支行"),
    ("普陀区", 2, "小微专营支行"),
]

# 网点类型档位：总业务量、窗口数、人员数、电子渠道偏移、开业年限加成
#
# 档位必须保证「窗口供给与业务量需求」匹配，否则会造出长期超负荷的网点。
# 第一版把自贸区分行营业部的业务量档位给到 2.60、窗口只给 1.60，
# 结果该网点窗口产能饱和度长期 124%、日均等候 215 分钟，
# 而它并没有被注入任何异常——这不是运营现实，而是我的参数标定错误
# （并且它会把「单位错误」类误报堆到那个网点上）。
# 修正后各类型的最高峰日饱和度落在 0.6~0.9 区间：
# 常态网点不应长期满负荷，只有注入了「产能不足」的网点才会被推到高位。
KIND_PROFILE = {
    "自贸区分行营业部": {"vol": 1.90, "win": 1.80, "staff": 1.75, "e": -0.05, "tenure": 14},
    "综合支行":         {"vol": 1.20, "win": 1.20, "staff": 1.15, "e": 0.00, "tenure": 7},
    "小微专营支行":     {"vol": 0.92, "win": 1.00, "staff": 0.90, "e": 0.03, "tenure": 4},
    "社区支行":         {"vol": 0.58, "win": 0.72, "staff": 0.62, "e": 0.05, "tenure": 2},
}

# 周内效应：周一最忙、周末最闲
WEEKDAY_FACTOR = [1.18, 1.10, 1.06, 1.04, 1.08, 1.16, 0.34]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- 锚点

def load_anchors():
    if not os.path.exists(ANCHORS_FILE):
        return {"used_fallback": True, "anchors": dict(FALLBACK_ANCHORS["anchors"])}, \
               "fallback: 锚点文件不存在"
    try:
        with io.open(ANCHORS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:                                     # noqa: BLE001
        return {"used_fallback": True, "anchors": dict(FALLBACK_ANCHORS["anchors"])}, \
               f"fallback: 锚点文件解析失败 {e}"

    anchors = {}
    metrics = raw.get("metrics") or []

    # 每个锚点列出候选关键词、**可接受的单位**与合理业务区间。
    # 为什么必须校验单位：人行报告里的「离柜交易总额 2626.80 万亿元」
    # 「电子支付业务笔数 1145.87 亿笔」这类指标名里含关键词，但它们是**绝对量**，
    # 一旦被当成比率取用，会把分流率设到 0.86 这种离谱水平，
    # 进而让整个模拟数据失真。因此只接受单位为 % 或纯比例（0~1）的值。
    SPEC = {
        "成本收入比": {
            "kws": ["成本收入比"],
            "ok_units": ["%", "比率", ""],
            "range": (0.10, 0.60),
            "note": "取自上市银行年报披露的成本收入比（公开值，按 % 归一化为比例）。",
        },
        "业务差错率": {
            "kws": ["差错率", "差错"],
            "ok_units": ["%", "比率", ""],
            "range": (0.0001, 0.05),
            "note": "公开渠道无银行业统一差错率口径，采用行业普遍区间作为模拟参数。",
        },
    }

    def normalize(val, unit):
        """把带单位的量归一化成比例；无法确认为比率的返回 None。"""
        u = (unit or "").strip()
        if u in ("%", "％"):
            v = val / 100.0
        elif u in ("", "比率", "比例"):
            v = val if val <= 1.0 else None      # 大于 1 说明不是比例
        else:
            return None                            # 万亿元 / 亿笔 / 万户 等一律拒绝
        return v

    for key, spec in SPEC.items():
        lo, hi = spec["range"]
        hit = None
        for kw in spec["kws"]:
            for m in metrics:
                name = str(m.get("metric_name", ""))
                if kw not in name or m.get("value") is None:
                    continue
                try:
                    raw_val = abs(float(m["value"]))
                except (TypeError, ValueError):
                    continue
                val = normalize(raw_val, m.get("unit"))
                if val is None or not (lo <= val <= hi):
                    continue
                hit = (m, val, kw, raw_val)
                break
            if hit:
                break
        if hit:
            m, val, kw, raw_val = hit
            anchors[key] = {
                "center": val,
                "range": (val * 0.8, val * 1.2),
                "source_id": m.get("source_id"),
                "raw_metric": m.get("metric_name"),
                "raw_value": raw_val,
                "raw_unit": m.get("unit"),
                "period": m.get("period"),
                "matched_keyword": kw,
                "note": spec["note"],
            }
        else:
            anchors[key] = dict(FALLBACK_ANCHORS["anchors"][key])
            anchors[key]["note"] = (spec["note"]
                                    + "（未在公开数据中找到单位与量级都匹配的指标）")

    # 电子渠道分流率：公开渠道**没有**这个口径。
    # 人行披露的是离柜交易总额、手机银行客户数等绝对量，协会也是绝对量；
    # 用它们去相除属于不同口径、不同量纲的硬算，得出的数字没有意义。
    # 因此这里明确使用行业普遍区间，并在文档与报告里说明「这是量级参照」。
    anchors["电子渠道分流率"] = dict(FALLBACK_ANCHORS["anchors"]["电子渠道分流率"])
    anchors["电子渠道分流率"]["note"] = (
        "公开渠道无「电子渠道分流率」口径（人行与银行业协会均为绝对量披露），"
        "采用行业普遍区间作为模拟参数的量级参照，**不是**由公开数据计算得出。"
    )

    for key, val in FALLBACK_ANCHORS["anchors"].items():
        anchors.setdefault(key, dict(val))

    # ---- 行业与区域参照（不参与模拟参数，仅用于报告与看板的口径说明）
    refs = {}
    ref_spec = {
        "全国银行业营业网点数": ["营业网点", "网点数量", "机构网点"],
        "自助银行数量": ["自助银行"],
        "离柜交易总额": ["离柜交易"],
        "手机银行个人客户数": ["手机银行"],
        "上海市常住人口": ["常住人口"],
        "上海自贸区临港新片区离岸贸易规模": ["离岸贸易"],
    }
    for name, kws in ref_spec.items():
        for m in metrics:
            mn = str(m.get("metric_name", ""))
            if any(k in mn for k in kws) and m.get("value") is not None:
                refs[name] = {
                    "value": m.get("value"), "unit": m.get("unit"),
                    "period": m.get("period"), "source_id": m.get("source_id"),
                    "raw_metric": mn,
                }
                break

    # ---- 岗位直接相关的分行序列（平安银行年报逐期单列「上海自贸试验区分行」）
    ftz = []
    for m in metrics:
        mn = str(m.get("metric_name", ""))
        if "上海自贸试验区分行" in mn:
            ftz.append({
                "metric": mn, "value": m.get("value"), "unit": m.get("unit"),
                "period": m.get("period"), "source_id": m.get("source_id"),
            })

    return {
        "used_fallback": len(anchors) == 0,
        "meta": {"source_file": ANCHORS_FILE,
                 "source_count": len(raw.get("sources") or []),
                 "metric_count": len(metrics),
                 "not_found_count": len(raw.get("not_found") or [])},
        "anchors": anchors,
        "industry_reference": refs,
        "ftz_branch_series": ftz,
    }, None


# ---------------------------------------------------------------- 生成网点

def build_branches(rng, anchors):
    branches, idx = [], 0
    for region, n, kind in BRANCH_PLAN:
        prof = KIND_PROFILE[kind]
        for k in range(n):
            idx += 1
            latency_skill = rng.gauss(0, 0.85)
            error_skill = rng.gauss(0, 0.75)
            complaint_skill = rng.gauss(0, 0.80)
            if latency_skill > 0.6:            # 等候长 → 差错与投诉也偏多
                error_skill += 0.45
                complaint_skill += 0.55

            vol = clamp(rng.gauss(1.0, 0.14), 0.75, 1.28) * prof["vol"]
            e_base = clamp(anchors["电子渠道分流率"]["center"] + prof["e"]
                           + rng.gauss(0, 0.045), 0.48, 0.92)
            is_ftz = 1 if "自贸" in region else 0
            name = (f"{region}分行营业部" if kind == "自贸区分行营业部"
                    else f"{region}第{k + 1}支行")
            branches.append(Branch(
                code=f"SH{idx:03d}", name=name, kind=kind, region=region,
                age_years=prof["tenure"] + rng.uniform(0.4, 12.0),
                base_total_volume=round(420.0 * vol, 1),
                e_channel=round(e_base, 4),
                windows=max(2, int(round(5 * prof["win"] + rng.gauss(0, 0.5)))),
                staff=max(4, int(round(13 * prof["staff"] + rng.gauss(0, 1.4)))),
                is_ftz=is_ftz, latency_skill=latency_skill,
                error_skill=error_skill, complaint_skill=complaint_skill,
            ))
    return branches


# ---------------------------------------------------------------- 异常设计

@dataclass
class Anomaly:
    branch: str
    start: str
    end: str
    kind: str        # data_error / caliber_gap / true_risk
    subtype: str
    description: str
    expect_metric: str
    expect_direction: str
    expect_min_delta: float = 0.15
    field_: dict = field(default_factory=dict)


def plan_anomalies(branches, rng, days):
    """设计注入的异常（在生成明细之前定好）。"""
    d0, d1 = days[0], days[-1]
    plan: list[Anomaly] = []

    def pick():
        used = {p.branch for p in plan}
        cand = [b for b in branches if b.code not in used]
        return rng.choice(cand)

    # ---- A1 数据错误：批量任务重跑，柜面业务量被重复计数
    b = pick()
    s = d0 + timedelta(days=rng.randint(22, 38))
    plan.append(Anomaly(
        b.code, s.isoformat(), (s + timedelta(days=2)).isoformat(),
        "data_error", "duplicate_batch",
        f"{b.code} {b.name}：核心系统日终批量任务重跑，柜面业务量按 1.85 倍重复计数 3 天；"
        f"成本收入比因分母虚高而被动下降，指标之间不自洽",
        "counter_txn_cnt", "up", 0.45,
    ))

    # ---- A2 数据错误：叫号系统字段单位错（分钟写成秒）
    b = pick()
    s = d0 + timedelta(days=rng.randint(58, 72))
    plan.append(Anomaly(
        b.code, s.isoformat(), s.isoformat(),
        "data_error", "unit_error",
        f"{b.code} {b.name}：叫号系统对接字段单位错误，等候时长按秒写入（放大 60 倍）1 天；"
        f"业务量、差错率均无异常",
        "avg_wait_minutes", "up", 8.0,
    ))

    # ---- B 口径差异：柜面业务量口径调整（剔除查询类交易）
    # 幅度取 0.75（名义 -25%）：柜面业务量 = 总业务量 ×（1 - 电子渠道分流率），
    # 因此对「柜面业务量」的实际影响会被渠道占比稀释，必须留出足够信噪比，
    # 否则真实的口径调整会淹没在周内波动里（第一版 0.78 就偏小）。
    b = pick()
    s = d0 + timedelta(days=rng.randint(30, 40))
    plan.append(Anomaly(
        b.code, s.isoformat(), d1.isoformat(),
        "caliber_gap", "definition_change",
        f"{b.code} {b.name}：{s.isoformat()} 起柜面业务量口径调整（剔除查询类交易），"
        f"柜面业务量台阶式下降约 30%，但等候时长、差错率、投诉率均保持基线水平",
        "counter_txn_cnt", "down", 0.25,
    ))

    # ---- C1 真实风险：产能不足（业务量与等候时长同步走高）
    b = pick()
    s = d0 + timedelta(days=rng.randint(20, 30))
    plan.append(Anomaly(
        b.code, s.isoformat(), d1.isoformat(),
        "true_risk", "capacity_shortage",
        f"{b.code} {b.name}：{s.isoformat()} 起周边社区集中交付入住，柜面业务量持续上升约 35%、"
        f"平均等候时长上升约 55%，窗口数未同步调整（产能不足）",
        "avg_wait_minutes", "up", 0.25,
    ))

    # ---- C2 真实风险：差错率漂移（新人上岗 + 复核缺失）
    b = pick()
    s = d0 + timedelta(days=rng.randint(25, 38))
    plan.append(Anomaly(
        b.code, s.isoformat(), d1.isoformat(),
        "true_risk", "error_drift",
        f"{b.code} {b.name}：{s.isoformat()} 起 3 名柜员轮换为新人，业务差错率由约 0.45% "
        f"逐步升至约 1.4%，事后监督复核量同步上升",
        "error_rate", "up", 0.40,
    ))

    # ---- C3 真实风险：集中服务投诉事件
    # 注意：基线投诉量本身很低（每万笔约 0.65 笔），所以必须按「绝对件数」注入
    # 而不是「乘一个倍数」——乘 6 之后期望值仍可能不足 1 笔，泊松抽样会长期取到 0，
    # 异常实际上就不存在了（第一版模拟器正是在这里出错的）。
    b = pick()
    s = d0 + timedelta(days=rng.randint(55, 68))
    plan.append(Anomaly(
        b.code, s.isoformat(), (s + timedelta(days=4)).isoformat(),
        "true_risk", "service_event",
        f"{b.code} {b.name}：{s.isoformat()} 起发生一起集中服务投诉事件，"
        f"每日新增投诉约 5~9 笔并持续 5 天",
        "complaint_per_10k", "up", 1.00,
    ))

    # ---- C4 真实风险：自助设备故障，柜面短时承压
    b = pick()
    s = d0 + timedelta(days=rng.randint(14, 26))
    plan.append(Anomaly(
        b.code, s.isoformat(), (s + timedelta(days=1)).isoformat(),
        "true_risk", "atm_outage",
        f"{b.code} {b.name}：自助设备故障 2 天，电子渠道分流率骤降、"
        f"柜面业务量与等候时长同步上升",
        "counter_txn_cnt", "up", 0.20,
    ))

    return plan


def anomaly_index(plan):
    idx = {}
    for a in plan:
        cur, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
        while cur <= end:
            idx.setdefault((a.branch, cur.isoformat()), []).append(a)
            cur += timedelta(days=1)
    return idx


# ---------------------------------------------------------------- 明细生成

def gen_rows(branches, days, plan, anchors, rng, start_date):
    idx = anomaly_index(plan)
    caliber_from = {a.branch: date.fromisoformat(a.start)
                    for a in plan if a.subtype == "definition_change"}
    ramp_from = {(a.branch, a.subtype): date.fromisoformat(a.start)
                 for a in plan if a.kind == "true_risk"
                 and a.subtype in ("capacity_shortage", "error_drift")}

    err_center = clamp(float(anchors["业务差错率"]["center"]), 0.0005, 0.02)
    e_center = float(anchors["电子渠道分流率"]["center"])
    cost_center = float(anchors["成本收入比"]["center"])

    rows = []
    for b in branches:
        for d in days:
            subs = {a.subtype for a in idx.get((b.code, d.isoformat()), [])}
            wd = d.weekday()
            dom = d.day
            drift = 1.0 + 0.0018 * (d - start_date).days            # 业务自然增长
            month_f = 1.0 + 0.05 * math.sin((dom - 1) / 30.0 * 2 * math.pi)

            # ===== 单一口径链：先算真实工作量，再算上报口径 =====
            # 这样每个注入只在一处生效，避免重复相乘（第一版曾在两处各乘一次）。
            base_vol = b.base_total_volume * WEEKDAY_FACTOR[wd] * month_f * drift
            e_ch = b.e_channel
            windows = b.windows

            real_vol = base_vol
            if "atm_outage" in subs:                     # 自助设备故障 → 客户转柜面
                real_vol *= 1.30
                e_ch -= 0.20
            cap_s = ramp_from.get((b.code, "capacity_shortage"))
            if cap_s and d >= cap_s:                     # 产能不足：业务量递增
                real_vol *= 1.0 + min(0.35, 0.030 * (d - cap_s).days)
            e_ch = clamp(e_ch, 0.25, 0.95)

            real_counter = real_vol * (1 - e_ch)
            smart_vol = real_vol * e_ch * 0.62
            mobile_vol = real_vol * e_ch * 0.38

            # ---- 等候时长：由产能占用率经排队论直觉推导
            # 必须用 real_counter 而非上报口径：口径调整只改变「上报的业务量」，
            # 真实排队压力没变，等候时长就不应跟着跳变——
            # 这正是区分「口径差异」与「真实风险」的关键依据。
            capacity_min = windows * EFFECTIVE_HOURS_PER_DAY * 60
            need_min = real_counter * SERVICE_MIN_PER_TXN
            u = clamp(need_min / capacity_min, 0.02, 0.985)
            wait = BASE_WAIT_COEF * (u / (1 - u) ** 0.85)
            wait += 1.8 * b.latency_skill + rng.gauss(0, 0.55)
            if "atm_outage" in subs:
                wait *= 1.35
            if wd == 0:
                wait *= 1.06
            if "unit_error" in subs:
                wait *= 60.0
            wait = max(1.0, wait)

            # ---- 上报口径业务量（只在这里叠加「口径类」失真）
            counter_vol = real_counter
            if "duplicate_batch" in subs:                # 批量重跑 → 重复计数
                counter_vol *= 1.85
            if b.code in caliber_from and d >= caliber_from[b.code]:
                counter_vol *= 0.60                     # 口径剔除查询类交易

            # ---- 差错：泊松计数，保证差错率指标有真实抽样波动
            err_rate = err_center * (1 + 0.45 * b.error_skill)
            err_s = ramp_from.get((b.code, "error_drift"))
            if err_s and d >= err_s:
                err_rate *= 1.0 + min(2.2, 0.16 * (d - err_s).days)
            err_rate *= rng.lognormvariate(0, 0.18)
            errors = poisson(rng, err_rate * counter_vol)

            # ---- 投诉：泊松计数；集中投诉事件按绝对件数注入（见 plan_anomalies 说明）
            comp_rate = BASE_COMPLAINT_PER_10K * (1 + 0.5 * b.complaint_skill)
            comp_rate *= rng.lognormvariate(0, 0.35)
            complaints = poisson(rng, comp_rate * counter_vol / 10000.0)
            if "service_event" in subs:
                complaints += rng.randint(5, 9)

            # ---- 成本收入比
            cost = cost_center * (1 + 0.07 * b.latency_skill) + rng.gauss(0, 0.010)
            if "duplicate_batch" in subs:
                cost *= 0.955          # 分母（业务量）虚高 → 成本率被动下降，不自洽

            # ---- 满意度
            sat = (BASE_SATISFACTION
                   - 0.028 * max(0.0, wait - 10.0)
                   - 0.30 * max(0.0, err_rate / max(err_center, 1e-9) - 1.0))
            if "unit_error" in subs:
                sat -= 0.15            # 系统里显示的超长等候也会影响当期评价
            sat = clamp(sat + rng.gauss(0, 0.045), 3.0, 5.0)

            review = poisson(rng, counter_vol * 0.018 + errors * 1.8)

            rows.append({
                "stat_date": d.isoformat(),
                "weekday": wd + 1,
                "branch_code": b.code,
                "branch_name": b.name,
                "branch_kind": b.kind,
                "region": b.region,
                "is_free_trade_zone": b.is_ftz,
                "open_windows": windows,
                "staff_on_duty": max(1, b.staff + int(round(rng.gauss(0, 0.6)))),
                "counter_txn_cnt": int(round(counter_vol)),
                "smart_device_txn_cnt": int(round(smart_vol)),
                "mobile_txn_cnt": int(round(mobile_vol)),
                "avg_wait_minutes": round(wait, 2),
                "max_wait_minutes": round(wait * rng.uniform(2.0, 3.2), 2),
                "txn_error_cnt": errors,
                "complaint_cnt": complaints,
                "post_review_cnt": review,
                "customer_satisfaction": round(sat, 3),
                "op_cost_ratio": round(cost, 5),
            })
    return rows


def poisson(rng, lam):
    """取 lam 的泊松随机数；用 Knuth 法避免额外依赖，lam 大时退回正态近似。"""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


# ---------------------------------------------------------------- 自检

def validate_injections(rows, plan, strict=True):
    """自检：确认每个注入的异常在生成数据里**确实可观测**。

    这是本项目最重要的一道质量闸门——第一版模拟器里
    「投诉率升至基线 4 倍」这个注入因为基线投诉数几乎是 0，
    乘 4 之后依然是 0，异常实际上根本没发生。
    没有这道自检，检测算法会去「检出」一个不存在的异常，
    评估结果就是假的。
    """
    import pandas as pd
    import numpy as np

    df = pd.DataFrame(rows)
    df["stat_date"] = pd.to_datetime(df["stat_date"])
    df["error_rate"] = df["txn_error_cnt"] / df["counter_txn_cnt"].replace(0, np.nan)
    df["complaint_per_10k"] = df["complaint_cnt"] / df["counter_txn_cnt"].replace(0, np.nan) * 10000
    df["total_txn_cnt"] = df["counter_txn_cnt"] + df["smart_device_txn_cnt"] + df["mobile_txn_cnt"]
    df["e_channel_rate"] = ((df["smart_device_txn_cnt"] + df["mobile_txn_cnt"])
                            / df["total_txn_cnt"].replace(0, np.nan))

    results = []
    for a in plan:
        s, e = pd.Timestamp(a.start), pd.Timestamp(a.end)
        sub = df[df["branch_code"] == a.branch]
        pre = sub[(sub["stat_date"] >= s - pd.Timedelta(days=14)) & (sub["stat_date"] < s)]
        dur = sub[(sub["stat_date"] >= s) & (sub["stat_date"] <= e)]
        # 缓慢漂移类异常的效应在末段最大，取最后 5 天评估
        tail = dur.tail(5) if a.subtype in ("error_drift",) else dur
        col = a.expect_metric
        base_v = float(pre[col].mean()) if len(pre) else float("nan")
        anom_v = float(tail[col].mean()) if len(tail) else float("nan")
        # 基线为 0 时（例如投诉这种低频计数指标），相对变化是无穷大，
        # 此时改用「绝对增量 / 基线量级」判断，避免 inf 让自检失效。
        if math.isnan(base_v) or math.isnan(anom_v):
            delta = float("nan")
            observed = False
            judge_basis = "无法计算（样本缺失）"
        elif abs(base_v) < 1e-12:
            observed = abs(anom_v) >= max(a.expect_min_delta, 1.0)
            delta = float("inf") if anom_v > 0 else (float("-inf") if anom_v < 0 else 0.0)
            judge_basis = f"基线为 0，改按绝对增量判断：{anom_v:.4g}"
        else:
            delta = (anom_v - base_v) / base_v
            observed = (delta >= a.expect_min_delta if a.expect_direction == "up"
                        else delta <= -a.expect_min_delta)
            judge_basis = f"相对变化 {delta:+.2%}"

        # 对「数据错误 / 口径差异」额外要求对照指标保持平稳，确保性质可区分。
        #
        # 判据用「与全行同期自然漂移的差值」，而不是绝对阈值。
        # 原因：业务量本身在 90 天里有自然增长，全行等候时长同期会漂移约 10%。
        # 若用绝对 ±15% 容差，会把自然漂移误判成「口径调整改变了真实运营」，
        # 从而误报自检失败（实测踩过：定义变更注入的对照偏移 +16.1%，其实全部来自漂移）。
        def drift_adjusted(col, target_ratio):
            """返回：目标网点该指标的变化幅度 减去 全行同期变化幅度。"""
            others = df[df["branch_code"] != a.branch]
            o_pre = others[(others["stat_date"] >= s - pd.Timedelta(days=14))
                           & (others["stat_date"] < s)][col].mean()
            o_dur = others[(others["stat_date"] >= s) & (others["stat_date"] <= e)][col].mean()
            if not o_pre or o_pre != o_pre:
                return 0.0, 0.0, 0.0
            b = pre[col].mean()
            v = dur[col].mean()
            if not b or b != b:
                return 0.0, 0.0, 0.0
            raw = (v - b) / b
            market = (o_dur - o_pre) / o_pre if o_pre else 0.0
            return raw, market, raw - market

        control_ok = True
        control_note = ""
        if a.subtype == "unit_error":
            raw, market, excess = drift_adjusted("counter_txn_cnt", 0.35)
            control_ok = abs(excess) < 0.35
            control_note = f"业务量对照(剔除全行漂移){excess:+.1%}"
        if a.subtype == "definition_change":
            raw, market, excess = drift_adjusted("avg_wait_minutes", 0.15)
            control_ok = abs(excess) < 0.15
            control_note = f"等候时长对照(剔除全行漂移){excess:+.1%}"

        results.append({
            "subtype": a.subtype, "kind": a.kind, "branch_code": a.branch,
            "expect_metric": col,
            "baseline": round(base_v, 4) if base_v == base_v else None,
            "anomaly_value": round(anom_v, 4) if anom_v == anom_v else None,
            "delta": (None if delta != delta else
                      ("inf" if delta == float("inf") else
                       ("-inf" if delta == float("-inf") else round(delta, 4)))),
            "judge_basis": judge_basis,
            "expected_direction": a.expect_direction,
            "min_delta": a.expect_min_delta,
            "observed": bool(observed), "control_stable": bool(control_ok),
            "control_note": control_note,
            "pass": bool(observed and control_ok),
        })

    failed = [r for r in results if not r["pass"]]
    if failed and strict:
        msg = "\n".join(
            f"  - {r['subtype']} @{r['branch_code']}: delta={r['delta']} "
            f"(需 {r['expected_direction']} >= {r['min_delta']}), "
            f"对照指标平稳={r['control_stable']} {r.get('control_note','')}"
            for r in failed
        )
        raise SystemExit(
            "注入自检未通过：以下异常在生成数据里不可观测，"
            "评估结果将不可信。请调整注入幅度或模型参数。\n" + msg
        )
    return results


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--start", default="2026-06-22")
    ap.add_argument("--out-dir", default=os.path.join(PROJ, "data", "raw"))
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    start_date = date.fromisoformat(args.start)
    days = [start_date + timedelta(days=i) for i in range(args.days)]

    anchors, warn = load_anchors()
    branches = build_branches(rng, anchors["anchors"])
    plan = plan_anomalies(branches, rng, days)
    rows = gen_rows(branches, days, plan, anchors["anchors"], rng, start_date)

    checks = []
    if not args.no_validate:
        checks = validate_injections(rows, plan, strict=True)

    os.makedirs(args.out_dir, exist_ok=True)
    paths = {
        "detail": os.path.join(args.out_dir, "branch_ops_detail.jsonl"),
        "branches": os.path.join(args.out_dir, "branches.json"),
        "truth": os.path.join(args.out_dir, "anomaly_ground_truth.json"),
        "meta": os.path.join(args.out_dir, "simulation_meta.json"),
        "checks": os.path.join(args.out_dir, "injection_selfcheck.json"),
    }

    with io.open(paths["detail"], "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with io.open(paths["branches"], "w", encoding="utf-8") as f:
        json.dump([{
            "branch_code": b.code, "branch_name": b.name, "branch_kind": b.kind,
            "region": b.region, "age_years": round(b.age_years, 1),
            "base_total_volume": b.base_total_volume,
            "e_channel_rate": b.e_channel, "open_windows": b.windows,
            "staff": b.staff, "is_free_trade_zone": b.is_ftz,
        } for b in branches], f, ensure_ascii=False, indent=2)

    with io.open(paths["truth"], "w", encoding="utf-8") as f:
        json.dump({
            "seed": args.seed,
            "warning": "本文件为模拟数据中人为注入的异常清单，用作检测算法的评估基准",
            "definition": {
                "data_error": "数据错误：录入/接口/单位问题，应修正或剔除，不是业务风险",
                "caliber_gap": "口径差异：统计口径变化导致的台阶式跳变，不应问责网点",
                "true_risk": "真实风险：业务指标真实劣化，需要运营干预",
            },
            "anomalies": [{
                "branch_code": a.branch, "start": a.start, "end": a.end,
                "kind": a.kind, "subtype": a.subtype, "description": a.description,
                "expect_metric": a.expect_metric,
                "expect_direction": a.expect_direction,
                "expect_min_delta": a.expect_min_delta,
            } for a in plan],
        }, f, ensure_ascii=False, indent=2)

    with io.open(paths["checks"], "w", encoding="utf-8") as f:
        json.dump(checks, f, ensure_ascii=False, indent=2)

    with io.open(paths["meta"], "w", encoding="utf-8") as f:
        json.dump({
            "simulated": True,
            "note": "本数据集为 bootstrap 模拟数据，用于方法验证；"
                    "生成参数锚定真实公开数据，锚点见 public_anchors 字段。"
                    "网点级日运营微观数据在国内没有公开来源，故采用模拟数据。",
            "seed": args.seed, "start_date": args.start, "days": args.days,
            "branches": len(branches), "rows": len(rows),
            "anomalies_injected": len(plan),
            "anchor_warning": warn,
            "public_anchors": anchors,
            "model_constants": {
                "service_min_per_txn": SERVICE_MIN_PER_TXN,
                "effective_hours_per_day": EFFECTIVE_HOURS_PER_DAY,
                "base_wait_coef": BASE_WAIT_COEF,
                "base_error_rate": BASE_ERROR_RATE,
                "base_complaint_per_10k": BASE_COMPLAINT_PER_10K,
                "base_cost_ratio": BASE_COST_RATIO,
            },
        }, f, ensure_ascii=False, indent=2)

    print(f"网点 {len(branches)} 家 / 明细 {len(rows)} 行 -> {paths['detail']}")
    print(f"注入异常 {len(plan)} 例 -> {paths['truth']}")
    if checks:
        print(f"注入自检：{sum(1 for c in checks if c['pass'])}/{len(checks)} 通过")
        for c in checks:
            flag = "OK " if c["pass"] else "FAIL"
            print(f"  [{flag}] {c['subtype']:20s} {c['branch_code']}  "
                  f"{c['expect_metric']}={c['baseline']} -> {c['anomaly_value']} "
                  f"(delta {c['delta']})")
    if warn:
        print("警告:", warn)


if __name__ == "__main__":
    main()
