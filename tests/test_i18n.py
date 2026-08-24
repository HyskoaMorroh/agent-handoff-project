#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三语文案表的完整性。缺键或占位符不匹配会在运行时静默降级，必须挡在测试里。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_handoff.i18n import LANGS, Translator, available, detect, normalize

I18N_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_handoff" / "i18n"


def load(lang: str) -> dict[str, str]:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_all_languages_present():
    assert set(available()) == set(LANGS)


@pytest.mark.parametrize("lang", ["zh-Hant", "en"])
def test_no_missing_or_extra_keys(lang):
    """键集合必须**双向**相等：缺键和孤儿键都要挡。

    只查「缺键」是不够的。孤儿键（某语言有、基准语言没有）意味着那条文案
    永远不会被用到，或者基准语言漏了一个功能的文案——两种都是真问题，而且
    它不会在界面上表现成 `??key??`，只会静默存在。

    对照过一个同源项目的做法：它有同样机制，但只对约 4.4% 的键（一个命名空间
    前缀 + 一份手工白名单）做断言，全量 2839 键靠人工纪律维持。实测它的
    zh-TW 就有 1 个孤儿键漏网。所以这里坚持全量 + 双向，不设白名单。
    """
    base = load("zh-Hans")
    other = load(lang)
    missing = sorted(set(base) - set(other))
    orphan = sorted(set(other) - set(base))
    assert not missing, f"{lang} 缺键: {missing}"
    assert not orphan, f"{lang} 有基准语言没有的孤儿键: {orphan}"


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


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("Chinese (Traditional)_Taiwan", "zh-Hant"),
        ("Chinese (Simplified)_China", "zh-Hans"),
        ("Chinese_Taiwan", "zh-Hant"),
        ("Chinese_Hong Kong SAR", "zh-Hant"),
        ("Chinese_China", "zh-Hans"),
        ("English_United States", "en"),
        ("English_United Kingdom", "en"),
    ],
)
def test_normalize_windows_locale_names(raw, expect):
    """Windows 的 `locale.getlocale()` 返回人类可读名，不是 BCP-47 标记。

    不认它的后果：一台繁体中文 Windows 只要没设 `LANG`（Windows 的默认状态），
    字符串以 `C` 开头、既不匹配 `en` 也不匹配 `zh`，于是落到基准语言，
    **整个界面和生成的交接文档全变简体**。简体机器上「恰好正确」掩盖了这一点，
    繁中机器上必然错。
    """
    assert normalize(raw) == expect


def test_detect_skips_neutral_c_locale(monkeypatch):
    """`LANG=C.UTF-8` 是「不做本地化」，不是某种语言。

    容器、CI、cron 默认就是它。当成有效标记会让英文 Docker 里的英文用户
    拿到简体中文输出。惯例是跳过它继续往下探测。
    """
    for var in ("AGENT_HANDOFF_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert detect() == "en", "中性 LANG 不该挡住后面真正的语言设置"


@pytest.mark.parametrize(
    "value,expect",
    [
        ("fr:en", "en"),          # 法语不支持，按 gettext 惯例回退到英语
        ("fr:de:en_US", "en"),
        ("zh_TW:en", "zh-Hant"),  # 第一段就支持，直接用
    ],
)
def test_detect_walks_the_language_priority_list(value, expect, monkeypatch):
    """`LANGUAGE` 是冒号分隔的**优先级列表**，不是单个标记。

    原版只取第一段就返回，于是 `fr:en` 落到基准语言（简体中文），
    而用户明明写了「法语不行就用英语」。
    """
    for var in ("AGENT_HANDOFF_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANGUAGE", value)
    assert detect() == expect


def test_detect_explicit_override_still_wins(monkeypatch):
    """`AGENT_HANDOFF_LANG` 优先于一切系统设置——回归保护。"""
    monkeypatch.setenv("AGENT_HANDOFF_LANG", "en")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert detect() == "en"


def test_confirm_words_exist_in_every_language():
    """确认词必须来自文案表。写死在代码里等于只有部分语言能用。

    原版 `menu.py` 写死成 `("y","yes","是","好")`：繁中用户输入「要」「確定」
    会落到 else 分支，`--by-repo` 静默不生效，用户以为功能坏了。
    而 `scripts/check_i18n.py` 的白名单以整个文件为粒度把 menu.py 排除在
    CJK 字面量检查外，所以 CI 一直看不见这处。
    """
    from agent_handoff.menu import _is_yes

    for lang in LANGS:
        tr = Translator(lang)
        words = [w.strip() for w in tr.t("menu.confirm.yes").split(",") if w.strip()]
        assert words, f"{lang}: 确认词表为空"
        for w in words:
            assert _is_yes(tr, w), f"{lang}: 文案表里的 {w!r} 没被接受"
        # 英文惯例始终可用，任何语言的用户都可能习惯性敲 y
        assert _is_yes(tr, "y") and _is_yes(tr, "yes")
        # 否定回答不能误判
        for w in ("n", "no", "", "x"):
            assert not _is_yes(tr, w), f"{lang}: {w!r} 被误判为确认"


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


# ── 前端引用的键 ──────────────────────────────────────────────────────
#
# 前端此前完全没有键覆盖测试：`app.js` 里 `t("gui.xxx")` 打错一个字、或者
# `index.html` 的 `data-i18n` 指向一个不存在的键，都只会在用户点到那一屏时
# 静默显示 `??key??`。而 CI 全绿。
#
# 这类静态契约检查的做法取自同类项目：用测试断言「前端能发射的全部键」是
# 文案表的子集，而不是等运行时暴露。

STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_handoff" / "gui" / "static"
# 键在 `t(...)` 的实参里，但不一定紧跟左括号：三元表达式
# `t(cond ? "a" : "b")` 里键在中间。只匹配 `t("` 会静默漏掉那些键——
# 实测本项目就有 4 个键这样写，第一版正则一个都没抓到，于是测试假绿。
#
# 做法：先框出每个 `t(` 到配对右括号的片段，再从片段里取全部字符串字面量，
# 最后用「像文案键」的形状过滤（小写点分，至少一个点）。宁可多扫几个字符串
# 再靠形状筛掉，也不能漏掉真正在用的键。
_T_CALL = re.compile(r"\bt\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_STR_LIT = re.compile(r"""["']([^"']+)["']""")
_KEY_SHAPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
_HTML_I18N = re.compile(r"""data-i18n\s*=\s*["']([a-z][a-z0-9_.]*)["']""", re.I)
# 运行时按值域拼出来的键族。各自的值域已在别处覆盖：`band.*` 由 BAND_ORDER
# 决定、`gui.ago.*` 由时间单位表决定、`gui.theme.*` 由三个主题按钮决定。
_DYNAMIC_PREFIXES = ("band.", "gui.ago.", "gui.theme.", "cli.sweep.")


def _frontend_keys() -> set[str]:
    keys: set[str] = set()
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for args in _T_CALL.findall(js):
        for lit in _STR_LIT.findall(args):
            if _KEY_SHAPE.match(lit):
                keys.add(lit)
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    keys |= set(_HTML_I18N.findall(html))
    return {k for k in keys if not k.startswith(_DYNAMIC_PREFIXES)}


def test_frontend_keys_exist_in_every_language():
    """前端引用的每个键，三种语言都必须有。

    缺一个的后果不是报错而是界面上出现 `??gui.xxx??`——比崩溃更难发现，
    因为只在某一屏某一种语言下才看得到。
    """
    used = _frontend_keys()
    assert used, "扫不到任何前端键，说明正则或文件路径不对"
    for lang in LANGS:
        table = load(lang)
        missing = sorted(k for k in used if k not in table)
        assert not missing, f"{lang} 缺前端要用的键: {missing}"


def test_frontend_text_carries_no_markup():
    """前端文案不能带 HTML 标签。

    渲染走 `textContent`（防 XSS：文案表里的内容会和会话原文一起进 DOM），
    所以 `<b>` 之类标签会被当成字面量显示给用户。强调交给 CSS，
    严重性交给措辞——屏幕阅读器读不出粗体，读得出「不可逆」。
    """
    used = _frontend_keys()
    for lang in LANGS:
        table = load(lang)
        tagged = sorted(
            k for k in used
            if k in table and re.search(r"<[a-zA-Z/!]", table[k])
        )
        assert not tagged, f"{lang} 前端文案里有标签: {tagged}"


# ── 三份 README 的结构 ────────────────────────────────────────────────
#
# 文案表有 `check_i18n.py` 守着，README 一直没人守——三份各自演进，
# 实测曾漂移到 zh-Hans 比另两份少 3 个小节标题（网页界面 / 交互菜单 / 命令行
# 被写成加粗段落而不是 `###`）。后果是主语言读者的目录里没有这三项，
# 深链失效，而 CI 全绿。
#
# 只断言**结构**不断言字数：译文长度天然不同，按字数比对会一直误报。

README_ROOT = Path(__file__).resolve().parent.parent
READMES = {
    "zh-Hans": README_ROOT / "README.md",
    "en": README_ROOT / "README.en.md",
    "zh-Hant": README_ROOT / "README.zh-Hant.md",
}
_HEADING = re.compile(r"^(#{2,3})\s+(.+)$", re.M)


def _heading_levels(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    return [len(m.group(1)) for m in _HEADING.finditer(text)]


def test_readmes_have_the_same_section_structure():
    """三份 README 的标题层级序列必须一致。

    少一个小节意味着那份语言的读者看不到某个功能的入口。加章节时三份一起加，
    这条测试就是那个提醒——它不检查翻译质量，只保证结构不漏。
    """
    for path in READMES.values():
        assert path.is_file(), f"{path} 不存在"
    levels = {lang: _heading_levels(p) for lang, p in READMES.items()}
    base_lang, base = "zh-Hans", levels["zh-Hans"]
    for lang, seq in levels.items():
        if lang == base_lang:
            continue
        assert seq == base, (
            f"{lang} 的标题结构与 {base_lang} 不同："
            f"{len(seq)} 个标题 vs {len(base)} 个；层级序列 {seq} vs {base}"
        )


# ── 主题与原生控件 ────────────────────────────────────────────────────
#
# CSS 此前完全没有测试。`color-scheme` 缺失不会报错、不会影响布局——
# 只是深色主题下滚动条、下拉箭头、复选框仍然是浅色的，页面配好了色而
# 原生控件白得刺眼。这种缺陷只能靠人打开界面才看得见。

STYLE_CSS = STATIC_DIR / "style.css"


def test_color_scheme_declared_for_every_theme_state():
    """三种主题状态都要声明 `color-scheme`，浏览器才会重画它自己的控件。

    状态有三个（不是两个）：跟随系统的浅色与深色，加上手动指定。
    手动选择必须能压过系统偏好——所以深色要在媒体查询和属性选择器里各声明一次。
    """
    css = STYLE_CSS.read_text(encoding="utf-8")
    checks = {
        "裸 :root 声明 light": r":root\s*\{[^}]*color-scheme:\s*light",
        "系统深色（带手动浅色守卫）": (
            r":root:not\(\[data-theme=\"light\"\]\)\s*\{\s*color-scheme:\s*dark"
        ),
        "手动深色": r":root\[data-theme=\"dark\"\]\s*\{\s*color-scheme:\s*dark",
    }
    missing = [name for name, pat in checks.items() if not re.search(pat, css, re.S)]
    assert not missing, f"缺 color-scheme 声明: {missing}"


def test_no_color_lives_only_inside_a_media_query():
    """每个颜色令牌都必须在裸 `:root` 上有定义。

    只定义在媒体查询里的颜色，在「系统浅色 + 手动选深色」这类组合下会缺失，
    表现成某个元素透明或继承到意外的颜色。裸 `:root` 是兜底，
    媒体查询与属性选择器只**重定义**它。
    """
    css = STYLE_CSS.read_text(encoding="utf-8")
    root_block = re.search(r":root\s*\{(.*?)\n\}", css, re.S)
    assert root_block, "找不到裸 :root 块"
    base = set(re.findall(r"(--[a-z0-9-]+)\s*:", root_block.group(1)))

    # 媒体查询与 [data-theme] 块里出现的令牌
    overrides: set[str] = set()
    for block in re.findall(r"(?:@media[^{]*\{\s*)?:root(?:\[[^\]]+\]|:not\([^)]+\))?\s*\{(.*?)\n\s*\}", css, re.S):
        overrides |= set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

    orphans = sorted(overrides - base)
    assert not orphans, f"这些令牌只在覆盖块里定义，裸 :root 缺兜底: {orphans}"
