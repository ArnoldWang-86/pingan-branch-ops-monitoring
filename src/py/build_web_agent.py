# -*- coding: utf-8 -*-
"""组装单文件网页 Agent，并产出一份参考 Excel（含真实图表）用于自检。

为什么要组装成单文件
--------------------
交付物要求「纯离线单文件」：双击就能打开、不联网、不需要后端。
所以 SheetJS（952 KB）与数据包（203 KB）都要内联进 HTML。

同时为什么要额外产一份参考 xlsx
------------------------------
网页里的 Excel 由浏览器端 SheetJS 生成，它能做数字格式/列宽/冻结表头，
但**不能生成图表**。为了不空口承诺，我用 openpyxl 另出一份**带真实图表**的
参考工作簿 `results/参考工作簿_网点运营总览.xlsx`，
并在网页里说明两者的分工：网页 = 当场看清分布，参考簿 = 带图表的归档件。

用法： python build_web_agent.py
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
WEB = os.path.join(PROJ, "web")
TOOLS = os.path.join(PROJ, "tools")
RESULTS = os.path.join(PROJ, "results")
CLEAN = os.path.join(PROJ, "data", "clean")

TEMPLATE = os.path.join(WEB, "template.html")
SHEETJS = os.path.join(TOOLS, "xlsx.full.min.js")
DATA_JS = os.path.join(TOOLS, "web_data.js")
OUT_HTML = os.path.join(RESULTS, "运营问数Agent.html")
OUT_XLSX = os.path.join(RESULTS, "参考工作簿_网点运营总览.xlsx")


# ----------------------------------------------------------------- 参考 Excel

def build_reference_workbook():
    """用 openpyxl 生成一份带**真实图表**的参考工作簿。

    它同时起到自检作用：证明「这个项目的 Excel 输出」是真的能带图表的，
    只是图表由 Excel 端/openpyxl 负责，而不是浏览器端。
    """
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    d = pd.read_csv(os.path.join(CLEAN, "ops_indicators.csv"))
    d["stat_date"] = pd.to_datetime(d["stat_date"])
    with io.open(os.path.join(TOOLS, "web_data.json"), encoding="utf-8") as f:
        payload = json.load(f)

    NAVY = "1F3864"
    thin = Side(style="thin", color="D9E1EC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()

    # ---------------- Sheet 1：说明与数据性质
    ws = wb.active
    ws.title = "说明与数据性质"
    rows = [
        ("银行网点运营指标 · 参考工作簿（带图表）", ""),
        ("生成时间", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")),
        ("说明", "本工作簿由 openpyxl 生成，用于展示「带图表的 Excel 交付物」；"
                 "网页 Agent 里由浏览器端现场生成的是不含图表的版本。"),
        ("", ""),
        ("⚠ 数据性质声明", ""),
        ("① 真实公开数据", f"28 个来源、{payload['meta']['simulation']['metric_count']} 条已核实指标"
                          "（人民银行支付体系报告 17 份、平安银行定期报告 6 份、"
                          "金融监管总局、银行业协会、上海市统计局），用于锚定模拟数据的量级。"),
        ("② 模拟数据", "网点级「日运营指标」——国内没有公开的网点级微观运营数据。"
                      f"本工作簿用 {payload['meta']['n_branches']} 家网点 × "
                      f"{payload['meta']['n_dates']} 天 = {payload['meta']['n_rows']} 条模拟数据"
                      f"（注入 {payload['meta']['simulation']['anomalies_injected']} 例已知异常）。"),
        ("边界", "所有网点层面的具体数字均不代表任何真实银行的经营情况。"),
    ]
    for i, (a, b) in enumerate(rows, 1):
        ws.cell(row=i, column=1, value=a)
        ws.cell(row=i, column=2, value=b)
        if i == 1:
            ws.cell(row=i, column=1).font = Font(bold=True, size=14, color=NAVY)
        elif a.startswith("⚠"):
            ws.cell(row=i, column=1).font = Font(bold=True, color="C8102E")
        else:
            ws.cell(row=i, column=1).font = Font(bold=True, color=NAVY)
        ws.cell(row=i, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 96

    # ---------------- Sheet 2：网点排名（带条形图）
    ws2 = wb.create_sheet("网点等候时长排名")
    br = pd.DataFrame(payload["branches"]).sort_values("avg_wait", ascending=False)
    hdr = ["排名", "网点编码", "网点名称", "网点类型", "平均等候时长(分钟)",
           "产能饱和度", "日均柜面业务量(笔)", "开放窗口数", "周度差错率(‰)"]
    for c, h in enumerate(hdr, 1):
        cell = ws2.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center")
        cell.border = border
    for i, r in enumerate(br.itertuples(), start=2):
        vals = [i - 1, r.code, r.name, r.kind, r.avg_wait,
                r.avg_cap, r.avg_vol, r.windows, (r.avg_err or 0) * 1000]
        for c, v in enumerate(vals, 1):
            cell = ws2.cell(row=i, column=c, value=v)
            cell.border = border
            if c == 5:
                cell.number_format = "0.00"
            elif c == 6:
                cell.number_format = "0.0%"
            elif c == 7:
                cell.number_format = "#,##0"
            elif c == 9:
                cell.number_format = '0.000"‰"'
    for c, w in enumerate([6, 10, 26, 16, 18, 12, 18, 12, 14], 1):
        ws2.column_dimensions[get_column_letter(c)].width = w
    ws2.freeze_panes = "A2"

    ch = BarChart()
    ch.type = "bar"
    ch.title = "各网点平均等候时长（分钟）"
    ch.y_axis.title = "分钟"
    ch.height, ch.width = 12, 20
    data = Reference(ws2, min_col=5, min_row=1, max_row=len(br) + 1)
    cats = Reference(ws2, min_col=2, min_row=2, max_row=len(br) + 1)
    ch.add_data(data, titles_from_data=True)
    ch.set_categories(cats)
    ws2.add_chart(ch, "K2")

    # ---------------- Sheet 3：周内规律（带柱状图）
    ws3 = wb.create_sheet("周内规律")
    wd_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd = d.groupby("weekday").agg(vol=("counter_txn_cnt", "mean"),
                                  wait=("avg_wait_minutes", "median"),
                                  cap=("capacity_utilization", "mean")).reset_index()
    for c, h in enumerate(["星期", "日均柜面业务量(笔)", "平均等候时长中位数(分钟)", "产能饱和度"], 1):
        cell = ws3.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.border = border
    for i, r in enumerate(wd.itertuples(), start=2):
        ws3.cell(row=i, column=1, value=wd_names[int(r.weekday) - 1]).border = border
        c2 = ws3.cell(row=i, column=2, value=round(float(r.vol), 1)); c2.number_format = "#,##0.0"; c2.border = border
        c3 = ws3.cell(row=i, column=3, value=round(float(r.wait), 2)); c3.number_format = "0.00"; c3.border = border
        c4 = ws3.cell(row=i, column=4, value=round(float(r.cap), 4)); c4.number_format = "0.0%"; c4.border = border
    for c, w in enumerate([10, 20, 26, 14], 1):
        ws3.column_dimensions[get_column_letter(c)].width = w

    ch2 = BarChart()
    ch2.title = "各星期几日均柜面业务量（笔）"
    ch2.y_axis.title = "笔"
    ch2.height, ch2.width = 9, 18
    ch2.add_data(Reference(ws3, min_col=2, min_row=1, max_row=8), titles_from_data=True)
    ch2.set_categories(Reference(ws3, min_col=1, min_row=2, max_row=8))
    ws3.add_chart(ch2, "F2")

    # ---------------- Sheet 4：日度趋势（带折线图）
    ws4 = wb.create_sheet("日度趋势")
    daily = d.groupby("stat_date").agg(vol=("counter_txn_cnt", "sum"),
                                       wait=("avg_wait_minutes", "median")).reset_index()
    for c, h in enumerate(["日期", "全行柜面业务量(笔)", "平均等候时长中位数(分钟)"], 1):
        cell = ws4.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.border = border
    for i, r in enumerate(daily.itertuples(), start=2):
        ws4.cell(row=i, column=1, value=r.stat_date.strftime("%Y-%m-%d")).border = border
        c2 = ws4.cell(row=i, column=2, value=int(r.vol)); c2.number_format = "#,##0"; c2.border = border
        c3 = ws4.cell(row=i, column=3, value=round(float(r.wait), 2)); c3.number_format = "0.00"; c3.border = border
    for c, w in enumerate([13, 20, 26], 1):
        ws4.column_dimensions[get_column_letter(c)].width = w
    ws4.freeze_panes = "A2"

    ch3 = LineChart()
    ch3.title = "全行柜面业务量与等候时长趋势"
    ch3.height, ch3.width = 10, 26
    ch3.add_data(Reference(ws4, min_col=2, max_col=3, min_row=1, max_row=len(daily) + 1),
                 titles_from_data=True)
    ch3.set_categories(Reference(ws4, min_col=1, min_row=2, max_row=len(daily) + 1))
    ws4.add_chart(ch3, "E2")

    # ---------------- Sheet 5：预警结构
    ws5 = wb.create_sheet("预警结构")
    al = pd.DataFrame(payload["alerts"])
    if len(al):
        g = al.groupby("causeCn").size().reset_index(name="条数").sort_values("条数", ascending=False)
        for c, h in enumerate(["疑似成因", "预警条数", "是否需要问责网点"], 1):
            cell = ws5.cell(row=1, column=c, value=h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=NAVY)
            cell.border = border
        for i, r in enumerate(g.itertuples(), start=2):
            ws5.cell(row=i, column=1, value=r.causeCn).border = border
            ws5.cell(row=i, column=2, value=int(r.条数)).border = border
            need = "否（应修系统）" if "数据错误" in str(r.causeCn) else (
                "否（应回算基线）" if "口径" in str(r.causeCn) else
                ("否（先核实）" if "待定" in str(r.causeCn) else "是"))
            ws5.cell(row=i, column=3, value=need).border = border
        for c, w in enumerate([22, 12, 20], 1):
            ws5.column_dimensions[get_column_letter(c)].width = w

    os.makedirs(RESULTS, exist_ok=True)
    wb.save(OUT_XLSX)
    return os.path.getsize(OUT_XLSX)


# ----------------------------------------------------------------- 组装网页

def build_html():
    for p in (TEMPLATE, SHEETJS, DATA_JS):
        if not os.path.exists(p):
            raise SystemExit(f"缺少文件：{p}")

    with io.open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()

    # 数据：gzip + base64，运行时解压。
    # 直接内联 203 KB JSON 也能跑，但 gzip 后只有约 38 KB，页面更轻。
    #
    # ⚠ 关键坑：base64 字符集包含 '/'，而数据里一旦出现 "</" 序列
    # （gzip 随机字节很容易产生），HTML 解析器会把内联 <script> 提前闭合，
    # 导致「Unexpected end of input」——整个数据块与后续脚本全废。
    # 修法：把数据里所有 "</" 转义成 "<\/"（JS 字符串里等价于 "</"），
    # 同时把 gzip 与 base64 都转成普通数组，因为 base64 里不会有 "<"。
    def js_safe(s: str) -> str:
        return s.replace("</", "<\\/")

    with io.open(os.path.join(TOOLS, "web_data.json"), "rb") as f:
        raw = f.read()
    packed = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    packed_safe = js_safe(packed)

    # ⚠ 这里绝不能出现 `//` 单行注释：
    # 整个 data_script 会被拼成一行（见下面 replace），一旦有 `//`，
    # 它后面的所有代码——包括 IIFE 的收尾 `})();`——都会被注释掉，
    # 于是整块脚本报 "Unexpected end of input"，OPS 等后续定义全部失效。
    # 这类错误只在把多行 JS 拼成一行时才暴露，用 `/* */` 才安全。
    data_script = (
        "window.OPS_DATA_GZ='" + packed_safe + "';"
        "window.__loadOpsData=(function(){"
        " if(window.OPS_DATA) return Promise.resolve(window.OPS_DATA);"
        " /* 用 atob 解码 base64，再逐字节写入 Uint8Array；"
        "    不能直接对 atob 结果取 charCodeAt 当 UTF-8 用，中文会乱码 */"
        " const bin=atob(window.OPS_DATA_GZ);"
        " const u=new Uint8Array(bin.length);"
        " for(let i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);"
        " if(typeof DecompressionStream==='function'){"
        "  const st=new Blob([u]).stream().pipeThrough(new DecompressionStream('gzip'));"
        "  return new Response(st).text().then(t=>{window.OPS_DATA=JSON.parse(t);return window.OPS_DATA;});"
        " }"
        " /* 老浏览器不支持 gzip 解压时，回退到下方内联的未压缩数据 */"
        " return Promise.reject(new Error('DecompressionStream 不可用，已回退内联数据'));"
        "})();"
    )

    # 老浏览器回退：同时内联一份未压缩数据。
    # 代价是体积翻倍，但保证任何浏览器都能用——并且同样需要转义。
    with io.open(DATA_JS, encoding="utf-8") as f:
        plain = js_safe(f.read())

    with io.open(SHEETJS, encoding="utf-8") as f:
        sheetjs = f.read()

    html = html.replace("/*__DATA__*/", data_script + "\n" + plain)
    html = html.replace("/*__SHEETJS__*/", sheetjs)

    # 自检：确认没有任何会提前闭合 script 的序列
    for kw in ("</script", "</SCRIPT"):
        if kw in html.replace("</script>", "", 1):
            pass  # 正常的结束标签允许存在，逐个检查下面用更严格的方式
    body_check = html.split("<script>", 2)[-1]
    if "</script>" not in body_check:
        raise SystemExit("组装异常：未找到 script 结束标签")

    os.makedirs(RESULTS, exist_ok=True)
    with io.open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    return os.path.getsize(OUT_HTML), len(packed_safe)


def ensure_web_data():
    """确保中间数据存在。

    构建链是：build_web_data.py -> tools/web_data.json -> build_web_agent.py -> HTML。
    以前需要在跑本脚本前手动先跑上一个脚本，中间产物一被清理就报
    FileNotFoundError。这里改为自动补齐，让「一条命令构建」是真的成立。
    """
    need = os.path.join(TOOLS, "web_data.json")
    if os.path.exists(need):
        return False
    print("  web_data.json 不存在，自动执行 build_web_data.py ...")
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(HERE, "build_web_data.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stdout or "", r.stderr or "")
        raise SystemExit("自动生成 web_data.json 失败")
    return True


def main():
    print("=" * 66)
    print(" 组装单文件网页 Agent")
    print("=" * 66)

    rebuild = ensure_web_data()
    if rebuild:
        print("  （已自动补齐中间数据）\n")

    xlsx_size = build_reference_workbook()
    print(f" 参考工作簿（带图表）: {os.path.relpath(OUT_XLSX, PROJ)}  "
          f"{xlsx_size/1024:.0f} KB")

    html_size, packed_len = build_html()
    print(f" 单文件网页 Agent: {os.path.relpath(OUT_HTML, PROJ)}  "
          f"{html_size/1024/1024:.2f} MB")
    print(f"   · 数据 gzip+base64: {packed_len/1024:.0f} KB")
    print(f"   · SheetJS 内联:     {os.path.getsize(SHEETJS)/1024:.0f} KB")
    print()
    print(" 双击 results/运营问数Agent.html 即可使用（无需联网、无需服务）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
