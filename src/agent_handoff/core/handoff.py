#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交接流程编排 —— CLI 与网页界面共用的唯一实现。

抽出来是为了让两个前端不可能行为漂移：原版的逻辑长在 `main()` 里，
任何第二个入口都只能靠解析 stdout，那样迟早对不上。

三步不变：
  1. 提交快照（自动排除计划文档声明为"用户私有"的文件）
  2. 按客观证据回填计划文档的复选框
  3. 生成交接 Markdown + 新会话开场提示词
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..i18n import Translator
from ..platform import agent_for, atomic_write_bytes, norm_path
from .evidence import score_tasks
from .gitops import (
    _strip_leading_dotslash,
    commit_paths,
    detect_concurrency,
    do_commit,
    foreign_commits,
    head_sha,
    is_repo,
    recent_commits,
    repo_meta,
)
from .plan import Task, find_intent_sections, find_plan, parse_plan, update_plan
from .probe import detect_env_pitfalls, detect_test_commands, run_tests
from .report import _fullness_cell, _vitals_id, build_handoff, build_prompt
from .vitals import scan_session_vitals

# 退出码：0 成功，2 参数/环境错误，3 检测到并发写入而停止。与原版一致。
EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_CONCURRENT = 3


@dataclass
class Options:
    """一次交接运行的全部开关。字段与 CLI 参数一一对应。"""

    repo: Path
    plan: str | None = None
    out: str | None = None
    message: str | None = None
    no_commit: bool = False
    skip_tests: bool = False
    test_timeout: int = 900
    no_vitals: bool = False
    force: bool = False
    dry_run: bool = False
    limit: int = 12
    jobs: int = 0
    # 用户勾选要传承的会话（转录文件的绝对路径）。空表示不带任何会话内容——
    # 与原版行为一致。非空时，这些会话的摘要写进交接文档，提示词点名它们。
    sessions: list[str] = field(default_factory=list)


@dataclass
class Result:
    """一次交接运行的结果。GUI 直接把它序列化成 JSON。"""

    code: int = EXIT_OK
    ctx: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    body: str = ""
    out_path: str = ""
    conflicts: list[str] = field(default_factory=list)
    race_warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        c = self.ctx
        return {
            "code": self.code,
            "error": self.error,
            "prompt": self.prompt,
            "out_path": self.out_path,
            "conflicts": self.conflicts,
            "race_warnings": self.race_warnings,
            "repo": c.get("repo", ""),
            "repo_name": c.get("repo_name", ""),
            "branch": c.get("branch", ""),
            "head": c.get("head", ""),
            "head_sha": c.get("head_sha", ""),
            "ahead": c.get("ahead", ""),
            "plan_rel": c.get("plan_rel", ""),
            "handoff_rel": c.get("handoff_rel", ""),
            "now": c.get("now", ""),
            "commit_result": c.get("commit_result", ""),
            "protected": c.get("protected", []),
            "ticked": c.get("ticked", 0),
            "total_steps": c.get("total_steps", 0),
            "report": {str(k): v for k, v in (c.get("report") or {}).items()},
            "done_by_task": {str(k): v for k, v in (c.get("done_by_task") or {}).items()},
            "test_commands": c.get("test_commands", {}),
            "test_results": c.get("test_results", {}),
            "failing": c.get("failing", []),
            "pitfalls": c.get("pitfalls", []),
            "next_tasks": c.get("next_tasks", []),
            "done_tasks": c.get("done_tasks", []),
            "gap_hints": c.get("gap_hints", []),
            "intent_sections": c.get("intent_sections", []),
            "vitals": c.get("vitals", []),
            "sessions": c.get("sessions", []),
            "recent_commits": c.get("recent_commits", ""),
            "dry_run": c.get("dry_run", False),
        }


# 本工具自己产生的提交主题。并发检测靠它区分"我们的提交"与"别人的提交"。
OUR_COMMIT_KEYS = ("cli.commit.msg", "cli.commit.docs_msg")


def _our_commit_prefixes(tr: Translator) -> tuple[str, ...]:
    """本工具的提交主题前缀，三种语言全算。

    切换语言之后，上一次运行留下的提交主题是另一种语言的；如果只认当前语言，
    并发检测会把自己上一轮的提交误报成"别人在写"。
    """
    from ..i18n import available

    out: list[str] = []
    for lang in available():
        t2 = Translator(lang)
        # 提交信息模板带时间戳占位符，取占位符之前的固定前缀。
        msg = t2.t("cli.commit.msg", stamp="")
        out.append(msg.rstrip().rstrip("{stamp}").strip())
        out.append(t2.t("cli.commit.docs_msg"))
    return tuple(dict.fromkeys(x for x in out if x))


def _scan_selected(key: str, originals: list[str]) -> dict[str, Any] | None:
    """扫一个不在体检列表里的转录。

    用户勾选的会话可能落在 `--limit` 之外，也可能是子代理转录（体检默认不列
    它们）。这时按原始路径直接扫，而不是当作「找不到」跳过——勾了却没传下去
    是最坏的结果：用户以为内容已经交接，实际上丢了。
    """
    from .vitals import scan_one

    for raw in originals:
        if norm_path(raw) != key:
            continue
        fp = Path(raw)
        if not fp.is_file():
            return None
        # APP 判定收拢在 `platform.agent_for`：判错会让续接命令给错，
        # 而三处各写一份判断迟早分叉。
        agent = agent_for(fp)
        row = scan_one(agent, fp)
        return row.to_dict() if row is not None else None
    return None


def _prev_path(out_path: Path) -> Path:
    """交接文件的备份路径。只有一处定义，避免两边拼法漂移。

    这个路径有两个用途，都必须用同一个拼法：写备份时的目标，以及从并发信号里
    排除自己产物时的排除项。两边各拼一次就会在改名时漏掉一处——那会让工具把
    自己刚写的备份当成「另一个会话正在写」，然后建议用户加 `--force`。
    """
    return out_path.with_suffix(".prev" + out_path.suffix)


def _keep_previous(
    out_path: Path,
    payload: bytes,
    say: Callable[[str], None],
    tr: Translator,
) -> Path | None:
    """内容真的变了才把旧的那份留一个备份，然后返回备份路径。

    交接文件名只带日期（`2026-08-23-handoff.md`），所以同一天跑第二次会覆盖
    第一次。多数时候这正是想要的——调完再跑，要的就是最新那份，攒一堆
    `-2`、`-3` 只会让人分不清该读哪个。

    但有一种情形不能覆盖：两次运行之间会话内容变了（勾了别的会话、跑了测试、
    提交了新代码），而旧那份里有新那份没有的事实。实测本仓库的
    `docs/2026-08-23-handoff.md` 一天内被覆盖过三次，前两份只能从 git 历史里
    挖——如果当时用了 `--no-commit`，就彻底没了。交接文件的全部意义是**别丢
    上下文**，它自己把上下文丢掉是最讽刺的失败。

    所以判据是「内容是否不同」而不是「文件是否存在」：字节相同就直接覆盖，
    不留噪声；不同才备份。备份名固定为 `<原名>.prev.md`，只保留最近一份——
    留成长链又会变成新的「该读哪个」问题，而真正的历史归 git 管。
    """
    try:
        if not out_path.is_file() or out_path.read_bytes() == payload:
            return None
    except OSError:
        # 读不了旧文件（权限、被占用）就不备份，也不因此中断整个交接：
        # 写不进去下一步自然会报错，这里没必要提前失败。
        return None

    backup = _prev_path(out_path)
    try:
        backup.write_bytes(out_path.read_bytes())
    except OSError as exc:
        # 备份失败只提示，不阻断。用户要的是新交接文件，
        # 为了留旧的而不产出新的是本末倒置。
        say(tr.t("cli.write.backup_failed", path=str(backup), err=str(exc)))
        return None
    say(tr.t("cli.write.kept_previous", path=str(backup)))
    return backup


def run_handoff(
    opts: Options,
    tr: Translator,
    log: Callable[[str], None] | None = None,
) -> Result:
    """跑完整的交接流程。`log` 收到每一步的进度行（CLI 打印，GUI 推流）。

    这个函数不打印、不 sys.exit：全部结果通过 Result 返回，让 CLI 决定退出码、
    让 GUI 决定怎么渲染。
    """
    say = log or (lambda _s: None)
    repo = opts.repo.resolve()

    if not repo.is_dir():
        return Result(code=EXIT_BAD_INPUT, error=tr.t("cli.err.not_dir", path=repo))
    # git 不再是硬门禁。会话传承、计划完成度、测试取证都不依赖 git——
    # 只有「提交快照」和「现场坐标」依赖。原先在这里直接失败，于是一个还没
    # git init 的目录（或根本不需要版本控制的工作目录，例如 Codex Desktop 的
    # 沙箱容器）完全用不了这个工具，哪怕用户要的只是把前序会话的结论带走。
    # 现在降级：没有 git 就跳过提交与 git 现场，其余照做，并在产出里说明。
    has_git = is_repo(repo)

    # 整个流程用同一个时刻。原版在六处各调一次 datetime.now()，于是提交信息、
    # 文件名、交接文件里的"生成时间"和提示词里的过期时间戳可以互相错开几秒，
    # 跨午夜运行时文件名的日期还会和文档标题的日期不一致。
    started = datetime.now()
    stamp_min = f"{started:%Y-%m-%d %H:%M}"
    stamp_sec = f"{started:%Y-%m-%d %H:%M:%S}"
    stamp_day = f"{started:%Y-%m-%d}"

    say(tr.t("cli.step.meta", repo=repo))
    if has_git:
        meta = repo_meta(repo)
    else:
        # 没有 git 时给出同形状的空壳，让下游渲染逻辑不必到处判空。
        # 值用明确的占位符而不是空串：空串会在提示词里渲染成「分支 」这种
        # 断句，读者分不清是「没有」还是「读失败」。
        meta = {
            "branch": "", "head": "", "head_sha": "", "head_full": "",
            "upstream": "", "ahead": "", "remote": "", "unpushed": "",
        }
        say(tr.t("cli.step.no_git"))

    say(tr.t("cli.step.plan"))
    plan_path = find_plan(repo, opts.plan)
    tasks: list[Task] = []
    protected: list[str] = []
    intent_sections: list[str] = []
    plan_rel = ""
    if plan_path:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
        tasks, protected = parse_plan(text)
        intent_sections = find_intent_sections(text)
        try:
            plan_rel = plan_path.relative_to(repo).as_posix()
        except ValueError:
            plan_rel = plan_path.as_posix()
        say(
            tr.t(
                "cli.plan.found",
                path=plan_rel,
                tasks=len(tasks),
                steps=sum(len(t.steps) for t in tasks),
                protected=len(protected),
            )
        )
        if intent_sections:
            say(tr.t("cli.plan.intent", sections=" / ".join(intent_sections)))
        else:
            say(tr.t("cli.plan.no_intent"))
    else:
        say(tr.t("cli.plan.missing"))

    say(tr.t("cli.step.score"))
    # 计划文档要排除在符号检索之外：它自己写着 ``- Produces `undo` ``，
    # 搜全库会搜到它，于是「宣称要做」变成「已经做完」的证据。
    report = score_tasks(repo, tasks, plan_rel)

    # 输出路径要在并发检测之前算出来：它和计划文档都要从"最近被改动"的
    # 信号里排除，否则上一次运行留下的它们会被当成"别人在写"。
    out_dir = plan_path.parent if plan_path else (repo / "docs")
    out_path = Path(opts.out) if opts.out else out_dir / f"{stamp_day}-handoff.md"
    if not out_path.is_absolute():
        out_path = repo / out_path

    say(tr.t("cli.step.commit"))
    head_before = head_sha(repo) if has_git else ""
    # 从并发信号里排除本工具自己的产物与计划文档声明为用户私有的文件：
    #   · 交接文件与计划文档：上一轮运行刚写过，会落在两分钟窗口里
    #   · 交接文件的 `.prev` 备份：同一天内容变了才会出现，同样是我们自己写的
    #   · 受保护文件：本来就永不提交，它被改动跟"另一个会话在写代码"无关，
    #     报出来只会让用户以为有冲突而去加 --force
    self_paths: set[str] = {
        _strip_leading_dotslash(p.replace("\\", "/")) for p in protected
    }
    for p in (out_path, _prev_path(out_path), plan_path):
        if p is None:
            continue
        try:
            self_paths.add(p.resolve().relative_to(repo).as_posix())
        except ValueError:
            continue

    blocking, advisory = detect_concurrency(repo, tr, ignore=self_paths) if has_git else ([], [])
    conflicts = blocking + advisory
    if conflicts:
        say(tr.t("cli.conc.detected"))
        for w in conflicts:
            say(f"        · {w}")
    if blocking and not opts.force:
        if opts.no_commit or opts.dry_run:
            say(tr.t("cli.conc.readonly"))
        else:
            say("")
            say(tr.t("cli.conc.stopped"))
            say(tr.t("cli.conc.reason1"))
            say(tr.t("cli.conc.reason2"))
            say("")
            say(tr.t("cli.conc.howto1"))
            say(tr.t("cli.conc.howto2"))
            say(tr.t("cli.conc.howto3"))
            return Result(code=EXIT_CONCURRENT, conflicts=conflicts)
    elif blocking and opts.force:
        say(tr.t("cli.conc.forced"))

    msg = opts.message or tr.t("cli.commit.msg", stamp=stamp_min)
    if not has_git:
        commit_result = tr.t("cli.commit.no_git")
    elif opts.no_commit:
        commit_result = tr.t("cli.commit.skipped")
    else:
        # 上一轮留下的 `.prev` 备份不该被提交进去。它是同日重跑的救生索，
        # 针对的正是「用了 --no-commit 所以 git 里没有上一份」这个缺口——
        # 让它自己进 git 就本末倒置，而且会给每次重跑加一个噪声文件。
        #
        # 时序上本轮的备份此刻还不存在（写交接文件在第 6 步，提交在第 4 步），
        # 所以要排除的是**上一轮**留下的那个。
        keep_out: list[str] = list(protected)
        try:
            keep_out.append(_prev_path(out_path).resolve().relative_to(repo).as_posix())
        except ValueError:
            pass
        commit_result = do_commit(repo, keep_out, msg, opts.dry_run, tr)
    first_line = commit_result.splitlines()[0] if commit_result else ""
    say(f"      {first_line}")

    total_steps = sum(len(t.steps) for t in tasks)
    ticked = 0
    if tasks and plan_path:
        added, total_steps = update_plan(plan_path, tasks, report, opts.dry_run)
        ticked = sum(1 for t in tasks for s in t.steps if s.done) + added
        say(tr.t("cli.plan.backfilled", added=added, done=ticked, total=total_steps))

    done_by_task: dict[int, int] = {}
    for t in tasks:
        n = sum(1 for s in t.steps if s.done)
        if report.get(t.num, {}).get("complete"):
            n = len(t.steps)
        done_by_task[t.num] = n

    say(tr.t("cli.step.tests"))
    test_commands = detect_test_commands(repo)
    test_results: dict[str, str] = {}
    failing: list[str] = []
    if opts.skip_tests:
        say(tr.t("cli.tests.skipped"))
    elif not test_commands:
        say(tr.t("cli.tests.none"))
    else:
        test_results, failing = run_tests(
            repo,
            test_commands,
            opts.test_timeout,
            tr,
            on_start=lambda n, c: say(tr.t("cli.tests.running", name=n, cmd=c)),
            on_done=lambda n, line: say(tr.t("cli.tests.result", line=line.splitlines()[0])),
        )

    pitfalls = detect_env_pitfalls(repo, tr)
    next_tasks = [n for n in sorted(report) if not report[n]["complete"]]
    done_tasks = [n for n in sorted(report) if report[n]["complete"]]
    gap_hints: list[str] = []
    for n in next_tasks[:2]:
        r = report[n]
        for f in r["files_missing"][:2]:
            gap_hints.append(tr.t("prompt.gap.file", task=n, path=f))
        for s in r["symbols_missing"][:3]:
            gap_hints.append(tr.t("prompt.gap.symbol", task=n, name=s))

    vitals: list[dict[str, Any]] = []
    picked: list[dict[str, Any]] = []
    if not opts.no_vitals:
        rows = scan_session_vitals(limit=opts.limit, jobs=opts.jobs)
        vitals = [r.to_dict() for r in rows]
        if vitals:
            worst = vitals[0]
            say(
                tr.t(
                    "cli.vitals.worst",
                    agent=worst["agent"],
                    # 会话 ID 前 8 位才有区分度。`file[:14]` 会把所有 Codex
                    # 转录截成同一个 `rollout-2026-0`，指不到任何具体文件。
                    file=_vitals_id(worst),
                    mb=f"{worst['mb']:.1f}",
                    # 判据是占用而不是体积，那就得把占用说出来。
                    context=_fullness_cell(worst, tr),
                    fatal=worst["fatal"],
                    band=tr.t(f"band.{worst['band']}"),
                )
            )

    if opts.sessions:
        # 勾选的会话按路径匹配。用 norm_path 比较：用户可能从卡片上抄的是
        # 反斜杠路径，而扫描结果里是 Path 的字符串形态。
        want = {norm_path(s) for s in opts.sessions}
        by_path = {norm_path(v["path"]): v for v in vitals}
        for key in want:
            hit = by_path.get(key)
            if hit is not None:
                picked.append(hit)
            else:
                # 勾选的会话不在本次扫描范围内（--limit 太小，或它是子代理
                # 转录）。单独扫它一次，而不是静默丢掉——静默丢掉会让用户
                # 以为内容传下去了，那正是这个功能要解决的问题。
                extra = _scan_selected(key, opts.sessions)
                if extra is not None:
                    picked.append(extra)
                else:
                    say(tr.t("cli.sessions.not_found", path=key))
        # 按「最后一次真的在动」排，不用文件 mtime：后者与最后一条记录大面积
        # 脱钩（Codex 侧尤其严重，它的 mtime 实质是创建时刻），而这个顺序决定
        # 哪个会话被当成「最近那个」写进文档开头。
        picked.sort(key=lambda v: v.get("active_at") or v["mtime"], reverse=True)
        say(tr.t("cli.sessions.picked", count=len(picked)))

        # 勾了分属不同项目的会话就说出来。
        #
        # 交接固化的是**一个仓库**的状态：提交快照、计划回填、测试取证全都针对
        # 这一个仓库。把别的项目的会话内容汇总进同一份提示词，等于让新会话面对
        # 两个现场——它会拿 A 项目的结论去改 B 项目的代码。
        #
        # 只提示不拦：同一份工作跨两个仓库（前后端分离、主仓 + 插件仓）是真实
        # 场景，用户可能确实要带过去。但默认假设是「勾错了」，所以要说清楚
        # 哪些会话不属于这个仓库。
        #
        # 判定用 `work_repo`（这个会话在改哪个仓库）而不是 `repo`（在哪启动）。
        # 用后者会大面积误报：实测本机会话的启动目录常常不是它们在改的仓库，
        # 于是「跨仓库」警告对几乎每个会话都触发一次，而真正跨仓库的那些反而
        # 淹没在噪声里。`work_repo` 没有证据时自己退回 `repo`，所以这不会让
        # 判定变松，只是让它对准。
        here = norm_path(str(repo))
        outside = [
            v for v in picked
            if (v.get("work_repo") or v.get("repo") or "").strip()
            and norm_path(v.get("work_repo") or v["repo"]) != here
        ]
        if outside:
            say(tr.t("cli.sessions.cross_repo", count=len(outside), repo=repo.name))
            for v in outside[:6]:
                say(tr.t(
                    "cli.sessions.cross_repo_item",
                    agent=v.get("agent", ""),
                    id=(v.get("session_id") or "")[:8] or "?",
                    repo=v.get("work_repo") or v.get("repo", ""),
                ))

    say(tr.t("cli.step.write"))
    try:
        handoff_rel = out_path.resolve().relative_to(repo).as_posix()
    except ValueError:
        handoff_rel = out_path.as_posix()
    try:
        plan_rel = plan_path.relative_to(repo).as_posix() if plan_path else ""
    except ValueError:
        plan_rel = plan_path.as_posix() if plan_path else ""

    ctx: dict[str, Any] = {
        "repo": repo.as_posix(),
        "repo_name": repo.name,
        "branch": meta["branch"],
        "head": meta["head"],
        "head_sha": meta["head_sha"],
        "ahead": meta["ahead"],
        # 仓库身份（可移植）与未推送状态（传不过去的部分）。
        "remote": meta.get("remote", ""),
        "head_full": meta.get("head_full", ""),
        "unpushed": meta.get("unpushed", ""),
        "plan_rel": plan_rel,
        "handoff_rel": handoff_rel,
        "date": stamp_day,
        "now": stamp_sec,
        "commit_result": commit_result,
        "protected": protected,
        "report": report,
        "done_by_task": done_by_task,
        "ticked": ticked,
        "total_steps": total_steps,
        "test_commands": test_commands,
        "test_results": test_results,
        "failing": failing,
        "pitfalls": pitfalls,
        "next_tasks": next_tasks,
        "done_tasks": done_tasks,
        "gap_hints": gap_hints,
        "intent_sections": intent_sections,
        "vitals": vitals,
        "sessions": picked,
        "recent_commits": recent_commits(repo) if has_git else "",
        "has_git": has_git,
        "dry_run": opts.dry_run,
    }
    ctx["prompt"] = build_prompt(ctx, tr)
    body = build_handoff(ctx, tr)

    res = Result(
        code=EXIT_OK,
        ctx=ctx,
        prompt=ctx["prompt"],
        body=body,
        out_path=str(out_path),
        conflicts=conflicts,
    )
    if opts.dry_run:
        return res

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 交接文件在两个平台上的字节必须完全一致：内容自己用 \n，不让 Windows 的
    # 文本模式把它转成 \r\n。
    #
    # 不用 `write_text(..., newline="")`：那个参数是 Python 3.10 才加的，而
    # 本项目声明支持 3.9。在 3.9 上它会抛 TypeError——只有真的在 3.9 上跑过
    # 才会发现，Windows 上装的 3.14 一路都是绿的。直接写字节，绕开文本层。
    payload = body.encode("utf-8")
    _keep_previous(out_path, payload, say, tr)
    # 原子写：写不完整比不写更坏。`write_bytes` 先把文件截断到 0 再写，中间
    # 被打断就留下一份半截的交接现场——而这份文档存在的全部意义就是「上一个
    # 会话没了，现场在这里」。并发检测只警告不阻断，所以两个会话同时跑到这里
    # 是允许发生的情况，不是理论可能。
    atomic_write_bytes(out_path, payload)
    say(f"      {out_path}")

    if has_git and not opts.no_commit:
        paths = [out_path]
        if plan_path:
            paths.append(plan_path)
        commit_paths(repo, paths, tr.t("cli.commit.docs_msg"))

    # 运行期间有人把 HEAD 从我们脚下挪走了吗？我们自己的提交是预期的；
    # 别的提交意味着第二个会话在赛跑，上面提示词里的 HEAD 可能已经不存在。
    if has_git:
        res.race_warnings = foreign_commits(repo, head_before, _our_commit_prefixes(tr))
    if res.race_warnings:
        say("")
        say(tr.t("cli.race.warn"))
        for s in res.race_warnings[:4]:
            say(f"        · {s}")
        say(tr.t("cli.race.explain1"))
        say(tr.t("cli.race.explain2"))
    return res
