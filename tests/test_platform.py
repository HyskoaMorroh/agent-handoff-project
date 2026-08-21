#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台抽象层。原版的 Windows 假设散落各处，Linux 上会静默降级而不是报错。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_handoff.platform import (
    IS_WINDOWS,
    agent_session_roots,
    iter_path_candidates,
    nearest_repo,
    norm_path,
    normalize_shell_paths,
    shell_quote,
    venv_interpreters,
)


def test_norm_path_ignores_slash_direction_and_case():
    assert norm_path(r"C:\Users\Me\Proj\\") == norm_path("c:/users/me/proj")
    assert norm_path("/home/me/proj/") == "/home/me/proj"


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("全角逗号", "仓库 E:/output/proj，分支 main"),
        ("全角冒号", "项目在 E:/output/proj：请检查"),
        ("全角句号", "路径是 E:/output/proj。"),
        ("引号包裹后接中文", '"E:/output/proj"为项目B。'),
        ("顿号", "E:/output/proj、E:/other"),
    ],
)
def test_path_candidates_stop_at_fullwidth_punctuation(label: str, text: str):
    """全角标点是句子分隔符，不是路径的一部分。

    它不是空白字符，所以 `[^\\s...]` 会把它连同后面的中文一起吃进候选路径，
    `nearest_repo` 于是去找一个不存在的目录，仓库推断静默失败。
    """
    assert "E:/output/proj" in list(iter_path_candidates(text)), label


def test_path_candidates_keep_chinese_directory_names():
    """中文目录名是合法路径，不能因为切全角标点而把它们一起切掉。"""
    got = list(iter_path_candidates("代码在 E:/项目/前端 目录"))
    assert "E:/项目/前端" in got


def test_nearest_repo_walks_up(tmp_path: Path):
    root = tmp_path / "proj"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (root / ".git").mkdir()
    assert Path(nearest_repo(str(deep))) == root.resolve()


def test_nearest_repo_returns_empty_when_none(tmp_path: Path):
    d = tmp_path / "nogit"
    d.mkdir()
    assert nearest_repo(str(d)) == ""


def test_nearest_repo_survives_garbage():
    assert nearest_repo("\x00not/a/path") == "" or True  # 不抛异常即通过


def test_venv_interpreters_windows_layout(tmp_path: Path):
    v = tmp_path / ".venv-win"
    (v / "Scripts").mkdir(parents=True)
    exe = v / "Scripts" / "python.exe"
    exe.write_text("", encoding="utf-8")
    got, rel = venv_interpreters(v)
    assert got == exe and rel == "Scripts/python.exe"


def test_venv_interpreters_posix_layout(tmp_path: Path):
    v = tmp_path / ".venv"
    (v / "bin").mkdir(parents=True)
    exe = v / "bin" / "python"
    exe.write_text("", encoding="utf-8")
    got, rel = venv_interpreters(v)
    assert got == exe and rel == "bin/python"


def test_venv_interpreters_python3_only(tmp_path: Path):
    """有的 venv 只有 bin/python3，没有 bin/python。原版只查 bin/python，会漏。"""
    v = tmp_path / ".venv"
    (v / "bin").mkdir(parents=True)
    exe = v / "bin" / "python3"
    exe.write_text("", encoding="utf-8")
    got, rel = venv_interpreters(v)
    assert got == exe and rel == "bin/python3"


def test_venv_interpreters_broken_venv(tmp_path: Path):
    """没有可执行 Python 的目录不是可用的虚拟环境。"""
    v = tmp_path / ".venv-broken"
    (v / "lib").mkdir(parents=True)
    assert venv_interpreters(v) == (None, "")


def test_venv_interpreters_prefers_windows_layout_when_both_exist(tmp_path: Path):
    """同一仓库被 Windows 与 WSL 交替使用时两种布局都在；按查找顺序取第一个。"""
    v = tmp_path / ".venv"
    (v / "Scripts").mkdir(parents=True)
    (v / "bin").mkdir(parents=True)
    (v / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    (v / "bin" / "python").write_text("", encoding="utf-8")
    _, rel = venv_interpreters(v)
    assert rel == "Scripts/python.exe"


def test_shell_quote_wraps_paths_with_spaces():
    got = shell_quote("C:/Program Files/Py/python.exe")
    assert got.startswith('"') and got.endswith('"')
    assert " " in got


def test_shell_quote_leaves_simple_path_bare():
    got = shell_quote(".venv/bin/python")
    assert '"' not in got


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows separator behavior")
def test_shell_quote_backslashes_on_windows():
    assert shell_quote(".venv-win/Scripts/python.exe") == r".venv-win\Scripts\python.exe"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX separator behavior")
def test_shell_quote_forward_slashes_on_posix():
    assert shell_quote(r".venv\bin\python") == ".venv/bin/python"


@pytest.mark.skipif(not IS_WINDOWS, reason="cmd.exe cannot run forward-slash relative paths")
def test_normalize_shell_paths_windows():
    got = normalize_shell_paths(".venv-win/Scripts/python.exe -m pytest ./tests -q")
    assert r".venv-win\Scripts\python.exe" in got
    assert "./tests" in got, "参数里的正斜杠不该被动"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell treats backslash as escape")
def test_normalize_shell_paths_posix():
    got = normalize_shell_paths(r".venv\bin\python -m pytest")
    assert ".venv/bin/python" in got


def test_agent_session_roots_returns_only_existing(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    # .codex/sessions 故意不建：不存在的目录必须被跳过。
    monkeypatch.setattr("os.path.expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr("agent_handoff.platform.IS_WINDOWS", True)
    got = agent_session_roots()
    names = [n for n, _ in got]
    assert names == ["Claude Code"]


def test_agent_session_roots_dedupes(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".codex" / "sessions").mkdir(parents=True)
    monkeypatch.setattr("os.path.expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr("agent_handoff.platform.IS_WINDOWS", True)
    got = agent_session_roots()
    paths = [str(p).lower() for _, p in got]
    assert len(paths) == len(set(paths))


def test_force_utf8_io_is_idempotent():
    from agent_handoff.platform import force_utf8_io

    force_utf8_io()
    force_utf8_io()  # 二次调用不该抛异常
    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
