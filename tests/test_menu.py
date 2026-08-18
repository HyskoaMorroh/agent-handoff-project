#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交互式菜单。它是"不想记参数"的人唯一会碰的入口，而原版从未被测过。

菜单不能靠人工点：这里用打好脚本的 stdin 喂它，断言它调了正确的命令、
在坏输入前停下、并且把语言选择真的存下来。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from agent_handoff import menu
from agent_handoff.i18n import Translator


@pytest.fixture(autouse=True)
def _no_clear(monkeypatch):
    """别在测试输出里刷屏。"""
    monkeypatch.setattr(menu, "clear_screen", lambda: None)


@pytest.fixture
def calls(monkeypatch):
    """拦下真正的子进程调用，只记录参数。菜单的职责就是拼对参数。"""
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(menu, "run_tool", lambda _tr, *a: seen.append(a) or 0)
    return seen


def _stdin(monkeypatch, *lines: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))


# ── 语言持久化 ────────────────────────────────────────────────────────

def test_language_choice_persists(tmp_path: Path, monkeypatch):
    """选过一次语言，下次打开还是那个语言。原版没有这个概念。

    配置存的是裸语言标记而不是 JSON：单个值不需要结构，纯文本还能让人
    直接用记事本改。
    """
    cfg = tmp_path / "lang"
    monkeypatch.setattr(menu, "CONFIG", cfg)
    menu._save_lang("zh-Hant")
    assert menu._load_lang() == "zh-Hant"
    assert cfg.read_text(encoding="utf-8").strip() == "zh-Hant"


def test_language_load_survives_corrupt_config(tmp_path: Path, monkeypatch):
    """配置文件写了垃圾要退回受支持的语言，不能让菜单起不来。"""
    cfg = tmp_path / "lang"
    cfg.write_text("not-a-language-tag\x00\x01", encoding="utf-8")
    monkeypatch.setattr(menu, "CONFIG", cfg)
    assert menu._load_lang() in ("zh-Hans", "zh-Hant", "en")


def test_language_load_normalizes_loose_tags(tmp_path: Path, monkeypatch):
    """手工改配置的人会写 zh_TW、en_US.UTF-8 这类形态，都要认。"""
    cfg = tmp_path / "lang"
    monkeypatch.setattr(menu, "CONFIG", cfg)
    for raw, want in (("zh_TW", "zh-Hant"), ("en_US.UTF-8", "en"), ("zh", "zh-Hans")):
        cfg.write_text(raw, encoding="utf-8")
        assert menu._load_lang() == want, raw


def test_language_load_without_config_falls_back(tmp_path: Path, monkeypatch):
    """第一次运行时配置还不存在。"""
    monkeypatch.setattr(menu, "CONFIG", tmp_path / "never" / "written")
    assert menu._load_lang() in ("zh-Hans", "zh-Hant", "en")


def test_language_save_survives_unwritable_dir(tmp_path: Path, monkeypatch):
    """存不下就算了——不能因为写配置失败而崩掉整个菜单。"""
    monkeypatch.setattr(menu, "CONFIG", tmp_path / "nope" / "deep" / "cfg.json")
    menu._save_lang("en")  # 不抛异常即通过


# ── 路径输入的守卫 ────────────────────────────────────────────────────

def test_ask_repo_accepts_dragged_path_with_quotes(repo: Path, monkeypatch, capsys):
    """从资源管理器拖文件夹进终端会自动带引号。"""
    _stdin(monkeypatch, f'"{repo}"')
    assert menu.ask_repo(Translator("en")) == repo


def test_ask_repo_rejects_missing_path(tmp_path: Path, monkeypatch, capsys):
    _stdin(monkeypatch, str(tmp_path / "nope"))
    assert menu.ask_repo(Translator("en")) is None
    assert "does not exist" in capsys.readouterr().out


def test_ask_repo_rejects_non_git_dir(tmp_path: Path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    _stdin(monkeypatch, str(plain))
    assert menu.ask_repo(Translator("en")) is None
    assert "not a git repository" in capsys.readouterr().out


def test_ask_repo_empty_input_cancels(monkeypatch, capsys):
    _stdin(monkeypatch, "")
    assert menu.ask_repo(Translator("en")) is None


def test_ask_repo_handles_eof(monkeypatch):
    """Ctrl+D / 管道结束不该抛回溯。"""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert menu.ask_repo(Translator("en")) is None


# ── 各菜单项调对了命令 ────────────────────────────────────────────────

def test_menu_vitals_passes_vitals_flag(calls, monkeypatch):
    _stdin(monkeypatch, "")
    menu.menu_vitals(Translator("en"))
    assert calls == [("--vitals",)]


def test_menu_quick_passes_skip_tests(repo: Path, calls, monkeypatch):
    _stdin(monkeypatch, str(repo), "")
    menu.menu_quick(Translator("en"))
    assert calls == [(str(repo), "--skip-tests")]


def test_menu_handoff_dry_runs_before_real_run(repo: Path, calls, monkeypatch):
    """先预演再执行是这个菜单的核心安全设计：让人在写之前看见将要发生什么。"""
    _stdin(monkeypatch, str(repo), "", "")
    menu.menu_handoff(Translator("en"))
    assert len(calls) == 2
    assert "--dry-run" in calls[0]
    assert "--dry-run" not in calls[1]


def test_menu_handoff_bad_path_never_runs_tool(tmp_path: Path, calls, monkeypatch):
    """路径不对时一次子进程都不该起——原版会带着坏路径往下走。"""
    _stdin(monkeypatch, str(tmp_path / "nope"), "")
    menu.menu_handoff(Translator("en"))
    assert calls == []


def test_menu_find_passes_keyword(calls, monkeypatch):
    _stdin(monkeypatch, "deadbeef", "")
    menu.menu_find(Translator("en"))
    assert calls == [("--find", "deadbeef")]


def test_menu_find_empty_keyword_skips(calls, monkeypatch):
    _stdin(monkeypatch, "", "")
    menu.menu_find(Translator("en"))
    assert calls == []


# ── 主循环 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["0", "q", "Q", ""])
def test_main_quits_cleanly(key: str, monkeypatch):
    _stdin(monkeypatch, key)
    assert menu.main() == 0


def test_main_handles_eof(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert menu.main() == 0


def test_main_ignores_garbage_choice(monkeypatch):
    """乱输入不该崩，也不该退出——回到菜单等下一次输入。"""
    _stdin(monkeypatch, "zzz", "99", "0")
    assert menu.main() == 0


def test_menu_renders_in_all_three_languages(monkeypatch, capsys):
    for lang in ("zh-Hans", "zh-Hant", "en"):
        monkeypatch.setattr(menu, "_load_lang", lambda lang=lang: lang)
        _stdin(monkeypatch, "0")
        menu.main()
        out = capsys.readouterr().out
        assert "??" not in out, f"{lang}: 菜单里有缺键占位符"
        assert Translator(lang).t("menu.title") in out
