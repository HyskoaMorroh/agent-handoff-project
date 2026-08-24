#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三语文案。简体中文是基准，繁体与英文缺键时回退到它。

为什么不用 gettext：gettext 要编译 .mo，而这个工具的全部卖点是「会话已经
死了，我不想再装任何东西」。JSON + 一个 dict 查表，零依赖、可读、可 diff。

键名用点分命名空间：`cli.*` 命令行、`gui.*` 界面、`doc.*` 生成的交接文档、
`prompt.*` 开场提示词、`band.*` 体征判定。占位符统一用 `{name}` 具名形式，
因为三种语言的语序不同，位置参数会错位。
"""
from __future__ import annotations

import json
import locale
import os
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BASE_LANG = "zh-Hans"
LANGS = ("zh-Hans", "zh-Hant", "en")

# 语言名用各自的语言写，这样切换菜单里每一项都认得出自己。
LANG_NAMES = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁體中文",
    "en": "English",
}

# 侧栏窄，切换钮只放得下一两个字，同样用各自的语言写。
# 放在这里而不是前端：新增语言时前端写死的三元判断会把它静默显示成 EN。
LANG_SHORT = {
    "zh-Hans": "简",
    "zh-Hant": "繁",
    "en": "EN",
}

_cache: dict[str, dict[str, str]] = {}


def _load_raw(lang: str) -> dict[str, str]:
    fp = HERE / f"{lang}.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def available() -> tuple[str, ...]:
    """真正带有文案文件的语言。缺文件的语言不该出现在切换菜单里。"""
    return tuple(lg for lg in LANGS if (HERE / f"{lg}.json").is_file())


def normalize(lang: str | None) -> str:
    """把用户给的语言标记归到三个受支持值之一。

    接受 `zh`、`zh_CN`、`zh-TW`、`zh-Hant-HK`、`en_US.UTF-8` 等常见写法。
    认不出来时返回基准语言，而不是抛异常——语言选错顶多字难看，不该让工具罢工。

    还要认 Windows 风格的 locale 全名。`locale.getlocale()` 在 Windows 上
    返回的不是 BCP-47 标记，而是 `Chinese (Traditional)_Taiwan`、
    `Chinese (Simplified)_China`、`English_United States` 这种人类可读名。
    实测不认它的后果是：一台繁体中文 Windows 只要没设 `LANG`（Windows 的默认
    状态），字符串以 `C` 开头、既不匹配 `en` 也不匹配 `zh`，于是落到基准语言，
    **整个界面和生成的交接文档全变简体**。简体机器上「恰好正确」掩盖了这个
    缺陷，繁中机器上必然错。
    """
    if not lang:
        return BASE_LANG
    tag = str(lang).replace("_", "-").split(".")[0].strip().lower()
    if not tag:
        return BASE_LANG

    # Windows locale 全名先转成 BCP-47 再走下面的通用分支。
    # 判据用地区名而不是脚本名：`Chinese (Traditional)_Taiwan` 与
    # `Chinese_Taiwan`（旧写法，无脚本段）都要认出来。
    if tag.startswith("chinese"):
        if "traditional" in tag or any(
            r in tag for r in ("taiwan", "hong kong", "hongkong", "macao", "macau")
        ):
            return "zh-Hant"
        return "zh-Hans"
    if tag.startswith("english"):
        return "en"

    if tag.startswith("en"):
        return "en"
    if tag.startswith("zh"):
        # 繁体的标志：Hant 脚本，或港澳台地区码。其余中文按简体。
        parts = set(tag.split("-"))
        if "hant" in parts or parts & {"tw", "hk", "mo"}:
            return "zh-Hant"
        return "zh-Hans"
    for cand in LANGS:
        if tag == cand.lower():
            return cand
    return BASE_LANG


# POSIX 里 `C` 与 `POSIX` 的含义是「不做本地化」，不是「某种语言」。
# 容器、CI、cron、纯 SSH 会话默认就是 `LANG=C.UTF-8`——把它当成有效标记的
# 后果是：`normalize("c")` 认不出，落到基准语言，于是一台英文 Docker 里的
# 英文用户拿到简体中文输出。惯例是把它视为「无语言偏好」并继续往下探测。
_NEUTRAL_LOCALES = frozenset({"c", "posix", "c.utf-8", "c.utf8", "und"})


def detect() -> str:
    """探测该用哪种语言：显式环境变量优先，然后系统区域设置。

    `AGENT_HANDOFF_LANG` 是本工具自己的开关，优先于系统设置，方便在中文系统上
    临时产出英文交接文档（给英文项目用）。

    `LANGUAGE` 与其他变量的语义不同：它是**冒号分隔的优先级列表**
    （`fr:en` 意为「法语优先、英语次选」）。只取第一段就返回会让 `fr:en`
    落到基准语言，而按 gettext 惯例应当回退到 `en`——所以这里逐段试，
    第一个能认出的受支持语言胜出。
    """
    for var in ("AGENT_HANDOFF_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        val = os.environ.get(var)
        if not val:
            continue
        for seg in val.split(":"):
            seg = seg.strip()
            if not seg or seg.split(".")[0].strip().lower() in _NEUTRAL_LOCALES:
                continue
            if seg.lower() in _NEUTRAL_LOCALES:
                continue
            got = normalize(seg)
            # `normalize` 认不出时返回基准语言。这里要区分「真的探测到简体」
            # 和「认不出所以兜底」：后者应当继续试下一段 / 下一个变量，
            # 否则 `LANGUAGE=fr:en` 会停在 fr 上，永远走不到 en。
            if got != BASE_LANG or _looks_like_base(seg):
                return got
    try:
        sys_lang = locale.getlocale()[0] or ""
    except (ValueError, TypeError):
        sys_lang = ""
    return normalize(sys_lang)


def _looks_like_base(tag: str) -> bool:
    """这个标记是否真的在说基准语言（而不是被兜底成基准语言）。"""
    low = str(tag).replace("_", "-").split(".")[0].strip().lower()
    if low.startswith("chinese"):
        return True
    return low.startswith("zh")


class Translator:
    """一个语言的文案表。缺键回退基准语言，再缺就把键名原样吐出来。

    绝不因为缺一句翻译就抛异常：这个工具在会话已经出事之后才被运行，
    那时候最不需要的就是它自己也崩。缺键会显示为 `??key??`，一眼能看出来该补。
    """

    __slots__ = ("lang", "_table", "_base")

    def __init__(self, lang: str | None = None) -> None:
        self.lang = normalize(lang) if lang else detect()
        if self.lang not in _cache:
            _cache[self.lang] = _load_raw(self.lang)
        if BASE_LANG not in _cache:
            _cache[BASE_LANG] = _load_raw(BASE_LANG)
        self._table = _cache[self.lang]
        self._base = _cache[BASE_LANG]

    def __call__(self, key: str, **kw: Any) -> str:
        return self.t(key, **kw)

    def t(self, key: str, **kw: Any) -> str:
        raw = self._table.get(key) or self._base.get(key)
        if raw is None:
            return f"??{key}??"
        if not kw:
            return raw
        try:
            return raw.format(**kw)
        except (KeyError, IndexError, ValueError):
            # 占位符对不上时给出原文而不是炸掉；漏参数比崩溃可修。
            return raw

    def table(self) -> dict[str, str]:
        """完整合并表，供 GUI 一次性取走全部文案（前端不再逐条请求）。"""
        merged = dict(self._base)
        merged.update(self._table)
        return merged


_default: Translator | None = None


def default() -> Translator:
    global _default
    if _default is None:
        _default = Translator()
    return _default


def set_default(lang: str | None) -> Translator:
    global _default
    _default = Translator(lang)
    return _default


def t(key: str, **kw: Any) -> str:
    return default().t(key, **kw)
