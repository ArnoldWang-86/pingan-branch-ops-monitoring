# -*- coding: utf-8 -*-
"""在间歇性网络下启用 GitHub Pages。

背景
----
本机到 GitHub 的 TLS 通道是**间歇可用**的：
同一分钟内可能一半请求成功、一半抛 SSLEOFError。
所以不能用「一次调用 + 少量重试」的做法，而要：
  ① 每次请求独立重试，指数退避；
  ② 整个流程循环多轮，直到拿到确定结果；
  ③ 每轮之间留间隔，等一个稳定窗口。

启用 Pages 的用途
----------------
GitHub 不渲染 HTML——点开 `results/运营问数Agent.html` 只会看到源码。
而本项目的两个核心交付物（Text-to-Excel Agent、交互式看板）
都需要「点开就能用」。启用 Pages 后可以直接在线打开。

用法： python enable_pages.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOK = os.environ.get("GH_TOK", "").strip()
OWNER, REPO = "ArnoldWang-86", "pingan-branch-ops-monitoring"
H = {"Authorization": "token " + TOK,
     "Accept": "application/vnd.github+json",
     "User-Agent": "ops-pages"}
API = "https://api.github.com"


def req(method, path, payload=None):
    """单次请求 + 退避重试，返回 Response 或 None。"""
    for i in range(6):
        try:
            kw = {"headers": dict(H), "timeout": 40}
            if payload is not None:
                kw["data"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                kw["headers"] = dict(H, **{"Content-Type": "application/json"})
            return requests.request(method, API + path, **kw)
        except Exception:                                     # noqa: BLE001
            time.sleep(2 + 2 * i)
    return None


def main():
    if not TOK:
        print("缺少 GH_TOK")
        return 1

    print("=" * 66)
    print(" 启用 GitHub Pages（间歇网络下循环重试）")
    print("=" * 66)

    html_url = f"https://{OWNER.lower()}.github.io/{REPO}/"
    for rnd in range(1, 13):
        print(f"\n--- 第 {rnd} 轮 ---")

        # 1) 查询现状
        r = req("GET", f"/repos/{OWNER}/{REPO}/pages")
        if r is None:
            print("  查询失败（网络），等待 8 秒后重试")
            time.sleep(8)
            continue

        if r.status_code == 200:
            d = r.json()
            print(f"  [已存在] {d.get('html_url')}")
            print(f"    状态: {d.get('status')}  分支: {d.get('source', {}).get('branch')}")
            if d.get("status") in ("built", "building"):
                print(f"\n  完成。在线地址：\n    {html_url}")
                return 0
            time.sleep(10)
            continue

        print(f"  查询返回 HTTP {r.status_code}，尝试创建 ...")
        r2 = req("POST", f"/repos/{OWNER}/{REPO}/pages",
                 {"source": {"branch": "main", "path": "/"}})
        if r2 is None:
            print("  创建失败（网络），等待 8 秒后重试")
            time.sleep(8)
            continue

        print(f"  创建返回 HTTP {r2.status_code}")
        if r2.status_code in (201, 202, 204):
            print("  [OK] Pages 已创建，等待构建 ...")
        else:
            body = (r2.text or "")[:260]
            print("  " + body)
            # 409 = 已存在；422 多为已启用或权限问题
            if r2.status_code not in (409, 422):
                time.sleep(8)
                continue

        # 轮询构建状态
        for _ in range(6):
            time.sleep(12)
            r3 = req("GET", f"/repos/{OWNER}/{REPO}/pages")
            if r3 is None or r3.status_code != 200:
                continue
            d = r3.json()
            print(f"    构建状态: {d.get('status')}")
            if d.get("status") == "built":
                print(f"\n  [OK] Pages 构建完成")
                print(f"    {d.get('html_url') or html_url}")
                return 0
        time.sleep(8)

    print("\n  多轮重试后仍未确认成功（网络持续不稳定）")
    print(f"  可稍后手动访问 {html_url} 验证，或运行本脚本重试。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
