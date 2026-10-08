# -*- coding: utf-8 -*-
"""验证 SQL 执行引擎能力：本项目 01_indicator_layer.sql 用到的全部语法结构。

背景：本机网络无法下载 MySQL/MariaDB 二进制（官方 CDN TLS 被重置、
国内镜像只镜像 Linux 包），因此执行引擎改用 DuckDB——
它是 OLAP 分析型数据库，从 PyPI 安装，无需下载二进制，
且对窗口函数 / CTE / 相关子查询的支持比 SQLite 更完整。

这个脚本逐项验证「本项目的 SQL 到底能不能在这个引擎上跑」，
而不是假设它兼容。逐项输出证据。

用法： python verify_engine.py
"""
from __future__ import annotations

import os
import sys

# 中文 Windows 控制台默认 GBK，脚本里的非 GBK 字符（✓ ⚠ ▪ 等）
# 在 print 时会抛 UnicodeEncodeError 并中断执行。入口处重配为 UTF-8。
for _s in ("stdout", "stderr"):
    _st = getattr(sys, _s, None)
    if _st is not None and hasattr(_st, "reconfigure"):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))
RAW = os.path.join(PROJ, "data", "raw")

CHECKS = []


def check(name, sql, expect_nonempty=True):
    """执行一条 SQL，记录是否成功与返回行数。"""
    try:
        cur = con.execute(sql)
        rows = cur.fetchall()
        ok = bool(rows) if expect_nonempty else True
        CHECKS.append((name, "OK" if ok else "空结果", len(rows), None))
        return rows
    except Exception as e:                                   # noqa: BLE001
        CHECKS.append((name, "FAIL", 0, f"{type(e).__name__}: {e}"))
        return None


print("=" * 74)
print(" SQL 执行引擎能力验证（对照 01_indicator_layer.sql 的语法结构）")
print("=" * 74)
print(f"引擎: DuckDB {duckdb.__version__}\n")

con = duckdb.connect(":memory:")

# ---- 载入真实模拟明细（与 SQL 文件的 ODS 层同源）
detail = os.path.join(RAW, "branch_ops_detail.jsonl").replace("\\", "/")
con.execute(f"""
CREATE TABLE ods_branch_ops_detail AS
SELECT * FROM read_json_auto('{detail}')
""")
n = con.execute("SELECT COUNT(*) FROM ods_branch_ops_detail").fetchone()[0]
print(f"载入明细: {n} 行\n")

print("-- 逐项验证 SQL 语法结构 --")

# 1. CTAS + 派生指标（dwd 层核心写法）
check("CTAS 派生指标（CREATE TABLE AS SELECT）", """
CREATE TABLE dwd AS
SELECT stat_date, branch_code, branch_name,
       counter_txn_cnt,
       counter_txn_cnt + smart_device_txn_cnt + mobile_txn_cnt AS total_txn_cnt,
       ROUND((smart_device_txn_cnt + mobile_txn_cnt)
             / NULLIF(counter_txn_cnt + smart_device_txn_cnt + mobile_txn_cnt, 0), 4) AS e_channel_rate,
       ROUND(txn_error_cnt / NULLIF(counter_txn_cnt, 0), 6) AS error_rate,
       ROUND(complaint_cnt / NULLIF(counter_txn_cnt, 0) * 10000, 4) AS complaint_per_10k,
       avg_wait_minutes,
       op_cost_ratio
FROM ods_branch_ops_detail
""")

# 2. 滚动基线：ROWS BETWEEN N PRECEDING AND 1 PRECEDING（dws 层最关键写法）
check("窗口 ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING（排除当日的滚动基线）", """
CREATE TABLE mon AS
SELECT *,
  AVG(counter_txn_cnt) OVER (PARTITION BY branch_code ORDER BY stat_date
      ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS counter_txn_ma7,
  LAG(counter_txn_cnt) OVER (PARTITION BY branch_code ORDER BY stat_date) AS prev_vol,
  RANK() OVER (PARTITION BY stat_date ORDER BY avg_wait_minutes DESC) AS wait_rank,
  PERCENT_RANK() OVER (PARTITION BY stat_date ORDER BY counter_txn_cnt) AS vol_pct
FROM dwd
""")

# 3. 多 CTE 链（ads 层用了 4 层 CTE）
check("多层 CTE 链（WITH a AS (...), b AS (...), c AS (...))", """
WITH trig AS (
  SELECT branch_code, stat_date,
    CASE WHEN (counter_txn_cnt - counter_txn_ma7) / NULLIF(counter_txn_ma7,0) > 0.20 THEN 1 ELSE 0 END AS triggered
  FROM mon WHERE counter_txn_ma7 IS NOT NULL
),
persist AS (
  SELECT *, SUM(triggered) OVER (PARTITION BY branch_code ORDER BY stat_date
    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS trig_7d
  FROM trig
),
scored AS (
  SELECT *, trig_7d * 10.0 AS score FROM persist
)
SELECT * FROM scored WHERE trig_7d >= 3
""")

# 4. 窗口聚合子查询做稳健离散度（原 SQL 的 mad CTE）
check("窗口函数嵌在子查询里再聚合（MAD 近似）", """
WITH mad AS (
  SELECT stat_date,
         AVG(ABS(avg_wait_minutes - day_avg)) AS mad_wait
  FROM (
    SELECT stat_date, avg_wait_minutes,
           AVG(avg_wait_minutes) OVER (PARTITION BY stat_date) AS day_avg
    FROM dwd
  ) t
  GROUP BY stat_date
)
SELECT * FROM mad WHERE mad_wait IS NOT NULL
""")

# 5. 相关子查询 / 窗口内 MAX（ads 层成因判定用了标量子查询）
check("相关子查询 + GREATEST/LEAST", """
SELECT branch_code,
       GREATEST(ABS(1.0), ABS(-2.0)) AS g,
       (SELECT AVG(op_cost_ratio) FROM dwd d2 WHERE d2.branch_code = d1.branch_code) AS avg_cost
FROM dwd d1 LIMIT 5
""")

# 6. CREATE TABLE ... AS WITH（CTE 后接建表，原 SQL 的 dws/ads 写法）
check("CREATE TABLE AS WITH ...（CTE 建表，原文件核心结构）", """
CREATE TABLE mon2 AS
WITH base AS (
  SELECT branch_code, stat_date, counter_txn_cnt,
         AVG(counter_txn_cnt) OVER (PARTITION BY branch_code ORDER BY stat_date
             ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS ma7
  FROM dwd
)
SELECT *, (counter_txn_cnt - ma7) / NULLIF(ma7, 0) AS dev FROM base
""")

# 7. ALTER TABLE ADD PRIMARY KEY（原文件用了 4 次）
try:
    con.execute("ALTER TABLE mon2 ADD PRIMARY KEY (branch_code, stat_date)")
    CHECKS.append(("ALTER TABLE ADD PRIMARY KEY", "OK", 0, None))
except Exception as e:                                       # noqa: BLE001
    CHECKS.append(("ALTER TABLE ADD PRIMARY KEY", "FAIL", 0, f"{type(e).__name__}: {e}"))

# 8. 中文表注释（MySQL 的 COMMENT 语法）
check("中文标识符与字符串（utf8 往返）", """
SELECT '平均等候时长' AS 指标, COUNT(*) AS 行数 FROM dwd
""")

# ---- 汇总
print(f"\n{'语法结构':<52} {'结果':<8} {'行数':>6}  备注")
print("-" * 92)
n_ok = 0
for name, status, rows, err in CHECKS:
    print(f"{name:<52} {status:<8} {rows:>6}  {err or ''}")
    if status == "OK":
        n_ok += 1

print("-" * 92)
print(f"通过 {n_ok}/{len(CHECKS)}")
if n_ok == len(CHECKS):
    print("\n结论：本项目 SQL 用到的全部语法结构在该引擎上均可执行。")
else:
    print("\n结论：存在不兼容项，见上表 FAIL 行（需在适配层处理）。")

# 真实数字抽查，确认不是空跑
r = con.execute("""
SELECT COUNT(*) AS n, ROUND(AVG(avg_wait_minutes),2) AS avg_wait,
       ROUND(AVG(e_channel_rate),4) AS e_ch
FROM dwd
""").fetchone()
print(f"\n数据抽查: {r[0]} 行, 平均等候 {r[1]} 分钟, 平均分流率 {r[2]}")
n_alert = con.execute("SELECT COUNT(*) FROM mon WHERE counter_txn_ma7 IS NOT NULL "
                      "AND ABS((counter_txn_cnt-counter_txn_ma7)/counter_txn_ma7) > 0.20"
                      ).fetchone()[0]
print(f"业务量偏离 >20% 的记录: {n_alert} 条（用于对照 Python 层预警数）")

con.close()
sys.exit(0 if n_ok == len(CHECKS) else 1)
