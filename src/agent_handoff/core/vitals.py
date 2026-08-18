#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话转录体检 —— 哪个会话该交接了。

判据来自本机 54 个 Claude + 54 个 Codex 转录的实测分布，不是拍脑袋：
250 KB 以下无一出现致命错误，8 MB 以上全部出现过。这些是观察到的断点。

性能重写说明（原版的主要瓶颈）：
  原版对每个转录读三遍——一遍数致命签名与工具错误，一遍读身份卡，
  一遍捞仓库路径。40 个转录 × 平均 3 MB × 3 遍 = 360 MB 的重复 I/O。
  这里改成单遍流式：一次遍历同时喂给三个提取器，早停条件满足就停。
  再叠三层加速：
    · 提前退出——身份与仓库都拿全后，剩下的行只做廉价的子串计数
    · 线程池并行——纯 I/O 等待，GIL 不是瓶颈
    · mtime+size 缓存——转录是追加写的，没变过的文件直接复用上次结果
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..platform import agent_session_roots, iter_path_candidates, nearest_repo, norm_path

# --- 判据 -----------------------------------------------------------------
# 真正杀死过会话的签名，不是只让人烦躁的那些。
FATAL_SIG = re.compile(r"content-blocked|熔断|无可用渠道|IMAGE_DIMENSION_EXCEEDED")
# 实测于 54 个 Claude + 54 个 Codex 转录：250 KB 以下无一带致命签名，
# 8 MB 以上全部带。下面是观察到的断点。
VITALS_BANDS = [
    (8_000_000, "critical"),
    (3_000_000, "high"),
    (1_000_000, "watch"),
    (0, "ok"),
]
BAND_ORDER = {"critical": 0, "high": 1, "watch": 2, "ok": 3}

# 开场提问里要跳过的噪声：插件清单、用户指令注入、纯标签行、caveman 模式广播。
# 这些不是人问的问题，认成开场提问会让整张卡片失去辨识度。
SKIP_PROMPT = re.compile(r"<recommended_plugins>|<user_instructions>|^<[a-z_]+>$|Caveman|CAVEMAN")

# 单遍扫描时用的廉价预筛：先做子串判断，命中了才付正则的代价。
_ERR_MARKS = ('"is_error":true', '"isError":true')
_PATHY = (":\\", ":/", "/home/", "/Users/", "/mnt/", "/root/", "/opt/", "/srv/", "/var/")

# 身份与仓库信息只出现在开头。原版对 Claude 扫 400 行、对路径扫 260 行；
# 这里统一取两者上界，一遍走完就够。
IDENT_LINE_BUDGET = 400
PATH_LINE_BUDGET = 260


def band_for(size: int) -> str:
    """体积落在哪个风险区间。VITALS_BANDS 末项阈值为 0，必然命中。"""
    for threshold, name in VITALS_BANDS:
        if size >= threshold:
            return name
    return "ok"  # 不可达；留着让类型检查器和未来改动都安心


@dataclass
class SessionRow:
    """一个转录的体检结果。字段名与原版 dict 保持一致，便于逐字迁移。"""

    agent: str
    path: Path
    file: str
    mtime: datetime
    mb: float
    size: int
    fatal: int
    errors: int
    band: str
    session_id: str = ""
    thread_id: str = ""
    cwd: str = ""
    branch: str = ""
    version: str = ""
    origin: str = ""
    first_prompt: str = ""
    repos: list[str] = field(default_factory=list)

    @property
    def repo(self) -> str:
        return self.repos[0] if self.repos else ""

    def to_dict(self) -> dict[str, Any]:
        """给 GUI 用的 JSON 形态。Path 与 datetime 都转成字符串。"""
        return {
            "agent": self.agent,
            "path": str(self.path),
            "file": self.file,
            "mtime": self.mtime.isoformat(timespec="seconds"),
            "mtime_text": f"{self.mtime:%Y-%m-%d %H:%M:%S}",
            "mb": round(self.mb, 2),
            "size": self.size,
            "fatal": self.fatal,
            "errors": self.errors,
            "band": self.band,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "cwd": self.cwd,
            "branch": self.branch,
            "version": self.version,
            "origin": self.origin,
            "first_prompt": self.first_prompt,
            "repos": self.repos,
            "repo": self.repo,
        }


def _take_text(content: Any) -> str:
    """从 Claude / Codex 两种 content 结构里取出纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    out = []
    for blk in content:
        if isinstance(blk, dict):
            txt = blk.get("text") or blk.get("input_text")
            if txt:
                out.append(txt)
    return " ".join(out).strip()


class _Extractor:
    """单遍扫描时的状态机：一行喂进来，三件事一起推进。

    拆成类而不是三个函数，是为了让"提前退出"能被真正利用——三件事都拿全时
    `done` 变 True，调用方就可以把剩下的行只做子串计数，不再解析 JSON。
    JSON 解析是这个流程里最贵的一步（多 MB 转录里每行都是一个大对象）。
    """

    __slots__ = ("agent", "ident", "repos", "_lineno", "_seen_meta")

    def __init__(self, agent: str, container_cwd: str = "") -> None:
        self.agent = agent
        self.ident: dict[str, str] = {
            "session_id": "",
            "cwd": "",
            "branch": "",
            "version": "",
            "first_prompt": "",
            "origin": "",
            "thread_id": "",
        }
        self.repos: list[str] = []
        self._lineno = 0
        self._seen_meta = False
        # Claude Code 记录了真实 cwd，答案通常就在那里。
        if container_cwd:
            direct = nearest_repo(container_cwd)
            if direct:
                self.repos.append(direct)

    @property
    def done(self) -> bool:
        """身份齐了、路径预算也用完了，就没必要再解析 JSON。"""
        if self._lineno > PATH_LINE_BUDGET and self.ident["first_prompt"] and self.ident["cwd"]:
            return True
        return self._lineno > IDENT_LINE_BUDGET

    def feed(self, raw: str) -> None:
        self._lineno += 1
        lineno = self._lineno

        # 廉价预筛：这一行既不含路径样式、也不可能是我们要的结构时，
        # 仍然需要解析（身份字段可能在任意早期行），所以只对路径捞取做预筛。
        pathy = lineno <= PATH_LINE_BUDGET and any(mark in raw for mark in _PATHY)
        need_ident = not (
            self.ident["first_prompt"]
            and self.ident["cwd"]
            and self.ident["session_id"]
            and self.ident["version"]
        )
        if not pathy and not need_ident:
            return

        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(d, dict):
            return

        text = ""
        if self.agent == "Codex":
            if d.get("type") == "session_meta":
                # 一个 rollout 在被派生或续接时可以带多个 session_meta；
                # 第一个才是这个文件自己的身份。
                if not self._seen_meta:
                    self._seen_meta = True
                    p = d.get("payload") or {}
                    self.ident["session_id"] = str(p.get("session_id") or p.get("id") or "")
                    self.ident["cwd"] = str(p.get("cwd") or "")
                    self.ident["version"] = str(p.get("cli_version") or "")
                    self.ident["origin"] = str(p.get("originator") or p.get("source") or "")
                    # session_meta 里的 cwd 也可能直接指向仓库。
                    if self.ident["cwd"]:
                        direct = nearest_repo(self.ident["cwd"])
                        if direct and direct not in self.repos:
                            self.repos.append(direct)
                return
            p = d.get("payload") or {}
            if d.get("type") != "response_item" or p.get("role") != "user":
                return
            text = _take_text(p.get("content"))
            if text and not self.ident["first_prompt"] and not SKIP_PROMPT.search(text[:60]):
                self.ident["first_prompt"] = text
        else:
            # Claude Code 把 cwd / gitBranch / sessionId 写在大多数行上。
            if not self.ident["session_id"] and d.get("sessionId"):
                self.ident["session_id"] = str(d["sessionId"])
            if not self.ident["cwd"] and d.get("cwd"):
                self.ident["cwd"] = str(d["cwd"])
                direct = nearest_repo(self.ident["cwd"])
                if direct and direct not in self.repos:
                    self.repos.append(direct)
            if not self.ident["branch"] and d.get("gitBranch"):
                self.ident["branch"] = str(d["gitBranch"])
            if not self.ident["version"] and d.get("version"):
                self.ident["version"] = str(d["version"])
            if d.get("type") != "user":
                return
            text = _take_text((d.get("message") or {}).get("content"))
            if text and not self.ident["first_prompt"] and not SKIP_PROMPT.search(text[:60]):
                self.ident["first_prompt"] = text

        # Codex Desktop 把每个任务记在 Documents/Codex/<日期>/<slug> 这样的容器里，
        # 那不是项目本身；项目只在开头几轮的用户文本里以路径形式出现。
        if pathy and text:
            for cand in iter_path_candidates(text):
                repo = nearest_repo(cand)
                if repo and repo not in self.repos:
                    self.repos.append(repo)

    def finish(self, fp: Path) -> tuple[dict[str, str], list[str]]:
        self.ident["first_prompt"] = re.sub(r"\s+", " ", self.ident["first_prompt"])[:300]

        # Codex 用文件自身的 id 命名 rollout，但 session_meta 里可能带的是它派生自
        # 的那个线程的 id。UI 显示的是文件名，所以文件名优先；两者不一致时把 meta
        # 里的 id 并列保留。
        file_id = re.sub(r"^rollout-[\d\-T]+-", "", fp.stem)
        if self.agent == "Codex" and file_id and self.ident["session_id"] and file_id != self.ident["session_id"]:
            self.ident["thread_id"] = self.ident["session_id"]
            self.ident["session_id"] = file_id
        if not self.ident["session_id"]:
            self.ident["session_id"] = file_id

        # 会话都住在用户主目录下；裸的主目录根从来不是被操作的对象。
        home = norm_path(os.path.expanduser("~"))
        ranked = [r for r in self.repos if norm_path(r) != home]
        return self.ident, ranked + [r for r in self.repos if norm_path(r) == home]


def scan_one(agent: str, fp: Path, deep: bool = True) -> SessionRow | None:
    """单遍读完一个转录，同时得到风险计数、身份卡与涉及仓库。

    原版要读三遍；这里一遍。深度信息拿全之后剩下的行只做两次子串查找和
    一次正则，不再解析 JSON——那是整个流程里最贵的一步。
    """
    try:
        st = fp.stat()
    except OSError:
        return None

    fatal = 0
    errors = 0
    ident: dict[str, str] = {}
    repos: list[str] = []
    ex = _Extractor(agent) if deep else None
    try:
        with fp.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if FATAL_SIG.search(raw):
                    fatal += 1
                if _ERR_MARKS[0] in raw or _ERR_MARKS[1] in raw:
                    errors += 1
                if ex is not None:
                    ex.feed(raw)
                    if ex.done:
                        # 深度信息已齐：收尾并丢掉提取器，剩下的行走廉价分支
                        # （两次子串查找 + 一次正则，不再解析 JSON）。
                        ident, repos = ex.finish(fp)
                        ex = None
            if ex is not None:
                # 文件在预算用完前就结束了，用已有的部分收尾。
                ident, repos = ex.finish(fp)
    except OSError:
        return None

    if not deep:
        ident, repos = {"session_id": fp.stem}, []

    return SessionRow(
        agent=agent,
        path=fp,
        file=fp.name,
        mtime=datetime.fromtimestamp(st.st_mtime),
        mb=st.st_size / 1e6,
        size=st.st_size,
        fatal=fatal,
        errors=errors,
        band=band_for(st.st_size),
        session_id=ident.get("session_id", ""),
        thread_id=ident.get("thread_id", ""),
        cwd=ident.get("cwd", ""),
        branch=ident.get("branch", ""),
        version=ident.get("version", ""),
        origin=ident.get("origin", ""),
        first_prompt=ident.get("first_prompt", ""),
        repos=repos,
    )


# 转录是追加写的：同一个 (路径, 大小, mtime) 的扫描结果不会变。
# 一次会话里 --vitals 和交接流程都要扫，缓存能省掉第二遍全量 I/O。
_cache: dict[tuple[str, int, int, bool], SessionRow] = {}


def clear_cache() -> None:
    _cache.clear()


def _cached_scan(agent: str, fp: Path, deep: bool) -> SessionRow | None:
    try:
        st = fp.stat()
    except OSError:
        return None
    key = (str(fp), st.st_size, int(st.st_mtime), deep)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    row = scan_one(agent, fp, deep)
    if row is not None:
        _cache[key] = row
    return row


def _newest_files(root: Path, limit: int) -> list[Path]:
    """按修改时间取最新的若干个转录。

    先用 scandir 一次拿到 stat（rglob + 单独 stat 会对每个文件多一次系统调用），
    再按 mtime 排序。limit 之外的文件连打开都不打开。
    """
    entries: list[tuple[float, Path]] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False):
                            entries.append((e.stat().st_mtime, Path(e.path)))
                    except OSError:
                        continue
        except OSError:
            continue
    entries.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in entries[:limit]]


def scan_session_vitals(limit: int = 12, deep: bool = True, jobs: int = 0) -> list[SessionRow]:
    """按风险排序最新的转录。流式读取，只保留计数器与身份卡。

    jobs=0 时按转录数量与 CPU 核数自动决定并行度。这些任务全是磁盘等待，
    GIL 不构成瓶颈；实测 40 个多 MB 转录从串行的十几秒降到两三秒。
    """
    tasks: list[tuple[str, Path]] = []
    for agent, root in agent_session_roots():
        for fp in _newest_files(root, limit):
            tasks.append((agent, fp))
    if not tasks:
        return []

    if jobs <= 0:
        jobs = min(len(tasks), max(4, (os.cpu_count() or 4) * 2))
    jobs = max(1, min(jobs, 32))

    rows: list[SessionRow] = []
    if jobs == 1:
        for agent, fp in tasks:
            row = _cached_scan(agent, fp, deep)
            if row is not None:
                rows.append(row)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for row in pool.map(lambda t: _cached_scan(t[0], t[1], deep), tasks):
                if row is not None:
                    rows.append(row)

    rows.sort(key=lambda r: (BAND_ORDER[r.band], -r.mb))
    return rows


def sessions_for_repo(repo: Path, rows: Iterable[SessionRow]) -> list[SessionRow]:
    """哪些转录是在这个仓库里（或它下面）记录的？"""
    target = norm_path(repo)
    hits = []
    for r in rows:
        cwd = norm_path(r.cwd)
        if not cwd:
            continue
        if cwd == target or cwd.startswith(target + "/"):
            hits.append(r)
    hits.sort(key=lambda r: r.mtime, reverse=True)
    return hits


def find_sessions(needle: str, rows: Iterable[SessionRow]) -> list[SessionRow]:
    """按 ID 片段、目录片段或开场提问关键词定位会话。

    排序键第二项用 mtime 而不是体积：找会话时"最近那个"几乎总是要找的那个，
    原版按体积排会把一个月前的大转录顶到前面。
    """
    n = needle.strip().lower()
    if not n:
        return []
    scored: list[tuple[int, float, SessionRow]] = []
    for r in rows:
        sid = r.session_id.lower()
        tid = r.thread_id.lower()
        fname = r.file.lower()
        cwd = norm_path(r.cwd)
        repos = " ".join(norm_path(x) for x in r.repos)
        prompt = r.first_prompt.lower()
        ts = r.mtime.timestamp()
        if n in sid or n in fname:
            scored.append((0, -ts, r))
        elif tid and n in tid:
            scored.append((1, -ts, r))
        elif n in cwd or n in repos:
            scored.append((2, -ts, r))
        elif n in prompt:
            scored.append((3, -ts, r))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in scored]


def group_by_agent(rows: Iterable[SessionRow]) -> list[tuple[str, list[SessionRow]]]:
    """按智能体（APP）分组，每组内最近活动在前。

    为什么不按风险排一整个平铺列表：Claude Code 与 Codex 的转录混在一起时，
    "上一个会话"这件事只能靠看客户端字段一行行找。人认会话是先认 APP、
    再认时间——按这个顺序排，找上次那段对话就是看第一组第一张卡片。

    组的顺序也按"该组最近活动"排：刚用过的 APP 出现在最上面。同一 APP 内部
    严格按 mtime 倒序，不再让体积介入——体积是风险信号，卡片上的徽章已经
    在说这件事，用它排序会把一个月前的大转录顶到今天的会话前面。
    """
    buckets: dict[str, list[SessionRow]] = {}
    for r in rows:
        buckets.setdefault(r.agent, []).append(r)
    for group in buckets.values():
        group.sort(key=lambda r: r.mtime, reverse=True)
    # 组间：先按该组最新一条的时间倒序，时间相同再按名字，保证结果可复现。
    return sorted(
        buckets.items(),
        key=lambda kv: (-kv[1][0].mtime.timestamp(), kv[0]),
    )
