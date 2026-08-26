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

from ..platform import norm_path
from .gitops import Proc, git_proc, run
from .plan import Task

# ripgrep 的单条正则不宜无限长；超过这个数量就分批，避免命令行长度上限
# （Windows 约 32767 字符）和正则编译爆栈。
SYMBOL_BATCH = 200
# 定义符号的关键字。跨语言：Python / JS / TS / Go / Rust / Java / C#。
DEF_KEYWORDS = r"def|class|function|const|let|var|export|struct|enum|interface|type|fn|func|impl|trait"
# 可以出现在「声明位置的名字」之前的修饰符。
# 只有这些词能挡在行首与符号名之间；注释符（`//`、`*`、`#`）不在其中，
# 于是 `// interface undo 由 store 提供` 这类**对符号的引用**天然不会被当成定义。
DECL_MODIFIERS = r"export|default|public|private|protected|static|readonly|abstract|final|override|async|get|set|declare"
# 纯 Python 兜底扫描时只看这些扩展名，避免读二进制。
TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php", ".swift", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".sh", ".ps1", ".sql",
}


def _symbol_pattern(symbols: Iterable[str]) -> str:
    """把一批符号合成一条交替正则（Python / ripgrep 的 Rust 正则都吃）。

    识别三种定义形态。只有第一种是原版有的，而后两种在 TS / Vue / 现代 JS 里
    才是主流写法——少了它们，一个写满 `undo: () => void` 的仓库会被判成
    「符号全缺」，接续会话于是重做已经做完的工作：

      A 关键字前缀   `def foo` / `export const bar` / `interface Baz`
      B 声明位属性   `undo: () => void`（interface 成员、对象字面量属性）
      C 方法简写     `performActionWithoutHistory(action: () => void) {`

    B 与 C 靠「符号处在声明位置」来区分定义与调用：符号必须在行首（只允许
    缩进与修饰符挡在前面），所以 `intent.undo()` 和 `this.perform...()` 这类
    带点号的调用不会命中。C 还要求行尾是 `{`，把 `undo()` 这种裸调用排除；
    并要求左括号后面不是另一个左括号，从而把 `perform...(() => {`（传回调的
    调用）与 `perform...(action: ...) {`（方法定义）分开。

    不用前后向断言：ripgrep 的 Rust 正则引擎不支持 lookaround，用了会让 rg
    以退出码 2 失败，然后静默退到更慢的后端。所有排除都用字符类表达。

    三个分支各带一个命名组（`nameA` / `nameB` / `nameC`）——同一个名字不能在
    多个分支里重复定义（Python 的 re 会报 "redefinition of group name"），
    所以由 `_pick_name` 读出实际命中的那一个。

    命中片段一律由 Python 侧用同一条正则复核并读取名字组，所以三条后端
    对「什么算定义」的答案必然一致。
    """
    alts = "|".join(re.escape(s) for s in symbols)
    mods = rf"(?:(?:{DECL_MODIFIERS})[ \t]+)*"
    a = rf"\b(?:{DEF_KEYWORDS})[ \t]+(?P<nameA>{alts})\b"
    b = rf"^[ \t]*{mods}(?P<nameB>{alts})[ \t]*:"
    c = (
        rf"^[ \t]*{mods}(?P<nameC>{alts})[ \t]*"
        rf"\((?:\)|[^(\n][^\n]{{0,200}}\))[ \t]*(?::[^{{\n]{{0,160}})?[ \t]*\{{[ \t]*$"
    )
    return f"(?:{a})|(?:{b})|(?:{c})"


def _pick_name(m: re.Match[str]) -> str:
    """读出这次命中的符号名：三个分支里只有一个组会有值。"""
    return m.group("nameA") or m.group("nameB") or m.group("nameC") or ""


# 行注释与块注释。注释里对符号的**引用**不是定义，但 `interface`/`type` 这类
# 关键字在中文注释里很自然（「// interface undo 由 store 提供」），A 分支会把
# 它当成定义。假阳性比假阴性更危险：它会让 update_plan 勾掉从未实现的步骤，
# 待办从计划文档里永久消失。所以判定前先把注释挖空。
_COMMENT_RX = re.compile(
    r"/\*.*?\*/"          # C 风格块注释
    r"|(?<![:\w])//[^\n]*"  # 行注释；`https://` 里的 // 不算（前面是字母或冒号）
    r"|^[ \t]*\*[^\n]*"   # JSDoc 续行
    r"|#[^\n]*",          # Python / Shell / YAML
    re.S | re.M,
)
# 单行字符串字面量。字符串里的 `def foo` 是数据不是定义——测试夹具、代码
# 生成模板、错误信息里都会出现。只处理不跨行的字面量：跨行意味着模板字符串
# 或未配对的引号，那时贪心匹配会把真实定义一起挖掉，宁可漏挖不可错挖。
_STRING_RX = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\\n])*`")


def _strip_noise(text: str) -> str:
    """把注释与单行字符串替换成等长空白，保留行列位置。

    不能直接删除：B / C 两个分支靠「符号在行首」判定，删掉内容会让后面的
    文本左移，把注释后的代码错判成行首。用空格填充可以保持所有偏移不变。

    顺序要紧：先挖注释再挖字符串。注释里的撇号（`don't`）如果留到第二步，
    会被当成字符串起点，一路吞到下一个撇号，把中间的真实定义抹掉。
    """
    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return _STRING_RX.sub(blank, _COMMENT_RX.sub(blank, text))


def _symbol_pattern_ere(symbols: Iterable[str]) -> str:
    """给 `git grep -E` 用的 POSIX ERE 版本。

    git 的 ERE 引擎不认 `(?:...)`（会报 "Invalid preceding regular expression"），
    也不保证认 `\\s`。改用捕获组 + `[[:space:]]`。原版对每个符号单独跑一次
    `git grep`，从没触发这个差异；批量化之后必须显式区分两种方言。

    **也不用 `\\b`。** 那是 GNU 扩展，不在 POSIX ERE 里。git 用平台的正则实现，
    于是同一条模式在不同系统上结果不同：

      | 平台 | git 的正则来源 | `\\b` |
      | --- | --- | --- |
      | Linux | glibc | 支持 |
      | Git for Windows | 自带 glibc 兼容层 | 支持 |
      | macOS（Homebrew git 2.55.0） | 系统 BSD libc | **不支持** |

    不支持时的表现是最坏的一种：不报错、不警告，`git grep` 以退出码 1
    （「没有匹配」）正常返回，于是 `_git_grep_batch` 交回一个空集并声称
    `ok=True`。调用方拿到「这个仓库里一个符号都没定义」，据此判定任务未完成，
    接续会话就会重做已经做完的工作。CI 第一次真正运行时，macOS 的两个
    Python 版本都在这里失败，而 Linux 与 Windows 全绿——分布与上表完全一致。

    去掉 `\\b` 让这一条变宽（`undef build_thing` 之类也会被捞上来），那是**安全的
    方向**：这个函数只负责把候选行捞出来，「是不是定义」由 Python 侧用
    `_symbol_pattern` 复核（见 `_names_in_fragment`）。捞多了会被否掉，捞不到
    就永远没有第二次机会。
    """
    alts = "|".join(re.escape(s) for s in symbols)
    mods = rf"(({DECL_MODIFIERS})[ \t]+)*"
    a = rf"({DEF_KEYWORDS})[[:space:]]+({alts})"
    b = rf"^[ \t]*{mods}({alts})[ \t]*:"
    c = rf"^[ \t]*{mods}({alts})[ \t]*\((\)|[^(\n][^\n]{{0,200}}\))[ \t]*(:[^{{\n]{{0,160}})?[ \t]*\{{[ \t]*$"
    return f"({a})|({b})|({c})"


def _names_in_fragment(fragment: str, rx: re.Pattern[str], wanted: set[str]) -> set[str]:
    """在一段命中文本里读出被定义的符号名。

    后端只负责把候选片段交上来，判定权在这里：用同一条 Python 正则复核，
    读命中的那个名字组。不靠「片段里的第一个/最后一个词」猜——A 形态的名字
    在关键字后面，B / C 形态的名字在最前面，靠位置猜必然在某一种上出错。
    """
    found: set[str] = set()
    for m in rx.finditer(_strip_noise(fragment)):
        got = _pick_name(m)
        if got in wanted:
            found.add(got)
    return found


def _defined_in_text(text: str, symbols: set[str]) -> set[str]:
    """在一段文本里找出哪些符号被定义了。一次正则扫完，不是每符号一次。"""
    if not symbols:
        return set()
    rx = re.compile(_symbol_pattern(symbols), re.M)
    found: set[str] = set()
    for m in rx.finditer(_strip_noise(text)):
        name = _pick_name(m)
        if name in symbols:
            found.add(name)
            if len(found) == len(symbols):
                break
    return found


def _rg_batch(repo: Path, symbols: list[str], exclude: Iterable[str] = ()) -> tuple[set[str], bool]:
    """用一次 ripgrep 调用找出这批符号里哪些被定义了。

    返回 (命中集合, ripgrep 是否可用)。不可用时调用方退回其他策略。

    `-o` 换成整行输出（`--no-line-number` 保留）：B / C 两种形态要靠「符号处在
    行首」判定，`-o` 只给出匹配片段，行首信息就丢了。整行交给 Python 侧复核。
    """
    if shutil.which("rg") is None:
        return set(), False
    found: set[str] = set()
    for i in range(0, len(symbols), SYMBOL_BATCH):
        chunk = symbols[i : i + SYMBOL_BATCH]
        cmd = [
            "rg",
            "--no-messages",
            "--no-filename",
            "--no-line-number",
            "-e",
            _symbol_pattern(chunk),
        ]
        for pat in exclude:
            cmd += ["-g", f"!{pat}"]
        # 显式给出搜索路径。不给路径时 rg 会读 stdin；这里 stdin 是 DEVNULL，
        # 于是它读到 EOF 就扫当前目录——正确但属于巧合，写明 `.` 才是契约。
        cmd.append(".")
        p = run(cmd, repo, timeout=120)
        # 退出码 1 = 没有匹配，那是正常结果不是错误；2 才是真出错。
        if p.code not in (0, 1):
            return found, False
        rx = re.compile(_symbol_pattern(chunk), re.M)
        found |= _names_in_fragment(p.out, rx, set(chunk))
    return found, True


def _git_grep_batch(repo: Path, symbols: list[str], exclude: Iterable[str] = ()) -> tuple[set[str], bool]:
    """ripgrep 缺席时的第一退路：git grep，同样批量化。

    加 `--untracked`：交接的典型时刻是「刚写完、还没 commit」，而 `git grep`
    默认只搜已跟踪文件。不加的话，同一个仓库在装了 ripgrep 的机器上判
    「已定义」、没装的机器上判「缺失」——完成度取决于工具链而不是代码。
    `--exclude-standard` 仍然生效，所以 node_modules 与构建产物不会被卷进来。
    """
    found: set[str] = set()
    for i in range(0, len(symbols), SYMBOL_BATCH):
        chunk = symbols[i : i + SYMBOL_BATCH]
        # `--` 之后是路径规格：用 `:!` 排除计划文档自身。模式必须在它之前。
        args = ["grep", "-hE", "--untracked", "--exclude-standard", _symbol_pattern_ere(chunk)]
        if exclude:
            args.append("--")
            args += [f":!{pat}" for pat in exclude]
        p: Proc = git_proc(repo, *args, timeout=120)
        # 1 = 没有匹配（正常结果）；其余都是真出错，含 128（正则被拒）。
        if p.code not in (0, 1):
            return found, False
        rx = re.compile(_symbol_pattern(chunk), re.M)
        found |= _names_in_fragment(p.out, rx, set(chunk))
    return found, True


def _python_scan(repo: Path, symbols: list[str], exclude: Iterable[str] = ()) -> set[str]:
    """最后的兜底：读源文件，一遍正则扫完。

    比原版的"每符号一次全库遍历"快一个数量级，因为文件只读一遍，
    正则也只编译一次。

    **必须包含未跟踪文件。** 交接的典型时刻正是「刚写完、还没 commit」，
    实测：一个 `git add` 之前的新文件里定义的符号，`git ls-files` 看不见，
    于是同一个仓库在装了 ripgrep 的机器上判「已定义」、没装的机器上判「缺失」——
    完成度取决于工具链而不是代码。`git ls-files` 的输出后面补上
    `--others --exclude-standard`（未跟踪且未被 .gitignore 忽略的文件），
    与 ripgrep 的可见范围对齐。
    """
    wanted = set(symbols)
    skip = {norm_path(x) for x in exclude}
    tracked = git_proc(repo, "ls-files", "-z", timeout=60)
    untracked = git_proc(repo, "ls-files", "-z", "--others", "--exclude-standard", timeout=60)
    rels: list[str] = []
    for p in (tracked, untracked):
        if p.ok and p.out:
            rels += [x for x in p.out.split("\0") if x]
    if rels:
        seen: set[str] = set()
        files = []
        for r in rels:
            if r in seen or Path(r).suffix.lower() not in TEXT_SUFFIXES:
                continue
            seen.add(r)
            files.append(repo / r)
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

    rx = re.compile(_symbol_pattern(wanted), re.M) if wanted else None
    found: set[str] = set()
    if rx is None:
        return found
    for fp in files:
        if len(found) == len(wanted):
            break
        if skip:
            try:
                rel = norm_path(fp.relative_to(repo).as_posix())
            except ValueError:
                rel = norm_path(fp)
            if rel in skip:
                continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found |= _names_in_fragment(text, rx, wanted)
    return found


def resolve_symbols(
    repo: Path, tasks: list[Task], exclude: Iterable[str] = ()
) -> tuple[dict[str, bool], bool]:
    """一次问清全部任务的全部符号是否被定义。

    返回 (符号 -> 是否已定义, 检索是否可信)。第二个值是关键：三条后端全部
    失败时，每个符号都是 False，但那代表「没查成」而不是「查过、确实没有」。
    合并成一个布尔值会让工具把检索故障渲染成「任务未开始」，接续会话据此
    重做已完成的工作，且报告里看不出证据不可信。

    先看每个任务自己声明的文件——那是最可能的定义位置，且读几个小文件比
    扫全库便宜得多。剩下的符号才交给全库检索，且合成一条正则一次搞定。

    `exclude` 是不参与全库检索的路径（计划文档自身）：计划文档里写着
    ``- Produces `undo` `` 这样的代码片段，搜全库会搜到它，于是「计划文档
    宣称要做的事」变成「已经做完的证据」，自己满足自己。
    """
    all_symbols: list[str] = []
    seen: set[str] = set()
    for t in tasks:
        for s in t.symbols:
            if s not in seen:
                seen.add(s)
                all_symbols.append(s)
    if not all_symbols:
        return {}, True

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
    trusted = True
    remaining = [s for s in all_symbols if not result[s]]
    if remaining:
        found, ok = _rg_batch(repo, remaining, exclude)
        if not ok:
            found, ok = _git_grep_batch(repo, remaining, exclude)
        if not ok:
            # 纯 Python 兜底没有「不可用」这一说：它要么读到文件，要么仓库是空的。
            found = _python_scan(repo, remaining, exclude)
        for s in found:
            result[s] = True
    return result, trusted


def score_tasks(repo: Path, tasks: list[Task], plan_rel: str = "") -> dict[int, dict]:
    """每个任务的客观完成证据：文件是否存在、符号是否被定义。

    `plan_rel` 是计划文档相对仓库的路径。它被排除在符号检索之外——否则计划
    文档里的代码片段会成为「已实现」的证据，把自己勾掉。
    """
    if not tasks:
        return {}
    exclude = [plan_rel] if plan_rel else []
    symbol_state, trusted = resolve_symbols(repo, tasks, exclude)

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
        # 检索不可信时不允许判「完成」：勾选是写进计划文档的不可逆动作，
        # 宁可少勾一次让人重看，也不要把没做的步骤勾掉、从待办里永久消失。
        if not trusted and t.symbols:
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
            "symbols_trusted": trusted,
            "steps": len(t.steps),
        }
    return report
