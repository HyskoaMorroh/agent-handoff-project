#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次交接打成能带到另一台电脑的包，以及在那台电脑上装回来。

为什么需要它：交接文档里的转录路径是**这台机器上的位置**，不是内容。
`~\\.claude\\projects\\C--Users-devin-proj\\<id>.jsonl` 这个路径里，目录名本身
编码了源机的 cwd——换机器后那个目录不存在，新会话拿到路径也读不到东西。
文档只给路径、不给内容，是「换机必然失效」的根本原因。

所以包里要**带转录本体的副本**，并且路径存成占位符：

    {CLAUDE_ROOT}/projects/<slug>/<id>.jsonl

导入时按目标机器自己的 `agent_session_roots()` 重新解析占位符——这与
`CLAUDE_CONFIG_DIR` / `CODEX_HOME` 的语义一致，也是同类工具的共识做法：
不存绝对路径再做字符串替换，而是存「相对哪个根」+ 导入时重新问一次根在哪。

红线：**只读转录，绝不写回或移动它们。** Claude Code 从 v2.1.205 起有一条
安全规则明确禁止篡改会话转录文件；导入时转录副本落在包自己的目录里，
由用户自行决定是否放进 agent 的数据目录。工具不代做这个决定。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..platform import agent_session_roots, norm_path

# 包格式的版本。读到比自己大的值就停下并让用户升级，而不是硬着头皮解析——
# 猜一个未知格式的字段含义，比明确说「我不认识这个版本」危险得多。
#
# 这个字段是**必需**的，而且必须是第一个被检查的东西：没有它的包一律拒收。
SCHEMA_VERSION = 1

# 包内的固定布局。名字写死在这里，不散落在各处拼接。
MANIFEST_NAME = "manifest.json"
TRANSCRIPTS_DIR = "transcripts"
DOC_DIR = "handoff"
# 每会话四件套的目录。一个会话一个子目录，里面是它自己的 Markdown、
# 定位信息与续接命令——这样「把某一个会话交给别人」是拷一个目录，
# 而不是从一份大文档里剪一段出来。
SESSIONS_DIR = "sessions"

# Codex 的 rollout 文件名是 `rollout-<时间戳>-<UUID>.jsonl`，而 `codex resume`
# 接受的是那个 UUID。整个文件名前缀传进去会查不到会话——resume 命令看着像对的，
# 粘进终端才发现不行。`vitals.py` 已经这么剥了，这里必须与它一致。
_ROLLOUT_PREFIX = re.compile(r"^rollout-[\d\-T]+-", re.I)


def bare_session_id(path: Path) -> str:
    """从转录文件名取出可用于 resume 的会话 ID。"""
    stem = re.sub(r"\.jsonl$", "", path.name, flags=re.I)
    return _ROLLOUT_PREFIX.sub("", stem)[:200]

# 占位符与它对应的根。导出时把绝对路径换成占位符，导入时反向解析。
#
# 名字用 `{CLAUDE_ROOT}` 这种花括号形态而不是 `$CLAUDE_ROOT`：后者在 shell 里
# 会被意外展开，而这些字符串会出现在给人看的文档里。
#
# 键是**根目录的末段名**而不是 agent 名：Codex 有两个根（`sessions` 与
# `archived_sessions`），共用一个 `{CODEX_ROOT}` 会让归档的转录在导入时被解析到
# `sessions/` 下——路径存在语法上仍然合法，所以不会报错，只会静默指向错误位置。
# 实测：`archived_sessions/2026/rollout-a.jsonl` 曾被还原成
# `sessions/2026/rollout-a.jsonl`。这类「看起来成功了」的错误最难发现。
PLACEHOLDERS = {
    "projects": "{CLAUDE_ROOT}",
    "sessions": "{CODEX_ROOT}",
    "archived_sessions": "{CODEX_ARCHIVED_ROOT}",
}
_PLACEHOLDER_RX = re.compile(r"^\{([A-Z_]+)\}(?:[/\\](.*))?$")


def _token_for(root: Path) -> str:
    """这个根对应哪个占位符。认不出返回空串。"""
    return PLACEHOLDERS.get(root.name.lower(), "")

# 单个转录的体积上限。超过就只记元信息不带正文。
#
# 为什么要有上限：实测本机有 115 MB 的单个转录，而一个交接包的意义是「能拷走」。
# 把一份 115 MB 的历史塞进包里，多半装的是工具输出而不是决策过程，而它会让
# 整个包大到没人愿意传。超限时包里仍然留下路径与摘要，用户可以自己单独拷。
MAX_TRANSCRIPT_BYTES = 24 * 1024 * 1024


@dataclass
class ExportedSession:
    """包里一个会话的记录。除 agent 外全部可空——转录格式会变，缺字段要降级。"""

    agent: str
    placeholder_path: str = ""
    stored_name: str = ""
    session_id: str = ""
    size: int = 0
    skipped_reason: str = ""
    # 每会话四件套所在的子目录（包内相对路径）。空表示没生成——
    # 转录读不到内容时只有副本，没有可读的 Markdown。
    artifacts_dir: str = ""
    resume: str = ""
    deep_link: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"agent": self.agent}
        for key, val in (
            ("placeholder_path", self.placeholder_path),
            ("stored_name", self.stored_name),
            ("session_id", self.session_id),
            ("skipped_reason", self.skipped_reason),
            ("artifacts_dir", self.artifacts_dir),
            ("resume", self.resume),
            ("deep_link", self.deep_link),
        ):
            if val:
                out[key] = val
        if self.size:
            out["size"] = self.size
        if self.raw:
            out["raw"] = self.raw
        return out


def _root_for(path: Path) -> tuple[str, Path] | None:
    """这个转录属于哪个 agent 的哪个根？认不出返回 None。

    比对用 `norm_path` 归一后的前缀：盘符大小写与分隔符在转录里都不固定。
    """
    target = norm_path(str(path))
    best: tuple[str, Path] | None = None
    for name, root in agent_session_roots():
        prefix = norm_path(str(root))
        if not (target.startswith(prefix + "/") or target == prefix):
            continue
        # 取最长匹配：`.codex/sessions` 与 `.codex/archived_sessions` 都是候选根，
        # 而 `agent_session_roots()` 的返回顺序不保证最长的在前。
        if best is None or len(prefix) > len(norm_path(str(best[1]))):
            best = (name, root)
    return best


def to_placeholder(path: Path) -> str:
    """绝对路径 -> `{CLAUDE_ROOT}/…` 形态。认不出根时返回空串。

    认不出就返回空而不是退回绝对路径：把源机的绝对路径写进包里，会让导入侧
    以为那是个能用的位置，然后在目标机器上悄悄失败。宁可明说「这条路径带不走」。
    """
    hit = _root_for(path)
    if hit is None:
        return ""
    _name, root = hit
    ph = _token_for(root)
    if not ph:
        return ""
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        # resolve 失败（路径不存在、权限、循环链接）时退回字符串切片：
        # 前缀已经比对过，切掉它是安全的。
        target = norm_path(str(path))
        prefix = norm_path(str(root))
        rel = target[len(prefix):].lstrip("/")
    return f"{ph}/{rel}" if rel else ph


def from_placeholder(spec: str) -> Path | None:
    """`{CLAUDE_ROOT}/…` -> 本机绝对路径。本机没有对应根时返回 None。

    这是导入侧的关键一步：**不做字符串替换，而是重新问一次根在哪。**
    目标机器的 `CLAUDE_CONFIG_DIR` 可能指向完全不同的位置，甚至是另一个盘。
    """
    m = _PLACEHOLDER_RX.match(spec.strip())
    if not m:
        return None
    token, rel = f"{{{m.group(1)}}}", (m.group(2) or "").replace("\\", "/")
    for _name, root in agent_session_roots():
        if _token_for(root) == token:
            return root / rel if rel else root
    return None


def _safe_stored_name(path: Path, taken: set[str]) -> str:
    """包内存放用的文件名。只保留安全字符，重名加序号。

    为什么要清洗：这个名字来自转录文件名，而转录文件名在导入侧会被拼进路径。
    不清洗就等于让包的内容决定写到哪——那是路径穿越。
    """
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:120] or "transcript.jsonl"
    if stem not in taken:
        taken.add(stem)
        return stem
    base, dot, ext = stem.rpartition(".")
    head = base if dot else stem
    tail = f".{ext}" if dot else ""
    for i in range(2, 1000):
        cand = f"{head}_{i}{tail}"
        if cand not in taken:
            taken.add(cand)
            return cand
    raise ValueError(f"too many name collisions for {path.name}")


def export_bundle(
    out_dir: Path,
    doc_path: Path | None,
    prompt: str,
    sessions: list[str],
    meta: dict[str, Any],
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
    tr: Any = None,
) -> dict[str, Any]:
    """把交接文档、提示词与勾选的转录打成一个可拷走的目录。

    返回 manifest 内容（同时已写进 `out_dir/manifest.json`）。

    不压缩成单个 zip：目录能让人直接翻看里面有什么，而交接包的价值恰恰在于
    「接手的人能核实内容」。要传输时用户自己 zip 一下即可，那是他们的选择。
    """
    if tr is None:
        # 调用方没给语言时用基准语言，而不是让渲染层拿 None 崩掉。
        # 交接包的四件套是给人读的，缺语言不该变成缺文件。
        from ..i18n import Translator

        tr = Translator()

    out_dir.mkdir(parents=True, exist_ok=True)
    tdir = out_dir / TRANSCRIPTS_DIR
    tdir.mkdir(exist_ok=True)

    rows: list[ExportedSession] = []
    taken: set[str] = set()
    for raw in sessions:
        fp = Path(raw)
        agent = "Codex" if fp.name.lower().startswith("rollout-") else "Claude Code"
        row = ExportedSession(agent=agent, placeholder_path=to_placeholder(fp))
        row.session_id = bare_session_id(fp)
        if not fp.is_file():
            row.skipped_reason = "not-found"
            rows.append(row)
            continue
        try:
            size = fp.stat().st_size
        except OSError as exc:
            row.skipped_reason = f"stat-failed: {exc}"
            rows.append(row)
            continue
        row.size = size
        if size > max_bytes:
            # 超限只跳过**原始副本**，四件套照做。
            #
            # 上限管的是「别把一份 100 MB 的原文拷进包」，而 `session.md` 恰恰是
            # 为这种转录准备的：实测 100 MB 的 rollout 渲染成几百 KB。整条 continue
            # 掉等于「最需要摘要的会话反而什么都没有」——那正好把功能用反了。
            row.skipped_reason = f"too-large: {size} > {max_bytes}"
            _write_session_artifacts(out_dir, row, fp, tr)
            rows.append(row)
            continue
        row.stored_name = _safe_stored_name(fp, taken)
        try:
            shutil.copyfile(fp, tdir / row.stored_name)
        except OSError as exc:
            row.stored_name = ""
            row.skipped_reason = f"copy-failed: {exc}"
        _write_session_artifacts(out_dir, row, fp, tr)
        rows.append(row)

    ddir = out_dir / DOC_DIR
    ddir.mkdir(exist_ok=True)
    doc_name = ""
    if doc_path is not None and doc_path.is_file():
        doc_name = re.sub(r"[^A-Za-z0-9._-]", "_", doc_path.name)[:120]
        shutil.copyfile(doc_path, ddir / doc_name)
    if prompt:
        (ddir / "prompt.txt").write_bytes(prompt.encode("utf-8"))

    manifest: dict[str, Any] = {
        # schema_version 必须是第一个键：读的人（和人眼）先看到它。
        "schema_version": SCHEMA_VERSION,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tool": "agent-handoff",
        "doc": f"{DOC_DIR}/{doc_name}" if doc_name else "",
        "prompt": f"{DOC_DIR}/prompt.txt" if prompt else "",
        "sessions": [r.to_dict() for r in rows],
        "repo": meta,
        # 转录副本刻意**不**脱敏：副本的价值就是原样保真，脱过的转录在另一台
        # 机器上作为「那段工作的记录」已经失真。但这意味着包里可能有用户当时
        # 粘进会话的任何东西——所以把这件事写进 manifest，让任何拿到包的人
        # 第一眼就看到，而不是等出事才知道。
        #
        # 交接文档与提示词是另一回事：它们经过 report 层的脱敏（家目录、
        # 他机用户名、密钥形态），因为它们是**生成物**，保真没有意义。
        "warning": (
            "transcripts/ holds verbatim copies, not redacted. They can contain "
            "anything that was pasted into those sessions - API keys, tokens, "
            "passwords. Review before sharing this bundle."
        ),
    }
    _write_json(out_dir / MANIFEST_NAME, manifest)
    return manifest


def _write_session_artifacts(
    out_dir: Path,
    row: ExportedSession,
    fp: Path,
    tr: Any,
) -> None:
    """给一个会话写出它自己的四件套目录。

    `sessions/<id>/` 里放四样东西，各自回答一个问题：
      · `resume.txt`  —— 怎么无损回到这个会话（**首选**，交接是退路）
      · `session.md`  —— 这个会话说过什么（分级带，可直接粘给新会话）
      · `locate.txt`  —— 工作目录、会话 ID、深度链接（截图里那几个「复制」项）
      · `meta.json`   —— 机器可读的同一批信息，供脚本消费

    分四个文件而不是一个大文档，是因为它们的用途不同：`resume.txt` 是一行命令，
    `session.md` 可能几十万字符。混在一起会让「我只想拿续接命令」变成
    先打开一个大文件再找。

    任何一步失败都只记原因、不抛：四件套是增值产物，它写不出来不该让整个
    交接包失败——转录副本与交接文档才是主体。
    """
    from .transcript import deep_link, merge_tool_runs, read_turns, render_markdown, resume_command

    sid = row.session_id or fp.stem
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:120] or "session"
    sdir = out_dir / SESSIONS_DIR / safe
    try:
        sdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        row.skipped_reason = (row.skipped_reason or "") + f" artifacts-mkdir-failed: {exc}"
        return

    row.resume = resume_command(row.agent, sid)
    row.deep_link = deep_link(row.agent, sid, "")

    try:
        turns = merge_tool_runs(read_turns(row.agent, fp))
    except (OSError, ValueError) as exc:
        turns = []
        row.skipped_reason = (row.skipped_reason or "") + f" parse-failed: {exc}"

    meta = {
        "agent": row.agent,
        "session_id": sid,
        "resume": row.resume,
        "deep_link": row.deep_link,
        "placeholder_path": row.placeholder_path,
        "stored_name": row.stored_name,
        "size": row.size,
        "turns": len(turns),
    }
    try:
        if row.resume:
            (sdir / "resume.txt").write_bytes((row.resume + "\n").encode("utf-8"))
        locate: list[str] = [f"session_id: {sid}"]
        if row.resume:
            locate.append(f"resume: {row.resume}")
        if row.deep_link:
            locate.append(f"deep_link: {row.deep_link}")
        if row.placeholder_path:
            locate.append(f"transcript: {row.placeholder_path}")
        (sdir / "locate.txt").write_bytes(("\n".join(locate) + "\n").encode("utf-8"))
        _write_json(sdir / "meta.json", meta)
        if turns:
            md = render_markdown(row.agent, meta, turns, tr)
            (sdir / "session.md").write_bytes(md.encode("utf-8"))
        row.artifacts_dir = f"{SESSIONS_DIR}/{safe}"
    except OSError as exc:
        row.skipped_reason = (row.skipped_reason or "") + f" artifacts-failed: {exc}"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """确定性地写 JSON：键排序 + LF + UTF-8。

    键排序让两次导出的 manifest 可以直接 diff——包会被放进 git，
    而键序随机的 JSON 每次都显示成整文件改动。
    """
    body = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_bytes(body.encode("utf-8"))


@dataclass
class ImportReport:
    """导入的结果。逐条记录成败，而不是首个异常就中断。

    为什么逐条：一个包里十份转录，第三份坏了不该让其余七份也进不来。
    用户需要看到「哪些进来了、哪些没有、为什么」。
    """

    schema_version: int = 0
    doc: str = ""
    prompt: str = ""
    resolved: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "doc": self.doc,
            "prompt": self.prompt,
            "resolved": self.resolved,
            "problems": self.problems,
        }


def read_manifest(bundle: Path) -> tuple[dict[str, Any], str]:
    """读并粗校验 manifest。返回 (数据, 错误说明)；错误非空时数据不可用。"""
    mf = bundle / MANIFEST_NAME
    if not mf.is_file():
        return {}, f"not a handoff bundle: no {MANIFEST_NAME} in {bundle}"
    try:
        raw = mf.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError 不是 OSError 也不是 JSONDecodeError，必须显式列出——
        # 一个被 GBK 编辑器另存过的 manifest 否则会让整个导入崩掉。
        return {}, f"cannot read {MANIFEST_NAME}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"{MANIFEST_NAME} is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return {}, f"{MANIFEST_NAME} must contain an object"

    ver = data.get("schema_version")
    if not isinstance(ver, int):
        return {}, "manifest has no integer schema_version; refusing to guess"
    if ver > SCHEMA_VERSION:
        # 版本比自己新时明确引导升级，而不是反复报解析失败——
        # 后者会让用户以为包坏了，然后去修一个没坏的东西。
        return {}, (
            f"bundle schema_version {ver} is newer than this build supports "
            f"({SCHEMA_VERSION}); upgrade agent-handoff to read it"
        )
    return data, ""


def import_bundle(bundle: Path) -> ImportReport:
    """解析一个包，把里面的占位符路径解析到本机，报告每一条的结果。

    **不复制任何文件到 agent 的数据目录。** 转录副本留在包里，报告只说明
    「这一条在本机对应哪个位置」。把别处的转录塞进 agent 的数据目录是有副作用的
    动作（会影响那个 app 的会话列表），必须由用户明确决定，工具不代劳。
    """
    rep = ImportReport()
    data, err = read_manifest(bundle)
    if err:
        rep.problems.append(err)
        return rep

    rep.schema_version = int(data.get("schema_version") or 0)
    for key in ("doc", "prompt"):
        rel = data.get(key)
        if not isinstance(rel, str) or not rel:
            continue
        # 包内路径必须留在包内。`..` 或绝对路径一律拒收：manifest 的内容
        # 决定读哪个文件，不设这道闸就等于让包读走机器上任意文件。
        target = _inside(bundle, rel)
        if target is None:
            rep.problems.append(f"{key} path escapes the bundle: {rel}")
        elif not target.is_file():
            rep.problems.append(f"{key} listed but missing: {rel}")
        else:
            setattr(rep, key, str(target))

    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        rep.problems.append("manifest has no sessions list")
        return rep

    for item in sessions:
        if not isinstance(item, dict):
            rep.problems.append(f"session entry is not an object: {item!r}")
            continue
        row: dict[str, Any] = {
            "agent": str(item.get("agent") or "")[:40],
            "session_id": str(item.get("session_id") or "")[:200],
            "placeholder_path": str(item.get("placeholder_path") or "")[:400],
        }
        stored = item.get("stored_name")
        if isinstance(stored, str) and stored:
            copy = _inside(bundle, f"{TRANSCRIPTS_DIR}/{stored}")
            if copy is None:
                rep.problems.append(f"stored_name escapes the bundle: {stored}")
            elif copy.is_file():
                row["bundled_copy"] = str(copy)
            else:
                rep.problems.append(f"stored transcript missing: {stored}")
        if row["placeholder_path"]:
            local = from_placeholder(row["placeholder_path"])
            if local is None:
                row["local_path"] = ""
                row["note"] = "no matching agent root on this machine"
            else:
                row["local_path"] = str(local)
                row["exists_locally"] = local.is_file()
        skipped = item.get("skipped_reason")
        if isinstance(skipped, str) and skipped:
            row["skipped_reason"] = skipped[:200]
        rep.resolved.append(row)
    return rep


def _inside(root: Path, rel: str) -> Path | None:
    """把包内相对路径解析成绝对路径，越界返回 None。

    `..`、绝对路径、符号链接指向包外——全部拒收。manifest 是**外部输入**，
    它说读哪个文件就读哪个文件的话，一个精心构造的包能读走机器上任意文件。
    """
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return None
    try:
        target = (root / rel).resolve()
        base = root.resolve()
    except OSError:
        return None
    return target if target == base or base in target.parents else None


def default_bundle_dir(repo: Path, stamp_day: str) -> Path:
    """包的默认位置：仓库外的家目录下，按仓库名 + 日期。

    刻意放在**仓库外**：包里有转录副本，而转录可能含用户在会话里粘过的任何东西。
    默认写进仓库就等于默认把它交给 `git add -A`——那正是这个工具在别处
    极力避免的事。要放进仓库让用户显式指定 `--export-bundle <路径>`。
    """
    home = Path(os.path.expanduser("~"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", repo.name)[:60] or "repo"
    return home / ".agent-handoff" / "bundles" / f"{name}-{stamp_day}"


__all__ = [
    "DOC_DIR",
    "MANIFEST_NAME",
    "MAX_TRANSCRIPT_BYTES",
    "PLACEHOLDERS",
    "SCHEMA_VERSION",
    "TRANSCRIPTS_DIR",
    "ExportedSession",
    "ImportReport",
    "default_bundle_dir",
    "export_bundle",
    "from_placeholder",
    "import_bundle",
    "read_manifest",
    "to_placeholder",
]
