# -*- coding: utf-8 -*-
"""通过 GitHub Git Data API 推送本地提交到远端仓库。

为什么不用 git push
-------------------
本机网络环境特殊：`github.com:443`（git 的 HTTPS 传输）不通，
`ssh.github.com:443` 虽能建连但认证握手会挂住。
唯一稳定可用的是 `api.github.com`（走 HTTPS 正常）。

所以改用 Git Data API 完成一次等价的推送：
  逐个创建 blob → 建 tree → 建 commit → 更新 ref
效果与 `git push` 一致：远端会得到一个真实的、可 clone 的提交，
只是提交对象由 API 构造而非本地 git 传输。

安全约定
--------
· 令牌只从环境变量 GH_TOK 读取，不写盘、不打印
· 只推送 `git ls-files` 列出的文件（即已提交的文件），
  因此 .env / 数据库 / 中间产物天然不会被上传

用法：
  set GH_TOK=...   （或用 git credential 注入）
  python push_via_api.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOK = os.environ.get("GH_TOK", "").strip()
OWNER = "ArnoldWang-86"
REPO = "pingan-branch-ops-monitoring"
BRANCH = "main"
GIT = r"C:\Program Files\Git\cmd\git.exe"

H = {
    "Authorization": "token " + TOK,
    "Accept": "application/vnd.github+json",
    "User-Agent": "ops-repo-push",
}
API = "https://api.github.com"


def api(method, path, payload=None, timeout=90):
    url = path if path.startswith("http") else API + path
    kw = {"headers": H, "timeout": timeout}
    if payload is not None:
        # 关键：ensure_ascii=False + 显式 UTF-8 字节，
        # 否则中文文件名会被转成 \\uXXXX 转义，GitHub 侧路径会不对。
        kw["data"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        kw["headers"] = dict(H, **{"Content-Type": "application/json"})
    return requests.request(method, url, **kw)


def main():
    if not TOK:
        raise SystemExit("缺少 GH_TOK 环境变量")

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(root)

    # 1) 本地已提交的文件清单（这是唯一的推送依据）
    #
    # ⚠ 两个必须处理的坑：
    #   ① git 默认 core.quotepath=true，会把中文路径转成 "\345\205..." 八进制转义，
    #      直接拿去做 open() 会报 Invalid argument。
    #   ② 含特殊字符的路径，git 会用双引号包裹输出。
    subprocess.run([GIT, "config", "core.quotepath", "false"], capture_output=True)
    files = subprocess.run([GIT, "ls-files"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace").stdout.split("\n")
    cleaned = []
    for f in files:
        f = f.strip()
        if not f:
            continue
        if len(f) >= 2 and f[0] == '"' and f[-1] == '"':
            f = f[1:-1]
        cleaned.append(f)
    files = cleaned
    print(f"本地已跟踪文件: {len(files)} 个")
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        print("  [ERR] 以下文件在磁盘上不存在，无法推送：")
        for m in missing[:10]:
            print("    " + m)
        return 1

    # 2) 空仓库引导
    #    GitHub 的 Git Data API 在**完全空的仓库**上不能创建 blob
    #    （返回 409 "Git Repository is empty."），因为底层还没有 git 对象库。
    #    所以先用 Contents API 写一个占位文件造出首个提交，后续再正常推送。
    #
    #    ⚠ 判断「是否为空」不能用 `GET /git/ref/heads/main` 的状态码：
    #    在空仓库上它也会返回 200（内容异常），我第一版因此跳过了引导、
    #    然后在建 blob 时报 409。可靠判据是「分支列表是否为空」。
    print("\n[0/4] 检查远端状态 ...")
    br = api("GET", f"/repos/{OWNER}/{REPO}/branches")
    is_empty = (br.status_code == 200 and len(br.json()) == 0) or br.status_code == 404
    if is_empty:
        print("      远端为空仓库，先用 Contents API 造初始提交 ...")
        boot = api("PUT", f"/repos/{OWNER}/{REPO}/contents/.gitkeep",
                   {"message": "chore: bootstrap repository", "branch": BRANCH,
                    "content": base64.b64encode(b"# bootstrap\n").decode("ascii")})
        if boot.status_code not in (200, 201):
            print(f"  [ERR] 引导失败 HTTP {boot.status_code} {boot.text[:200]}")
            return 1
        print("      引导提交已创建")
    else:
        names = [b["name"] for b in br.json()] if br.status_code == 200 else []
        print(f"      远端已有分支: {names}")

    # 3) 建 blob
    print("\n[1/4] 创建 blob ...")
    tree_items = []
    for i, f in enumerate(files, 1):
        with open(f, "rb") as fh:
            content = fh.read()
        r = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                {"content": base64.b64encode(content).decode("ascii"),
                 "encoding": "base64"})
        if r.status_code != 201:
            print(f"  [ERR] {f}: HTTP {r.status_code} {r.text[:160]}")
            return 1
        tree_items.append({
            "path": f.replace("\\", "/"),
            "mode": "100644",
            "type": "blob",
            "sha": r.json()["sha"],
        })
        if i % 10 == 0 or i == len(files):
            print(f"      {i}/{len(files)}")

    # 3) 建 tree
    print("\n[2/4] 创建 tree ...")
    r = api("POST", f"/repos/{OWNER}/{REPO}/git/trees",
            {"tree": tree_items})
    if r.status_code != 201:
        print(f"  [ERR] HTTP {r.status_code} {r.text[:300]}")
        return 1
    tree_sha = r.json()["sha"]
    print(f"      tree {tree_sha[:12]}  条目 {len(tree_items)}")

    # 4) 建 commit（父提交取远端 main，若为空仓库则无父）
    print("\n[3/4] 创建 commit ...")
    msg = subprocess.run([GIT, "log", "-1", "--pretty=%B"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout.strip()
    parents = []
    r = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}")
    if r.status_code == 200:
        parents = [r.json()["object"]["sha"]]
        print(f"      远端已有 {BRANCH}，父提交 {parents[0][:12]}")
    else:
        print("      远端为空仓库，无父提交")

    body = {"message": msg or "init", "tree": tree_sha}
    if parents:
        body["parents"] = parents
    r = api("POST", f"/repos/{OWNER}/{REPO}/git/commits", body)
    if r.status_code != 201:
        print(f"  [ERR] HTTP {r.status_code} {r.text[:300]}")
        return 1
    commit_sha = r.json()["sha"]
    print(f"      commit {commit_sha[:12]}")

    # 5) 更新 ref
    print("\n[4/4] 更新分支 ref ...")
    if parents:
        r = api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
                {"sha": commit_sha, "force": False})
    else:
        r = api("POST", f"/repos/{OWNER}/{REPO}/git/refs",
                {"ref": f"refs/heads/{BRANCH}", "sha": commit_sha})
    if r.status_code not in (200, 201):
        print(f"  [ERR] HTTP {r.status_code} {r.text[:300]}")
        return 1
    print(f"      {BRANCH} -> {commit_sha[:12]}")

    # 6) 写 topics（仓库描述里带不了，单独设）
    api("PUT", f"/repos/{OWNER}/{REPO}/topics",
        {"names": ["data-analysis", "banking", "anomaly-detection", "operations",
                   "text-to-excel", "pandas", "duckdb", "sql", "plotly",
                   "data-quality"]})

    # 7) 复核
    r = api("GET", f"/repos/{OWNER}/{REPO}")
    d = r.json()
    print("\n" + "=" * 62)
    print(f"  仓库: {d.get('html_url')}")
    print(f"  大小: {d.get('size')} KB   分支: {d.get('default_branch')}")
    print(f"  topics: {', '.join(d.get('topics') or [])}")
    r2 = api("GET", f"/repos/{OWNER}/{REPO}/commits?per_page=1")
    if r2.status_code == 200 and r2.json():
        c = r2.json()[0]
        print(f"  最新提交: {c['sha'][:12]}  {c['commit']['message'].splitlines()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
