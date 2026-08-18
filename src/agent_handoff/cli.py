#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口。原版的全部参数、行为与退出码逐字保留。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core.handoff import EXIT_BAD_INPUT, EXIT_OK, Options, run_handoff
from .core.vitals import SessionRow, find_sessions, scan_session_vitals
from .i18n import Translator, available, detect
from .platform import force_utf8_io


def print_session_card(r: SessionRow, tr: Translator, index: int | None = None) -> None:
    """一个转录，渲染成人能认出"这是哪个会话"的形态。"""
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
    print(tr.t("cli.card.session_id", value=r.session_id or tr.t("cli.card.unknown")))
    if r.thread_id:
        print(tr.t("cli.card.thread_id", value=r.thread_id))
    print(tr.t("cli.card.mtime", value=f"{r.mtime:%Y-%m-%d %H:%M:%S}"))
    if r.cwd:
        print(tr.t("cli.card.cwd", value=r.cwd))
    extra = " ".join(x for x in (r.version, r.origin) if x)
    if extra:
        print(tr.t("cli.card.client", value=extra))
    if r.first_prompt:
        print(tr.t("cli.card.prompt", value=r.first_prompt[:150]))
    if r.repos:
        more = tr.t("cli.card.repos_more", count=len(r.repos) - 1) if len(r.repos) > 1 else ""
        print(tr.t("cli.card.repos", value=r.repos[0] + more))
        # 原版这里复用了上面装客户端字符串的 `extra` 变量做循环变量，
        # 属于遮蔽；改名 `other` 免得以后有人在循环后再读 extra 拿到错值。
        for other in r.repos[1:3]:
            print(f"                {other}")
    print(tr.t("cli.card.file", value=str(r.path)))


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

    if risky:
        print(tr.t("cli.vitals.need") + "\n")
        for i, r in enumerate(risky, 1):
            print_session_card(r, tr, i)
            print()

        by_repo: dict[str, list[SessionRow]] = {}
        for r in risky:
            key = r.repo or r.cwd or tr.t("cli.vitals.no_cwd")
            by_repo.setdefault(key, []).append(r)
        print("─" * 66)
        print(tr.t("cli.vitals.by_repo") + "\n")
        for cwd, group in sorted(by_repo.items(), key=lambda kv: -max(g.mb for g in kv[1])):
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

    healthy = [r for r in rows if r.band not in ("critical", "high")]
    if healthy:
        print("─" * 66)
        print(tr.t("cli.vitals.rest", count=len(healthy)) + "\n")
        for r in healthy[:10]:
            label = tr.t(f"band.{r.band}")
            sid = r.session_id[:8]
            tail = f"  {r.cwd}" if r.cwd else ""
            print(f"  {label}  {r.mb:>5.1f} MB  {r.agent:<12} {sid}{tail}")
    return 0


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
