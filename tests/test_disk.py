#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""磁盘占用扫描。重点：

  · 只 stat 不读内容——这是它能在 1 GB 转录上十毫秒级完成的唯一原因
  · 分类判据（子代理 / 已归档 / 空）必须与 vitals 的同名判断一致
  · 永不删除任何文件
"""
from __future__ import annotations

from pathlib import Path

from agent_handoff.core.disk import EMPTY_BYTES, HUGE_BYTES, by_repo, scan_disk


def _mk(fp: Path, size: int) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    # 写真实字节而不是 truncate：稀疏文件在某些文件系统上 st_size 与实际不符，
    # 而这个模块的全部结论都建立在 st_size 上。
    fp.write_bytes(b"x" * size)


def _home(monkeypatch, root: Path) -> None:
    monkeypatch.setattr("os.path.expanduser", lambda p: str(root) if p == "~" else p)
    monkeypatch.setattr("agent_handoff.platform.IS_WINDOWS", True)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def test_scan_counts_size_and_finds_every_transcript(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    _mk(home / ".claude" / "projects" / "C--p" / "a.jsonl", 1000)
    _mk(home / ".codex" / "sessions" / "2026" / "08" / "22" / "rollout-x-1.jsonl", 2000)
    # 非 .jsonl 不算：转录目录里还有别的文件（锁、索引、临时文件）。
    _mk(home / ".claude" / "projects" / "C--p" / "notes.txt", 9999)
    _home(monkeypatch, home)

    rep = scan_disk()
    assert len(rep.rows) == 2
    assert rep.total_bytes == 3000


def test_scan_classifies_reclaimable_kinds(monkeypatch, tmp_path: Path):
    """三类的判据各自独立，混在一起会让「安全」程度失去意义。"""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "C--p"
    _mk(proj / "main.jsonl", EMPTY_BYTES + 10)                    # 普通会话
    _mk(proj / "subagents" / "agent-1.jsonl", 5000)               # 目录判定
    _mk(proj / "agent-2.jsonl", 5000)                             # 文件名判定
    _mk(proj / "tiny.jsonl", 100)                                 # 空会话
    _mk(home / ".codex" / "archived_sessions" / "rollout-a.jsonl", EMPTY_BYTES + 10)
    _home(monkeypatch, home)

    rep = scan_disk()
    kinds = dict(rep.reclaimable())
    assert {r.path.name for r in kinds["subagent"]} == {"agent-1.jsonl", "agent-2.jsonl"}
    assert {r.path.name for r in kinds["archived"]} == {"rollout-a.jsonl"}
    assert {r.path.name for r in kinds["empty"]} == {"tiny.jsonl"}


def test_subagent_is_not_double_counted_as_empty(monkeypatch, tmp_path: Path):
    """一个既小又属于子代理的转录只该出现在一类里，否则「可回收」会被重复累加。"""
    home = tmp_path / "home"
    _mk(home / ".claude" / "projects" / "C--p" / "agent-tiny.jsonl", 50)
    _home(monkeypatch, home)

    rep = scan_disk()
    kinds = dict(rep.reclaimable())
    assert len(kinds.get("subagent", [])) == 1
    assert "empty" not in kinds, "子代理已经计入 subagent，不能再计一次"


def test_biggest_is_sorted_desc_and_flags_huge(monkeypatch, tmp_path: Path):
    """排行榜是这个功能的主产出：实测本机单个 90 MB 文件占总量 8%，
    而「超过 30 天」是 0 个——按体积排比按时间过期有用。"""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "C--p"
    _mk(proj / "small.jsonl", 1000)
    _mk(proj / "big.jsonl", HUGE_BYTES + 1)
    _mk(proj / "mid.jsonl", 50_000)
    _home(monkeypatch, home)

    rep = scan_disk()
    sizes = [r.size for r in rep.biggest]
    assert sizes == sorted(sizes, reverse=True)
    assert rep.biggest[0].path.name == "big.jsonl"
    assert rep.biggest[0].is_huge
    assert not rep.biggest[-1].is_huge


def test_scan_survives_unreadable_directory(monkeypatch, tmp_path: Path):
    """一个目录读不了不能让整次扫描失败——少统计一个目录比中途退出好。"""
    home = tmp_path / "home"
    _mk(home / ".claude" / "projects" / "C--p" / "a.jsonl", 500)
    _home(monkeypatch, home)

    real = __import__("os").scandir
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr("agent_handoff.core.disk.os.scandir", flaky)
    rep = scan_disk()          # 不该抛异常
    assert isinstance(rep.total_bytes, int)


def test_by_repo_merges_paths_differing_only_in_case(monkeypatch, tmp_path: Path):
    """转录里的 cwd 直接来自当时的 shell，盘符大小写不固定。

    不归一化会让同一个仓库分成两行，用户以为是两个项目。
    """
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "C--p"
    _mk(proj / "a.jsonl", 1000)
    _mk(proj / "b.jsonl", 2000)
    _home(monkeypatch, home)

    rep = scan_disk()
    paths = sorted(str(r.path) for r in rep.rows)
    from agent_handoff.platform import norm_path
    cwd_of = {
        norm_path(paths[0]): r"c:\Users\me\proj",
        norm_path(paths[1]): r"C:\Users\me\proj",
    }
    got = by_repo(rep, cwd_of)
    repos = [name for name, _, _ in got]
    assert len(repos) == 1, got
    assert got[0][1] == 2
    assert got[0][2] == 3000


def test_by_repo_groups_unknown_instead_of_splitting_by_date(monkeypatch, tmp_path: Path):
    """拿不到 cwd 时要合成一类。

    原先退回存放目录名，而 Codex 的布局是 `年/月/日`，聚合表里就冒出 `21`、
    `22` 这种行——读者无法判断那是什么，比明说「不知道」更糟。
    """
    home = tmp_path / "home"
    for day in ("21", "22"):
        _mk(home / ".codex" / "sessions" / "2026" / "08" / day / f"rollout-{day}.jsonl", 1000)
    _home(monkeypatch, home)

    rep = scan_disk()
    got = by_repo(rep, {})          # 空映射：一个 cwd 都解析不出来
    assert [name for name, _, _ in got] == ["<unknown>"]
    assert got[0][1] == 2


def test_by_repo_keeps_subagents_separate(monkeypatch, tmp_path: Path):
    """子代理不摊到各仓库上，否则每个仓库的数字都虚高。"""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "C--p"
    _mk(proj / "main.jsonl", 1000)
    _mk(proj / "agent-1.jsonl", 4000)
    _home(monkeypatch, home)

    rep = scan_disk()
    from agent_handoff.platform import norm_path
    cwd_of = {norm_path(str(r.path)): "/repo" for r in rep.rows}
    got = {name: size for name, _, size in by_repo(rep, cwd_of)}
    assert got["/repo"] == 1000, "子代理不能计入仓库占用"
    assert got["<subagents>"] == 4000


def test_scan_respects_limit_per_root(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    for i in range(5):
        _mk(home / ".claude" / "projects" / "C--p" / f"s{i}.jsonl", 100)
    _home(monkeypatch, home)

    assert len(scan_disk(limit=2).rows) == 2
    assert len(scan_disk().rows) == 5      # 0 = 不限


def test_scan_reports_elapsed_time(monkeypatch, tmp_path: Path):
    """耗时要报出来：这个功能的卖点就是「只读元信息所以很快」，
    不显示耗时用户无从判断它是不是真的没读内容。"""
    home = tmp_path / "home"
    _mk(home / ".claude" / "projects" / "C--p" / "a.jsonl", 100)
    _home(monkeypatch, home)

    rep = scan_disk()
    assert rep.elapsed_ms >= 0.0
    assert rep.roots, "扫过的位置要一并报出来，否则用户不知道它找了哪里"
