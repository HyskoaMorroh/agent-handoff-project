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
import re
import subprocess
from pathlib import Path

import pytest

from agent_handoff.core.gitops import git, head_sha
from agent_handoff.core.handoff import EXIT_BAD_INPUT, EXIT_CONCURRENT, EXIT_OK, Options, run_handoff
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


def test_rejects_non_repo(tmp_path: Path, tr):
    d = tmp_path / "plain"
    d.mkdir()
    res = _run(d, tr)
    assert res.code == EXIT_BAD_INPUT and "not a git repository" in res.error


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


# ── 日志回调 ──────────────────────────────────────────────────────────

def test_log_callback_receives_all_six_steps(repo: Path, tr):
    lines: list[str] = []
    run_handoff(Options(repo=repo, skip_tests=True, no_vitals=True), tr, log=lines.append)
    joined = "\n".join(lines)
    for i in range(1, 7):
        assert f"[{i}/6]" in joined
