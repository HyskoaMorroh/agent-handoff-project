#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客观完成度证据：文件是否存在、符号是否真的被定义。

性能重写说明（原版最大的算法瓶颈）：
  原版对每个符号单独跑一次 `rg`。一份 12 个任务、每任务 6 个符号的计划文档
  = 72 次进程启动。Windows 上每次 subprocess 约 30-60 ms，光是进程创建就
  两三秒，还要 72 次全库遍历。

  这里改成一次调用：把所有符号合成一条交替正则（`\\b(def|class|...)\\s+(a|b|c)\\b`），
  让 ripgrep 用它的 Aho-Corasick / DFA 一遍扫完全库，用 `-o` 只输出命中的
  符号名，然后在 Python 里归类。72 次 → 1 次。

  没有 ripgrep 时退回 `git grep`（同样批量化），再退回纯 Python 扫描
  （只读 git 跟踪的文本文件，避开 node_modules 和二进制）。
"""
from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from .gitops import Proc, git_proc, run
from .plan import Task

# ripgrep 的单条正则不宜无限长；超过这个数量就分批，避免命令行长度上限
# （Windows 约 32767 字符）和正则编译爆栈。
SYMBOL_BATCH = 200
# 定义符号的关键字。跨语言：Python / JS / TS / Go / Rust / Java / C#。
DEF_KEYWORDS = r"def|class|function|const|let|var|export|struct|enum|interface|type|fn|func|impl|trait"
# 纯 Python 兜底扫描时只看这些扩展名，避免读二进制。
TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php", ".swift", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".sh", ".ps1", ".sql",
}


def _symbol_pattern(symbols: Iterable[str]) -> str:
    """把一批符号合成一条交替正则（Python / ripgrep 的 Rust 正则都吃）。

    `-o` 输出时我们要拿到符号名本身，所以定义关键字放在非捕获组里，
    符号交替放在最后——这样每条命中行的尾部就是符号名。
    """
    alts = "|".join(re.escape(s) for s in symbols)
    return rf"\b(?:{DEF_KEYWORDS})\s+(?:{alts})\b"


def _symbol_pattern_ere(symbols: Iterable[str]) -> str:
    """给 `git grep -E` 用的 POSIX ERE 版本。

    git 的 ERE 引擎不认 `(?:...)`（会报 "Invalid preceding regular expression"），
    也不保证认 `\\s`。改用捕获组 + `[[:space:]]`。原版对每个符号单独跑一次
    `git grep`，从没触发这个差异；批量化之后必须显式区分两种方言。
    """
    alts = "|".join(re.escape(s) for s in symbols)
    return rf"\b({DEF_KEYWORDS})[[:space:]]+({alts})\b"


def _defined_in_text(text: str, symbols: set[str]) -> set[str]:
    """在一段文本里找出哪些符号被定义了。一次正则扫完，不是每符号一次。"""
    if not symbols:
        return set()
    rx = re.compile(_symbol_pattern(symbols))
    found: set[str] = set()
    for m in rx.finditer(text):
        # 命中串形如 `def foo` / `export const bar`；末段就是符号名。
        name = m.group(0).split()[-1]
        if name in symbols:
            found.add(name)
            if len(found) == len(symbols):
                break
    return found


def _rg_batch(repo: Path, symbols: list[str]) -> tuple[set[str], bool]:
    """用一次 ripgrep 调用找出这批符号里哪些被定义了。

    返回 (命中集合, ripgrep 是否可用)。不可用时调用方退回其他策略。
    """
    if shutil.which("rg") is None:
        return set(), False
    found: set[str] = set()
    for i in range(0, len(symbols), SYMBOL_BATCH):
        chunk = symbols[i : i + SYMBOL_BATCH]
        p = run(
            [
                "rg",
                "--no-messages",
                "--no-filename",
                "--no-line-number",
                "-o",
                "-e",
                _symbol_pattern(chunk),
            ],
            repo,
            timeout=120,
        )
        # 退出码 1 = 没有匹配，那是正常结果不是错误；2 才是真出错。
        if p.code not in (0, 1):
            return found, False
        wanted = set(chunk)
        for line in p.out.splitlines():
            parts = line.split()
            if parts and parts[-1] in wanted:
                found.add(parts[-1])
    return found, True


def _git_grep_batch(repo: Path, symbols: list[str]) -> tuple[set[str], bool]:
    """ripgrep 缺席时的第一退路：git grep，同样批量化。

    只搜 git 跟踪的文件，天然避开 node_modules 与构建产物。
    """
    found: set[str] = set()
    for i in range(0, len(symbols), SYMBOL_BATCH):
        chunk = symbols[i : i + SYMBOL_BATCH]
        # 不加 `--`：git grep 会把 `--` 之后的当路径，模式必须在它之前。
        p: Proc = git_proc(repo, "grep", "-hoE", _symbol_pattern_ere(chunk), timeout=120)
        # 1 = 没有匹配（正常结果）；其余都是真出错，含 128（正则被拒）。
        if p.code not in (0, 1):
            return found, False
        wanted = set(chunk)
        for line in p.out.splitlines():
            parts = line.split()
            if parts and parts[-1] in wanted:
                found.add(parts[-1])
    return found, True


def _python_scan(repo: Path, symbols: list[str]) -> set[str]:
    """最后的兜底：只读 git 跟踪的源文件，一遍正则扫完。

    比原版的"每符号一次全库遍历"快一个数量级，因为文件只读一遍，
    正则也只编译一次。
    """
    wanted = set(symbols)
    p = git_proc(repo, "ls-files", "-z", timeout=60)
    if p.ok and p.out:
        rels = [x for x in p.out.split("\0") if x]
        files = [repo / r for r in rels if Path(r).suffix.lower() in TEXT_SUFFIXES]
    else:
        # 不是 git 仓库或 ls-files 失败：退回受限遍历。
        import os

        from .gitops import WALK_SKIP

        files = []
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in WALK_SKIP]
            for n in names:
                if Path(n).suffix.lower() in TEXT_SUFFIXES:
                    files.append(Path(root) / n)

    rx = re.compile(_symbol_pattern(wanted)) if wanted else None
    found: set[str] = set()
    if rx is None:
        return found
    for fp in files:
        if len(found) == len(wanted):
            break
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in rx.finditer(text):
            name = m.group(0).split()[-1]
            if name in wanted:
                found.add(name)
    return found


def resolve_symbols(repo: Path, tasks: list[Task]) -> dict[str, bool]:
    """一次问清全部任务的全部符号是否被定义。

    先看每个任务自己声明的文件——那是最可能的定义位置，且读几个小文件比
    扫全库便宜得多。剩下的符号才交给全库检索，且合成一条正则一次搞定。
    """
    all_symbols: list[str] = []
    seen: set[str] = set()
    for t in tasks:
        for s in t.symbols:
            if s not in seen:
                seen.add(s)
                all_symbols.append(s)
    if not all_symbols:
        return {}

    result: dict[str, bool] = dict.fromkeys(all_symbols, False)

    # 第一轮：任务自己声明的文件。每个文件只读一次，即使被多个任务引用。
    hint_cache: dict[str, str] = {}
    for t in tasks:
        pending = {s for s in t.symbols if not result.get(s)}
        if not pending:
            continue
        for rel in t.files:
            if not pending:
                break
            fp = repo / rel
            if not fp.is_file():
                continue
            key = str(fp)
            if key not in hint_cache:
                try:
                    hint_cache[key] = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    hint_cache[key] = ""
            for name in _defined_in_text(hint_cache[key], pending):
                result[name] = True
            pending = {s for s in pending if not result[s]}

    # 第二轮：剩下的符号一次全库检索。
    remaining = [s for s in all_symbols if not result[s]]
    if remaining:
        found, ok = _rg_batch(repo, remaining)
        if not ok:
            found, ok = _git_grep_batch(repo, remaining)
        if not ok:
            found = _python_scan(repo, remaining)
        for s in found:
            result[s] = True
    return result


def score_tasks(repo: Path, tasks: list[Task]) -> dict[int, dict]:
    """每个任务的客观完成证据：文件是否存在、符号是否被定义。"""
    if not tasks:
        return {}
    symbol_state = resolve_symbols(repo, tasks)

    report: dict[int, dict] = {}
    for t in tasks:
        present = [f for f in t.files if (repo / f).exists()]
        missing = [f for f in t.files if not (repo / f).exists()]
        syms_ok, syms_missing = [], []
        for s in dict.fromkeys(t.symbols):
            (syms_ok if symbol_state.get(s) else syms_missing).append(s)
        file_ratio = len(present) / len(t.files) if t.files else 0.0
        denom = len(syms_ok) + len(syms_missing)
        sym_ratio = len(syms_ok) / denom if denom else 0.0
        if t.files and t.symbols:
            complete = file_ratio == 1.0 and sym_ratio == 1.0
        elif t.files:
            complete = file_ratio == 1.0
        elif t.symbols:
            complete = sym_ratio == 1.0
        else:
            complete = False
        report[t.num] = {
            "title": t.title,
            "files_present": present,
            "files_missing": missing,
            "symbols_ok": syms_ok,
            "symbols_missing": syms_missing,
            "file_ratio": file_ratio,
            "symbol_ratio": sym_ratio,
            "complete": complete,
            "steps": len(t.steps),
        }
    return report
