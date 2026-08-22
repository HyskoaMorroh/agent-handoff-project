#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""磁盘占用扫描：转录吃了多少空间，哪些可以安全回收。

与 `vitals` 分开是因为两者的成本模型完全不同。`vitals` 要读文件内容才能算出
上下文占用；这里只需要 `stat`。实测本机 423 个转录、1.09 GB：

    只遍历目录          8 ms
    遍历 + stat        10 ms      <- 本模块
    读每个文件前 64 KB  219 ms     <- 慢 22 倍

所以这里一行内容都不读，`--sweep` 在 1 GB 的转录上是十毫秒级的操作。想知道
「哪些会话的结论已经落进仓库」需要读内容，那是 `vitals` 的活，不混进来。

**本模块永不删除任何文件。** 转录里可能存着唯一一份工作记录，而删除不可逆；
判断哪些真能丢需要人看一眼。所以这里只统计、分类、排序，并给出一条可复制的
命令让用户自己执行。
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..platform import agent_session_roots, norm_path

# 「巨型」的门槛。实测本机最大单份 90 MB，而中位数不到 1 MB——真正吃磁盘的
# 永远是少数几个文件，所以排行榜比按时间过期有用得多（本机「超过 30 天」是 0 个）。
HUGE_BYTES = 50 * 1024 * 1024
# 「空会话」门槛。低于这个体积的转录装不下一轮有内容的对话，实测都是开了就关
# 或者只有一句话的会话。32 KB 是保守取值：宁可少报，不可把有内容的判成空的。
EMPTY_BYTES = 32 * 1024
# 排行榜列多少条。再多就要翻屏，而翻屏找大文件不如直接看前几名。
TOP_N = 12


@dataclass
class DiskRow:
    """一个转录的磁盘视角。刻意不含任何需要读内容才能得到的字段。"""

    agent: str
    path: Path
    size: int
    mtime: datetime
    # 子代理的工作记录，不是人的对话。数量远超主会话——实测 423 个里 77 个。
    is_subagent: bool = False
    # Codex 归档目录里的会话。归档只是移出活动列表，转录本身还在，
    # 但它已经不能原生续接了。
    is_archived: bool = False

    @property
    def mb(self) -> float:
        return self.size / 1e6

    @property
    def is_empty(self) -> bool:
        return self.size < EMPTY_BYTES

    @property
    def is_huge(self) -> bool:
        return self.size >= HUGE_BYTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "path": str(self.path),
            "file": self.path.name,
            "size": self.size,
            "mb": round(self.mb, 2),
            "mtime": self.mtime.isoformat(timespec="seconds"),
            "mtime_text": f"{self.mtime:%Y-%m-%d %H:%M:%S}",
            "is_subagent": self.is_subagent,
            "is_archived": self.is_archived,
            "is_empty": self.is_empty,
            "is_huge": self.is_huge,
        }


@dataclass
class DiskReport:
    """一次扫描的全部结论。"""

    rows: list[DiskRow] = field(default_factory=list)
    roots: list[tuple[str, Path]] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def total_bytes(self) -> int:
        return sum(r.size for r in self.rows)

    @property
    def biggest(self) -> list[DiskRow]:
        """占用排行榜。按体积降序，最大的在前。"""
        return sorted(self.rows, key=lambda r: -r.size)[:TOP_N]

    def group(self, kind: str) -> list[DiskRow]:
        """可回收分类。三类各自的「安全」程度不同，所以分开而不是合成一堆。"""
        if kind == "subagent":
            return [r for r in self.rows if r.is_subagent]
        if kind == "archived":
            return [r for r in self.rows if r.is_archived and not r.is_subagent]
        if kind == "empty":
            return [r for r in self.rows if r.is_empty and not r.is_subagent]
        raise ValueError(f"unknown group: {kind}")

    def reclaimable(self) -> list[tuple[str, list[DiskRow]]]:
        """按「删了最不心疼」的顺序给出三类。空列表的分类不出现。"""
        out = []
        for kind in ("subagent", "archived", "empty"):
            got = self.group(kind)
            if got:
                out.append((kind, got))
        return out


def scan_disk(limit: int = 0) -> DiskReport:
    """扫一遍所有转录的体积与时间。只 stat，不读内容。

    `limit` 是每个根目录最多看多少个文件，0 表示不限。默认不限：这个扫描本身
    是十毫秒级的，截断反而会让「总共占了多少」这个最主要的问题答错。

    用显式栈而不是递归的 `rglob`：转录目录的层级由应用决定
    （Codex 是 `年/月/日`，Claude 是一层 slug 目录），递归深度不可控，
    而 `rglob("*.jsonl")` 在有大量非转录文件的目录上会白扫一遍。
    """
    import time

    started = time.perf_counter()
    roots = agent_session_roots()
    rows: list[DiskRow] = []
    seen: set[str] = set()

    for agent, root in roots:
        found = 0
        stack: list[Path] = [root]
        while stack:
            if limit and found >= limit:
                break
            here = stack.pop()
            try:
                entries = list(os.scandir(here))
            except OSError:
                # 权限不足或目录刚被删掉。跳过这一个，不让整次扫描失败——
                # 「扫到一半报错退出」比「少统计一个目录」更糟。
                continue
            for e in entries:
                if limit and found >= limit:
                    break
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                        continue
                    if not e.name.endswith(".jsonl"):
                        continue
                    st = e.stat()
                except OSError:
                    continue
                key = norm_path(e.path)
                if key in seen:
                    # 同一个文件可能被两个根目录同时覆盖（环境变量指到家目录里）。
                    continue
                seen.add(key)
                fp = Path(e.path)
                parts = {p.lower() for p in fp.parts}
                rows.append(DiskRow(
                    agent=agent,
                    path=fp,
                    size=st.st_size,
                    mtime=datetime.fromtimestamp(st.st_mtime),
                    is_subagent="subagents" in parts or fp.name.lower().startswith("agent-"),
                    is_archived="archived_sessions" in parts,
                ))
                found += 1

    return DiskReport(
        rows=rows,
        roots=roots,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def by_repo(report: DiskReport, cwd_of: dict[str, str] | None = None) -> list[tuple[str, int, int]]:
    """按仓库聚合占用，返回 (仓库, 文件数, 字节数)，占用大的在前。

    `cwd_of` 把转录路径映射到它工作过的目录，由调用方从 vitals 结果里传进来——
    本模块不读文件内容，自己拿不到 cwd。

    映射不全是常态：vitals 有 `--limit`，多数转录不在它的结果里。那些转录
    **合并成一类**而不是按存放目录散开。原先退回 `path.parent.name`，Codex 的
    存放布局是 `年/月/日`，于是聚合结果里冒出 `21`、`22`、`18` 这种行——
    读者完全无法判断那是什么，比明说「不知道」更糟。
    """
    buckets: dict[str, list[DiskRow]] = defaultdict(list)
    # 同一个仓库可能以不同大小写出现（`c:\proj` 与 `C:\proj`——转录里的 cwd
    # 直接来自当时的 shell，盘符大小写不固定）。按归一化后的路径归组，
    # 但显示时用第一次见到的原样写法，不把用户的路径改写成小写。
    display: dict[str, str] = {}
    for r in report.rows:
        if r.is_subagent:
            # 子代理归到它的主会话名下会更准，但那需要读内容。这里单独成组，
            # 免得把 77 个子代理摊到各仓库上，让每个仓库的数字都虚高。
            buckets["<subagents>"].append(r)
            display.setdefault("<subagents>", "<subagents>")
            continue
        raw = ""
        if cwd_of:
            raw = cwd_of.get(norm_path(str(r.path)), "")
        key = norm_path(raw) if raw else "<unknown>"
        buckets[key].append(r)
        display.setdefault(key, raw or "<unknown>")

    out = [(display[k], len(v), sum(x.size for x in v)) for k, v in buckets.items()]
    out.sort(key=lambda t: -t[2])
    return out
