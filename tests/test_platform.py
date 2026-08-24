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
    is_foreign_path,
    iter_path_candidates,
    local_home_names,
    nearest_repo,
    norm_path,
    normalize_shell_paths,
    path_is_stale,
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


def test_agent_session_roots_honours_env_overrides(monkeypatch, tmp_path: Path):
    """两个应用都能用环境变量把数据目录挪走（笔记本上挪到 D 盘很常见）。

    不读这两个变量的后果不是少扫几个文件，而是「一个转录都找不到」。
    自定义目录要排在家目录之前——用户显式指定过就该优先。
    """
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".codex" / "sessions").mkdir(parents=True)
    alt_claude = tmp_path / "d-drive" / "claude-data"
    (alt_claude / "projects").mkdir(parents=True)
    alt_codex = tmp_path / "d-drive" / "codex-data"
    (alt_codex / "sessions").mkdir(parents=True)
    (alt_codex / "archived_sessions").mkdir(parents=True)

    monkeypatch.setattr("os.path.expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr("agent_handoff.platform.IS_WINDOWS", True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt_claude))
    monkeypatch.setenv("CODEX_HOME", str(alt_codex))

    got = agent_session_roots()
    paths = [norm_path(str(p)) for _, p in got]
    assert norm_path(str(alt_claude / "projects")) in paths
    assert norm_path(str(alt_codex / "sessions")) in paths
    assert norm_path(str(alt_codex / "archived_sessions")) in paths
    # 家目录仍然保留：用户可能两处都有历史记录，丢掉一边就是丢会话。
    assert norm_path(str(home / ".claude" / "projects")) in paths
    # 显式指定的排在前面。
    assert paths.index(norm_path(str(alt_claude / "projects"))) < paths.index(
        norm_path(str(home / ".claude" / "projects"))
    )


def test_agent_session_roots_ignores_blank_and_quoted_env(monkeypatch, tmp_path: Path):
    """空值不能当成路径；Windows 上用户复制粘贴常带一对引号。"""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    alt = tmp_path / "quoted"
    (alt / "projects").mkdir(parents=True)
    monkeypatch.setattr("os.path.expanduser", lambda p: str(home) if p == "~" else p)
    monkeypatch.setattr("agent_handoff.platform.IS_WINDOWS", True)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
    monkeypatch.setenv("CODEX_HOME", "")
    got = [norm_path(str(p)) for _, p in agent_session_roots()]
    assert got == [norm_path(str(home / ".claude" / "projects"))]

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f'"{alt}"')
    got = [norm_path(str(p)) for _, p in agent_session_roots()]
    assert norm_path(str(alt / "projects")) in got


# ── 迁机：认出「另一台电脑的路径」 ────────────────────────────────────

def _fake_home(monkeypatch, name: str = "devin") -> None:
    """把本机家目录固定成一个已知名字，好断言「谁是本机、谁是外人」。"""
    monkeypatch.setattr("os.path.expanduser", lambda p: rf"C:\Users\{name}" if p == "~" else p)
    monkeypatch.setenv("USERNAME", name)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)


@pytest.mark.parametrize("raw", [
    r"D:\Users\bob\myproj",
    "/home/alice/proj",
    "/Users/carol/work",
    "/mnt/c/Users/dave/p",
    r"E:\Users\erin\x\y\z",
])
def test_other_peoples_home_dirs_are_foreign(monkeypatch, raw: str):
    """判据靠结构而不是猜用户名：硬编码名单换台机器就失效。"""
    _fake_home(monkeypatch)
    assert is_foreign_path(raw) is True


@pytest.mark.parametrize("raw", [
    r"C:\Users\devin\vscode",
    r"c:/users/DEVIN/vscode",          # 大小写混写仍是本机
    r"C:\Users\devin\deleted-yesterday",  # 本机名字，目录没了也不算迁机
])
def test_local_home_is_never_foreign(monkeypatch, raw: str):
    """本机用户名一旦认出就是结论，不看存在性。

    否则「昨天删掉的目录」会被报成外来转录——那是误报，且会让工具
    对着正常会话隐藏 resume 命令。
    """
    _fake_home(monkeypatch)
    assert is_foreign_path(raw) is False


def test_unrecognised_absolute_path_is_not_foreign(monkeypatch):
    """认不出家目录形状时不算外来——「路径不存在」是弱信号。

    本机删掉一个项目目录是常事，而外来判断会关掉原生续接。那个会话在应用
    索引里还在，续接本来可用，误判等于把能用的功能关掉。
    路径失效由 `path_is_stale` 单独表达。
    """
    _fake_home(monkeypatch)
    assert is_foreign_path(r"Z:\no-such-mount\proj") is False
    assert is_foreign_path("/opt/never-here/proj") is False
    assert is_foreign_path(r"E:\output\proj") is False


def test_path_is_stale_only_for_missing_absolute_paths(monkeypatch):
    """「打不开」与「属于别的机器」是两件事，后果不同。"""
    _fake_home(monkeypatch)
    assert path_is_stale(str(Path.cwd())) is False        # 存在
    assert path_is_stale(r"Z:\no-such-mount\proj") is True
    assert path_is_stale("/opt/never-here/proj") is True
    assert path_is_stale("src/agent_handoff") is False    # 相对片段不判
    assert path_is_stale("") is False


@pytest.mark.parametrize("raw", ["", "   ", "src/agent_handoff", "docs/plan.md"])
def test_relative_and_empty_are_not_foreign(monkeypatch, raw: str):
    """相对片段与空值无从判断，不能报成外来——那会给正常会话加警告。"""
    _fake_home(monkeypatch)
    assert is_foreign_path(raw) is False


def test_local_home_names_includes_env_and_home(monkeypatch):
    """WSL 下 `~` 是 Linux 侧的名字，而转录可能来自 Windows 侧，两个都算本机。"""
    monkeypatch.setattr("os.path.expanduser", lambda p: "/home/linuxname" if p == "~" else p)
    monkeypatch.setenv("USERNAME", "WinName")
    got = local_home_names()
    assert "linuxname" in got
    assert "winname" in got, "环境变量里的名字也要算本机，否则 WSL 下自己的路径被判外来"


def test_force_utf8_io_is_idempotent():
    from agent_handoff.platform import force_utf8_io

    force_utf8_io()
    force_utf8_io()  # 二次调用不该抛异常
    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"


# ── 图文说明的查找 ────────────────────────────────────────────────────

def test_guide_is_found_in_the_installed_package():
    """装好的包里也要能找到图文说明。

    此前 `gui/server.py` 从自己往上数四层、`menu.py` 数三层去找
    `docs/guide.html`——两者在源码检出下都对，但 `docs/` 从来不进 wheel，
    所以 `pip install` 之后网页界面的「指南」入口必然 404、菜单第 6 项必然
    打印「找不到」。这份 104 KB 的三语文档正是本项目的主要说明载体。

    现在 `build_guide.py` 会把它复制进 `gui/static/`（该副本不进 git，
    避免两个真源漂移），`find_guide()` 优先找那一份。
    """
    from agent_handoff.platform import find_guide

    got = find_guide()
    assert got is not None, "本仓库里 guide.html 必须能找到"
    assert got.is_file()
    assert got.stat().st_size > 10_000, "图文说明是完整的三语文档，不该只有几百字节"


def test_guide_lookup_prefers_the_packaged_copy(tmp_path, monkeypatch):
    """包内副本优先于源码检出。

    装好的包里不存在源码树，这条保证查找顺序不依赖「恰好有 docs/」。
    """
    from agent_handoff import platform as plat

    pkg_static = tmp_path / "agent_handoff" / "gui" / "static"
    pkg_static.mkdir(parents=True)
    (pkg_static / "guide.html").write_text("packaged", encoding="utf-8")
    # 让 find_guide 以为自己住在这个假包里。
    monkeypatch.setattr(plat, "__file__", str(tmp_path / "agent_handoff" / "platform.py"))

    got = plat.find_guide()
    assert got is not None
    assert got.read_text(encoding="utf-8") == "packaged"


def test_guide_lookup_returns_none_when_absent(tmp_path, monkeypatch):
    """找不到就返回 None，不能抛异常。

    图文说明缺失不影响工具的任何实际功能：网页界面隐藏入口、菜单打印
    「找不到」即可。为此让调用方崩掉是本末倒置。
    """
    from agent_handoff import platform as plat

    empty = tmp_path / "nowhere" / "agent_handoff"
    empty.mkdir(parents=True)
    monkeypatch.setattr(plat, "__file__", str(empty / "platform.py"))
    monkeypatch.delenv("AGENT_HANDOFF_HOME", raising=False)

    assert plat.find_guide() is None


def test_declared_home_is_the_last_resort(tmp_path, monkeypatch):
    """`AGENT_HANDOFF_HOME` 指向的检出也算一个来源。"""
    from agent_handoff import platform as plat

    empty = tmp_path / "pkg" / "agent_handoff"
    empty.mkdir(parents=True)
    monkeypatch.setattr(plat, "__file__", str(empty / "platform.py"))

    checkout = tmp_path / "checkout"
    (checkout / "docs").mkdir(parents=True)
    (checkout / "docs" / "guide.html").write_text("declared", encoding="utf-8")
    monkeypatch.setenv("AGENT_HANDOFF_HOME", str(checkout))

    got = plat.find_guide()
    assert got is not None
    assert got.read_text(encoding="utf-8") == "declared"
