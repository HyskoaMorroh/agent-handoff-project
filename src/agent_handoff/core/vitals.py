#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话转录体检 —— 哪个会话该交接了。

判据来自本机 54 个 Claude + 54 个 Codex 转录的实测分布，不是拍脑袋：
250 KB 以下无一出现致命错误，8 MB 以上全部出现过。这些是观察到的断点。

性能重写说明（原版的主要瓶颈）：
  原版对每个转录读三遍——一遍数致命签名与工具错误，一遍读身份卡，
  一遍捞仓库路径。40 个转录 × 平均 3 MB × 3 遍 = 360 MB 的重复 I/O。
  这里改成单遍流式：一次遍历同时喂给三个提取器，早停条件满足就停。
  再叠三层加速：
    · 提前退出——身份与仓库都拿全后，剩下的行只做廉价的子串计数
    · 线程池并行——纯 I/O 等待，GIL 不是瓶颈
    · mtime+size 缓存——转录是追加写的，没变过的文件直接复用上次结果
"""
from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..platform import agent_session_roots, iter_path_candidates, nearest_repo, norm_path

# --- 判据 -----------------------------------------------------------------
# 真正杀死过会话的签名，不是只让人烦躁的那些。
#
# 必须锚定在**错误载荷**上，不能在整行原文上裸匹配。实测本机 14 个主转录：
# 239 个命中里 94 个来自 assistant 正文、83 个来自 user 正文——那是人和模型在
# **讨论**这些词（用户的 CLAUDE.md 里就写着「出现 content-blocked、熔断时…」），
# 不是会话真的死了。裸匹配会把「引用」当「发生」，于是 17/24 个转录被标 fatal>0，
# 其中 16 个体积完全健康，风险列变成纯噪声。
#
# 锚定方式：这些词必须出现在 JSON 的错误字段附近（error/message/type 的值里），
# 而不是散文中间。用「引号 + 冒号」的结构约束代替语义判断。
FATAL_SIG = re.compile(
    r'"(?:error|error_type|error_message|message|reason|stop_reason|code)"\s*:\s*'
    r'(?:"[^"]{0,200})?(?:content-blocked|熔断|无可用渠道|IMAGE_DIMENSION_EXCEEDED)'
)
# 会话被用户主动打断。实测 Codex 侧 `is_error` 在 40 个 rollout 里只有 3 次，
# 而 `turn_aborted` 有 6 次——后者才是「这轮没做完」的真实信号。半成品被当成
# 已完成是交接里最贵的误判，所以单独计数。
ABORT_SIG = re.compile(r'"turn_aborted"|"reason"\s*:\s*"interrupted"')
# 实测于 54 个 Claude + 54 个 Codex 转录：250 KB 以下无一带致命签名，
# 8 MB 以上全部带。下面是观察到的断点。
VITALS_BANDS = [
    (8_000_000, "critical"),
    (3_000_000, "high"),
    (1_000_000, "watch"),
    (0, "ok"),
]
BAND_ORDER = {"critical": 0, "high": 1, "watch": 2, "ok": 3}

# 开场提问里要跳过的噪声：插件清单、用户指令注入、纯标签行、caveman 模式广播。
# 这些不是人问的问题，认成开场提问会让整张卡片失去辨识度。
# `<local-command-caveat>` / `<command-name>` / `Caveat:` 是 Claude Code 在
# 斜杠命令与本地命令回显时注入的样板文字，对所有会话都一样——实测 20 个主转录
# 里 4 个的开场提问是这段，卡片上最有辨识度的字段变成对谁都一样的噪声。
SKIP_PROMPT = re.compile(
    r"<recommended_plugins>|<user_instructions>|^<[a-z_]+>$|Caveman|CAVEMAN"
    r"|<local-command-|<command-name>|<command-message>|^Caveat:"
    r"|#\s*Files mentioned by the user"
)

# 会话里真正值得带进新会话的东西，按可信度从高到低：
#   1. Codex 的 `compacted` 事件——上一轮压缩时模型自己写的交接摘要，
#      实测每份中位 5826 字符，含仓库路径、真实 HEAD、已读文档、任务范围。
#   2. Claude Code 的 `ai-title`——一句话话题摘要（「Kirara-ai3.3.0b8 handoff
#      prompt verification」），正是人认会话所需。
#   3. Claude Code 的 `last-prompt`——最后一次用户输入，说明会话停在哪。
# 这三样此前一个都没被读取：工具只统计体积与错误计数，会话的实际结论
# 在「转录 -> 交接文档」这一跳就被丢弃，新会话于是对前情一无所知。
#
# 压缩摘要**必须保留每一个窗口**，不能只留最后一个。实测本机 70 个带压缩的
# rollout（52 个是多窗口，最多 19 个窗口）：`window_number` 从 1 递增，
# `previous_window_id` 串成链，每个窗口只总结它自己那一段，后一个**不**包含
# 前一个（`last contains first verbatim: False`，全部样本如此）。只留最后一个
# 会丢掉中位 78%、p90 96% 的具体事实（commit sha、文件路径、测试计数），
# 其中 11 个 rollout 的「用户目标 / 红线约束」只出现在早期窗口——恰恰是最
# 不能丢的部分。
#
# 摘要也不再截断：截断点落在哪里完全取决于模型当时怎么分段，实测 62/70 个
# 末窗超过 4000 字符，中位被切掉 2925 字符、最多 13241 字符。交接文档是文件，
# 多几十 KB 无所谓；真正有上限的是提示词，而提示词里只放话题与路径。
_DIGEST_PREVIEW = 300
# 单条用户原话的保留长度。用户的要求可以很长（本机实测有 866 与 5084 字符的），
# 但交接需要的是要求本身，不是贴在里面的整份日志。
_ASK_CHARS = 1200
# Codex 在压缩摘要前面加的一段固定说明（「Another language model started to
# solve this problem…use the information in this summary to assist with your own
# analysis:」）。它对每份摘要都一样，留着会让卡片上的话题行变成对所有会话
# 都相同的英文样板，正文反而被挤出预览长度。真正的摘要从它之后开始。
_DIGEST_PREAMBLE = re.compile(
    r"^.{0,400}?summary produced by the other language model[^:]{0,120}:\s*",
    re.S,
)

# 单遍扫描时用的廉价预筛：先做子串判断，命中了才付正则的代价。
_ERR_MARKS = ('"is_error":true', '"isError":true')


def _looks_pathy(raw: str) -> bool:
    """这一行有没有可能含绝对路径？只用来省掉正则，判错方向必须偏保守。

    早先这里是一张目录白名单（/home/、/mnt/、/opt/…）。那是错的：枚举永远
    不全——/tmp、/workspace、/data、/srv/git 各种都漏，而漏掉的后果是那一行
    的路径永远不被提取，仓库推断静默失败。真 Linux 上跑测试才暴露。

    改成结构判断：盘符形态看 `:\\` 或 `:/`，POSIX 形态看有没有连续两段
    `/x/y`。宁可多进几次正则，不可漏。
    """
    if ":\\" in raw or ":/" in raw:
        return True
    first = raw.find("/")
    if first < 0:
        return False
    # `/a/b` 至少要有第二个斜杠，且两个斜杠之间非空——排除 "//" 和 "http://"
    # 之后紧跟的空段这类噪声（它们已被上面的 `:/` 命中，不影响正确性）。
    second = raw.find("/", first + 1)
    return second > first + 1

# 身份与仓库信息只出现在开头。原版对 Claude 扫 400 行、对路径扫 260 行；
# 这里统一取两者上界，一遍走完就够。
IDENT_LINE_BUDGET = 400
PATH_LINE_BUDGET = 260


def band_for(size: int, fatal: int = 0, aborted: int = 0) -> str:
    """风险区间。体积是主判据，但致命错误与中断能把它往上抬。

    原版只看体积，于是一个 0.9 MB 但真的撞过熔断的会话被标「健康」，
    而 1.7 MB 一切正常的被标「留意」。用户照徽章决策就会漏掉真出事的那个。
    体积衡量的是「还能撑多久」，fatal/aborted 衡量的是「已经出事了没有」——
    两件事都要进判定。
    """
    band = "ok"
    for threshold, name in VITALS_BANDS:
        if size >= threshold:
            band = name
            break
    if fatal or aborted:
        # 已经出过致命错误或被打断：至少「留意」，出过多次直接升到「尽快交接」。
        floor = "high" if (fatal + aborted) >= 3 else "watch"
        if BAND_ORDER[floor] < BAND_ORDER[band]:
            band = floor
    return band


@dataclass
class SessionRow:
    """一个转录的体检结果。字段名与原版 dict 保持一致，便于逐字迁移。"""

    agent: str
    path: Path
    file: str
    mtime: datetime
    mb: float
    size: int
    fatal: int
    errors: int
    band: str
    # 被用户主动打断的轮次。实测 Codex 侧 `is_error` 在 40 个 rollout 里只有
    # 3 次，而 `turn_aborted` 有 6 次——后者才是「这轮没做完」的真实信号，
    # 而半成品被当成已完成是交接里最贵的误判。
    aborted: int = 0
    session_id: str = ""
    thread_id: str = ""
    cwd: str = ""
    branch: str = ""
    version: str = ""
    origin: str = ""
    first_prompt: str = ""
    # 会话实际做了什么。空字符串表示这份转录里没有对应的事件，不是「读失败」。
    title: str = ""        # Claude 的 ai-title：一句话话题
    last_prompt: str = ""  # Claude 的 last-prompt：会话停在哪
    digest: str = ""       # Codex 的 compacted 摘要：模型自己写的交接记录
    digest_windows: int = 0  # 摘要由几个压缩窗口拼成；0 表示这份转录没有压缩过
    asks: list[str] = field(default_factory=list)  # 用户原话，逐字保留
    is_subagent: bool = False
    repos: list[str] = field(default_factory=list)

    @property
    def repo(self) -> str:
        return self.repos[0] if self.repos else ""

    @property
    def label(self) -> str:
        """人认会话时最有辨识度的一行。

        优先级：AI 话题标题 > 压缩摘要的第一句实质内容 > 开场提问。前两者是
        会话内容的提炼，后者在斜杠命令回显时会退化成对所有会话都一样的样板。

        摘要开头往往是 `# 交接摘要` / `## 当前进度` 这种标题，本身不含信息，
        所以跳过纯标题行、窗口分隔行与列表符号，取第一行有实质内容的文本。
        取**最早**那个窗口：用户目标写在会话开头，越往后越是过程细节。
        """
        if self.title:
            return self.title
        if self.digest:
            for line in self.digest.splitlines():
                text = line.strip().lstrip("#*->+= ").strip()
                if text.startswith("压缩窗口 "):
                    continue
                if len(text) >= 8:
                    return text[:_DIGEST_PREVIEW]
        return self.first_prompt[:_DIGEST_PREVIEW]

    def to_dict(self) -> dict[str, Any]:
        """给 GUI 用的 JSON 形态。Path 与 datetime 都转成字符串。"""
        return {
            "agent": self.agent,
            "path": str(self.path),
            "file": self.file,
            "mtime": self.mtime.isoformat(timespec="seconds"),
            "mtime_text": f"{self.mtime:%Y-%m-%d %H:%M:%S}",
            "mb": round(self.mb, 2),
            "size": self.size,
            "fatal": self.fatal,
            "aborted": self.aborted,
            "errors": self.errors,
            "band": self.band,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "cwd": self.cwd,
            "branch": self.branch,
            "version": self.version,
            "origin": self.origin,
            "first_prompt": self.first_prompt,
            "title": self.title,
            "last_prompt": self.last_prompt,
            "digest": self.digest,
            "digest_windows": self.digest_windows,
            "asks": self.asks,
            "label": self.label,
            "is_subagent": self.is_subagent,
            "repos": self.repos,
            "repo": self.repo,
        }


def _take_text(content: Any) -> str:
    """从 Claude / Codex 两种 content 结构里取出纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    out = []
    for blk in content:
        if isinstance(blk, dict):
            txt = blk.get("text") or blk.get("input_text")
            if txt:
                out.append(txt)
    return " ".join(out).strip()


class _Extractor:
    """单遍扫描时的状态机：一行喂进来，三件事一起推进。

    拆成类而不是三个函数，是为了让"提前退出"能被真正利用——三件事都拿全时
    `done` 变 True，调用方就可以把剩下的行只做子串计数，不再解析 JSON。
    JSON 解析是这个流程里最贵的一步（多 MB 转录里每行都是一个大对象）。
    """

    __slots__ = ("agent", "ident", "repos", "_lineno", "_seen_meta")

    def __init__(self, agent: str, container_cwd: str = "") -> None:
        self.agent = agent
        self.ident: dict[str, str] = {
            "session_id": "",
            "cwd": "",
            "branch": "",
            "version": "",
            "first_prompt": "",
            "origin": "",
            "thread_id": "",
            "title": "",
            "last_prompt": "",
            "digest": "",
        }
        self.repos: list[str] = []
        self._lineno = 0
        self._seen_meta = False
        # Claude Code 记录了真实 cwd，答案通常就在那里。
        if container_cwd:
            direct = nearest_repo(container_cwd)
            if direct:
                self.repos.append(direct)

    @property
    def done(self) -> bool:
        """身份齐了、路径预算也用完了，就没必要再解析 JSON。

        摘要类字段（title / last_prompt / digest）不参与早停判定：它们出现在
        文件**末尾**（压缩发生在会话后期，ai-title 随对话更新而追加），
        用它们当早停条件会让扫描永远读到文件尾，早停就失去意义。
        所以只在预算内顺手收集，收不到就算了——空值表示「这份转录没有」，
        不表示读失败。
        """
        if self._lineno > PATH_LINE_BUDGET and self.ident["first_prompt"] and self.ident["cwd"]:
            return True
        return self._lineno > IDENT_LINE_BUDGET

    def feed(self, raw: str) -> None:
        self._lineno += 1
        lineno = self._lineno

        # 廉价预筛：这一行既不含路径样式、也不可能是我们要的结构时，
        # 仍然需要解析（身份字段可能在任意早期行），所以只对路径捞取做预筛。
        pathy = lineno <= PATH_LINE_BUDGET and _looks_pathy(raw)
        need_ident = not (
            self.ident["first_prompt"]
            and self.ident["cwd"]
            and self.ident["session_id"]
            and self.ident["version"]
        )
        if not pathy and not need_ident:
            return

        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(d, dict):
            return

        text = ""
        if self.agent == "Codex":
            if d.get("type") == "session_meta":
                # 一个 rollout 在被派生或续接时可以带多个 session_meta；
                # 第一个才是这个文件自己的身份。
                if not self._seen_meta:
                    self._seen_meta = True
                    p = d.get("payload") or {}
                    self.ident["session_id"] = str(p.get("session_id") or p.get("id") or "")
                    self.ident["cwd"] = str(p.get("cwd") or "")
                    self.ident["version"] = str(p.get("cli_version") or "")
                    self.ident["origin"] = str(p.get("originator") or p.get("source") or "")
                    # session_meta 里的 cwd 也可能直接指向仓库。
                    if self.ident["cwd"]:
                        direct = nearest_repo(self.ident["cwd"])
                        if direct and direct not in self.repos:
                            self.repos.append(direct)
                return
            p = d.get("payload") or {}
            if d.get("type") != "response_item" or p.get("role") != "user":
                return
            text = _take_text(p.get("content"))
            if text and not self.ident["first_prompt"] and not SKIP_PROMPT.search(text[:60]):
                self.ident["first_prompt"] = text
        else:
            # Claude Code 把 cwd / gitBranch / sessionId 写在大多数行上。
            if not self.ident["session_id"] and d.get("sessionId"):
                self.ident["session_id"] = str(d["sessionId"])
            if not self.ident["cwd"] and d.get("cwd"):
                self.ident["cwd"] = str(d["cwd"])
                direct = nearest_repo(self.ident["cwd"])
                if direct and direct not in self.repos:
                    self.repos.append(direct)
            if not self.ident["branch"] and d.get("gitBranch"):
                self.ident["branch"] = str(d["gitBranch"])
            if not self.ident["version"] and d.get("version"):
                self.ident["version"] = str(d["version"])
            if d.get("type") != "user":
                return
            text = _take_text((d.get("message") or {}).get("content"))
            if text and not self.ident["first_prompt"] and not SKIP_PROMPT.search(text[:60]):
                self.ident["first_prompt"] = text

        # Codex Desktop 把每个任务记在 Documents/Codex/<日期>/<slug> 这样的容器里，
        # 那不是项目本身；项目只在开头几轮的用户文本里以路径形式出现。
        if pathy and text:
            for cand in iter_path_candidates(text):
                repo = nearest_repo(cand)
                if repo and repo not in self.repos:
                    self.repos.append(repo)

    def finish(self, fp: Path) -> tuple[dict[str, str], list[str]]:
        self.ident["first_prompt"] = re.sub(r"\s+", " ", self.ident["first_prompt"])[:300]

        # Codex 用文件自身的 id 命名 rollout，但 session_meta 里可能带的是它派生自
        # 的那个线程的 id。UI 显示的是文件名，所以文件名优先；两者不一致时把 meta
        # 里的 id 并列保留。
        file_id = re.sub(r"^rollout-[\d\-T]+-", "", fp.stem)
        if self.agent == "Codex" and file_id and self.ident["session_id"] and file_id != self.ident["session_id"]:
            self.ident["thread_id"] = self.ident["session_id"]
            self.ident["session_id"] = file_id
        if not self.ident["session_id"]:
            self.ident["session_id"] = file_id

        # 会话都住在用户主目录下；裸的主目录根从来不是被操作的对象。
        home = norm_path(os.path.expanduser("~"))
        ranked = [r for r in self.repos if norm_path(r) != home]
        return self.ident, ranked + [r for r in self.repos if norm_path(r) == home]


# 摘要类事件的廉价预筛。这些事件出现在文件**末尾**，早停之后才轮到它们，
# 所以不能塞进 _Extractor（那会让早停失效，每个多 MB 转录都要整份解析）。
# 改成对每一行做子串判断，命中了才付 json.loads 的代价——实测每份转录
# 命中 3-30 行，不到千分之一。
_DIGEST_MARKS = ('"compacted"', '"ai-title"', '"last-prompt"')


class _DigestCollector:
    """收集「这个会话到底做了什么」。

    与 _Extractor 分开是因为两者在文件里的位置相反：身份在开头，摘要在末尾。
    """

    __slots__ = ("title", "last_prompt", "windows", "asks")

    def __init__(self) -> None:
        self.title = ""
        self.last_prompt = ""
        # 每个压缩窗口一段，按出现顺序。键是 window_number（没有就用出现序号），
        # 用 dict 去重：同一个窗口在续接的 rollout 里可能被重放。
        self.windows: dict[int, str] = {}
        # 用户的原话。压缩摘要是模型的转述，转述会丢措辞里的约束
        # （「不要删除项目 A」「不要强制推送」这类），而 `replacement_history`
        # 里保留着被摘要替换掉的原始 user 消息，逐字可用。
        self.asks: list[str] = []

    @property
    def digest(self) -> str:
        """把所有压缩窗口按顺序拼成一份完整记录。

        窗口之间加分隔标题，否则读者无法判断「第 3 段说的已完成」是哪个阶段的
        已完成——多个窗口各自都有「## 当前进度」，拼在一起会互相矛盾。
        """
        if not self.windows:
            return ""
        if len(self.windows) == 1:
            return next(iter(self.windows.values()))
        parts = []
        for i, key in enumerate(sorted(self.windows), 1):
            parts.append(f"===== 压缩窗口 {i} / {len(self.windows)} =====\n\n{self.windows[key]}")
        return "\n\n".join(parts)

    def feed(self, raw: str) -> None:
        if not any(mark in raw for mark in _DIGEST_MARKS):
            return
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(d, dict):
            return
        kind = d.get("type")
        if kind == "ai-title":
            # 后出现的标题是更新后的话题，覆盖旧的。
            title = str(d.get("aiTitle") or "").strip()
            if title:
                self.title = title[:200]
        elif kind == "last-prompt":
            text = str(d.get("lastPrompt") or "").strip()
            if text:
                self.last_prompt = re.sub(r"\s+", " ", text)[:600]
        elif kind == "compacted":
            p = d.get("payload") or {}
            msg = str(p.get("message") or "").strip()
            if msg:
                num = p.get("window_number")
                key = int(num) if isinstance(num, int) else len(self.windows) + 1
                # 不截断、不覆盖：每个窗口只总结它自己那一段，丢一个就少一个阶段。
                self.windows[key] = _DIGEST_PREAMBLE.sub("", msg).strip()
            # 被这次压缩替换掉的原始消息里，user 角色的就是用户原话。
            for item in p.get("replacement_history") or []:
                if not isinstance(item, dict) or item.get("role") != "user":
                    continue
                text = _take_text(item.get("content"))
                if not text or SKIP_PROMPT.search(text[:60]):
                    continue
                # 摘要本身也会作为 user 消息回灌，别把它当成用户的要求。
                if _DIGEST_PREAMBLE.match(text):
                    continue
                one = re.sub(r"\s+", " ", text).strip()
                if len(one) >= 4 and one not in self.asks:
                    self.asks.append(one[:_ASK_CHARS])


def _is_subagent(fp: Path) -> bool:
    """这份转录是子代理的，还是人真正对话的主会话？

    Claude Code 把子代理写进 `<会话id>/subagents/agent-*.jsonl`，Codex 给
    派生线程写独立的 rollout。它们不是人认得出的「那段对话」，而且数量远超
    主会话——实测本机最新 12 个 Claude 文件里 7 个是子代理（58%），最新 40 个
    Codex rollout 里 33 个带 parent_thread_id（83%）。不区分的话，默认
    `--limit 12` 会被子代理吃满，用户要交接的主会话被挤出列表。

    只看路径，不解析内容：这个判断要在「决定读哪些文件」之前做出来。
    """
    parts = [p.lower() for p in fp.parts]
    return "subagents" in parts or fp.name.lower().startswith("agent-")


def scan_one(agent: str, fp: Path, deep: bool = True) -> SessionRow | None:
    """单遍读完一个转录，同时得到风险计数、身份卡与涉及仓库。

    原版要读三遍；这里一遍。深度信息拿全之后剩下的行只做两次子串查找和
    一次正则，不再解析 JSON——那是整个流程里最贵的一步。
    """
    try:
        st = fp.stat()
    except OSError:
        return None

    fatal = 0
    errors = 0
    aborted = 0
    ident: dict[str, str] = {}
    repos: list[str] = []
    ex = _Extractor(agent) if deep else None
    dg = _DigestCollector() if deep else None
    try:
        with fp.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if FATAL_SIG.search(raw):
                    fatal += 1
                if ABORT_SIG.search(raw):
                    aborted += 1
                if _ERR_MARKS[0] in raw or _ERR_MARKS[1] in raw:
                    errors += 1
                if dg is not None:
                    dg.feed(raw)
                if ex is not None:
                    ex.feed(raw)
                    if ex.done:
                        # 深度信息已齐：收尾并丢掉提取器，剩下的行走廉价分支
                        # （子串查找 + 一次正则，摘要预筛也只是子串查找）。
                        ident, repos = ex.finish(fp)
                        ex = None
            if ex is not None:
                # 文件在预算用完前就结束了，用已有的部分收尾。
                ident, repos = ex.finish(fp)
    except OSError:
        return None

    if not deep:
        ident, repos = {"session_id": fp.stem}, []

    return SessionRow(
        agent=agent,
        path=fp,
        file=fp.name,
        mtime=datetime.fromtimestamp(st.st_mtime),
        mb=st.st_size / 1e6,
        size=st.st_size,
        fatal=fatal,
        errors=errors,
        band=band_for(st.st_size, fatal, aborted),
        aborted=aborted,
        session_id=ident.get("session_id", ""),
        thread_id=ident.get("thread_id", ""),
        cwd=ident.get("cwd", ""),
        branch=ident.get("branch", ""),
        version=ident.get("version", ""),
        origin=ident.get("origin", ""),
        first_prompt=ident.get("first_prompt", ""),
        title=dg.title if dg else "",
        last_prompt=dg.last_prompt if dg else "",
        digest=dg.digest if dg else "",
        digest_windows=len(dg.windows) if dg else 0,
        asks=list(dg.asks) if dg else [],
        is_subagent=_is_subagent(fp),
        repos=repos,
    )


# 转录是追加写的：同一个 (路径, 大小, mtime) 的扫描结果不会变。
# 一次会话里 --vitals 和交接流程都要扫，缓存能省掉第二遍全量 I/O。
#
# 必须有上限。键里含 mtime，而转录是**持续追加**的：网页界面长驻时每次
# `/api/vitals` 都会因为 mtime 变化生成新键，旧条目永不失效——每个 SessionRow
# 现在还带着完整的压缩摘要（实测单份可达 96 KB），一晚下来就是几百 MB。
# 逐出最旧的：转录越新越可能被再次问到。
_CACHE_MAX = 256
_cache: OrderedDict[tuple[str, int, int, bool], SessionRow] = OrderedDict()


def clear_cache() -> None:
    _cache.clear()


def _cached_scan(agent: str, fp: Path, deep: bool) -> SessionRow | None:
    try:
        st = fp.stat()
    except OSError:
        return None
    key = (str(fp), st.st_size, int(st.st_mtime), deep)
    hit = _cache.get(key)
    if hit is not None:
        # 命中的挪到末尾：LRU 的「最近用过」语义，避免热点条目被冷条目挤掉。
        _cache.move_to_end(key)
        return hit
    row = scan_one(agent, fp, deep)
    if row is not None:
        _cache[key] = row
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return row


def _newest_files(root: Path, limit: int, include_subagents: bool = False) -> list[Path]:
    """按修改时间取最新的若干个转录。

    先用 scandir 一次拿到 stat（rglob + 单独 stat 会对每个文件多一次系统调用），
    再按 mtime 排序。limit 之外的文件连打开都不打开。

    默认排除子代理转录：`limit` 是「给人看几个会话」的预算，被子代理占满就
    等于没有预算。子代理与主会话按 mtime 混排时前者几乎总在前面（它们是
    主会话工作过程中写出来的），所以必须在取 limit **之前**过滤，不能事后筛。
    """
    entries: list[tuple[float, Path]] = []
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False):
                            fp = Path(e.path)
                            if not include_subagents and _is_subagent(fp):
                                continue
                            entries.append((e.stat().st_mtime, fp))
                    except OSError:
                        continue
        except OSError:
            continue
    entries.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in entries[:limit]]


def scan_session_vitals(
    limit: int = 12, deep: bool = True, jobs: int = 0, include_subagents: bool = False
) -> list[SessionRow]:
    """按风险排序最新的转录。流式读取，只保留计数器与身份卡。

    jobs=0 时按转录数量与 CPU 核数自动决定并行度。这些任务全是磁盘等待，
    GIL 不构成瓶颈；实测 40 个多 MB 转录从串行的十几秒降到两三秒。
    """
    tasks: list[tuple[str, Path]] = []
    for agent, root in agent_session_roots():
        for fp in _newest_files(root, limit, include_subagents):
            tasks.append((agent, fp))
    if not tasks:
        return []

    if jobs <= 0:
        jobs = min(len(tasks), max(4, (os.cpu_count() or 4) * 2))
    jobs = max(1, min(jobs, 32))

    rows: list[SessionRow] = []
    if jobs == 1:
        for agent, fp in tasks:
            row = _cached_scan(agent, fp, deep)
            if row is not None:
                rows.append(row)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for row in pool.map(lambda t: _cached_scan(t[0], t[1], deep), tasks):
                if row is not None:
                    rows.append(row)

    rows.sort(key=lambda r: (BAND_ORDER[r.band], -r.mb))
    return rows


def sessions_for_repo(repo: Path, rows: Iterable[SessionRow]) -> list[SessionRow]:
    """哪些转录在这个仓库上工作过？

    不能只比 `cwd`：Codex Desktop 的 cwd 是 `Documents/Codex/<日期>/<slug>`
    这样的任务沙箱，实测 20/20 个 rollout 的 cwd 都解析不到任何 git 仓库。
    只看 cwd 的话，这个函数对所有 Codex 会话恒返回空——「按仓库收集会话」
    这个能力名存实亡。

    所以同时看 `repos`：那是从会话正文里捞出来的、经 `nearest_repo` 验证过
    的真实仓库路径。cwd 命中仍然保留（Claude Code 记录真实 cwd，那条路更直接）。
    """
    target = norm_path(repo)

    def touches(r: SessionRow) -> bool:
        cwd = norm_path(r.cwd)
        if cwd and (cwd == target or cwd.startswith(target + "/")):
            return True
        for cand in r.repos:
            c = norm_path(cand)
            if c == target or c.startswith(target + "/"):
                return True
        return False

    hits = [r for r in rows if touches(r)]
    hits.sort(key=lambda r: r.mtime, reverse=True)
    return hits


def find_sessions(needle: str, rows: Iterable[SessionRow]) -> list[SessionRow]:
    """按 ID 片段、目录片段或会话内容关键词定位会话。

    排序键第二项用 mtime 而不是体积：找会话时"最近那个"几乎总是要找的那个，
    原版按体积排会把一个月前的大转录顶到前面。

    话题标题与压缩摘要也参与匹配：用户记得的往往是「那次改工作流撤销的对话」
    而不是会话 ID 的前八位，而开场提问在斜杠命令回显时会退化成样板文字。
    """
    n = needle.strip().lower()
    if not n:
        return []
    scored: list[tuple[int, float, SessionRow]] = []
    for r in rows:
        sid = r.session_id.lower()
        tid = r.thread_id.lower()
        fname = r.file.lower()
        cwd = norm_path(r.cwd)
        repos = " ".join(norm_path(x) for x in r.repos)
        prompt = r.first_prompt.lower()
        content = f"{r.title}\n{r.last_prompt}\n{r.digest}".lower()
        ts = r.mtime.timestamp()
        if n in sid or n in fname:
            scored.append((0, -ts, r))
        elif tid and n in tid:
            scored.append((1, -ts, r))
        elif n in cwd or n in repos:
            scored.append((2, -ts, r))
        elif n in prompt or n in content:
            scored.append((3, -ts, r))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in scored]


def group_by_agent(rows: Iterable[SessionRow]) -> list[tuple[str, list[SessionRow]]]:
    """按智能体（APP）分组，每组内最近活动在前。

    为什么不按风险排一整个平铺列表：Claude Code 与 Codex 的转录混在一起时，
    "上一个会话"这件事只能靠看客户端字段一行行找。人认会话是先认 APP、
    再认时间——按这个顺序排，找上次那段对话就是看第一组第一张卡片。

    组的顺序也按"该组最近活动"排：刚用过的 APP 出现在最上面。同一 APP 内部
    严格按 mtime 倒序，不再让体积介入——体积是风险信号，卡片上的徽章已经
    在说这件事，用它排序会把一个月前的大转录顶到今天的会话前面。
    """
    buckets: dict[str, list[SessionRow]] = {}
    for r in rows:
        buckets.setdefault(r.agent, []).append(r)
    for group in buckets.values():
        group.sort(key=lambda r: r.mtime, reverse=True)
    # 组间：先按该组最新一条的时间倒序，时间相同再按名字，保证结果可复现。
    return sorted(
        buckets.items(),
        key=lambda kv: (-kv[1][0].mtime.timestamp(), kv[0]),
    )
