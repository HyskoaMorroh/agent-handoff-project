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

    转录**不会**因为项目在别的盘而跟着搬家：实测本机 E 盘上的项目，转录仍在
    C 盘家目录下，盘符只出现在转录内容记录的 `cwd` 里。但两个应用都允许用
    环境变量把整个数据目录挪走（`CLAUDE_CONFIG_DIR` / `CODEX_HOME`），
    笔记本上把它们指向 D 盘是常见做法。不读这两个变量的后果不是少扫几个文件，
    而是「一个转录都找不到」——所以它们优先于家目录。
    """
    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(name: str, p: Path) -> None:
        key = norm_path(str(p))
        if key in seen:
            return
        seen.add(key)
        if p.is_dir():
            roots.append((name, p))

    # 显式指定的数据目录优先。`CLAUDE_CONFIG_DIR` 指向 `.claude` 本身，
    # `CODEX_HOME` 指向 `.codex` 本身——都是目录，不是它们的父目录。
    for env, name, subs in (
        ("CLAUDE_CONFIG_DIR", "Claude Code", ("projects",)),
        ("CODEX_HOME", "Codex", ("sessions", "archived_sessions")),
    ):
        raw = (os.environ.get(env) or "").strip().strip('"')
        if not raw:
            continue
        base = Path(os.path.expanduser(os.path.expandvars(raw)))
        for sub in subs:
            add(name, base / sub)

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

    for home in homes:
        # `.codex/archived_sessions` 也要扫：在 Codex 里归档一个会话只是把它移出
        # 活动列表，转录本身还在，而「归档了但还想把结论带走」恰恰是交接的典型
        # 场景——实测用户勾中的会话就在这里，工具却报「找不到这个转录」。
        for name, rel in (
            ("Claude Code", ".claude/projects"),
            ("Codex", ".codex/sessions"),
            ("Codex", ".codex/archived_sessions"),
        ):
            add(name, home / rel)
    return roots


def norm_path(s: str) -> str:
    """比较路径时不在乎斜杠方向与盘符大小写。"""
    return re.sub(r"[\\/]+", "/", str(s)).rstrip("/").lower()


def split_multi(text: str) -> list[str]:
    """把一段「多个值」的输入拆成去重后的列表。

    命令行与网页界面共用同一份实现：`--find a,b` 与在网页搜索框里粘
    `a, b` 必须给出相同结果，两处各写一份迟早会在细节上分叉。

    分隔符含全角逗号、顿号与空白：中文输入法下敲出来的是 `，` 和 `、`，
    而从别处复制一串会话 ID 时最常见的分隔就是换行或空格。只认半角逗号
    会让「粘进来一串 ID」变成一个找不到的长字符串。

    顺带去掉包裹的引号——shell 里带空格的路径常被整段引起来。
    """
    out: list[str] = []
    for part in re.split(r"[,，、;；\s]+", str(text or "")):
        cleaned = part.strip().strip('"').strip("'")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


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


# 「另一台电脑上的家目录」的结构特征。用结构而不是猜用户名：
# 硬编码用户名列表在换机器时必然失效，而 `<盘符>:\Users\<任意名>` 与
# `/home/<任意名>` 这两种形状在所有 Windows / Linux 机器上都成立。
# 用于两件事：判断转录是不是本机产生的，以及脱敏时连别人的用户名一起处理。
FOREIGN_HOME_RX = re.compile(
    r"(?:^|(?<=[\s\"'`(=]))"
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/home/|/Users/|/mnt/[a-z]/Users/)"
    r"([^\\/\s\"'`,;:)]+)",
    re.I,
)


def local_home_names() -> set[str]:
    """本机家目录的名字。用来把「别人的用户名」与「我自己的」区分开。

    返回集合而不是单个字符串：WSL 里 `~` 是 Linux 侧的名字，而转录可能来自
    Windows 侧，两个名字都算本机。
    """
    names: set[str] = set()
    try:
        home = Path(os.path.expanduser("~"))
    except (OSError, RuntimeError):
        return names
    if home.name:
        names.add(home.name.lower())
    # WSL 下 /mnt/c/Users/<name> 也是「本机」，名字可能与 Linux 侧不同。
    for env in ("USERNAME", "USER", "LOGNAME"):
        val = (os.environ.get(env) or "").strip()
        if val:
            names.add(val.lower())
    return names


def is_foreign_path(raw: str) -> bool:
    """这个路径确定属于另一台电脑吗？

    只认一条强判据：**路径形如某个家目录，而其中的用户名不是本机的**。
    不猜用户名清单（那换台机器就失效），靠 `FOREIGN_HOME_RX` 的结构匹配。

    刻意**不**把「绝对路径但不存在」算进来，那是弱信号：本机删掉一个项目目录
    是常事，而这个判断会关掉原生续接——那个会话在应用索引里还在，续接本来
    是可用的，误判等于把能用的功能关掉。路径失效那种情况用 `path_is_stale`
    单独表达，两者后果不同：一个说「续接不了」，一个只说「这条路径打不开」。
    """
    raw = (raw or "").strip()
    if not raw:
        return False
    m = FOREIGN_HOME_RX.search(raw)
    if not m:
        return False
    return m.group(1).lower() not in local_home_names()


def path_is_stale(raw: str) -> bool:
    """这条路径在本机打不开吗？

    与 `is_foreign_path` 分开：本机删掉的目录也会命中这里，但那**不**意味着
    会话不能续接。用来提示「照这条路径找不到东西」，不用来决定续接可行性。

    只对绝对路径成立——相对片段无从判断，报了只会给正常会话加噪声。
    """
    raw = (raw or "").strip()
    if not raw or not re.match(r"^(?:[A-Za-z]:[\\/]|[\\/])", raw):
        return False
    try:
        return not Path(raw).exists()
    except (OSError, ValueError):
        return False


# 图文说明的文件名。历史上还叫过 `使用说明.html`，留着兼容旧检出。
_GUIDE_NAMES = ("guide.html", "使用说明.html")


def find_guide() -> Path | None:
    """找到图文说明，找不到返回 None。

    为什么要一个共用函数：这个文件此前在两处各自推断路径——`gui/server.py`
    从自己往上数四层，`menu.py` 数三层（两者深度不同，所以层数本来就该不同，
    各自都对）。但两处都只认「源码检出」这一种布局，于是 `pip install` 之后
    都找不到它：`docs/` 从来没有进过 wheel，往上数几层都是 `site-packages`。

    找的顺序，从最可靠到最兜底：
      1. 包内的 `gui/static/guide.html` —— 装进 wheel 的那一份，随包走，
         `pip install` 之后唯一还在的副本
      2. 源码检出的 `<repo>/docs/guide.html` —— 开发时用的那份，也是
         `build_guide.py` 实际生成的位置
      3. `AGENT_HANDOFF_HOME` 指向的检出 —— 用户显式声明工具装在哪

    返回 `None` 时调用方要降级（网页界面隐藏入口、菜单打印「找不到」），
    不能报错：图文说明缺失不影响工具的任何实际功能。
    """
    here = Path(__file__).resolve().parent

    # 1. 包内副本（wheel 场景）。
    for name in _GUIDE_NAMES:
        cand = here / "gui" / "static" / name
        if cand.is_file():
            return cand

    # 2. 源码检出。`platform.py` 在 `<repo>/src/agent_handoff/` 下，
    #    所以 `<repo>` 是往上两层。
    repo = here.parent.parent
    for name in _GUIDE_NAMES:
        for cand in (repo / "docs" / name, repo / name):
            if cand.is_file():
                return cand

    # 3. 用户声明的检出位置。
    declared = os.environ.get("AGENT_HANDOFF_HOME", "").strip()
    if declared:
        try:
            base = Path(declared).expanduser()
        except (OSError, ValueError):
            return None
        for name in _GUIDE_NAMES:
            for cand in (base / "docs" / name, base / name):
                try:
                    if cand.is_file():
                        return cand
                except OSError:
                    continue
    return None
