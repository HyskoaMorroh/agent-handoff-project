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
import threading
from collections import OrderedDict, deque
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..platform import (
    TranscriptCompressedError,
    agent_evidence,
    agent_session_roots,
    is_foreign_path,
    is_transcript_name,
    iter_path_candidates,
    last_record_time,
    nearest_repo,
    norm_path,
    open_transcript,
)
from .attribution import ATTRIBUTION_LINE_BUDGET, AttributionCollector, RepoVerdict
from .workspace import WorkspaceMap

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
#
# 注意：体积只是**兜底**判据，读不到 token 数时才用。实测体积与上下文占用
# 严重脱钩——1.0 MB 的会话可以已用 194183 token，1.9 MB 的会话可以压缩过
# 10 次。能读到 token 就按 FULLNESS_BANDS / TOKEN_BANDS 判。
VITALS_BANDS = [
    (8_000_000, "critical"),
    (3_000_000, "high"),
    (1_000_000, "watch"),
    (0, "ok"),
]
# 占用率判据。分母来自转录自己写的上下文上限（Codex 的 `model_context_window`）。
# 90% 往上已经没有余量做完一件事就得压缩；75% 是「这次会话别再开新战场」；
# 55% 是「可以开始想交接了」。
FULLNESS_BANDS = [
    (0.90, "critical"),
    (0.75, "high"),
    (0.55, "watch"),
    (0.0, "ok"),
]
# 没有分母时的绝对阈值。按当下常见的 200k 窗口折算 FULLNESS_BANDS 得到，
# 对更大的窗口偏保守——宁可早提醒，也不要漏掉真要满的会话。
#
# 这组阈值是**兜底**，不是判据的正解。任何单一绝对值都不可能同时对 128k 和
# 1M 窗口正确：实测本机一个 Claude 会话 102365 token，按 200k 折算判「健康」，
# 若模型其实是 128k 窗口那已经 80%、该判「尽快交接」；若是 1M 窗口则只有 10%，
# 判「健康」是对的。同一个数字，结论相反。
#
# Codex 在 `token_count` 事件里直接写 `model_context_window`，所以走真实占用率。
# Claude Code 的转录不写窗口上限（实测本机 16 个 Claude 转录全部读不到），
# 只能落到这组绝对值。用 `AGENT_HANDOFF_CONTEXT_WINDOW` 声明真实窗口即可
# 改按占用率判——这比让工具去猜模型型号可靠：模型会换，而用户知道自己在用什么。
TOKEN_BANDS = [
    (180_000, "critical"),
    (150_000, "high"),
    (110_000, "watch"),
    (0, "ok"),
]
# 用户声明的上下文窗口上限，覆盖读不到窗口时的兜底阈值。
# 只接受正整数；写错了当没写，不让一个笔误把整张体征表judge成健康。
_WINDOW_ENV = "AGENT_HANDOFF_CONTEXT_WINDOW"
# 排序用。`unknown` 排在 watch 与 ok 之间：它不是「健康」——正文一行没读到，
# 有可能是最该交接的那个会话；但也不该顶到 critical 前面去挤掉有真实证据的行。
# 位置本身就是「这条需要你自己看一眼」的意思。
BAND_ORDER = {"critical": 0, "high": 1, "watch": 2, "unknown": 3, "ok": 4}

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
#
# 三个键，因为两家 APP 的失败记录形状完全不同：
#   · Claude：tool_result 块上的 `is_error`
#   · Codex MCP 成功调用但工具自己报错：`result.Ok.isError`
#   · Codex MCP 调用本身失败（服务器没起来、超时）：`result.Err`
# 实测最近 10 份 rollout：按结构判定 64 条错误，`isError` 命中 50 条、
# `Err` 命中 14 条——两者相加正好 64，一条不漏也不重。缺 `Err` 那条的后果是
# 「MCP 服务器整个连不上」这类最严重的失败一次都不计数。
#
# 每个键给紧凑与带空格两种写法。**实测本机的转录全是紧凑的**（Claude 与 Codex
# 都用紧凑分隔符），但只认紧凑形态是在赌序列化细节永远不变——而 `json.dumps`
# 的默认输出恰恰是带空格的，任何一次上游改写工具链就会让整列错误静默归零。
# 多几次子串查找的代价可以忽略，漏计一整类失败的代价不行。
#
# 不用宽松匹配（只找键名）：`is_error` 在真实转录里出现 155 次，而
# `"is_error":true` 只有 13 次——多出来的都是人和模型在**讨论**这个字段
# （代码片段、日志摘录）。宽松匹配等于把 JSON 解析次数放大十倍，
# 而预筛存在的全部理由就是避免这个。
_ERR_MARKS = (
    '"is_error":true', '"is_error": true',
    '"isError":true', '"isError": true',
    '"result":{"Err"', '"result": {"Err"',
)

# 保留几条错误原文，每条多长。
#
# 为什么要留原文：原版只数个数（`errors` 计数），于是交接文档里写着「工具错误
# 10 次」——这条信息没有任何行动价值。新会话不知道错在哪、也不知道试过什么，
# 于是按同样的方式再错一遍。而报错原文里有路径、有行号、有「为什么不行」，
# 那正是「已经排除的方案」这类最贵的上下文。
#
# 只留最后 3 条：早期的错误往往已经被修掉了（会话继续下去就是在修它），
# 而**最后几条是还没解决的**——那才是交给下一个会话的东西。
_ERR_KEEP = 3
_ERR_CHARS = 600

# 上下文占用与压缩事件的预筛标记。两家的写法完全不同：
#   · Claude Code：assistant 消息里的 `message.usage`，字段
#     `input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`。
#     实测本会话末条 input_tokens=194183，而文件只有 1.0 MB——这就是体积判据
#     失准的直接证据。转录里**没有**上下文上限，算不出真占用率。
#   · Codex：`type:"event_msg"` + `payload.type:"token_count"`，
#     `payload.info.last_token_usage.total_tokens` 是占用，
#     `payload.info.model_context_window` 是上限（实测 121600）——两者都有，
#     占用率可以直接算，不必猜阈值。取 total_tokens 而不是 input_tokens 是为了
#     跟 Codex 自己的 `tokens_in_context_window()` 对齐，见 `_feed_usage` 注释。
# 压缩事件同样两家不同：Claude 是 `type:"system"` + `subtype:"compact_boundary"`，
# 带 `compactMetadata.preTokens`（压缩发生时的占用，属于历史事实）；
# Codex 是 `type:"compacted"` 记录。
_USAGE_MARKS = ('"usage"', '"token_count"')
_COMPACT_MARKS = ('"compact_boundary"', '"compacted"')
# 逐轮上下文的预筛。
#
# 为什么需要它：Codex 的 model / approval_policy / sandbox_policy / cwd **是逐轮
# 的**（写在 `turn_context` 记录里），不在 `session_meta` 里。只读 session_meta
# 的后果是这几项永远为空——而「上一个会话用的是哪个模型、在什么沙箱策略下跑」
# 恰恰决定了新会话能不能重现它的结果。同一份 rollout 里模型换过是常态
# （/model 切换、供应商降级）。
#
# 取**最后一条**而不是第一条：交接要交代的是「停下来时是什么状态」。
_TURNCTX_MARK = '"turn_context"'

# 时间线最多留多少个采样点。
#
# 120 是按显示宽度定的：卡片里的时间线宽约 600 CSS 像素，再多的点在屏幕上
# 挤成一团，抽稀反而让线更好读。而画一条趋势线用不着更高的分辨率——这条线
# 要回答的是「怎么涨的」，不是「第 837 轮精确是多少」。
_TIMELINE_MAX = 120


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


def band_for(
    size: int,
    fatal: int = 0,
    aborted: int = 0,
    tokens: int = 0,
    window: int = 0,
    compactions: int = 0,
    unknown: bool = False,
) -> str:
    """风险区间。有 token 数就按占用率判，没有才退回体积。

    为什么不能只看体积：体积和上下文占用严重脱钩。实测本机四个转录——
      · 1.0 MB 的会话已用 194183 token（接近满），体积判据说「健康」
      · 1.9 MB 的会话**自动压缩过 10 次**，体积判据说「留意」
      · 27.4 MB 的会话峰值 710340 token，体积判据碰巧说对了
    体积小恰恰可能因为压缩一直在丢历史——越小丢得越多。而 token 数就在转录里
    明写着（Claude 的 `message.usage`、Codex 的 `token_count` 事件），
    没有理由去猜。

    `window` 是模型的上下文上限。Codex 在 `model_context_window` 里直接给出；
    Claude 转录里没有，此时用占用量本身对照保守阈值——那比体积仍然准得多。

    `compactions` 是已发生的压缩次数，属于**历史事实**而不是推测：压缩过就说明
    上下文真的满过——自动压缩只在快装不下时才触发。所以压缩过的会话直接按
    「满过」对待，而不是拿压缩前的占用去对照阈值：那个数字看着可能只有 167k，
    但它恰恰是触发压缩的那一刻，等于 100%。

    fatal/aborted 的抬升逻辑不变：它们回答「已经出事了没有」，与「还能撑多久」
    是两件事。

    `unknown` 为真时（压缩归档且本机没有 zstd 实现，正文一行都没读到）直接
    返回 `unknown` 而不是走体积兜底。理由是压缩后的体积与阈值不可比：zstd
    level 3 对 JSONL 通常能压到十分之一上下，于是一个 27 MB 的爆满会话在盘上
    只有 2 MB 多，体积判据会说「留意」甚至「健康」——正好把最该交接的会话
    判成最不需要交接的。宁可承认不知道。
    """
    if unknown:
        return "unknown"

    band = _band_by_fullness(tokens, window) if tokens else _band_by_size(size)

    if compactions:
        # 压缩发生过，就说明上下文真的顶到过上限——自动压缩不会在有余量时触发。
        # 压缩一次已经丢过一轮细节，两次以上就是反复丢：这类会话看着体积不大
        # （压缩本身在缩小它），实际最该交接。实测一个 1.9 MB 的会话压了 10 次。
        #
        # 为什么是 2 而不是官方熔断用的 3：两者目的不同。Claude Code 用「连续
        # 3 次 auto-compact」判定 thrash loop 并**停止继续压缩**——那是保护
        # 运行时不要空转。这里判定的是「该不该交接」，只要丢过两轮历史就已经
        # 值得换个干净会话，不必等到运行时都认为出了问题。
        #
        # 实测本机 42 个转录支持这个取值：压缩 1 次的那几个会话，占用率是
        # 98%–108%（131164/129437/118624 token 对 121600 窗口），压缩 2 次的是
        # 96%–123%——没有一个属于「压缩次数低但余量健康」。抬升在这批数据上
        # 从不产生假警报，因为触发压缩本身就意味着当时装不下了。
        floor = "critical" if compactions >= 2 else "high"
        if BAND_ORDER[floor] < BAND_ORDER[band]:
            band = floor

    if fatal or aborted:
        # 已经出过致命错误或被打断：至少「留意」，出过多次直接升到「尽快交接」。
        floor = "high" if (fatal + aborted) >= 3 else "watch"
        if BAND_ORDER[floor] < BAND_ORDER[band]:
            band = floor
    return band


def _band_by_size(size: int) -> str:
    """体积兜底判据。只在转录里读不到 token 数时使用。"""
    for threshold, name in VITALS_BANDS:
        if size >= threshold:
            return name
    return "ok"


def _declared_window() -> int:
    """用户声明的上下文窗口上限，读不到或不合法时返回 0。

    为什么需要它：Claude Code 的转录不写窗口上限（实测本机 16 个 Claude 转录
    全部读不到），于是只能对照按 200k 折算的绝对阈值。而窗口大小在 128k 到 1M
    之间跨了将近一个数量级——同一个 token 数在两端得到相反的结论。

    官方自己也在这上面反复出过 bug：把 1M 窗口的模型按 200k 计算，会在远未装满
    时就触发自动压缩。既然工具猜不准、官方也未公布固定阈值，就把这个事实交给
    知道答案的人——用户清楚自己在用哪个模型。

    不合法的值（负数、零、非数字）按「没声明」处理：一个笔误不该把整张体征表
    judge 成健康，那正是这个工具存在的意义所反对的。
    """
    raw = os.environ.get(_WINDOW_ENV, "").strip()
    if not raw:
        return 0
    try:
        val = int(raw)
    except ValueError:
        return 0
    return val if val > 0 else 0


def _band_by_fullness(tokens: int, window: int) -> str:
    """按上下文占用率判定。

    有上限就算真占用率。上限有两个来源，按可信度排序：
      1. 转录自己写的（Codex 的 `model_context_window`）——实测值，最可信
      2. `AGENT_HANDOFF_CONTEXT_WINDOW` 环境变量——用户声明的，用于 Claude
         转录这种读不到上限的情形

    两者都没有时才退回 `TOKEN_BANDS` 的绝对阈值。那些阈值按当下常见的 200k
    窗口取，对更大的窗口偏保守，宁可早提醒也不要漏掉真要满的会话。
    """
    limit = window if window > 0 else _declared_window()
    if limit > 0:
        ratio = tokens / limit
        for threshold, name in FULLNESS_BANDS:
            if ratio >= threshold:
                return name
        return "ok"
    for threshold, name in TOKEN_BANDS:
        if tokens >= threshold:
            return name
    return "ok"


def band_reason(
    size: int,
    fatal: int = 0,
    aborted: int = 0,
    tokens: int = 0,
    window: int = 0,
    compactions: int = 0,
    unknown: bool = False,
) -> dict[str, Any]:
    """`band_for` 判成这一档的**依据**，拆成结构化事实。

    为什么必须有这个：判定逻辑里有两条**抬档地板**——压缩过 2 次直接抬到
    `critical`、致命错误加打断合计 ≥3 抬到 `high`。抬档之后界面上只剩一个
    「立刻交接」的红徽章，而占用率那一格可能显示 62%。用户看到的是自相矛盾的
    两个数字，无从判断工具是算错了还是有别的道理。实测本机一个 1.9 MB 的会话
    压缩过 10 次、占用率读出来只有中位——正是这种情形。

    返回的是**事实而不是句子**：`basis` 说明主判据来自哪里（占用率／绝对
    token／体积／读不到），`floors` 列出实际生效的抬档及其原因。文案由 i18n
    负责，这样三种语言共用同一份判定证据，也让「为什么是这一档」可以进
    交接文档、进网页界面、进 JSON 输出，三处说法完全一致。

    `raised` 为真表示最终档位来自抬档而不是主判据——这一条单独给出来，
    因为它正是「数字看着还行却被判成危险」的唯一原因。
    """
    if unknown:
        return {
            "band": "unknown",
            "basis": "unreadable",
            "detail": {},
            "floors": [],
            "raised": False,
        }

    limit = window if window > 0 else _declared_window()
    if tokens and limit > 0:
        basis = "fullness"
        detail: dict[str, Any] = {
            "tokens": tokens,
            "window": limit,
            "percent": round(tokens / limit * 100, 1),
            # 上限是转录自己写的还是用户声明的。这一点影响可信度：前者是实测值，
            # 后者是声明值，声明错了整列占用率都会偏。
            "window_from": "transcript" if window > 0 else "declared",
        }
    elif tokens:
        basis = "tokens"
        detail = {"tokens": tokens}
    else:
        basis = "size"
        detail = {"size": size}

    base = _band_by_fullness(tokens, window) if tokens else _band_by_size(size)
    band = base
    floors: list[dict[str, Any]] = []

    if compactions:
        floor = "critical" if compactions >= 2 else "high"
        applied = BAND_ORDER[floor] < BAND_ORDER[band]
        floors.append({"kind": "compactions", "count": compactions, "floor": floor, "applied": applied})
        if applied:
            band = floor

    if fatal or aborted:
        floor = "high" if (fatal + aborted) >= 3 else "watch"
        applied = BAND_ORDER[floor] < BAND_ORDER[band]
        floors.append({
            "kind": "incidents",
            "fatal": fatal,
            "aborted": aborted,
            "floor": floor,
            "applied": applied,
        })
        if applied:
            band = floor

    return {
        "band": band,
        "basis": basis,
        "detail": detail,
        "floors": floors,
        "raised": band != base,
    }


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
    # 上下文占用。0 表示这份转录里读不到 token 数（此时判定退回体积）。
    # 取峰值而非末值：末条 assistant 可能来自子代理或没有 cache 记账，
    # 实测两个转录的末值都是 0。
    tokens: int = 0
    # 模型的上下文上限。Codex 在 `model_context_window` 里直接给出；
    # Claude 转录里没有，为 0 时按绝对阈值判而不是算占用率。
    context_window: int = 0
    # 已发生的压缩次数。压缩过就说明上下文真的满过——这是历史事实，
    # 比任何体积推断都硬。实测一个 1.9 MB 的会话压缩过 10 次。
    compactions: int = 0
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
    # 最近几条工具报错的原文。个数（`errors`）回答「出过多少次事」，
    # 原文回答「错在哪、试过什么不行」——后者才是下一个会话能用上的。
    errors_text: list[str] = field(default_factory=list)
    # 会话**停下来时**的模型与策略（Codex 的最后一条 `turn_context`）。
    # 这几项是逐轮的，不在 session_meta 里——同一份 rollout 里模型换过是常态
    # （/model 切换、供应商降级）。新会话要重现上一个会话的结果，得知道它
    # 当时在什么配置下跑。Claude 侧不写这种记录，为空。
    turn_context: dict[str, str] = field(default_factory=dict)
    # 上下文占用随轮次的走势 + 压缩发生的位置。空字典表示采样点不足两个
    # （读不到 token，或者会话太短），此时界面不画线——一个点画出来会被
    # 读成「一直是这个值」。
    #
    # 为什么峰值不够：同样是 97% 占用，一路平稳爬上去和最后两轮突然翻倍是
    # 两种处境，后者说明刚才那一步塞进了巨量内容，而那正是下一个会话要避开的。
    timeline: dict[str, Any] = field(default_factory=dict)
    is_subagent: bool = False
    repos: list[str] = field(default_factory=list)
    # 这份转录是 Codex 压缩归档的（`.jsonl.zst`，7 天后自动压缩），而本机没有
    # 可用的 zstd 实现，所以正文一行都没读到。会话仍然列出来——用户至少知道
    # 那里有东西；但所有来自正文的字段（体征、摘要、用户原话）都是空的，
    # 界面必须把这一点说清楚，不能让人误以为「这个会话很干净」。
    compressed_unreadable: bool = False
    # 转录里**最后一条记录**写下的时间。0 表示没取到（此时 `active_at` 退回
    # `mtime`）。与 `mtime` 并存而不是替换它：`mtime` 的语义是「文件什么时候
    # 被改的」，那在磁盘清理（`--sweep`）里仍然是对的口径。
    last_active_ts: float = 0.0
    # 上面那个时间从哪来。界面要如实说出是哪一种，因为可信度不同：
    #   · `record`            —— 转录正文里读到的，可信
    #   · `mtime`             —— 尾部没找到时间戳，回落文件时间
    #   · `mtime-compressed`  —— 压缩转录不能 seek 尾部，直接回落
    time_source: str = "mtime"
    # 「这个会话在哪个仓库工作」的结论与全部依据。空的 verdict（`primary` 为
    # 空串）表示一条证据都没收集到——那与「结论是 cwd」不同，界面要区分。
    #
    # 与 `repo` 并存而不是替换它：`repo` 回答「在哪启动」，那个值本身有用
    # （resume 必须在 cwd 下执行）。这里回答「在改什么」。两者不一致是常态，
    # 明说比挑一个显示更诚实。
    verdict: RepoVerdict = field(default_factory=RepoVerdict)

    @property
    def work_repo(self) -> str:
        """这个会话**实际在改**的仓库。没有证据时退回 `repo`。

        为什么需要它：`repo` 取 `cwd` 再往上找 `.git`，而 cwd 是「在哪启动」，
        不是「在改什么」。实测本机 12 份有文件写入证据的 Claude 转录，两者一致
        的只有 1 份；Codex 侧 151 份含 `workdir` 的 rollout 里一致的只有 16 份
        （它的 cwd 是会话沙箱目录，根本不在任何 git 仓库里）。

        具体到一个例子：某会话 614 次工具调用，258 次文件写入**全部**落在
        `agent-handoff-project`，而 cwd 指向 kirara 目录——卡片上「用这个仓库
        交接」指向的仓库，这个会话从头到尾没改过一个字节。

        没有证据时退回 `repo` 而不是留空：纯讨论、纯搜索、早期被打断的会话确实
        没有文件证据，那时启动目录是唯一线索，仍然比空白有用。`verdict` 的
        `confidence` 会说明这一点。
        """
        return self.verdict.primary or self.repo

    @property
    def active_at(self) -> datetime:
        """这个会话**最后一次真的在动**是什么时候。

        为什么不能直接用 `mtime`：文件时间与最后一条记录大面积脱钩，且两个
        方向都会错。本机实测——

          · Claude 侧 63 份转录：22 份偏差超过 60 秒，最差约 2.8 天。子代理
            边车写入、云同步、备份程序都会把 mtime 往后推。
          · Codex 侧 324 份 rollout：287 份的 mtime **早于**最后一条记录，
            而其中 268 份的 mtime 与文件名里的**开始**时间相差不到 2 秒——
            Codex 的 mtime 实质是创建时刻，跟最后活动无关。

        排序也受影响：492 份会话按两种口径排，位次相同的只有 122 份，最大
        偏移 93 位。「最近那个」几乎总是要找的那个，排错了就等于找不到。

        取不到记录时间时退回 `mtime` 而不是留空：一个不准的时间仍然比没有
        时间有用，但 `time_source` 会说清楚它不准。
        """
        if self.last_active_ts > 0:
            try:
                return datetime.fromtimestamp(self.last_active_ts)
            except (OSError, OverflowError, ValueError):
                return self.mtime
        return self.mtime

    @property
    def repo(self) -> str:
        """这个会话**在哪个仓库里工作**。

        `repos` 是从会话正文里捞出来的全部仓库路径，一个会话提到别的项目是常态
        （审计外部参考、对比两个仓库、把路径粘进来问问题）。取第一个作为归属
        会出错：实测一个在 `agent-handoff-project` 里跑的会话，因为正文大量提到
        `E:/output/kirara-ai/kirara-ai3.3.0b8`，被判成了后者——**用户看到的归属
        与他实际在哪工作完全对不上**，那让整张表失去意义。

        所以优先用 `cwd`：它是会话开始时 shell 的实际工作目录，由 harness 写进
        转录（Claude 的 `cwd` 字段、Codex 的 `session_meta.cwd`），是「在哪工作」
        的直接证据，而正文里的路径只是「提到过」。

        `cwd` 可能比仓库根更深（在子目录里启动），所以要往上找到 `.git`；
        找不到时才退回 `repos` 的第一个——那至少是个经 `nearest_repo` 验证过的
        真实仓库，比空白有用。
        """
        if self.cwd:
            here = nearest_repo(self.cwd)
            if here:
                return here
        return self.repos[0] if self.repos else ""

    @property
    def is_foreign(self) -> bool:
        """这份转录是从另一台电脑搬过来的吗？

        判据是它记录的工作目录像不像本机的——不猜用户名，靠
        `platform.is_foreign_path` 的结构判断。cwd 为空时无从判断，
        当作本机（宁可少提示，也不要对着正常会话喊「这是外来的」）。

        为什么要区分：搬过来的转录里，路径、原生续接、仓库推断全都失效，
        但**内容仍然有价值**——那正是迁机时最想带走的东西。所以不是丢弃它，
        而是把哪些字段还能信、哪些不能，明确告诉用户。
        """
        return is_foreign_path(self.cwd)

    @property
    def resume_cmd(self) -> str:
        """在原生 app 里继续这个会话的命令。

        为什么值得给出：交接是有损的（工具授权、后台进程、被否决方案的推理都
        传不过去），所以只要原生续接还可行，它就严格优于交接。把命令摆在卡片上
        等于先给出更好的那个选项，而不是把用户往交接上推。

        Codex 的会话 ID 是 8-4-4-4-12 的 UUID，而 rollout 文件名前缀自带很多
        连字符（`rollout-2026-08-22T06-09-26-<uuid>`）。取尾部五段比写正则稳：
        无论前缀怎么变，UUID 总是最后五段。
        （这个取法来自 codex-history-vscode 的 getResumeCommand。）

        归档过的 Codex 会话不能续接——Codex 只在活动目录里找——所以那种情况
        返回空字符串，不给一条注定失败的命令。

        从别的电脑搬过来的转录同理：Claude / Codex 都按自己的数据目录建索引，
        拷进来的 jsonl 不在索引里，命令一定报「找不到会话」。同一条原则，
        同一种处理——不给注定失败的命令。
        """
        sid = (self.session_id or "").strip()
        if not sid:
            return ""
        if self.is_foreign:
            return ""
        if self.agent == "Codex":
            if "archived_sessions" in {p.lower() for p in self.path.parts}:
                return ""
            parts = sid.split("-")
            uuid = "-".join(parts[-5:]) if len(parts) >= 5 else sid
            return f"codex resume {uuid}"
        return f"claude --resume {sid}"

    @property
    def resume_cmd_cd(self) -> str:
        """续接命令，**带上切到启动目录那一步**。没有可用命令或路径时返回空串。

        为什么必需：`claude --resume <id>` / `codex resume <id>` 只在**启动目录**
        下才能找到那个会话——两个 APP 都按目录给会话建索引。而卡片上另一行显示的
        是「在改哪个仓库」，那两个路径经常不同（实测本机三个会话全部不同）。
        用户复制了纯命令、在当前目录粘贴执行，得到的是「找不到会话」。

        用 `pushd` 而不是 `cd`：cmd 的 `cd` 换目录但**不换盘符**。在 C: 上执行
        `cd "E:\\proj"`，提示符看着变了，实际工作目录还在 C:，紧跟的 resume 于是
        跑在错的项目里——这比直接报错更糟，因为它看起来成功了。`pushd` 两者都换，
        在 PowerShell 里也是 `Push-Location` 的别名，一条命令覆盖三种壳。
        （这条 Windows 细节来自 claude-code-log 的 resume 命令拼装，它在注释里
        记下了同一个坑。）

        路径里带换行时**不给命令**：多行内容粘进终端会被逐行立即执行，引号包不住
        换行。这种情况下宁可不给，也不能给一条会执行意料之外内容的命令行。

        POSIX 路径不需要换盘符，但 `pushd` 在 sh / bash / zsh 里同样存在且语义
        一致（压栈 + cd），所以两边共用一条形态，不为平台分叉。
        """
        cmd = self.resume_cmd
        if not cmd:
            return ""
        where = self.cwd.strip()
        if not where or "\n" in where or "\r" in where:
            return ""
        # 双引号里的反斜杠在 cmd 与 PowerShell 里都是字面量，Windows 路径可以
        # 原样放进去。路径自带双引号时不处理——那种路径在任何壳里都需要另一套
        # 转义，而实测从未出现，编一套没验证过的转义比不给更危险。
        if '"' in where:
            return ""
        return f'pushd "{where}" && {cmd}'

    @property
    def deep_link(self) -> str:
        """能直接唤起 APP 回到这条线程的链接。没有就返回空串。

        只有 Codex 注册了 URI scheme（`codex://threads/<id>`，见它自己的桌面端
        集成代码）。Claude Code 没有，所以那边永远返回空——编一个不存在的
        `claude://…` 会让用户点了没反应，比不给更糟：他无从判断是链接错了
        还是 APP 没装。

        边界与 `resume_cmd` 完全一致：外来转录与归档会话都不给。链接打开的是
        APP 自己的索引，而这两种情况下那个索引里没有这条线程。
        """
        sid = (self.session_id or "").strip()
        if not sid or self.is_foreign or self.agent != "Codex":
            return ""
        if "archived_sessions" in {p.lower() for p in self.path.parts}:
            return ""
        parts = sid.split("-")
        uuid = "-".join(parts[-5:]) if len(parts) >= 5 else sid
        return f"codex://threads/{uuid}"

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
                if _DIGEST_SEP_RE.match(line.strip()):
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
            # 「最后一次真的在动」与文件时间分开给：界面显示前者，磁盘视图
            # 仍然用后者。`time_source` 让界面能标注这个时间可不可信——
            # 一个说不清来源的时间戳会被当成确定事实读。
            "active_at": self.active_at.isoformat(timespec="seconds"),
            "active_at_text": f"{self.active_at:%Y-%m-%d %H:%M:%S}",
            "time_source": self.time_source,
            "mb": round(self.mb, 2),
            "size": self.size,
            "fatal": self.fatal,
            "aborted": self.aborted,
            "errors": self.errors,
            "band": self.band,
            # 判定的**理由**，结构化给出。此前界面只显示徽章与几个指标，
            # 而抬档地板（压缩 ≥2 直接 critical、fatal+aborted ≥3 直接 high）
            # 完全不可见——用户看到「1.0 MB 却判 critical」无从判断是工具在
            # 按体积瞎猜还是真有依据。这里把主依据与每一次抬档都列出来。
            "band_reason": band_reason(
                size=self.size,
                fatal=self.fatal,
                aborted=self.aborted,
                tokens=self.tokens,
                window=self.context_window,
                compactions=self.compactions,
                unknown=self.compressed_unreadable,
            ),
            # 判定的主依据。GUI 要能显示「凭什么这么判」，否则用户只看到体积
            # 和一个徽章，会以为工具在按文件大小瞎猜。
            "tokens": self.tokens,
            "context_window": self.context_window,
            "compactions": self.compactions,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "cwd": self.cwd,
            "branch": self.branch,
            "version": self.version,
            "origin": self.origin,
            # 凭什么判定是这个 APP。卡片上会同时出现「谁写的」与「在谈论谁」，
            # 后者常是另一个 APP 的东西，不把依据摆出来读者只能猜。
            "agent_evidence": agent_evidence(self.path),
            "first_prompt": self.first_prompt,
            "title": self.title,
            "last_prompt": self.last_prompt,
            "digest": self.digest,
            "digest_windows": self.digest_windows,
            "asks": self.asks,
            # 报错原文。个数已经在 `errors` 里，这里是「错在哪」——
            # 交接文档据此告诉新会话哪些路已经走不通。
            "errors_text": self.errors_text,
            # 停下来时的模型与策略。逐轮字段，不在 session_meta 里。
            "turn_context": self.turn_context,
            # 占用走势 + 压缩位置。界面画成一条小线（sparkline），让「怎么涨的」
            # 与「涨到多少」一起可见——后者已经在 tokens 里了。
            "timeline": self.timeline,
            "label": self.label,
            "resume_cmd": self.resume_cmd,
            # 带 `pushd` 的那一条。两条都给：纯命令给已经在正确目录里的人，
            # 带 pushd 的给从别处复制走的人（那是多数情况）。
            "resume_cmd_cd": self.resume_cmd_cd,
            "deep_link": self.deep_link,
            # 这份转录是不是从别的电脑搬来的。为真时路径、原生续接、仓库推断
            # 都不可信，但内容仍然有价值——界面据此标注而不是隐藏。
            "is_foreign": self.is_foreign,
            "is_subagent": self.is_subagent,
            # 压缩归档且本机读不到正文。为真时来自正文的字段（tokens、
            # compactions、摘要、用户原话）全是空的——不是「这个会话很干净」，
            # 而是「一行都没读到」。界面必须把这两种情况分开，否则会给出
            # 完全相反的结论。
            "compressed_unreadable": self.compressed_unreadable,
            "repos": self.repos,
            "repo": self.repo,
            # 「在改哪个仓库」与「在哪启动」分开给。前者是用户真正在问的，
            # 后者是 resume 必须用的——两个都要，不一致时界面同时显示并说明。
            "work_repo": self.work_repo,
            "verdict": self.verdict.to_dict(),
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
        #
        # 用 `_session_id_from_name` 而不是 `fp.stem`：压缩归档的 rollout 叫
        # `….jsonl.zst`，`stem` 只剥最后一层，会把 `.jsonl` 留在 ID 尾巴上——
        # 那个字符串拼进 `codex resume` 必然报「找不到会话」。
        file_id = re.sub(r"^rollout-[\d\-T]+-", "", _session_id_from_name(fp))
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

# 压缩窗口之间的分隔标记。刻意不带任何自然语言：这一层是纯解析，拿不到
# Translator，而 digest 会原样进交接文档——写死中文会让英文文档里冒出中文标题。
# 形如 `===== [2/3] =====`，`_DIGEST_SEP_RE` 是配对的识别式。
_DIGEST_SEP_RE = re.compile(r"^=+\s*\[\d+\s*/\s*\d+\]\s*=+$")


def _digest_sep(i: int, total: int) -> str:
    return f"===== [{i}/{total}] ====="


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
            parts.append(f"{_digest_sep(i, len(self.windows))}\n\n{self.windows[key]}")
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


class _TimelineCollector:
    """按轮次采样「上下文怎么涨起来的」，供界面画时间线。

    为什么单看一个峰值不够：`tokens` 回答「最满的时候有多满」，但交接决策还要
    知道**怎么涨的**。同样是 97% 占用，一路平稳爬上去和最后两轮突然翻倍，是
    两种完全不同的处境——后者说明刚才那一步塞进了巨量内容（读了个大文件、
    粘了份日志），下一个会话该避开的正是那一步。压缩点同理：知道「压过 3 次」
    不如知道「三次都挤在最后十轮里」，那是 thrash 的形状。

    采样而不是全留：一份转录可以有几千轮，而画一条线用不了那么多点。上限
    `_TIMELINE_MAX` 个点，超了就**等距抽稀**（保留首尾与压缩点）。压缩点永不
    抽掉——它们是这条线上唯一的「事件」，抽掉就看不出阶段边界了。

    独立成类而不是把这些字段塞进 `_FullnessCollector`：那个类维护的是几个标量
    （峰值、上限、次数），职责是「有多满」；这里维护的是序列，职责是「怎么涨的」。
    混在一起会让一个类同时管两种形状的状态。

    但**不自己解析 JSON**：由 `_FullnessCollector` 在它已经 `json.loads` 出来的
    对象上调 `feed_usage` / `feed_compaction`。两者的预筛标记完全相同，各自再
    解析一遍等于把这条路径上最贵的一步付两次——实测每份转录命中几十到几百行。
    """

    __slots__ = ("points", "_compact_at", "_n")

    def __init__(self) -> None:
        # 每个点是 (轮次序号, 占用 token)。用轮次而不是墙钟时间：Claude 的
        # 转录里逐轮时间戳并非每行都有，而轮次序号一定单调。
        self.points: list[tuple[int, int]] = []
        # 压缩发生在第几个点上。用集合而不是标记位，因为抽稀时要按这个集合
        # 决定哪些点不能丢。
        self._compact_at: set[int] = set()
        self._n = 0

    def feed_usage(self, tokens: int) -> None:
        """记一个占用采样点。由 `_FullnessCollector` 解析出来之后调过来。"""
        if tokens <= 0:
            return
        self._n += 1
        self.points.append((self._n, tokens))

    def feed_compaction(self) -> None:
        """记一次压缩。位置就是当前轮次——压缩之后占用会掉回低位。"""
        self._compact_at.add(self._n)

    def as_dict(self) -> dict[str, Any]:
        """给界面的形态：抽稀后的点 + 压缩位置。

        点数少于两个时返回空——一条线至少要两个点，一个点画出来是误导
        （看起来像「一直是这个值」）。
        """
        if len(self.points) < 2:
            return {}
        pts = self._thin()
        return {
            "points": [{"i": i, "tokens": v} for i, v in pts],
            "compactions_at": sorted(self._compact_at),
            "turns": self._n,
        }

    def _thin(self) -> list[tuple[int, int]]:
        """等距抽稀到 `_TIMELINE_MAX` 个点，但保留首尾与压缩点。

        为什么不直接取前 N 个：那会把一条长会话画成「开头那段」，而交接最关心
        的恰恰是**结尾**的走势。也不能取后 N 个——那会丢掉「从哪儿开始涨的」。
        """
        if len(self.points) <= _TIMELINE_MAX:
            return self.points
        keep: dict[int, tuple[int, int]] = {}
        step = len(self.points) / _TIMELINE_MAX
        for k in range(_TIMELINE_MAX):
            idx = min(len(self.points) - 1, int(k * step))
            keep[idx] = self.points[idx]
        # 首尾必留：末点是「现在有多满」，首点是基线。
        keep[0] = self.points[0]
        keep[len(self.points) - 1] = self.points[-1]
        # 压缩点必留：它们是这条线上唯一的事件标记。
        for pos, pt in enumerate(self.points):
            if pt[0] in self._compact_at:
                keep[pos] = pt
        return [keep[k] for k in sorted(keep)]


class _FullnessCollector:
    """收集上下文占用与压缩次数——判断「还能撑多久」的真实依据。

    为什么必须有这个：体积与占用严重脱钩。实测 1.0 MB 的会话已用 194183 token
    （体积判据说「健康」），1.9 MB 的会话自动压缩过 10 次（体积判据说「留意」）。
    token 数就明写在转录里，没有理由去猜。

    取**峰值**而不是末值：末条 assistant 消息可能来自子代理或没有 cache 记账，
    实测两个转录的末值都是 0，照末值判会把满会话判成空的。

    与 _Extractor 不同，这个采集器**不能早停**：占用随会话增长，最大值可能出现
    在文件任何位置；压缩事件也散落全篇。所以照 _DigestCollector 的做法，
    先用子串预筛，命中了才付 json.loads 的代价——实测每份转录命中几十到几百行。
    """

    __slots__ = ("peak_tokens", "window", "compactions", "pre_tokens", "timeline")

    def __init__(self) -> None:
        self.peak_tokens = 0
        self.window = 0          # 上下文上限；只有 Codex 在转录里写
        self.compactions = 0     # 已发生的压缩次数——压缩过就是满过
        self.pre_tokens = 0      # 压缩发生时的占用峰值（Claude 的 preTokens）
        # 时间线采样。挂在这里而不是单独一个 feed 循环，是为了复用已经解析好的
        # JSON——两个采集器的预筛标记完全相同，各自再 json.loads 一遍是白付
        # 一倍最贵的代价（实测每份转录命中几十到几百行）。
        self.timeline = _TimelineCollector()

    def feed(self, raw: str) -> None:
        has_usage = any(m in raw for m in _USAGE_MARKS)
        has_compact = any(m in raw for m in _COMPACT_MARKS)
        if not has_usage and not has_compact:
            return
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(d, dict):
            return

        if has_compact:
            self._feed_compaction(d)
        if has_usage:
            self._feed_usage(d)

    def _feed_compaction(self, d: dict) -> None:
        # Claude：type:"system" + subtype:"compact_boundary"，
        # compactMetadata.preTokens 是压缩时的占用，属于历史事实。
        if d.get("subtype") == "compact_boundary":
            self.compactions += 1
            self.timeline.feed_compaction()
            meta = d.get("compactMetadata")
            if isinstance(meta, dict):
                pre = meta.get("preTokens")
                if isinstance(pre, int) and pre > self.pre_tokens:
                    self.pre_tokens = pre
        # Codex：独立的 compacted 记录。摘要内容由 _DigestCollector 负责，
        # 这里只数次数。
        elif d.get("type") == "compacted":
            self.compactions += 1
            self.timeline.feed_compaction()

    def _feed_usage(self, d: dict) -> None:
        msg = d.get("message")
        if isinstance(msg, dict):
            # Claude Code：占用 = 新输入 + 两种缓存读入。输出不算占用，
            # 它下一轮才会作为输入回到上下文里。
            u = msg.get("usage")
            if isinstance(u, dict):
                total = 0
                for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    v = u.get(key)
                    if isinstance(v, int):
                        total += v
                if total > self.peak_tokens:
                    self.peak_tokens = total
                self.timeline.feed_usage(total)
                return

        # Codex：event_msg / token_count，占用与上限都在 payload.info 里。
        payload = d.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            return
        info = payload.get("info")
        if not isinstance(info, dict):
            return
        win = info.get("model_context_window")
        if isinstance(win, int) and win > 0:
            self.window = win
        # last_token_usage 是「这一轮送进去多少」，也就是当前占用；
        # total_token_usage 是全会话累计，会远超窗口，不能当占用用。
        #
        # 取哪个字段要跟 Codex 自己一致。它显示占用率时走
        # `TokenUsage::tokens_in_context_window()`，实现就是 `self.total_tokens`
        # （codex-rs/protocol/src/protocol.rs:2261-2263，TUI 侧 token_usage.rs:39-41
        # 同义）。`TokenUsage` 有六个字段（input / cached_input /
        # cache_write_input / output / reasoning_output / total），只取
        # input_tokens 会漏掉 output 与 reasoning_output——推理密集的会话
        # 因此被系统性低估，而这类会话恰恰是最需要交接的。
        #
        # 没有 total_tokens 时才退回 input_tokens：老版本转录可能不写它，
        # 少算一点也比整条记录当没看见好。
        last = info.get("last_token_usage")
        if isinstance(last, dict):
            v = last.get("total_tokens")
            if not isinstance(v, int):
                v = last.get("input_tokens")
            if isinstance(v, int):
                if v > self.peak_tokens:
                    self.peak_tokens = v
                self.timeline.feed_usage(v)

    @property
    def tokens(self) -> int:
        """判定用的占用量：实测峰值与压缩前占用取大。

        压缩会把占用打回低位，只看压缩后的峰值会低估这个会话真正到过多满。
        """
        return max(self.peak_tokens, self.pre_tokens)


def _is_subagent(fp: Path) -> bool:
    """这份转录是子代理的，还是人真正对话的主会话？

    Claude Code 把子代理写进 `<会话id>/subagents/agent-*.jsonl`，Codex 给
    派生线程写独立的 rollout。它们不是人认得出的「那段对话」，而且数量远超
    主会话——实测本机最新 12 个 Claude 文件里 7 个是子代理（58%），最新 40 个
    Codex rollout 里 33 个带 parent_thread_id（83%）。不区分的话，默认
    `--limit 12` 会被子代理吃满，用户要交接的主会话被挤出列表。

    只看路径，不解析内容：这个判断要在「决定读哪些文件」之前做出来。

    布局与官方记载一致，不是本机个例：Claude Code 的会话在磁盘上是一个**目录**
    而不是单个文件，清理会话时删的是「整个 session 目录，含其中的 subagent
    转录」。实测本机 `~/.claude/projects` 下 144 个 jsonl 分两种深度——55 个
    深度 2 的主转录，89 个深度 4 的嵌套文件，后者第三层目录名 89/89 全部是
    `subagents`，其父目录 ID 也 89/89 对应真实主会话。所以按路径判就够，
    不需要读文件内容去猜。
    """
    parts = [p.lower() for p in fp.parts]
    return "subagents" in parts or fp.name.lower().startswith("agent-")


class _TurnContextCollector:
    """收集最后一条 `turn_context`——会话停下来时的模型与策略。

    独立于 `_Extractor` 是因为两者的位置相反：身份在文件开头（拿到就能早停），
    而「停下来时是什么状态」只能在读完之后才知道，所以这个采集器不能早停。
    照 `_DigestCollector` 的做法先做子串预筛，命中了才付 json.loads 的代价。

    只有 Codex 写这种记录；Claude 侧留空，由界面自己决定不显示。
    """

    __slots__ = ("model", "approval", "sandbox", "effort", "cwd")

    def __init__(self) -> None:
        self.model = ""
        self.approval = ""
        self.sandbox = ""
        self.effort = ""
        self.cwd = ""

    def feed(self, raw: str) -> None:
        if _TURNCTX_MARK not in raw:
            return
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(d, dict) or d.get("type") != "turn_context":
            return
        p = d.get("payload")
        if not isinstance(p, dict):
            return
        # 每条都覆盖：循环走完之后留下的就是最后一条。
        # 空值不覆盖已有值——某一轮可能只写了部分字段。
        for attr, key in (
            ("model", "model"),
            ("approval", "approval_policy"),
            ("sandbox", "sandbox_policy"),
            ("effort", "effort"),
            ("cwd", "cwd"),
        ):
            val = p.get(key)
            if isinstance(val, str) and val.strip():
                setattr(self, attr, val.strip())
            elif isinstance(val, dict):
                # sandbox_policy 有时是个对象（`{"mode": "workspace-write", …}`）。
                # 取里面最像名字的那个字段，取不到就跳过——不把整个 JSON 塞进去。
                for k in ("mode", "type", "name", "policy"):
                    inner = val.get(k)
                    if isinstance(inner, str) and inner.strip():
                        setattr(self, attr, inner.strip())
                        break

    def as_dict(self) -> dict[str, str]:
        """只给有值的字段。空字段留在 dict 里会让界面显示一排「—」。"""
        out = {}
        for key in ("model", "approval", "sandbox", "effort", "cwd"):
            val = getattr(self, key)
            if val:
                out[key] = val
        return out


def _error_text(raw: str) -> str:
    """从一条失败记录里取出报错正文，并尽量带上是哪个工具失败的。

    三种结构（都是实测的，不是照文档猜）：
      · Claude：`message.content[]` 里 `is_error` 为真的 tool_result 块，
        正文在它的 `content`（字符串或块数组）。
      · Codex MCP：`payload.result.Ok.isError` 为真，正文在 `result.Ok.content[]`；
        工具名在 `payload.invocation.{server,tool}`。
      · Codex MCP 调用本身失败：`payload.result.Err`，那是一个字符串或对象——
        「服务器没起来 / 超时」这类，比工具自己报错更严重。

    取不到就返回空串，**不退回整行原始 JSON**：那一行里有 call_id、时间戳、
    转义后的嵌套引号，读者从里面读不出「哪里错了」，而它会把额度全占掉。

    带上工具名的理由：报错正文往往只说「File access blocked」，不说是谁被挡了。
    新会话看到 `context-mode/ctx_execute_file: File access blocked…` 才知道
    该换哪个工具，只看正文只能知道「有东西被挡了」。
    """
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(d, dict):
        return ""

    def take(val: Any) -> str:
        if isinstance(val, str):
            return val
        if isinstance(val, list):
            out = []
            for b in val:
                if isinstance(b, dict):
                    t = b.get("text") or b.get("content")
                    if isinstance(t, str):
                        out.append(t)
                elif isinstance(b, str):
                    out.append(b)
            return "\n".join(out)
        if isinstance(val, dict):
            # `result.Err` 有时是个对象。取里面像消息的字段，不 dump 整个对象。
            for k in ("message", "error", "reason", "text", "detail"):
                t = val.get(k)
                if isinstance(t, str) and t.strip():
                    return t
        return ""

    def done(text: str, tool: str = "") -> str:
        flat = " ".join((text or "").split())
        if not flat:
            return ""
        if tool:
            flat = f"{tool}: {flat}"
        return flat[:_ERR_CHARS]

    # Claude：message.content[] 里 is_error 为真的那块。
    msg = d.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and (b.get("is_error") or b.get("isError")):
                    got = done(take(b.get("content")))
                    if got:
                        return got

    pl = d.get("payload")
    if not isinstance(pl, dict):
        return ""

    # 工具名：MCP 的两段式在 invocation 里，普通函数调用只有 name。
    inv = pl.get("invocation")
    tool = ""
    if isinstance(inv, dict):
        server = str(inv.get("server") or "").strip()
        name = str(inv.get("tool") or inv.get("name") or "").strip()
        tool = f"{server}/{name}" if server and name else name
    if not tool:
        tool = str(pl.get("name") or pl.get("tool") or "").strip()

    # Codex MCP：result.Err（调用失败）优先于 result.Ok.isError（工具自己报错），
    # 因为前者更严重——工具根本没跑起来。
    res = pl.get("result")
    if isinstance(res, dict):
        if "Err" in res:
            got = done(take(res["Err"]), tool)
            if got:
                return got
        ok = res.get("Ok")
        if isinstance(ok, dict) and (ok.get("isError") or ok.get("is_error")):
            got = done(take(ok.get("content")), tool)
            if got:
                return got

    # Codex 普通函数调用：payload.output（字符串或 {content}）。
    out = pl.get("output")
    if isinstance(out, dict):
        got = done(take(out.get("content") or out.get("text") or out.get("output")), tool)
        if got:
            return got
    elif isinstance(out, str):
        got = done(out, tool)
        if got:
            return got
    got = done(take(pl.get("content")), tool)
    return got


def _session_id_from_name(fp: Path) -> str:
    """从文件名兜底取会话标识，正文读不到时用。

    不能直接用 `Path.stem`：压缩转录叫 `xxx.jsonl.zst`，`stem` 只剥最后一层，
    留下 `xxx.jsonl` —— 那个字符串既不是会话 ID 也不是文件名，拼到 resume
    命令里必然失败。这里两层都剥干净。
    """
    name = fp.name
    for suffix in (".jsonl.zst", ".jsonl"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return fp.stem


def scan_one(
    agent: str,
    fp: Path,
    deep: bool = True,
    workspaces: WorkspaceMap | None = None,
) -> SessionRow | None:
    """单遍读完一个转录，同时得到风险计数、身份卡与涉及仓库。

    原版要读三遍；这里一遍。深度信息拿全之后剩下的行只做两次子串查找和
    一次正则，不再解析 JSON——那是整个流程里最贵的一步。

    `workspaces` 是本机的工作区分组（多根工作区里哪些仓库并列打开过）。传进来
    时，落在多根工作区里的 `cwd` 会被降权——那种值只说明「哪个文件夹排在
    `folders` 第一位」。默认 `None` 是「不知道有没有工作区」，此时 `cwd` 按
    原有权重处理，行为与改动前完全一致。
    """
    try:
        st = fp.stat()
    except OSError:
        return None

    # 最后一次真的在动是什么时候。在读正文**之前**取：这一步只 seek 尾部读
    # 32 KB，与下面的逐行扫描互不影响，而把它放在这里能保证即使正文读失败
    # （压缩缺 zstd、流坏了）也仍然有一个时间可用。
    last_ts, time_src = last_record_time(fp)

    fatal = 0
    errors = 0
    aborted = 0
    ident: dict[str, str] = {}
    repos: list[str] = []
    unreadable = False
    # 最近几条错误原文。用定长环形缓冲（deque with maxlen）而不是无界 list：
    # 一份转录可能有几百条错误，全留下来会让每个 SessionRow 都拖着几十 KB
    # 走进缓存——那正是 `_CACHE_MAX` 注释里记过的那笔内存账。
    err_texts: deque[str] = deque(maxlen=_ERR_KEEP)
    ex = _Extractor(agent) if deep else None
    dg = _DigestCollector() if deep else None
    fl = _FullnessCollector() if deep else None
    tc = _TurnContextCollector() if deep else None
    # 归属证据只在 deep 时收：非 deep 是「只要个数」的快速模式，而这一项要
    # 解析工具调用的入参，比数错误便宜不了多少。
    at = AttributionCollector(agent, ATTRIBUTION_LINE_BUDGET) if deep else None
    try:
        with open_transcript(fp) as fh:
            for raw in fh:
                if FATAL_SIG.search(raw):
                    fatal += 1
                if ABORT_SIG.search(raw):
                    aborted += 1
                if any(m in raw for m in _ERR_MARKS):
                    errors += 1
                    if deep:
                        # 只在 deep 时取原文：非 deep 是「只要个数」的快速模式。
                        text = _error_text(raw)
                        if text:
                            err_texts.append(text)
                if dg is not None:
                    dg.feed(raw)
                if fl is not None:
                    fl.feed(raw)
                if tc is not None:
                    tc.feed(raw)
                if at is not None:
                    at.feed(raw)
                if ex is not None:
                    ex.feed(raw)
                    if ex.done:
                        # 深度信息已齐：收尾并丢掉提取器，剩下的行走廉价分支
                        # （子串查找 + 一次正则，摘要预筛也只是子串查找）。
                        #
                        # 归属收集器**不**在这里丢：写文件通常发生在会话中后段，
                        # 而身份卡在开头几行就齐了。跟着 ex 一起丢等于系统性地
                        # 漏掉这个会话真正在改什么。
                        ident, repos = ex.finish(fp)
                        ex = None
            if ex is not None:
                # 文件在预算用完前就结束了，用已有的部分收尾。
                ident, repos = ex.finish(fp)
    except TranscriptCompressedError:
        # 压缩归档 + 本机无 zstd 实现。不能 return None：那等于让这个会话从
        # 列表里消失，而「消失」正是这次要修掉的沉默失效。带着空正文继续，
        # 由 compressed_unreadable 告诉界面为什么什么都没有。
        unreadable = True
        ex = None
        dg = None
        fl = None
        tc = None
        at = None
        ident, repos = {"session_id": _session_id_from_name(fp)}, []
    except OSError:
        return None
    except Exception:
        # zstd 流本身坏了（截断、校验失败）也走「读不到正文」这条路，而不是
        # 让一个坏文件把整轮扫描带崩。真正读不了的文件与缺解压能力的文件
        # 在界面上的说法一样：正文拿不到。
        unreadable = True
        ex = None
        dg = None
        fl = None
        tc = None
        at = None
        ident, repos = {"session_id": _session_id_from_name(fp)}, []

    if not deep and not unreadable:
        ident, repos = {"session_id": _session_id_from_name(fp)}, []

    # 归属结论。`cwd` 与 `repos` 由 `_Extractor` 收集，直接传进来参与分层，
    # 不重复解析——它们分别是「在哪启动」与「提到过」，是分层里最弱的两级。
    verdict = (
        at.verdict(cwd=ident.get("cwd", ""), mentioned=repos, workspaces=workspaces)
        if at is not None
        else RepoVerdict()
    )

    return SessionRow(
        agent=agent,
        path=fp,
        file=fp.name,
        mtime=datetime.fromtimestamp(st.st_mtime),
        mb=st.st_size / 1e6,
        size=st.st_size,
        fatal=fatal,
        errors=errors,
        band=band_for(
            st.st_size, fatal, aborted,
            tokens=fl.tokens if fl else 0,
            window=fl.window if fl else 0,
            compactions=fl.compactions if fl else 0,
            unknown=unreadable,
        ),
        aborted=aborted,
        tokens=fl.tokens if fl else 0,
        context_window=fl.window if fl else 0,
        compactions=fl.compactions if fl else 0,
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
        errors_text=list(err_texts),
        turn_context=tc.as_dict() if tc else {},
        timeline=fl.timeline.as_dict() if fl else {},
        is_subagent=_is_subagent(fp),
        repos=repos,
        compressed_unreadable=unreadable,
        last_active_ts=last_ts,
        time_source=time_src,
        verdict=verdict,
    )


# 转录是追加写的：同一个 (路径, 大小, mtime) 的扫描结果不会变。
# 一次会话里 --vitals 和交接流程都要扫，缓存能省掉第二遍全量 I/O。
#
# 必须有上限。键里含 mtime，而转录是**持续追加**的：网页界面长驻时每次
# `/api/vitals` 都会因为 mtime 变化生成新键，旧条目永不失效——每个 SessionRow
# 现在还带着完整的压缩摘要（实测单份可达 96 KB），一晚下来就是几百 MB。
# 逐出最旧的：转录越新越可能被再次问到。
#
# 加锁的理由：`scan_session_vitals` 用 ThreadPoolExecutor 并发扫描，多个线程
# 会同时走到这里。GIL 保证单个 `__setitem__` 不撕裂，但「查 → move_to_end →
# 写 → 循环 popitem」是四步组合，线程在中间被切走时 `len(_cache)` 的判断就
# 基于过期的视图：两个线程各自认为只需逐出一条，结果都不逐出，缓存越过上限；
# 反过来也可能把对方刚写进去的热条目当成最旧的挤掉。锁只圈住这几步字典操作，
# 真正的重活（`scan_one` 读文件）在锁外，所以并发度不受影响。
_CACHE_MAX = 256
_cache: OrderedDict[tuple[str, int, int, bool], SessionRow] = OrderedDict()
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached_scan(
    agent: str,
    fp: Path,
    deep: bool,
    workspaces: WorkspaceMap | None = None,
) -> SessionRow | None:
    try:
        st = fp.stat()
    except OSError:
        return None
    # 缓存键不含 `workspaces`：工作区分组在一轮扫描内是不变的，而它是按引用
    # 传下来的同一个对象。把它编进键里会让每次重新发现工作区都整体作废缓存，
    # 收益为零。真正会让结果变化的是转录本身（路径、大小、mtime）。
    key = (str(fp), st.st_size, int(st.st_mtime), deep)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            # 命中的挪到末尾：LRU 的「最近用过」语义，避免热点条目被冷条目挤掉。
            _cache.move_to_end(key)
            return hit
    # 读文件放在锁外：一份转录可能上百 MB，持锁读会把并发扫描退化成串行。
    # 代价是两个线程可能同时扫同一份文件（重复工作，但结果相同），
    # 而收益是 N 份不同转录仍然真并发——后者是常态，前者是巧合。
    row = scan_one(agent, fp, deep, workspaces)
    if row is not None:
        with _cache_lock:
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
                        elif is_transcript_name(e.name) and e.is_file(follow_symlinks=False):
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
    limit: int = 12,
    deep: bool = True,
    jobs: int = 0,
    include_subagents: bool = False,
    workspaces: WorkspaceMap | None = None,
) -> list[SessionRow]:
    """按风险排序最新的转录。流式读取，只保留计数器与身份卡。

    jobs=0 时按转录数量与 CPU 核数自动决定并行度。这些任务全是磁盘等待，
    GIL 不构成瓶颈；实测 40 个多 MB 转录从串行的十几秒降到两三秒。

    `workspaces` 省略时**自动发现**一次：多根工作区里的 `cwd` 只说明「哪个
    文件夹排在第一位」，而这个判断对整轮扫描是同一个答案，发现一次复用给所有
    转录。实测发现耗时 0.12 秒（读锁文件加找 `.code-workspace`），相对整轮
    扫描可以忽略。传 `False` 之外的显式值可以跳过发现——测试要的正是那个。
    """
    tasks: list[tuple[str, Path]] = []
    for agent, root in agent_session_roots():
        for fp in _newest_files(root, limit, include_subagents):
            tasks.append((agent, fp))
    if not tasks:
        return []

    if workspaces is None:
        # 发现放在这里而不是每个 `scan_one` 里：那会对每份转录重复扫一遍磁盘。
        workspaces = WorkspaceMap.discover()

    if jobs <= 0:
        jobs = min(len(tasks), max(4, (os.cpu_count() or 4) * 2))
    jobs = max(1, min(jobs, 32))

    rows: list[SessionRow] = []
    if jobs == 1:
        for agent, fp in tasks:
            row = _cached_scan(agent, fp, deep, workspaces)
            if row is not None:
                rows.append(row)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for row in pool.map(lambda t: _cached_scan(t[0], t[1], deep, workspaces), tasks):
                if row is not None:
                    rows.append(row)

    # 同一区间内按占用排，不按体积：体积大不等于更该交接。一个压缩过 10 次的
    # 2 MB 会话比一个从没压缩的 20 MB 会话更紧急，而体积排序会把它压到后面。
    # 压缩次数优先，其次占用量，最后才拿体积兜底（读不到 token 时唯一可用的）。
    rows.sort(key=lambda r: (BAND_ORDER[r.band], -r.compactions, -r.tokens, -r.mb))
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
    hits.sort(key=lambda r: r.active_at, reverse=True)
    return hits


def locate_by_id(
    needles: Iterable[str],
    deep: bool = True,
    workspaces: WorkspaceMap | None = None,
) -> list[SessionRow]:
    """按会话 ID 片段在**全部**转录里定位，不受 `--limit` 约束。

    为什么必须绕开 limit：`--find` 的语义是「我知道 ID，帮我找到它」。而按
    limit 扫最新若干个再在结果里搜，等于「只在最近的会话里找」——实测本机
    467 个转录、每个根只扫 40 个，覆盖 26%，给了准确 ID 也有七成概率说
    「没找到」。那是最坏的一种失败：用户握着正确的信息，工具告诉他信息是错的。

    这里只按**文件名**匹配，不读文件内容，所以代价是一次目录遍历（实测 463 个
    转录 14 毫秒）。命中之后才去读那几份转录的正文——一份也可能上百 MB，
    读全量是不可接受的，读命中的几份是必要的。

    多个片段一起给：返回的顺序与 `needles` 的顺序一致（同一片段命中多个时按
    最近活动优先），因为用户列出 ID 的顺序通常就是他心里的优先级。
    """
    wanted = [n.strip().lower() for n in needles if n and n.strip()]
    if not wanted:
        return []

    if workspaces is None:
        # 与 `scan_session_vitals` 同一个理由：发现一次，供本轮全部命中复用。
        workspaces = WorkspaceMap.discover()

    # 先把全部候选文件的名字与路径收齐。子代理转录也纳入：用户拿着一个具体的
    # ID 来找，那份转录是不是子代理的不该影响「能不能找到」。
    candidates: list[tuple[str, Path]] = []
    for agent, root in agent_session_roots():
        stack = [root]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(Path(e.path))
                            elif is_transcript_name(e.name) and e.is_file(follow_symlinks=False):
                                candidates.append((agent, Path(e.path)))
                        except OSError:
                            continue
            except OSError:
                # 一个根坏掉不该让其余的也找不到。
                continue

    out: list[SessionRow] = []
    seen: set[str] = set()
    for needle in wanted:
        matched: list[tuple[float, str, Path]] = []
        for agent, fp in candidates:
            key = norm_path(fp)
            if key in seen:
                continue
            if needle in fp.name.lower():
                try:
                    matched.append((fp.stat().st_mtime, agent, fp))
                except OSError:
                    continue
        # 同一片段命中多个时最近活动在前——找会话时"最近那个"几乎总是要找的那个。
        matched.sort(key=lambda t: t[0], reverse=True)
        for _mtime, agent, fp in matched:
            key = norm_path(fp)
            if key in seen:
                continue
            row = _cached_scan(agent, fp, deep, workspaces)
            if row is not None:
                seen.add(key)
                out.append(row)
    return out


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
        ts = r.active_at.timestamp()
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
    严格按最后活动倒序，不再让体积介入——体积是风险信号，卡片上的徽章已经
    在说这件事，用它排序会把一个月前的大转录顶到今天的会话前面。

    排序键用 `active_at`（转录里最后一条记录的时间）而不是文件 mtime：实测
    492 份会话按两种口径排，位次相同的只有 122 份，最大偏移 93 位。Codex 侧
    尤其严重——它的 mtime 实质是会话**创建**时刻，用它排序等于按开始时间排。
    """
    buckets: dict[str, list[SessionRow]] = {}
    for r in rows:
        buckets.setdefault(r.agent, []).append(r)
    for group in buckets.values():
        group.sort(key=lambda r: r.active_at, reverse=True)
    # 组间：先按该组最新一条的时间倒序，时间相同再按名字，保证结果可复现。
    return sorted(
        buckets.items(),
        key=lambda kv: (-kv[1][0].active_at.timestamp(), kv[0]),
    )
