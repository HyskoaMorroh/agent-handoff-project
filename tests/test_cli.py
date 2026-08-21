#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 与 GUI 服务的接口契约。重点：
  · 原版的每一个参数都还在、语义没变、退出码没变
  · GUI 的安全边界（令牌、Origin、目录穿越）真的挡得住
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_handoff.cli import build_parser, main
from agent_handoff.i18n import Translator

ORIGINAL_FLAGS = [
    "--plan", "--out", "-m", "--message", "--no-commit", "--skip-tests",
    "--test-timeout", "--vitals", "--no-vitals", "--find", "--limit",
    "--force", "--dry-run",
]


# ── 参数契约 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("flag", ORIGINAL_FLAGS)
def test_original_flag_still_accepted(flag):
    """原版的每一个参数都必须还在。少一个就是回归。"""
    tr = Translator("en")
    ap = build_parser(tr)
    assert any(flag in a.option_strings for a in ap._actions), flag


def test_repo_defaults_to_dot():
    ns = build_parser(Translator("en")).parse_args([])
    assert ns.repo == "."


def test_original_defaults_unchanged():
    ns = build_parser(Translator("en")).parse_args([])
    assert ns.test_timeout == 900
    assert ns.limit == 12
    assert ns.no_commit is False
    assert ns.skip_tests is False
    assert ns.force is False
    assert ns.dry_run is False
    assert ns.vitals is False
    assert ns.no_vitals is False
    assert ns.plan is None and ns.out is None and ns.message is None


def test_new_flags_default_to_original_behavior():
    """新增开关的默认值必须让行为与原版一致——加了才改变什么。"""
    ns = build_parser(Translator("en")).parse_args([])
    assert ns.gui is False
    assert ns.no_browser is False
    assert ns.json is False
    assert ns.port == 0
    assert ns.jobs == 0
    assert ns.lang is None


def test_help_text_follows_lang():
    en = build_parser(Translator("en")).format_help()
    hans = build_parser(Translator("zh-Hans")).format_help()
    assert "Repository path" in en
    assert "仓库路径" in hans


def test_lang_choices_are_the_three():
    ap = build_parser(Translator("en"))
    action = next(a for a in ap._actions if "--lang" in a.option_strings)
    assert set(action.choices) == {"zh-Hans", "zh-Hant", "en"}


# ── 退出码 ────────────────────────────────────────────────────────────

def test_exit_code_2_on_missing_path(tmp_path: Path, capsys):
    """路径不存在仍是参数错误。

    但「存在却不是 git 仓库」不再是错误——那个用例移到了 test_handoff.py 的
    test_accepts_non_repo_with_degraded_git_steps：缺 git 只让提交快照降级，
    会话传承与完成度评估照旧。
    """
    assert main([str(tmp_path / "nope"), "--skip-tests", "--no-vitals", "--lang", "en"]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_non_repo_runs_and_exits_zero(tmp_path: Path, capsys):
    d = tmp_path / "plain"
    d.mkdir()
    assert main([str(d), "--skip-tests", "--no-vitals", "--no-commit", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "not under git" in out


def test_exit_code_3_on_concurrency(repo: Path, capsys):
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "pkg/other.py"], cwd=str(repo), capture_output=True)
    assert main([str(repo), "--skip-tests", "--no-vitals", "--lang", "en"]) == 3


def test_exit_code_0_on_success(repo: Path, capsys):
    assert main([str(repo), "--skip-tests", "--no-vitals", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "Opening prompt" in out


def test_find_returns_1_when_nothing_matches(capsys):
    assert main(["--find", "zzz-no-such-session-zzz", "--lang", "en"]) == 1
    assert "No session matches" in capsys.readouterr().out


def test_dry_run_prints_preview_and_writes_nothing(repo: Path, capsys):
    assert main([str(repo), "--dry-run", "--skip-tests", "--no-vitals", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert not list((repo / "docs").glob("*-handoff.md"))


def test_json_output_is_machine_readable(repo: Path, capsys):
    assert main([str(repo), "--skip-tests", "--no-vitals", "--json", "--lang", "en"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == 0
    assert payload["prompt"]
    assert payload["out_path"]
    assert "report" in payload


def test_vitals_json_is_a_list(capsys, monkeypatch):
    monkeypatch.setattr("agent_handoff.core.vitals.agent_session_roots", lambda: [])
    assert main(["--vitals", "--json", "--lang", "en"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_vitals_never_touches_repo(repo: Path, monkeypatch, capsys):
    """--vitals 是只读的：不该产生任何提交或文件。"""
    from agent_handoff.core.gitops import head_sha

    before = head_sha(repo)
    monkeypatch.setattr("agent_handoff.core.vitals.agent_session_roots", lambda: [])
    main(["--vitals", "--lang", "en"])
    assert head_sha(repo) == before
    assert not list((repo / "docs").glob("*-handoff.md"))


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert "agent-handoff" in capsys.readouterr().out


def test_module_is_runnable_as_m(repo: Path):
    """`python -m agent_handoff.cli` 必须能跑——menu.py 就是这么调它的。"""
    p = subprocess.run(
        [sys.executable, "-m", "agent_handoff.cli", str(repo),
         "--dry-run", "--skip-tests", "--no-vitals", "--lang", "en"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(Path(__file__).resolve().parent.parent),
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")},
    )
    assert p.returncode == 0, p.stdout + p.stderr


# ── GUI 服务的安全边界 ────────────────────────────────────────────────

@pytest.fixture
def gui_server():
    """起一个真的 HTTP 服务，测完关掉。绑定 127.0.0.1 随机端口。"""
    import threading
    from http.server import ThreadingHTTPServer

    from agent_handoff.gui.server import TOKEN, Handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.lang = "en"
    httpd.default_repo = ""
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", TOKEN
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str, token: str | None = None, headers: dict | None = None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Handoff-Token", token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_gui_index_injects_bootstrap(gui_server):
    base, token = gui_server
    code, body = _get(f"{base}/")
    assert code == 200
    assert "__BOOTSTRAP__" not in body, "bootstrap 占位符必须被替换"
    assert token in body
    assert "Session Salvage" in body


def test_gui_api_requires_token(gui_server):
    base, _ = gui_server
    code, _ = _get(f"{base}/api/vitals")
    assert code == 401


def test_gui_api_rejects_wrong_token(gui_server):
    base, _ = gui_server
    code, _ = _get(f"{base}/api/vitals", token="not-the-token")
    assert code == 401


def test_gui_api_accepts_correct_token(gui_server):
    base, token = gui_server
    code, body = _get(f"{base}/api/vitals?limit=1", token=token)
    assert code == 200
    assert "rows" in json.loads(body)


def test_gui_rejects_foreign_host_header(gui_server):
    """DNS rebinding 防线：Host 不是回环就拒。"""
    base, token = gui_server
    code, _ = _get(f"{base}/api/vitals", token=token, headers={"Host": "evil.example.com"})
    assert code == 403


def test_gui_rejects_foreign_origin(gui_server):
    base, token = gui_server
    code, _ = _get(f"{base}/api/vitals", token=token, headers={"Origin": "https://evil.example.com"})
    assert code == 403


def test_gui_blocks_directory_traversal(gui_server):
    base, _ = gui_server
    for probe in ("/../../../../etc/passwd", "/..%2f..%2fsecret", "/static/../../server.py"):
        code, _ = _get(f"{base}{probe}")
        assert code in (403, 404), probe


def test_gui_serves_its_own_static_files(gui_server):
    base, _ = gui_server
    for name in ("/style.css", "/app.js"):
        code, body = _get(f"{base}{name}")
        assert code == 200 and body


def test_gui_strings_endpoint_switches_language(gui_server):
    base, token = gui_server
    _, hans = _get(f"{base}/api/strings?lang=zh-Hans", token=token)
    _, en = _get(f"{base}/api/strings?lang=en", token=token)
    assert json.loads(hans)["strings"]["band.critical"] == "立刻交接"
    assert json.loads(en)["strings"]["band.critical"] == "hand off now"


def test_gui_check_repo_validates(gui_server, repo: Path, tmp_path: Path):
    import urllib.parse

    base, token = gui_server
    q = urllib.parse.quote(str(repo))
    _, ok = _get(f"{base}/api/check-repo?path={q}", token=token)
    parsed_ok = json.loads(ok)
    assert parsed_ok["ok"] is True and not parsed_ok["warn"]

    # 缺 git 不再是「不合格」，而是一条提示：只有提交快照做不了，
    # 会话传承与完成度评估照旧。原先这里返回 ok=False，界面据此拦下整个
    # 流程——用户一个还没 git init 的工作目录就完全用不了这个工具。
    plain = tmp_path / "plain"
    plain.mkdir()
    _, bad = _get(f"{base}/api/check-repo?path={urllib.parse.quote(str(plain))}", token=token)
    parsed = json.loads(bad)
    assert parsed["ok"] is True and parsed["warn"] == "not_git"

    _, missing = _get(f"{base}/api/check-repo?path={urllib.parse.quote(str(tmp_path / 'nope'))}", token=token)
    assert json.loads(missing) == {"ok": False, "reason": "missing"}


def test_gui_unknown_endpoint_404(gui_server):
    base, token = gui_server
    code, _ = _get(f"{base}/api/nope", token=token)
    assert code == 404


def test_gui_sets_security_headers(gui_server):
    import urllib.request

    base, _ = gui_server
    with urllib.request.urlopen(f"{base}/", timeout=10) as r:
        assert "default-src 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"
