#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检。重点：

  · 只读——不创建目录、不装东西、不改环境变量
  · 不抛异常，哪一项坏掉都不能带崩整轮诊断
  · 「设了但指错」必须报出来（那是最难查的一类）
  · WARN 不能让退出码非零，否则 CI 会逼人关掉整条检查
"""
from __future__ import annotations

import json

import pytest

from agent_handoff.core.doctor import FAIL, OK, WARN, run_doctor


def _by_key(result, key):
    return [c for c in result["checks"] if c["key"] == key]


def _one(result, key):
    hits = _by_key(result, key)
    assert hits, f"没有 {key} 这一项"
    return hits[0]


def test_runs_without_raising_and_reports_a_level():
    """诊断本身崩掉是最没道理的失败。"""
    r = run_doctor()
    assert r["level"] in (OK, WARN, FAIL)
    assert isinstance(r["checks"], list) and r["checks"]
    assert isinstance(r["transcripts"], int)


def test_result_is_json_serialisable():
    """网页界面靠它，所以必须能直接进 JSON。"""
    json.dumps(run_doctor())


def test_python_version_is_reported():
    c = _one(run_doctor(), "python")
    # 测试跑得起来说明 Python 够新，所以这一项必然是 OK。
    assert c["level"] == OK
    assert c["data"]["version"].count(".") == 2


def test_git_check_reports_path_and_version_when_present(monkeypatch):
    c = _one(run_doctor(), "git")
    if c["level"] == OK:
        assert c["data"]["path"]
        # 版本问不出来不该让「git 存在」变成失败，所以 version 可以为空串。
        assert "version" in c["data"]
    else:
        assert c["level"] == FAIL


def test_git_missing_is_fail(monkeypatch):
    """git 缺失是阻断级：提交快照、计划回填、并发检测全靠它。"""
    monkeypatch.setattr("agent_handoff.core.doctor.shutil.which", lambda _n: None)
    r = run_doctor()
    assert _one(r, "git")["level"] == FAIL
    assert r["level"] == FAIL


def test_zstd_missing_is_warn_not_fail(monkeypatch, tmp_path):
    """缺 zstd 时工具照常工作，只是压缩归档的会话读不到正文。

    这一条必须是 warn：让它把退出码搞成非零，会逼人在 CI 里关掉整条检查，
    那比不检查更糟。
    """
    monkeypatch.setattr("agent_handoff.core.doctor.zstd_opener", lambda: None)
    # 数据根要给一个真实存在且有转录的目录：conftest 把 HOME 指向空的临时目录，
    # 于是 `roots.none` 会判 FAIL——那会盖住这里要验的「zstd 缺失只是 warn」。
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "rollout-x.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "agent_handoff.core.doctor.agent_session_roots",
        lambda: [("Codex", root)],
    )
    r = run_doctor()
    assert _one(r, "zstd")["level"] == WARN
    assert r["level"] != FAIL, "warn 不该把整体判成不能跑"


def test_zstd_present_names_the_implementation(monkeypatch):
    """哪个实现被用上了对排查有用：标准库与第三方的行为略有差异。"""
    monkeypatch.setattr("agent_handoff.core.doctor.zstd_opener", lambda: (lambda p: None))
    c = _one(run_doctor(), "zstd")
    assert c["level"] == OK
    assert "impl" in c["data"]


def test_env_pointing_at_a_missing_directory_is_warn(monkeypatch, tmp_path):
    """「设了但指错」比「没设」更难查：没设时用默认路径还能扫到东西。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "definitely-not-here"))
    envs = _by_key(run_doctor(), "env")
    hit = [c for c in envs if c["data"]["name"] == "CODEX_HOME"]
    assert hit, "设了 CODEX_HOME 却没被报告"
    assert hit[0]["level"] == WARN
    assert hit[0]["data"]["exists"] is False


def test_env_pointing_at_a_real_directory_is_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hit = [c for c in _by_key(run_doctor(), "env") if c["data"]["name"] == "CODEX_HOME"]
    assert hit and hit[0]["level"] == OK
    assert hit[0]["data"]["exists"] is True


def test_unset_env_is_not_reported(monkeypatch):
    """没设的变量不该占一行：诊断输出要短到能一眼看完。"""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    names = [c["data"]["name"] for c in _by_key(run_doctor(), "env")]
    assert "CODEX_HOME" not in names


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "12.5"])
def test_context_window_typo_is_warn(monkeypatch, bad):
    """笔误会让整列占用率失真，而 `_declared_window` 会静默忽略它。"""
    monkeypatch.setenv("AGENT_HANDOFF_CONTEXT_WINDOW", bad)
    hit = [c for c in _by_key(run_doctor(), "env")
           if c["data"]["name"] == "AGENT_HANDOFF_CONTEXT_WINDOW"]
    assert hit, "设了却没被报告"
    assert hit[0]["level"] == WARN


def test_context_window_valid_is_ok(monkeypatch):
    monkeypatch.setenv("AGENT_HANDOFF_CONTEXT_WINDOW", "200000")
    hit = [c for c in _by_key(run_doctor(), "env")
           if c["data"]["name"] == "AGENT_HANDOFF_CONTEXT_WINDOW"]
    assert hit and hit[0]["level"] == OK
    assert hit[0]["data"]["parsed"] == 200000


def test_writable_probe_cleans_up_after_itself(tmp_path, monkeypatch):
    """唯一的写操作是一个几字节的探针文件，且必须删掉。

    一个诊断工具在临时目录里留垃圾，等于每次自检都让磁盘多一个文件。
    """
    monkeypatch.setattr("agent_handoff.core.doctor.tempfile.gettempdir", lambda: str(tmp_path))
    before = set(tmp_path.iterdir())
    c = _one(run_doctor(), "writable")
    assert c["level"] in (OK, FAIL)
    leftovers = {p for p in tmp_path.iterdir() if p.name.startswith("agent-handoff-doctor-")}
    assert not leftovers, f"探针文件没删掉：{leftovers}"
    assert set(tmp_path.iterdir()) == before


def test_roots_are_counted_by_name_only(monkeypatch, tmp_path):
    """只数文件名，不读正文——读正文可能是几百 MB。"""
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "rollout-a.jsonl").write_text("not valid json at all", encoding="utf-8")
    (root / "rollout-b.jsonl.zst").write_bytes(b"\x00\x01garbage")
    (root / "notes.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(
        "agent_handoff.core.doctor.agent_session_roots",
        lambda: [("Codex", root)],
    )
    r = run_doctor()
    c = _one(r, "root")
    # 正文全是垃圾也不影响计数——这一项刻意不解析内容。
    assert c["data"]["count"] == 2, "只应数两个转录（.txt 不算）"
    assert c["data"]["compressed"] == 1
    assert r["transcripts"] == 2


def test_empty_root_is_warn_not_silence(monkeypatch, tmp_path):
    """目录在但一份转录都没有：不是错误，但要说出来，否则用户以为扫描坏了。"""
    monkeypatch.setattr(
        "agent_handoff.core.doctor.agent_session_roots",
        lambda: [("Codex", tmp_path)],
    )
    assert _one(run_doctor(), "root")["level"] == WARN


def test_no_roots_at_all_is_fail(monkeypatch):
    monkeypatch.setattr("agent_handoff.core.doctor.agent_session_roots", lambda: [])
    r = run_doctor()
    assert _one(r, "roots.none")["level"] == FAIL
    assert r["level"] == FAIL


def test_unreadable_root_does_not_crash_the_scan(monkeypatch, tmp_path):
    """一个目录坏掉不该让其余的也报不出来。"""
    def boom(_path):
        raise OSError("simulated")

    monkeypatch.setattr(
        "agent_handoff.core.doctor.agent_session_roots",
        lambda: [("Codex", tmp_path)],
    )
    monkeypatch.setattr("agent_handoff.core.doctor.os.scandir", boom)
    r = run_doctor()
    c = _one(r, "root.unreadable")
    assert c["level"] == WARN
    assert c["data"]["error"] == "OSError"


def test_stdio_encoding_is_reported():
    c = _one(run_doctor(), "stdio")
    assert c["data"]["encoding"]
    assert c["data"]["platform"] in ("windows", "posix")
