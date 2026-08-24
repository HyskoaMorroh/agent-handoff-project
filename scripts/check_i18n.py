#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验三种语言的文案文件互相对齐。CI 与本地都跑。

四件事必须一致，缺一个界面上就会出现 ??key?? 或者语言错乱：
  1. 键集合完全相同
  2. 每个键的占位符集合完全相同（三种语言语序不同，位置参数会错位）
  3. 没有空值
  4. 源码里没有绕过文案表的中文字面量（那种串永远不跟随语言）
"""
from __future__ import annotations

import io
import json
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "src" / "agent_handoff" / "i18n"
SRC = ROOT / "src" / "agent_handoff"
BASE = "zh-Hans"
PLACEHOLDER = re.compile(r"\{(\w+)\}")
CJK = re.compile(r"[一-鿿]")

# 允许带中文字面量的地方，附理由。加白名单前先问：这串会不会被用户看见？
# 会被看见就该进文案表；只用来「认」中文输入或标注语言自己，才可以留在代码里。
CJK_LITERAL_ALLOW = {
    # 解析中文计划文档与中文错误标记用的模式，是输入侧，不是输出文案。
    "core/plan.py": "解析中文计划文档的正则",
    "core/vitals.py": "识别中文致命错误标记的正则",
    # 语言名与短标签按设计各用自己的语言写。
    "i18n/__init__.py": "LANG_NAMES / LANG_SHORT 各自用自己的语言",
    # 历史文件名回退。图文说明曾叫 `使用说明.html`，旧检出里还有这个名字，
    # 所以它是**查找用的输入**而不是给用户看的文案——翻译它反而会让旧检出找不到。
    # 路径推断统一到 platform.find_guide() 之后，这个字面量从 menu.py 搬到了这里。
    "platform.py": "旧版说明文件名 使用说明.html（查找输入，非输出文案）",
    # 识别 harness 注入到 user 轮里的样板。中文的「继续」是**要认出来的输入**，
    # 不是输出给用户的文案：转录里只写「继续」的那一轮不承载任何诉求，
    # 把它当成用户原话带进新会话，等于让新会话从一句状态标记开始理解项目。
    "core/transcript.py": "识别转录里中文样板轮的模式（输入侧，非输出文案）",
}


def _cjk_literals_in_python(fp: Path) -> list[tuple[int, str]]:
    """找出 fp 里带中文的字符串字面量，跳过注释与文档字符串。

    用真正的分词器而不是正则：注释和文档字符串里的中文是解释代码用的，
    完全正当，靠正则区分不开就只能全靠人工复核，那等于没有检查。
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(fp.read_text(encoding="utf-8")).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, UnicodeDecodeError) as exc:
        return [(0, f"无法分词：{exc}")]
    out: list[tuple[int, str]] = []
    prev = None
    for tok in toks:
        if tok.type == tokenize.STRING and CJK.search(tok.string):
            # 文档字符串是某个块的第一条语句，前一个有效 token 必然是缩进或换行。
            is_doc = prev in (None, tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL)
            if not is_doc:
                out.append((tok.start[0], tok.string.strip()[:60]))
        if tok.type not in (tokenize.COMMENT, tokenize.NL):
            prev = tok.type
    return out


def check_source_has_no_stray_cjk() -> list[str]:
    """源码里的中文字面量要么在白名单里，要么是缺陷。

    这一类缺陷 CI 以前抓不到：文案表三语对齐得再好，只要有一句写死在代码里，
    切了语言它也不动。实测发现过 `"；".join(...)` 让英文输出里出现全角分号、
    以及压缩窗口分隔标题把中文写进英文交接文档。
    """
    problems: list[str] = []
    for fp in sorted(SRC.rglob("*.py")):
        rel = fp.relative_to(SRC).as_posix()
        if rel in CJK_LITERAL_ALLOW:
            continue
        for line, text in _cjk_literals_in_python(fp):
            problems.append(
                f"{rel}:{line} 中文字面量绕过了文案表 -> {text}"
                "（该进 i18n/*.json；确属输入侧模式或语言自称，则加进 CJK_LITERAL_ALLOW 并写明理由）"
            )
    return problems


def main() -> int:
    files = sorted(I18N.glob("*.json"))
    if not files:
        print(f"no translation files under {I18N}", file=sys.stderr)
        return 1

    tables: dict[str, dict[str, str]] = {}
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"{fp.name}: invalid JSON — {exc}", file=sys.stderr)
            return 1
        if not isinstance(data, dict):
            print(f"{fp.name}: top level must be an object", file=sys.stderr)
            return 1
        tables[fp.stem] = data

    if BASE not in tables:
        print(f"base language {BASE}.json is missing", file=sys.stderr)
        return 1

    base = tables[BASE]
    problems: list[str] = []

    for lang, table in sorted(tables.items()):
        missing = sorted(set(base) - set(table))
        extra = sorted(set(table) - set(base))
        if missing:
            problems.append(f"{lang}: {len(missing)} missing keys -> {', '.join(missing[:12])}")
        if extra:
            problems.append(f"{lang}: {len(extra)} unknown keys -> {', '.join(extra[:12])}")
        for key, value in sorted(table.items()):
            if not isinstance(value, str):
                problems.append(f"{lang}: {key} is not a string")
                continue
            if not value.strip():
                problems.append(f"{lang}: {key} is empty")
            if key in base:
                want = set(PLACEHOLDER.findall(base[key]))
                got = set(PLACEHOLDER.findall(value))
                if want != got:
                    problems.append(
                        f"{lang}: {key} placeholders differ — "
                        f"base {sorted(want)} vs {sorted(got)}"
                    )

    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        print(f"\n{len(problems)} problem(s)", file=sys.stderr)
        return 1

    stray = check_source_has_no_stray_cjk()
    if stray:
        for p in stray:
            print(p, file=sys.stderr)
        print(f"\n{len(stray)} problem(s)", file=sys.stderr)
        return 1

    print(f"{len(tables)} languages, {len(base)} keys each — all aligned")
    print("源码没有绕过文案表的中文字面量")
    return 0


if __name__ == "__main__":
    sys.exit(main())
