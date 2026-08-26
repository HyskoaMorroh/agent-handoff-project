#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区分组的发现：哪些仓库其实是同一个窗口里并列打开的。

为什么需要这个模块。

Claude Code 的 VSCode 扩展在多根工作区里把 `workspaceFolders[0]`——即
`.code-workspace` 里 `folders` 的**第一个**条目——当作 cwd，其余根转成
`--add-dir`。这个行为在官方文档里没有任何记载，是从扩展代码（`extension.js`
的 `realpathSync(Y[0] || homedir())`）读出来的；切换活动编辑器不改它，扩展也
没有任何 cwd 相关的配置项可以覆盖。

后果：用户在 A 目录启动、整场在改 B 仓库时，转录里的 `cwd` 一直指着 A。而
`cwd` 恰恰是此前判定「这个会话属于哪个项目」的唯一依据。

这个模块要回答的问题不是「工作目标是哪个」——那是 `attribution` 的事——而是
**「这个 cwd 可不可信」**。如果 cwd 所在的仓库是某个多根工作区的一个根，那么
cwd 只是那个工作区的第一个条目，它没有携带「用户在改什么」的信息，应当被降权；
同时该工作区里的**其他根**是有价值的候选，应当摆出来给用户看。

两个发现来源，都便宜且只读：

  · `~/.claude/ide/*.lock`——扩展为每个 VSCode 窗口写一份，内含该窗口的
    `workspaceFolders` 全量数组。这是**上游自己写下的事实**，不是推断。
    进程结束后锁文件常常留着，那反而有用：它记录了一个曾经存在过的工作区分组，
    而历史会话正是在那种分组下跑的。
  · `*.code-workspace`——用户手写的工作区定义。它比锁文件更持久（锁文件会被
    清理），而且在工具从未与那个窗口共存过时也能读到。

刻意**依赖**根的顺序，但只依赖到必要的程度：锁文件里的数组顺序就是 `folders`
顺序，而扩展固定取第一个。所以「cwd 等于某个多根组的**第一个**根」才说明这个
会话可能来自那个工作区窗口；cwd 等于某个靠后的根时，它**不可能**是那个窗口的
cwd（扩展不会选它），那种情况下 cwd 反而是可信的——它来自一个单根窗口。

不这样区分的代价是实测过的：一个仓库既被单独打开过、又出现在某个工作区里时，
无条件降权会把单根窗口下**正确**的 cwd 也标成「工作区的一个根」。那不会算出
错的仓库（降权只是把它排到 `mention` 之前），但会给用户一句不准确的说明。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

from ..platform import nearest_repo, norm_path

# 找 `.code-workspace` 时的搜索深度。
#
# 取 3 而不是更深：工作区文件是人手动存的，人会把它放在能找到的地方——家目录、
# 桌面、项目的父目录。深度 3 从家目录出发已经覆盖 `~/Desktop/proj/x.code-workspace`
# 这种。再深就是在扫整块盘，而这个函数在每次扫描会话时都会被问到。
WORKSPACE_SCAN_DEPTH = 3
# 搜索时跳过的目录。与 `plan.PLAN_SKIP_DIRS` 分开维护：这里要跳的是「体积大且
# 不可能放工作区文件」的目录，判据不同。
WORKSPACE_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".hg", ".svn", "__pycache__", ".venv", "venv",
    "dist", "build", "out", "target", "site-packages", ".pytest_cache",
    "AppData", "Application Data", "$Recycle.Bin", "System Volume Information",
    ".cache", ".npm", ".gradle", ".m2", ".cargo", ".rustup", ".nuget",
    "OneDriveTemp", "Windows", "Program Files", "Program Files (x86)",
})
# `.code-workspace` 是 JSONC——允许注释与尾逗号。标准库没有 JSONC 解析器，
# 而为了读一个 `folders` 数组引入依赖不值得（本项目零运行时依赖）。
# 这两条正则做最小清理：去掉整行注释与行尾注释，去掉尾逗号。
#
# 不处理字符串内部含 `//` 的情况（Windows UNC 路径 `\\\\server\\share` 在 JSON
# 里是 `\\\\\\\\server`，不含裸 `//`；而 `https://` 出现在 `folders` 里没有意义）。
# 解析失败就跳过这个文件——读不到一个工作区定义只是少一条线索，不该让扫描失败。
_LINE_COMMENT = re.compile(r"(?m)^\s*//[^\n]*$|(?<=[\s,{}\[\]\"])//[^\n]*$")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _lock_dir() -> Path:
    """扩展写锁文件的目录。`CLAUDE_CONFIG_DIR` 生效时跟着它走。"""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(env) if env else (Path.home() / ".claude")
    return root / "ide"


def ide_workspace_groups() -> list[list[str]]:
    """从 `~/.claude/ide/*.lock` 读出每个 VSCode 窗口的工作区根列表。

    返回的每一项是**一个窗口**的根列表，保持锁文件里的原始写法（不规范化，
    调用方按需处理）。只有一个根的窗口也返回——调用方需要区分「单根窗口」与
    「多根工作区」，那正是 cwd 可不可信的分界。

    读不到就返回空列表：没装 VSCode 扩展、锁目录不存在、权限不足都算这种情况，
    此时工作区发现只是少一个来源，不影响别的判定。
    """
    out: list[list[str]] = []
    try:
        entries = sorted(_lock_dir().glob("*.lock"))
    except OSError:
        return out
    for fp in entries:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 锁文件可能正在被写、也可能是别的版本换了格式。跳过。
            continue
        if not isinstance(data, dict):
            continue
        roots = data.get("workspaceFolders")
        if not isinstance(roots, list):
            continue
        group = [str(r) for r in roots if isinstance(r, str) and r.strip()]
        if group:
            out.append(group)
    return out


def _parse_workspace_file(fp: Path) -> list[str]:
    """读一个 `.code-workspace`，返回它声明的根路径（已相对该文件解析）。"""
    try:
        raw = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    cleaned = _LINE_COMMENT.sub("", raw)
    cleaned = _TRAILING_COMMA.sub(r"\1", cleaned)
    try:
        data = json.loads(cleaned)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    folders = data.get("folders")
    if not isinstance(folders, list):
        return []
    out: list[str] = []
    for item in folders:
        if not isinstance(item, dict):
            continue
        p = item.get("path")
        if not isinstance(p, str) or not p.strip():
            continue
        # `folders[].path` 可以是相对路径，相对于工作区文件所在目录。
        # 实测本机那份用的正是 `../agent-handoff-project`——不解析就完全对不上。
        cand = Path(p)
        if not cand.is_absolute():
            cand = fp.parent / cand
        try:
            out.append(str(cand.resolve()))
        except OSError:
            out.append(str(cand))
    return out


def find_workspace_files(seeds: Iterable[Path]) -> list[Path]:
    """在给定的起点下找 `.code-workspace` 文件。

    `seeds` 通常是家目录加上待判定仓库的父目录：工作区文件要么被放在家目录附近
    （用户存盘时的默认位置），要么就在项目旁边。
    """
    found: list[Path] = []
    seen: set[str] = set()
    for seed in seeds:
        try:
            base = Path(seed).resolve()
        except OSError:
            continue
        if not base.is_dir():
            continue
        key = norm_path(str(base))
        if key in seen:
            continue
        seen.add(key)
        for root, dirs, files in os.walk(base):
            try:
                depth = len(Path(root).relative_to(base).parts)
            except ValueError:
                dirs[:] = []
                continue
            if depth >= WORKSPACE_SCAN_DEPTH:
                dirs[:] = []
            else:
                dirs[:] = [
                    d for d in dirs
                    if d not in WORKSPACE_SKIP_DIRS and not d.startswith("$")
                ]
            for fn in files:
                if fn.endswith(".code-workspace"):
                    found.append(Path(root) / fn)
    return found


class WorkspaceMap:
    """哪些仓库在同一个窗口里并列打开过。

    构造一次、多处复用：`scan_session_vitals` 扫几百个转录时，每个都要问
    「这个 cwd 是不是多根工作区的一个根」，而答案对整轮扫描是不变的。
    """

    __slots__ = ("_groups", "_index", "_leads")

    def __init__(self, groups: Iterable[Iterable[str]]) -> None:
        # 每组是一个规范化后的仓库根集合。
        #
        # 为什么按**仓库**而不是按原始路径分组：工作区的根可以是仓库的子目录
        # （`folders` 指向 `repo/packages/web` 是常见写法），而我们要回答的问题
        # 是仓库级的。`nearest_repo` 把根提升到仓库；提升不上去的（不在任何
        # git 仓库里的目录）直接丢——它们不可能是「在改哪个仓库」的答案。
        self._groups: list[frozenset[str]] = []
        self._index: dict[str, set[str]] = {}
        # 每个多根组的**第一个**仓库。扩展固定取 `folders[0]` 作 cwd，所以只有
        # 这些仓库会以「工作区的 cwd」身份出现。
        self._leads: set[str] = set()
        for group in groups:
            repos: list[str] = []
            for raw in group:
                repo = nearest_repo(raw)
                if not repo:
                    continue
                key = norm_path(repo)
                if key not in repos:
                    repos.append(key)
            # 单个仓库的组不进索引：那是单根窗口，cwd 在那里是可信的，
            # 而这个类存在的全部意义是标出**不**可信的那些。
            if len(repos) < 2:
                continue
            frozen = frozenset(repos)
            if frozen not in self._groups:
                self._groups.append(frozen)
                for r in repos:
                    self._index.setdefault(r, set()).update(set(repos) - {r})
            # 首个根即使在重复组里也要记：同一个工作区可能同时有锁文件与
            # `.code-workspace` 两个来源，组相同但都该确认首根。
            self._leads.add(repos[0])

    @classmethod
    def discover(cls, extra_seeds: Iterable[Path] = ()) -> WorkspaceMap:
        """从本机的锁文件与 `.code-workspace` 文件构造。

        两个来源都是「有就用，没有就算」：锁文件要求装了 VSCode 扩展，
        工作区文件要求用户手动存过。一个都找不到时返回空映射，此时所有 cwd
        都按可信处理——那正是单根窗口下的正确行为。
        """
        groups: list[list[str]] = list(ide_workspace_groups())
        seeds: list[Path] = [Path.home(), *extra_seeds]
        for fp in find_workspace_files(seeds):
            roots = _parse_workspace_file(fp)
            if len(roots) >= 2:
                groups.append(roots)
        return cls(groups)

    def __bool__(self) -> bool:
        return bool(self._groups)

    def groups(self) -> list[list[str]]:
        """全部多根组，每组第一个是那个窗口的 cwd（`folders[0]`）。

        给自检用：把发现到的工作区摆出来，让用户看到「哦，这两个项目被工具
        认成一个窗口了」。顺序稳定（按首根路径排），这样两次运行的输出一致。
        """
        out: list[list[str]] = []
        for lead in sorted(self._leads):
            others = sorted(self._index.get(lead, ()))
            if others:
                out.append([lead, *others])
        return out

    def is_multi_root(self, repo: str) -> bool:
        """这个仓库是否曾在某个多根工作区里被并列打开。

        为真时它出现在同一个窗口里，但**不必然**是那个窗口的 cwd——扩展只取
        `folders[0]`。要判断「这个 cwd 是不是工作区带来的」用 `is_workspace_cwd`。
        """
        if not repo:
            return False
        return norm_path(repo) in self._index

    def is_workspace_cwd(self, repo: str) -> bool:
        """这个仓库是否是某个多根工作区的**第一个**根。

        只有这种仓库会以 cwd 的身份出现在多根工作区的会话里：扩展固定取
        `workspaceFolders[0]`。为真时那个 cwd 几乎不携带「在改什么」的信息——
        它只说明这个文件夹在 `folders` 里排第一。

        排在后面的根即使属于同一个工作区，也不会成为该窗口的 cwd。所以当 cwd
        等于某个靠后的根时，它一定来自**别的**（单根）窗口，此时 cwd 是可信的。
        """
        if not repo:
            return False
        return norm_path(repo) in self._leads

    def siblings(self, repo: str) -> list[str]:
        """与这个仓库同处一个工作区的其他仓库，按路径排序（可复现）。

        它们是「用户可能实际在改的地方」的候选。没有别的证据时，把这份清单
        摆出来比只显示一个 cwd 有用——用户认得出哪个是自己的项目。
        """
        if not repo:
            return []
        return sorted(self._index.get(norm_path(repo), ()))
