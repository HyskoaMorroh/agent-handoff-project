#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regenerate the README screenshots from the real GUI.

Why a script instead of manual captures: the earlier screenshots were taken by
hand on a real machine, so they printed the operator's actual home directory
(`C:\\Users\\<name>\\...`) across the most prominent image in the README — the
same paths the handoff document goes out of its way to redact. They also went
stale silently whenever the UI changed.

This drives the shipped server with a synthetic HOME and synthetic transcripts,
so the captures are reproducible, contain no personal paths, and always show the
current UI. Requires Chrome/Edge; skipped in CI where no browser is present.

Usage:  python scripts/shoot_screenshots.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "img"
# 只拍体检视图：它是这个工具唯一有内容可看的界面。交接视图在没填仓库路径时
# 是一张空表单，截下来只能证明「有个表单」——而且视图切换有淡入动画，
# headless 抓到的往往是两个导航项同时高亮的过渡帧。
SHOTS = (
    # (filename, theme, viewport)
    ("gui-light.png", "light", (1440, 1000)),
    ("gui-dark.png", "dark", (1440, 1000)),
)


def find_chrome() -> str | None:
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(cand).is_file():
            return cand
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def fake_sessions(home: Path) -> None:
    """Synthetic transcripts: enough shape for the cards to look real, no real data."""
    cx = home / ".codex" / "sessions" / "2026" / "08" / "21"
    cl = home / ".claude" / "projects" / "demo-project"
    cx.mkdir(parents=True, exist_ok=True)
    cl.mkdir(parents=True, exist_ok=True)

    repo = "/work/checkout-service"
    rows = [
        {"type": "session_meta", "payload": {
            "session_id": "01a02005-67da-7ea2-9ab3-43a4097cf299",
            "cwd": str(home / "Documents" / "Codex" / "2026-08-21" / "checkout"),
            "cli_version": "0.148.0", "originator": "Codex Desktop"}},
        {"type": "response_item", "payload": {"role": "user", "content": [
            {"text": f"继续 checkout-service。仓库 {repo}，分支 main。先读 docs/plan.md。"}]}},
        {"type": "compacted", "payload": {
            "window_number": 1,
            "message": ("# 交接摘要\n\n## 当前进度\n- 已进入仓库：`" + repo + "`\n"
                        "- Task 3 的退款流程已完成，测试全绿\n- Task 4 的对账报表尚未开始"),
            "replacement_history": [
                {"role": "user", "content": [{"text": "不要改动 config/production.yaml"}]}]}},
        {"type": "event_msg", "payload": {"type": "task_complete",
                                          "error": {"message": "503: 所有供应商已熔断，无可用渠道"}}},
    ]
    with (cx / "rollout-2026-08-21T09-12-04-01a02005-67da-7ea2-9ab3-43a4097cf299.jsonl").open(
            "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        # pad the file so its size lands in a visible band
        for i in range(2600):
            fh.write(json.dumps({"type": "response_item", "payload": {
                "role": "assistant", "content": [{"text": f"分析第 {i} 步的调用链与边界条件。"}]}},
                ensure_ascii=False) + "\n")

    claude = [
        {"type": "ai-title", "sessionId": "7f3c1d20-4b8a-4e02-9a71-2c6d8e5f0a13",
         "aiTitle": "Refund flow rewrite and reconciliation report"},
        {"type": "system", "sessionId": "7f3c1d20-4b8a-4e02-9a71-2c6d8e5f0a13",
         "cwd": repo, "gitBranch": "main", "version": "2.1.237"},
        {"type": "user", "message": {"content": "把对账报表的分页改成游标分页"}},
        {"type": "last-prompt", "sessionId": "7f3c1d20-4b8a-4e02-9a71-2c6d8e5f0a13",
         "lastPrompt": "把对账报表的分页改成游标分页，并补一个跨月边界的测试"},
    ]
    with (cl / "7f3c1d20-4b8a-4e02-9a71-2c6d8e5f0a13.jsonl").open("w", encoding="utf-8") as fh:
        for r in claude:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        for i in range(900):
            fh.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": f"第 {i} 处改动的影响面评估。"}]}},
                ensure_ascii=False) + "\n")


def shoot(chrome: str, url: str, png: Path, size: tuple[int, int], profile: Path) -> bool:
    args = [
        chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        f"--window-size={size[0]},{size[1]}",
        f"--screenshot={png}", f"--user-data-dir={profile}",
        "--force-device-scale-factor=2", "--virtual-time-budget=4000",
        url,
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if not png.is_file():
        print(r.stderr[-500:], file=sys.stderr)
        return False
    return True


def main() -> int:
    chrome = find_chrome()
    if not chrome:
        print("no Chrome/Edge found — skipping screenshots", file=sys.stderr)
        return 0

    # 假家目录必须建在一个**路径里不含真实用户名**的地方。系统临时目录在
    # Windows 上是 `C:\Users\<name>\AppData\Local\Temp\...`，用它当 HOME 会让
    # 转录路径把用户名印在 README 最显眼的那张图上——那正是要避免的事。
    base_tmp = Path(os.environ.get("SYSTEMDRIVE", "C:") + os.sep) / "ah-shots-tmp"
    if base_tmp.exists():
        shutil.rmtree(base_tmp, ignore_errors=True)
    try:
        base_tmp.mkdir(parents=True)
    except OSError:
        base_tmp = Path(tempfile.mkdtemp(prefix="ah-shots-"))
    try:
        return run(chrome, base_tmp)
    finally:
        shutil.rmtree(base_tmp, ignore_errors=True)


def run(chrome: str, tmp: Path) -> int:
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    fake_sessions(home)
    profile = tmp / "chrome"

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    port = free_port()

    # 走 serve() 而不是 main_entry()：后者不接受端口与「别开浏览器」，
    # 而截图必须固定端口、绝不弹窗。
    #
    # 令牌写进文件而不是从 stdout 读：子进程的 stdout 是块缓冲的，
    # readline() 会一直阻塞到缓冲区满——脚本因此挂死而不是失败。
    token_file = tmp / "token.txt"
    boot = (
        "import sys, pathlib;"
        "from agent_handoff.gui import server as S;"
        f"pathlib.Path(r'{token_file}').write_text(S.TOKEN, encoding='utf-8');"
        f"sys.exit(S.serve(port={port}, open_browser=False))"
    )
    server = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", boot],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}/"
    try:
        token = ""
        for _ in range(150):
            if server.poll() is not None:
                print("server exited:\n" + (server.stdout.read() or ""), file=sys.stderr)
                return 1
            if token_file.is_file():
                token = token_file.read_text(encoding="utf-8").strip()
                if token:
                    break
            time.sleep(0.1)
        if not token:
            print("the server never wrote its one-time token", file=sys.stderr)
            return 1
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "?token=" + token, timeout=1).read(1)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)

        OUT.mkdir(parents=True, exist_ok=True)
        ok = 0
        for i, (name, theme, size) in enumerate(SHOTS):
            url = f"{base}?token={token}#theme={theme}"
            # 每张图一个干净 profile：profile 会存 localStorage，上一张存下的
            # ah.theme 会把下一张的主题带跑偏。
            if shoot(chrome, url, OUT / name, size, profile.with_name(f"chrome-{i}")):
                kb = (OUT / name).stat().st_size // 1024
                print(f"{name}  {size[0]}x{size[1]}  {theme}  {kb} KB")
                ok += 1
        return 0 if ok == len(SHOTS) else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
