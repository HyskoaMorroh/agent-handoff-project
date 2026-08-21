#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染交接 Markdown 与新会话开场提示词。这里的一切都来自实测。"""
from __future__ import annotations

import os
import re
from typing import Any

from ..i18n import Translator


# 家目录在交接**文档**里要脱敏。文档会被 git 提交、可能推送到公开仓库，
# 而转录的绝对路径里带着操作系统用户名（`C:\Users\alice\.codex\…`）。
# 那不是密钥，但也没有理由公开：接续会话读转录靠的是提示词，
# 提示词是本机粘贴的、保留真实路径，两者的受众不同。
#
# 每次调用都重新读家目录，不在导入时算好：测试要能通过 monkeypatch 换 HOME，
# 而导入时求值会把第一次的值钉死，脱敏在换过家目录的环境里静默失效——
# 那正是「以为脱敏了其实没脱」的最坏情形。
def _home_variants() -> list[str]:
    """家目录的各种书写形态，长的在前，避免短的先替换把长的切碎。

    包含 Claude Code 的 slug 形态：它把 cwd 里的非字母数字换成 `-` 作项目
    目录名，于是 `C:\\Users\\alice` 变成 `C--Users-alice`——只换路径前缀的话，
    用户名仍留在那一段里。
    """
    home = os.path.expanduser("~")
    if not home:
        return []
    forms = {home, home.replace("\\", "/"), home.replace("/", "\\")}
    slug = re.sub(r"[^A-Za-z0-9]", "-", home)
    out = [f for f in forms if f]
    if len(slug) > 3:
        out.append(slug)
    # 长的先替换：slug 通常最长，且包含盘符与分隔符的编码形态。
    return sorted(out, key=len, reverse=True)


def _redact_home(text: str) -> str:
    """把家目录换成 `~`（slug 形态换成 `_HOME_`），让文档不带操作系统用户名。

    只替换目录前缀，不改路径其余部分——文件名本身（会话 ID）是定位转录所
    必需的，脱掉它文档就失去了可操作性。
    """
    if not text:
        return text
    out = text
    for form in _home_variants():
        # slug 是目录名的一段，写成 `~` 会读成「这里嵌了个家目录」，
        # 而它其实只是被编码过的项目标识。
        repl = "_HOME_" if re.fullmatch(r"[A-Za-z0-9-]+", form) else "~"
        out = re.sub(re.escape(form), repl, out, flags=re.I)
    return out


def _vitals_id(r: dict[str, Any]) -> str:
    """体征表里用什么标识一个转录。

    原版用 `file[:14]`。Codex 的文件名都以 `rollout-2026-0…` 开头，实测 12 行
    全部截成同一个字符串，读者无法把任何一行对应到具体文件；
    `doc.vitals.worst` 也用同一截断，「最紧迫的是 rollout-2026-0」指不到任何东西。
    会话 ID 的前 8 位才是有区分度的，退回文件名尾部。
    """
    sid = (r.get("session_id") or "").strip()
    if sid:
        return sid[:8]
    name = r.get("file") or ""
    return name[-18:] if len(name) > 18 else name


def build_prompt(ctx: dict[str, Any], tr: Translator) -> str:
    """新会话的开场提示词。

    六块内容，缺任何一块都会让接续会话走错路：
      1. 现场坐标（仓库 / 分支 / HEAD）——否则它不知道在哪
      2. 先读计划文档，并点名意图段落——否则它把计划当待办清单，漏掉红线
      3. 已完成任务的名字——否则它重做已完成的工作
      4. 具体缺口（缺哪个文件、哪个符号）——否则它从头找
      5. 前序会话：话题、结论、转录路径——否则前面几十轮的判断全部丢失，
         新会话从零重新推导，还可能推出相反的结论
      6. 过期声明（生成时间 + HEAD）——否则旧提示词被复用，指向已不存在的提交

    第 5 块为什么只放摘要与路径、不放全文：两份多 MB 的转录合计上百万字符，
    任何提示词都装不下。完整摘要写进交接文档（新会话被要求读它），提示词里
    放的是「有哪些会话、各自结论是什么、原始转录在哪」——够它自己去取。
    """
    L: list[str] = []
    a = L.append
    a(tr.t("prompt.resume", repo_name=ctx["repo_name"], repo=ctx["repo"], branch=ctx["branch"], head=ctx["head_sha"]))
    # 仓库身份与「这台机器上的位置」是两件事。远程 URL + 完整 sha 在任何机器上
    # 都能定位到同一个状态；路径不能。未推送的提交则根本传不过去——新会话在别处
    # clone 只会拿到远程有的东西，不说清楚它会以为自己看到的是全部。
    if ctx.get("remote"):
        a(tr.t("prompt.identity", remote=ctx["remote"], sha=ctx.get("head_full") or ctx["head_sha"]))
    else:
        a(tr.t("prompt.no_remote"))
    if ctx.get("unpushed"):
        a(tr.t("prompt.unpushed", count=ctx["unpushed"]))
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
                # 两句之间要有分隔。三种语言的句末标点不同（。vs .），
                # 统一在拼接处补一个空格，而不是把空格写进任一句的模板里。
                line = line.rstrip() + " " + tr.t(
                    "prompt.dont_redo", tasks=" / ".join(f"Task {n}" for n in done_tasks)
                )
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

    sessions = ctx.get("sessions") or []
    if sessions:
        a(tr.t("prompt.sessions.head", count=len(sessions)))
        for s in sessions:
            a(tr.t(
                "prompt.sessions.item",
                agent=s.get("agent", ""),
                topic=(s.get("label") or "").strip()[:120] or "-",
                mtime=s.get("mtime_text", ""),
            ))
            a(tr.t("prompt.sessions.path", path=s.get("path", "")))
            if s.get("session_id"):
                a(tr.t("prompt.sessions.id", value=s["session_id"]))
        a(tr.t("prompt.sessions.howto", handoff=ctx["handoff_rel"]))
        # 交接是有损的。不说明这一点，新会话会把「摘要里没有」当成「没发生过」，
        # 于是把上一个会话已经排除的方案重新试一遍。
        a(tr.t("prompt.sessions.lossy"))
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


def _fence(text: str) -> tuple[str, str]:
    """给一段可能自带代码围栏的文本挑一条更长的围栏。

    摘要是模型写的 Markdown，里面几乎一定有 ``` 代码块（贴命令、贴测试输出）。
    用三个反引号包它会被内层的第一个 ``` 提前闭合，后面的内容就漏进文档结构里
    ——实测摘要里的 `### 测试修复` 因此变成了交接文档自己的三级标题。
    CommonMark 允许围栏用更多的反引号，只要比内层最长的连续反引号更长。
    """
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return ticks + "text", ticks


def _block(add, text: str) -> None:
    """把一段外来文本作为代码块写进文档，不让它破坏文档结构。"""
    open_fence, close_fence = _fence(text)
    add(open_fence)
    add(text)
    add(close_fence)


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
            a(f"| {r['agent']} | `{_vitals_id(r)}` | {r['mb']:.1f} MB | {r['fatal']} | {r.get('aborted', 0)} | {r['errors']} | {label} |")
        worst = ctx["vitals"][0]
        if worst["band"] in ("critical", "high"):
            a(
                "\n"
                + tr.t(
                    "doc.vitals.worst",
                    agent=worst["agent"],
                    file=_vitals_id(worst),
                    mb=f"{worst['mb']:.1f}",
                    advice=tr.t(f"band.advice.{worst['band']}"),
                )
            )
        a("")

    sessions = ctx.get("sessions") or []
    if sessions:
        a(tr.t("doc.h.sessions") + "\n")
        a(tr.t("doc.sessions.intro") + "\n")
        for s in sessions:
            # 标题来自 label，label 可能取自摘要的第一句实质内容——而摘要里
            # 照抄了大量绝对路径。标题同样要脱敏，否则用户名从这里漏出去。
            title = _redact_home((s.get("label") or "").strip()) or s.get("session_id", "")
            a(f"### {s.get('agent', '')} — {title}\n")
            a(tr.t("doc.sessions.id", value=s.get("session_id", "") or "-"))
            if s.get("thread_id"):
                a(tr.t("doc.sessions.thread", value=s["thread_id"]))
            a(tr.t("doc.sessions.mtime", value=s.get("mtime_text", "")))
            if s.get("cwd"):
                a(tr.t("doc.sessions.cwd", value=_redact_home(s["cwd"])))
            for rp in (s.get("repos") or [])[:3]:
                a(tr.t("doc.sessions.repo", value=_redact_home(rp)))
            a(tr.t("doc.sessions.file", value=_redact_home(s.get("path", ""))))
            if s.get("digest_windows", 0) > 1:
                a(tr.t("doc.sessions.windows", count=s["digest_windows"]))
            asks = s.get("asks") or []
            if asks:
                a("")
                a(tr.t("doc.sessions.asks"))
                for one in asks:
                    _block(a, _redact_home(one))
            if s.get("last_prompt"):
                a("")
                a(tr.t("doc.sessions.last_prompt"))
                _block(a, _redact_home(s["last_prompt"]))
            if s.get("digest"):
                a("")
                a(tr.t("doc.sessions.digest"))
                # 摘要是 Markdown（带 # 标题与代码块），整段放进代码围栏，
                # 避免它的标题层级与本文档打架、列表被当成本文档的结构。
                # 摘要来自转录，里面照抄了大量绝对路径，同样要脱敏。
                _block(a, _redact_home(s["digest"]))
            a("")

    a(tr.t("doc.h.prompt") + "\n")
    a(tr.t("doc.prompt.howto"))
    a(tr.t("doc.prompt.howto2") + "\n")
    # 提示词里含会话话题，话题可能带反引号（`E:\path` 这类），同样要挑更长的围栏。
    _block(a, ctx["prompt"])
    return "\n".join(L) + "\n"
