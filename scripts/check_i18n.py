#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验三种语言的文案文件互相对齐。CI 与本地都跑。

三件事必须一致，缺一个界面上就会出现 ??key?? 或者格式化异常：
  1. 键集合完全相同
  2. 每个键的占位符集合完全相同（三种语言语序不同，位置参数会错位）
  3. 没有空值
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "src" / "agent_handoff" / "i18n"
BASE = "zh-Hans"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


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

    print(f"{len(tables)} languages, {len(base)} keys each — all aligned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
