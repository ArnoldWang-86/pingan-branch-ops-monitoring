# -*- coding: utf-8 -*-
"""把本地提交推送到 GitHub（走 Git Data API，支持新增/修改/删除）。

为什么不能用 git push
---------------------
本机 `github.com:443` 的传输通道不通（git push 永远报
"Failed to connect to github.com port 443"），只有 `api.github.com` 可用。
所以推送改为：blob → tree → commit → 更新 ref。

设计要点（都是踩过坑之后的结论）
--------------------------------
1. **以本地 HEAD 的树为准，而不是 `git ls-files`。**
   早期版本用 `git ls-files` 取清单，而它不含已删除的文件，
   导致「本地删了、远端还留着」——实测 `docs/~$方法说明书.docx`
   从索引移除并推送后，远端依然存在。

2. **逐个比对 blob SHA，只上传真正变化的文件。**
   git 的 blob SHA 就是内容哈希，直接对比即可判断文件是否变化，
   不必重传那两个几 MB 的 HTML。
   这解决了早期版本「把 56 个文件全部重传」的问题。

3. **用 base_tree 复用远端未变更的条目**，只 patch 差异部分。

用法：
  python push_now.py           # 推荐：自动取令牌后调用本脚本
  python incremental_push.py   # 已设好 GH_TOK 时直接用
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

GIT_CANDIDATES = [
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Git\cmd\git.exe"),
]
H = {"Authorization": "token " + TOK,
     "Accept": "application/vnd.github+json",
     "User-Agent": "ops-push"}
API = "https://api.github.com"


def find_git() -> str:
    for p in GIT_CANDIDATES:
        if os.path.exists(p):
            return p
    return "git"


GIT = find_git()


def git(*args, check=True):
    subprocess.run([GIT, "config", "core.quotepath", "false"], capture_output=True)
    r = subprocess.run([GIT] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr[:200]}")
    return r.stdout.strip()


def api(method, path, payload=None, tries=6):
    """间歇性网络：单请求也带退避重试。"""
    last = None
    for i in range(tries):
        try:
            kw = {"headers": dict(H), "timeout": 60}
            if payload is not None:
                kw["data"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                kw["headers"] = dict(H, **{"Content-Type": "application/json"})
            return requests.request(method, API + path, **kw)
        except Exception as e:                                # noqa: BLE001
            last = type(e).__name__
            time.sleep(2 + 2 * i)
    print(f"    [网络失败] {method} {path} ({last})")
    return None


def local_tree_entries():
    """取本地 HEAD 的完整文件清单（路径 -> blob SHA）。

    用 `git ls-tree -r HEAD` 而不是 `git ls-files`：
    前者反映的是**提交内容**，包含删除结果；后者只看工作区现存文件。
    """
    out = git("ls-tree", "-r", "HEAD")
    entries = {}
    for line in out.split("\n"):
        if not line.strip():
            continue
        # 形如：100644 blob <sha>\t<path>
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            entries[path] = parts[2]
    return entries


def remote_tree_entries(tree_sha):
    r = api("GET", f"/repos/{OWNER}/{REPO}/git/trees/{tree_sha}?recursive=1")
    if r is None or r.status_code != 200:
        return None
    return {x["path"]: x["sha"] for x in r.json().get("tree", [])
            if x["type"] == "blob"}


def push_once():
    """返回 0 成功 / 1 可重试 / 2 永久失败。"""
    local_head = git("rev-parse", "HEAD")
    print(f"本地 HEAD : {local_head[:12]}")

    r = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    if r is None:
        return 1
    if r.status_code != 200:
        print(f"  读取远端 ref 失败 HTTP {r.status_code}: {r.text[:160]}")
        return 2
    remote_head = r.json()["object"]["sha"]
    print(f"远端 HEAD : {remote_head[:12]}")

    if local_head == remote_head:
        print("\n两者一致，无需推送。")
        return 0

    local = local_tree_entries()
    print(f"本地提交内文件: {len(local)}")

    # 远端当前树
    r = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{remote_head}")
    if r is None:
        return 1
    remote_tree_sha = r.json()["tree"]["sha"]
    remote = remote_tree_entries(remote_tree_sha)
    if remote is None:
        return 1
    print(f"远端当前文件:   {len(remote)}")

    to_add = {p: s for p, s in local.items() if remote.get(p) != s}
    to_del = [p for p in remote if p not in local]
    print(f"\n需上传: {len(to_add)} 个    需删除: {len(to_del)} 个")
    for p in list(to_add)[:15]:
        print("  + " + p)
    if len(to_add) > 15:
        print(f"  ... 另 {len(to_add)-15} 个")
    for p in to_del:
        print("  - " + p)

    if not to_add and not to_del:
        # 文件内容与远端完全一致，只是提交历史不同：
        # 直接复用本地提交的树，不必再创建 tree。
        print("\n文件内容完全一致，只是提交历史不同。直接以本地提交为准更新 ref。")
        r = api("GET", f"/repos/{OWNER}/{REPO}/git/commits/{local_head}")
        if r is None or r.status_code != 200:
            return 1
        tree_sha = r.json()["tree"]["sha"]
    else:
        print("\n创建 blob ...")
        items = []
        for i, (p, _sha) in enumerate(sorted(to_add.items()), 1):
            if not os.path.exists(p):
                print(f"  [跳过] 工作区无此文件: {p}")
                continue
            with open(p, "rb") as fh:
                content = fh.read()
            rb = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                     {"content": base64.b64encode(content).decode("ascii"),
                      "encoding": "base64"})
            if rb is None or rb.status_code != 201:
                print(f"  [ERR] {p}: HTTP {rb.status_code if rb else 'N/A'}")
                return 1
            items.append({"path": p, "mode": "100644", "type": "blob",
                          "sha": rb.json()["sha"]})
            if i % 10 == 0 or i == len(to_add):
                print(f"  {i}/{len(to_add)}")
        # 删除：GitHub 约定 sha 为 null 表示移除该路径
        for p in to_del:
            items.append({"path": p, "mode": "100644", "type": "blob", "sha": None})

        print("\n创建 tree ...")
        rt = api("POST", f"/repos/{OWNER}/{REPO}/git/trees",
                 {"base_tree": remote_tree_sha, "tree": items})
        if rt is None or rt.status_code != 201:
            print(f"  [ERR] HTTP {rt.status_code if rt else 'N/A'}")
            return 1
        tree_sha = rt.json()["sha"]
        print(f"  tree {tree_sha[:12]}")

    msg = git("log", "-1", "--pretty=%B") or "update"
    rc = api("POST", f"/repos/{OWNER}/{REPO}/git/commits",
             {"message": msg, "tree": tree_sha, "parents": [remote_head]})
    if rc is None or rc.status_code != 201:
        print(f"  [ERR] 创建 commit 失败 HTTP {rc.status_code if rc else 'N/A'}")
        return 1
    new_commit = rc.json()["sha"]
    print(f"  commit {new_commit[:12]}")

    rp = api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
             {"sha": new_commit, "force": False})
    if rp is None or rp.status_code != 200:
        print(f"  [ERR] 更新 ref 失败 HTTP {rp.status_code if rp else 'N/A'}")
        return 1
    print(f"  {BRANCH} -> {new_commit[:12]}")

    # 让本地引用跟上，避免下次又按旧基准比对
    subprocess.run([GIT, "update-ref", f"refs/remotes/origin/{BRANCH}", new_commit],
                   capture_output=True)
    print("\n推送完成。")
    print(f"  https://github.com/{OWNER}/{REPO}")
    return 0


def main():
    if not TOK:
        print("缺少 GH_TOK 环境变量（可用 push_now.py 自动取）")
        return 1
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(root)
    print("=" * 66)
    print(" 推送到 GitHub（Git Data API，间歇网络下循环重试）")
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
    print("\n多轮重试后仍未成功，请稍后再运行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
