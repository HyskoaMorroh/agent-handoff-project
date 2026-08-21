#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口。原版的全部参数、行为与退出码逐字保留。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import __version__
from .core.handoff import EXIT_BAD_INPUT, EXIT_OK, Options, run_handoff
from .core.vitals import (
    SessionRow,
    find_sessions,
    group_by_agent,
    scan_session_vitals,
    sessions_for_repo,
)
from .i18n import Translator, available, detect
from .platform import force_utf8_io


def print_session_card(r: SessionRow, tr: Translator, index: int | None = None) -> None:
    """一个转录，渲染成人能认出"这是哪个会话"的形态。

    卡片上必须有「话题」：会话 ID 前八位对人没有意义，而开场提问在斜杠命令
    回显时对所有会话都一样。话题来自 AI 标题或压缩摘要，是会话内容的提炼。
    """
    label = tr.t(f"band.{r.band}")
    num = f"[{index}] " if index is not None else ""
    print(
        tr.t(
            "cli.card.summary",
            index=num,
            agent=r.agent,
            label=label,
            mb=f"{r.mb:.1f}",
            fatal=r.fatal,
            errors=r.errors,
        )
    )
    if r.label:
        print(tr.t("cli.card.topic", value=r.label[:110]))
    # 被打断的轮次要显式说出来：半成品看起来和完成品一样，而按「已完成」
    # 继续做下去，代价是把没做完的工作当成做完的。
    if r.aborted:
        print(tr.t("cli.card.aborted", count=r.aborted))
    print(tr.t("cli.card.session_id", value=r.session_id or tr.t("cli.card.unknown")))
    if r.thread_id:
        print(tr.t("cli.card.thread_id", value=r.thread_id))
    print(tr.t("cli.card.mtime", value=f"{r.mtime:%Y-%m-%d %H:%M:%S}"))
    if r.cwd:
        print(tr.t("cli.card.cwd", value=r.cwd))
    extra = " ".join(x for x in (r.version, r.origin) if x)
    if extra:
        print(tr.t("cli.card.client", value=extra))
    if r.last_prompt:
        print(tr.t("cli.card.last_prompt", value=r.last_prompt[:110]))
    elif r.first_prompt:
        print(tr.t("cli.card.prompt", value=r.first_prompt[:150]))
    if r.repos:
        more = tr.t("cli.card.repos_more", count=len(r.repos) - 1) if len(r.repos) > 1 else ""
        print(tr.t("cli.card.repos", value=r.repos[0] + more))
        # 原版这里复用了上面装客户端字符串的 `extra` 变量做循环变量，
        # 属于遮蔽；改名 `other` 免得以后有人在循环后再读 extra 拿到错值。
        for other in r.repos[1:3]:
            print(f"                {other}")
    print(tr.t("cli.card.file", value=str(r.path)))
    if r.digest:
        # 摘要存在意味着这个会话有一份模型自己写的交接记录，可以被提取进
        # 新会话的提示词。不显示全文（几千字），只说明它存在、有多长。
        print(tr.t("cli.card.digest", chars=len(r.digest)))


def cmd_find(args: argparse.Namespace, tr: Translator) -> int:
    rows = scan_session_vitals(limit=max(args.limit, 40), jobs=args.jobs)
    hits = find_sessions(args.find, rows)
    if not hits:
        print(tr.t("cli.find.none", needle=args.find))
        print(tr.t("cli.find.hint"))
        return 1

    if args.json:
        print(json.dumps([r.to_dict() for r in hits], ensure_ascii=False, indent=2))
        return 0

    shown = hits[:8]
    header = tr.t("cli.find.header", needle=args.find, count=len(hits))
    if len(hits) > len(shown):
        header += tr.t("cli.find.truncated", shown=len(shown))
    print(header + "：\n" if tr.lang.startswith("zh") else header + ":\n")
    for i, r in enumerate(shown, 1):
        print_session_card(r, tr, i)
        print()

    repos = {r.repo for r in hits if r.repo}
    if len(repos) == 1:
        only = repos.pop()
        print(tr.t("cli.find.same_repo", repo=only))
        print(tr.t("cli.find.solidify", repo=only))
    elif repos:
        print(tr.t("cli.find.multi_repo"))
        for p in sorted(repos):
            print(f'  agent-handoff "{p}"')
    else:
        print(tr.t("cli.find.no_repo"))
    return 0


def cmd_vitals(args: argparse.Namespace, tr: Translator) -> int:
    rows = scan_session_vitals(limit=args.limit, jobs=args.jobs)
    if not rows:
        if args.json:
            print(json.dumps([], ensure_ascii=False))
        else:
            print(tr.t("cli.vitals.none"))
        return 0

    if args.json:
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=2))
        return 0

    risky = [r for r in rows if r.band in ("critical", "high")]
    print(tr.t("cli.vitals.scanned", total=len(rows), risky=len(risky)) + "\n")

    # 按 APP 分组、组内最近活动在前。人认会话是先认 APP、再认时间；
    # 混在一起时"上一个会话"只能靠一行行看客户端字段找。
    for agent, group in group_by_agent(rows):
        risky_n = sum(1 for r in group if r.band in ("critical", "high"))
        tail = f"   {tr.t('gui.vitals.risky')} {risky_n}" if risky_n else ""
        print(f"  {agent}   ({len(group)}){tail}")
        print(f"  {'─' * 64}\n")
        for i, r in enumerate(group, 1):
            print_session_card(r, tr, i)
            print()

    if risky:
        by_repo: dict[str, list[SessionRow]] = {}
        for r in risky:
            key = r.repo or r.cwd or tr.t("cli.vitals.no_cwd")
            by_repo.setdefault(key, []).append(r)
        print("─" * 66)
        print(tr.t("cli.vitals.by_repo") + "\n")
        # 仓库分组按"最近被碰过"排，与上面的会话顺序一致。
        for cwd, group in sorted(by_repo.items(), key=lambda kv: -max(g.mtime.timestamp() for g in kv[1])):
            ids = ", ".join(g.session_id[:8] for g in group)
            print(f"  {cwd}")
            print(tr.t("cli.vitals.group", count=len(group), ids=ids))
            if cwd.startswith("<"):
                print(tr.t("cli.vitals.no_path"))
            elif (Path(cwd) / ".git").exists():
                print(f'      agent-handoff "{cwd}"')
            else:
                print(tr.t("cli.vitals.not_repo"))
            print()
        print(tr.t("cli.vitals.once"))
    else:
        print(tr.t("cli.vitals.no_risk"))
    return 0


def _parse_pick(raw: str, total: int) -> list[int]:
    """把用户输入的编号串解析成下标列表（0 起）。

    容错优先：这是给人用的输入框，写 `1,3 4`、`1、3`、`a` 都该work。
    非法编号静默丢弃而不是报错重问——用户已经看着列表在选，越界通常是手误，
    重问一遍比忽略更烦人。返回顺序去重后保持用户输入的顺序。
    """
    text = raw.strip().lower()
    if not text:
        return []
    if text in ("a", "all", "全部", "全选"):
        return list(range(total))
    out: list[int] = []
    for token in re.split(r"[\s,，、;；]+", text):
        if not token.isdigit():
            continue
        n = int(token)
        if 1 <= n <= total and (n - 1) not in out:
            out.append(n - 1)
    return out


def pick_sessions(rows: list[SessionRow], repo: Path, tr: Translator) -> list[str]:
    """让用户从列表里勾选要传承的会话。返回选中转录的路径。

    为什么需要这一步：工具没法可靠判断「哪些会话属于这个仓库」——Codex 的 cwd
    是任务沙箱，从正文捞出来的路径也可能是顺带提到的别的项目。人看一眼话题
    就知道，所以把最终决定权交给人，但把认会话所需的信息（话题、时间、仓库、
    转录路径）都摆在卡片上，而不是只给一串 ID 让人猜。
    """
    if not rows:
        print(tr.t("cli.pick.empty_list"))
        return []

    def related(r: SessionRow) -> bool:
        # 与 `sessions_for_repo` 同一套判据，直接复用而不是再写一遍：
        # 两处各写一遍必然漂移，而它们回答的是同一个问题
        #（这个转录在这个仓库上工作过吗）。
        return bool(sessions_for_repo(repo, [r]))

    # 与这个仓库相关的排在前面：它们几乎总是用户要选的，放在需要翻屏的位置
    # 等于没有这个功能。组内按最近活动排。
    ordered = sorted(rows, key=lambda r: (not related(r), -r.mtime.timestamp()))

    print()
    print(tr.t("cli.pick.head"))
    print(tr.t("cli.pick.related"))
    print(tr.t("cli.pick.hint"))
    print()
    for i, r in enumerate(ordered, 1):
        mark = "★ " if related(r) else "  "
        print(f"  {mark}", end="")
        print_session_card(r, tr, i)
        print()

    try:
        raw = input(tr.t("cli.pick.prompt"))
    except (EOFError, KeyboardInterrupt):
        # 管道里跑（无 tty）或用户按了 Ctrl-C：当作"不选"，不要中断整个交接。
        print()
        print(tr.t("cli.pick.none"))
        return []

    idx = _parse_pick(raw, len(ordered))
    if not idx:
        print(tr.t("cli.pick.none"))
        return []
    chosen = [ordered[i] for i in idx]
    titles = "；".join((r.label or r.session_id)[:40] for r in chosen)
    print(tr.t("cli.pick.chosen", count=len(chosen), titles=titles))
    return [str(r.path) for r in chosen]


def build_parser(tr: Translator) -> argparse.ArgumentParser:
    """构造参数解析器。参数名与语义与原版完全一致，只新增了几个可选开关。"""
    ap = argparse.ArgumentParser(prog="agent-handoff", description=tr.t("cli.desc"))
    ap.add_argument("repo", nargs="?", default=".", help=tr.t("cli.arg.repo"))
    ap.add_argument("--plan", help=tr.t("cli.arg.plan"))
    ap.add_argument("--out", help=tr.t("cli.arg.out"))
    ap.add_argument("-m", "--message", help=tr.t("cli.arg.message"))
    ap.add_argument("--no-commit", action="store_true", help=tr.t("cli.arg.no_commit"))
    ap.add_argument("--skip-tests", action="store_true", help=tr.t("cli.arg.skip_tests"))
    ap.add_argument("--test-timeout", type=int, default=900, help=tr.t("cli.arg.test_timeout"))
    ap.add_argument("--vitals", action="store_true", help=tr.t("cli.arg.vitals"))
    ap.add_argument("--no-vitals", action="store_true", help=tr.t("cli.arg.no_vitals"))
    ap.add_argument("--find", metavar="KEYWORD", help=tr.t("cli.arg.find"))
    ap.add_argument("--limit", type=int, default=12, help=tr.t("cli.arg.limit"))
    ap.add_argument("--force", action="store_true", help=tr.t("cli.arg.force"))
    ap.add_argument("--dry-run", action="store_true", help=tr.t("cli.arg.dry_run"))
    # 新增开关。默认值都保持原版行为，加了才改变什么。
    ap.add_argument("--lang", choices=list(available()), help=tr.t("cli.arg.lang"))
    ap.add_argument("--gui", action="store_true", help=tr.t("cli.arg.gui"))
    ap.add_argument("--port", type=int, default=0, help=tr.t("cli.arg.port"))
    ap.add_argument("--no-browser", action="store_true", help=tr.t("cli.arg.no_browser"))
    ap.add_argument("--jobs", type=int, default=0, help=tr.t("cli.arg.jobs"))
    ap.add_argument("--json", action="store_true", help=tr.t("cli.arg.json"))
    # 会话传承。--sessions 给脚本用（可重复、可逗号分隔），--pick-sessions 给人用。
    ap.add_argument("--sessions", action="append", default=[], metavar="PATH",
                    help=tr.t("cli.arg.sessions"))
    ap.add_argument("--pick-sessions", action="store_true", help=tr.t("cli.arg.pick_sessions"))
    ap.add_argument("--version", action="version", version=f"agent-handoff {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    force_utf8_io()

    # 两遍解析：第一遍只为拿 --lang，好让帮助文本本身就是目标语言。
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--lang", choices=list(available()))
    known, _ = pre.parse_known_args(argv if argv is not None else sys.argv[1:])
    tr = Translator(known.lang or detect())

    args = build_parser(tr).parse_args(argv)

    if args.gui:
        from .gui.server import serve

        return serve(
            lang=tr.lang,
            port=args.port,
            open_browser=not args.no_browser,
            default_repo=args.repo if args.repo != "." else "",
        )

    from .core.gitops import git_available

    if args.find:
        return cmd_find(args, tr)
    if args.vitals:
        return cmd_vitals(args, tr)

    if not git_available():
        print(tr.t("cli.err.no_git"), file=sys.stderr)
        return EXIT_BAD_INPUT

    # --sessions 支持逗号分隔，也支持重复传参；两种写法合并后去重。
    sessions: list[str] = []
    for item in args.sessions:
        for part in re.split(r"[,，;；]+", item):
            part = part.strip().strip('"').strip("'")
            if part and part not in sessions:
                sessions.append(part)

    if args.pick_sessions:
        # 勾选要在交接开始前完成：它决定交接文件里写什么，而交接文件一旦写出
        # 就可能被提交。跑完再问等于要重跑一遍。
        rows = scan_session_vitals(limit=max(args.limit, 20), jobs=args.jobs)
        for path in pick_sessions(rows, Path(args.repo).resolve(), tr):
            if path not in sessions:
                sessions.append(path)

    opts = Options(
        repo=Path(args.repo),
        plan=args.plan,
        out=args.out,
        message=args.message,
        no_commit=args.no_commit,
        skip_tests=args.skip_tests,
        test_timeout=args.test_timeout,
        no_vitals=args.no_vitals,
        force=args.force,
        dry_run=args.dry_run,
        limit=args.limit,
        jobs=args.jobs,
        sessions=sessions,
    )
    quiet = args.json
    res = run_handoff(opts, tr, log=None if quiet else print)

    if res.error:
        print(res.error, file=sys.stderr)
        return res.code
    if res.code != EXIT_OK:
        return res.code

    if args.json:
        payload = res.to_dict()
        payload["body"] = res.body
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    if args.dry_run:
        print("\n" + tr.t("cli.dry.write", path=res.out_path, bytes=len(res.body)) + "\n")
        print("\n".join(res.body.splitlines()[:40]))
        return EXIT_OK

    print("\n" + "=" * 68)
    print(tr.t("cli.prompt.banner"))
    print("=" * 68)
    print(res.prompt)
    print("=" * 68)
    return EXIT_OK


def main_entry() -> None:
    """console_scripts 入口。KeyboardInterrupt 不该打印回溯。"""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main_entry()
