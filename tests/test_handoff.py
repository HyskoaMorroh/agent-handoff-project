#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端交接流程 + 报告渲染 + CLI。重点：
  · 三步都真的发生了，且 dry-run 一个字节都不写
  · 提示词五块内容齐全（现场 / 先读计划 / 别重做 / 缺口 / 过期声明）
  · 时间戳全程一致（原版调六次 now()，跨午夜会自相矛盾）
  · 并发时退出码 3，且没有任何写入
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from agent_handoff.core.gitops import git, head_sha
from agent_handoff.core.handoff import EXIT_BAD_INPUT, EXIT_CONCURRENT, EXIT_OK, Options, run_handoff
from agent_handoff.core.report import _redact, _redact_home, build_handoff
from agent_handoff.i18n import Translator


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True)


def _run(repo: Path, tr, **kw):
    opts = Options(repo=repo, skip_tests=kw.pop("skip_tests", True), no_vitals=kw.pop("no_vitals", True), **kw)
    return run_handoff(opts, tr)


# ── 前置校验 ──────────────────────────────────────────────────────────

def test_rejects_non_directory(tmp_path: Path, tr):
    f = tmp_path / "afile.txt"
    f.write_text("x\n", encoding="utf-8")
    res = _run(f, tr)
    assert res.code == EXIT_BAD_INPUT and "not a directory" in res.error


def test_accepts_non_repo_with_degraded_git_steps(tmp_path: Path, tr):
    """缺 git 只让 git 相关的步骤降级，不再整体拒绝。

    这个测试原先断言 EXIT_BAD_INPUT + "not a git repository"。那个行为是错的：
    会话传承、完成度评估、测试取证都不碰 git，把它们一起挡掉让工具在「还没
    git init 的工作目录」上完全不可用——而那是交接最常见的场景之一。
    """
    d = tmp_path / "plain"
    d.mkdir()
    res = _run(d, tr, no_commit=True)
    assert res.code == EXIT_OK
    assert res.ctx["has_git"] is False


# ── 三步都发生了 ──────────────────────────────────────────────────────

def test_full_run_commits_backfills_and_writes(repo: Path, tr):
    (repo / "pkg" / "extra.py").write_text("w = 1\n", encoding="utf-8")
    before = head_sha(repo)
    res = _run(repo, tr)

    assert res.code == EXIT_OK
    # 1. 提交快照
    assert head_sha(repo) != before
    assert "pkg/extra.py" in git(repo, "ls-files")
    # 2. 回填计划（Task 1 完成 -> 两步勾上）
    plan = (repo / "docs" / "plan.md").read_text(encoding="utf-8")
    assert plan.count("- [x]") == 2
    assert res.ctx["ticked"] == 2 and res.ctx["total_steps"] == 4
    # 3. 生成交接文件
    out = Path(res.out_path)
    assert out.is_file() and out.name.endswith("-handoff.md")
    assert "handoff" in out.read_text(encoding="utf-8").lower()


def test_handoff_file_and_plan_are_committed(repo: Path, tr):
    res = _run(repo, tr)
    tracked = git(repo, "ls-files")
    assert Path(res.out_path).name in tracked
    assert git(repo, "status", "--porcelain") == ""


def test_dry_run_writes_absolutely_nothing(repo: Path, tr):
    (repo / "pkg" / "extra.py").write_text("w = 1\n", encoding="utf-8")
    before_head = head_sha(repo)
    before_plan = (repo / "docs" / "plan.md").read_bytes()

    res = _run(repo, tr, dry_run=True)

    assert res.code == EXIT_OK
    assert head_sha(repo) == before_head
    assert (repo / "docs" / "plan.md").read_bytes() == before_plan
    assert not Path(res.out_path).exists()
    assert res.body and res.prompt  # 但内容照样生成了


def test_no_commit_still_writes_handoff(repo: Path, tr):
    before = head_sha(repo)
    res = _run(repo, tr, no_commit=True)
    assert res.code == EXIT_OK
    assert head_sha(repo) == before
    assert Path(res.out_path).is_file()
    assert res.ctx["commit_result"] == tr.t("cli.commit.skipped")


def test_protected_files_stay_untracked(repo: Path, tr):
    (repo / "secret.key").write_text("TOPSECRET\n", encoding="utf-8")
    (repo / "docs" / "LOGO.jpg").write_bytes(b"\xff\xd8jpeg")
    res = _run(repo, tr)
    tracked = git(repo, "ls-files")
    assert "secret.key" not in tracked
    assert "docs/LOGO.jpg" not in tracked
    assert set(res.ctx["protected"]) == {"docs/LOGO.jpg", "secret.key"}


def test_custom_out_path(repo: Path, tr):
    res = _run(repo, tr, out="notes/my-handoff.md")
    assert Path(res.out_path) == repo / "notes" / "my-handoff.md"
    assert Path(res.out_path).is_file()


def test_explicit_plan_path(repo: Path, tr):
    res = _run(repo, tr, plan="docs/plan.md")
    assert res.ctx["plan_rel"] == "docs/plan.md"


def test_missing_plan_still_produces_handoff(tmp_path: Path, tr):
    r = tmp_path / "bare"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@e.c")
    _git(r, "config", "user.name", "T")
    (r / "x.py").write_text("a = 1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")

    res = _run(r, tr)
    assert res.code == EXIT_OK
    assert res.ctx["plan_rel"] == ""
    assert res.ctx["report"] == {}
    assert Path(res.out_path).is_file()
    assert tr.t("doc.plan_missing") in res.body


# ── 并发保护 ──────────────────────────────────────────────────────────

def test_concurrency_stops_with_exit_3_and_writes_nothing(repo: Path, tr):
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "pkg/other.py")  # 模拟另一个会话暂存了但没提交
    before = head_sha(repo)
    before_plan = (repo / "docs" / "plan.md").read_bytes()

    res = _run(repo, tr)

    assert res.code == EXIT_CONCURRENT
    assert res.conflicts
    assert head_sha(repo) == before
    assert (repo / "docs" / "plan.md").read_bytes() == before_plan
    assert not list((repo / "docs").glob("*-handoff.md"))


def test_force_overrides_concurrency_but_keeps_protection(repo: Path, tr):
    """--force 只放过并发警告，不放过受保护文件。"""
    (repo / "secret.key").write_text("s\n", encoding="utf-8")
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "pkg/other.py")

    res = _run(repo, tr, force=True)

    assert res.code == EXIT_OK
    assert res.conflicts, "警告仍应被报告"
    assert "secret.key" not in git(repo, "ls-files")
    assert "pkg/other.py" in git(repo, "ls-files")


def test_no_commit_bypasses_concurrency_stop(repo: Path, tr):
    """只分析不写 git 时，并发不构成危险，应继续。"""
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "pkg/other.py")
    res = _run(repo, tr, no_commit=True)
    assert res.code == EXIT_OK
    assert res.conflicts


def test_dry_run_bypasses_concurrency_stop(repo: Path, tr):
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "pkg/other.py")
    res = _run(repo, tr, dry_run=True)
    assert res.code == EXIT_OK


def test_own_previous_outputs_do_not_trigger_concurrency(repo: Path, tr):
    """连跑两次不该在第二次被自己的产物挡住——原版的两分钟窗口会误报。"""
    first = _run(repo, tr)
    assert first.code == EXIT_OK
    (repo / "pkg" / "more.py").write_text("m = 1\n", encoding="utf-8")
    second = _run(repo, tr)
    assert second.code == EXIT_OK, second.conflicts


def test_protected_file_edit_is_not_a_concurrency_signal(repo: Path, tr):
    """受保护文件永不提交，它被改动跟"另一个会话在写代码"无关。

    报出来只会让用户以为有冲突而去加 --force，而 --force 会把真正的阻断
    信号一起放过。这是比误报本身更坏的二阶后果。
    """
    (repo / "docs" / "LOGO.jpg").write_bytes(b"\xff\xd8new-logo-bytes")
    res = _run(repo, tr)
    assert res.code == EXIT_OK
    assert not any("LOGO" in c for c in res.conflicts), res.conflicts


# ── 时间戳一致性 ──────────────────────────────────────────────────────

def test_timestamps_are_consistent(repo: Path, tr):
    """原版在六处各调一次 now()：提交信息、文件名、文档标题、过期声明可以互相错开，
    跨午夜时文件名日期与标题日期还会不一致。"""
    res = _run(repo, tr)
    day = res.ctx["date"]
    assert Path(res.out_path).name == f"{day}-handoff.md"
    assert res.ctx["now"].startswith(day)
    assert day in res.body.splitlines()[0]
    assert res.ctx["now"] in res.prompt


# ── 提示词五块 ────────────────────────────────────────────────────────

def test_prompt_has_scene_coordinates(repo: Path, tr):
    res = _run(repo, tr)
    assert res.ctx["repo"] in res.prompt
    assert res.ctx["branch"] in res.prompt
    assert res.ctx["head_sha"] in res.prompt


def test_prompt_names_intent_sections(repo: Path, tr):
    res = _run(repo, tr)
    assert "docs/plan.md" in res.prompt
    assert "Goal" in res.prompt
    assert "Global Constraints" in res.prompt


def test_prompt_lists_done_tasks_by_name(repo: Path, tr):
    """原版只说"已完成 N 步"，新会话不知道哪几个任务不用重做。"""
    res = _run(repo, tr)
    assert "Task 1" in res.prompt
    assert res.ctx["done_tasks"] == [1]


def test_prompt_sentences_are_separated(repo: Path, tr):
    """进度句与"不要重做"句直接相接会粘成 `1 left.Do not redo Task 1.`。

    三种语言句末标点不同（。/ .），空格必须在拼接处补，不能写进模板。
    """
    res = _run(repo, tr)
    line = next(ln for ln in res.prompt.splitlines() if "Do not redo" in ln)
    assert ". Do not redo" in line or "。 Do not redo" in line, line
    assert not re.search(r"[.。]\S", line.replace("...", "")), line


def test_prompt_sentence_separation_in_all_languages(repo: Path):
    """繁中与简中的句末是「。」，英文是「.」。三种都不能粘连。"""
    from agent_handoff.i18n import Translator

    for lang in ("zh-Hans", "zh-Hant", "en"):
        t2 = Translator(lang)
        res = _run(repo, t2)
        redo = t2.t("prompt.dont_redo", tasks="Task 1")
        line = next((ln for ln in res.prompt.splitlines() if redo in ln), None)
        assert line, f"{lang}: 找不到 dont_redo 行"
        assert " " + redo in line, f"{lang}: 两句之间缺空格 -> {line}"


def test_prompt_lists_concrete_gaps(repo: Path, tr):
    res = _run(repo, tr)
    assert "pkg/ui.py" in res.prompt
    assert "render_ui" in res.prompt


def test_prompt_names_protected_files(repo: Path, tr):
    res = _run(repo, tr)
    assert "docs/LOGO.jpg" in res.prompt
    assert "secret.key" in res.prompt


def test_prompt_carries_expiry_and_head(repo: Path, tr):
    res = _run(repo, tr)
    assert res.ctx["head_sha"] in res.prompt
    assert res.ctx["now"] in res.prompt


def test_prompt_head_is_reachable(repo: Path, tr):
    """提示词里的 HEAD 必须是真实存在的提交，否则新会话 checkout 会失败。"""
    res = _run(repo, tr)
    sha = res.ctx["head_sha"]
    assert git(repo, "rev-parse", "--verify", sha)


# ── 报告渲染 ──────────────────────────────────────────────────────────

def test_handoff_body_sections(repo: Path, tr):
    res = _run(repo, tr)
    body = res.body
    for key in ("doc.h.scene", "doc.h.step1", "doc.h.step2", "doc.h.step3", "doc.h.prompt"):
        assert tr.t(key) in body


def test_handoff_body_table_and_verdicts(repo: Path, tr):
    res = _run(repo, tr)
    assert tr.t("doc.table.head") in res.body
    assert tr.t("doc.verdict.done") in res.body
    assert tr.t("doc.verdict.none").replace("*", "") in res.body.replace("*", "")


@pytest.mark.parametrize("lang", ["zh-Hans", "zh-Hant", "en"])
def test_handoff_tables_have_matching_separator_row(repo: Path, lang):
    """分隔行的列数必须等于表头，否则 Markdown 不渲染成表格。

    表头文案来自 i18n，三种语言各有一份；写死分隔行会在某一语言改列数时静默失配。
    体征表默认不渲染（no_vitals），所以这里显式塞一行体征进 ctx 再重渲染，
    确保两张表都真的被检查到——否则测试会在「文档里没这张表」的情况下空转通过。
    """
    tr = Translator(lang)
    res = _run(repo, tr)
    ctx = dict(res.ctx)
    ctx["vitals"] = [{
        "agent": "Codex", "session_id": "01a02005", "file": "rollout-x.jsonl",
        "mb": 70.5, "fatal": 30, "aborted": 0, "errors": 39, "band": "critical",
    }]
    body = build_handoff(ctx, tr)

    checked = []
    lines = body.splitlines()
    for i, line in enumerate(lines[:-1]):
        nxt = lines[i + 1]
        if not line.startswith("|") or not nxt.startswith("|"):
            continue
        if set(nxt.strip()) - set("|- :"):
            continue
        head_cells = line.strip().strip("|").split("|")
        rule_cells = nxt.strip().strip("|").split("|")
        checked.append(line)
        assert len(head_cells) == len(rule_cells), (
            f"{lang}: 表头 {len(head_cells)} 列，分隔行 {len(rule_cells)} 列\n{line}\n{nxt}"
        )
    assert tr.t("doc.vitals.head") in checked, f"{lang}: 体征表没被检查到\n{checked}"
    assert tr.t("doc.table.head") in checked, f"{lang}: 完成度表没被检查到\n{checked}"


def test_handoff_body_escapes_pipe_in_task_title(tmp_path: Path, tr):
    """任务标题里的竖线会把 Markdown 表格拆散。"""
    r = tmp_path / "p"
    (r / "docs").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@e.c")
    _git(r, "config", "user.name", "T")
    (r / "docs" / "plan.md").write_text(
        "### Task 1: a | b | c\n\n**Files:**\n- Create: `z.py`\n\n**Steps:**\n"
        "- [ ] **Step 1** x\n- [ ] **Step 2** y\n- [ ] **Step 3** z\n",
        encoding="utf-8",
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "i")
    res = _run(r, tr, no_commit=True)
    rows = [ln for ln in res.body.splitlines() if ln.startswith("| 1 ")]
    assert rows, res.body
    # 未转义的竖线才是单元格分隔符；转义后这一行必须仍是 5 格。
    cells = re.split(r"(?<!\\)\|", rows[0])
    assert len(cells) == 7, cells  # 首尾各一个空串 + 5 个单元格
    assert r"a \| b \| c" in cells[1]


def test_handoff_body_lists_gaps(repo: Path, tr):
    res = _run(repo, tr)
    assert tr.t("doc.h.gaps") in res.body
    assert "pkg/ui.py" in res.body
    assert "render_ui" in res.body


def test_handoff_body_embeds_prompt(repo: Path, tr):
    res = _run(repo, tr)
    assert "```text" in res.body
    assert res.prompt in res.body


def test_handoff_warns_plan_is_not_substitute(repo: Path, tr):
    res = _run(repo, tr)
    assert "docs/plan.md" in res.body
    assert tr.t("doc.not_substitute2") in res.body


def test_handoff_file_bytes_use_lf_on_all_platforms(repo: Path, tr):
    """两个平台上产出的交接文件字节必须一致，否则跨平台 git diff 全是噪声。"""
    res = _run(repo, tr)
    data = Path(res.out_path).read_bytes()
    assert b"\r\n" not in data


# ── 语言 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", ["zh-Hans", "zh-Hant", "en"])
def test_all_languages_produce_complete_output(repo: Path, lang):
    tr = Translator(lang)
    res = _run(repo, tr, no_commit=True)
    assert res.code == EXIT_OK
    assert "??" not in res.body, "文案缺键"
    assert "??" not in res.prompt
    assert tr.t("doc.h.prompt") in res.body


def test_language_affects_generated_document(repo: Path):
    en = _run(repo, Translator("en"), no_commit=True)
    hans = _run(repo, Translator("zh-Hans"), no_commit=True)
    assert "Current state" in en.body
    assert "现场" in hans.body


def test_result_to_dict_is_json_serializable(repo: Path, tr):
    res = _run(repo, tr, no_commit=True)
    json.dumps(res.to_dict())


# ── 测试取证 ──────────────────────────────────────────────────────────

def test_test_evidence_recorded(repo: Path, tr):
    """给仓库放一个真 pytest 测试，确认取证链路端到端可用。"""
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    res = _run(repo, tr, skip_tests=False)
    assert res.ctx["test_commands"], "应该识别出 pytest 命令"
    assert res.ctx["test_results"]
    assert tr.t("doc.h.step3") in res.body


def test_skip_tests_records_the_skip(repo: Path, tr):
    res = _run(repo, tr, skip_tests=True)
    assert res.ctx["test_results"] == {}
    assert tr.t("doc.step3.skipped") in res.body


# ── 会话传承 ──────────────────────────────────────────────────────────

def _transcript(tmp_path: Path, name: str = "rollout-2026-08-21T00-00-00-abc123.jsonl") -> Path:
    """一份带压缩摘要的 Codex 转录。摘要是会话自己写的，正是要传承的东西。

    文件名里的 id 必须与 session_meta 里的一致——真实的 rollout 就是这样命名的，
    两者不一致时工具会把文件名当会话 ID、把 meta 里的当源线程 ID。
    """
    fp = tmp_path / "codexlogs" / name
    fp.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"session_id": "abc123", "cwd": str(tmp_path)}},
        {"type": "compacted", "payload": {"message": "# 交接摘要\n\n实测后端 557 passed，Task 5 已完成"}},
    ]
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return fp


def test_selected_session_digest_lands_in_handoff_document(repo: Path, tr, tmp_path: Path):
    """勾选的会话摘要必须整段进交接文档——这是「无损传承」的落点。

    提示词装不下多 MB 转录，所以完整内容只能落在文档里，提示词负责指路。
    """
    fp = _transcript(tmp_path)
    res = _run(repo, tr, sessions=[str(fp)])
    assert res.code == EXIT_OK
    assert "实测后端 557 passed，Task 5 已完成" in res.body
    assert "abc123" in res.body
    assert tr.t("doc.h.sessions") in res.body


def test_selected_session_named_in_prompt_without_full_text(repo: Path, tr, tmp_path: Path):
    """提示词点名会话（话题 / ID / 转录路径），但不内联整份摘要。

    话题本身取自摘要首句，所以会与摘要有重叠——这是有意的（话题就是给人认的）。
    要断言的是「整份摘要没被塞进提示词」，用长度衡量而不是找某个词。
    """
    fp = _transcript(tmp_path)
    res = _run(repo, tr, sessions=[str(fp)])
    assert "abc123" in res.prompt
    assert fp.name in res.prompt
    # 摘要正文留在交接文档里；提示词只负责指向那份文档。
    digest = res.ctx["sessions"][0]["digest"]
    assert digest in res.body
    assert digest not in res.prompt
    assert res.ctx["handoff_rel"] in res.prompt


def test_no_selection_keeps_original_output(repo: Path, tr):
    """不选任何会话时行为与原版一致：文档里没有「前序会话」一节。"""
    res = _run(repo, tr)
    assert res.ctx["sessions"] == []
    assert tr.t("doc.h.sessions") not in res.body


def test_selected_session_outside_scan_is_still_read(repo: Path, tr, tmp_path: Path):
    """勾了却没传下去是最坏的结果：用户以为交接了，实际丢了。

    no_vitals=True 时根本没有扫描列表，勾选的转录仍然必须被单独读进来。
    """
    fp = _transcript(tmp_path)
    res = _run(repo, tr, sessions=[str(fp)], no_vitals=True)
    assert len(res.ctx["sessions"]) == 1
    assert "557 passed" in res.body


def test_missing_selected_session_is_reported_not_silent(repo: Path, tr, tmp_path: Path):
    """指定的转录不存在时要说出来，不能静默跳过。"""
    lines: list[str] = []
    opts = Options(repo=repo, skip_tests=True, no_vitals=True,
                   sessions=[str(tmp_path / "nope.jsonl")])
    res = run_handoff(opts, tr, log=lines.append)
    assert res.code == EXIT_OK
    assert res.ctx["sessions"] == []
    assert any("nope.jsonl" in ln for ln in lines), lines


def test_runs_without_git(tmp_path: Path, tr):
    """git 不是硬门禁。

    原先一个没 git init 的目录直接以 EXIT_BAD_INPUT 失败，于是用户连
    「把前序会话的结论带走」都做不到——而那一步根本不碰 git。实测正是这个
    场景挡住了用户：Codex Desktop 的工作目录没有版本控制。
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "note.txt").write_text("x\n", encoding="utf-8")
    res = _run(plain, tr, no_commit=True)
    assert res.code == EXIT_OK, res.error
    assert res.ctx["has_git"] is False
    # 提示词不能出现「分支 ，HEAD 」这种空值断句。
    assert tr.t("prompt.resume_no_git", repo_name="plain", repo=plain.as_posix()) in res.prompt
    assert tr.t("prompt.no_git") in res.prompt
    # 不能渲染出空值断句（`分支 ，HEAD 。`）——那读起来像读取失败。
    assert "，分支 ，HEAD" not in res.prompt
    assert "branch , HEAD" not in res.prompt
    # 文档要说清没有版本控制，而不是留空。
    assert tr.t("doc.scene.no_git") in res.body
    assert tr.t("cli.commit.no_git") in res.body


def test_no_git_still_carries_sessions(tmp_path: Path, tr):
    """缺 git 时会话传承必须照做——那正是用户要的那一步。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    fp = tmp_path / "rollout-2026-08-22T00-00-00-ng1.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"session_id": "ng1", "cwd": str(plain)}},
        {"type": "compacted", "payload": {
            "message": "结论：改用 cc-switch 接管",
            "window_number": 1,
            "replacement_history": [
                {"role": "user", "content": [{"text": "不要动 settings.json"}]},
            ],
        }},
    ]
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    res = _run(plain, tr, no_commit=True, sessions=[str(fp)])
    assert res.code == EXIT_OK
    assert "结论：改用 cc-switch 接管" in res.body
    assert "不要动 settings.json" in res.body
    assert "ng1" in res.prompt


def test_no_git_expiry_has_no_head(tmp_path: Path, tr):
    """过期声明挂在 HEAD 上；没有 git 就只能给时间，不能渲染空 sha。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    res = _run(plain, tr, no_commit=True)
    assert tr.t("prompt.expiry_no_git", now=res.ctx["now"]) in res.prompt


def test_prompt_carries_portable_repo_identity(repo: Path, tr):
    """路径是「这台机器上的位置」，不是仓库身份。

    换机器 / 容器 / WSL / Codespaces 后 `E:/output/...` 不存在，新会话就无从
    定位；而同一个 remote 下的两个工作副本也只能靠路径区分。有 remote 时必须
    给出 remote URL + 完整 sha；没有 remote 时必须说清楚「只在本机」。
    """
    res = _run(repo, tr)
    # conftest 造的仓库没有远程，所以应当出现「只存在于本机」的声明。
    assert tr.t("prompt.no_remote") in res.prompt
    assert res.ctx["remote"] == ""


def test_prompt_reports_unpushed_commits(repo: Path, tr):
    """未推送的提交在别处 clone 拿不到——不声明，新会话会以为远程已有。"""
    res = _run(repo, tr)
    # conftest 的仓库有提交但没有任何远程，所以全部提交都算未推送。
    assert res.ctx["unpushed"], res.ctx
    assert tr.t("prompt.unpushed", count=res.ctx["unpushed"]) in res.prompt


def test_prompt_declares_handoff_is_lossy(repo: Path, tr, tmp_path: Path):
    """交接是有损的。不说明这一点，新会话会把「摘要里没有」当成「没发生过」，
    于是把上一个会话已经排除的方案重新试一遍。"""
    fp = _transcript(tmp_path)
    res = _run(repo, tr, sessions=[str(fp)])
    assert tr.t("prompt.sessions.lossy") in res.prompt


@pytest.mark.parametrize(
    ("label", "value", "must_keep"),
    [
        ("反斜杠转录路径", r"{home}\.codex\sessions\rollout-x.jsonl", "rollout-x.jsonl"),
        ("正斜杠转录路径", "{home}/.claude/projects/p/abc.jsonl", "abc.jsonl"),
        ("散文里的路径", "摘要提到 {home}\\Documents\\slug 这个目录", "Documents"),
    ],
)
def test_redact_home_forms(label: str, value: str, must_keep: str):
    home = os.path.expanduser("~")
    user = os.path.basename(home)
    got = _redact_home(value.format(home=home))
    assert user.lower() not in got.lower(), label
    assert must_keep in got, f"{label}: 定位所需的部分被脱掉了"


def test_redact_home_covers_claude_project_slug():
    """Claude Code 把 cwd 里的非字母数字换成 `-` 作项目目录名，于是
    `C:\\Users\\alice` 变成 `C--Users-alice`——家目录前缀换掉后用户名仍在。"""
    home = os.path.expanduser("~")
    user = os.path.basename(home)
    slug = re.sub(r"[^A-Za-z0-9]", "-", home)
    got = _redact_home(f"{home}\\.claude\\projects\\{slug}\\5370f0b4.jsonl")
    assert user.lower() not in got.lower()
    assert "5370f0b4.jsonl" in got


def test_redact_home_leaves_other_paths_alone():
    """家目录之外的路径不能被动——仓库路径是接续会话要用的信息。"""
    outside = "E:/output/kirara-ai/kirara-ai3.3.0b8"
    assert _redact_home(outside) == outside


# ── 迁机：别人机器上的用户名也要脱 ────────────────────────────────────

@pytest.mark.parametrize(("raw", "want_gone"), [
    (r"D:\Users\bob\myproj\src", "bob"),
    ("/home/alice/proj/src", "alice"),
    ("/Users/carol/work", "carol"),
    ("/mnt/c/Users/dave/p", "dave"),
    # Claude 的 slug 目录名：用户名嵌在 `-` 分隔的一段里，认不出真实分隔符。
    # 实测漏掉这一形态会让用户名从转录路径漏出去。
    (r"~\.claude\projects\D--Users-bob-myproj\x.jsonl", "bob"),
])
def test_redact_strips_other_peoples_usernames(raw: str, want_gone: str, monkeypatch):
    monkeypatch.setenv("USERNAME", "devin")
    got = _redact(raw)
    assert want_gone not in got, f"别人的用户名漏了：{got}"
    assert "_USER_" in got


def test_redact_keeps_the_rest_of_a_foreign_path(monkeypatch):
    """只换用户名那一段。读者仍要靠路径判断「那台机器上的项目在哪」。"""
    monkeypatch.setenv("USERNAME", "devin")
    got = _redact(r"D:\Users\bob\myproj\src\auth.py")
    assert "myproj" in got and "auth.py" in got
    assert got.startswith(r"D:\Users\_USER_")


def test_redact_does_not_touch_the_local_username_twice(monkeypatch, tmp_path: Path):
    """本机名字由 `_redact_home` 处理成 `~`；外来规则不该再碰它。

    两条规则都作用于同一个名字时会产生 `_USER_` 混在 `~` 里的怪输出。
    """
    fake = tmp_path / "devin"
    fake.mkdir()
    monkeypatch.setattr("os.path.expanduser", lambda p: str(fake) if p == "~" else p)
    monkeypatch.setenv("USERNAME", "devin")
    got = _redact(str(fake / "proj"))
    assert "_USER_" not in got
    assert got.startswith("~")


def test_redact_leaves_non_home_paths_alone(monkeypatch):
    """不长得像家目录的路径不能被动——那是接续会话要用的信息。"""
    monkeypatch.setenv("USERNAME", "devin")
    for raw in ("E:/output/kirara-ai/kirara-ai3.3.0b8", "/opt/app/src", "src/x.py"):
        assert _redact(raw) == raw


def test_document_flags_a_foreign_session(repo: Path, tr):
    """外来会话的路径在本机全无效，文档必须说出来。

    否则读者（和接续会话的智能体）会照着那些路径去找文件，找不到才发现不对。
    """
    ctx = dict(_run(repo, tr).ctx)
    ctx["sessions"] = [{
        "agent": "Claude Code", "session_id": "mig99", "label": "重构认证模块",
        "cwd": r"D:\Users\bob\myproj", "repos": [], "path": r"D:\Users\bob\.claude\x.jsonl",
        "mtime_text": "2026-08-22 10:00:00", "is_foreign": True, "digest_windows": 0,
        "asks": [], "last_prompt": "", "digest": "",
    }]
    body = build_handoff(ctx, tr)
    assert tr.t("doc.sessions.foreign") in body
    assert "bob" not in body, "外来用户名不能进文档"


def test_document_does_not_flag_a_local_session(repo: Path, tr):
    """本机会话不该带这条警告——它会让读者以为路径不可用。"""
    ctx = dict(_run(repo, tr).ctx)
    ctx["sessions"] = [{
        "agent": "Claude Code", "session_id": "loc1", "label": "本机会话",
        "cwd": str(repo), "repos": [str(repo)], "path": str(repo / "x.jsonl"),
        "mtime_text": "2026-08-22 10:00:00", "is_foreign": False, "digest_windows": 0,
        "asks": [], "last_prompt": "", "digest": "",
    }]
    body = build_handoff(ctx, tr)
    assert tr.t("doc.sessions.foreign") not in body


def test_document_redacts_home_directory(repo: Path, tr, tmp_path: Path, monkeypatch):
    """交接文档会被 git 提交、可能推到公开仓库；转录路径里带着操作系统用户名。

    提示词是本机粘贴的、保留真实路径（接续会话要照它去读转录）；
    **文档**里的家目录必须脱敏成 `~`，两者受众不同。
    """
    # 用一个假家目录，名字不可能出现在临时路径里，避免误判成泄露。
    fake_home = tmp_path / "hh"
    fake_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    home = str(fake_home)
    marker = fake_home.name

    # 转录放在假家目录**里面**，与真实布局一致（转录就住在 ~/.codex 下）。
    fp = fake_home / ".codex" / "sessions" / "rollout-2026-08-21T00-00-00-red1.jsonl"
    fp.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"session_id": "red1", "cwd": home + r"\Documents\Codex\x"}},
        {"type": "compacted", "payload": {
            "message": f"摘要提到 {home}\\.codex\\sessions\\y.jsonl 这个文件",
            "window_number": 1,
            "replacement_history": [
                {"role": "user", "content": [{"text": f"看一下 {home}/notes.md"}]},
            ],
        }},
    ]
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    res = _run(repo, tr, sessions=[str(fp)])
    # 整份文档都不能带家目录名：它会被 git 提交，可能推到公开仓库。
    # 文档里嵌的提示词副本同样要脱敏——受众是仓库的读者，不是本机的粘贴板。
    assert marker not in res.body, "文档泄露了家目录名"
    assert "~" in res.body, "家目录应被替换成 ~"
    # 会话 ID 文件名必须保留，否则文档失去可操作性。
    assert "red1" in res.body
    # 终端打印用的那份提示词保留真实路径：它是给人当场复制粘贴的。
    assert marker in res.prompt, "提示词本身不该脱敏——它在本机使用"


def test_user_asks_land_in_document_verbatim(repo: Path, tr, tmp_path: Path):
    fp = tmp_path / "codexlogs" / "rollout-2026-08-21T00-00-00-ask1.jsonl"
    fp.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"session_id": "ask1", "cwd": str(tmp_path)}},
        {"type": "compacted", "payload": {
            "message": "摘要：用户要求发布",
            "window_number": 1,
            "replacement_history": [
                {"role": "user", "content": [{"text": "不要删除项目 A，也不要强制推送"}]},
            ],
        }},
    ]
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    res = _run(repo, tr, sessions=[str(fp)])
    assert tr.t("doc.sessions.asks") in res.body
    assert "不要删除项目 A，也不要强制推送" in res.body


def test_all_compaction_windows_reach_the_document(repo: Path, tr, tmp_path: Path):
    """多窗口摘要必须整段进文档，且标注窗口数，让人能判断覆盖范围。"""
    fp = tmp_path / "codexlogs" / "rollout-2026-08-21T00-00-00-multi.jsonl"
    fp.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"session_id": "multi", "cwd": str(tmp_path)}},
        {"type": "compacted", "payload": {"message": "第一阶段：定目标", "window_number": 1}},
        {"type": "compacted", "payload": {"message": "第二阶段：改代码", "window_number": 2}},
    ]
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    res = _run(repo, tr, sessions=[str(fp)])
    assert "第一阶段：定目标" in res.body
    assert "第二阶段：改代码" in res.body
    assert tr.t("doc.sessions.windows", count=2) in res.body


@pytest.mark.parametrize("lang", ["zh-Hans", "zh-Hant", "en"])
def test_session_section_translated_in_all_languages(repo: Path, tmp_path: Path, lang: str):
    fp = _transcript(tmp_path)
    t2 = Translator(lang)
    res = _run(repo, t2, sessions=[str(fp)], no_commit=True)
    assert "??" not in res.body, "文案缺键"
    assert "??" not in res.prompt
    assert t2.t("doc.h.sessions") in res.body


def test_english_output_carries_no_chinese_of_our_own(tmp_path: Path):
    """英文产出里不能有**我们自己写的**中文。

    英国人切到 EN 之后看到中英混排会认为软件坏了。工具自己生成的每一句都
    必须跟随语言。仓库内容（任务标题、文件名）与前序会话的引用不算——那些
    是输入，照抄是对的；引用段另有一句说明为什么会出现别的语言。

    所以这里用一个**全英文**的仓库：产出里出现任何中文都只可能来自工具自己。
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@e"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=str(repo), capture_output=True, check=False)
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(
        "**Goal:** ship the thing.\n\n"
        "## Global Constraints\n- `docs/LOGO.jpg` is user-owned and must not be committed.\n\n"
        "### Task 1: build the core\n\n"
        "**Files:**\n- Create: `pkg/core.py`\n\n"
        "**Interfaces:**\n- Produces `build_thing()`\n\n"
        "**Steps:**\n- [ ] **Step 1** write core\n- [ ] **Step 2** wire it up\n",
        encoding="utf-8",
    )
    (repo / "pkg").mkdir()
    (repo / "pkg" / "core.py").write_text("def build_thing():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, check=False)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), capture_output=True, check=False)

    en = Translator("en")
    res = _run(repo, en, no_commit=True)
    leaked = re.findall(r"[一-鿿]", res.prompt)
    assert not leaked, f"英文提示词里有我们自己的中文：{''.join(leaked[:20])}"
    doc_leaked = re.findall(r"[一-鿿]", res.body)
    assert not doc_leaked, f"英文交接文档里有我们自己的中文：{''.join(doc_leaked[:20])}"


def test_quoted_session_material_keeps_its_language(repo: Path, tmp_path: Path):
    """引用保留原语言，且文档要说明这一点。

    否则读者会把中文引用当成本地化没做完，而它是刻意的：那些是原始记录。
    """
    fp = tmp_path / "codexlogs" / "rollout-2026-08-21T00-00-00-cjk1.jsonl"
    fp.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"session_id": "cjk1", "cwd": str(tmp_path)}},
        {"type": "compacted", "payload": {
            "message": "# 交接摘要\n\n## 目标\n\n审计四个项目",
            "window_number": 1,
            "replacement_history": [
                {"role": "user", "content": [{"text": "不要删除项目 A"}]},
            ],
        }},
    ]
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    en = Translator("en")
    res = _run(repo, en, sessions=[str(fp)], no_commit=True)
    # 引用逐字保留。
    assert "不要删除项目 A" in res.body
    assert "审计四个项目" in res.body
    # 并且用英文说明了为什么这里会出现别的语言。
    assert en.t("doc.sessions.verbatim_note") in res.body
    assert "primary records" in res.body


def test_digest_with_code_fences_does_not_break_document(repo: Path, tr, tmp_path: Path):
    """摘要是模型写的 Markdown，几乎一定自带 ``` 代码块。

    用三个反引号包它会被内层的第一个 ``` 提前闭合，摘要后半段就漏进文档结构
    ——实测摘要里的 `### 测试修复` 变成了交接文档自己的三级标题。
    """
    fp = tmp_path / "codexlogs" / "rollout-2026-08-21T00-00-00-fence1.jsonl"
    fp.parent.mkdir(parents=True, exist_ok=True)
    digest = "# 交接摘要\n\n```bash\npytest -q\n```\n\n### 测试修复\n\n改了三处"
    rows = [
        {"type": "session_meta", "payload": {"session_id": "fence1", "cwd": str(tmp_path)}},
        {"type": "compacted", "payload": {"message": digest}},
    ]
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    res = _run(repo, tr, sessions=[str(fp)])
    body = res.body
    # 外层围栏必须比内层长，否则内层的 ``` 会提前闭合。
    assert "````text" in body
    # 摘要内容完整保留。
    assert "改了三处" in body
    # 关键：那个三级标题必须待在代码围栏**内部**，不能成为文档自己的结构。
    # 用围栏配对判断，而不是找字符串——它在围栏里出现是正确的。
    inside = re.findall(r"^(`{3,})text\n(.*?)^\1$", body, re.S | re.M)
    assert any("### 测试修复" in blk for _f, blk in inside), "标题应当在代码块内"
    outside = re.sub(r"^(`{3,})text\n.*?^\1$", "", body, flags=re.S | re.M)
    assert "### 测试修复" not in outside, outside


# ── 日志回调 ──────────────────────────────────────────────────────────

def test_log_callback_receives_all_six_steps(repo: Path, tr):
    lines: list[str] = []
    run_handoff(Options(repo=repo, skip_tests=True, no_vitals=True), tr, log=lines.append)
    joined = "\n".join(lines)
    for i in range(1, 7):
        assert f"[{i}/6]" in joined
