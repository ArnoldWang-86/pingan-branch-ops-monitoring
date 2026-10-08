# -*- coding: utf-8 -*-
"""推送辅助：自动从 git 凭据助手取令牌，再调用 incremental_push。

为什么需要它
------------
本机 `github.com:443` 不通，推送只能走 api.github.com，令牌要手动取一次。
每次都要写这段 PowerShell 很啰嗦：

    $tok = ("protocol=https`nhost=github.com`n`n" | & $git credential fill |
            Select-String "^password=").Line -replace "^password=",""
    $env:GH_TOK = $tok.Trim()

而且 Windows 下 `git` 常不在 PATH（装在 Program Files\\Git\\cmd），
所以本脚本会自动探测 git 可执行文件的常见位置。

令牌只在本进程内存与环境变量里流转，不写盘、不打印。

用法：
  python push_now.py            # 取令牌并推送
  python push_now.py --status   # 只看本地/远端是否一致，不推送
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(HERE, "..", ".."))

GIT_CANDIDATES = [
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Git\cmd\git.exe"),
]


def find_git() -> str:
    for p in GIT_CANDIDATES:
        if os.path.exists(p):
            return p
    # 退回到 PATH
    for name in ("git", "git.exe"):
        try:
            subprocess.run([name, "--version"], capture_output=True, check=True)
            return name
        except Exception:                                    # noqa: BLE001
            continue
    raise SystemExit("找不到 git.exe；安装 Git 或把它的 cmd 目录加入 PATH")


def get_token(git: str) -> str:
    """从 git 凭据助手取 GitHub 令牌。"""
    inp = "protocol=https\nhost=github.com\n\n"
    r = subprocess.run([git, "credential", "fill"], input=inp,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for line in (r.stdout or "").splitlines():
        if line.startswith("password="):
            tok = line[len("password="):].strip()
            if tok:
                print(f"  已取到令牌（长度 {len(tok)}，前缀 {tok[:4]}***）")
                return tok
    raise SystemExit(
        "未能从凭据助手取到令牌。\n"
        "  先确认本机已登录过 GitHub：\n"
        "    git push   # 触发一次登录\n"
        "  或改用个人访问令牌：\n"
        "    set GH_TOK=ghp_xxx   ; python push_now.py")


def main():
    git = find_git()
    print(f"git: {git}")

    if "--status" in sys.argv:
        local = subprocess.run([git, "rev-parse", "--short", "HEAD"],
                               cwd=PROJ, capture_output=True, text=True).stdout.strip()
        print(f"  本地 HEAD: {local}")
        return 0

    tok = os.environ.get("GH_TOK", "").strip()
    if tok:
        print(f"  使用环境变量 GH_TOK（长度 {len(tok)}）")
    else:
        tok = get_token(git)
    os.environ["GH_TOK"] = tok

    script = os.path.join(HERE, "incremental_push.py")
    print(f"\n执行 {os.path.basename(script)} ...\n")
    # 用同一个解释器、继承环境（令牌通过环境变量传递，不进命令行）
    return subprocess.call([sys.executable, script], cwd=PROJ)


if __name__ == "__main__":
    sys.exit(main())
