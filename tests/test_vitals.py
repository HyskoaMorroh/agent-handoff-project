#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转录扫描。重点：
  · 单遍重写的结果必须与原版三遍逐字一致（身份卡、仓库推断、致命计数）
  · POSIX 路径必须能被认出（原版只认盘符，Linux 上永远推断不出仓库）
  · 判定区间边界严格按实测断点
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from agent_handoff.core.vitals import (
    BAND_ORDER,
    VITALS_BANDS,
    _newest_files,
    band_for,
    clear_cache,
    find_sessions,
    scan_one,
    scan_session_vitals,
    sessions_for_repo,
)
from agent_handoff.platform import iter_path_candidates


def _write_jsonl(fp: Path, rows: list[dict]) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _pad(fp: Path, target: int) -> None:
    """把文件填到指定体积，用于测试判定区间。填充行是合法 JSON，不含任何信号。"""
    with fp.open("a", encoding="utf-8") as fh:
        filler = json.dumps({"type": "noise", "pad": "x" * 400}) + "\n"
        while fp.stat().st_size < target:
            fh.write(filler * 50)
            fh.flush()


# ── 判定区间 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "size,band",
    [
        (0, "ok"),
        (250_000, "ok"),
        (999_999, "ok"),
        (1_000_000, "watch"),
        (2_999_999, "watch"),
        (3_000_000, "high"),
        (7_999_999, "high"),
        (8_000_000, "critical"),
        (50_000_000, "critical"),
    ],
)
def test_band_boundaries(size, band):
    assert band_for(size) == band


def test_bands_cover_zero():
    """末项阈值必须是 0，否则小文件会落空。原版靠这个不变式才不会 band 未设。"""
    assert VITALS_BANDS[-1][0] == 0
    assert set(BAND_ORDER) == {name for _, name in VITALS_BANDS}


# ── 跨平台路径识别 ────────────────────────────────────────────────────

def test_iter_path_candidates_finds_windows_paths():
    got = list(iter_path_candidates(r'请看 C:\Users\me\proj\file.py 里的问题'))
    assert any(c.startswith("C:\\Users\\me\\proj") for c in got)


def test_iter_path_candidates_finds_posix_paths():
    """原版只有盘符正则，Linux 上 guess_repos 永远返回空。"""
    got = list(iter_path_candidates("look at /home/me/proj/src/file.py please"))
    assert any(c.startswith("/home/me/proj") for c in got)


def test_iter_path_candidates_strips_cjk_punctuation():
    got = list(iter_path_candidates("路径是 /home/me/proj/x.py。然后"))
    assert "/home/me/proj/x.py" in got


def test_iter_path_candidates_both_forms_in_one_text():
    text = r'WSL 里是 /mnt/c/Users/me/proj，Windows 里是 C:\Users\me\proj'
    got = list(iter_path_candidates(text))
    assert any(c.startswith("/mnt/c/Users/me/proj") for c in got)
    assert any(c.startswith("C:\\Users\\me\\proj") for c in got)


# ── 身份卡提取 ────────────────────────────────────────────────────────

def test_scan_one_claude_identity(tmp_path: Path):
    fp = tmp_path / "claude.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "abc12345", "cwd": "/home/me/proj",
         "gitBranch": "feature/x", "version": "1.2.3"},
        {"type": "user", "message": {"content": [{"type": "text", "text": "帮我修一下登录 bug"}]}},
    ])
    row = scan_one("Claude Code", fp)
    assert row is not None
    assert row.session_id == "abc12345"
    assert row.cwd == "/home/me/proj"
    assert row.branch == "feature/x"
    assert row.version == "1.2.3"
    assert row.first_prompt == "帮我修一下登录 bug"


def test_scan_one_skips_injected_prompt_noise(tmp_path: Path):
    """插件清单与 caveman 广播不是人问的问题，认成开场提问会丢辨识度。"""
    fp = tmp_path / "claude.jsonl"
    _write_jsonl(fp, [
        {"type": "user", "message": {"content": [{"type": "text", "text": "<recommended_plugins>a b c</recommended_plugins>"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "CAVEMAN MODE ACTIVE"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "真正的问题在这里"}]}},
    ])
    row = scan_one("Claude Code", fp)
    assert row.first_prompt == "真正的问题在这里"


def test_scan_one_codex_session_meta(tmp_path: Path):
    fp = tmp_path / "rollout-2026-08-18T10-00-00-thread999.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "meta777", "cwd": "/srv/app",
                                             "cli_version": "0.9", "originator": "desktop"}},
        {"type": "response_item", "payload": {"role": "user", "content": [{"input_text": "开始"}]}},
    ])
    row = scan_one("Codex", fp)
    # 文件名 id 优先（UI 显示的是它），meta 里的 id 并列保留为源线程。
    assert row.session_id == "thread999"
    assert row.thread_id == "meta777"
    assert row.origin == "desktop"
    assert row.version == "0.9"


def test_scan_one_codex_first_meta_wins(tmp_path: Path):
    """派生 / 续接会带多个 session_meta；第一个才是这个文件自己的身份。"""
    fp = tmp_path / "rollout-2026-08-18T10-00-00-aaa.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "first", "cwd": "/one"}},
        {"type": "session_meta", "payload": {"session_id": "second", "cwd": "/two"}},
    ])
    row = scan_one("Codex", fp)
    assert row.cwd == "/one"
    assert row.thread_id == "first"


def test_scan_one_session_id_falls_back_to_filename(tmp_path: Path):
    fp = tmp_path / "rollout-2026-08-18T10-00-00-fallbackid.jsonl"
    _write_jsonl(fp, [{"type": "noise"}])
    assert scan_one("Codex", fp).session_id == "fallbackid"


def test_scan_one_counts_fatal_and_errors(tmp_path: Path):
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [
        {"type": "x", "note": "content-blocked by upstream"},
        {"type": "x", "note": "熔断了"},
        {"type": "x", "note": "IMAGE_DIMENSION_EXCEEDED"},
        {"type": "result", "is_error": True},
    ])
    # is_error 的判据是原始子串 `"is_error":true`，json.dumps 会加空格，手写一行。
    with fp.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"tool_result","is_error":true}\n')
        fh.write('{"type":"tool_result","isError":true}\n')
    row = scan_one("Claude Code", fp)
    assert row.fatal == 3
    assert row.errors == 2


def test_scan_one_counts_fatal_past_early_exit_budget(tmp_path: Path):
    """身份提取提前退出后，致命计数仍必须扫到文件尾——原版是分三遍读的，
    单遍重写最容易在这里少数。"""
    fp = tmp_path / "c.jsonl"
    rows = [{"type": "system", "sessionId": "s1", "cwd": "/p"},
            {"type": "user", "message": {"content": "q"}}]
    rows += [{"type": "noise", "i": i} for i in range(500)]
    rows += [{"type": "x", "note": "content-blocked"}]  # 第 503 行，远超 400 行预算
    _write_jsonl(fp, rows)
    row = scan_one("Claude Code", fp)
    assert row.session_id == "s1"
    assert row.fatal == 1, "提前退出后仍要数完致命签名"


def test_scan_one_tolerates_malformed_json(tmp_path: Path):
    fp = tmp_path / "c.jsonl"
    fp.write_text('not json at all\n{"type":"user","message":{"content":"ok"}}\n', encoding="utf-8")
    row = scan_one("Claude Code", fp)
    assert row is not None and row.first_prompt == "ok"


def test_scan_one_shallow_mode_skips_identity(tmp_path: Path):
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "deep1", "cwd": "/p"}])
    row = scan_one("Claude Code", fp, deep=False)
    assert row.session_id == "c"  # 文件名 stem
    assert row.cwd == ""


def test_scan_one_missing_file(tmp_path: Path):
    assert scan_one("Claude Code", tmp_path / "nope.jsonl") is None


# ── 仓库推断 ──────────────────────────────────────────────────────────

def test_repo_inferred_from_cwd(tmp_path: Path):
    proj = tmp_path / "realproj"
    (proj / ".git").mkdir(parents=True)
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "s", "cwd": str(proj)}])
    row = scan_one("Claude Code", fp)
    assert row.repo and Path(row.repo).name == "realproj"


def test_repo_inferred_from_prompt_text_posix(tmp_path: Path):
    """Codex Desktop 的 cwd 是任务容器，项目只在开头几轮的文本里出现。"""
    proj = tmp_path / "fromtext"
    (proj / "sub").mkdir(parents=True)
    (proj / ".git").mkdir()
    container = tmp_path / "container"
    container.mkdir()
    fp = tmp_path / "r.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "s", "cwd": str(container)}},
        {"type": "response_item", "payload": {"role": "user",
         "content": [{"input_text": f"看看 {(proj / 'sub').as_posix()} 里的代码"}]}},
    ])
    row = scan_one("Codex", fp)
    assert any(Path(r).name == "fromtext" for r in row.repos)


def test_home_root_ranked_last(tmp_path: Path, monkeypatch):
    """裸的用户主目录从来不是被操作的对象，即使它恰好是个 git 仓库。"""
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    real = tmp_path / "realwork"
    (real / ".git").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "s", "cwd": str(home)},
        {"type": "user", "message": {"content": f"see {real.as_posix()}/x.py"}},
    ])
    row = scan_one("Claude Code", fp)
    assert Path(row.repos[0]).name == "realwork"


# ── 排序与查找 ────────────────────────────────────────────────────────

def _row(band_size: int, agent="Claude Code", **kw):
    from agent_handoff.core.vitals import SessionRow

    return SessionRow(
        agent=agent, path=Path("/x/y.jsonl"), file=kw.pop("file", "y.jsonl"),
        mtime=kw.pop("mtime", datetime(2026, 8, 18, 12, 0, 0)),
        mb=band_size / 1e6, size=band_size, fatal=kw.pop("fatal", 0),
        errors=0, band=band_for(band_size), **kw,
    )


def test_find_sessions_ranks_id_over_prompt():
    a = _row(1000, session_id="deadbeef", first_prompt="nothing")
    b = _row(9_000_000, session_id="other", first_prompt="deadbeef mentioned here")
    got = find_sessions("deadbeef", [b, a])
    assert got[0] is a, "ID 精确匹配必须排在提问文本匹配之前"


def test_find_sessions_prefers_recent_within_same_rank():
    """原版同级按体积排，会把一个月前的大转录顶到前面。找会话时"最近那个"才对。"""
    old = _row(9_000_000, cwd="/p/myproj", mtime=datetime(2026, 7, 1))
    new = _row(500_000, cwd="/p/myproj", mtime=datetime(2026, 8, 18))
    got = find_sessions("myproj", [old, new])
    assert got[0] is new


def test_find_sessions_matches_cwd_and_repos():
    r = _row(1000, cwd="/home/me/kirara", repos=["/home/me/kirara"])
    assert find_sessions("kirara", [r]) == [r]
    assert find_sessions("KIRARA", [r]) == [r]


def test_find_sessions_empty_needle():
    assert find_sessions("   ", [_row(1000)]) == []


def test_find_sessions_no_match():
    assert find_sessions("zzz", [_row(1000, session_id="abc")]) == []


def test_sessions_for_repo_matches_subdirectories(tmp_path: Path):
    a = _row(1000, cwd="/p/proj")
    b = _row(1000, cwd="/p/proj/sub/deeper")
    c = _row(1000, cwd="/p/other")
    got = sessions_for_repo(Path("/p/proj"), [a, b, c])
    assert len(got) == 2
    assert all(g is a or g is b for g in got)
    assert c not in got


def test_sessions_for_repo_ignores_case_and_slashes():
    a = _row(1000, cwd=r"C:\Users\Me\Proj")
    got = sessions_for_repo(Path("c:/users/me/proj"), [a])
    assert got == [a]


# ── 目录扫描与并行 ────────────────────────────────────────────────────

def test_newest_files_respects_limit_and_order(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    made = []
    for i in range(5):
        fp = root / f"s{i}.jsonl"
        fp.write_text("{}\n", encoding="utf-8")
        os.utime(fp, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
        made.append(fp)
    got = _newest_files(root, 3)
    assert got == [made[4], made[3], made[2]]


def test_newest_files_recurses_and_ignores_other_suffixes(tmp_path: Path):
    root = tmp_path / "s"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "deep.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "a" / "note.txt").write_text("x\n", encoding="utf-8")
    got = _newest_files(root, 10)
    assert [p.name for p in got] == ["deep.jsonl"]


def test_scan_session_vitals_sorts_by_risk(tmp_path: Path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    small = root / "small.jsonl"
    _write_jsonl(small, [{"type": "system", "sessionId": "small", "cwd": "/p"}])
    big = root / "big.jsonl"
    _write_jsonl(big, [{"type": "system", "sessionId": "big", "cwd": "/p"}])
    _pad(big, 8_500_000)

    monkeypatch.setattr(
        "agent_handoff.core.vitals.agent_session_roots", lambda: [("Claude Code", root)]
    )
    clear_cache()
    rows = scan_session_vitals(limit=10)
    assert [r.band for r in rows] == ["critical", "ok"]
    assert rows[0].session_id == "big"


def test_scan_session_vitals_parallel_equals_serial(tmp_path: Path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    for i in range(6):
        _write_jsonl(root / f"s{i}.jsonl", [
            {"type": "system", "sessionId": f"id{i}", "cwd": "/p"},
            {"type": "user", "message": {"content": f"q{i}"}},
            {"type": "x", "note": "content-blocked"} if i % 2 else {"type": "ok"},
        ])
    monkeypatch.setattr(
        "agent_handoff.core.vitals.agent_session_roots", lambda: [("Claude Code", root)]
    )
    clear_cache()
    serial = [r.to_dict() for r in scan_session_vitals(limit=10, jobs=1)]
    clear_cache()
    par = [r.to_dict() for r in scan_session_vitals(limit=10, jobs=6)]
    assert serial == par


def test_scan_session_vitals_no_roots(monkeypatch):
    monkeypatch.setattr("agent_handoff.core.vitals.agent_session_roots", lambda: [])
    assert scan_session_vitals() == []


def test_cache_reuses_unchanged_file(tmp_path: Path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    fp = root / "s.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "x", "cwd": "/p"}])
    monkeypatch.setattr(
        "agent_handoff.core.vitals.agent_session_roots", lambda: [("Claude Code", root)]
    )
    clear_cache()
    first = scan_session_vitals(limit=5)[0]
    second = scan_session_vitals(limit=5)[0]
    assert first is second, "未变动的转录应复用缓存结果"


def test_to_dict_is_json_serializable(tmp_path: Path):
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "s", "cwd": "/p"}])
    d = scan_one("Claude Code", fp).to_dict()
    json.dumps(d)  # 不抛异常即通过
    assert isinstance(d["path"], str)
    assert isinstance(d["mtime"], str)
