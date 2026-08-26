#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开跑之前先说清楚这台机器缺什么。

为什么需要它：这个工具的失败模式几乎全是**沉默的**——环境少一样东西，结果
不是报错，而是某一列数字变空、某一批会话不出现在列表里。实测过的三种：

  · 本机没有 zstd 实现时，7 天前的 Codex 会话一个都不显示（列表看起来正常，
    只是短了一截，用户没有任何线索知道漏掉了什么）。
  · `CODEX_HOME` 指到别处而目录不存在时，扫描结果是空的，看起来像「你还没
    用过 Codex」。
  · git 不可用时，交接流程走到第四步才失败——前三步的输出已经打出来了，
    用户以为快成功了。

网页界面早就把 `gitAvailable` 广播给前端，但前端从来不读它（见
`gui/server.py` 的 bootstrap），所以 git 缺失只在跑到一半时才暴露。这个模块
把这些检查提前到一条命令里，并且**同一份结果**同时供 CLI、网页界面与 JSON
输出使用，三处说法不会打架。

设计约束：
  · 只读。不创建目录、不装东西、不改环境变量——一个诊断工具去修环境，
    等于把「我以为它没动我的机器」变成一次意外。唯一的写操作是往临时目录
    写一个几字节的探针文件并立刻删掉，用来回答「输出目录能不能写」。
  · 不抛异常。诊断本身崩掉是最没道理的失败，所以每一项都自己兜住。
  · 结果是**事实而不是句子**：`checks` 里每项给 `key` / `level` / `data`，
    文案由 i18n 渲染。这样三种语言共用同一份判据。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..platform import (
    IS_WINDOWS,
    agent_session_roots,
    is_transcript_name,
    zstd_opener,
)
from .workspace import WorkspaceMap

# 诊断项的严重度。名字与体征分档刻意不同：那边说的是「会话有多满」，
# 这边说的是「这台机器缺什么」，两套语义混用会让界面上出现两种意思的
# 同名徽章。
OK = "ok"
WARN = "warn"
FAIL = "fail"

# 环境变量：会改变工具去哪里找东西。列出来是因为「设了但指错了」比「没设」
# 更难查——没设的时候用默认路径，设错的时候是一片空白。
_WATCHED_ENV = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "CODEX_SESSIONS_ROOT",
    "AGENT_HANDOFF_CONTEXT_WINDOW",
)


def _check_python() -> dict[str, Any]:
    """Python 版本。低于 3.9 直接不能跑（项目声明的最低版本）。"""
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < (3, 9):
        return {"key": "python", "level": FAIL, "data": {"version": text, "need": "3.9"}}
    return {"key": "python", "level": OK, "data": {"version": text}}


def _check_git() -> dict[str, Any]:
    """git 在不在 PATH 上，以及版本。

    不用 `core.gitops.git_available()`：那个函数缓存结果、且只回答是否可用。
    这里要连版本一起给出来——「git 太老不支持某个参数」这类问题看版本才看得出。
    """
    exe = shutil.which("git")
    if not exe:
        return {"key": "git", "level": FAIL, "data": {}}
    ver = ""
    try:
        import subprocess

        p = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        if p.returncode == 0:
            ver = (p.stdout or "").strip()
    except Exception:
        # 版本问不出来不影响「git 存在」这个结论，别让诊断本身失败。
        pass
    return {"key": "git", "level": OK, "data": {"path": exe, "version": ver}}


def _check_zstd() -> dict[str, Any]:
    """能不能读 `.jsonl.zst`。

    这一项是 WARN 而不是 FAIL：没有 zstd 时工具照常工作，只是压缩归档的会话
    读不到正文（列表里仍然出现，标注「读不到正文」）。但它必须被说出来——
    这是本工具最容易沉默失效的一处，7 天前的 Codex 会话会整批变成空壳。
    """
    if zstd_opener() is not None:
        # 哪一个实现被用上了，对排查有用（标准库和第三方的行为略有差异）。
        impl = ""
        for name, mod in (("compression.zstd", "compression.zstd"), ("zstandard", "zstandard"), ("pyzstd", "pyzstd")):
            try:
                __import__(mod)
                impl = name
                break
            except Exception:
                continue
        return {"key": "zstd", "level": OK, "data": {"impl": impl}}
    return {"key": "zstd", "level": WARN, "data": {}}


def _check_roots() -> list[dict[str, Any]]:
    """每个智能体的数据根：在不在、里面有多少份转录、其中多少是压缩的。

    只数文件名，不读正文——一次目录遍历（实测 463 份转录 14 毫秒），
    而读正文可能是几百 MB。
    """
    out: list[dict[str, Any]] = []
    roots = agent_session_roots()
    if not roots:
        return [{"key": "roots.none", "level": FAIL, "data": {}}]
    for agent, root in roots:
        total = 0
        packed = 0
        try:
            stack = [root]
            while stack:
                cur = stack.pop()
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(Path(e.path))
                            elif is_transcript_name(e.name) and e.is_file(follow_symlinks=False):
                                total += 1
                                if e.name.lower().endswith(".zst"):
                                    packed += 1
                        except OSError:
                            continue
        except OSError as exc:
            out.append({
                "key": "root.unreadable",
                "level": WARN,
                "data": {"agent": agent, "path": str(root), "error": exc.__class__.__name__},
            })
            continue
        out.append({
            "key": "root",
            # 目录在但一份转录都没有：不是错误（可能刚装上），但要说出来——
            # 否则用户以为扫描坏了。
            "level": OK if total else WARN,
            "data": {"agent": agent, "path": str(root), "count": total, "compressed": packed},
        })
    return out


def _check_env() -> list[dict[str, Any]]:
    """被读取的环境变量：设了什么、指向的目录存不存在。

    「设了但指错」是最难查的一类：没设时用默认路径能扫到东西，设错时是
    一片空白，而两种情形在界面上长得一模一样。
    """
    out: list[dict[str, Any]] = []
    for name in _WATCHED_ENV:
        raw = (os.environ.get(name) or "").strip().strip('"')
        if not raw:
            continue
        data: dict[str, Any] = {"name": name, "value": raw}
        if name == "AGENT_HANDOFF_CONTEXT_WINDOW":
            # 这个不是路径，是个数。笔误（负数、非数字）会让整列占用率失真，
            # 而 `_declared_window` 会静默忽略它——诊断里必须说出来。
            try:
                val = int(raw)
                level = OK if val > 0 else WARN
                data["parsed"] = val
            except ValueError:
                level = WARN
                data["parsed"] = None
        else:
            expanded = Path(os.path.expanduser(os.path.expandvars(raw)))
            data["exists"] = expanded.is_dir()
            level = OK if expanded.is_dir() else WARN
        out.append({"key": "env", "level": level, "data": data})
    return out


def _check_writable() -> dict[str, Any]:
    """临时目录能不能写。

    交接文档用原子写（同目录临时文件 + 替换），所以真正要求的是**目标目录**
    可写；但目标目录取决于用户给的 `--out`，诊断时还不知道。这里退一步只验
    临时目录——它不能写的话，连测试探针都跑不起来，是更早的问题。
    """
    try:
        with tempfile.NamedTemporaryFile(prefix="agent-handoff-doctor-", delete=False) as fh:
            probe = Path(fh.name)
            fh.write(b"ok")
        probe.unlink()
        return {"key": "writable", "level": OK, "data": {"path": tempfile.gettempdir()}}
    except Exception as exc:
        return {
            "key": "writable",
            "level": FAIL,
            "data": {"path": tempfile.gettempdir(), "error": exc.__class__.__name__},
        }


def _check_stdio() -> dict[str, Any]:
    """标准输出的编码。

    中文 Windows 上默认是 GBK，而转录里必然有 emoji 与各种语言的文字——
    编码不对时打印会抛 UnicodeEncodeError，把一次成功的扫描变成一次崩溃。
    `force_utf8_io()` 在入口处已经处理，这里只是确认它生效了。
    """
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    ok = "utf" in enc
    return {
        "key": "stdio",
        "level": OK if ok else WARN,
        "data": {"encoding": enc or "?", "platform": "windows" if IS_WINDOWS else "posix"},
    }


def _check_workspaces() -> list[dict[str, Any]]:
    """本机有没有多根工作区，以及它会不会让 `cwd` 变得不可信。

    为什么这值得一条诊断：多根工作区是**沉默的失真源**。Claude Code 的 VSCode
    扩展在多根工作区里把 `folders` 的第一个条目当作 cwd，其余根转成
    `--add-dir`；切换活动编辑器不改它，也没有任何配置项能覆盖。于是「在 A 目录
    启动、整场改 B 仓库」的会话，转录里的 `cwd` 一直指着 A。

    用户看不到这件事发生。他只会发现工具报的仓库不对，而原因藏在一个未文档化的
    扩展行为里。把它列进自检等于把这条因果关系摆到台面上。

    这一项**永不 FAIL**：多根工作区是合法用法，不是错误配置。发现了就是 WARN
    加一句解释，没发现就是 OK。
    """
    try:
        wm = WorkspaceMap.discover()
    except Exception:
        # 诊断自己崩掉最没道理。发现失败就当作「没有工作区」。
        return [{"key": "workspace.none", "level": OK, "data": {}}]
    groups = wm.groups()
    if not groups:
        return [{"key": "workspace.none", "level": OK, "data": {}}]
    out: list[dict[str, Any]] = []
    for group in groups:
        # 第一个根是那个窗口的 cwd，其余的是被 `--add-dir` 带进来的。
        # 分开说，因为用户能做的事不一样：想让 cwd 对准某个项目，就把它挪到
        # `folders` 第一位。
        out.append({
            "key": "workspace",
            "level": WARN,
            "data": {
                "lead": group[0],
                "others": ", ".join(group[1:]),
                # 其余根的个数，不含首根。文案里说的是「另外 N 个根」，
                # 给 `len(group)` 会把首根也数进去，读者对不上手里的清单。
                "count": len(group) - 1,
            },
        })
    return out


def run_doctor() -> dict[str, Any]:
    """把所有检查跑一遍，返回结构化结果。

    `level` 是整体结论：任一项 FAIL 则 FAIL，否则任一项 WARN 则 WARN。
    调用方据此决定退出码——但**WARN 不该让退出码非零**：缺 zstd 是可以正常
    工作的状态，让 CI 因为它红掉会逼人去关掉这条检查。
    """
    checks: list[dict[str, Any]] = [
        _check_python(),
        _check_git(),
        _check_zstd(),
        _check_stdio(),
        _check_writable(),
    ]
    checks.extend(_check_roots())
    checks.extend(_check_env())
    checks.extend(_check_workspaces())

    level = OK
    if any(c["level"] == FAIL for c in checks):
        level = FAIL
    elif any(c["level"] == WARN for c in checks):
        level = WARN

    # 转录总数单独给出来：首次上手时这一个数字回答了「工具能看见我的会话吗」，
    # 而那正是第一个要确认的事。
    total = sum(c["data"].get("count", 0) for c in checks if c["key"] == "root")
    return {"level": level, "checks": checks, "transcripts": total}
