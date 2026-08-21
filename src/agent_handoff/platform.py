#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台差异的唯一收容所。

原版脚本把 Windows 假设散落在各处（`os.startfile`、只匹配盘符的路径正则、
只在 nt 上做的斜杠归一化），Linux 上会静默降级而不是报错——那比崩溃更糟。
所有分支集中在这里，其他模块只调函数，不问自己跑在哪个系统上。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# 原版只认 `C:\...` 形式，Linux/macOS 的 `/home/me/proj` 一个都匹配不到，
# guess_repos 在非 Windows 上等于永远返回空。两种形态都要认。
WIN_PATH_RX = re.compile(r'[A-Za-z]:[\\/][^\s"\'<>|*?\n]{3,140}')
POSIX_PATH_RX = re.compile(r'(?:/[\w.\-+@]+){2,12}/?')
# 路径尾部要削掉的半角标点。
PATH_TRIM = '\\/",。、）)]}>;:='
# 全角标点是句子里的分隔符，不可能出现在路径中间——出现即意味着路径已经结束。
# 用户几乎总是写「仓库 E:/output/proj，分支 main」，而全角逗号不是空白字符，
# WIN_PATH_RX 的 `[^\s...]` 会把它连同后面的「分支」一起吃进候选路径，于是
# `nearest_repo` 去找 `E:/output/proj，分支` 这个不存在的目录，仓库推断静默
# 失败——实测本机 5/6 个最新 Codex 转录的 repos 因此为空。
# 只在这里截断而不是把所有中日韩字符都排除：中文目录名（`E:/项目/前端`）是
# 合法路径，一并排除会引入新的漏检。
_FULLWIDTH_CUT = re.compile(r"[，。、：；！？（）【】《》「」“”…]")


def iter_path_candidates(text: str):
    """从任意文本里捞出「看起来像绝对路径」的片段，两种平台形态都认。

    先扫盘符形态，再扫 POSIX 形态。跨平台捞取而不是按当前系统二选一：
    转录可能是在另一台机器上产生的（WSL 写的记录在 Windows 上读，或反之）。

    命中片段先在第一个全角标点处截断，再削尾部标点——见 `_FULLWIDTH_CUT`。
    """
    seen = set()
    for rx in (WIN_PATH_RX, POSIX_PATH_RX):
        for m in rx.finditer(text):
            cand = m.group(0)
            cut = _FULLWIDTH_CUT.search(cand)
            if cut:
                cand = cand[: cut.start()]
            cand = cand.rstrip(PATH_TRIM)
            if len(cand) < 4 or cand in seen:
                continue
            seen.add(cand)
            yield cand


def venv_interpreters(venv: Path) -> tuple[Path | None, str]:
    """返回 (可执行 Python 路径, 相对写法)；没有就 (None, "")。

    Windows 放在 `Scripts/python.exe`，POSIX 放在 `bin/python`（有时只有
    `bin/python3`）。三种都查，不假设当前系统的布局就是唯一布局——同一个仓库
    可能被 Windows 和 WSL 交替使用，两种 venv 目录会同时存在。
    """
    for rel in ("Scripts/python.exe", "bin/python", "bin/python3", "Scripts/python3.exe"):
        cand = venv / rel
        if cand.is_file():
            return cand, rel
    return None, ""


def shell_quote(path: str) -> str:
    """把路径转成能直接塞进 shell 命令行的形态。

    Windows 的 cmd.exe 无法执行 `.venv-win/Scripts/python.exe` 这种带正斜杠的
    相对路径——它会把第一段当命令名。POSIX shell 反过来：反斜杠是转义符。
    另外含空格的路径两边都要加引号，原版漏了这点，装在 `C:\\Program Files`
    下的解释器会被拆成两个参数。
    """
    p = path.replace("/", "\\") if IS_WINDOWS else path.replace("\\", "/")
    return f'"{p}"' if " " in p else p


def normalize_shell_paths(cmd: str) -> str:
    """归一化 shell 字符串里的可执行文件路径分隔符。

    原版只在 nt 上做，且只认 `.exe` 结尾。POSIX 上 `.venv\\bin\\python` 这种
    （从 Windows 配置里抄来的）命令会因为反斜杠被当成转义符而失败。
    """
    if IS_WINDOWS:
        return re.sub(
            r"(?<![\w:])((?:\.[\w.\-]+|[\w.\-]+)(?:/[\w.\-]+)+\.exe)",
            lambda m: m.group(1).replace("/", "\\"),
            cmd,
        )
    return re.sub(
        r"(?<![\w:])((?:\.[\w.\-]+|[\w.\-]+)(?:\\[\w.\-]+)+)(?=\s|$)",
        lambda m: m.group(1).replace("\\", "/"),
        cmd,
    )


def open_in_browser(target: str) -> bool:
    """在系统默认应用里打开文件或 URL。成功返回 True。

    `os.startfile` 只有 Windows 有，原版在 Linux 上会抛 AttributeError 并被
    宽泛的 except 吞掉，用户只看到「自动打开失败」。三平台各走各的路。
    """
    try:
        if IS_WINDOWS:
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
            return True
        if IS_MACOS:
            return subprocess.run(["open", target], check=False).returncode == 0
        for opener in ("xdg-open", "gio", "wslview"):
            if shutil.which(opener):
                args = [opener, "open", target] if opener == "gio" else [opener, target]
                return subprocess.run(args, check=False).returncode == 0
        # 无桌面环境（纯 SSH 会话）时退回 webbrowser，它还能试 BROWSER 变量。
        import webbrowser

        return webbrowser.open(target)
    except OSError:
        return False


def clear_screen() -> None:
    """清屏。终端不支持时静默跳过，不要把控制字符吐到日志里。"""
    if not sys.stdout.isatty():
        return
    os.system("cls" if IS_WINDOWS else "clear")  # noqa: S605


def force_utf8_io() -> None:
    """把 stdout 与 stderr 都钉到 UTF-8。

    原版只处理了 stdout，于是 GBK 控制台下 stderr 上的中文报错会变成
    UnicodeEncodeError——错误信息本身把错误吃掉了。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def agent_session_roots() -> list[tuple[str, Path]]:
    """各智能体存放转录的位置。不存在的目录直接跳过。

    Windows 的 Claude Code / Codex 装在 %USERPROFILE% 下；在 WSL 里跑时，
    真正的记录往往在 Windows 侧的用户目录，所以两处都找。
    """
    roots: list[tuple[str, Path]] = []
    homes = [Path(os.path.expanduser("~"))]

    # WSL：/mnt/c/Users/<name> 才是宿主机的家目录，转录在那边。
    if not IS_WINDOWS:
        for base in (Path("/mnt/c/Users"), Path("/c/Users")):
            if not base.is_dir():
                continue
            try:
                for entry in base.iterdir():
                    if entry.is_dir() and entry.name.lower() not in {"public", "default", "all users"}:
                        homes.append(entry)
            except OSError:
                pass

    seen: set[str] = set()
    for home in homes:
        for name, rel in (("Claude Code", ".claude/projects"), ("Codex", ".codex/sessions")):
            p = home / rel
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            if p.is_dir():
                roots.append((name, p))
    return roots


def norm_path(s: str) -> str:
    """比较路径时不在乎斜杠方向与盘符大小写。"""
    return re.sub(r"[\\/]+", "/", str(s)).rstrip("/").lower()


def nearest_repo(start: str) -> str:
    """从一个目录往上走，直到看见 .git。都没有就返回 ''。"""
    try:
        p = Path(start).resolve()
    except (OSError, ValueError):
        return ""
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return str(cand)
    return ""
