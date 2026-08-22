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


def test_ask_repo_warns_but_accepts_non_git_dir(tmp_path: Path, monkeypatch, capsys):
    """缺 git 只提示，不再取消选择。

    原先返回 None 直接把用户挡回主菜单，于是一个没 git init 的工作目录
    完全用不了这个工具——哪怕用户要的只是把前序会话的结论带走，而那一步
    根本不碰 git。
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    _stdin(monkeypatch, str(plain))
    assert menu.ask_repo(Translator("en")) == plain
    out = capsys.readouterr().out
    assert "not under git" in out
    assert "You can still continue" in out


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
    assert calls == [(str(repo), "--skip-tests", "--pick-sessions")]


def test_menu_handoff_offers_session_picking(repo: Path, calls, monkeypatch):
    """真正执行那一遍要带 --pick-sessions：不问就等于会话内容永远传不下去。

    预演那一遍不带它——预演是给人看「将要发生什么」的，中间插一个需要输入的
    问答会打断阅读。
    """
    _stdin(monkeypatch, str(repo), "", "")
    menu.menu_handoff(Translator("en"))
    assert "--pick-sessions" not in calls[0]
    assert "--pick-sessions" in calls[1]


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


def test_menu_sweep_defaults_to_metadata_only(calls, monkeypatch):
    """默认不带 --by-repo：那一档要读转录内容，慢几个数量级。

    「占了多少、哪些能扔」只需要 stat，实测 1 GB 十几毫秒；按仓库聚合要把
    几百个转录读一遍，实测 24 秒。默认走快的那条。
    """
    _stdin(monkeypatch, "", "")
    menu.menu_sweep(Translator("en"))
    assert calls == [("--sweep",)]


@pytest.mark.parametrize("yes", ["y", "Y", "yes", "是", "好"])
def test_menu_sweep_by_repo_on_request(yes: str, calls, monkeypatch):
    """答应了才聚合。中文界面下用户会打「是」，不能只认 y。"""
    _stdin(monkeypatch, yes, "")
    menu.menu_sweep(Translator("zh-Hans"))
    assert calls == [("--sweep", "--by-repo")]


def test_menu_sweep_handles_eof(calls, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    menu.menu_sweep(Translator("en"))
    assert calls == []


# ── 主循环 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["0", "q", "Q", ""])
def test_main_quits_cleanly(key: str, monkeypatch):
    _stdin(monkeypatch, key)
    assert menu.main() == 0


@pytest.mark.parametrize("lang", ["zh-Hans", "zh-Hant", "en"])
def test_every_menu_number_has_a_label(lang: str):
    """插入新项时最容易漏的就是文案：编号在循环里写死，缺文案就打印 ??menu.8??。"""
    tr = Translator(lang)
    for n in ("1", "2", "3", "4", "5", "6", "7", "8", "0"):
        label = tr.t(f"menu.{n}")
        assert not label.startswith("??"), (lang, n, label)
        assert label.strip(), (lang, n)


def test_main_dispatches_each_number_to_its_own_screen(monkeypatch):
    """编号改过一次（插入磁盘占用后 5/6/7 全部后移），必须钉住映射。

    错位的后果很具体：用户想看说明书，点开的是网页界面。
    """
    seen: list[str] = []
    for name in ("menu_vitals", "menu_handoff", "menu_quick", "menu_find",
                 "menu_sweep", "menu_help", "menu_gui"):
        monkeypatch.setattr(menu, name, (lambda n: lambda tr: seen.append(n))(name))
    # menu_lang 要返回 Translator，主循环拿它的返回值继续用。
    monkeypatch.setattr(menu, "menu_lang", lambda tr: (seen.append("menu_lang"), tr)[1])
    _stdin(monkeypatch, "1", "2", "3", "4", "5", "6", "7", "8", "0")
    assert menu.main() == 0
    assert seen == ["menu_vitals", "menu_handoff", "menu_quick", "menu_find",
                    "menu_sweep", "menu_help", "menu_gui", "menu_lang"]


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
