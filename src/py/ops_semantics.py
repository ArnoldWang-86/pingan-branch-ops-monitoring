# -*- coding: utf-8 -*-
"""语义层：把「数据库里查不到、但业务上必须知道」的口径写下来。

**这一层的角色，以及一处需要如实说明的设计张力**
------------------------------------------------
运行时，Excel 是在**浏览器里**由 SheetJS 现场生成的（见 `web/template.html`），
Python 脚本不参与那一步。所以严格说：

  · **本文件 = 语义层的「设计原件」**：口径、陷阱、分析纪律的唯一权威定义，
    供人阅读、供代码评审、供 Prompt 注入参考。
  · **浏览器里有一份对应的 JS 实现**（`web/template.html` 的 `metricNote()`
    与操作定义里的 `note` 字段），它才是真正写进 Excel 备注的那一份。

两份定义**存在漂移风险**——改了 Python 里的口径说明，不会自动同步到浏览器。
可行的收敛方式有两个：① 用本文件生成 JS 常量内联进页面；
② 只保留浏览器一份，去掉本文件。当前版本选了「保留两份 + 明确标注」，
因为 Python 版还承担着「给 LLM 的提示词」与「给评审看的设计文档」两个用途。
**如果你要把它产品化，应当改成 ①。** 这是已知技术债，不是疏忽。

为什么需要单独一层
------------------
这个项目的指标几乎每一个都有陷阱。举例：

  · 「平均等候时长」在数据里有一个字段单位错误的异常值（放大 60 倍），
    直接算均值会被它拉到 400 分钟以上。
  · 「业务差错率」是低频计数指标，日粒度上分母只有几十到几百笔、
    分子是 0~2 笔，日粒度的偏离几乎全是抽样噪声，必须按周看。
  · 「电子渠道分流率」不能与「柜面业务量」相加，两者是互补不是并列。
  · 最重要的一条：**整份网点明细数据是模拟生成的**。

这些口径不可能从字段名里猜出来，所以必须显式写下来。
"""
from __future__ import annotations

import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))

# ---------------------------------------------------------------- 数据性质

DATA_NATURE = {
    "is_simulated": True,
    "headline": "本工作簿包含两类性质不同的数据，请勿混用",
    "real_data": (
        "真实公开数据：来自中国人民银行《支付体系运行总体情况》17 份报告、"
        "平安银行 6 份定期报告、国家金融监督管理总局、中国银行业协会、"
        "上海市统计局等 28 个来源、601 条已核实指标，每条可按 source_id 溯源到原文。"
    ),
    "simulated_data": (
        "模拟数据：网点级「日运营指标」（20 家网点 × 90 天 = 1,800 条）。"
        "国内没有公开的网点级微观运营数据集——等候时长、柜面业务量、业务差错率、"
        "投诉率都属于各行内部经营数据。因此本项目构造了带已知异常的模拟数据集，"
        "用于验证监控与预警方法本身。"
    ),
    "boundary": (
        "模拟数据锚定的是「量级与波动范围」，不是个体数值。"
        "本工作簿中所有网点层面的具体数字均不代表任何真实银行的经营情况。"
    ),
}

# ---------------------------------------------------------------- 指标口径

# 每个指标的：业务含义、计算口径、已知陷阱。
# 「trap」字段会在生成 Excel 时被自动写入该表的备注行——
# 这样任何人打开工作簿都能看到口径警示，不需要另外查文档。
METRICS = {
    "counter_txn_cnt": {
        "label": "柜面业务量",
        "unit": "笔",
        "meaning": "当日通过人工柜台办理的业务笔数",
        "caliber": "直接取自 ODS 明细，未做口径调整",
        "trap": None,
    },
    "total_txn_cnt": {
        "label": "总业务量",
        "unit": "笔",
        "meaning": "柜面 + 智能设备 + 移动端三渠道合计",
        "caliber": "柜面业务量 + 智能设备业务量 + 移动端业务量",
        "trap": "与「柜面业务量」是包含关系，不可并列相加",
    },
    "e_channel_rate": {
        "label": "电子渠道分流率",
        "unit": "比率",
        "meaning": "非柜面渠道业务量占总业务量的比例",
        "caliber": "(智能设备 + 移动端) / 总业务量",
        "trap": "与柜面业务量是互补关系，两者相加恒为 1",
    },
    "avg_wait_minutes": {
        "label": "平均等候时长",
        "unit": "分钟",
        "meaning": "客户取号到开始办理的平均等待时间",
        "caliber": "直接取自叫号系统",
        "trap": ("存在一个「字段单位错误」的注入异常（等候时长被按秒写入，放大 60 倍），"
                 "直接算均值会被拉到 400 分钟以上。本 Agent 对超过 180 分钟的值"
                 "自动标记为「疑似单位错误」并单独统计，不混入正常均值"),
    },
    "error_rate": {
        "label": "业务差错率",
        "unit": "比率",
        "meaning": "柜面业务中被认定差错的笔数占比",
        "caliber": "差错笔数 / 柜面业务量",
        "trap": ("差错是**低频计数事件**：日粒度上分子常为 0~2 笔，"
                 "偏离几乎全是抽样噪声。**必须按周看**"
                 "（7 天累计差错 / 7 天累计业务量），日粒度只能作参考"),
    },
    "complaint_per_10k": {
        "label": "投诉率",
        "unit": "笔/万笔",
        "meaning": "每万笔柜面业务对应的客户投诉数",
        "caliber": "投诉笔数 / 柜面业务量 × 10000",
        "trap": ("基线常为 0，**相对变化率会变成无穷大**"
                 "（0→2 笔就是 +∞）。必须用绝对增量判定，"
                 "不能用百分比"),
    },
    "capacity_utilization": {
        "label": "窗口产能饱和度",
        "unit": "比率",
        "meaning": "单窗口实际业务量占理论产能的比例",
        "caliber": "（柜面业务量 / 开放窗口数）/ (7.5 小时 × 3600 / 单笔 8.5 分钟)",
        "trap": "长期超过 100% 说明窗口供给不足；但**常态网点不应长期满负荷**",
    },
    "op_cost_ratio": {
        "label": "运营成本收入比",
        "unit": "比率",
        "meaning": "运营成本占营业收入的比例",
        "caliber": "直接取自系统",
        "trap": "其分母是业务量，业务量被虚高（如批量重跑重复计数）会让该比率被动下降——可作为数据错误的判别特征",
    },
    "customer_satisfaction": {
        "label": "客户满意度",
        "unit": "分（0-5）",
        "meaning": "当日服务评价均分",
        "caliber": "直接取自评价系统",
        "trap": None,
    },
    "vol_dev": {
        "label": "业务量偏离度",
        "unit": "比率",
        "meaning": "当日柜面业务量相对「同星期几基线」的偏离",
        "caliber": "(当日值 − 同星期几基线) / 同星期几基线",
        "trap": ("**必须按星期几对齐**。网点业务量周内效应极强"
                 "（周日只有工作日的约 1/3），用 7 日滚动均值做基线会让"
                 "每个周日都显示 -65%，即每周制造一次假异常"),
    },
    "wait_dev": {
        "label": "等候时长偏离度",
        "unit": "比率",
        "meaning": "当日平均等候时长相对「同星期几基线」的偏离",
        "caliber": "(当日值 − 同星期几基线) / 同星期几基线",
        "trap": None,
    },
}

# ---------------------------------------------------------------- 全局口径纪律

DISCIPLINE = [
    "所有比率类指标在数据层只保留分子与分母，比率在查询时计算，避免出现两套口径",
    "周内效应必须剥离：任何时间序列比较都用「同星期几」基线，不用滚动均值",
    "低频计数指标（差错、投诉）不用相对变化率，用绝对增量",
    "异常值不等于是脏数据：先区分「数据错误 / 口径差异 / 真实风险」三类再处置",
    "模拟数据与真实公开数据分开呈现，任何输出都必须带数据性质声明",
]

# ---------------------------------------------------------------- 供 LLM 的提示

def semantic_prompt() -> str:
    """拼出给 LLM 的语义层说明（用于提示词注入）。"""
    L = []
    L.append("【数据性质 — 必须遵守】")
    L.append(f"· {DATA_NATURE['real_data']}")
    L.append(f"· {DATA_NATURE['simulated_data']}")
    L.append(f"· 边界：{DATA_NATURE['boundary']}")
    L.append("")
    L.append("【指标口径与已知陷阱】")
    for key, m in METRICS.items():
        L.append(f"· {m['label']}（字段 {key}，单位 {m['unit']}）"
                 f"：{m['meaning']}；口径 = {m['caliber']}")
        if m.get("trap"):
            L.append(f"  ⚠ 陷阱：{m['trap']}")
    L.append("")
    L.append("【分析纪律】")
    for i, d in enumerate(DISCIPLINE, 1):
        L.append(f"{i}. {d}")
    return "\n".join(L)


def metric_note(field: str) -> str:
    """取某字段的口径备注（用于写入 Excel 表的备注行）。"""
    m = METRICS.get(field)
    if not m:
        return ""
    s = f"{m['label']}：{m['meaning']}；口径 = {m['caliber']}"
    if m.get("trap"):
        s += f"　⚠ {m['trap']}"
    return s


def data_disclaimer_lines() -> list[str]:
    """数据性质声明（写入每个工作簿的第一个 sheet）。"""
    return [
        DATA_NATURE["headline"],
        "",
        "① " + DATA_NATURE["real_data"],
        "② " + DATA_NATURE["simulated_data"],
        "",
        "边界：" + DATA_NATURE["boundary"],
    ]


if __name__ == "__main__":
    for _s in ("stdout", "stderr"):
        _st = getattr(__import__("sys"), _s, None)
        if _st is not None and hasattr(_st, "reconfigure"):
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:                                # noqa: BLE001
                pass
    print(semantic_prompt())
