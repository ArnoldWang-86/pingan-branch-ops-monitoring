# -*- coding: utf-8 -*-
"""把本地领先于远端的提交推送到 GitHub（走 Git Data API）。

为什么需要它
------------
本机 `github.com:443` 的 push 通道不通（git push 永远报
"Failed to connect to github.com port 443"），但 `api.github.com` 可用。
所以推送只能走 Git Data API：blob → tree → commit → 更新 ref。

与 push_via_api.py 的分工
-------------------------
  · push_via_api.py  —— 首次全量推送（空仓库引导 + 全量 blob），保留作为完整参考
  · 本脚本           —— 日常增量推送：只上传本地 HEAD 与远端不一致的文件

它会自动判断「哪些文件变了」，因此日常改几个文件只上传那几个，
不必每次重传 54 个（尤其那两个几 MB 的 HTML）。

用法：
  python incremental_push.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOK = os.environ.get("GH_TOK", "").strip()
OWNER, REPO = "ArnoldWang-86", "pingan-branch-ops-monitoring"
BRANCH = "main"
GIT = r"C:\Program Files\Git\cmd\git.exe"
H = {"Authorization": "token " + TOK,
     "Accept": "application/vnd.github+json",
     "User-Agent": "ops-push"}
API = "https://api.github.com"


def git(*args, check=True):
    subprocess.run([GIT, "config", "core.quotepath", "false"], capture_output=True)
    r = subprocess.run([GIT] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr[:200]}")
    return r.stdout.strip()


def api(method, path, payload=None, tries=6):
    """间歇性网络：单请求也带退避重试。"""
    for i in range(tries):
        try:
            kw = {"headers": dict(H), "timeout": 60}
            if payload is not None:
                kw["data"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                kw["headers"] = dict(H, **{"Content-Type": "application/json"})
            return requests.request(method, API + path, **kw)
        except Exception:                                     # noqa: BLE001
            time.sleep(2 + 2 * i)
    return None


def push_once():
    """执行一次推送。返回 0 成功 / 1 需要重试 / 2 永久失败。"""
    local_head = git("rev-parse", "HEAD")
    print(f"本地 HEAD : {local_head[:12]}")

    # 远端当前 HEAD
    r = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    if r is None:
        return 1                    # 网络问题，可重试
    if r.status_code != 200:
        print(f"  读取远端 ref 失败 HTTP {r.status_code}: {r.text[:160]}")
        return 2
    remote_head = r.json()["object"]["sha"]
    print(f"远端 HEAD : {remote_head[:12]}")

    if local_head == remote_head:
        print("\n两者一致，无需推送。")
        return 0

    # 计算差异文件（本地相对远端）
    #
    # ⚠ 已知缺陷（务实取舍，不是疏忽）：
    # 正常用法是「本地提交 → 运行本脚本」。但提交之后，本地 HEAD 与远端 HEAD
    # 就已经分叉（远端还没有这个提交），于是 `git diff remote local` 会把
    # **所有文件**都报成差异，本脚本会把 56 个文件全部重传一遍——
    # 功能上正确（结果一样），但浪费带宽，尤其在两个几 MB 的 HTML 上。
    #
    # 想真正只传变更文件，必须在**提交之前**比对工作区，但那时又缺提交信息。
    # 正确解法是用 `git diff --name-only origin/main` 取「相对于远端基线」的变更，
    # 再单独取本地提交信息。鉴于本机到 GitHub 的通道间歇可用、重传代价可接受，
    # 这里保留现状并明确标注，而不是假装它做到了增量。
    changed = []
    out = git("diff", "--name-only", remote_head, local_head, check=False)
    if out:
        changed = [x for x in out.split("\n") if x.strip()]
    if not changed:
        print("\n提交不同但文件内容无差异（可能只是提交信息差异）。")
        changed = git("ls-files").split("\n")
        changed = [x for x in changed if x.strip()]

    print(f"\n需上传文件: {len(changed)} 个")
    for c in changed[:20]:
        print("  " + c)
    if len(changed) > 20:
        print(f"  ... 另 {len(changed) - 20} 个")

    # 远端已有的 tree，用来复用未变更的条目
    r = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{remote_head}")
    if r is None or r.status_code != 200:
        print("  无法读取远端提交")
        return 1
    base_tree_sha = r.json()["tree"]["sha"]

    # 为差异文件建 blob
    print("\n创建 blob ...")
    items = []
    for i, f in enumerate(changed, 1):
        if not os.path.exists(f):
            print(f"  跳过（已删除，本期未处理删除）: {f}")
            continue
        with open(f, "rb") as fh:
            content = fh.read()
        rb = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                 {"content": base64.b64encode(content).decode("ascii"),
                  "encoding": "base64"})
        if rb is None or rb.status_code != 201:
            print(f"  [ERR] {f}: HTTP {rb.status_code if rb else 'N/A'}")
            return 1
        items.append({"path": f.replace("\\", "/"), "mode": "100644",
                      "type": "blob", "sha": rb.json()["sha"]})
        if i % 10 == 0 or i == len(changed):
            print(f"  {i}/{len(changed)}")

    if not items:
        print("没有需要上传的内容。")
        return 0

    # 以远端树为基底打补丁，未变更文件自动保留
    print("\n创建 tree ...")
    rt = api("POST", f"/repos/{OWNER}/{REPO}/git/trees",
             {"base_tree": base_tree_sha, "tree": items})
    if rt is None or rt.status_code != 201:
        print(f"  [ERR] HTTP {rt.status_code if rt else 'N/A'}")
        return 1
    tree_sha = rt.json()["sha"]
    print(f"  tree {tree_sha[:12]}")

    # 提交
    msg = git("log", "-1", "--pretty=%B") or "update"
    rc = api("POST", f"/repos/{OWNER}/{REPO}/git/commits",
             {"message": msg, "tree": tree_sha, "parents": [remote_head]})
    if rc is None or rc.status_code != 201:
        print(f"  [ERR] HTTP {rc.status_code if rc else 'N/A'}")
        return 1
    new_commit = rc.json()["sha"]
    print(f"  commit {new_commit[:12]}")

    # 更新分支（不用 force，保证不覆盖别人的提交）
    rp = api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
             {"sha": new_commit, "force": False})
    if rp is None or rp.status_code != 200:
        print(f"  [ERR] 更新 ref 失败 HTTP {rp.status_code if rp else 'N/A'}: "
              f"{(rp.text[:200] if rp else '')}")
        return 1
    print(f"  {BRANCH} -> {new_commit[:12]}")

    # 让本地记录也跟上（否则下次 diff 基准不对）
    subprocess.run([GIT, "fetch", "origin", BRANCH], capture_output=True)
    print("\n推送完成。")
    print(f"  https://github.com/{OWNER}/{REPO}")
    return 0


def main():
    if not TOK:
        print("缺少 GH_TOK 环境变量")
        return 1

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(root)

    # 本机到 GitHub 的 TLS 通道间歇可用，整流程循环多轮等一个稳定窗口
    print("=" * 66)
    print(" 增量推送到 GitHub（间歇网络下循环重试）")
    print("=" * 66)
    for rnd in range(1, 11):
        print(f"\n--- 第 {rnd} 轮 ---")
        try:
            rc = push_once()
        except Exception as e:                                # noqa: BLE001
            print(f"  异常: {type(e).__name__}: {e}")
            rc = 1
        if rc == 0:
            return 0
        if rc == 2:
            return 1
        wait = min(20, 5 + 3 * rnd)
        print(f"  网络不稳定，{wait} 秒后重试 ...")
        time.sleep(wait)

    print("\n多轮重试后仍未成功（网络持续不稳定），请稍后再运行本脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
