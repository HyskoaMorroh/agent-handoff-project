#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试命令推断与输出摘要。原版的 summarize_test_output 只认 pytest 与 vitest，
且收了 name 参数却从没用过。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_handoff.core.probe import (
    detect_env_pitfalls,
    detect_test_commands,
    run_tests,
    summarize_test_output,
)

# ── 命令推断 ──────────────────────────────────────────────────────────

def test_detects_pytest_from_venv(repo: Path):
    (repo / "pyproject.toml").write_text('[project]\nname="d"\nversion="0"\n', encoding="utf-8")
    (repo / "tests").mkdir()
    v = repo / ".venv"
    (v / "bin").mkdir(parents=True)
    (v / "bin" / "python").write_text("", encoding="utf-8")
    cmds = detect_test_commands(repo)
    assert "backend" in cmds
    assert "pytest" in cmds["backend"]
    assert "./tests" in cmds["backend"]


def test_quotes_interpreter_path_with_spaces(repo: Path, monkeypatch):
    """`C:\\Program Files\\...\\python.exe` 不加引号会被 shell 拆成两个参数。"""
    (repo / "pyproject.toml").write_text('[project]\nname="d"\nversion="0"\n', encoding="utf-8")
    monkeypatch.setattr("agent_handoff.core.probe.shutil.which", lambda _n: None)
    monkeypatch.setattr("sys.executable", r"C:\Program Files\Py\python.exe")
    cmds = detect_test_commands(repo)
    assert cmds["backend"].startswith('"')


def test_picks_real_script_name_not_assumed_test(repo: Path):
    """package.json 里叫 test:unit 时不能假设有 test —— 直接跑会 Missing script。"""
    web = repo / "webui"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps({"scripts": {"test:unit": "vitest", "type-check": "tsc"}}), encoding="utf-8"
    )
    cmds = detect_test_commands(repo)
    key = "frontend-test (webui)"
    assert key in cmds and "test:unit" in cmds[key]
    assert "frontend-types (webui)" in cmds


def test_root_package_json_uses_no_prefix(repo: Path):
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    cmds = detect_test_commands(repo)
    assert cmds["frontend-test (.)"].startswith("npm run test")


def test_malformed_package_json_is_skipped(repo: Path):
    web = repo / "webui"
    web.mkdir()
    (web / "package.json").write_text("{not json", encoding="utf-8")
    detect_test_commands(repo)  # 不抛异常即通过


def test_no_metadata_yields_no_commands(tmp_path: Path):
    d = tmp_path / "bare"
    d.mkdir()
    assert detect_test_commands(d) == {}


# ── 输出摘要 ──────────────────────────────────────────────────────────

def test_summarize_pytest_pass(tr):
    assert summarize_test_output("backend", "== 12 passed in 1.2s ==", tr) == "12 passed"


def test_summarize_pytest_failures_with_ids(tr):
    out = "FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n== 2 failed, 8 passed =="
    got = summarize_test_output("backend", out, tr)
    assert got.startswith("2 failed, 8 passed")
    assert "tests/test_a.py::test_x" in got
    assert "tests/test_b.py::test_y" in got


def test_summarize_pytest_errors_and_skips(tr):
    out = "== 1 failed, 2 error, 3 passed, 4 skipped =="
    got = summarize_test_output("backend", out, tr)
    for bit in ("1 failed", "2 error", "3 passed", "4 skipped"):
        assert bit in got


def test_summarize_dedupes_failure_ids(tr):
    out = "FAILED t.py::a\nFAILED t.py::a\n== 1 failed =="
    assert summarize_test_output("backend", out, tr).count("t.py::a") == 1


def test_summarize_vitest(tr):
    assert summarize_test_output("frontend", "Tests  42 passed (42)", tr) == "42 passed"


def test_summarize_vitest_failures(tr):
    got = summarize_test_output("frontend", "Tests  3 failed | 39 passed", tr)
    assert "3 failed" in got


def test_summarize_jest(tr):
    got = summarize_test_output("frontend", "Tests:       2 failed, 18 passed, 20 total", tr)
    assert "2 failed" in got and "18 passed" in got


def test_summarize_cargo(tr):
    got = summarize_test_output("rust", "test result: FAILED. 7 passed; 2 failed; 0 ignored", tr)
    assert got == "2 failed, 7 passed"


def test_summarize_go_failures(tr):
    out = "--- FAIL: TestAlpha (0.00s)\n--- FAIL: TestBeta (0.01s)\nFAIL"
    got = summarize_test_output("go", out, tr)
    assert got.startswith("2 failed")
    assert "TestAlpha" in got


def test_summarize_go_pass(tr):
    assert summarize_test_output("go", "ok  \texample.com/pkg\t0.02s", tr) == "ok"


def test_summarize_uses_name_when_output_is_opaque(tr):
    """原版收了 name 却从没用过；输出无从判断时它是唯一的线索。"""
    got = summarize_test_output("frontend-lint (webui)", "", tr)
    assert "frontend-lint (webui)" in got


def test_summarize_timeout(tr):
    got = summarize_test_output("backend", "partial\n<timeout after 900s>", tr)
    assert "900" in got


def test_summarize_timeout_is_never_reported_as_passed(tr):
    """超时被杀的测试不能报成「通过」——那是给出假证据，比不给证据糟得多。

    超时是把进程杀掉，被杀之前 pytest 已经打印了一部分结果。原版把超时检查排在
    框架摘要之后，于是 `"3 passed\\n<timeout after 900s>"` 先命中 pytest 分支，
    摘要变成 `"3 passed"`，交接文档据此声称测试通过——而其余用例一个都没跑。
    接续会话会拿这份文档当「代码是好的」的依据。
    """
    got = summarize_test_output("backend", "3 passed in 4.2s\n<timeout after 900s>", tr)
    assert "900" in got, "必须说明是超时"
    assert got != "3 passed"
    assert not got.startswith("3 passed"), "超时不能被摘要成通过"


def test_summarize_timeout_keeps_the_partial_count(tr):
    """超时前已跑完的数量有诊断价值，但必须和超时标记一起出现。"""
    got = summarize_test_output("backend", "7 passed in 30s\n<timeout after 60s>", tr)
    assert "60" in got
    assert "7" in got, "保留「超时前通过 7 个」这条线索"


def test_summarize_timeout_translated_in_all_languages():
    """超时文案三语都要有，不能是硬编码英文。"""
    from agent_handoff.i18n import Translator, available

    raw = "3 passed in 4.2s\n<timeout after 900s>"
    for lang in available():
        got = summarize_test_output("backend", raw, Translator(lang))
        assert "900" in got
        assert got != "3 passed"


def test_summarize_normal_pass_is_unaffected(tr):
    """加了超时前置判断后，正常通过的输出必须照原样摘要。"""
    assert summarize_test_output("backend", "===== 491 passed in 402.11s =====", tr) == "491 passed"


def test_summarize_command_not_found(tr):
    assert summarize_test_output("backend", "<command not found>", tr) == tr.t("cli.tests.not_found")


def test_summarize_falls_back_to_tail(tr):
    out = "line one\nline two\nsomething odd at the end"
    got = summarize_test_output("x", out, tr)
    assert "something odd at the end" in got


# ── 并行执行 ──────────────────────────────────────────────────────────

def test_run_tests_parallel_matches_serial(repo: Path, tr):
    import sys

    py = sys.executable
    cmds = {
        "a": f'"{py}" -c "print(\'== 1 passed ==\')"',
        "b": f'"{py}" -c "print(\'== 2 passed ==\')"',
        "c": f'"{py}" -c "print(\'== 3 passed ==\')"',
    }
    serial, sf = run_tests(repo, cmds, 60, tr, parallel=False)
    par, pf = run_tests(repo, cmds, 60, tr, parallel=True)
    assert serial == par == {"a": "1 passed", "b": "2 passed", "c": "3 passed"}
    assert sf == pf == []


def test_run_tests_collects_failure_ids(repo: Path, tr):
    import sys

    py = sys.executable
    script = "print('FAILED t.py::x'); print('== 1 failed ==')"
    cmds = {"a": f'"{py}" -c "{script}"'}
    _, failing = run_tests(repo, cmds, 60, tr)
    assert failing == ["FAILED t.py::x"]


def test_run_tests_empty_commands(repo: Path, tr):
    assert run_tests(repo, {}, 60, tr) == ({}, [])


def test_run_tests_callbacks_fire(repo: Path, tr):
    import sys

    started, done = [], []
    cmds = {"a": f'"{sys.executable}" -c "print(\'== 1 passed ==\')"'}
    run_tests(
        repo, cmds, 60, tr,
        on_start=lambda n, _c: started.append(n),
        on_done=lambda n, _line: done.append(n),
    )
    assert started == ["a"] and done == ["a"]


# ── 环境陷阱 ──────────────────────────────────────────────────────────

def test_pitfall_reports_broken_venv(repo: Path, tr):
    v = repo / ".venv-broken"
    (v / "lib").mkdir(parents=True)
    notes = detect_env_pitfalls(repo, tr)
    assert any("not a usable virtualenv" in n for n in notes)
    assert any("Do not delete" in n for n in notes)


def test_pitfall_reports_usable_venv(repo: Path, tr):
    v = repo / ".venv"
    (v / "bin").mkdir(parents=True)
    (v / "bin" / "python").write_text("", encoding="utf-8")
    notes = detect_env_pitfalls(repo, tr)
    assert any("Usable interpreter" in n and "bin/python" in n for n in notes)


def test_pitfall_reports_missing_test_script(repo: Path, tr):
    web = repo / "webui"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({"scripts": {"test:unit": "vitest"}}), encoding="utf-8")
    notes = detect_env_pitfalls(repo, tr)
    assert any("Missing script" in n and "test:unit" in n for n in notes)


def test_pitfall_reports_foreign_registry_in_any_subdir(repo: Path, tr):
    """原版硬编码了 webui/yarn.lock，别的子目录一律看不见。"""
    for sub in ("frontend", "webui", "admin"):
        d = repo / sub
        d.mkdir()
        (d / "yarn.lock").write_text(
            'resolved "https://registry.npmmirror.com/vue/-/vue-3.0.0.tgz"\n', encoding="utf-8"
        )
    notes = detect_env_pitfalls(repo, tr)
    hits = [n for n in notes if "registry.npmmirror.com" in n]
    assert len(hits) >= 2, notes


def test_pitfall_ignores_official_registry(repo: Path, tr):
    (repo / "yarn.lock").write_text(
        'resolved "https://registry.npmjs.org/vue/-/vue-3.0.0.tgz"\n', encoding="utf-8"
    )
    notes = detect_env_pitfalls(repo, tr)
    assert not any("registry" in n for n in notes)


def test_pitfall_clean_repo_is_quiet(repo: Path, tr):
    assert detect_env_pitfalls(repo, tr) == []
