#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染交接 Markdown 与新会话开场提示词。这里的一切都来自实测。"""
from __future__ import annotations

import re
from typing import Any

from ..i18n import Translator


def build_prompt(ctx: dict[str, Any], tr: Translator) -> str:
    """新会话的开场提示词。

    五块内容，缺任何一块都会让接续会话走错路：
      1. 现场坐标（仓库 / 分支 / HEAD）——否则它不知道在哪
      2. 先读计划文档，并点名意图段落——否则它把计划当待办清单，漏掉红线
      3. 已完成任务的名字——否则它重做已完成的工作
      4. 具体缺口（缺哪个文件、哪个符号）——否则它从头找
      5. 过期声明（生成时间 + HEAD）——否则旧提示词被复用，指向已不存在的提交
    """
    L: list[str] = []
    a = L.append
    a(tr.t("prompt.resume", repo_name=ctx["repo_name"], repo=ctx["repo"], branch=ctx["branch"], head=ctx["head_sha"]))
    a("")

    if ctx["plan_rel"]:
        intent = ctx.get("intent_sections") or []
        if intent:
            a(tr.t("prompt.read_plan_intent", plan=ctx["plan_rel"]))
            a(tr.t("prompt.intent_sections", sections=" / ".join(intent[:4])))
            a(tr.t("prompt.intent_sections2"))
        else:
            a(tr.t("prompt.read_plan_plain", plan=ctx["plan_rel"]))
        if ctx["handoff_rel"]:
            a(tr.t("prompt.then_handoff", handoff=ctx["handoff_rel"]))
        a("")
        if ctx["ticked"]:
            line = tr.t("prompt.progress", done=ctx["ticked"], left=ctx["total_steps"] - ctx["ticked"])
            done_tasks = ctx.get("done_tasks") or []
            if done_tasks:
                line += tr.t("prompt.dont_redo", tasks=" / ".join(f"Task {n}" for n in done_tasks))
            a(line)
    elif ctx["handoff_rel"]:
        a(tr.t("prompt.handoff_only", handoff=ctx["handoff_rel"]))
    a("")

    if ctx["next_tasks"]:
        a(tr.t("prompt.priority", tasks=" → ".join(f"Task {n}" for n in ctx["next_tasks"][:4])))
        gaps = ctx.get("gap_hints") or []
        if gaps:
            a(tr.t("prompt.gaps"))
            for g in gaps[:4]:
                a(f"  {g}")
    if ctx["failing"]:
        a("")
        a(tr.t("prompt.fix_failing"))
        for f in ctx["failing"][:6]:
            a(f"  {f}")
    a("")

    if ctx["pitfalls"]:
        a(tr.t("prompt.env"))
        for p in ctx["pitfalls"][:5]:
            a("  - " + re.sub(r"[`*]", "", p))
    for p in ctx.get("protected") or []:
        a(tr.t("prompt.protected", path=p))

    a("")
    a(tr.t("prompt.expiry", now=ctx["now"], head=ctx["head_sha"]))
    return "\n".join(x for x in L if x is not None)


def build_handoff(ctx: dict[str, Any], tr: Translator) -> str:
    """渲染交接 Markdown。"""
    L: list[str] = []
    a = L.append
    a(tr.t("doc.title", repo=ctx["repo_name"], date=ctx["date"]) + "\n")
    a(tr.t("doc.generated"))
    a(tr.t("doc.generated2"))
    if ctx["plan_rel"]:
        a(tr.t("doc.not_substitute", plan=ctx["plan_rel"]))
        a(tr.t("doc.not_substitute2") + "\n")
    else:
        a("\n")

    a(tr.t("doc.h.scene") + "\n")
    a(tr.t("doc.scene.repo", repo=ctx["repo"]))
    branch_line = tr.t("doc.scene.branch", branch=ctx["branch"])
    if ctx["branch"] not in ("main", "master"):
        branch_line += tr.t("doc.scene.not_trunk")
    a(branch_line)
    a(tr.t("doc.scene.head", head=ctx["head"]))
    if ctx["ahead"]:
        a(tr.t("doc.scene.ahead", count=ctx["ahead"]))
    if ctx["plan_rel"]:
        a(tr.t("doc.scene.plan", plan=ctx["plan_rel"]))
    a(tr.t("doc.scene.now", now=ctx["now"]) + "\n")

    a(tr.t("doc.h.step1") + "\n")
    a("```")
    a(ctx["commit_result"])
    a("```")
    if ctx["protected"]:
        a("\n" + tr.t("doc.protected"))
        for p in ctx["protected"]:
            a(f"- `{p}`")
    a("")

    a(tr.t("doc.h.step2") + "\n")
    if ctx["report"]:
        if ctx.get("intent_sections"):
            a(tr.t("doc.step2.note", sections=" / ".join(ctx["intent_sections"][:4])) + "\n")
        a(tr.t("doc.table.head"))
        a("|---|---|---|---|---|")
        for num in sorted(ctx["report"]):
            r = ctx["report"][num]
            done = ctx["done_by_task"].get(num, 0)
            todo = r["steps"] - done
            fe_total = len(r["files_present"]) + len(r["files_missing"])
            se_total = len(r["symbols_ok"]) + len(r["symbols_missing"])
            fe = f"{len(r['files_present'])}/{fe_total}" if fe_total else "—"
            se = f"{len(r['symbols_ok'])}/{se_total}" if se_total else "—"
            if r["complete"]:
                verdict = tr.t("doc.verdict.done")
            elif r["files_present"] or r["symbols_ok"]:
                verdict = tr.t("doc.verdict.partial")
            else:
                verdict = tr.t("doc.verdict.none")
            title = r["title"].split(":", 1)[-1].strip()[:38]
            # 竖线会破坏 Markdown 表格；任务标题里出现过。
            title = title.replace("|", "\\|")
            a(f"| {num} {title} | {done} / {todo} | {fe} | {se} | {verdict} |")
        a("\n" + tr.t("doc.total", ticked=ctx["ticked"], total=ctx["total_steps"]) + "\n")

        gaps = [(n, r) for n, r in sorted(ctx["report"].items()) if r["files_missing"] or r["symbols_missing"]]
        if gaps:
            a(tr.t("doc.h.gaps") + "\n")
            for num, r in gaps:
                a(f"**Task {num}** — {r['title'].split(':', 1)[-1].strip()}")
                for f in r["files_missing"]:
                    a(tr.t("doc.gap.file", path=f))
                for s in r["symbols_missing"]:
                    a(tr.t("doc.gap.symbol", name=s))
                a("")
    else:
        a(tr.t("doc.plan_missing") + "\n")

    a(tr.t("doc.h.step3") + "\n")
    if ctx["test_results"]:
        for name, line in ctx["test_results"].items():
            a(f"**{name}** — `{ctx['test_commands'][name]}`")
            a("```")
            a(line)
            a("```")
    else:
        a(tr.t("doc.step3.skipped"))
    a("")

    if ctx["pitfalls"]:
        a(tr.t("doc.h.env") + "\n")
        for n in ctx["pitfalls"]:
            a(f"- {n}")
        a("")

    if ctx["recent_commits"]:
        a(tr.t("doc.h.commits") + "\n```")
        a(ctx["recent_commits"])
        a("```\n")

    if ctx["vitals"]:
        a(tr.t("doc.h.vitals") + "\n")
        a(tr.t("doc.vitals.basis") + "\n")
        a(tr.t("doc.vitals.head"))
        a("|---|---|---|---|---|---|")
        for r in ctx["vitals"][:8]:
            label = tr.t(f"band.{r['band']}")
            if r["band"] == "critical":
                label = f"**{label}**"
            a(f"| {r['agent']} | `{r['file'][:14]}` | {r['mb']:.1f} MB | {r['fatal']} | {r['errors']} | {label} |")
        worst = ctx["vitals"][0]
        if worst["band"] in ("critical", "high"):
            a(
                "\n"
                + tr.t(
                    "doc.vitals.worst",
                    agent=worst["agent"],
                    file=worst["file"][:14],
                    mb=f"{worst['mb']:.1f}",
                    advice=tr.t(f"band.advice.{worst['band']}"),
                )
            )
        a("")

    a(tr.t("doc.h.prompt") + "\n")
    a(tr.t("doc.prompt.howto"))
    a(tr.t("doc.prompt.howto2") + "\n")
    a("```text")
    a(ctx["prompt"])
    a("```")
    return "\n".join(L) + "\n"
