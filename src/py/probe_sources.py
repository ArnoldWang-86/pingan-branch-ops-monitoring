# -*- coding: utf-8 -*-
"""探测可用的 MySQL/MariaDB 便携版下载源，并实测速度。

为什么单独写探测脚本：本机直连官方 CDN 时 HEAD 返回 200 但实际下载 0 字节
（被掐断），而国内镜像多数只镜像 Linux 的 apt/yum 包。
所以需要一个能实测「真实吞吐」而不是只看 HTTP 状态码的工具。

用法： python probe_sources.py            # 只测速，不下载
      python probe_sources.py --best    # 测速后自动下载最快的一个
"""
from __future__ import annotations

import argparse
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

import time

CANDIDATES = [
    ("MySQL 8.4.0 cdn", "https://cdn.mysql.com//Downloads/MySQL-8.4/mysql-8.4.0-winx64.zip"),
    ("MySQL 8.0.40 cdn", "https://cdn.mysql.com//Downloads/MySQL-8.0/mysql-8.0.40-winx64.zip"),
    ("MariaDB 11.4.4 archive", "https://archive.mariadb.org/mariadb-11.4.4/winx64-packages/mariadb-11.4.4-winx64.zip"),
    ("MariaDB tuna", "https://mirrors.tuna.tsinghua.edu.cn/mariadb/mariadb-11.4.4/winx64-packages/mariadb-11.4.4-winx64.zip"),
    ("MariaDB ustc", "https://mirrors.ustc.edu.cn/mariadb/mariadb-11.4.4/winx64-packages/mariadb-11.4.4-winx64.zip"),
    ("MariaDB nju", "https://mirrors.nju.edu.cn/mariadb/mariadb-11.4.4/winx64-packages/mariadb-11.4.4-winx64.zip"),
    ("MariaDB tencent", "https://mirrors.cloud.tencent.com/mariadb/mariadb-11.4.4/winx64-packages/mariadb-11.4.4-winx64.zip"),
    ("MariaDB aliyun", "https://mirrors.aliyun.com/mariadb/mariadb-11.4.4/winx64-packages/mariadb-11.4.4-winx64.zip"),
    ("MariaDB huawei", "https://mirrors.huaweicloud.com/mariadb/mariadb-11.4.4/winx64-packages/mariadb-11.4.4-winx64.zip"),
    ("MariaDB 10.11 tuna", "https://mirrors.tuna.tsinghua.edu.cn/mariadb/mariadb-10.11.10/winx64-packages/mariadb-10.11.10-winx64.zip"),
]

PROBE_SECONDS = 12
MIN_ACCEPTABLE_KBPS = 100


def probe(name, url, seconds=PROBE_SECONDS):
    """实测真实吞吐（不是只看状态码）。返回 (状态, KB/s, 试探字节数)。"""
    import requests
    try:
        t0 = time.time()
        got = 0
        with requests.get(url, stream=True, timeout=20) as r:
            if r.status_code not in (200, 206):
                return f"HTTP {r.status_code}", 0, 0
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                got += len(chunk)
                if time.time() - t0 >= seconds:
                    break
        el = max(time.time() - t0, 0.01)
        return "OK", got / 1024 / el, got
    except Exception as e:                                   # noqa: BLE001
        return type(e).__name__, 0, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--best", action="store_true", help="下载最快的一个源")
    ap.add_argument("--seconds", type=int, default=PROBE_SECONDS)
    args = ap.parse_args()

    print("=" * 74)
    print(" 探测 MySQL / MariaDB 便携版下载源（实测真实吞吐）")
    print("=" * 74)
    results = []
    for name, url in CANDIDATES:
        status, kbps, got = probe(name, url, args.seconds)
        flag = "可用" if kbps >= MIN_ACCEPTABLE_KBPS else ("过慢" if kbps > 0 else "不可用")
        print(f"  [{flag:6s}] {name:24s} {status:16s} {kbps:8.0f} KB/s  ({got/1024/1024:.1f} MB / {args.seconds}s)")
        results.append({"name": name, "url": url, "status": status,
                        "kbps": kbps, "ok": kbps >= MIN_ACCEPTABLE_KBPS})

    good = sorted([r for r in results if r["ok"]], key=lambda r: -r["kbps"])
    print("-" * 74)
    if not good:
        print("结论：没有可用的快速源。")
        print("建议改用执行引擎方案 B（SQLite 适配层，无需下载）。")
        return 1

    print("可用源（按速度排序）：")
    for r in good[:5]:
        print(f"  {r['kbps']:8.0f} KB/s  {r['name']}")

    if args.best:
        best = good[0]
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           os.path.basename(best["url"]))
        print(f"\n开始下载最快源: {best['name']}\n  -> {out}")
        import requests
        with requests.get(best["url"], stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            t0 = time.time()
            with open(out, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if done % (20 * 1024 * 1024) < 1024 * 1024:
                        el = time.time() - t0
                        print(f"  {done/1024/1024:6.1f} MB / {total/1024/1024:.1f} MB "
                              f"({done/max(total,1)*100:5.1f}%)  {done/1024/max(el,0.1):7.0f} KB/s",
                              flush=True)
        print(f"下载完成: {out}  ({os.path.getsize(out)/1024/1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
