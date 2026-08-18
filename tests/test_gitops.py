#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git 操作与并发检测。重点：
  · `git()` 失败与空输出必须可区分（空仓库的 rev-parse HEAD）
  · 受保护文件在 `--force` 下也不能被提交
  · 并发信号不该被本工具自己的产物或构建垃圾触发
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from agent_handoff.core.gitops import (
    _exclude_pathspecs,
    changed_paths,
    commit_paths,
    detect_concurrency,
    dirty_submodules,
    do_commit,
    foreign_commits,
    git,
    git_proc,
    head_sha,
    is_repo,
    recent_commits,
    repo_meta,
    run,
)


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True)


# ── run() ────────────────────────────────────────────────────────────────

def test_run_captures_exit_code_and_output(tmp_path: Path):
    p = run(["git", "--version"], tmp_path)
    assert p.ok and "git version" in p.out


def test_run_missing_command_returns_127(tmp_path: Path):
    p = run(["definitely-not-a-real-binary-xyz"], tmp_path)
    assert p.code == 127 and not p.ok


def test_run_never_raises_on_bad_input(tmp_path: Path):
    p = run([""], tmp_path)
    assert not p.ok  # 不抛异常就是通过


def test_run_timeout_keeps_partial_output(tmp_path: Path):
    import sys

    code = "import sys,time; print('before'); sys.stdout.flush(); time.sleep(5)"
    p = run([sys.executable, "-c", code], tmp_path, timeout=1)
    assert p.code == 124
    assert "timeout after 1s" in p.out


# ── 空仓库：失败与空输出的区分 ────────────────────────────────────────

def test_head_sha_empty_on_repo_without_commits(tmp_path: Path):
    """原版的 git() 返回空串，调用方分不清"失败"和"HEAD 是空的"。"""
    r = tmp_path / "empty"
    r.mkdir()
    _git(r, "init", "-q")
    assert head_sha(r) == ""
    assert git_proc(r, "rev-parse", "--verify", "HEAD").ok is False


def test_head_sha_present_after_commit(repo: Path):
    sha = head_sha(repo)
    assert len(sha) == 40


def test_repo_meta_on_empty_repo(tmp_path: Path):
    r = tmp_path / "empty"
    r.mkdir()
    _git(r, "init", "-q")
    meta = repo_meta(r)
    assert meta["head"] == "<no commits>"
    assert meta["head_full"] == ""
    assert meta["ahead"] == ""


def test_repo_meta_branch_and_head(repo: Path):
    meta = repo_meta(repo)
    assert meta["branch"] in ("main", "master")
    assert meta["head_sha"]
    assert meta["ahead"] == ""


def test_is_repo_true_and_false(repo: Path, tmp_path: Path):
    assert is_repo(repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert is_repo(plain) is False


def test_is_repo_rejects_fake_dot_git_file(tmp_path: Path):
    """一个恰好叫 .git 的普通文件会骗过"存在性检查"，但 git 自己不认。"""
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / ".git").write_text("not a git dir\n", encoding="utf-8")
    assert is_repo(fake) is False


# ── 受保护文件 ────────────────────────────────────────────────────────

def test_exclude_pathspecs_uses_literal_form():
    """`:!path` 简写在旧 git 上不支持，且路径含 * 或 [ 时会被当通配。"""
    got = _exclude_pathspecs(["docs/LOGO.jpg", "a[1].key", "./x.txt", "win\\path.bin"])
    assert got == [
        ":(exclude,literal)docs/LOGO.jpg",
        ":(exclude,literal)a[1].key",
        ":(exclude,literal)x.txt",
        ":(exclude,literal)win/path.bin",
    ]


def test_do_commit_excludes_protected(repo: Path, tr):
    (repo / "secret.key").write_text("TOPSECRET\n", encoding="utf-8")
    (repo / "docs" / "LOGO.jpg").write_bytes(b"\xff\xd8jpegbytes")
    (repo / "pkg" / "new.py").write_text("x = 1\n", encoding="utf-8")

    do_commit(repo, ["secret.key", "docs/LOGO.jpg"], "test snapshot", dry=False, tr=tr)

    tracked = git(repo, "ls-files")
    assert "pkg/new.py" in tracked
    assert "secret.key" not in tracked
    assert "docs/LOGO.jpg" not in tracked


def test_do_commit_protected_with_bracket_in_name(repo: Path, tr):
    """含 `[` 的文件名在 `:!` 简写下会被当成字符类，导致排除失效。"""
    weird = "a[1].key"
    (repo / weird).write_text("secret\n", encoding="utf-8")
    (repo / "pkg" / "new.py").write_text("x = 1\n", encoding="utf-8")
    do_commit(repo, [weird], "snap", dry=False, tr=tr)
    tracked = git(repo, "ls-files")
    assert "pkg/new.py" in tracked
    assert weird not in tracked


def test_do_commit_nonexistent_protected_path_is_harmless(repo: Path, tr):
    (repo / "pkg" / "new.py").write_text("x = 1\n", encoding="utf-8")
    out = do_commit(repo, ["never/existed.bin"], "snap", dry=False, tr=tr)
    assert "failed" not in out.lower()
    assert "pkg/new.py" in git(repo, "ls-files")


def test_do_commit_clean_worktree(repo: Path, tr):
    out = do_commit(repo, [], "snap", dry=False, tr=tr)
    assert out == tr.t("cli.commit.clean")


def test_do_commit_dry_writes_nothing(repo: Path, tr):
    (repo / "pkg" / "new.py").write_text("x = 1\n", encoding="utf-8")
    before = head_sha(repo)
    out = do_commit(repo, ["secret.key"], "snap", dry=True, tr=tr)
    assert "DRY-RUN" in out
    assert ":(exclude,literal)secret.key" in out
    assert head_sha(repo) == before
    assert "pkg/new.py" not in git(repo, "ls-files")


def test_do_commit_only_protected_changed(repo: Path, tr):
    """全部改动都属于受保护文件时不该提交空快照。"""
    (repo / "secret.key").write_text("s\n", encoding="utf-8")
    out = do_commit(repo, ["secret.key"], "snap", dry=False, tr=tr)
    assert out == tr.t("cli.commit.nothing_staged")


# ── 并发检测 ──────────────────────────────────────────────────────────
# detect_concurrency 返回 (阻断信号, 提示信号)。分级的理由：
# "两分钟内有文件被改"最常见的成因是用户自己刚改完就来跑交接，
# 把它当阻断会让正常用法被自己挡住，逼用户加 --force —— 而 --force
# 会连真正的阻断信号一起放过，反而更危险。

def test_detect_concurrency_flags_foreign_staging(repo: Path, tr):
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "pkg/other.py")
    blocking, _ = detect_concurrency(repo, tr)
    assert any("staged" in w for w in blocking)


def test_detect_concurrency_flags_index_lock(repo: Path, tr):
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    try:
        blocking, _ = detect_concurrency(repo, tr)
        assert any("index.lock" in w for w in blocking)
    finally:
        (repo / ".git" / "index.lock").unlink()


def test_detect_concurrency_flags_merge_head(repo: Path, tr):
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
    blocking, _ = detect_concurrency(repo, tr)
    assert any("MERGE_HEAD" in w for w in blocking)


def test_detect_concurrency_clean_repo_is_quiet(repo: Path, tr):
    assert detect_concurrency(repo, tr) == ([], [])


def test_detect_concurrency_ignores_build_artifacts(repo: Path, tr):
    """原版把工作树里任何两分钟内被碰过的文件都当信号；缓存与产物会误报。"""
    for rel in ("__pycache__/x.pyc", "node_modules/pkg/index.js", "dist/bundle.js"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("junk\n", encoding="utf-8")
    assert detect_concurrency(repo, tr) == ([], [])


def test_detect_concurrency_ignores_own_outputs(repo: Path, tr):
    """上一轮运行留下的交接文件与计划文档不该被当成"别人在写"。"""
    (repo / "docs" / "2026-08-18-handoff.md").write_text("# handoff\n", encoding="utf-8")
    (repo / "docs" / "plan.md").write_text(
        (repo / "docs" / "plan.md").read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    got = detect_concurrency(
        repo, tr, ignore={"docs/2026-08-18-handoff.md", "docs/plan.md"}
    )
    assert got == ([], [])


def test_detect_concurrency_recent_edit_is_advisory_not_blocking(repo: Path, tr):
    """用户自己刚改完文件就来跑交接是最常见的正常用法，不能阻断。"""
    (repo / "pkg" / "core.py").write_text("def build_thing():\n    return 2\n", encoding="utf-8")
    blocking, advisory = detect_concurrency(repo, tr)
    assert blocking == []
    assert any("two minutes" in w for w in advisory)


def test_detect_concurrency_staging_blocks_even_with_recent_edit(repo: Path, tr):
    """真正只可能由另一个进程造成的信号仍必须阻断。"""
    (repo / "pkg" / "core.py").write_text("def build_thing():\n    return 3\n", encoding="utf-8")
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "pkg/other.py")
    blocking, advisory = detect_concurrency(repo, tr)
    assert blocking and advisory


def test_changed_paths_includes_untracked(repo: Path):
    (repo / "brand_new.py").write_text("z = 3\n", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("def build_thing():\n    return 9\n", encoding="utf-8")
    got = changed_paths(repo)
    assert "brand_new.py" in got
    assert "pkg/core.py" in got


# ── HEAD 赛跑检测 ─────────────────────────────────────────────────────

def test_foreign_commits_ignores_our_own(repo: Path):
    before = head_sha(repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "chore: session handoff snapshot 2026-08-18 10:00")
    assert foreign_commits(repo, before, ("chore: session handoff snapshot",)) == []


def test_foreign_commits_detects_intruder(repo: Path):
    before = head_sha(repo)
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: somebody else was here")
    got = foreign_commits(repo, before, ("chore: session handoff snapshot",))
    assert got == ["feat: somebody else was here"]


def test_foreign_commits_no_movement(repo: Path):
    before = head_sha(repo)
    assert foreign_commits(repo, before, ("x",)) == []


def test_foreign_commits_empty_before_is_skipped(tmp_path: Path):
    """空仓库里 before 为空；"HEAD 变了"是我们自己造成的，不该报警。"""
    r = tmp_path / "empty"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@e.c")
    _git(r, "config", "user.name", "T")
    before = head_sha(r)
    (r / "f.txt").write_text("f\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "first")
    assert foreign_commits(r, before, ("x",)) == []


# ── 其他 ──────────────────────────────────────────────────────────────

def test_commit_paths_only_touches_named_files(repo: Path):
    (repo / "docs" / "h.md").write_text("# h\n", encoding="utf-8")
    (repo / "pkg" / "unrelated.py").write_text("q = 1\n", encoding="utf-8")
    assert commit_paths(repo, [repo / "docs" / "h.md"], "docs: x") is True
    tracked = git(repo, "ls-files")
    assert "docs/h.md" in tracked
    assert "pkg/unrelated.py" not in tracked


def test_commit_paths_outside_repo_returns_false(repo: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n", encoding="utf-8")
    assert commit_paths(repo, [outside], "docs: x") is False


def test_recent_commits(repo: Path):
    assert "init" in recent_commits(repo)


def test_dirty_submodules_empty_when_none(repo: Path):
    assert dirty_submodules(repo) == []
