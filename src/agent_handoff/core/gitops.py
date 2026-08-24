#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""子进程与 git 操作。

原版的 `git()` 失败时返回空串，于是调用方分不清「命令失败」和「输出为空」——
空仓库里 `git rev-parse HEAD` 会失败，但看起来跟"HEAD 是空字符串"一样。
这里把返回码和输出一起交出去，让调用方自己决定怎么处理。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..i18n import Translator
from ..platform import IS_WINDOWS, normalize_shell_paths

# 会话交接不该被一条卡住的命令拖死。除测试外的所有 git 调用都短超时。
GIT_TIMEOUT = 60
# 判定"另一个会话正在写"的时间窗口。两分钟够覆盖一次工具调用的间隔，
# 又不至于把十分钟前的手工编辑算进来。
RECENT_WINDOW = 120
# detached HEAD 时放在 branch 字段里的标记。不用 git 自己那个字面量 `HEAD`：
# 那会让文档显示「分支：HEAD」，读者以为真有个叫 HEAD 的分支。
# 下游按这个常量识别 detached 状态并给出专门的警告，所以它是接口的一部分，
# 不能随手改字面量。
DETACHED = "<detached>"
# 遍历工作树时跳过的目录。缺一个就会把 node_modules 里几万个文件也 stat 一遍。
WALK_SKIP = {
    ".git", ".hg", ".svn", "node_modules", ".venv", ".venv-win", "venv",
    "dist", "build", "out", "target", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".tox", ".next", ".nuxt", ".turbo",
    ".gradle", ".idea", ".vscode", "coverage", "htmlcov", ".cache",
}


@dataclass
class Proc:
    """一次子进程调用的结果。"""

    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return self.out.strip()


def run(cmd: list[str] | str, cwd: Path, timeout: int = GIT_TIMEOUT) -> Proc:
    """跑一条命令，永不抛异常。

    字符串命令走 shell。Windows 上 shell 是 cmd.exe，它无法执行
    `.venv-win/Scripts/python.exe` 这种带正斜杠的相对路径——正斜杠会让它
    把第一段当成命令名。POSIX 上反过来，反斜杠是转义符。两边都归一化。
    """
    if isinstance(cmd, str):
        cmd = normalize_shell_paths(cmd)
    try:
        p = subprocess.run(  # noqa: S603
            cmd,
            cwd=str(cwd),
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            # 不继承调用方的 stdin：pytest 之类的命令若等输入会永久挂住。
            stdin=subprocess.DEVNULL,
        )
        return Proc(p.returncode, (p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired as exc:
        # 超时的进程可能已经产出了部分输出，那部分往往正是失败原因所在。
        partial = ""
        for chunk in (exc.stdout, exc.stderr):
            if not chunk:
                continue
            partial += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        return Proc(124, partial + f"\n<timeout after {timeout}s>")
    except FileNotFoundError:
        return Proc(127, "<command not found>")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return Proc(1, f"<{type(exc).__name__}: {exc}>")


def git_available() -> bool:
    """PATH 里有没有可执行的 git。所有证据都来自 git，没有它就没得谈。"""
    return shutil.which("git") is not None


def git_proc(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> Proc:
    """跑一条 git 命令，返回码与输出都交出去。"""
    return run(["git", *args], repo, timeout)


def git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    """便捷包装：成功返回去空白的输出，失败返回空串。

    只在"失败与空输出可以等同处理"的地方用它。需要区分二者时（例如空仓库里
    的 `rev-parse HEAD`）用 `git_proc`。
    """
    p = git_proc(repo, *args, timeout=timeout)
    return p.text if p.ok else ""


def is_repo(path: Path) -> bool:
    """是不是 git 工作树。

    只看 `.git` 存在不够：worktree 和 submodule 里 `.git` 是文件而不是目录，
    而一个恰好叫 `.git` 的普通文件会骗过存在性检查。交给 git 自己判断。
    """
    if not path.is_dir():
        return False
    if not git_available():
        # 没有 git 时退回到存在性检查，至少能给出有意义的错误信息。
        return (path / ".git").exists()
    p = git_proc(path, "rev-parse", "--is-inside-work-tree", timeout=15)
    return p.ok and p.text == "true"


def head_sha(repo: Path) -> str:
    """当前 HEAD 的完整 SHA；空仓库（还没有提交）返回空串。

    原版无法区分这两种情况，于是空仓库里的并发检测会把"HEAD 没变"误判成
    "HEAD 变了"，因为两次都拿到空串。这里显式检查 HEAD 是否存在。
    """
    p = git_proc(repo, "rev-parse", "--verify", "HEAD", timeout=15)
    return p.text if p.ok else ""


def repo_meta(repo: Path) -> dict[str, str]:
    """一次拿齐分支、HEAD、领先远程多少个提交。

    原版分四次调 git；这里把能合并的合并，并且用 `for-each-ref` 一次问出
    上游关系，少两次进程启动。Windows 上每次 subprocess 大约 30-60 ms，
    一次交接流程里省下的不是零头。
    """
    # 分支名要用 `symbolic-ref` 问，不能用 `rev-parse --abbrev-ref HEAD`。
    #
    # 后者在 detached HEAD 下返回**字面量** `"HEAD"`——非空、退出码 0，于是
    # `or "<detached>"` 那种写法永远不触发（原版就是这样，那是死代码）。
    # 文档于是显示「分支：HEAD」，接续会话被告知一个不存在的分支名。
    #
    # `symbolic-ref --short -q HEAD` 在 detached 时返回空并且退出码非 0，
    # 这才是 git 用来区分两者的接口。实测：在分支上返回 `master`，
    # detached 时返回空。
    branch = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    detached = not branch
    if detached:
        # 空仓库（还没有任何提交）也没有 symbolic-ref 的解析结果吗？有——
        # 未出生的分支仍然是 symbolic-ref，所以走到这里就是真的 detached。
        branch = DETACHED
    head_line = git(repo, "log", "--oneline", "-1") or "<no commits>"
    sha = head_sha(repo)

    ahead = ""
    upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        ahead = git(repo, "rev-list", "--count", f"{upstream}..HEAD")
    elif sha:
        # 没有配置上游时，退回猜测常见的主干远程引用。
        for ref in ("origin/main", "origin/master", "origin/HEAD"):
            if git_proc(repo, "rev-parse", "--verify", "--quiet", ref, timeout=15).ok:
                ahead = git(repo, "rev-list", "--count", f"{ref}..HEAD")
                break

    # 仓库的**身份**是 remote URL + 完整 sha，不是它在这台机器上的路径。
    # 提示词只写 `E:/output/...` 时，新会话在别的机器、容器、WSL 或 Codespaces
    # 里打开就无从定位；而同一个 remote 下的两个工作副本（a5 与 b8）也只能靠
    # 路径区分，路径一失效就分不清该用哪个状态。
    remote = git(repo, "remote", "get-url", "origin")
    # 未推送的提交对「在别处 clone 之后接续」等于不存在。
    # 用 `--not --remotes` 而不是 `@{u}..HEAD` 的计数：后者依赖远程跟踪引用是否
    # 新鲜，FETCH_HEAD 过期时会偏小——实测项目 A 的 ahead 显示 0，而它的 HEAD
    # 在任何远程上都不存在。
    # 没有任何远程时 `--not --remotes` 排除不掉东西，返回全部提交，那恰好是
    # 正确答案：一个提交都传不出去。但要显式分支，否则空仓库会算出负数。
    if remote or git_proc(repo, "remote", timeout=15).out.strip():
        unpushed = git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes")
    else:
        unpushed = git(repo, "rev-list", "--count", "HEAD")
    unpushed_n = int(unpushed) if unpushed.strip().isdigit() else 0

    return {
        "branch": branch,
        "head": head_line,
        "head_sha": sha[:12] if sha else (head_line.split()[0] if head_line != "<no commits>" else ""),
        "head_full": sha,
        "upstream": upstream,
        "ahead": ahead if ahead and ahead != "0" else "",
        "remote": remote,
        "unpushed": str(unpushed_n) if unpushed_n else "",
    }


def changed_paths(repo: Path) -> set[str]:
    """git 认为有改动的路径（含未跟踪）。用于给并发检测降噪。

    原版把工作树里任何两分钟内被碰过的文件都当成并发信号，于是编译产物、
    缓存、编辑器临时文件都会误报。只有 git 真的看见改动的路径才算证据。
    """
    p = git_proc(repo, "status", "--porcelain", "-z", "--untracked-files=normal")
    if not p.ok:
        return set()
    out: set[str] = set()
    for entry in p.out.split("\0"):
        if len(entry) > 3:
            path = entry[3:]
            # 重命名形如 `R  old -> new`，-z 模式下旧名在下一段，这里取新名足够。
            out.add(path.strip().strip('"'))
    return out


def detect_concurrency(
    repo: Path, tr: Translator, ignore: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """另一个智能体会话是不是正在写这个仓库？返回 (阻断信号, 提示信号)。

    两个会话抢一个工作树是唯一真能丢工作的失败模式：我们的 `git add` 落在
    对方之上，或者对方的 `commit --amend` 重写我们刚做的提交。廉价信号，全部只读。

    信号分两级，因为它们的证明力差一个数量级：

    阻断（只能由另一个进程造成，默认停止）
      - 非空的暂存区，而且不是我们放进去的（对方 add 了还没提交）
      - git 自己的操作锁（提交 / rebase / merge 正在进行）

    提示（同样的现象由用户自己造成的可能性更大，只报告不停止）
      - 两分钟内被改动、且 git 看得见的源文件。**最常见的情形恰恰是：
        会话刚卡死，用户立刻来跑交接，自己刚改的文件当然在两分钟内。**
        原版把这条也当阻断，于是正常用法会被自己挡住，用户只能加 --force——
        而 --force 会连真正的阻断信号一起放过，反而更危险。

    `ignore` 排除本工具自己的产物（交接文件、计划文档）：上一次运行留下的
    它们会落在两分钟窗口内。
    """
    blocking: list[str] = []
    advisory: list[str] = []
    skip = {str(x).replace("\\", "/") for x in (ignore or set())}

    staged = git(repo, "diff", "--cached", "--name-only")
    if staged:
        n = len([ln for ln in staged.splitlines() if ln.strip()])
        blocking.append(tr.t("conc.staged", count=n))

    gitdir = repo / ".git"
    if gitdir.is_file():
        # worktree / submodule：.git 是一行 `gitdir: <路径>` 的文件。
        try:
            pointer = gitdir.read_text(encoding="utf-8", errors="replace").strip()
            if pointer.startswith("gitdir:"):
                target = Path(pointer.split(":", 1)[1].strip())
                gitdir = target if target.is_absolute() else (repo / target)
        except OSError:
            pass
    for lock, key in (
        ("index.lock", "conc.lock.index"),
        ("MERGE_HEAD", "conc.lock.merge"),
        ("rebase-merge", "conc.lock.rebase"),
        ("rebase-apply", "conc.lock.rebase"),
        ("CHERRY_PICK_HEAD", "conc.lock.cherry"),
    ):
        if (gitdir / lock).exists():
            blocking.append(tr.t("conc.lock", lock=lock, what=tr.t(key)))

    # 只有 git 看得见改动的路径才可能是并发证据；其余（构建产物、缓存、
    # 编辑器临时文件）全是噪声，原版会把它们全部误报。
    tracked_changes = changed_paths(repo)
    now = time.time()
    recent: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in WALK_SKIP]
        # `os.walk` 默认不跟随符号链接，所以 root 正常总在 repo 下面。但仓库
        # 本身经由符号链接进入时（macOS 的 /tmp -> /private/tmp 是最常见的一种），
        # root 拿到的是解析后的真实路径，与 repo 不同前缀，relative_to 会抛
        # ValueError。`commit_paths` 里算相对路径时已经为此加过守卫，这里当时漏了：
        # 一个 ValueError 会让整个并发检测中断，而并发检测正是防止两个会话
        # 互相覆盖的那道闸。跳过算不出相对路径的目录，其余照常检查。
        try:
            rel_root = Path(root).relative_to(repo)
        except ValueError:
            continue
        if len(rel_root.parts) > 4:
            dirs[:] = []
            continue
        for fn in files:
            rel = _strip_leading_dotslash((rel_root / fn).as_posix())
            if rel in skip or rel not in tracked_changes:
                continue
            try:
                if now - (Path(root) / fn).stat().st_mtime < RECENT_WINDOW:
                    recent.append(rel)
            except OSError:
                continue
        if len(recent) > 6:
            break
    if recent:
        more = tr.t("conc.recent_more", count=len(recent)) if len(recent) > 4 else ""
        advisory.append(tr.t("conc.recent", files=", ".join(recent[:4]) + more))
    return blocking, advisory


def _strip_leading_dotslash(rel: str) -> str:
    """去掉开头的 `./`，但一个字符都不多去。

    原版写 `.lstrip("./")`。`str.lstrip` 收的是**字符集合**而不是前缀：
    它会把开头所有的 `.` 和 `/` 逐个剥掉，于是点文件的名字被吃掉一截——
    实测 `.env` 变成 `env`、`.gitignore` 变成 `gitignore`、
    `.claude/settings.json` 变成 `claude/settings.json`。

    这不是排版问题。这些路径要拼成 `:(exclude,literal)<rel>` 交给 git，
    名字错一个字符，排除就落空：计划文档明明把 `.env` 声明为用户私有，
    `git add -A` 仍会把它提交进去。凭据正是最常见的点文件内容，
    所以这里必须按前缀剥，且只剥 `./`。
    """
    out = rel
    while out.startswith("./"):
        out = out[2:]
    return out


def _exclude_pathspecs(protected: list[str]) -> list[str]:
    """把受保护路径转成 git 排除 pathspec。

    原版用 `:!path` 简写。它在旧版 git 上不被支持，且当路径含 `*`、`[`、`:`
    这类字符时会被当成通配。长写法 `:(exclude,literal)path` 两个问题都治：
    literal 关掉通配，exclude 是明确的排除意图。
    """
    out = []
    for p in protected:
        rel = _strip_leading_dotslash(str(p).replace("\\", "/").strip())
        if rel:
            out.append(f":(exclude,literal){rel}")
    return out


def do_commit(repo: Path, protected: list[str], message: str, dry: bool, tr: Translator) -> str:
    """把除计划文档声明为用户私有的文件之外的一切暂存，然后提交。"""
    dirty = git(repo, "status", "--porcelain")
    if not dirty:
        return tr.t("cli.commit.clean")

    excludes = _exclude_pathspecs(protected)
    add_cmd = ["git", "add", "-A", "--", "."] + excludes
    if dry:
        return (
            tr.t("cli.commit.dry")
            + "\n      "
            + " ".join(add_cmd)
            + f'\n      git commit -m "{message}"'
        )

    p = run(add_cmd, repo, 120)
    if not p.ok:
        return tr.t("cli.commit.add_failed", detail=p.text[:300])
    if not git(repo, "diff", "--cached", "--name-only"):
        return tr.t("cli.commit.nothing_staged")
    # 不签名、不跑钩子之外的额外交互：交接发生在事故之后，任何等输入的
    # 环节都会让工具挂住。钩子本身保留——用户装它就是要它跑。
    p = run(["git", "commit", "-m", message], repo, 120)
    if not p.ok:
        return tr.t("cli.commit.failed", detail=p.text[:300])
    lines = [ln for ln in p.text.splitlines() if ln.strip()]
    return lines[0] if lines else tr.t("cli.commit.done")


def commit_paths(repo: Path, paths: list[Path], message: str) -> bool:
    """只提交指定的几个文件（交接文件与被回填的计划文档）。"""
    rels = []
    for p in paths:
        try:
            rels.append(p.resolve().relative_to(repo.resolve()).as_posix())
        except ValueError:
            continue  # 输出目录在仓库之外，跳过
    if not rels:
        return False
    if not run(["git", "add", "--", *rels], repo, 60).ok:
        return False
    if not git(repo, "diff", "--cached", "--name-only"):
        return False
    return run(["git", "commit", "-m", message], repo, 60).ok


def foreign_commits(repo: Path, before: str, ours: tuple[str, ...]) -> list[str]:
    """运行期间有别人把 HEAD 推进了吗？

    我们自己的提交是预期的；除此之外的任何提交都意味着第二个会话在跟我们赛跑，
    而上面生成的提示词里那个 HEAD 可能已经不存在了。

    `before` 为空（空仓库）时不做判断：那时"HEAD 变了"是我们自己造成的。
    """
    if not before:
        return []
    now = head_sha(repo)
    if not now or now == before:
        return []
    p = git_proc(repo, "log", "--format=%s", f"{before}..HEAD")
    if not p.ok:
        return []
    return [s for s in p.text.splitlines() if s and not any(s.startswith(o) for o in ours)]


def recent_commits(repo: Path, count: int = 5) -> str:
    return git(repo, "log", "--oneline", f"-{count}")


def dirty_submodules(repo: Path) -> list[str]:
    """有未提交改动的子模块。

    父仓库的提交不会带上子模块内部的改动，接续会话会看到一棵不一致的树——
    这是原版完全没有覆盖的一类静默丢工作。
    """
    p = git_proc(repo, "submodule", "status", "--recursive", timeout=45)
    if not p.ok:
        return []
    out = []
    for line in p.text.splitlines():
        # 前缀 `+` 表示已检出的提交与父仓库记录的不一致，`U` 表示有冲突。
        if line[:1] in ("+", "U"):
            parts = line[1:].split()
            if len(parts) >= 2:
                out.append(parts[1])
    return out


__all__ = [
    "DETACHED",
    "GIT_TIMEOUT",
    "IS_WINDOWS",
    "Proc",
    "RECENT_WINDOW",
    "WALK_SKIP",
    "changed_paths",
    "commit_paths",
    "detect_concurrency",
    "dirty_submodules",
    "do_commit",
    "foreign_commits",
    "git",
    "git_available",
    "git_proc",
    "head_sha",
    "is_repo",
    "recent_commits",
    "repo_meta",
    "run",
]
