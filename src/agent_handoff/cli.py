#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口。原版的全部参数、行为与退出码逐字保留。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .core.disk import TOP_N, by_repo, scan_disk
from .core.handoff import EXIT_BAD_INPUT, EXIT_OK, Options, run_handoff
from .core.report import _redact_home, _rule
from .core.vitals import (
    SessionRow,
    find_sessions,
    group_by_agent,
    locate_by_id,
    scan_session_vitals,
    sessions_for_repo,
)
from .i18n import Translator, available, detect
from .platform import force_utf8_io, is_foreign_path, norm_path, split_multi


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
    # 占用是判定的主依据，必须和判定挨着显示——否则用户看到「立刻交接」
    # 却只看得到体积，会以为工具在按文件大小瞎猜。
    line = _fullness_line(r, tr)
    if line:
        print(line)
    # 从别的电脑搬来的转录：下面所有路径在本机都无效，而且续接不了。
    # 早说一句，免得用户照着路径去找文件、或者去试一条必然失败的命令。
    if r.is_foreign:
        print(tr.t("cli.card.foreign"))
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
    # 原生续接严格优于交接：交接是有损的（工具授权、后台进程、被否决方案的
    # 推理都传不过去）。只要还能原生续接，就先把那条路摆出来。
    if r.resume_cmd:
        print(tr.t("cli.card.resume", value=r.resume_cmd))
    if r.digest:
        # 摘要存在意味着这个会话有一份模型自己写的交接记录，可以被提取进
        # 新会话的提示词。不显示全文（几千字），只说明它存在、有多长。
        print(tr.t("cli.card.digest", chars=len(r.digest)))


def _fullness_line(r: SessionRow, tr: Translator) -> str:
    """占用行。三种情况的措辞不同，因为可信程度不同。

    有上限就报真占用率；没有上限只报占用量（不能编一个分母出来）；
    压缩过就直接说压缩过——那是最硬的证据，比任何百分比都清楚。
    """
    if r.compactions:
        return tr.t("cli.card.compacted", count=r.compactions, tokens=f"{r.tokens:,}")
    if not r.tokens:
        return ""
    if r.context_window:
        pct = round(r.tokens * 100 / r.context_window)
        return tr.t(
            "cli.card.fullness",
            pct=pct, tokens=f"{r.tokens:,}", window=f"{r.context_window:,}",
        )
    return tr.t("cli.card.tokens", tokens=f"{r.tokens:,}")


def _split_multi(items: list[str] | str | None) -> list[str]:
    """CLI 侧的薄包装。真正的实现在 `platform.split_multi`——网页界面也要用它，
    两处各写一份就会在「顿号算不算分隔符」这种细节上分叉。
    """
    if items is None:
        return []
    raw = [items] if isinstance(items, str) else list(items)
    out: list[str] = []
    for item in raw:
        for part in split_multi(str(item)):
            if part not in out:
                out.append(part)
    return out


def cmd_find(args: argparse.Namespace, tr: Translator) -> int:
    """按 ID 片段 / 目录 / 关键词定位会话，支持一次给多个。

    两条路径合并结果：
      1. `locate_by_id` 按**文件名**在全部转录里精确定位，不受 `--limit` 约束。
         用户握着 ID 时必须能找到——按 limit 扫最新若干个再搜，实测覆盖 26%，
         给了正确 ID 也有七成概率说「没找到」，那是最坏的失败。
      2. `find_sessions` 在已体检的行里按目录 / 话题 / 摘要关键词搜。这条要读
         正文，所以仍受 limit 约束——按关键词找本来就是模糊查询，覆盖最近的
         若干个是合理取舍。
    """
    needles = _split_multi(args.find)
    if not needles:
        print(tr.t("cli.find.hint"))
        return EXIT_BAD_INPUT

    hits: list = []
    seen: set[str] = set()

    def take(rows: list) -> None:
        for r in rows:
            key = norm_path(r.path)
            if key not in seen:
                seen.add(key)
                hits.append(r)

    take(locate_by_id(needles))
    rows = scan_session_vitals(limit=max(args.limit, 40), jobs=args.jobs)
    for needle in needles:
        take(find_sessions(needle, rows))

    shown_needle = ", ".join(needles)
    if not hits:
        print(tr.t("cli.find.none", needle=shown_needle))
        print(tr.t("cli.find.hint"))
        return 1

    if args.json:
        print(json.dumps([r.to_dict() for r in hits], ensure_ascii=False, indent=2))
        return 0

    # 多个 ID 时不截断：用户明确列出了要找哪几个，砍掉一部分等于没回答问题。
    # 单个关键词是模糊查询，截断到 8 个仍然合理。
    limit_shown = len(hits) if len(needles) > 1 else 8
    shown = hits[:limit_shown]
    header = tr.t("cli.find.header", needle=shown_needle, count=len(hits))
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


def cmd_sweep(args: argparse.Namespace, tr: Translator) -> int:
    """磁盘占用报告。**只统计，不删任何文件。**

    删除转录不可逆，而转录里可能存着唯一一份工作记录，所以这里给出可审阅的
    清单和一条可复制的命令，由人决定要不要执行。这与工具其余部分的立场一致：
    只给证据，不做不可逆动作。

    默认只 stat 不读内容——实测 423 个转录 1.09 GB 用 12 毫秒。按仓库聚合需要
    知道每个转录在哪个目录工作过，那要读内容，所以只在 `--by-repo` 时才付
    这份代价（而且复用 vitals 的缓存）。
    """
    report = scan_disk()
    if not report.rows:
        print(tr.t("cli.sweep.none"))
        return 0

    if args.json:
        payload = {
            "total_bytes": report.total_bytes,
            "elapsed_ms": round(report.elapsed_ms, 1),
            "rows": [r.to_dict() for r in report.rows],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    lines: list[str] = []
    add = lines.append
    add(tr.t("cli.sweep.head",
             count=len(report.rows),
             size=_human_bytes(report.total_bytes),
             ms=f"{report.elapsed_ms:.0f}") + "\n")

    # 可回收分类。三类的「安全」程度不同，所以分开列并各配一句理由。
    groups = report.reclaimable()
    if groups:
        add(tr.t("cli.sweep.reclaim") + "\n")
        for kind, rows in groups:
            add(tr.t(f"cli.sweep.kind.{kind}",
                     count=len(rows),
                     size=_human_bytes(sum(r.size for r in rows))))
            add("      " + tr.t(f"cli.sweep.why.{kind}"))
        add("")

    # 占用排行榜。真正吃磁盘的永远是少数几个文件——本机实测「超过 30 天」是
    # 0 个，而单个 90 MB 的文件占了总量的 8%，所以排行榜比按时间过期有用。
    add(tr.t("cli.sweep.biggest", n=len(report.biggest)) + "\n")
    for r in report.biggest:
        tags = []
        if r.is_subagent:
            tags.append(tr.t("cli.sweep.tag.subagent"))
        if r.is_archived:
            tags.append(tr.t("cli.sweep.tag.archived"))
        tag = ("  " + " ".join(tags)) if tags else ""
        add(f"  {_human_bytes(r.size):>10}  {r.agent:12} {r.path.name[:44]}{tag}")
    add("")

    if args.by_repo:
        # 这一步要读内容才能拿到 cwd，所以单独一个开关。复用 vitals 的扫描，
        # 它已经把 cwd 与 repos 解析好了，不必再实现一遍。
        #
        # limit 要足够大：vitals 默认只看每个应用最新 12 个，剩下几百个转录
        # 全会落进「未知」，聚合表就没意义了。按实际文件数放大，让每个转录
        # 都有机会被解析到。
        want = max(args.limit, len(report.rows))
        vit = scan_session_vitals(limit=want, jobs=args.jobs)
        cwd_of = {}
        for v in vit:
            key = norm_path(str(v.path))
            cwd_of[key] = v.repo or v.cwd or ""
        rows_by_repo = by_repo(report, cwd_of)
        add(tr.t("cli.sweep.by_repo") + "\n")
        for name, n, size in rows_by_repo[:TOP_N]:
            shown = tr.t("cli.sweep.unknown_repo") if name == "<unknown>" else name
            add(f"  {_human_bytes(size):>10}  {n:4}  {shown[:58]}")
        add("")

    add(tr.t("cli.sweep.no_delete"))
    text = "\n".join(lines)
    print(text)

    if args.out:
        out = Path(args.out).expanduser()
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            # 写字节而不是 `write_text(..., newline="\n")`：`newline=` 是
            # `pathlib` 3.10 才有的参数，而本项目声明支持 3.9（CI 也跑 3.9）。
            # 用它会让 `--sweep --out` 在 3.9 上直接抛 TypeError。
            # `core/handoff.py` 早就为同一个坑改用了 write_bytes，这里补齐。
            #
            # 顺带把换行钉成 LF：报告要进 git，`.gitattributes` 也声明了
            # `*.md text eol=lf`，让平台默认换行渗进来只会制造无意义的 diff。
            body = _sweep_markdown(report, tr, args)
            out.write_bytes(body.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
        except OSError as exc:
            print(tr.t("cli.sweep.write_failed", path=str(out), err=str(exc)))
            return EXIT_BAD_INPUT
        print("\n" + tr.t("cli.sweep.written", path=str(out)))
    return 0


def _human_bytes(n: int) -> str:
    """给人看的体积。KB 以下不显示小数——「0.03 MB」比「31 KB」难读。"""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _sweep_markdown(report, tr: Translator, args: argparse.Namespace) -> str:
    """导出成 md。存档或对比两次扫描时比终端输出好用。"""
    L: list[str] = []
    a = L.append
    a(tr.t("cli.sweep.md.title", date=f"{datetime.now():%Y-%m-%d %H:%M}") + "\n")
    a(tr.t("cli.sweep.head",
           count=len(report.rows),
           size=_human_bytes(report.total_bytes),
           ms=f"{report.elapsed_ms:.0f}") + "\n")
    a("## " + tr.t("cli.sweep.md.roots") + "\n")
    for agent, root in report.roots:
        a(f"- {agent}: `{_redact_home(str(root))}`")
    a("")

    groups = report.reclaimable()
    if groups:
        a("## " + tr.t("cli.sweep.reclaim") + "\n")
        head = tr.t("cli.sweep.md.thead")
        a(head)
        a(_rule(head))
        for kind, rows in groups:
            a(f"| {tr.t(f'cli.sweep.md.name.{kind}')} | {len(rows)} | "
              f"{_human_bytes(sum(r.size for r in rows))} | {tr.t(f'cli.sweep.why.{kind}')} |")
        a("")

    a("## " + tr.t("cli.sweep.biggest", n=len(report.biggest)) + "\n")
    head2 = tr.t("cli.sweep.md.thead2")
    a(head2)
    a(_rule(head2))
    for r in report.biggest:
        flags = []
        if r.is_subagent:
            flags.append(tr.t("cli.sweep.tag.subagent"))
        if r.is_archived:
            flags.append(tr.t("cli.sweep.tag.archived"))
        a(f"| {_human_bytes(r.size)} | {r.agent} | `{r.path.name}` | "
          f"{r.mtime:%Y-%m-%d} | {' '.join(flags) or '—'} |")
    a("")
    a("> " + tr.t("cli.sweep.no_delete"))
    return "\n".join(L) + "\n"


def cmd_import_bundle(args: argparse.Namespace, tr: Translator) -> int:
    """读一个交接包，把里面的占位符路径解析到本机，报告每一条的结果。

    只读：不复制任何转录到 agent 的数据目录。把别处的转录塞进 `~/.claude` 或
    `~/.codex` 会改变那个 app 的会话列表，是有副作用的动作——必须由用户看过
    清单之后自己决定，工具不代劳。何况 Claude Code 自 v2.1.205 起明确禁止
    篡改会话转录文件。
    """
    from .core.portable import import_bundle

    bundle = Path(args.import_bundle).expanduser()
    if not bundle.is_dir():
        print(tr.t("cli.err.not_dir", path=str(bundle)), file=sys.stderr)
        return EXIT_BAD_INPUT

    rep = import_bundle(bundle)
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK if not rep.problems else EXIT_BAD_INPUT

    if rep.problems and not rep.resolved:
        # 连一条都没解析出来：包本身有问题，直接把原因摊开。
        for p in rep.problems:
            print(f"  {p}", file=sys.stderr)
        return EXIT_BAD_INPUT

    print(tr.t("cli.bundle.read", path=str(bundle), version=rep.schema_version))
    if rep.doc:
        print(tr.t("cli.bundle.doc", path=rep.doc))
    if rep.prompt:
        print(tr.t("cli.bundle.prompt", path=rep.prompt))

    if rep.resolved:
        print(tr.t("cli.bundle.sessions", count=len(rep.resolved)))
    for row in rep.resolved:
        sid = row.get("session_id") or tr.t("cli.card.unknown")
        print(f"  · {row.get('agent', '?')}  {sid}")
        if row.get("bundled_copy"):
            print(tr.t("cli.bundle.copy", path=row["bundled_copy"]))
        local = row.get("local_path")
        if local:
            key = "cli.bundle.local_have" if row.get("exists_locally") else "cli.bundle.local_missing"
            print(tr.t(key, path=local))
        elif row.get("note"):
            print(tr.t("cli.bundle.no_root"))
        if row.get("skipped_reason"):
            print(tr.t("cli.bundle.skipped", why=row["skipped_reason"]))

    for p in rep.problems:
        print(tr.t("cli.bundle.problem", detail=p), file=sys.stderr)
    print()
    print(tr.t("cli.bundle.readonly"))
    return EXIT_OK


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
        # 变量名不叫 by_repo：那是 core.disk 里同名函数，遮蔽掉会给以后在这个
        # 函数里想用它的人埋一个「明明导入了却是个 dict」的坑。
        risky_by_repo: dict[str, list[SessionRow]] = {}
        for r in risky:
            key = r.repo or r.cwd or tr.t("cli.vitals.no_cwd")
            risky_by_repo.setdefault(key, []).append(r)
        print("─" * 66)
        print(tr.t("cli.vitals.by_repo") + "\n")
        # 仓库分组按"最近被碰过"排，与上面的会话顺序一致。
        for cwd, group in sorted(risky_by_repo.items(), key=lambda kv: -max(g.mtime.timestamp() for g in kv[1])):
            ids = ", ".join(g.session_id[:8] for g in group)
            print(f"  {cwd}")
            print(tr.t("cli.vitals.group", count=len(group), ids=ids))
            if cwd.startswith("<"):
                print(tr.t("cli.vitals.no_path"))
            elif (Path(cwd) / ".git").exists():
                print(f'      agent-handoff "{cwd}"')
            elif is_foreign_path(cwd):
                # 「先 git init」对另一台机器上的目录是错的建议——那个目录
                # 本机根本没有。要做的是在本机找到对应仓库，或直接把这个会话
                # 的内容交接进来。
                print(tr.t("cli.vitals.foreign_repo"))
            else:
                print(tr.t("cli.vitals.not_repo"))
            print()
        print(tr.t("cli.vitals.once"))
    else:
        print(tr.t("cli.vitals.no_risk"))
    return 0


def _parse_pick(raw: str, total: int, all_words: tuple[str, ...] = ()) -> list[int]:
    """把用户输入的编号串解析成下标列表（0 起）。

    容错优先：这是给人用的输入框，写 `1,3 4`、`1、3`、`a` 都该work。
    非法编号静默丢弃而不是报错重问——用户已经看着列表在选，越界通常是手误，
    重问一遍比忽略更烦人。返回顺序去重后保持用户输入的顺序。

    `all_words` 是当前语言的「全选」写法，由调用方从 i18n 取。`a` / `all`
    始终接受：提示语里写的就是它们，而且键盘上永远打得出来。
    """
    text = raw.strip().lower()
    if not text:
        return []
    if text in ("a", "all") or text in tuple(w.strip().lower() for w in all_words):
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

    idx = _parse_pick(raw, len(ordered), tuple(tr.t("cli.pick.all_words").split("|")))
    if not idx:
        print(tr.t("cli.pick.none"))
        return []
    chosen = [ordered[i] for i in idx]
    # 分隔符跟随语言：英文用半角分号加空格，中文用全角分号。
    # 写死「；」会让 --lang en 的输出里冒出一个 CJK 标点。
    joiner = "；" if tr.lang.startswith("zh") else "; "
    titles = joiner.join((r.label or r.session_id)[:40] for r in chosen)
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
    ap.add_argument("--sweep", action="store_true", help=tr.t("cli.arg.sweep"))
    ap.add_argument("--by-repo", action="store_true", help=tr.t("cli.arg.by_repo"))
    # 可重复、可逗号分隔：一次找多个会话是常态（要把几段对话一起交接时，
    # 手边就是一串 ID）。单个用法完全不变，所以旧脚本不受影响。
    ap.add_argument("--find", action="append", default=[], metavar="KEYWORD",
                    help=tr.t("cli.arg.find"))
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
    # 跨机器搬运。导出把转录**副本**一起打包，所以换机之后内容还在；
    # 只给路径的交接文档在另一台机器上必然失效——那些路径里编码着源机的 cwd。
    ap.add_argument("--export-bundle", nargs="?", const="", default=None, metavar="DIR",
                    help=tr.t("cli.arg.export_bundle"))
    ap.add_argument("--import-bundle", metavar="DIR", help=tr.t("cli.arg.import_bundle"))
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
    if args.sweep:
        # 磁盘报告不碰仓库、也不需要 git，所以排在 git 检查之前。
        return cmd_sweep(args, tr)
    if args.import_bundle:
        # 导入只读包、解析路径、报告结果；不碰仓库也不需要 git。
        return cmd_import_bundle(args, tr)
    if args.vitals:
        return cmd_vitals(args, tr)

    if not git_available():
        print(tr.t("cli.err.no_git"), file=sys.stderr)
        return EXIT_BAD_INPUT

    # --sessions 支持逗号分隔，也支持重复传参；两种写法合并后去重。
    sessions: list[str] = _split_multi(args.sessions)

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

    # 导出包要在结果确定之后、输出提示词之前做：包里要放刚写出的交接文档。
    # dry-run 时不导出——那一趟本来就不产出文件。
    bundle_info = ""
    if args.export_bundle is not None and not args.dry_run:
        bundle_info = _do_export(args, res, sessions, tr)

    if args.json:
        payload = res.to_dict()
        payload["body"] = res.body
        if bundle_info:
            payload["bundle"] = bundle_info
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


def _do_export(
    args: argparse.Namespace,
    res: Any,
    sessions: list[str],
    tr: Translator,
) -> str:
    """把这次交接打成可拷走的包，返回包目录（失败返回空串）。

    失败只提示不改变退出码：交接本身已经成功，包是附加产物。为了打包失败而
    让整条命令报错，会让用户以为交接也没成——那比没有包糟得多。
    """
    from .core.portable import default_bundle_dir, export_bundle

    stamp_day = datetime.now().strftime("%Y-%m-%d")
    repo = Path(args.repo).expanduser().resolve()
    target = (
        Path(args.export_bundle).expanduser()
        if args.export_bundle
        else default_bundle_dir(repo, stamp_day)
    )
    meta = {
        "name": res.ctx.get("repo_name", ""),
        "branch": res.ctx.get("branch", ""),
        "head": res.ctx.get("head_sha", ""),
        "remote": res.ctx.get("remote", ""),
        # 刻意**不**写仓库的本机绝对路径：那正是换机之后失效的东西，
        # 而 remote + head 才是仓库的身份。
    }
    try:
        mf = export_bundle(
            out_dir=target,
            doc_path=Path(res.out_path) if res.out_path else None,
            prompt=res.prompt,
            sessions=sessions,
            meta=meta,
        )
    except OSError as exc:
        print(tr.t("cli.bundle.export_failed", path=str(target), err=str(exc)),
              file=sys.stderr)
        return ""

    if not args.json:
        carried = sum(1 for s in mf["sessions"] if s.get("stored_name"))
        print()
        print(tr.t("cli.bundle.exported", path=str(target), count=carried))
        skipped = [s for s in mf["sessions"] if s.get("skipped_reason")]
        for s in skipped:
            print(tr.t("cli.bundle.skipped", why=s["skipped_reason"]))
        if carried:
            # 只在真的带了副本时警告。没带副本却警告「里面有敏感内容」会训练
            # 用户忽略这条提示，那比不提示更糟。
            print(tr.t("cli.bundle.verbatim"))
        print(tr.t("cli.bundle.howto", path=str(target)))
    return str(target)


def main_entry() -> None:
    """console_scripts 入口。KeyboardInterrupt 不该打印回溯。"""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main_entry()
