#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划文档的定位、解析与回填。

设计原则：不硬编码任何项目名、路径、任务名或测试命令。项目相关信息全部从
仓库自身推断：
  计划文档 Files: 段        -> 每个任务应产出哪些文件
  计划文档 Interfaces: 段   -> 每个任务应产出哪些符号
  计划文档 约束段           -> 哪些文件不得提交
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CHECKBOX = re.compile(r"^(?P<indent>\s*)[-*+]\s+\[(?P<mark>[ xX~])\]\s*(?P<body>.*)$", re.M)
# 任务标题：允许 1-6 级、允许行首缩进。原版卡死在 `#{2,4}` + 行首无空格，
# `##### Task 5` 与缩进过的标题会被整段忽略，那个任务的全部步骤随之消失。
TASK_HEAD = re.compile(
    r"^[ \t]*#{1,6}\s+(?P<title>(?:Task|任务|任務|Phase|阶段|階段)\s*(?P<num>\d+)[^\n]*)$",
    re.M,
)
# 文件行。三处放宽，每一处都对应真实 markdown 写法：
#   · 动词可以被 `**` 包住——`- **Modify**: x` 是最常见的计划写法
#   · 冒号可选——`- Modify \`a/b.ts\`` 同样明确
#   · 动词整体可选——`- \`a/b.ts\` — 说明` 也是文件行
# 动词表补 Delete/Remove/删除：计划里「要删掉某文件」也是产出。
_FILE_VERB = r"Create|Modify|Add|Update|Delete|Remove|Rename|新建|修改|新增|更新|删除|刪除|重命名"
FILE_LINE = re.compile(
    rf"^\s*[-*+]\s*(?:\*\*)?(?:{_FILE_VERB})?(?:\*\*)?\s*[:：]?\s*(?P<rest>.+)$",
    re.I,
)
# 一行里可能列多个路径（`- Create: \`a.ts\`, \`b.ts\``）。只取第一个会让
# file_ratio 的分母偏小，缺失文件被漏报，完成度偏乐观。
PATH_IN_LINE = re.compile(r"`(?P<path>[^`\s]{2,200})`")
# 没有反引号时退回裸路径：必须含 `/` 或扩展名，否则整句散文都会被当路径。
BARE_PATH = re.compile(r"(?<![\w`])(?P<path>[\w.\-]*(?:/[\w.\-]+)+|\w[\w.\-]*\.[A-Za-z0-9]{1,8})(?![\w`])")
BACKTICK = re.compile(r"`([^`]+)`")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PROTECTED_HINT = re.compile(
    r"`(?P<path>[^`\n]{1,120})`[^\n]{0,160}?"
    r"(?:user-owned|用户私有|使用者私有|must not be[^\n]{0,80}?(?:staged|packaged|committed)|不得[^\n]{0,40}?(?:提交|打包))",
    re.I,
)
# 步骤号。粗体可选：`- [ ] Step 1: x` 与 `- [ ] **Step 1** x` 都算。
STEP_HEAD = re.compile(r"(?:\*\*)?(?:Step|步骤|步驟)\s*(\d+)", re.I)
# Interfaces 段里哪些行在声明产出。原版只认 Produces/产出，
# `- Exports: \`undo\`` / `- 提供 \`undo\`` / `- \`undo()\` — 撤销` 全部漏掉，
# 于是那个任务符号证据为空，落进「没有符号就不算完成」的死角。
INTERFACE_DECL = re.compile(
    r"Produces|Exports?|Provides|Returns|Adds|产出|產出|提供|导出|導出|新增|返回",
    re.I,
)

INTENT_RX = re.compile(
    r"^(?:\*\*(?P<bold>Goal|Objective|目标|目標|Architecture|架构|架構|Background|背景)[:：]?\*\*|"
    r"#{2,3}\s*(?P<head>Global Constraints|Constraints|全局约束|全域約束|约束|約束|Background|背景|Overview|概述))",
    re.M,
)

# 找计划文档时要跳过的目录。缺一个就会去读 node_modules 里成千上万个 .md。
PLAN_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", ".venv-win", "venv",
    "dist", "build", "out", "target", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".tox", ".next", ".nuxt", ".turbo",
    "site-packages", ".idea", ".vscode", "coverage", "htmlcov", ".cache",
    "vendor", "third_party", ".github",
}
PLAN_MAX_DEPTH = 5
PLAN_HEAD_BYTES = 60000
# 理论下界：一个任务标题 + 3 个 `- [ ] **Step N**` 复选框，最紧凑也要 ~120 字节。
# 取 200 留余量。真正省 I/O 的是下面的 `"- [" not in head` 子串预筛，
# 这道门槛只是先挡掉徽章型 README 和空 stub，不能设成"我猜真文档多大"。
PLAN_MIN_BYTES = 80
# 先只读这一块做判定；绝大多数文件在这里就被否掉，不必读满 60 KB。
PLAN_FIRST_CHUNK = 16000


@dataclass
class Step:
    task_num: int
    number: int
    line_index: int
    text: str
    done: bool
    evidence: str = ""


@dataclass
class Task:
    num: int
    title: str
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


def find_plan(repo: Path, explicit: str | None) -> Path | None:
    """定位实现计划。最新的、带复选框的 markdown 胜出。

    原版对深度 5 以内的每个 .md 都读 60 KB。一个装了依赖的仓库里那是几千个
    文件。这里加两道廉价前置筛：先按体积排除（真计划文档不会小于 ~1 KB），
    再只读头部一块而不是 60 KB——任务标题和复选框如果存在，一定在前 16 KB 里
    出现过至少一次。只有通过初筛的文件才付读全 60 KB 的代价。
    """
    if explicit:
        p = Path(explicit) if os.path.isabs(explicit) else (repo / explicit)
        return p if p.is_file() else None

    candidates: list[tuple[float, Path]] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in PLAN_SKIP_DIRS and not d.startswith(".")]
        try:
            depth = len(Path(root).relative_to(repo).parts)
        except ValueError:
            continue
        if depth > PLAN_MAX_DEPTH:
            dirs[:] = []
            continue
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            fp = Path(root) / fn
            try:
                st = fp.stat()
            except OSError:
                continue
            if st.st_size < PLAN_MIN_BYTES:
                continue
            try:
                with fp.open(encoding="utf-8", errors="replace") as fh:
                    head = fh.read(PLAN_FIRST_CHUNK)
                    # 最廉价的预筛先行：没有任何 `- [` 的文件不可能是计划文档，
                    # 一次子串查找就排掉绝大多数 README / CHANGELOG。
                    if "- [" not in head:
                        continue
                    # 头一块没定论时才付读满 60 KB 的代价。
                    if not TASK_HEAD.search(head) or len(CHECKBOX.findall(head)) < 3:
                        rest = fh.read(max(0, PLAN_HEAD_BYTES - len(head)))
                        if rest:
                            head += rest
                    if not TASK_HEAD.search(head):
                        continue
                    if len(CHECKBOX.findall(head)) < 3:
                        continue
            except OSError:
                continue
            candidates.append((st.st_mtime, fp))
    if not candidates:
        return None
    # 同一时间戳时按路径排序，让结果可复现（原版靠 Path 的比较，Windows 上
    # 大小写不同的路径顺序不稳定）。
    candidates.sort(key=lambda t: (t[0], str(t[1]).lower()), reverse=True)
    return candidates[0][1]


def parse_plan(text: str) -> tuple[list[Task], list[str]]:
    """从计划文档里抽出任务（文件、符号、步骤）与受保护路径。"""
    lines = text.splitlines()
    tasks: list[Task] = []
    current: Task | None = None
    section: str | None = None

    for idx, raw in enumerate(lines):
        head = TASK_HEAD.match(raw)
        if head:
            current = Task(num=int(head.group("num")), title=head.group("title").strip())
            tasks.append(current)
            section = None
            continue

        low = raw.strip().lower()
        if low.startswith("**files") or low.startswith("**文件") or low.startswith("**檔案"):
            section = "files"
            continue
        if low.startswith("**interfaces") or low.startswith("**接口") or low.startswith("**介面"):
            section = "interfaces"
            continue
        # 换到别的粗体小节就退出当前小节。原版这里用 raw 而上面两个判断用
        # strip 后的 low，于是缩进过的 `  **Constraints:**` 无法结束 files 段，
        # 后面所有 `- Create: x` 都会被算进上一个任务。统一用 low。
        if low.startswith("**") and section:
            section = None

        if current is None:
            continue

        if section == "files":
            fm = FILE_LINE.match(raw)
            if fm:
                rest = fm.group("rest")
                # 一行可以列多个路径。优先取反引号里的（明确标注），没有反引号
                # 时才退回裸路径识别，避免把散文里的词当成文件。
                found = [m.group("path") for m in PATH_IN_LINE.finditer(rest)]
                if not found:
                    found = [m.group("path") for m in BARE_PATH.finditer(rest)]
                for path in found:
                    path = path.strip().strip("`,;、")
                    # 计划里的路径偶尔写成 `/webui/src/x.ts` 或绝对路径；
                    # 直接 `repo / path` 会逃出仓库（PureWindowsPath 会跳到盘符根），
                    # 于是文件判定跑到仓库外，永远判缺失。归一化成仓库内相对路径。
                    path = path.replace("\\", "/").lstrip("/")
                    if path and path not in current.files:
                        current.files.append(path)
            continue

        if section == "interfaces" and INTERFACE_DECL.search(raw):
            for token in BACKTICK.findall(raw):
                base = token.split("(")[0].split("[")[0].strip()
                base = base.split(".")[-1] if "." in base and " " not in base else base
                # 原版要求 len > 3，于是 `run` `add` `get` `fn` `id` 这些真实接口名
                # 被静默丢弃。真正要挡的是单字母占位符，2 个字符起就够。
                if IDENT.fullmatch(base) and len(base) >= 2 and base not in current.symbols:
                    current.symbols.append(base)
            continue

        cb = CHECKBOX.match(raw)
        if cb:
            body = cb.group("body")
            sm = STEP_HEAD.match(body)
            if sm:
                current.steps.append(
                    Step(
                        task_num=current.num,
                        number=int(sm.group(1)),
                        line_index=idx,
                        text=re.sub(r"\*\*", "", body)[:160],
                        # `[~]` 是「部分完成」，不是完成——按未完成算才安全。
                        done=cb.group("mark").lower() == "x",
                    )
                )

    protected = set()
    for m in PROTECTED_HINT.finditer(text):
        cand = m.group("path").strip()
        # 受保护路径必须看着像路径，不能是句子片段。
        if not cand or " " in cand or "\n" in cand or len(cand) > 120:
            continue
        if "/" not in cand and "." not in cand:
            continue
        protected.add(cand)
    return tasks, sorted(protected)


def find_intent_sections(text: str) -> list[str]:
    """计划文档里哪些段落承载原始要求，而不是步骤清单？

    只说"去看复选框"的交接提示词，会让新会话把计划当待办清单，从而漏掉目标、
    红线约束和整份计划所依赖的架构承诺。要点名。
    """
    found: list[str] = []
    for m in INTENT_RX.finditer(text):
        label = (m.group("bold") or m.group("head") or "").strip()
        if label and label not in found:
            found.append(label)
    return found


def _detect_newline(data: bytes) -> str:
    """文件用的是哪种换行。按出现次数判定，混用时取多数。

    原版用 `read_text` + `splitlines(keepends=True)` + `newline=""` 写回：
    读取时通用换行把 CRLF 折成 LF，写回时原样写 LF，于是一个 CRLF 的计划文档
    在回填一个复选框之后整份文件的每一行都变了——git diff 里几百行改动，
    而真正的改动只有一处。这会让接续会话完全看不出发生了什么。
    """
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and crlf >= lf:
        return "\r\n"
    if b"\r" in data and not lf and not crlf:
        return "\r"  # 老式 Mac 换行，罕见但存在
    return "\n"


def update_plan(
    plan_path: Path,
    tasks: list[Task],
    report: dict[int, dict],
    dry: bool,
) -> tuple[int, int]:
    """把所有文件与符号都到位的任务的每一步都勾上。

    换行风格原样保留：只改被勾选的那几行，其余字节不动。
    """
    total = sum(len(t.steps) for t in tasks)
    try:
        data = plan_path.read_bytes()
    except OSError:
        return 0, total
    newline = _detect_newline(data)
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    # CRLF 文件按 \n 切开后每行尾部还留着 \r，勾选替换发生在行首，不受影响。

    changed = 0
    for t in tasks:
        if not report.get(t.num, {}).get("complete"):
            continue
        for st in t.steps:
            if st.done:
                continue
            if not (0 <= st.line_index < len(lines)):
                continue  # 计划文档在解析后被改过；不要写错行
            raw = lines[st.line_index]
            if "- [ ]" not in raw:
                continue
            lines[st.line_index] = raw.replace("- [ ]", "- [x]", 1)
            changed += 1

    if changed and not dry:
        body = "\n".join(lines)
        if newline != "\n":
            # 先归一化再统一替换，避免把已有的 \r\n 变成 \r\r\n。
            body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
        plan_path.write_bytes(body.encode("utf-8"))
    return changed, total
