#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/i18n/*.json 与页面模板合成单文件图文说明 docs/guide.html。

为什么要生成而不是手写三份 HTML：原版的 使用说明.html 是一份 60 KB 的单文件，
好处是双击就能看、不需要服务器。但三种语言手写三份，任何一处措辞改动都要
改三遍，迟早漂移。这里把文案抽到 JSON、结构留在模板，生成出的仍然是
一份自包含的单文件（三种语言全部内嵌，切换不发请求）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "docs" / "i18n"
TEMPLATE = ROOT / "docs" / "guide.template.html"
OUT = ROOT / "docs" / "guide.html"
BASE = "zh-Hans"
ORDER = ("zh-Hans", "zh-Hant", "en")


def _check_markup_channel(tables: dict[str, dict[str, str]]) -> list[str]:
    """模板里用 textContent 渲染的键不能带 HTML 标签。

    页面有两条渲染通道：`rich()` 走 innerHTML（文案里可以有 `<code>`、
    `<strong>`），而模板里的 `data-g="..."` 属性走 textContent。后者写了标签
    不会报错，会把 `<em>兜底</em>` 原样显示在正文里——实测就这么漏出去过一次，
    而且只有肉眼看渲染结果才发现。

    所以在这里比对：凡是以 `data-g` 出现在模板里的键，一律不许带尖括号标签。
    """
    if not TEMPLATE.is_file():
        return []
    html = TEMPLATE.read_text(encoding="utf-8")
    text_keys = set(re.findall(r'data-g="([^"]+)"', html))
    tag = re.compile(r"</?[a-zA-Z][^>]*>")
    out: list[str] = []
    for lang, table in tables.items():
        for key in sorted(text_keys):
            val = table.get(key)
            if isinstance(val, str) and tag.search(val):
                found = tag.search(val)
                out.append(
                    f"guide.{lang}: {key} 是 textContent 渲染的键，不能带 HTML 标签"
                    f"（找到 {found.group(0)!r}）——改用纯文本，或把它挪到 rich() 渲染的位置"
                )
    return out


def main() -> int:
    tables: dict[str, dict[str, str]] = {}
    for lang in ORDER:
        fp = I18N / f"guide.{lang}.json"
        if not fp.is_file():
            print(f"missing {fp}", file=sys.stderr)
            return 1
        try:
            tables[lang] = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"{fp.name}: {exc}", file=sys.stderr)
            return 1

    base = tables[BASE]
    problems = []
    for lang, table in tables.items():
        missing = sorted(set(base) - set(table))
        extra = sorted(set(table) - set(base))
        if missing:
            problems.append(f"guide.{lang}: missing {', '.join(missing[:10])}")
        if extra:
            problems.append(f"guide.{lang}: unknown {', '.join(extra[:10])}")
    problems.extend(_check_markup_channel(tables))
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 1

    if not TEMPLATE.is_file():
        print(f"missing {TEMPLATE}", file=sys.stderr)
        return 1
    html = TEMPLATE.read_text(encoding="utf-8")

    # 参数说明直接嵌入程序自己的 i18n 表里 `cli.arg.*` 那部分：文档与 --help
    # 共用一处文案，改一边不可能忘另一边。只取需要的键，不把整张表塞进页面。
    cli_dir = ROOT / "src" / "agent_handoff" / "i18n"
    cli: dict[str, dict[str, str]] = {}
    for lang in ORDER:
        fp = cli_dir / f"{lang}.json"
        if not fp.is_file():
            print(f"missing {fp}", file=sys.stderr)
            return 1
        table = json.loads(fp.read_text(encoding="utf-8"))
        cli[lang] = {k: v for k, v in table.items() if k.startswith("cli.arg.")}
        if not cli[lang]:
            print(f"{fp.name}: no cli.arg.* keys", file=sys.stderr)
            return 1

    # </script> 出现在 JSON 字符串里会提前关闭标签。
    blob = json.dumps(tables, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    cli_blob = json.dumps(cli, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    for placeholder in ("__GUIDE_I18N__", "__CLI_I18N__", "__VERSION__"):
        if placeholder not in html:
            print(f"template has no {placeholder} placeholder", file=sys.stderr)
            return 1
    html = html.replace("__GUIDE_I18N__", blob).replace("__CLI_I18N__", cli_blob)

    # 版本号显示在顶栏。从包里读而不是手写：文档说它是 v2.3.0 而程序是 v2.4.0
    # 这种不一致，读者只会当成文档不可信。
    version = ""
    init = ROOT / "src" / "agent_handoff" / "__init__.py"
    if init.is_file():
        m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
        if m:
            version = "v" + m.group(1)
    if not version:
        print("cannot read __version__ from the package", file=sys.stderr)
        return 1
    html = html.replace("__VERSION__", version)

    OUT.write_text(html, encoding="utf-8", newline="")
    size = OUT.stat().st_size
    print(f"{OUT.relative_to(ROOT).as_posix()} — {len(base)} keys x {len(tables)} languages, {size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
