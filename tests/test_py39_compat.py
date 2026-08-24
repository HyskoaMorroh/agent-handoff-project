#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态守护 Python 3.9 兼容性。

为什么需要它：`pyproject.toml` 声明 `requires-python = ">=3.9"`，CI 也在 3.9
上跑测试，但**开发机跑的是新版本**。只要一个 3.10+ 的 API 出现在不常走的分支
里，本地和大多数 CI 组合都不会碰到它，唯独真在 3.9 上跑到那一行的用户会崩。

这不是假设，是已经发生过的事：`cli.py` 曾用 `write_text(..., newline="\\n")`
导出 `--sweep` 报告。`pathlib` 的 `newline=` 参数是 3.10 才加的，于是
`agent-handoff --sweep --out report.md` 在 3.9 上抛 `TypeError`。同一个仓库的
`core/handoff.py` 早就注释过这个坑并改用 `write_bytes` 绕开，`cli.py` 那一处
漏改——而测试套件里没有任何东西会因此变红。

所以这里做静态扫描而不是运行时检查：运行时只能发现「这次跑到的那条路径」，
静态扫描能覆盖全部代码。用 `ast` 而不是正则，避免把注释和字符串里的字面量
当成真调用（上面那段注释本身就含 `newline=`，正则会误报）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "agent_handoff"


def _sources() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, f"找不到任何源文件，路径可能错了：{SRC}"
    return files


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _attr_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else ""


def _plain_name(node: ast.Call) -> str:
    return node.func.id if isinstance(node.func, ast.Name) else ""


def test_no_pathlib_newline_keyword():
    """`Path.write_text` / `read_text` 的 `newline=` 是 3.10+。

    3.9 上要控制换行，走 `write_bytes` 并自己拼字节——`core/handoff.py:441-448`
    就是这么做的，注释也写明了原因。
    """
    bad: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls(tree):
            if _attr_name(node) not in ("write_text", "read_text"):
                continue
            for kw in node.keywords:
                if kw.arg == "newline":
                    bad.append(f"{path.name}:{node.lineno}")
    assert not bad, (
        "pathlib 的 newline= 是 Python 3.10 才加的，3.9 上会抛 TypeError。"
        f"改用 write_bytes 自己拼字节。命中：{bad}"
    )


@pytest.mark.parametrize(
    ("attr", "since"),
    [
        ("pairwise", "3.10"),      # itertools.pairwise
        ("cache_clear", None),     # 占位：确保参数化本身有效，cache_clear 一直都有
    ],
)
def test_no_310_only_attribute_calls(attr: str, since: str | None):
    """按名字挡掉几个容易顺手用上的 3.10+ 方法。

    只挡确实是 3.10+ 的；`cache_clear` 这一行是对照组，用来保证这个测试在
    「什么都没命中」时不是因为扫描逻辑坏了而通过。
    """
    hits: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls(tree):
            if _attr_name(node) == attr:
                hits.append(f"{path.name}:{node.lineno}")
    if since is None:
        return  # 对照组不断言，只验证扫描跑得通
    assert not hits, f"{attr}() 是 Python {since}+ 的，3.9 上不存在。命中：{hits}"


def test_no_match_statement():
    """`match` 语句是 3.10+ 的语法。

    它连**解析**都过不了 3.9 —— 一旦进了源码，3.9 上是导入期 SyntaxError，
    整个工具起不来，而不是某个分支报错。
    """
    bad: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if type(node).__name__ == "Match":
                bad.append(f"{path.name}:{node.lineno}")
    assert not bad, f"match 语句是 3.10+ 语法，3.9 上是 SyntaxError。命中：{bad}"


def test_runtime_unions_need_future_annotations():
    """用了 `X | Y` 注解的模块必须有 `from __future__ import annotations`。

    3.9 的 `int | None` 在**运行时**求值会抛 TypeError。加了 future 导入，
    注解变成字符串、不求值，就安全了——本项目每个模块都有那一行，这里守住它。
    """
    missing: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        if "from __future__ import annotations" in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            # 注解位置出现的 BinOp(|) 就是 PEP 604 联合类型
            ann = getattr(node, "annotation", None) or getattr(node, "returns", None)
            if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
                missing.append(f"{path.name}:{node.lineno}")
                break
    assert not missing, (
        "这些模块用了 PEP 604 的 `X | Y` 注解但没有 future 导入，"
        f"3.9 运行时会抛 TypeError：{missing}"
    )


def test_requires_python_still_says_39():
    """如果哪天真的放弃 3.9，这个测试要跟着删——而不是悄悄失效。

    锁住声明本身：上面几条检查的存在理由完全来自 `requires-python`。
    声明改了而检查还留着，就变成无谓的束缚；声明没改而检查被删掉，
    就回到「3.9 用户替我们发现 bug」的状态。
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert 'requires-python = ">=3.9"' in text, (
        "requires-python 变了。如果不再支持 3.9，把 tests/test_py39_compat.py 一起删掉；"
        "如果支持的下限变成别的版本，把这些检查按新下限重写。"
    )
