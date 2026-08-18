#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三语文案表的完整性。缺键或占位符不匹配会在运行时静默降级，必须挡在测试里。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_handoff.i18n import LANGS, Translator, available, normalize

I18N_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_handoff" / "i18n"


def load(lang: str) -> dict[str, str]:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_all_languages_present():
    assert set(available()) == set(LANGS)


@pytest.mark.parametrize("lang", ["zh-Hant", "en"])
def test_no_missing_or_extra_keys(lang):
    base = load("zh-Hans")
    other = load(lang)
    assert set(base) == set(other), f"{lang} key set differs"


@pytest.mark.parametrize("lang", ["zh-Hant", "en"])
def test_placeholders_match(lang):
    """占位符集合必须一致：多一个会 KeyError，少一个会静默丢信息。"""
    base = load("zh-Hans")
    other = load(lang)
    ph = re.compile(r"\{(\w+)\}")
    for key, val in base.items():
        assert set(ph.findall(val)) == set(ph.findall(other[key])), f"{lang}:{key}"


def test_missing_key_is_visible_not_fatal():
    """缺键必须返回可见标记而不是抛异常——事故之后崩溃比难看糟得多。"""
    tr = Translator("en")
    assert tr.t("nope.not.a.key") == "??nope.not.a.key??"


def test_bad_placeholder_falls_back_to_raw():
    """占位符对不上时给原文，不抛异常。"""
    tr = Translator("en")
    out = tr.t("cli.plan.found")  # 少给全部参数
    assert "{path}" in out


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("zh", "zh-Hans"),
        ("zh_CN", "zh-Hans"),
        ("zh-Hans-CN", "zh-Hans"),
        ("zh-TW", "zh-Hant"),
        ("zh_HK", "zh-Hant"),
        ("zh-Hant-MO", "zh-Hant"),
        ("en_US.UTF-8", "en"),
        ("en-GB", "en"),
        ("de_DE", "zh-Hans"),   # 认不出来就回基准语言
        ("", "zh-Hans"),
        (None, "zh-Hans"),
    ],
)
def test_normalize(raw, expect):
    assert normalize(raw) == expect


def test_env_var_overrides_locale(monkeypatch):
    from agent_handoff.i18n import detect

    monkeypatch.setenv("AGENT_HANDOFF_LANG", "zh-Hant")
    assert detect() == "zh-Hant"


def test_traditional_is_not_simplified():
    """繁体表不能是简体表的拷贝——那等于没翻译。"""
    hans, hant = load("zh-Hans"), load("zh-Hant")
    same = [k for k in hans if hans[k] == hant[k]]
    # 少量条目（纯 ASCII 的命令示例等）本就相同，但大多数必须不同。
    assert len(same) < len(hans) * 0.35, f"{len(same)}/{len(hans)} entries identical"


def test_table_merges_base_for_fallback():
    tr = Translator("en")
    table = tr.table()
    assert set(table) >= set(load("zh-Hans"))
