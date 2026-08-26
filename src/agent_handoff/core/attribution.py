#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这个会话到底在哪个仓库工作。

为什么需要一个独立模块：`SessionRow.repo` 回答的是「会话在哪启动」——它取
`cwd` 再往上找 `.git`，找不到才退回「正文里提到过的第一个仓库」。那个口径在
很多情况下是对的，但它回答不了用户真正在问的问题。

实测的分歧有多大（本机数据，`~/.claude/projects` 与 `~/.codex/sessions`）：

  · Claude 侧 12 份有文件写入证据的转录里，「cwd 推出的仓库」与「写得最多的
    仓库」一致的只有 **1 份**。一个具体例子：某会话 614 次工具调用，其中
    **258 次文件写入全部落在 `agent-handoff-project`，0 次落在 cwd 所在的
    kirara 目录**——卡片上「用这个仓库交接」指向的那个仓库，这个会话从头到尾
    没改过一个字节。
  · Codex 侧更彻底：151 份含 `exec_command.workdir` 的 rollout 里，cwd 推出的
    仓库与「命令跑得最多的仓库」一致的只有 **16 份**。因为 Codex 把 `cwd` 设成
    自己的会话沙箱目录（`~/Documents/Codex/<日期>/<名字>`），那里根本没有
    `.git`，`nearest_repo` 直接返回空，于是归属落到最弱的「提到过」证据上。

所以这里做的是**证据分层**：把转录里所有能回答「在哪工作」的信号按可靠性
排序，取最强的那一层作结论，并把全部候选与命中次数一起交出去，让界面能展开
给人看。结论不可信时如实说不可信，而不是挑一个显示出来。

刻意不改 `SessionRow.repo`：它的语义（启动目录所在仓库）在没有强证据时仍然
正确，而 resume 必须在 cwd 下执行，那个值本身有用。两个口径并存、都显示、
不一致时明说，比替换掉一个更诚实。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..platform import nearest_repo, norm_path
from .workspace import WorkspaceMap

# 归属证据的行预算。放在这个模块而不是 `vitals`：它是收集器自己的参数，
# 而报告、CLI、网页三处都要引用同一个值来说明「只看了前 N 行」。
#
# 为什么不能沿用 `vitals` 里 260/400 那两个身份预算：那两个针对的是「身份卡与
# 开场提到的路径」，它们确实只出现在开头。而**文件写入通常发生在会话中后段**
# ——先读代码、讨论方案，然后才动手改。用 260 行的预算去找写入证据，等于
# 系统性地漏掉这个会话真正在改什么。
#
# 取 1500：实测本机含写入证据的转录里，第一次 Edit 的行号在几十到几百行之间，
# 1500 行能覆盖绝大多数；而它只影响「解析多少行 JSON」这一项成本，且有子串
# 预筛在前，不命中的行零成本。超预算时结论仍然给出，但会标 `truncated`，
# 界面据此说明「只看了前 N 行」——否则「没有写入证据」会被读成「这个会话
# 没改过东西」。
ATTRIBUTION_LINE_BUDGET = 1500

# 证据等级，按可靠性从强到弱。数字小 = 更可靠。
#
# 分级的依据是**语义**而不是命中数量：一次真实的文件写入比一百次「提到路径」
# 更能说明这个会话在改什么。所以排序永远先比等级，同级内才比命中数。
LEVEL_RANK: dict[str, int] = {
    # 上游 harness 自己声明的工作区根。只有 Codex 有（`turn_context.workspace_roots`），
    # 它不是推断出来的，是 harness 写下来的事实。
    "workspace": 0,
    # 改了这个文件：Claude 的 Edit / Write / MultiEdit / NotebookEdit，
    # Codex 的 apply_patch。最强的行为证据。
    "edit": 1,
    # 命令在这个目录里跑（Codex 的 `exec_command.workdir`）。比「看了什么」强：
    # 跑测试、跑 git 都需要在正确的目录里。
    "exec": 2,
    # 看了这个文件：Read / Grep / Glob 的路径参数。会话在读哪个仓库的代码，
    # 是比「提到过」硬得多的信号，但比「改过」弱——读可能只是为了对比参考。
    "read": 3,
    # 启动目录（`cwd` + `nearest_repo`）。现状唯一的依据。
    "cwd": 4,
    # 多根工作区里的 cwd。比普通 cwd 更弱，因为它几乎不携带信息：
    # Claude Code 的 VSCode 扩展在多根工作区下把 `folders` 的**第一个**条目
    # 当作 cwd，其余根转成 `--add-dir`；切换活动编辑器不改它，也没有配置项能
    # 覆盖。所以那个值只说明「哪个文件夹排在第一位」。
    #
    # 排在 `mention` **之前**而不是之后：正文提到的路径可以被一次粘贴污染，
    # 而工作区的根至少是用户自己挑进同一个窗口的。两者都弱，但弱得不一样。
    "workspace_cwd": 5,
    # 正文里提到过的路径。粘一次日志就能污染，最弱。
    "mention": 6,
}
# 结论可以被判为「确定」的等级：行为证据或 harness 声明。
_STRONG_LEVELS = frozenset({"workspace", "edit", "exec"})
# 「读过文件」要有多少次命中，才够资格盖过启动目录成为结论。
#
# 为什么不能一次就算：读别的仓库是常态——对比参考实现、查一份文档、看一眼
# 依赖源码。实测一个讨论 CLIProxyAPI 部署的会话，只因为顺手读了 2 个
# `agent-handoff-project` 的文件，就被判成在改那个仓库，而它整场在讨论
# 另一件事。2 次读取是噪声，不是归属。
#
# 取 5：一个会话真的在某仓库里工作时，读取次数通常是几十上百（实测 61、95、
# 105）；而顺手参考往往只有一两次。5 这条线把两者分开，且宁可保守——判不出
# 来时退回启动目录，界面说清依据是启动目录，比给一个错的结论好。
_READ_MIN = 5
# 第一名要比第二名强多少倍才算「大概是」而不是「说不准」。
#
# 取 3 而不是 2：跨仓库工作（主仓 + 插件仓、前后端分离）是真实场景，两边命中数
# 接近时**不该**假装知道答案。宁可说「说不准」并把候选摊开，也不要给一个
# 看起来确定的错答案——后者会让用户照着它开新会话。
_DOMINANCE = 3
# 每个仓库最多留几个代表文件。够让人认出「哦是那个模块」，又不至于把证据面板
# 撑成文件列表。
_SAMPLES = 3

# 顶层 `"cwd": "..."` 的廉价提取。用正则而不是 `json.loads` 整行：Claude 侧
# 几乎每一行都带 cwd，而这些行大多同时是几十 KB 的消息体，为一个字段解析整个
# 对象是这条路径上最贵的浪费。
#
# 只认转义后的形态（`\\` 与 `\"`），因为原始行就是 JSON 文本。取到之后把
# `\\` 还原成单个反斜杠——Windows 路径在 JSON 里是双写的。
_CWD_RX = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
# 只在行首这一段里找 cwd。
#
# Claude 把 cwd 写在信封层（与 `sessionId`、`type` 同级），所以它总在行首附近：
# 实测 33655 条带 cwd 的行里，中位偏移 1383 字节、p99 是 16102。而单行可以长到
# 3.4 MB（贴了大文件的那种），对整行跑正则等于把最贵的一步付满。
#
# 取 32 KB：覆盖 p99 还有两倍余量，而扫描量对最长的行降低了两个数量级。
# 超出这一段的 cwd 会被漏掉，但那种行的 cwd 与前后行几乎必然相同——cwd 是
# 逐行重复写的，漏掉一次不影响按次数排序的结论。
_CWD_HEAD = 32768

# 会**改动**文件的工具名（Claude Code）。
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# 会**读取**文件的工具名（Claude Code）。Bash 不在里面：它的参数是命令行，
# 里面的路径要靠猜，而猜错会把弱证据混进强证据层。
_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "NotebookRead"})
# 工具参数里承载路径的键。按出现概率排，命中即停。
_PATH_KEYS = ("file_path", "notebook_path", "path", "pattern")


@dataclass
class RepoEvidence:
    """一个候选仓库，以及支持它的证据。"""

    repo: str          # `norm_path` 规范化后的路径，用于比较与去重
    display: str       # 第一次见到时的原始写法，用于显示
    level: str         # LEVEL_RANK 里的键
    hits: int = 0      # 这一层里命中了多少次
    samples: list[str] = field(default_factory=list)  # 代表文件，未脱敏（出口再脱）

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.display,
            "level": self.level,
            "hits": self.hits,
            "samples": list(self.samples),
        }


@dataclass
class RepoVerdict:
    """「这个会话在哪个仓库工作」的结论与它的全部依据。"""

    primary: str = ""            # 结论（原始写法，可直接显示或拼命令）
    confidence: str = "none"     # certain | likely | weak | none
    basis: str = ""              # 结论来自哪一层（LEVEL_RANK 的键）
    evidence: list[RepoEvidence] = field(default_factory=list)
    # `cwd` 推出的仓库与结论不一致。为真时界面**必须同时显示两者**：
    # 结论回答「改了什么」，cwd 回答「在哪能 resume」，两个都要用。
    conflict: bool = False
    # 证据采集在预算内没读完整份转录。为真时结论仍然可用，但界面要说明
    # 「只看了前 N 行」——否则「没有写入证据」会被读成「这个会话没改过东西」。
    truncated: bool = False
    # 这份转录里的 `cwd` 指向过**多个不同仓库**。
    #
    # 为什么值得单独说：`cwd` 是逐行写的运行时快照，会话中途可以换目录
    # （`/cd`、多根工作区、shell 里 `cd` 过去之后的 hook 记录）。实测 66 份
    # 含 cwd 的转录里 7 份真的横跨多个目录。这一位为真时，「启动目录」这个
    # 说法本身就不准确——界面该说的是「这个会话待过多个目录」。
    cwd_moved: bool = False
    # `cwd` 落在某个**多根工作区**的一个根上。
    #
    # 为真时 `cwd` 几乎不携带「在改哪个仓库」的信息：Claude Code 的 VSCode
    # 扩展在多根工作区里把 `workspaceFolders[0]`（`.code-workspace` 里
    # `folders` 的第一个条目）当作 cwd，其余根转成 `--add-dir`。切换活动编辑器
    # 不改它，也没有任何配置项能覆盖。所以那个值只说明「哪个文件夹排在第一位」，
    # 与用户在改什么无关。
    #
    # 这一位不改变结论，只改变**说法**：界面把 cwd 那一行标成「工作区的一个根」
    # 而不是「启动目录」，并把同工作区的其他根列出来当候选。
    cwd_in_workspace: bool = False
    # 与 cwd 同处一个工作区的其他仓库。用户可能实际在改的地方。
    workspace_siblings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "confidence": self.confidence,
            "basis": self.basis,
            "conflict": self.conflict,
            "truncated": self.truncated,
            "cwd_moved": self.cwd_moved,
            "cwd_in_workspace": self.cwd_in_workspace,
            "workspace_siblings": list(self.workspace_siblings),
            "evidence": [e.to_dict() for e in self.evidence],
        }


def _add(bucket: dict[tuple[str, str], RepoEvidence], level: str, raw_path: str) -> None:
    """把一条证据记进桶里。`raw_path` 是文件或目录路径，不必存在。

    仓库解析走 `nearest_repo`（往上找 `.git`），所以传文件路径也可以——它会
    自己走到仓库根。解析不出仓库的路径直接丢：那说明它不在任何 git 仓库里，
    对「在哪个仓库工作」这个问题没有意义。
    """
    if not raw_path:
        return
    repo = nearest_repo(raw_path)
    if not repo:
        return
    key = (level, norm_path(repo))
    ev = bucket.get(key)
    if ev is None:
        # 第一次见到时记下原始写法。同一个仓库在转录里可能有 `e:\` 与 `E:\`
        # 两种写法（Windows 盘符大小写），`norm_path` 保证它们进同一个桶，
        # 而显示用第一次那个——凭空改写用户的路径大小写只会让人怀疑读错了。
        ev = RepoEvidence(repo=norm_path(repo), display=repo, level=level)
        bucket[key] = ev
    ev.hits += 1
    if len(ev.samples) < _SAMPLES and raw_path not in ev.samples:
        ev.samples.append(raw_path)


def _cwd_level(repo: str, workspaces: WorkspaceMap | None) -> str:
    """这个 cwd 该算哪一级证据。

    只有多根工作区的**第一个**根才降到 `workspace_cwd`：扩展固定取
    `workspaceFolders[0]` 作 cwd，切换活动编辑器不改、也没有配置项能覆盖，
    所以那种值只说明「哪个文件夹排在 `folders` 第一位」。

    排在后面的根不降权。它们属于同一个工作区，但那个窗口的 cwd 不会是它们——
    所以当 cwd 恰好等于某个靠后的根时，这个会话一定来自别的（单根）窗口，
    那里的 cwd 是正常可信的。
    """
    if workspaces is not None and workspaces.is_workspace_cwd(repo):
        return "workspace_cwd"
    return "cwd"


def _tool_paths(inp: Any) -> list[str]:
    """从工具入参里取出路径。取不到返回空列表。

    只认已知的键名，不遍历所有值去猜哪个像路径：猜错会把命令行片段、正则、
    甚至 diff 正文当成路径塞进强证据层，而强证据层的可信度是这套分层的全部
    价值所在。
    """
    if not isinstance(inp, dict):
        return []
    out: list[str] = []
    for k in _PATH_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


class AttributionCollector:
    """单遍扫描时顺带收集归属证据。

    与 `_Extractor` 分开：那个类的职责是「身份卡 + 提到过的仓库」，且它在拿全
    身份后就被丢弃以省下 JSON 解析。归属证据恰恰主要出现在**会话中后段**
    （写文件通常发生在读完代码之后），所以它需要自己的、更长的行预算。

    预筛全部是子串查找。不命中的行零成本跳过——多 MB 转录里绝大多数行既没有
    工具调用也没有工作区声明。
    """

    __slots__ = (
        "agent", "_bucket", "_lineno", "_budget", "truncated",
        "_meta_cwd", "_turn_cwd", "_cwds",
    )

    def __init__(self, agent: str, budget: int) -> None:
        self.agent = agent
        self._bucket: dict[tuple[str, str], RepoEvidence] = {}
        self._lineno = 0
        self._budget = budget
        self.truncated = False
        self._meta_cwd = ""
        self._turn_cwd = ""
        # 这份转录里出现过的**全部** cwd 取值，按 norm_path 去重后计数。
        #
        # 为什么要收集全部而不只取第一个：`cwd` 在 Claude 的转录里是**逐行**
        # 写的运行时快照，不是会话级常量。实测本机 66 份含 cwd 的转录中，
        # 22 份出现过多个取值，其中 7 份是**真正不同的目录**（另外 15 份只差
        # 盘符大小写）。最极端的一份在 `C:\Users\devin` 与 9 个 temp 子目录
        # 之间来回；本会话的那份在 kirara 与 agent-handoff-project 之间切换
        # 19 次。
        #
        # 只取第一个（`_Extractor` 的做法）拿到的是「会话启动时那一刻」的值，
        # 那在多根工作区里等于 VSCode 的 `folders[0]`——与用户实际在改哪个
        # 项目无关。所以这里按出现次数排序：一个会话在哪个目录里待得最久，
        # 比它启动时在哪更能说明问题。
        self._cwds: dict[str, tuple[str, int]] = {}

    def feed(self, raw: str) -> None:
        self._lineno += 1
        over = self._lineno > self._budget
        if over and not self.truncated:
            # 只在第一次越界时记一笔。
            self.truncated = True

        # cwd 单独处理，且**不受行预算约束**。
        #
        # 两个理由。一是它便宜：只做一次子串查找加一次限长正则，不解析 JSON。
        # 实测 6.5 MB / 2086 行的转录，全量扫完 43 毫秒，而只扫前 1500 行是
        # 33 毫秒——多出来的 10 毫秒买到的是完整答案。
        #
        # 二是不全扫会给出**错的结论**而不只是不完整的结论：会话中途换目录发生
        # 在后段（先在启动目录里干活、后来才 cd 过去），预算截断恰好把那部分
        # 切掉。实测本会话在 1500 行预算下 `cwd_moved` 判 False，放开后判 True
        # ——前者等于告诉用户「这个会话一直在同一个目录」，而事实相反。
        #
        # 工具证据仍然受预算约束：那些要 json.loads 整行，是另一个量级的成本。
        if self.agent != "Codex" and '"cwd"' in raw:
            m = _CWD_RX.search(raw, 0, _CWD_HEAD)
            if m:
                self._note_cwd(m.group(1).replace("\\\\", "\\"))

        if over:
            return

        # 预筛：这一行有没有可能带证据。三类标记分别对应三种记录形状。
        if self.agent == "Codex":
            # 这里**不能**给键名加引号。Codex 的 `arguments` 是一个 JSON
            # **字符串**，它内部的引号在外层是转义的——`workdir` 在原始行里
            # 长成 `\"workdir\"`，找 `"workdir"` 一次也命中不了，整条 exec
            # 证据会全部静默丢掉（实测：151 份含 workdir 的 rollout 一个都没被
            # 采集到）。用裸词匹配，代价是偶尔多解析一行，那比漏掉一整级证据
            # 便宜得多。
            #
            # `cwd` 也要在这里放行：Codex 的逐轮目录写在 `turn_context.cwd`，
            # 而那种记录可以既没有 `workspace_roots` 也没有 `workdir`。漏掉它
            # 等于让「换过目录」在 Codex 侧永远判不出来。
            if not (
                "workspace_roots" in raw
                or "workdir" in raw
                or "apply_patch" in raw
                or '"cwd"' in raw
            ):
                return
        else:
            # Claude 侧的 `input` 是**对象**而不是字符串，键名在原始行里就是
            # 带引号的形态，所以这里可以带引号——多一个引号少一次误命中。
            if '"tool_use"' not in raw:
                return
            if not any(k in raw for k in ('"file_path"', '"notebook_path"', '"path"')):
                return

        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(d, dict):
            return

        if self.agent == "Codex":
            self._feed_codex(d)
        else:
            self._feed_claude(d)

    def _feed_claude(self, d: dict[str, Any]) -> None:
        """Claude Code：证据在 `message.content[]` 的 `tool_use` 块里。"""
        msg = d.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for blk in content:
            if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                continue
            name = str(blk.get("name") or "")
            if name in _WRITE_TOOLS:
                level = "edit"
            elif name in _READ_TOOLS:
                level = "read"
            else:
                # MCP 工具与 Bash 不参与：前者的参数结构各服务器不同，后者的
                # 路径埋在命令行里要靠猜。宁可少一条证据，不要往强证据层里
                # 掺猜出来的东西。
                continue
            for p in _tool_paths(blk.get("input")):
                _add(self._bucket, level, p)

    def _note_cwd(self, raw_cwd: str) -> None:
        """记一次 cwd 出现。按 `norm_path` 归并，保留第一次见到的原始写法。

        为什么要计数而不是只记「见过」：一份转录里的 cwd 取值分布本身就是证据。
        实测本会话在两个目录之间切换 19 次，其中 1417 行指向启动目录、93 行指向
        真正在改的项目——但**另一份**转录可能恰好相反。用次数排序让数据自己说话，
        不预设「第一个就是对的」。
        """
        cwd = raw_cwd.strip()
        if not cwd:
            return
        key = norm_path(cwd)
        if not key:
            return
        display, n = self._cwds.get(key, (cwd, 0))
        self._cwds[key] = (display, n + 1)

    def _feed_codex(self, d: dict[str, Any]) -> None:
        """Codex：三处证据，可靠性依次下降。"""
        p = d.get("payload")
        if not isinstance(p, dict):
            return

        # 1. harness 声明的工作区根。逐轮记录，同一份 rollout 里可以变化
        #    （加了目录、切了项目），所以每次都收——最终按命中数排序。
        roots = p.get("workspace_roots")
        if isinstance(roots, list):
            for r in roots:
                if isinstance(r, str) and r.strip():
                    _add(self._bucket, "workspace", r.strip())

        # 2. 逐轮 cwd。它与 `session_meta.cwd` 不同：后者是会话开始时的值，
        #    而 Codex 允许中途换目录。留着但归入 cwd 层。
        if d.get("type") == "turn_context":
            cw = p.get("cwd")
            if isinstance(cw, str) and cw.strip():
                self._turn_cwd = cw.strip()
                self._note_cwd(cw)
                _add(self._bucket, "cwd", cw.strip())
        elif d.get("type") == "session_meta" and not self._meta_cwd:
            cw = p.get("cwd")
            if isinstance(cw, str) and cw.strip():
                self._meta_cwd = cw.strip()

        # 3. 工具调用。`arguments` 是 JSON **字符串**，不是对象。
        name = str(p.get("name") or "")
        args_raw = p.get("arguments")
        if not name or not isinstance(args_raw, str):
            return
        try:
            args = json.loads(args_raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(args, dict):
            return
        if name == "apply_patch":
            # 补丁的目标路径在 `input`/`patch` 正文里，形如 `*** Update File: x`。
            # 只取显式给出的路径参数，不去解析 diff 正文——解析 diff 是另一个
            # 问题，解析错了会把强证据层污染。
            for p2 in _tool_paths(args):
                _add(self._bucket, "edit", p2)
            return
        wd = args.get("workdir")
        if isinstance(wd, str) and wd.strip():
            _add(self._bucket, "exec", wd.strip())

    def verdict(
        self,
        cwd: str = "",
        mentioned: Iterable[str] = (),
        workspaces: WorkspaceMap | None = None,
    ) -> RepoVerdict:
        """把收集到的证据结算成结论。

        `cwd` 与 `mentioned` 由调用方补进来（它们由 `_Extractor` 收集，不重复
        解析）。这两层参与**展示**，也在没有任何强证据时充当结论——那时结论
        标为 `weak`，界面据此说明「依据是启动目录，本次会话未改动任何文件」。

        `workspaces` 是本机的工作区分组。传进来时，落在多根工作区里的 cwd 会被
        降到 `workspace_cwd` 那一级——那种 cwd 只说明「哪个文件夹排在 `folders`
        第一位」，与用户在改什么无关。
        """
        bucket = dict(self._bucket)
        # 自己收集到的全部 cwd 取值都进候选，按出现次数计入命中数。
        #
        # 为什么不只用调用方传进来的那一个：`_Extractor` 取的是**第一个**出现的
        # cwd，而 cwd 是逐行写的运行时快照。多根工作区下第一个值等于 VSCode 的
        # `folders[0]`，与用户在改哪个项目无关。实测本会话的 cwd 在两个目录之间
        # 切换 19 次，只看第一个必然读到启动目录。
        cwd_counts: dict[str, int] = {}
        for key, (display, n) in self._cwds.items():
            repo = nearest_repo(display)
            if not repo:
                continue
            rk = norm_path(repo)
            cwd_counts[rk] = cwd_counts.get(rk, 0) + n
            level = _cwd_level(repo, workspaces)
            k = (level, rk)
            ev = bucket.get(k)
            if ev is None:
                ev = RepoEvidence(repo=rk, display=repo, level=level)
                bucket[k] = ev
            ev.hits += n
        if cwd:
            repo = nearest_repo(cwd)
            if repo and norm_path(repo) not in cwd_counts:
                # 调用方给的 cwd 我们自己没收到过（预算之外、或 Codex 的
                # `session_meta` 路径）。补一条，等级同样按工作区归属决定。
                _add(bucket, _cwd_level(repo, workspaces), cwd)
        for m in mentioned:
            _add(bucket, "mention", m)

        # 排序：先按等级（强的在前），同级按命中数倒序，再按路径保证可复现。
        evidence = sorted(
            bucket.values(),
            key=lambda e: (LEVEL_RANK.get(e.level, 99), -e.hits, e.repo),
        )
        if not evidence:
            return RepoVerdict(truncated=self.truncated)

        # 「读过文件」命中太少时不让它当结论：那更可能是顺手参考而不是在那里
        # 工作。把它降到启动目录之后——证据仍然列出来（用户能看到读过什么），
        # 只是不拿它下结论。
        ranked = [
            e for e in evidence
            if not (e.level == "read" and e.hits < _READ_MIN)
        ]
        if not ranked:
            ranked = [e for e in evidence if e.level != "read"]
        if not ranked:
            # 只有零星的读取证据，且没有任何别的信号。不下结论比下错结论好。
            return RepoVerdict(evidence=evidence, truncated=self.truncated)

        top = ranked[0]
        # 同一层里的第二名（不同仓库）才构成竞争。跨层不比：等级本身已经是
        # 「哪个更可信」的答案。
        peers = [e for e in ranked if e.level == top.level and e.repo != top.repo]
        strong = top.level in _STRONG_LEVELS
        if not peers:
            confidence = "certain" if strong else "weak"
        elif top.hits >= peers[0].hits * _DOMINANCE:
            confidence = "likely" if strong else "weak"
        else:
            # 势均力敌：不假装知道。界面会因此强制摊开候选列表。
            confidence = "weak"

        cwd_repo = norm_path(nearest_repo(cwd)) if cwd else ""
        # cwd 落在多根工作区里时，把同工作区的其他根摆出来当候选。
        #
        # 为什么有用：那种 cwd 只说明「哪个文件夹排在 folders 第一位」，而用户
        # 真正在改的往往是另一个根。没有行为证据时（纯讨论、纯搜索的会话）这份
        # 兄弟清单是唯一能帮用户认出「哦是那个项目」的线索。
        #
        # 探测对象取**证据里那条 `workspace_cwd`**，而不是只看调用方传进来的
        # `cwd`：后者是 `_Extractor` 抓到的第一个值，预算之外或压缩转录里可能
        # 为空，而那时我们自己收集的 cwd 证据仍然在。用前者才不会漏。
        in_ws = False
        siblings: list[str] = []
        if workspaces is not None:
            probe = ""
            for e in evidence:
                if e.level == "workspace_cwd":
                    probe = e.repo
                    break
            if not probe:
                probe = cwd_repo or (top.repo if top.level == "cwd" else "")
            if probe and workspaces.is_multi_root(probe):
                in_ws = True
                siblings = [
                    s for s in workspaces.siblings(probe)
                    # 已经在证据里的就不用再列一遍：那条证据本身信息更全
                    # （带等级与命中数）。
                    if s not in {e.repo for e in evidence}
                ]

        # 多个 cwd 取值指向不同仓库，本身就是一条要说出来的事实：那说明这个
        # 会话在多个项目之间来回，或者它跑在多根工作区里而启动目录不是主战场。
        # 界面据此提示，而不是静默挑一个显示。
        cwd_repos = {
            norm_path(r)
            for r in (nearest_repo(d) for d, _ in self._cwds.values())
            if r
        }
        return RepoVerdict(
            primary=top.display,
            confidence=confidence,
            basis=top.level,
            evidence=evidence,
            conflict=bool(cwd_repo and cwd_repo != top.repo),
            truncated=self.truncated,
            cwd_moved=len(cwd_repos) > 1,
            cwd_in_workspace=in_ws,
            workspace_siblings=siblings,
        )
