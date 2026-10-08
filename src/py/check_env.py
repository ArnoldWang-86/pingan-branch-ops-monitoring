# -*- coding: utf-8 -*-
"""环境自检：确认 SQL Agent 所需的全部依赖就位。

用法： python check_env.py
"""
import importlib
import os
import shutil
import subprocess
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


OK, FAIL, WARN = "OK  ", "FAIL", "WARN"


def line(status, name, detail=""):
    print(f"  [{status}] {name:22s} {detail}")


def check_python_pkg(mod, attr="__version__"):
    try:
        m = importlib.import_module(mod)
        v = getattr(m, attr, "")
        line(OK, mod, str(v))
        return True
    except Exception as e:                                   # noqa: BLE001
        line(FAIL, mod, str(e)[:70])
        return False


def check_db_engine(root):
    """检查 SQL 执行引擎。

    引擎选择的背景：原设计目标是 MySQL 8，但本机网络无法获取 MySQL/MariaDB
    二进制（官方 CDN 的 TLS 被重置、国内镜像只镜像 Linux 的 apt/yum 包、
    winget 需管理员提权），因此改用 DuckDB —— OLAP 分析型数据库，
    从 PyPI 安装、无需下载二进制，且窗口函数/CTE 支持完整。
    """
    ok = True
    try:
        import duckdb
        line(OK, "duckdb", duckdb.__version__)
    except ImportError as e:
        line(FAIL, "duckdb", str(e)[:70])
        return False

    db = os.path.join(root, "tools", "pingan_ops.duckdb")
    if os.path.exists(db):
        size_mb = os.path.getsize(db) / 1024 / 1024
        try:
            con = duckdb.connect(db, read_only=True)
            tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
            con.close()
            line(OK, "库文件 pingan_ops", f"{size_mb:.1f} MB / {len(tables)} 张表")
            expected = {"ods_branch_ops_detail", "dim_branch", "dwd_branch_ops_daily",
                        "dws_branch_ops_monitor", "ads_ops_alert"}
            missing = expected - set(tables)
            if missing:
                line(WARN, "五层表", f"缺少 {sorted(missing)}")
            else:
                line(OK, "五层表齐全", "ODS / DIM / DWD / DWS / ADS")
        except Exception as e:                               # noqa: BLE001
            line(FAIL, "库文件可读性", str(e)[:70])
            ok = False
    else:
        line(WARN, "库文件 pingan_ops", "尚未落库，请运行 load_to_db.py")

    # MySQL 仅作为「生产目标」说明，不作为就绪条件
    if shutil.which("mysql") or os.path.exists(
            os.path.join(root, "tools", "mysql-8.4.6-winx64", "bin", "mysqld.exe")):
        line(OK, "MySQL（可选）", "已安装")
    else:
        line(WARN, "MySQL（可选）",
             "未安装；SQL 以 MySQL 8 语法编写，本地用 DuckDB 执行验证")
    return ok


def check_dotenv(root):
    env = os.path.join(root, ".env")
    if not os.path.exists(env):
        line(FAIL, ".env", "不存在")
        return False
    try:
        from dotenv import dotenv_values
        v = dotenv_values(env)
        key = v.get("DEEPSEEK_API_KEY") or ""
        if key.startswith("sk-") and len(key) > 20:
            line(OK, ".env / API key", f"{key[:8]}****  (len={len(key)})")
        else:
            line(FAIL, ".env / API key", "格式异常")
            return False
        gi = os.path.join(root, ".gitignore")
        if os.path.exists(gi) and ".env" in open(gi, encoding="utf-8").read():
            line(OK, ".gitignore 排除 .env", "")
        else:
            line(WARN, ".gitignore", "未确认排除 .env")
        return True
    except Exception as e:                                   # noqa: BLE001
        line(FAIL, ".env 解析", str(e)[:70])
        return False


def check_api_reachable(base_url):
    """真调一次 API，确认 key 有效（这比只看格式可靠）。"""
    try:
        import requests
        r = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {os.environ.get('DEEPSEEK_API_KEY','')}",
                     "Content-Type": "application/json"},
            json={"model": "deepseek-chat",
                  "messages": [{"role": "user", "content": "回复两个字：就绪"}],
                  "max_tokens": 10},
            timeout=30,
        )
        if r.status_code == 200:
            txt = r.json()["choices"][0]["message"]["content"].strip()
            line(OK, "DeepSeek API", f"HTTP 200, 返回: {txt!r}")
            return True
        line(FAIL, "DeepSeek API", f"HTTP {r.status_code}: {r.text[:120]}")
        return False
    except Exception as e:                                   # noqa: BLE001
        line(FAIL, "DeepSeek API", str(e)[:90])
        return False


def main():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("=" * 66)
    print(" Text-to-Excel Agent 环境自检")
    print("=" * 66)
    print(f"项目根目录: {root}\n")

    print("-- Python 依赖 --")
    py_ok = all(check_python_pkg(m) for m in
                ["pymysql", "cryptography", "sqlalchemy", "dotenv",
                 "pytest", "requests", "pandas", "plotly"])

    print("\n-- SQL 执行引擎 --")
    db_ok = check_db_engine(root)

    print("\n-- 私有配置 --")
    env_ok = check_dotenv(root)
    if env_ok:
        from dotenv import dotenv_values
        vals = dotenv_values(os.path.join(root, ".env"))
        for k, v in vals.items():
            if k and v:
                os.environ.setdefault(k, v)
        api_ok = check_api_reachable(
            os.environ.get("DEEPSEEK_BASE_URL",
                           "https://api.deepseek.com/chat/completions"))
    else:
        api_ok = False

    print("\n" + "=" * 66)
    parts = [("Python 依赖", py_ok), ("SQL 执行引擎", db_ok),
             (".env 配置", env_ok), ("API 连通", api_ok)]
    for name, ok in parts:
        print(f"  {name:14s} {'就绪' if ok else '未就绪'}")
    all_ok = all(o for _, o in parts)
    print("=" * 66)
    print("总体：" + ("全部就绪，可以开始 Text-to-Excel Agent 开发"
                      if all_ok else "存在未就绪项，见上方 FAIL/WARN"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
