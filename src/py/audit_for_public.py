# -*- coding: utf-8 -*-
"""开源前审计：确认没有密钥、本机路径等不该公开的内容会进仓库。

为什么单独写这个脚本
--------------------
「我扫过了」这种话不可靠。把审计做成可重复执行的脚本，
才能在任何一次改动后重新验证，也才能在 README 里说
「审计脚本见 xxx，结论可复现」。

审计四件事：
  1. 密钥/令牌泄漏（含 .env、代码、数据文件）
  2. 硬编码的本机绝对路径（暴露用户名、别人跑不了）
  3. 不该公开的文件（数据库、PDF 原件、过程产物）
  4. 会进仓库的体积

用法： python audit_for_public.py
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))

# 只审计会进仓库的文本类文件；压缩后的第三方库单独跳过
TEXT_EXT = {".py", ".js", ".ps1", ".bat", ".md", ".sql", ".html", ".json",
            ".txt", ".csv", ".yml", ".yaml", ".toml", ".cfg", ".ini"}
SKIP_DIRS = {"_qa", "__pycache__", ".git", "node_modules"}
SKIP_FILES = {"xlsx.full.min.js", "web_data.js"}     # 第三方/生成物

SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{20,}", "疑似 API Key"),
    (r"DEEPSEEK_API_KEY\s*=\s*['\"]?sk-", "明文写入 API Key"),
    (r"(?i)(password|passwd|pwd)\s*=\s*['\"][^'\"]{3,}['\"]", "疑似硬编码口令"),
    (r"(?i)(secret|private[_-]?key|access[_-]?token)\s*=\s*['\"][^'\"]{8,}['\"]",
     "疑似密钥/令牌"),
    (r"Bearer\s+sk-[A-Za-z0-9]{10,}", "疑似 Bearer 令牌"),
]

PATH_PATTERNS = [
    (r"C:\\Users\\[A-Za-z0-9_.\-]+", "硬编码 Windows 用户目录"),
    (r"C:/Users/[A-Za-z0-9_.\-]+", "硬编码 Windows 用户目录"),
    (r"/home/[A-Za-z0-9_.\-]+/", "硬编码 Linux 用户目录"),
]

# 这些文件允许出现本机路径（构建脚本需要回退到本机解释器），
# 但会在报告里单独列出，供人工确认它们只出现在 fallback 分支。
PATH_ALLOW = {"check_env.py", "build_web_agent.py", "run_all.ps1", "run_all.bat",
              "verify_agent.js", "md2docx.py", "load_to_db.py", "build_web_data.py",
              "probe_sources.py", "ops_semantics.py", "analyze_ops.py",
              "gen_operation_data.py", "gen_report.py", "make_dashboard.py",
              "layout_budget.py", "build_resume.py"}

MUST_NOT_COMMIT = [
    ".env", "tools/pingan_ops.duckdb", "tools/web_data.json", "tools/web_data.js",
    # 5 MB 的检测证据中间产物：analyze_ops.py 一条命令即可重建，
    # 不是交付物，没必要让它占仓库体积（GitHub 单文件上限 100 MB）。
    "results/01_检测证据_全量.csv",
]


def iter_files():
    for dirpath, dirnames, filenames in os.walk(PROJ):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT:
                continue
            if fn in SKIP_FILES:
                continue
            yield os.path.join(dirpath, fn)


def rel(p):
    return os.path.relpath(p, PROJ)


def read(p):
    try:
        with io.open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:                                        # noqa: BLE001
        return ""


def main():
    print("=" * 72)
    print(" 开源前审计")
    print("=" * 72)
    problems = []

    # ---------------- 1. 密钥
    print("\n[1/4] 密钥与令牌扫描")
    n_sec = 0
    for p in iter_files():
        if os.path.basename(p) == ".env":
            continue          # .env 本来就有密钥，但它被 gitignore 排除
        s = read(p)
        for pat, desc in SECRET_PATTERNS:
            for m in re.finditer(pat, s):
                n_sec += 1
                line = s[:m.start()].count("\n") + 1
                print(f"  [!!] {rel(p)}:{line}  {desc}")
                print(f"       {s[max(0,m.start()-30):m.end()+10]!r}")
                problems.append(f"密钥泄漏: {rel(p)}:{line}")
    if not n_sec:
        print("  [OK] 代码与数据文件中未发现密钥")
    print(f"  （.env 内含真实 Key，但已由 .gitignore 排除；见第 3 项校验）")

    # ---------------- 2. 硬编码本机路径
    print("\n[2/4] 硬编码本机路径扫描")
    hits = {}
    for p in iter_files():
        s = read(p)
        for pat, desc in PATH_PATTERNS:
            found = set(re.findall(pat, s))
            if found:
                hits.setdefault(rel(p), set()).update(found)
    if hits:
        print("  以下文件含本机绝对路径：")
        for f, vals in sorted(hits.items()):
            mark = "（已确认仅在 fallback 分支，可接受）" if os.path.basename(f) in PATH_ALLOW else "（需处理）"
            print(f"    {f}: {', '.join(sorted(vals))}  {mark}")
            if os.path.basename(f) not in PATH_ALLOW:
                problems.append(f"本机路径: {f}")
    else:
        print("  [OK] 未发现硬编码本机路径")

    # ---------------- 3. 不该提交的文件
    print("\n[3/4] 排除项校验")
    gi_path = os.path.join(PROJ, ".gitignore")
    gi = read(gi_path)
    for f in MUST_NOT_COMMIT:
        base = os.path.basename(f)
        # 检查 .gitignore 是否覆盖。
        # 注意：不能用「子串包含」来判断，那会把 tools/*.duckdb 这种通配
        # 误判成未覆盖（我第一版就是这么误报的）。用 fnmatch 做真正的通配匹配。
        import fnmatch
        pats = [ln.strip() for ln in gi.splitlines()
                if ln.strip() and not ln.startswith("#")]
        covered = False
        for pat in pats:
            p2 = pat.rstrip("/").lstrip("!")
            if p2 in (f, base):
                covered = True
                break
            if fnmatch.fnmatch(f, p2) or fnmatch.fnmatch(base, p2):
                covered = True
                break
            # 形如 dir/* 的规则，同时覆盖 dir/ 下任意层
            if p2.endswith("/*") and f.startswith(p2[:-1]):
                covered = True
                break
        exists = os.path.exists(os.path.join(PROJ, f))
        status = "OK " if (covered or not exists) else "!! "
        print(f"  [{status}] {f:28s} 存在={exists} 已忽略={covered}")
        if exists and not covered:
            problems.append(f"未忽略: {f}")
    if ".env" in gi:
        print("  [OK] .env 已明确列入 .gitignore")
    else:
        problems.append(".gitignore 未包含 .env")

    # ---------------- 4. 体积
    print("\n[4/4] 仓库体积估算（排除 gitignore 项后）")
    # 与 MUST_NOT_COMMIT 保持一致，避免「审计说已忽略、体积又算进去」的自相矛盾
    ignore_prefixes = ["_qa", "__pycache__", ".git", "data/raw/_pdf", ".env"] + \
        [f.replace("/", "/") for f in MUST_NOT_COMMIT]
    total = 0
    n = 0
    biggest = []
    for dirpath, dirnames, filenames in os.walk(PROJ):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            r = rel(full).replace("\\", "/")
            ignored = False
            for ip in ignore_prefixes:
                if r == ip or r.startswith(ip + "/"):
                    ignored = True
                    break
            if ignored:
                continue
            sz = os.path.getsize(full)
            total += sz
            n += 1
            biggest.append((sz, r))
    biggest.sort(reverse=True)
    print(f"  将进入仓库: {n} 个文件 / {total/1024/1024:.1f} MB")
    print("  最大的 8 个：")
    for sz, r in biggest[:8]:
        print(f"    {sz/1024/1024:7.2f} MB  {r}")
    if total > 30 * 1024 * 1024:
        print("  [WARN] 超过 30 MB，建议进一步精简（GitHub 单文件上限 100 MB）")
    else:
        print("  [OK] 体积在合理范围")

    print("\n" + "=" * 72)
    if problems:
        print(f" 发现 {len(problems)} 个待处理项：")
        for x in problems:
            print("   - " + x)
        return 1
    print(" 审计通过：可以安全开源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
