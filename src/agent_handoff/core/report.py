#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染交接 Markdown 与新会话开场提示词。这里的一切都来自实测。"""
from __future__ import annotations

import os
import re
from typing import Any

from ..i18n import Translator
from ..platform import local_home_names
from .attribution import ATTRIBUTION_LINE_BUDGET

# transcript 里的 `_redact` 是**函数内**导入的（延迟），所以这里在模块层导入它
# 不会成环。反过来写就会。
from .transcript import deep_link, resume_command

# 单个会话的压缩摘要在交接文档里的上限。
#
# 摘要此前**不设上限**，理由是「每个窗口只总结它自己那一段，丢一个就少一个
# 阶段」——那个理由对，但代价被漏算了。实测本机一份 70 窗口的 rollout：摘要
# 320,024 字符，约 8 万 token。交接文档存在的意义是救一个快满的会话，而把
# 这份文档粘进新会话会**当场吃掉它 40% 的 200k 窗口**——工具本身成了它要
# 解决的那个问题。
#
# 6 万字符（约 1.5 万 token）的取值依据：实测 22 窗口以下的摘要都在 10 万
# 字符以内，而 22 窗口已经是「压缩过很多次」的会话；超过这个量级的摘要，
# 早期窗口讲的是几百轮之前的事，对「接下来做什么」几乎没有信息量。
#
# 超限时**保留最后的窗口**而不是最前的：最新的窗口讲的是当前进度，而交接要
# 回答的正是「上一个会话停在哪」。丢掉的部分明确告知，不静默截断——静默截断
# 会让读者以为看到了全部，然后据此推断「那件事没发生过」。
DIGEST_DOC_CHARS = 60_000


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

    还包含 POSIX 壳在 Windows 上的写法。Git Bash / MSYS 把 `HOME` 设成
    `/c/Users/alice`，WSL 设成 `/mnt/c/Users/alice`——从这些壳里跑工具时，
    `expanduser("~")` 返回的是那种形态，而文档里的路径来自转录、写的是
    `C:/Users/alice`。两边形态不同，实测一条也匹配不上，用户名全量漏出去。
    所以不能只依赖当前 HOME 的写法，要把同一个家目录的所有等价形态都列出来。
    """
    home = os.path.expanduser("~")
    if not home:
        return []
    forms = {home, home.replace("\\", "/"), home.replace("/", "\\")}

    # 跨壳形态互转：Windows 盘符路径 ↔ MSYS `/c/…` ↔ WSL `/mnt/c/…`。
    posix = home.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", posix)
    if m:
        drive, rest = m.group(1).lower(), m.group(2)
        forms.add(f"/{drive}/{rest}")
        forms.add(f"/mnt/{drive}/{rest}")
    else:
        m = re.match(r"^(?:/mnt)?/([a-zA-Z])/(.+)$", posix)
        if m:
            drive, rest = m.group(1), m.group(2)
            forms.add(f"{drive.upper()}:/{rest}")
            forms.add(f"{drive.upper()}:\\" + rest.replace("/", "\\"))
            forms.add(f"/{drive.lower()}/{rest}")
            forms.add(f"/mnt/{drive.lower()}/{rest}")

    slug = re.sub(r"[^A-Za-z0-9]", "-", home)
    out = [f for f in forms if f]
    if len(slug) > 3:
        out.append(slug)
    # 长的先替换：slug 通常最长，且包含盘符与分隔符的编码形态。
    return sorted(out, key=len, reverse=True)


def _sep_insensitive(form: str) -> str:
    """把一个路径形态编成「分隔符不敏感」的正则。

    为什么不能直接 `re.escape`：路径分隔符在一条路径里可以混用。
    `os.path.join` 拼出来的、转录 JSON 里记下来的，实测常见
    `C:\\Users/devin/proj` 与 `C:/Users\\devin\\proj` 这类混合形态。
    整体转义只能匹配「全 `/`」或「全 `\\`」，混的一条都匹配不上，
    于是用户名从文档里漏出去——而调用方以为已经脱敏了。

    做法：按分隔符切段，段内转义，段间用 `[\\\\/]+` 连接。同时容忍重复
    分隔符（`C:\\\\Users` 在 JSON 原文里就是这样）。
    """
    parts = [p for p in re.split(r"[\\/]+", form) if p]
    if not parts:
        return re.escape(form)
    body = r"[\\/]+".join(re.escape(p) for p in parts)
    # 保留开头的分隔符（POSIX 形态 `/c/Users/…` 的那一个）。
    return (r"[\\/]+" if re.match(r"^[\\/]", form) else "") + body


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
        if re.fullmatch(r"[A-Za-z0-9-]+", form):
            out = re.sub(re.escape(form), "_HOME_", out, flags=re.I)
        else:
            out = re.sub(_sep_insensitive(form), "~", out, flags=re.I)
    return out


# 密钥要在**任何**外来文本进文档之前脱掉。
#
# 为什么这一道必须存在：会话原文（用户问了什么、最后一句输入、压缩摘要）
# 是原样写进交接文档的，而用户在会话里粘过 API key、`Authorization: Bearer …`、
# 数据库口令是常态。文档随后被自动提交进 git——凭据一旦进历史，删文件也去不掉，
# 只能改写历史或轮换密钥。家目录脱敏挡不住这个：密钥不是路径。
#
# 只认**有明确前缀**的形态，不做「高熵字符串」猜测：那类启发式会把 commit sha、
# base64 编码的正常内容、长文件名一起打码，把文档毁掉。宁可漏掉自造格式的私有
# token，也不能让读者面对一份处处是 `***` 的交接文档——那等于没有文档。
_SECRET_RULES: list[tuple[str, str]] = [
    # OpenAI / Anthropic 及多数仿此格式的服务
    (r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_-]{16,}", "sk-***"),
    # GitHub：ghp 个人令牌、gho OAuth、ghu/ghs 应用、ghr 刷新
    (r"\bgh[pousr]_[A-Za-z0-9]{16,}", "gh*_***"),
    # GitHub 细粒度令牌
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}", "github_pat_***"),
    # Slack
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "xox*-***"),
    # AWS access key id（配套的 secret 无固定前缀，靠下面的赋值规则兜）
    (r"\bAKIA[0-9A-Z]{16}\b", "AKIA***"),
    # Google API key
    (r"\bAIza[A-Za-z0-9_-]{35}\b", "AIza***"),
    # HTTP 认证头
    (r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}", r"\1 ***"),
    # PEM 私钥整块
    (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY----- *** -----END PRIVATE KEY-----",
    ),
    # URL 里的口令：`https://user:pass@host` —— 只打码口令，保留主机名
    (r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):[^\s/@]{3,}@", r"\1:***@"),
    # `KEY=value` / `key: value` 形态。键名必须含密钥类词根，避免误伤普通配置。
    #
    # 键名的前后缀都写成**可选**（`*` 而不是「至少一个字符」）：词根可能就是
    # 整个键名。实测写成必需前缀时，`api_key:`、`token:`、`password=` 这类
    # 最常见的裸键名一个都匹配不上，只有 `MY_API_KEY` 这种带前缀的能中——
    # 而裸键名恰恰是配置文件和会话里出现最多的写法。
    #
    # 值排除纯数字。`token` 这个词根同时出现在密钥和**计量字段**里：
    # `max_tokens=8192`、`tokens: 4096`、`token_count: 123` 是模型配置的常态，
    # 把它们打成 `***` 会毁掉交接文档最有用的那部分信息（上下文占用是本工具
    # 的核心判据）。密钥不会是纯十进制数，所以按值的形状排除最省事，
    # 且不需要维护一张永远补不全的键名白名单。
    #
    # 值的三种写法都要认：双引号、单引号、裸值。裸值分支必须排在最后，
    # 否则它会先吃掉开头的引号再停在那里，带引号的值反而漏掉。
    (
        r"(?i)\b([A-Za-z0-9_]*"
        r"(?:api[_-]?key|secret|passwd|password|token|credential|private[_-]?key)"
        r"[A-Za-z0-9_]*)"
        r"(\s*[:=]\s*)"
        r"(?!\d+(?:\s|$|[,;)\]}]))"
        r"(?:\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"',;)\]}]{4,})",
        r"\1\2***",
    ),
]


def _redact_secrets(text: str) -> str:
    """把外来文本里形状明确的凭据换成占位符。

    保留前缀（`sk-***` 而不是整段 `***`）：读者需要知道「这里曾有一个
    OpenAI 密钥」才能判断该去轮换哪一个，全打成星号反而丢掉了这条线索。
    """
    if not text:
        return text
    out = text
    for pattern, repl in _SECRET_RULES:
        out = re.sub(pattern, repl, out)
    return out


def _redact_foreign_users(text: str) -> str:
    """把**别人机器**上的用户名也脱掉。

    `_redact_home` 只认本机家目录。转录从另一台电脑搬过来时，里面记的是
    `D:\\Users\\bob\\...` 这类路径，本机脱敏规则一条也匹配不上——于是交接文档
    反而把别人的用户名写了进去。迁机恰恰是这个工具该支持的场景，不能在这里漏。

    两种形态都要处理，实测漏掉后者会让用户名从转录路径里漏出去：
      · 真实路径 `D:\\Users\\bob\\proj`、`/home/bob/proj`
      · Claude 的 slug 目录名 `D--Users-bob-myproj`——它把 cwd 里的非字母数字
        全换成 `-`，用户名嵌在其中一段里，认不出真实分隔符

    只替换用户名那一段，保留其余路径：读者仍要靠路径判断「那台机器上的项目在
    哪」，抹掉整条路径等于把有用信息一起丢了。本机用户名交给 `_redact_home`
    处理（它还负责 slug 形态），这里跳过，免得同一个名字被换两次。
    """
    if not text:
        return text
    local = local_home_names()

    def sub(m: re.Match[str]) -> str:
        name = m.group(2)
        if name.lower() in local:
            return m.group(0)
        return m.group(1) + "_USER_"

    # 分组 1 是家目录前缀（含分隔符），分组 2 是紧随其后的那一段名字。
    #
    # 前缀集合覆盖实际会遇到的家目录布局。原版只有四条（盘符 Users、/home/、
    # /Users/、/mnt/x/Users/），漏掉的这些都是真实存在的形态：
    #   · `/export/home/`、`/usr/home/` —— Solaris / 部分 BSD 的布局
    #   · `C:\\Documents and Settings\\` —— 旧 Windows，转录可能来自老机器
    #   · `\\\\?\\C:\\Users\\` —— Windows 长路径前缀，超过 260 字符时出现
    # 漏一条就等于那种机器上的用户名原样进文档。
    #
    # `/root` 刻意不在这里。它是**家目录本身**而不是家目录的父目录：
    # `/root/myproj` 里紧随其后的那一段是项目名，不是用户名。把它当前缀处理
    # 会把项目名打成 `_USER_`，抹掉读者判断「那台机器上项目在哪」所需的信息，
    # 而 `root` 这个用户名本来就没有隐私可言（容器里人人都是 root）。
    out = re.sub(
        r"((?:(?:\\\\\?\\)?[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+"
        r"|/home/|/Users/|/export/home/|/usr/home/"
        r"|/mnt/[a-z]/(?:Users|Documents and Settings)/))"
        r"([^\\/\s\"'`,;:)\]}]+)",
        sub,
        text,
        flags=re.I,
    )
    # slug 形态：`<盘符>--Users-<名字>-<其余>`。用户名是 `Users-` 之后、
    # 下一个 `-` 之前的那一段。
    return re.sub(
        r"((?:^|[\s\"'`(=\\/])[A-Za-z]--+Users-+)([^\\/\s\"'`,;:)\]}-]+)",
        sub,
        out,
        flags=re.I,
    )


def _redact(text: str) -> str:
    """文档里所有外来文本都过这一道：先脱密钥，再脱本机家目录，最后脱别人的用户名。

    三者顺序要紧：
      · 密钥最先——它可能嵌在路径里（`/home/bob/.config/sk-…`），
        路径脱敏若先跑会改动上下文，让密钥正则错过匹配边界。
      · `_redact_home` 把本机家目录变成 `~`，之后 `_redact_foreign_users`
        就不会再碰它——本机名字只被处理一次。

    这是**唯一**的对外文本出口。任何写进交接文档的外来字符串都必须过这里，
    包括测试摘要、git 报错、pitfalls 和提交主题：它们同样可能带绝对路径，
    而绝对路径里带着用户名。
    """
    return _redact_foreign_users(_redact_home(_redact_secrets(text)))


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


def _fullness_cell(r: dict[str, Any], tr: Translator) -> str:
    """体征表里的占用格。判定的主依据，所以排在体积之前。

    三种写法对应三种可信程度：压缩次数最硬（自动压缩只在快装不下时触发），
    占用率次之（分母来自转录自己写的上限），只有占用量时不编分母。
    读不到就写破折号——空着会让人以为是 0。

    压缩归档且本机没有 zstd 实现时另写一句，而不是复用破折号：破折号的意思是
    「这份转录里没写占用」，而这里的情况是「正文一行都没读到」。两者能做的事
    完全不同——后者装个解压包就能拿到全部数据。
    """
    if r.get("compressed_unreadable"):
        return tr.t("doc.vitals.cell.unreadable")
    if r.get("compactions"):
        return tr.t("doc.vitals.cell.compacted", count=r["compactions"])
    tokens = r.get("tokens") or 0
    if not tokens:
        return "—"
    window = r.get("context_window") or 0
    if window:
        return f"{round(tokens * 100 / window)}%"
    return f"{tokens:,}"


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
    # 没有 git 时不要渲染「分支 ，HEAD 」这种断句——空值读起来像读取失败。
    # 换一句只讲目录的开场，并说明为什么没有 git 现场。
    if ctx.get("has_git", True):
        a(tr.t(
            "prompt.resume",
            repo_name=ctx["repo_name"],
            repo=ctx["repo"],
            branch=ctx["branch"],
            head=ctx["head_sha"],
        ))
        # 仓库身份与「这台机器上的位置」是两件事。远程 URL + 完整 sha 在任何机器上
        # 都能定位到同一个状态；路径不能。未推送的提交则根本传不过去——新会话在别处
        # clone 只会拿到远程有的东西，不说清楚它会以为自己看到的是全部。
        if ctx.get("remote"):
            a(tr.t("prompt.identity", remote=ctx["remote"], sha=ctx.get("head_full") or ctx["head_sha"]))
        else:
            a(tr.t("prompt.no_remote"))
        if ctx.get("unpushed"):
            a(tr.t("prompt.unpushed", count=ctx["unpushed"]))
    else:
        a(tr.t("prompt.resume_no_git", repo_name=ctx["repo_name"], repo=ctx["repo"]))
        a(tr.t("prompt.no_git"))
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
                # 「最后一次真的在动」优先于文件时间：后者会被子代理写入、
                # 云同步、备份推后，Codex 侧甚至等于会话创建时刻。取不到时
                # 才退回 mtime——这份提示词里的时间是新会话判断「上一个会话
                # 停在多久之前」的唯一依据。
                mtime=s.get("active_at_text") or s.get("mtime_text", ""),
            ))
            a(tr.t("prompt.sessions.path", path=s.get("path", "")))
            if s.get("session_id"):
                a(tr.t("prompt.sessions.id", value=s["session_id"]))
            # 工作目录要单独给：多个会话汇总时，它们可能在不同子目录里跑过，
            # 而新会话第一件事就是 cd 到对的地方。只给仓库根不够。
            if s.get("cwd"):
                a(tr.t("prompt.sessions.cwd", path=_redact(s["cwd"])))
            # 原生续接命令要给出来：它无损，而这份交接是有损摘要。
            # 只要那个会话还在同一台机器上、还完好，读到这里的人就该先试它。
            cmd = resume_command(s.get("agent", ""), s.get("session_id", "") or "")
            if cmd:
                a(tr.t("prompt.sessions.resume", cmd=cmd))
            link = deep_link(
                s.get("agent", ""), s.get("session_id", "") or "", s.get("thread_id", "") or ""
            )
            if link:
                a(tr.t("prompt.sessions.deeplink", url=link))
            # 每会话四件套在包里的位置。**按会话 ID 直接拼**，不依赖导出结果：
            # 提示词在打包之前就生成好了，等 `artifacts_dir` 回填是等不到的
            # （实测那个字段只存在于包的 manifest 里，这一行于是永不显示）。
            #
            # 路径形态是固定契约（`sessions/<id>/`），所以拼出来的一定对；
            # 没打包时这一行指向一个不存在的目录，但它前面写着「若已打包」，
            # 而且同一段里已经给了转录原始路径作为不打包时的退路。
            sid = (s.get("session_id") or "").strip()
            if sid:
                a(tr.t("prompt.sessions.artifacts", path=f"sessions/{sid}"))
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
    # 过期声明挂在 HEAD 上：没有 git 就没有可比较的锚点，只能给时间。
    if ctx.get("has_git", True) and ctx["head_sha"]:
        a(tr.t("prompt.expiry", now=ctx["now"], head=ctx["head_sha"]))
    else:
        a(tr.t("prompt.expiry_no_git", now=ctx["now"]))
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


# 摘要里窗口之间的分隔行，形如 `===== [2/3] =====`。
# 与 `vitals._digest_sep` 生成的形态配对——那边刻意不带任何语言，所以这条
# 识别式在三种界面语言下都成立。不从 vitals 导入是为了不让渲染层依赖采集层：
# 交接文档也可能从导入的包（`--import-bundle`）渲染，那时没有采集器参与。
_DIGEST_SEP = re.compile(r"^=+\s*\[\d+\s*/\s*\d+\]\s*=+$", re.M)


def _clip_digest(digest: str, limit: int = DIGEST_DOC_CHARS) -> tuple[str, int]:
    """摘要超限时保留**最新的**窗口，返回 (保留的正文, 丢掉的字符数)。

    为什么保留最新而不是最早：交接要回答「上一个会话停在哪」，那写在最后一个
    窗口里。最早的窗口讲的是几百轮之前的事——实测一份 70 窗口的 rollout 里，
    第 1 个窗口说的是「先调研同类项目」，而第 70 个说的是当前正在改哪个函数。

    按**窗口边界**切而不是按字符数硬切：硬切会把一个窗口劈成两半，读者拿到
    半句话开头的一段文字，既不知道它属于哪个阶段，也无法判断它是否完整。
    单个窗口就已经超限时才退回字符截断——那时没有更好的选择。
    """
    if len(digest) <= limit:
        return digest, 0
    # 按分隔行切成若干块。`_DIGEST_SEP` 是多行模式，split 后偶数位是分隔行
    # 本身（第一块可能是没有分隔行的单窗口摘要）。
    marks = list(_DIGEST_SEP.finditer(digest))
    if not marks:
        # 单个窗口就超限：只能字符截断，且保留尾部。
        kept = digest[-limit:]
        return kept, len(digest) - len(kept)

    # 从后往前累加窗口，直到再加一个就超限。
    starts = [m.start() for m in marks]
    cut_at = starts[-1]
    for pos in reversed(starts):
        if len(digest) - pos > limit:
            break
        cut_at = pos
    kept = digest[cut_at:]
    if not kept.strip():
        # 极端情况：最后一个窗口自己就超限。退回字符截断，仍然保尾。
        kept = digest[-limit:]
    return kept, len(digest) - len(kept)


def _rule(head: str) -> str:
    """按表头的实际列数生成分隔行。

    分隔行的 cell 数必须与表头一致，否则 Markdown 根本不把它当表格渲染。
    表头来自各语言的 i18n 文案，写死列数会在文案改列时静默失配，所以从表头数。
    """
    cells = head.strip().strip("|").split("|")
    return "|" + "|".join(["---"] * len(cells)) + "|"


def build_handoff(ctx: dict[str, Any], tr: Translator) -> str:
    """渲染交接 Markdown。"""
    L: list[str] = []
    a = L.append
    a(tr.t("doc.title", repo=ctx["repo_name"], date=ctx["date"]) + "\n")
    a(tr.t("doc.generated"))
    a(tr.t("doc.generated2"))
    if ctx["plan_rel"]:
        a(tr.t("doc.not_substitute", plan=_redact(ctx["plan_rel"])))
        a(tr.t("doc.not_substitute2") + "\n")
    else:
        a("\n")

    a(tr.t("doc.h.scene") + "\n")
    # 仓库路径也要脱敏。它是本机路径，读者就在本机用，但这份文档会进 git、
    # 可能推到公开仓库——受众不同。`~/proj` 在 shell 与两个 APP 里都会展开，
    # 脱敏后仍然可直接使用。
    a(tr.t("doc.scene.repo", repo=_redact(ctx["repo"])))
    # 分支 / HEAD / 领先数都只在有 git 时才有意义。渲染空值会让读者
    # 分不清「没有版本控制」和「读 git 失败」。
    if ctx.get("has_git", True):
        branch_line = tr.t("doc.scene.branch", branch=ctx["branch"])
        # detached HEAD 要单独说，不能只标「不是主干」。
        #
        # 在 detached 状态下提交，提交不属于任何分支：切走之后它就只能靠 sha
        # 找回，`git branch` 里看不到，reflog 过期后会被 gc 掉。接续会话必须
        # 知道这件事——否则它照常提交，而那些提交随时可能变成悬空对象。
        #
        # 判定用 `<detached>` 这个标记而不是字面量 `HEAD`：gitops 那边刻意
        # 不透传 git 的字面量，正是为了让「分支」和「没有分支」可区分。
        if ctx["branch"] == "<detached>":
            branch_line += tr.t("doc.scene.detached")
        elif ctx["branch"] not in ("main", "master"):
            branch_line += tr.t("doc.scene.not_trunk")
        a(branch_line)
        a(tr.t("doc.scene.head", head=ctx["head"]))
        if ctx["ahead"]:
            a(tr.t("doc.scene.ahead", count=ctx["ahead"]))
    else:
        a(tr.t("doc.scene.no_git"))
    if ctx["plan_rel"]:
        a(tr.t("doc.scene.plan", plan=_redact(ctx["plan_rel"])))
    a(tr.t("doc.scene.now", now=ctx["now"]) + "\n")

    a(tr.t("doc.h.step1") + "\n")
    a("```")
    a(_redact(ctx["commit_result"]))
    a("```")
    if ctx["protected"]:
        a("\n" + tr.t("doc.protected"))
        for p in ctx["protected"]:
            a(f"- `{_redact(p)}`")
    a("")

    a(tr.t("doc.h.step2") + "\n")
    if ctx["report"]:
        if ctx.get("intent_sections"):
            a(tr.t("doc.step2.note", sections=" / ".join(ctx["intent_sections"][:4])) + "\n")
        a(tr.t("doc.table.head"))
        a(_rule(tr.t("doc.table.head")))
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
            a(f"**{name}** — `{_redact(ctx['test_commands'][name])}`")
            a("```")
            a(_redact(line))
            a("```")
    else:
        a(tr.t("doc.step3.skipped"))
    a("")

    if ctx["pitfalls"]:
        a(tr.t("doc.h.env") + "\n")
        for n in ctx["pitfalls"]:
            a(f"- {_redact(n)}")
        a("")

    if ctx["recent_commits"]:
        a(tr.t("doc.h.commits") + "\n```")
        a(_redact(ctx["recent_commits"]))
        a("```\n")

    if ctx["vitals"]:
        a(tr.t("doc.h.vitals") + "\n")
        a(tr.t("doc.vitals.basis") + "\n")
        a(tr.t("doc.vitals.head"))
        a(_rule(tr.t("doc.vitals.head")))
        for r in ctx["vitals"][:8]:
            label = tr.t(f"band.{r['band']}")
            if r["band"] == "critical":
                label = f"**{label}**"
            a(
                f"| {r['agent']} | `{_vitals_id(r)}` | {_fullness_cell(r, tr)} | "
                f"{r['mb']:.1f} MB | {r['fatal']} | {r.get('aborted', 0)} | {r['errors']} | {label} |"
            )
        worst = ctx["vitals"][0]
        if worst["band"] in ("critical", "high"):
            a(
                "\n"
                + tr.t(
                    "doc.vitals.worst",
                    agent=worst["agent"],
                    file=_vitals_id(worst),
                    mb=f"{worst['mb']:.1f}",
                    # 最紧迫的那个要说清凭什么紧迫。只报体积会让读者以为判据是
                    # 文件大小——而那正是这次改掉的东西。
                    context=_fullness_cell(worst, tr),
                    advice=tr.t(f"band.advice.{worst['band']}"),
                )
            )
        a("")

    sessions = ctx.get("sessions") or []
    if sessions:
        a(tr.t("doc.h.sessions") + "\n")
        a(tr.t("doc.sessions.intro") + "\n")
        # 引用的内容保留原语言。不说这一句，切到 EN 的读者会把中文引用当成
        # 本地化没做完，而它其实是刻意的——译过就不再是那个会话说过的话。
        a(tr.t("doc.sessions.verbatim_note") + "\n")
        for s in sessions:
            # 标题来自 label，label 可能取自摘要的第一句实质内容——而摘要里
            # 照抄了大量绝对路径。标题同样要脱敏，否则用户名从这里漏出去。
            title = _redact((s.get("label") or "").strip()) or s.get("session_id", "")
            a(f"### {s.get('agent', '')} — {title}\n")
            a(tr.t("doc.sessions.id", value=s.get("session_id", "") or "-"))
            if s.get("thread_id"):
                a(tr.t("doc.sessions.thread", value=s["thread_id"]))
            # 时间取转录里最后一条记录，回落到文件时间时明确标注：文档会被
            # 另一个会话当事实读，一个来源不明的时间戳会让它误判「多久之前」。
            stamp = s.get("active_at_text") or s.get("mtime_text", "")
            if s.get("time_source") and s["time_source"] != "record":
                stamp = tr.t("doc.sessions.time_approx", value=stamp)
            a(tr.t("doc.sessions.mtime", value=stamp))
            # 转录来自别的电脑时，下面这些路径在本机全都无效。明说一句，
            # 免得读者（和接续会话的智能体）照着去找文件，找不到才发现不对。
            if s.get("is_foreign"):
                a(tr.t("doc.sessions.foreign"))
            if s.get("cwd"):
                a(tr.t("doc.sessions.cwd", value=_redact(s["cwd"])))
            # 「在改哪个仓库」要写进文档，而且要写在启动目录旁边。
            #
            # 为什么：这份文档的读者是**下一个会话**，它会照着这里的路径去改
            # 代码。只给启动目录等于把它送进错的仓库——实测本机三个会话的启动
            # 目录全部不是它们真正在改的那个。两个都写、并说明依据与冲突，
            # 让接手方自己判断该在哪工作、该在哪 resume。
            vd = s.get("verdict") or {}
            if vd.get("primary"):
                a(tr.t(
                    "doc.sessions.work_repo",
                    value=_redact(vd["primary"]),
                    note=tr.t("cli.card.conf." + (vd.get("confidence") or "none")),
                    basis=tr.t("evidence.level." + vd["basis"]) if vd.get("basis") else "-",
                ))
                if vd.get("conflict"):
                    a(tr.t("doc.sessions.work_conflict"))
                # 证据逐条列出，最多 4 条：让接手方能核实这个结论，而不是接受
                # 一个无从追溯的路径。超过 4 条时后面的都是弱证据，没有价值。
                for e in (vd.get("evidence") or [])[:4]:
                    a(tr.t(
                        "doc.sessions.evidence",
                        level=tr.t("evidence.level." + e.get("level", "mention")),
                        hits=e.get("hits", 0),
                        value=_redact(e.get("repo", "")),
                    ))
                if vd.get("truncated"):
                    a(tr.t("doc.sessions.evidence_partial", lines=ATTRIBUTION_LINE_BUDGET))
            for rp in (s.get("repos") or [])[:3]:
                a(tr.t("doc.sessions.repo", value=_redact(rp)))
            a(tr.t("doc.sessions.file", value=_redact(s.get("path", ""))))
            # 停下来时的模型与策略。这几项是逐轮记录的，不在会话元数据里——
            # 而新会话要重现上一个会话的结果，得知道它当时在什么配置下跑
            # （同一份转录里模型换过是常态）。
            tctx = s.get("turn_context") or {}
            if tctx:
                bits = [f"{k}={v}" for k, v in tctx.items()]
                a(tr.t("doc.sessions.turn_context", value=_redact(", ".join(bits))))
            if s.get("digest_windows", 0) > 1:
                a(tr.t("doc.sessions.windows", count=s["digest_windows"]))
            asks = s.get("asks") or []
            if asks:
                a("")
                a(tr.t("doc.sessions.asks"))
                for one in asks:
                    _block(a, _redact(one))
            if s.get("last_prompt"):
                a("")
                a(tr.t("doc.sessions.last_prompt"))
                _block(a, _redact(s["last_prompt"]))
            # 报错原文。放在摘要之前：摘要是模型的转述，而这是原始事实，
            # 而且它回答的是最贵的那个问题——「哪条路已经走不通」。
            # 只数个数（上面体征表里的 errors 列）等于告诉新会话「出过 10 次事」，
            # 它据此什么也做不了，于是按同样的方式再错一次。
            errs = s.get("errors_text") or []
            if errs:
                a("")
                a(tr.t("doc.sessions.errors", count=len(errs)))
                for one in errs:
                    _block(a, _redact(one))
            if s.get("digest"):
                a("")
                a(tr.t("doc.sessions.digest"))
                # 摘要是 Markdown（带 # 标题与代码块），整段放进代码围栏，
                # 避免它的标题层级与本文档打架、列表被当成本文档的结构。
                # 摘要来自转录，里面照抄了大量绝对路径，同样要脱敏。
                digest, cut = _clip_digest(s["digest"])
                if cut:
                    # 先说被截了多少，再给正文。顺序很重要：读者要在读之前
                    # 就知道这不是全部，否则他会把「最早的阶段」当成不存在。
                    a(tr.t("doc.sessions.digest_clipped", dropped=f"{cut:,}",
                           kept=f"{len(digest):,}"))
                _block(a, _redact(digest))
            a("")

    a(tr.t("doc.h.prompt") + "\n")
    a(tr.t("doc.prompt.howto"))
    a(tr.t("doc.prompt.howto2") + "\n")
    # 提示词里含会话话题，话题可能带反引号（`E:\path` 这类），同样要挑更长的围栏。
    #
    # 这份**文档里嵌的副本**也要脱敏。终端打印的那份保留真实路径（那是给人
    # 复制粘贴的，就在本机用），但文档会进 git、可能推到公开仓库——两处受众
    # 不同，同一段文字要有两种形态。此前只脱敏了会话小节，用户名照样从这里
    # 漏出去：实测生成的文档里 `C:\Users\<name>\.codex\…` 还在提示词块中。
    #
    # 家目录写成 `~` 之后提示词仍然可用：`~/.codex/…` 在 shell 与两个 APP 里
    # 都会被展开，接续会话照它一样能找到转录。
    #
    # 走 `_redact` 而不是只脱本机家目录：勾选的会话可能来自另一台电脑，
    # 那些路径里带的是别人的用户名，本机规则一条都匹配不上。
    _block(a, _redact(ctx["prompt"]))
    return "\n".join(L) + "\n"
