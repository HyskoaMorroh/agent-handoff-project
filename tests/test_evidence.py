#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""完成度证据。重点：批量符号检索的结果必须与"每符号单独查"逐字一致，
且在 ripgrep 缺席时三条退路都给同一个答案。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_handoff.core.evidence import (
    _defined_in_text,
    _git_grep_batch,
    _python_scan,
    _rg_batch,
    resolve_symbols,
    score_tasks,
)
from agent_handoff.core.plan import parse_plan


def _tasks(repo: Path):
    return parse_plan((repo / "docs" / "plan.md").read_text(encoding="utf-8"))[0]


def test_defined_in_text_finds_python_and_js():
    src = """
def build_thing():
    pass
class ThingBuilder: pass
export const render_ui = () => 0
function helper_fn() {}
"""
    got = _defined_in_text(src, {"build_thing", "ThingBuilder", "render_ui", "helper_fn", "absent_sym"})
    assert got == {"build_thing", "ThingBuilder", "render_ui", "helper_fn"}


def test_defined_in_text_ignores_mere_usage():
    """只被调用不算定义——原版的判据也是这个，不能因为批量化而放宽。"""
    assert _defined_in_text("x = build_thing()\nbuild_thing()", {"build_thing"}) == set()


def test_resolve_symbols_hits_and_misses(repo: Path):
    got = resolve_symbols(repo, _tasks(repo))
    assert got["build_thing"] is True
    assert got["ThingBuilder"] is True
    assert got["render_ui"] is False


def test_score_tasks_complete_and_incomplete(repo: Path):
    rep = score_tasks(repo, _tasks(repo))
    assert rep[1]["complete"] is True
    assert rep[1]["files_missing"] == []
    assert rep[1]["symbols_missing"] == []
    assert rep[2]["complete"] is False
    assert rep[2]["files_missing"] == ["pkg/ui.py"]
    assert rep[2]["symbols_missing"] == ["render_ui"]


def test_score_tasks_ratios(repo: Path):
    rep = score_tasks(repo, _tasks(repo))
    assert rep[1]["file_ratio"] == 1.0 and rep[1]["symbol_ratio"] == 1.0
    assert rep[2]["file_ratio"] == 0.0 and rep[2]["symbol_ratio"] == 0.0


def test_score_tasks_partial(repo: Path):
    """Task 2 的文件到了但符号没定义 -> 部分完成，不能判定为完成。"""
    (repo / "pkg" / "ui.py").write_text("# todo\n", encoding="utf-8")
    rep = score_tasks(repo, _tasks(repo))
    assert rep[2]["files_missing"] == []
    assert rep[2]["symbols_missing"] == ["render_ui"]
    assert rep[2]["complete"] is False


def test_score_tasks_becomes_complete_when_symbol_lands(repo: Path):
    (repo / "pkg" / "ui.py").write_text("def render_ui():\n    return 0\n", encoding="utf-8")
    rep = score_tasks(repo, _tasks(repo))
    assert rep[2]["complete"] is True


def test_score_tasks_no_files_no_symbols_is_not_complete():
    """既没声明文件也没声明符号的任务不能凭空算完成——原版的保守判定，保留。"""
    tasks, _ = parse_plan("### Task 1: x\n\n**Steps:**\n- [ ] **Step 1** do\n")
    rep = score_tasks(Path("."), tasks)
    assert rep[1]["complete"] is False


def test_score_tasks_empty_input():
    assert score_tasks(Path("."), []) == {}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_rg_batch_matches_python_scan(repo: Path):
    syms = ["build_thing", "ThingBuilder", "render_ui"]
    rg_found, ok = _rg_batch(repo, syms)
    assert ok
    assert rg_found == _python_scan(repo, syms)


def test_git_grep_batch_matches_python_scan(repo: Path):
    syms = ["build_thing", "ThingBuilder", "render_ui"]
    gg_found, ok = _git_grep_batch(repo, syms)
    assert ok
    assert gg_found == _python_scan(repo, syms) == {"build_thing", "ThingBuilder"}


def test_all_three_backends_agree(repo: Path, monkeypatch):
    """三条退路必须给同一个答案，否则"有没有装 ripgrep"会改变判定结果。"""
    syms = ["build_thing", "ThingBuilder", "render_ui"]
    expected = {"build_thing", "ThingBuilder"}
    assert _python_scan(repo, syms) == expected
    assert _git_grep_batch(repo, syms)[0] == expected
    if shutil.which("rg"):
        assert _rg_batch(repo, syms)[0] == expected

    # 强制走 Python 兜底：模拟 rg 与 git grep 都不可用。
    monkeypatch.setattr("agent_handoff.core.evidence.shutil.which", lambda _n: None)
    monkeypatch.setattr(
        "agent_handoff.core.evidence._git_grep_batch", lambda *_a, **_k: (set(), False)
    )
    got = resolve_symbols(repo, _tasks(repo))
    assert got["build_thing"] and got["ThingBuilder"] and not got["render_ui"]


def test_symbol_with_regex_metacharacters_is_escaped(repo: Path, monkeypatch):
    """符号名里出现正则元字符时不能把整条批量正则搞坏。"""
    src = "def normal_sym():\n    pass\n"
    (repo / "pkg" / "core.py").write_text(src, encoding="utf-8")
    weird = ["normal_sym", "a.b*c", "x[1]"]
    found = _python_scan(repo, weird)
    assert found == {"normal_sym"}


def test_batching_over_limit(repo: Path, monkeypatch):
    """符号数超过单批上限时要分批，且结果与不分批一致。"""
    monkeypatch.setattr("agent_handoff.core.evidence.SYMBOL_BATCH", 2)
    syms = ["build_thing", "ThingBuilder", "render_ui", "nope_a", "nope_b"]
    assert _git_grep_batch(repo, syms)[0] == {"build_thing", "ThingBuilder"}


def test_hint_files_short_circuit_avoids_full_scan(repo: Path, monkeypatch):
    """任务自己声明的文件里就能找到全部符号时，不该再跑全库检索。"""
    calls = []
    monkeypatch.setattr(
        "agent_handoff.core.evidence._rg_batch",
        lambda r, s: (calls.append(s), (set(), True))[1],
    )
    monkeypatch.setattr(
        "agent_handoff.core.evidence._git_grep_batch",
        lambda r, s: (calls.append(s), (set(), True))[1],
    )
    tasks, _ = parse_plan(
        "### Task 1: x\n\n**Files:**\n- Create: `pkg/core.py`\n- Create: `pkg/util.py`\n\n"
        "**Interfaces:**\n- Produces `build_thing()`, `ThingBuilder`\n"
    )
    got = resolve_symbols(repo, tasks)
    assert got == {"build_thing": True, "ThingBuilder": True}
    assert calls == [], "命中提示文件后不该再做全库检索"
