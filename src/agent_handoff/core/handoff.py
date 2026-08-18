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
from .evidence import score_tasks
from .gitops import (
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
from .report import build_handoff, build_prompt
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
    if not is_repo(repo):
        return Result(code=EXIT_BAD_INPUT, error=tr.t("cli.err.not_repo", path=repo))

    # 整个流程用同一个时刻。原版在六处各调一次 datetime.now()，于是提交信息、
    # 文件名、交接文件里的"生成时间"和提示词里的过期时间戳可以互相错开几秒，
    # 跨午夜运行时文件名的日期还会和文档标题的日期不一致。
    started = datetime.now()
    stamp_min = f"{started:%Y-%m-%d %H:%M}"
    stamp_sec = f"{started:%Y-%m-%d %H:%M:%S}"
    stamp_day = f"{started:%Y-%m-%d}"

    say(tr.t("cli.step.meta", repo=repo))
    meta = repo_meta(repo)

    say(tr.t("cli.step.plan"))
    plan_path = find_plan(repo, opts.plan)
    tasks: list[Task] = []
    protected: list[str] = []
    intent_sections: list[str] = []
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
    report = score_tasks(repo, tasks)

    # 输出路径要在并发检测之前算出来：它和计划文档都要从"最近被改动"的
    # 信号里排除，否则上一次运行留下的它们会被当成"别人在写"。
    out_dir = plan_path.parent if plan_path else (repo / "docs")
    out_path = Path(opts.out) if opts.out else out_dir / f"{stamp_day}-handoff.md"
    if not out_path.is_absolute():
        out_path = repo / out_path

    say(tr.t("cli.step.commit"))
    head_before = head_sha(repo)
    self_paths: set[str] = set()
    for p in (out_path, plan_path):
        if p is None:
            continue
        try:
            self_paths.add(p.resolve().relative_to(repo).as_posix())
        except ValueError:
            continue

    blocking, advisory = detect_concurrency(repo, tr, ignore=self_paths)
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
    if opts.no_commit:
        commit_result = tr.t("cli.commit.skipped")
    else:
        commit_result = do_commit(repo, protected, msg, opts.dry_run, tr)
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
    if not opts.no_vitals:
        rows = scan_session_vitals(limit=opts.limit, jobs=opts.jobs)
        vitals = [r.to_dict() for r in rows]
        if vitals:
            worst = vitals[0]
            say(
                tr.t(
                    "cli.vitals.worst",
                    agent=worst["agent"],
                    file=worst["file"][:14],
                    mb=f"{worst['mb']:.1f}",
                    fatal=worst["fatal"],
                    band=worst["band"],
                )
            )

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
        "recent_commits": recent_commits(repo),
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
    # newline="" 保留字符串里的换行原样；内容自己用 \n，不让 Windows 转成 \r\n，
    # 这样交接文件在两个平台上的字节完全一致。
    out_path.write_text(body, encoding="utf-8", newline="")
    say(f"      {out_path}")

    if not opts.no_commit:
        paths = [out_path]
        if plan_path:
            paths.append(plan_path)
        commit_paths(repo, paths, tr.t("cli.commit.docs_msg"))

    # 运行期间有人把 HEAD 从我们脚下挪走了吗？我们自己的提交是预期的；
    # 别的提交意味着第二个会话在赛跑，上面提示词里的 HEAD 可能已经不存在。
    res.race_warnings = foreign_commits(repo, head_before, _our_commit_prefixes(tr))
    if res.race_warnings:
        say("")
        say(tr.t("cli.race.warn"))
        for s in res.race_warnings[:4]:
            say(f"        · {s}")
        say(tr.t("cli.race.explain1"))
        say(tr.t("cli.race.explain2"))
    return res
