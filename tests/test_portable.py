#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨机器交接包。重点：

  · 路径必须存成占位符，导入时**重新解析**而不是字符串替换
  · 转录本体要带走——只给路径的交接文档在另一台机器上必然失效
  · manifest 是外部输入：路径穿越、版本过新、编码损坏都要挡住
  · 逐条报告成败，一份坏转录不能让其余的进不来
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_handoff.core.portable import (
    DOC_DIR,
    MANIFEST_NAME,
    SCHEMA_VERSION,
    TRANSCRIPTS_DIR,
    default_bundle_dir,
    export_bundle,
    from_placeholder,
    import_bundle,
    read_manifest,
    to_placeholder,
)


@pytest.fixture()
def fake_roots(tmp_path: Path, monkeypatch):
    """把两个 agent 的数据目录指到 tmp 下，返回 (claude_projects, codex_sessions)。"""
    claude = tmp_path / "home" / ".claude"
    codex = tmp_path / "home" / ".codex"
    (claude / "projects").mkdir(parents=True)
    (codex / "sessions").mkdir(parents=True)
    (codex / "archived_sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return claude / "projects", codex / "sessions"


# ── 占位符往返 ────────────────────────────────────────────────────────

def test_placeholder_roundtrip(fake_roots):
    """绝对路径 -> 占位符 -> 绝对路径，必须回到原处。"""
    projects, sessions = fake_roots
    for original in (
        projects / "C--Users-x-proj" / "abc.jsonl",
        sessions / "2026" / "08" / "rollout-x.jsonl",
    ):
        spec = to_placeholder(original)
        assert spec.startswith("{"), f"没有换成占位符: {spec}"
        back = from_placeholder(spec)
        assert back is not None
        assert str(back).replace("\\", "/").lower() == str(original).replace("\\", "/").lower()


def test_archived_and_live_roots_get_different_placeholders(fake_roots, tmp_path):
    """`sessions` 与 `archived_sessions` 不能共用一个占位符。

    共用的后果是归档转录在导入侧被解析到 `sessions/` 下——路径语法合法，
    所以不报错，只是**静默指向错误位置**。这类「看起来成功了」的错误最难发现。
    """
    _projects, sessions = fake_roots
    archived = sessions.parent / "archived_sessions"
    live_spec = to_placeholder(sessions / "2026" / "a.jsonl")
    arch_spec = to_placeholder(archived / "2026" / "a.jsonl")
    assert live_spec != arch_spec, "两个根必须映射到不同的占位符"

    back = from_placeholder(arch_spec)
    assert back is not None
    assert "archived_sessions" in str(back).replace("\\", "/")


def test_unknown_path_gets_no_placeholder(fake_roots, tmp_path):
    """认不出根时返回空串，而不是退回源机的绝对路径。

    退回绝对路径会让导入侧以为那是个能用的位置，然后在目标机器上悄悄失败。
    宁可明说「这条路径带不走」。
    """
    assert to_placeholder(tmp_path / "elsewhere" / "x.jsonl") == ""


def test_placeholder_resolves_against_the_target_machine(fake_roots, tmp_path, monkeypatch):
    """换机器的核心语义：同一个占位符，跟着目标机器的根走。

    这就是为什么不能存绝对路径再做字符串替换——目标机器的 `CLAUDE_CONFIG_DIR`
    可能指向完全不同的位置，甚至另一个盘。
    """
    projects, _sessions = fake_roots
    spec = to_placeholder(projects / "slug" / "a.jsonl")

    other = tmp_path / "othermachine" / ".claude"
    (other / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))

    back = from_placeholder(spec)
    assert back is not None
    assert str(back).startswith(str(other)), f"没有跟着新根走: {back}"


def test_no_matching_root_returns_none(fake_roots, monkeypatch, tmp_path):
    """本机没有对应根时返回 None，不编造路径。"""
    projects, _ = fake_roots
    spec = to_placeholder(projects / "slug" / "a.jsonl")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nope2"))
    assert from_placeholder(spec) is None


# ── 导出 ──────────────────────────────────────────────────────────────

def test_export_carries_the_transcript_body(fake_roots, tmp_path):
    """包里必须有转录**副本**，不能只有路径。

    只给路径是「换机必然失效」的根本原因：那些路径里编码着源机的 cwd。
    """
    projects, _ = fake_roots
    t = projects / "slug" / "abc.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_bytes(b'{"role":"user"}\n')

    doc = tmp_path / "handoff.md"
    doc.write_text("# doc\n", encoding="utf-8")
    out = tmp_path / "bundle"

    mf = export_bundle(out, doc, "PROMPT", [str(t)], {"name": "r"})
    assert mf["schema_version"] == SCHEMA_VERSION
    stored = mf["sessions"][0]["stored_name"]
    copied = out / TRANSCRIPTS_DIR / stored
    assert copied.read_bytes() == b'{"role":"user"}\n', "副本内容必须一字不差"
    assert (out / DOC_DIR / doc.name).is_file()
    assert (out / DOC_DIR / "prompt.txt").read_text(encoding="utf-8") == "PROMPT"
    assert mf["sessions"][0]["placeholder_path"].startswith("{")


def test_export_stores_no_absolute_repo_path(fake_roots, tmp_path):
    """包里不该有仓库的本机绝对路径——那正是换机之后失效的东西。"""
    doc = tmp_path / "h.md"
    doc.write_text("x\n", encoding="utf-8")
    out = tmp_path / "b"
    export_bundle(out, doc, "p", [], {"name": "r", "head": "abc", "remote": ""})
    raw = (out / MANIFEST_NAME).read_text(encoding="utf-8")
    assert str(tmp_path) not in raw, "manifest 里出现了本机绝对路径"


def test_export_skips_oversized_transcript_but_keeps_the_record(fake_roots, tmp_path):
    """超限的转录不带正文，但路径与 id 要留下——用户可以自己单独拷那一份。"""
    projects, _ = fake_roots
    big = projects / "slug" / "big.jsonl"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * 4096)
    out = tmp_path / "b"
    mf = export_bundle(out, None, "", [str(big)], {}, max_bytes=1024)
    row = mf["sessions"][0]
    assert not row.get("stored_name"), "超限不该复制正文"
    assert "too-large" in row["skipped_reason"]
    assert row["placeholder_path"].startswith("{"), "路径记录仍要保留"


def test_export_records_a_missing_transcript(fake_roots, tmp_path):
    """勾了却找不到的转录要留记录，不能静默消失。"""
    projects, _ = fake_roots
    out = tmp_path / "b"
    mf = export_bundle(out, None, "", [str(projects / "gone.jsonl")], {})
    assert mf["sessions"][0]["skipped_reason"] == "not-found"


def test_export_deduplicates_stored_names(fake_roots, tmp_path):
    """不同目录下的同名转录不能互相覆盖。"""
    projects, _ = fake_roots
    names = []
    paths = []
    for sub in ("a", "b"):
        p = projects / sub / "same.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(sub.encode())
        paths.append(str(p))
    mf = export_bundle(tmp_path / "b", None, "", paths, {})
    names = [s["stored_name"] for s in mf["sessions"]]
    assert len(set(names)) == 2, f"存名撞了: {names}"


def test_manifest_json_is_deterministic(fake_roots, tmp_path):
    """键排序 + LF：包会进 git，键序随机的 JSON 每次都显示成整文件改动。"""
    out = tmp_path / "b"
    export_bundle(out, None, "", [], {"name": "r"})
    raw = (out / MANIFEST_NAME).read_bytes()
    assert b"\r\n" not in raw, "必须是 LF"
    data = json.loads(raw.decode("utf-8"))
    assert list(data) == sorted(data), "键必须排序"


# ── 导入 ──────────────────────────────────────────────────────────────

def test_import_reports_each_session(fake_roots, tmp_path):
    projects, _ = fake_roots
    t = projects / "slug" / "abc.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_bytes(b"{}\n")
    out = tmp_path / "b"
    export_bundle(out, None, "P", [str(t)], {})

    rep = import_bundle(out)
    assert rep.schema_version == SCHEMA_VERSION
    assert not rep.problems, rep.problems
    row = rep.resolved[0]
    assert row["exists_locally"] is True
    assert Path(row["bundled_copy"]).is_file()


def test_import_does_not_write_into_agent_dirs(fake_roots, tmp_path):
    """导入是只读的：不往 agent 数据目录塞任何东西。

    把别处的转录放进 `~/.claude` 会改变那个 app 的会话列表，是有副作用的动作，
    必须由用户看过清单后自己决定。Claude Code 自 v2.1.205 起也明确禁止
    篡改会话转录文件。
    """
    projects, sessions = fake_roots
    t = projects / "slug" / "a.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_bytes(b"{}\n")
    out = tmp_path / "b"
    export_bundle(out, None, "", [str(t)], {})

    before = set(projects.rglob("*")) | set(sessions.rglob("*"))
    import_bundle(out)
    after = set(projects.rglob("*")) | set(sessions.rglob("*"))
    assert before == after, "导入动了 agent 数据目录"


def test_import_rejects_a_newer_schema(tmp_path):
    """版本比自己新时明确引导升级，而不是硬着头皮解析。

    猜一个未知格式的字段含义，比明确说「我不认识这个版本」危险得多。
    """
    b = tmp_path / "b"
    b.mkdir()
    (b / MANIFEST_NAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1, "sessions": []}),
        encoding="utf-8",
    )
    _data, err = read_manifest(b)
    assert "newer" in err and "upgrade" in err
    rep = import_bundle(b)
    assert rep.problems and not rep.resolved


def test_import_requires_an_integer_schema_version(tmp_path):
    b = tmp_path / "b"
    b.mkdir()
    (b / MANIFEST_NAME).write_text(json.dumps({"sessions": []}), encoding="utf-8")
    _data, err = read_manifest(b)
    assert "schema_version" in err


@pytest.mark.parametrize(
    "escape",
    ["../outside.md", "..\\outside.md", "/etc/passwd", "C:\\Windows\\x.md"],
)
def test_import_refuses_paths_outside_the_bundle(tmp_path, escape):
    """manifest 是外部输入。它说读哪个文件就读哪个的话，
    一个精心构造的包能读走机器上任意文件。"""
    b = tmp_path / "b"
    b.mkdir()
    (b / MANIFEST_NAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "doc": escape, "sessions": []}),
        encoding="utf-8",
    )
    rep = import_bundle(b)
    assert rep.doc == "", f"越界路径被接受了: {escape}"
    assert any("escape" in p or "missing" in p for p in rep.problems)


def test_import_refuses_stored_name_escape(tmp_path):
    b = tmp_path / "b"
    (b / TRANSCRIPTS_DIR).mkdir(parents=True)
    (b / MANIFEST_NAME).write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "sessions": [{"agent": "Codex", "stored_name": "../../secret.jsonl"}],
        }),
        encoding="utf-8",
    )
    rep = import_bundle(b)
    assert not rep.resolved[0].get("bundled_copy")
    assert any("escape" in p for p in rep.problems)


def test_import_survives_a_bad_encoding(tmp_path):
    """被 GBK 编辑器另存过的 manifest 不能让整个导入崩掉。

    `UnicodeDecodeError` 的 MRO 是 `ValueError`——既不是 `OSError` 也不是
    `JSONDecodeError`，所以必须显式列出来捕获。
    """
    b = tmp_path / "b"
    b.mkdir()
    (b / MANIFEST_NAME).write_bytes(b'{"schema_version":1,"doc":"\xff\xfe\x00bad"}')
    _data, err = read_manifest(b)
    assert err, "应当报错而不是抛异常"


def test_import_reports_a_broken_entry_without_dropping_the_rest(fake_roots, tmp_path):
    """一份坏转录不能让其余的进不来。"""
    projects, _ = fake_roots
    good = projects / "slug" / "good.jsonl"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(b"{}\n")
    out = tmp_path / "b"
    export_bundle(out, None, "", [str(good)], {})

    data = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    data["sessions"].append("not-an-object")
    data["sessions"].append({"agent": "Codex", "stored_name": "gone.jsonl"})
    (out / MANIFEST_NAME).write_text(json.dumps(data), encoding="utf-8")

    rep = import_bundle(out)
    assert len(rep.resolved) == 2, "合法条目仍要被解析出来"
    assert len(rep.problems) >= 2
    assert Path(rep.resolved[0]["bundled_copy"]).is_file()


def test_import_rejects_a_non_bundle_directory(tmp_path):
    rep = import_bundle(tmp_path)
    assert rep.problems and MANIFEST_NAME in rep.problems[0]


# ── 默认位置 ──────────────────────────────────────────────────────────

def test_default_bundle_dir_stays_out_of_the_repo(tmp_path):
    """默认位置刻意在仓库外：包里有转录副本，而转录可能含任何粘进会话的东西。

    默认写进仓库就等于默认把它交给 `git add -A`——那正是这个工具在别处
    极力避免的事。
    """
    repo = tmp_path / "myrepo"
    d = default_bundle_dir(repo, "2026-08-23")
    assert repo not in d.parents and d != repo
    assert d.name == "myrepo-2026-08-23"


# ── GUI 贯通 ──────────────────────────────────────────────────────────
#
# `_run_job` 是网页界面唯一的执行入口，此前完全没有测试。打包参数要经它传下去，
# 而后台线程里的异常只会变成一个 job 状态——不测就只能靠人点界面才发现断链。

def _mkrepo(root: Path) -> Path:
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@e.c"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    (root / "mod.py").write_text("a = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(root), capture_output=True, text=True)
    return root


def test_gui_job_exports_a_bundle(tmp_path, fake_roots):
    """网页界面勾了「打包」之后，包必须真的出现。"""
    from agent_handoff.core.handoff import Options
    from agent_handoff.gui import server as S
    from agent_handoff.i18n import Translator

    repo = _mkrepo(tmp_path / "guirepo")
    target = tmp_path / "guibundle"
    jid = S._new_job()
    S._run_job(
        jid,
        Options(repo=repo, no_commit=True, skip_tests=True, no_vitals=True),
        Translator("en"),
        bundle=str(target),
    )
    job = S._jobs[jid]
    assert job["state"] == "done", job.get("result")
    assert job["result"]["bundle"] == str(target)
    assert (target / MANIFEST_NAME).is_file()
    assert (target / DOC_DIR / "prompt.txt").is_file()


def test_gui_job_without_bundle_writes_nothing_extra(tmp_path, fake_roots):
    """不勾就不打包——默认行为必须与加这个功能之前一致。"""
    from agent_handoff.core.handoff import Options
    from agent_handoff.gui import server as S
    from agent_handoff.i18n import Translator

    repo = _mkrepo(tmp_path / "nob")
    jid = S._new_job()
    S._run_job(
        jid,
        Options(repo=repo, no_commit=True, skip_tests=True, no_vitals=True),
        Translator("en"),
        bundle=None,
    )
    job = S._jobs[jid]
    assert job["state"] == "done", job.get("result")
    assert "bundle" not in job["result"]


def test_gui_job_survives_an_unwritable_bundle_target(tmp_path, fake_roots, monkeypatch):
    """打包失败不能把整个任务标成 error。

    交接本身已经完成，包是附加产物。为了打包失败而让任务显示成失败，
    会让用户以为交接也没成——那比没有包糟得多。
    """
    from agent_handoff.core import portable as P
    from agent_handoff.core.handoff import Options
    from agent_handoff.gui import server as S
    from agent_handoff.i18n import Translator

    repo = _mkrepo(tmp_path / "failb")

    def boom(**kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(P, "export_bundle", boom)
    jid = S._new_job()
    S._run_job(
        jid,
        Options(repo=repo, no_commit=True, skip_tests=True, no_vitals=True),
        Translator("en"),
        bundle=str(tmp_path / "nope"),
    )
    job = S._jobs[jid]
    assert job["state"] == "done", "打包失败不该改变任务状态"
    assert "bundle" not in job["result"]
    assert any("read-only" in line for line in job["log"]), job["log"]


def test_manifest_warns_that_copies_are_verbatim(fake_roots, tmp_path):
    """包里必须写明转录副本没脱敏。

    副本刻意保真——脱过的转录作为「那段工作的记录」已经失真。但这意味着包里
    可能有用户当时粘进会话的密钥、令牌、口令。把这件事写进 manifest，
    让任何拿到包的人第一眼看到，而不是等出事才知道。
    """
    out = tmp_path / "b"
    mf = export_bundle(out, None, "", [], {})
    warn = mf.get("warning", "")
    assert "verbatim" in warn and "redact" in warn
    # manifest 落盘的那份也要有，不只是返回值。
    on_disk = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert on_disk.get("warning") == warn


def test_verbatim_warning_only_fires_when_copies_were_carried(fake_roots, tmp_path, capsys):
    """没带副本却警告「里面有敏感内容」会训练用户忽略提示，那比不提示更糟。"""
    import argparse

    from agent_handoff.cli import _do_export
    from agent_handoff.core.handoff import Result
    from agent_handoff.i18n import Translator

    tr = Translator("en")
    projects, _ = fake_roots
    t = projects / "s" / "a.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_bytes(b"{}\n")

    doc = tmp_path / "h.md"
    doc.write_text("x\n", encoding="utf-8")
    res = Result(code=0, out_path=str(doc), prompt="p", ctx={"repo_name": "r"})

    def run(sessions: list[str], where: str) -> str:
        ns = argparse.Namespace(
            repo=str(tmp_path), export_bundle=str(tmp_path / where), json=False
        )
        _do_export(ns, res, sessions, tr)
        return capsys.readouterr().out

    # 用文案里的实际句子判定，不用键名的一部分：pytest 的临时目录名会包含
    # 测试函数名（含 "verbatim"），拿那个词做包含检查会误命中输出里的路径。
    needle = tr.t("cli.bundle.verbatim").strip()[:40]

    with_copy = run([str(t)], "withcopy")
    assert needle in with_copy, "带了副本必须警告"

    without = run([], "nocopy")
    assert needle not in without, "没带副本不该警告"
