#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转录扫描。重点：
  · 单遍重写的结果必须与原版三遍逐字一致（身份卡、仓库推断、致命计数）
  · POSIX 路径必须能被认出（原版只认盘符，Linux 上永远推断不出仓库）
  · 判定区间边界严格按实测断点
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from agent_handoff.core.vitals import (
    _DIGEST_SEP_RE,
    BAND_ORDER,
    VITALS_BANDS,
    _newest_files,
    band_for,
    clear_cache,
    find_sessions,
    group_by_agent,
    scan_one,
    scan_session_vitals,
    sessions_for_repo,
)
from agent_handoff.platform import iter_path_candidates


def _write_jsonl(fp: Path, rows: list[dict]) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _pad(fp: Path, target: int) -> None:
    """把文件填到指定体积，用于测试判定区间。填充行是合法 JSON，不含任何信号。"""
    with fp.open("a", encoding="utf-8") as fh:
        filler = json.dumps({"type": "noise", "pad": "x" * 400}) + "\n"
        while fp.stat().st_size < target:
            fh.write(filler * 50)
            fh.flush()


# ── 判定区间 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "size,band",
    [
        (0, "ok"),
        (250_000, "ok"),
        (999_999, "ok"),
        (1_000_000, "watch"),
        (2_999_999, "watch"),
        (3_000_000, "high"),
        (7_999_999, "high"),
        (8_000_000, "critical"),
        (50_000_000, "critical"),
    ],
)
def test_band_boundaries(size, band):
    assert band_for(size) == band


def test_bands_cover_zero():
    """末项阈值必须是 0，否则小文件会落空。原版靠这个不变式才不会 band 未设。"""
    assert VITALS_BANDS[-1][0] == 0
    assert set(BAND_ORDER) == {name for _, name in VITALS_BANDS}


# ── 跨平台路径识别 ────────────────────────────────────────────────────

def test_iter_path_candidates_finds_windows_paths():
    got = list(iter_path_candidates(r'请看 C:\Users\me\proj\file.py 里的问题'))
    assert any(c.startswith("C:\\Users\\me\\proj") for c in got)


def test_iter_path_candidates_finds_posix_paths():
    """原版只有盘符正则，Linux 上 guess_repos 永远返回空。"""
    got = list(iter_path_candidates("look at /home/me/proj/src/file.py please"))
    assert any(c.startswith("/home/me/proj") for c in got)


def test_iter_path_candidates_strips_cjk_punctuation():
    got = list(iter_path_candidates("路径是 /home/me/proj/x.py。然后"))
    assert "/home/me/proj/x.py" in got


def test_iter_path_candidates_both_forms_in_one_text():
    text = r'WSL 里是 /mnt/c/Users/me/proj，Windows 里是 C:\Users\me\proj'
    got = list(iter_path_candidates(text))
    assert any(c.startswith("/mnt/c/Users/me/proj") for c in got)
    assert any(c.startswith("C:\\Users\\me\\proj") for c in got)


# ── 身份卡提取 ────────────────────────────────────────────────────────

def test_scan_one_claude_identity(tmp_path: Path):
    fp = tmp_path / "claude.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "abc12345", "cwd": "/home/me/proj",
         "gitBranch": "feature/x", "version": "1.2.3"},
        {"type": "user", "message": {"content": [{"type": "text", "text": "帮我修一下登录 bug"}]}},
    ])
    row = scan_one("Claude Code", fp)
    assert row is not None
    assert row.session_id == "abc12345"
    assert row.cwd == "/home/me/proj"
    assert row.branch == "feature/x"
    assert row.version == "1.2.3"
    assert row.first_prompt == "帮我修一下登录 bug"


def test_scan_one_skips_injected_prompt_noise(tmp_path: Path):
    """插件清单与 caveman 广播不是人问的问题，认成开场提问会丢辨识度。"""
    fp = tmp_path / "claude.jsonl"
    _write_jsonl(fp, [
        {"type": "user", "message": {"content": [{"type": "text", "text": "<recommended_plugins>a b c</recommended_plugins>"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "CAVEMAN MODE ACTIVE"}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "真正的问题在这里"}]}},
    ])
    row = scan_one("Claude Code", fp)
    assert row.first_prompt == "真正的问题在这里"


def test_scan_one_codex_session_meta(tmp_path: Path):
    fp = tmp_path / "rollout-2026-08-18T10-00-00-thread999.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "meta777", "cwd": "/srv/app",
                                             "cli_version": "0.9", "originator": "desktop"}},
        {"type": "response_item", "payload": {"role": "user", "content": [{"input_text": "开始"}]}},
    ])
    row = scan_one("Codex", fp)
    # 文件名 id 优先（UI 显示的是它），meta 里的 id 并列保留为源线程。
    assert row.session_id == "thread999"
    assert row.thread_id == "meta777"
    assert row.origin == "desktop"
    assert row.version == "0.9"


def test_scan_one_codex_first_meta_wins(tmp_path: Path):
    """派生 / 续接会带多个 session_meta；第一个才是这个文件自己的身份。"""
    fp = tmp_path / "rollout-2026-08-18T10-00-00-aaa.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "first", "cwd": "/one"}},
        {"type": "session_meta", "payload": {"session_id": "second", "cwd": "/two"}},
    ])
    row = scan_one("Codex", fp)
    assert row.cwd == "/one"
    assert row.thread_id == "first"


def test_scan_one_session_id_falls_back_to_filename(tmp_path: Path):
    fp = tmp_path / "rollout-2026-08-18T10-00-00-fallbackid.jsonl"
    _write_jsonl(fp, [{"type": "noise"}])
    assert scan_one("Codex", fp).session_id == "fallbackid"


def test_scan_one_counts_fatal_and_errors(tmp_path: Path):
    """致命签名必须锚定在错误载荷上，不能在整行原文里裸匹配。

    实测本机 14 个主转录：239 个裸匹配命中里 94 个来自 assistant 正文、
    83 个来自 user 正文——那是人和模型在**讨论**这些词，不是会话真的死了。
    """
    fp = tmp_path / "c.jsonl"
    with fp.open("w", encoding="utf-8") as fh:
        # 真的错误载荷：字段值里出现签名。
        fh.write('{"type":"event_msg","error":{"message":"503: 所有供应商已熔断，无可用渠道"}}\n')
        fh.write('{"type":"event_msg","error_message":"content-blocked by upstream"}\n')
        fh.write('{"type":"event_msg","reason":"IMAGE_DIMENSION_EXCEEDED"}\n')
        # 只是在讨论这些词：不能计数。
        fh.write('{"type":"user","message":{"content":"遇到 content-blocked 时要重试"}}\n')
        fh.write('{"type":"assistant","message":{"content":"熔断 是一种保护机制"}}\n')
        fh.write('{"type":"tool_result","is_error":true}\n')
        fh.write('{"type":"tool_result","isError":true}\n')
    row = scan_one("Claude Code", fp)
    assert row.fatal == 3, "只数真的错误载荷"
    assert row.errors == 2


def test_fatal_signature_ignores_discussion(tmp_path: Path):
    """整份转录只是在讨论这些词时，fatal 必须为 0。

    否则 17/24 个转录被标 fatal>0（其中 16 个体积完全健康），风险列变成噪声，
    用户照徽章决策就会漏掉真正出事的会话。
    """
    fp = tmp_path / "talk.jsonl"
    with fp.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"user","message":{"content":"出现 content-blocked、所有供应商已熔断 时怎么办"}}\n')
        fh.write('{"type":"assistant","message":{"content":"无可用渠道 一般是配额问题"}}\n')
    row = scan_one("Claude Code", fp)
    assert row.fatal == 0


def test_scan_one_counts_aborted_turns(tmp_path: Path):
    """被用户打断的轮次要单独计数。

    实测 Codex 侧 `is_error` 在 40 个 rollout 里只有 3 次，而 `turn_aborted`
    有 6 次——后者才是「这轮没做完」的真实信号，而把半成品当成已完成是
    交接里最贵的误判。
    """
    fp = tmp_path / "rollout-2026-08-21T00-00-00-ab.jsonl"
    with fp.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"session_meta","payload":{"session_id":"ab","cwd":"/p"}}\n')
        fh.write('{"type":"event_msg","payload":{"type":"turn_aborted"}}\n')
        fh.write('{"type":"event_msg","payload":{"reason":"interrupted"}}\n')
    row = scan_one("Codex", fp)
    assert row.aborted == 2


def test_band_accounts_for_failures():
    """体积衡量「还能撑多久」，fatal/aborted 衡量「已经出事了没有」。

    原版只看体积，于是 0.9 MB 但撞过熔断的会话被标「健康」，
    而 1.7 MB 一切正常的被标「留意」——用户照徽章决策会看错优先级。
    """
    assert band_for(900_000) == "ok"
    assert band_for(900_000, fatal=1) == "watch"
    assert band_for(900_000, fatal=7) == "high"
    assert band_for(100_000, aborted=4) == "high"
    # 体积已经更严重时不能被往下拉。
    assert band_for(9_000_000, fatal=1) == "critical"


# ── 上下文占用判据 ────────────────────────────────────────────────────

def test_tokens_outrank_size():
    """有 token 数就按它判，体积只是兜底。

    实测本机：1.0 MB 的会话已用 194183 token，体积判据说「健康」；
    27.4 MB 的会话峰值 710340。体积与占用严重脱钩，谁都不能替代谁。
    """
    # 小文件 + 高占用 -> 按占用判，不能因为文件小就说健康。
    assert band_for(500_000) == "ok"                      # 只看体积
    assert band_for(500_000, tokens=194_183) == "critical"  # 实测本会话的数字
    # 大文件 + 低占用 -> 按占用判，不能因为文件大就喊立刻交接。
    assert band_for(9_000_000) == "critical"
    assert band_for(9_000_000, tokens=20_000) == "ok"


def test_fullness_uses_the_window_when_the_transcript_gives_one():
    """Codex 在 `model_context_window` 里写了上限，占用率可以直接算。

    实测一个 Codex 会话 121407 / 121600 = 99.8%——真顶到上限了，
    而体积判据只说「尽快交接」。
    """
    assert band_for(0, tokens=121_407, window=121_600) == "critical"
    assert band_for(0, tokens=95_000, window=121_600) == "high"     # 78%
    assert band_for(0, tokens=70_000, window=121_600) == "watch"    # 58%
    assert band_for(0, tokens=30_000, window=121_600) == "ok"       # 25%
    # 同样的占用量，窗口大一倍就没那么紧张。
    assert band_for(0, tokens=95_000, window=400_000) == "ok"


def test_compaction_history_raises_the_band():
    """压缩过就是满过——这是历史事实，不是推断。

    自动压缩只在快装不下时才触发，所以压缩过的会话直接按「满过」对待，
    而不是拿压缩前的占用去对照阈值：那个数字看着可能只有 167k，
    但它恰恰是触发压缩的那一刻，等于 100%。

    实测一个 1.9 MB 的会话自动压缩过 10 次：体积判据说「留意」，
    但它已经反复丢掉早期事实，是最该交接的那一类。
    """
    assert band_for(1_900_000, tokens=1_000) == "ok"
    assert band_for(1_900_000, tokens=1_000, compactions=1) == "high"
    assert band_for(1_900_000, tokens=1_000, compactions=10) == "critical"
    # 占用已经更严重时不能被往下拉。
    assert band_for(0, tokens=190_000, compactions=1) == "critical"


def test_zero_tokens_falls_back_to_size():
    """读不到 token 时必须退回体积，不能一律判成健康。

    Claude 的旧转录、被截断的文件、非深度扫描都可能没有 usage 记录。
    那种情况下体积仍然是唯一可用的信号。
    """
    assert band_for(9_000_000, tokens=0) == "critical"
    assert band_for(0, tokens=0) == "ok"


def test_scan_reads_claude_usage(tmp_path: Path):
    """Claude 的占用在 `message.usage` 里，等于新输入加两种缓存读入。

    取峰值而不是末值：实测两个真转录的末条 assistant 占用都是 0
    （子代理或没有 cache 记账），照末值判会把满会话判成空的。
    """
    fp = tmp_path / "claude.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "u1", "cwd": "/p"},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 5_000, "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 1_000, "output_tokens": 900}}},
        # 峰值出现在中间，末条是 0——必须取到峰值。
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 194_183, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 987}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 0, "output_tokens": 0}}},
    ])
    row = scan_one("Claude Code", fp)
    assert row.tokens == 194_183, "取峰值，且 output_tokens 不算占用"
    assert row.context_window == 0, "Claude 转录里没有上限"
    assert row.band == "critical"


def test_scan_reads_codex_token_count(tmp_path: Path):
    """Codex 把占用和上限都写在 token_count 事件里。

    `last_token_usage` 才是当前占用；`total_token_usage` 是全会话累计，
    会远超窗口，当占用用会永远判成爆满。
    """
    fp = tmp_path / "rollout-2026-08-22T06-00-00-cx.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "cx", "cwd": "/p"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 999_999, "total_tokens": 999_999},
            "last_token_usage": {"input_tokens": 121_407, "total_tokens": 123_000},
            "model_context_window": 121_600}}},
    ])
    row = scan_one("Codex", fp)
    assert row.tokens == 121_407, "用 last_token_usage，不是 total"
    assert row.context_window == 121_600
    assert row.band == "critical"        # 99.8% 占用


def test_scan_counts_claude_compaction_boundaries(tmp_path: Path):
    """Claude 的压缩事件藏在 `type:"system"` 里，带 preTokens。

    按顶层 subtype 找是对的，但要注意它不是独立的记录类型——
    实测一个未压缩过的转录里根本没有这个字段，容易误以为 Claude 不写压缩事件。
    """
    fp = tmp_path / "claude2.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "c2", "cwd": "/p"},
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 167_941, "durationMs": 245_296}},
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 150_000}},
    ])
    row = scan_one("Claude Code", fp)
    assert row.compactions == 2
    # 压缩把占用打回低位，只看压缩后的峰值会低估这个会话到过多满。
    assert row.tokens == 167_941
    assert row.band == "critical"


def test_scan_survives_transcripts_without_any_token_data(tmp_path: Path):
    """没有 usage / token_count 的转录不能崩，也不能谎报占用为某个值。"""
    fp = tmp_path / "bare.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "bare", "cwd": "/p"},
        {"type": "user", "message": {"content": [{"type": "text", "text": "你好"}]}},
    ])
    row = scan_one("Claude Code", fp)
    assert row.tokens == 0
    assert row.context_window == 0
    assert row.compactions == 0
    assert row.band == "ok"     # 退回体积判据，文件很小


# ── 原生续接命令 ──────────────────────────────────────────────────────

def test_resume_cmd_for_claude(tmp_path: Path):
    """Claude 用会话 ID 原样续接。"""
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "fc0e9aac-7840-4413-a154-91be846600b6", "cwd": "/p"}])
    row = scan_one("Claude Code", fp)
    assert row.resume_cmd == "claude --resume fc0e9aac-7840-4413-a154-91be846600b6"


def test_resume_cmd_for_codex_takes_the_uuid_tail(tmp_path: Path):
    """Codex 只认 UUID。rollout 文件名前缀自带连字符，取尾部五段比写正则稳。"""
    fp = tmp_path / "rollout-2026-08-22T06-09-26-01a0265f-2c5c-7962-a1b2-7a75df296ab4.jsonl"
    _write_jsonl(fp, [{"type": "session_meta", "payload": {
        "session_id": "01a0265f-2c5c-7962-a1b2-7a75df296ab4", "cwd": "/p"}}])
    row = scan_one("Codex", fp)
    assert row.resume_cmd == "codex resume 01a0265f-2c5c-7962-a1b2-7a75df296ab4"


def test_archived_codex_session_offers_no_resume(tmp_path: Path):
    """归档过的 Codex 会话续接不了——Codex 只在活动目录里找。

    给一条注定失败的命令比不给更糟：用户会以为工具在骗他。
    """
    d = tmp_path / "archived_sessions"
    d.mkdir()
    fp = d / "rollout-2026-08-22T03-36-15-01a025d2-ef0b-7690-bf6c-5671d08765ee.jsonl"
    _write_jsonl(fp, [{"type": "session_meta", "payload": {
        "session_id": "01a025d2-ef0b-7690-bf6c-5671d08765ee", "cwd": "/p"}}])
    row = scan_one("Codex", fp)
    assert row.resume_cmd == ""
    # 归档的 Claude 会话没有这个概念，不能一并禁掉。
    assert scan_one("Claude Code", fp).resume_cmd.startswith("claude --resume")


def test_resume_cmd_empty_without_a_session_id(tmp_path: Path):
    """认不出会话 ID 时不编一条命令出来。"""
    fp = tmp_path / "x.jsonl"
    _write_jsonl(fp, [{"type": "noise"}])
    row = scan_one("Claude Code", fp)
    assert row.session_id == "x"          # 回退到文件名
    assert row.resume_cmd == "claude --resume x"
    row.session_id = ""
    assert row.resume_cmd == ""


def test_resume_cmd_is_in_the_gui_payload(tmp_path: Path):
    """网页界面靠 to_dict 拿数据；漏了这个字段按钮就不会出现。"""
    fp = tmp_path / "c2.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "abc12345", "cwd": "/p"}])
    d = scan_one("Claude Code", fp).to_dict()
    assert d["resume_cmd"] == "claude --resume abc12345"
    for key in ("tokens", "context_window", "compactions"):
        assert key in d, key


# ── 迁机：从别的电脑搬来的转录 ────────────────────────────────────────

def _foreign(tmp_path: Path, name: str = "foreign.jsonl") -> Path:
    """造一份「来自另一台电脑」的转录：cwd 用别人的用户名。"""
    fp = tmp_path / name
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "mig99", "cwd": r"D:\Users\bob\myproj",
         "gitBranch": "main", "version": "2.1.0"},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 160_000, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 400}}},
    ])
    return fp


def test_foreign_transcript_is_flagged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USERNAME", "devin")
    row = scan_one("Claude Code", _foreign(tmp_path))
    assert row.is_foreign is True
    assert row.to_dict()["is_foreign"] is True


def test_foreign_transcript_offers_no_resume(tmp_path: Path, monkeypatch):
    """两个应用都只索引自己数据目录里的会话，拷进来的 jsonl 不在索引里。

    命令一定报「找不到会话」——与归档 Codex 会话同一条原则：
    不给注定失败的命令。
    """
    monkeypatch.setenv("USERNAME", "devin")
    row = scan_one("Claude Code", _foreign(tmp_path))
    assert row.resume_cmd == ""
    # Codex 侧同理，且与「归档」是两条独立的原因。
    cx = tmp_path / "rollout-2026-08-22T00-00-00-01a0aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    _write_jsonl(cx, [{"type": "session_meta", "payload": {
        "session_id": "01a0aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cwd": "/home/alice/x"}}])
    assert scan_one("Codex", cx).resume_cmd == ""


def test_local_transcript_still_offers_resume(tmp_path: Path, monkeypatch):
    """外来判断不能把正常会话也扫进去——那会让 resume 功能整体消失。"""
    monkeypatch.setenv("USERNAME", "devin")
    fp = tmp_path / "local.jsonl"
    # cwd 指向一个真实存在的本机目录。
    _write_jsonl(fp, [{"type": "system", "sessionId": "loc1", "cwd": str(tmp_path)}])
    row = scan_one("Claude Code", fp)
    assert row.is_foreign is False
    assert row.resume_cmd == "claude --resume loc1"


def test_foreign_transcript_still_reports_tokens_and_band(tmp_path: Path, monkeypatch):
    """迁机不影响 token 判据：占用数与机器无关，那正是它比体积可靠的地方。

    外来转录的**内容仍然有价值**——那是迁机时最想带走的东西。所以标注它，
    不是丢弃它。
    """
    monkeypatch.setenv("USERNAME", "devin")
    row = scan_one("Claude Code", _foreign(tmp_path))
    assert row.tokens == 160_000
    assert row.band == "high"      # 160k 落在 TOKEN_BANDS 的 high 区间


def test_scan_one_counts_fatal_past_early_exit_budget(tmp_path: Path):
    """身份提取提前退出后，致命计数仍必须扫到文件尾——原版是分三遍读的，
    单遍重写最容易在这里少数。"""
    fp = tmp_path / "c.jsonl"
    rows = [{"type": "system", "sessionId": "s1", "cwd": "/p"},
            {"type": "user", "message": {"content": "q"}}]
    rows += [{"type": "noise", "i": i} for i in range(500)]
    _write_jsonl(fp, rows)
    # 第 503 行，远超 400 行的身份预算；必须仍被数到。
    with fp.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"event_msg","error":{"message":"content-blocked"}}\n')
    row = scan_one("Claude Code", fp)
    assert row.session_id == "s1"
    assert row.fatal == 1, "提前退出后仍要数完致命签名"


def test_scan_one_tolerates_malformed_json(tmp_path: Path):
    fp = tmp_path / "c.jsonl"
    fp.write_text('not json at all\n{"type":"user","message":{"content":"ok"}}\n', encoding="utf-8")
    row = scan_one("Claude Code", fp)
    assert row is not None and row.first_prompt == "ok"


def test_scan_one_shallow_mode_skips_identity(tmp_path: Path):
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "deep1", "cwd": "/p"}])
    row = scan_one("Claude Code", fp, deep=False)
    assert row.session_id == "c"  # 文件名 stem
    assert row.cwd == ""


def test_scan_one_missing_file(tmp_path: Path):
    assert scan_one("Claude Code", tmp_path / "nope.jsonl") is None


# ── 仓库推断 ──────────────────────────────────────────────────────────

def test_repo_inferred_from_cwd(tmp_path: Path):
    proj = tmp_path / "realproj"
    (proj / ".git").mkdir(parents=True)
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "s", "cwd": str(proj)}])
    row = scan_one("Claude Code", fp)
    assert row.repo and Path(row.repo).name == "realproj"


def test_repo_inferred_from_prompt_text_posix(tmp_path: Path):
    """Codex Desktop 的 cwd 是任务容器，项目只在开头几轮的文本里出现。"""
    proj = tmp_path / "fromtext"
    (proj / "sub").mkdir(parents=True)
    (proj / ".git").mkdir()
    container = tmp_path / "container"
    container.mkdir()
    fp = tmp_path / "r.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "s", "cwd": str(container)}},
        {"type": "response_item", "payload": {"role": "user",
         "content": [{"input_text": f"看看 {(proj / 'sub').as_posix()} 里的代码"}]}},
    ])
    row = scan_one("Codex", fp)
    assert any(Path(r).name == "fromtext" for r in row.repos)


@pytest.mark.parametrize(
    "line,want",
    [
        (r'{"cwd": "C:\\Users\\me\\proj"}', True),
        ('{"cwd": "C:/Users/me/proj"}', True),
        ('{"text": "/home/me/proj/x.py"}', True),
        # 早先的预筛是一张目录白名单，下面这些前缀全漏。漏掉的后果是那一行的
        # 路径永远不被提取、仓库推断静默失败——真 Linux 上跑测试才暴露。
        ('{"text": "/tmp/build/x.py"}', True),
        ('{"text": "/workspace/app/main.go"}', True),
        ('{"text": "/data/repos/thing"}', True),
        ('{"text": "/media/disk/proj"}', True),
        ('{"text": "/nix/store/abc/pkg"}', True),
        # 没有连续两段路径的行不值得付正则代价。
        ('{"type": "user"}', False),
        ('{"text": "just words here"}', False),
        ('{"text": "a/b"}', False),
        ("", False),
    ],
)
def test_looks_pathy_is_structural_not_a_whitelist(line: str, want: bool):
    from agent_handoff.core.vitals import _looks_pathy

    assert _looks_pathy(line) is want, line


def test_repo_inferred_from_unusual_posix_root(tmp_path: Path):
    """项目在 /tmp、/workspace 这类不在任何白名单里的位置也要能推断出来。"""
    proj = tmp_path / "oddplace"
    (proj / ".git").mkdir(parents=True)
    fp = tmp_path / "u.jsonl"
    _write_jsonl(fp, [
        {"type": "user", "sessionId": "s",
         "message": {"content": f"fix the bug in {proj.as_posix()}/main.py"}},
    ])
    row = scan_one("Claude Code", fp)
    assert any(Path(r).name == "oddplace" for r in row.repos), row.repos


def test_home_root_ranked_last(tmp_path: Path, monkeypatch):
    """裸的用户主目录从来不是被操作的对象，即使它恰好是个 git 仓库。"""
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    real = tmp_path / "realwork"
    (real / ".git").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "s", "cwd": str(home)},
        {"type": "user", "message": {"content": f"see {real.as_posix()}/x.py"}},
    ])
    row = scan_one("Claude Code", fp)
    assert Path(row.repos[0]).name == "realwork"


# ── 排序与查找 ────────────────────────────────────────────────────────

def _row(band_size: int, agent="Claude Code", **kw):
    from agent_handoff.core.vitals import SessionRow

    return SessionRow(
        agent=agent, path=Path("/x/y.jsonl"), file=kw.pop("file", "y.jsonl"),
        mtime=kw.pop("mtime", datetime(2026, 8, 18, 12, 0, 0)),
        mb=band_size / 1e6, size=band_size, fatal=kw.pop("fatal", 0),
        errors=0, band=band_for(band_size), **kw,
    )


def test_find_sessions_ranks_id_over_prompt():
    a = _row(1000, session_id="deadbeef", first_prompt="nothing")
    b = _row(9_000_000, session_id="other", first_prompt="deadbeef mentioned here")
    got = find_sessions("deadbeef", [b, a])
    assert got[0] is a, "ID 精确匹配必须排在提问文本匹配之前"


# ── 按 APP 分组 ───────────────────────────────────────────────────────
# 人认会话是先认 APP、再认时间。混在一起时"上一个会话"只能一行行看客户端字段找。

def test_group_by_agent_splits_by_app():
    rows = [
        _row(1000, agent="Codex", mtime=datetime(2026, 8, 18, 10)),
        _row(1000, agent="Claude Code", mtime=datetime(2026, 8, 18, 9)),
        _row(1000, agent="Codex", mtime=datetime(2026, 8, 18, 8)),
    ]
    got = group_by_agent(rows)
    assert [agent for agent, _ in got] == ["Codex", "Claude Code"]
    assert [len(g) for _, g in got] == [2, 1]


def test_group_by_agent_newest_first_within_group():
    old = _row(9_000_000, agent="Codex", mtime=datetime(2026, 7, 1))
    mid = _row(1000, agent="Codex", mtime=datetime(2026, 8, 10))
    new = _row(500, agent="Codex", mtime=datetime(2026, 8, 18))
    _, group = group_by_agent([mid, old, new])[0]
    assert group == [new, mid, old], "组内必须严格按时间倒序，体积不参与"


def test_group_order_follows_most_recent_activity():
    """刚用过的 APP 出现在最上面，即使它的转录更小、风险更低。"""
    rows = [
        _row(9_000_000, agent="Claude Code", mtime=datetime(2026, 8, 1)),
        _row(1000, agent="Codex", mtime=datetime(2026, 8, 18)),
    ]
    assert [a for a, _ in group_by_agent(rows)] == ["Codex", "Claude Code"]


def test_group_by_agent_does_not_drop_rows():
    rows = [_row(1000, agent=a, mtime=datetime(2026, 8, 18, i)) for i, a in enumerate(["A", "B", "A", "C"])]
    got = group_by_agent(rows)
    assert sum(len(g) for _, g in got) == len(rows)


def test_group_by_agent_empty():
    assert group_by_agent([]) == []


def test_group_by_agent_stable_for_same_timestamp():
    """时间戳相同时按 APP 名排，保证两次运行结果一致。"""
    ts = datetime(2026, 8, 18, 12)
    rows = [_row(1000, agent="Zed", mtime=ts), _row(1000, agent="Aider", mtime=ts)]
    assert [a for a, _ in group_by_agent(rows)] == ["Aider", "Zed"]
    assert [a for a, _ in group_by_agent(list(reversed(rows)))] == ["Aider", "Zed"]


def test_find_sessions_prefers_recent_within_same_rank():
    """原版同级按体积排，会把一个月前的大转录顶到前面。找会话时"最近那个"才对。"""
    old = _row(9_000_000, cwd="/p/myproj", mtime=datetime(2026, 7, 1))
    new = _row(500_000, cwd="/p/myproj", mtime=datetime(2026, 8, 18))
    got = find_sessions("myproj", [old, new])
    assert got[0] is new


def test_find_sessions_matches_cwd_and_repos():
    r = _row(1000, cwd="/home/me/kirara", repos=["/home/me/kirara"])
    assert find_sessions("kirara", [r]) == [r]
    assert find_sessions("KIRARA", [r]) == [r]


def test_find_sessions_empty_needle():
    assert find_sessions("   ", [_row(1000)]) == []


def test_find_sessions_no_match():
    assert find_sessions("zzz", [_row(1000, session_id="abc")]) == []


def test_sessions_for_repo_matches_subdirectories(tmp_path: Path):
    a = _row(1000, cwd="/p/proj")
    b = _row(1000, cwd="/p/proj/sub/deeper")
    c = _row(1000, cwd="/p/other")
    got = sessions_for_repo(Path("/p/proj"), [a, b, c])
    assert len(got) == 2
    assert all(g is a or g is b for g in got)
    assert c not in got


def test_sessions_for_repo_ignores_case_and_slashes():
    a = _row(1000, cwd=r"C:\Users\Me\Proj")
    got = sessions_for_repo(Path("c:/users/me/proj"), [a])
    assert got == [a]


def test_sessions_for_repo_matches_via_repos_not_only_cwd():
    """Codex 的 cwd 是任务沙箱，只比 cwd 会让这个函数对所有 Codex 会话恒返回空。

    实测 20/20 个 rollout 的 cwd 都落在 Documents/Codex/<日期>/<slug> 下，
    解析不到任何 git 仓库；真正的仓库只出现在会话正文里。
    """
    sandbox = _row(1000, cwd=r"C:\Users\Me\Documents\Codex\2026-08-21\slug",
                   repos=[r"E:\output\proj"])
    got = sessions_for_repo(Path("E:/output/proj"), [sandbox])
    assert got == [sandbox]


# ── 会话内容提取 ──────────────────────────────────────────────────────

def test_extracts_claude_title_and_last_prompt(tmp_path: Path):
    """ai-title 与 last-prompt 出现在文件末尾，早停之后才轮到它们。"""
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "s1", "cwd": "/p"},
        {"type": "user", "message": {"content": "开场提问"}},
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "工作流撤销重做验证"},
        {"type": "last-prompt", "sessionId": "s1", "lastPrompt": "继续修 undo"},
    ])
    row = scan_one("Claude Code", fp)
    assert row.title == "工作流撤销重做验证"
    assert row.last_prompt == "继续修 undo"
    # 话题优先用 AI 标题：会话 ID 前八位对人没有意义。
    assert row.label == "工作流撤销重做验证"


def test_extracts_codex_compaction_digest(tmp_path: Path):
    """Codex 的 compacted 事件带着模型自己写的交接摘要——最有价值的一段。"""
    fp = tmp_path / "rollout-2026-08-21T00-00-00-abc.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "abc", "cwd": "/p"}},
        {"type": "compacted", "payload": {"message": "# 交接摘要\n\n## 当前进度\n\n已完成 Task 5"}},
    ])
    row = scan_one("Codex", fp)
    assert "已完成 Task 5" in row.digest
    # 标题行本身没有信息量，话题要取第一句实质内容。
    assert row.label == "已完成 Task 5"


def test_digest_strips_codex_boilerplate_preamble(tmp_path: Path):
    """压缩摘要前面那段固定英文说明对每个会话都一样，留着会挤掉正文。"""
    fp = tmp_path / "rollout-2026-08-21T00-00-00-abc.jsonl"
    preamble = (
        "Another language model started to solve this problem and produced a summary "
        "of its thinking process. Here is the summary produced by the other language "
        "model, use the information in this summary to assist with your own analysis:"
    )
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "abc", "cwd": "/p"}},
        {"type": "compacted", "payload": {"message": preamble + "\n# 交接摘要\n\n真正的内容"}},
    ])
    row = scan_one("Codex", fp)
    assert not row.digest.startswith("Another language model")
    assert row.digest.startswith("# 交接摘要")


def test_all_compaction_windows_are_kept(tmp_path: Path):
    """每个压缩窗口只总结它自己那一段，后一个**不**包含前一个。

    实测本机 70 个带压缩的 rollout（52 个多窗口，最多 19 个）：`window_number`
    递增、`previous_window_id` 串链，且没有任何样本的末窗逐字包含首窗。
    只留最后一个会丢掉中位 78% 的具体事实，其中 11 个 rollout 的「用户目标 /
    红线」只出现在早期窗口——恰恰最不能丢。
    """
    fp = tmp_path / "rollout-2026-08-21T00-00-00-abc.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "abc", "cwd": "/p"}},
        {"type": "compacted", "payload": {"message": "用户目标：迁移预设", "window_number": 1}},
        {"type": "compacted", "payload": {"message": "已完成 Task 5", "window_number": 2}},
        {"type": "compacted", "payload": {"message": "已推送并打 tag", "window_number": 3}},
    ])
    row = scan_one("Codex", fp)
    assert row.digest_windows == 3
    # 三段内容都在，一段都不能少。
    for expected in ("用户目标：迁移预设", "已完成 Task 5", "已推送并打 tag"):
        assert expected in row.digest, expected
    # 顺序必须是时间顺序，否则「当前进度」会互相矛盾。
    assert row.digest.index("用户目标") < row.digest.index("已完成 Task 5")
    assert row.digest.index("已完成 Task 5") < row.digest.index("已推送并打 tag")


def test_single_window_needs_no_separator(tmp_path: Path):
    """只有一个窗口时不加分隔标题——那是噪声。"""
    fp = tmp_path / "rollout-2026-08-21T00-00-00-one.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "one", "cwd": "/p"}},
        {"type": "compacted", "payload": {"message": "唯一的摘要", "window_number": 1}},
    ])
    row = scan_one("Codex", fp)
    assert row.digest == "唯一的摘要"
    assert row.digest_windows == 1


def test_window_separator_carries_no_natural_language(tmp_path: Path):
    """分隔标记不能带任何语言的词。

    digest 原样进交接文档，而这一层是纯解析、拿不到 Translator。
    写死中文会让 `--lang en` 的文档里冒出中文标题；改成纯符号 `[i/n]`。
    """
    fp = tmp_path / "rollout-2026-08-21T00-00-00-sep.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "sep", "cwd": "/p"}},
        {"type": "compacted", "payload": {"message": "第一段", "window_number": 1}},
        {"type": "compacted", "payload": {"message": "第二段", "window_number": 2}},
    ])
    row = scan_one("Codex", fp)
    seps = [ln for ln in row.digest.splitlines() if _DIGEST_SEP_RE.match(ln.strip())]
    assert len(seps) == 2, row.digest
    for ln in seps:
        assert not re.search(r"[一-鿿]", ln), ln
        assert not re.search(r"[A-Za-z]", ln), ln
    assert "[1/2]" in row.digest and "[2/2]" in row.digest


def test_topic_skips_the_window_separator(tmp_path: Path):
    """话题要跳过分隔行取正文。分隔标记换了写法，识别式必须跟着换。"""
    fp = tmp_path / "rollout-2026-08-21T00-00-00-top.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "top", "cwd": "/p"}},
        {"type": "compacted", "payload": {"message": "# 交接摘要\n迁移预设到新的配置格式", "window_number": 1}},
        {"type": "compacted", "payload": {"message": "继续收尾并补测试", "window_number": 2}},
    ])
    row = scan_one("Codex", fp)
    assert not _DIGEST_SEP_RE.match(row.label.strip())
    assert "[1/2]" not in row.label
    assert row.label.startswith("迁移预设")


def test_digest_is_not_truncated(tmp_path: Path):
    """摘要不再截断：实测 62/70 个末窗超过 4000 字符，中位被切掉 2925 字符。

    截断点落在哪完全取决于模型当时怎么分段，切掉的往往正是结论与待办。
    """
    long_msg = "开头结论\n" + ("详细过程 " * 3000) + "\n结尾的待办事项"
    fp = tmp_path / "rollout-2026-08-21T00-00-00-long.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "long", "cwd": "/p"}},
        {"type": "compacted", "payload": {"message": long_msg, "window_number": 1}},
    ])
    row = scan_one("Codex", fp)
    assert len(row.digest) > 10000
    assert "结尾的待办事项" in row.digest


def test_user_asks_are_kept_verbatim(tmp_path: Path):
    """压缩摘要是模型的转述；转述会丢措辞里的约束。

    `replacement_history` 里保留着被摘要替换掉的原始 user 消息——实测本机
    一个 rollout 的「将项目B推送到 GitHub 并创建 tag」只存在于这里，
    任何摘要都没有逐字保留它。
    """
    fp = tmp_path / "rollout-2026-08-21T00-00-00-ask.jsonl"
    _write_jsonl(fp, [
        {"type": "session_meta", "payload": {"session_id": "ask", "cwd": "/p"}},
        {"type": "compacted", "payload": {
            "message": "摘要：用户要求发布",
            "window_number": 1,
            "replacement_history": [
                {"role": "user", "content": [{"text": "不要删除项目 A，也不要强制推送"}]},
                {"role": "developer", "content": [{"text": "<app-context>忽略我</app-context>"}]},
                {"role": "user", "content": [{"text": "# Files mentioned by the user: 忽略我"}]},
            ],
        }},
    ])
    row = scan_one("Codex", fp)
    assert row.asks == ["不要删除项目 A，也不要强制推送"]


def test_label_falls_back_to_first_prompt(tmp_path: Path):
    """没有标题也没有摘要时才退回开场提问。"""
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "s", "cwd": "/p"},
        {"type": "user", "message": {"content": "帮我看看这个 bug"}},
    ])
    row = scan_one("Claude Code", fp)
    assert row.label == "帮我看看这个 bug"


def test_skips_slash_command_boilerplate_prompt(tmp_path: Path):
    """斜杠命令回显对所有会话都一样，认成开场提问会让卡片失去辨识度。"""
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [
        {"type": "system", "sessionId": "s", "cwd": "/p"},
        {"type": "user", "message": {"content": "<local-command-caveat>Caveat: the messages below…"}},
        {"type": "user", "message": {"content": "这才是真正的提问"}},
    ])
    row = scan_one("Claude Code", fp)
    assert row.first_prompt == "这才是真正的提问"


def test_find_sessions_matches_topic_and_digest():
    """用户记得的是「那次改撤销的对话」，不是 ID 前缀。"""
    a = _row(1000, session_id="aaa", title="工作流撤销重做")
    b = _row(1000, session_id="bbb", digest="修了模型目录的竞态")
    assert find_sessions("撤销", [a, b]) == [a]
    assert find_sessions("竞态", [a, b]) == [b]


# ── 子代理转录 ────────────────────────────────────────────────────────

def test_newest_files_excludes_subagent_transcripts(tmp_path: Path):
    """子代理数量远超主会话，混排时几乎总在前面，会把 limit 吃满。

    实测本机最新 12 个 Claude 文件里 7 个是子代理转录。
    """
    root = tmp_path / "projects" / "proj"
    (root / "sess" / "subagents").mkdir(parents=True)
    main = root / "main.jsonl"
    main.write_text("{}\n", encoding="utf-8")
    sub = root / "sess" / "subagents" / "agent-abc.jsonl"
    sub.write_text("{}\n", encoding="utf-8")
    # 让子代理更新，确保它按 mtime 会排在前面。
    os.utime(main, (1_700_000_000, 1_700_000_000))
    os.utime(sub, (1_700_009_999, 1_700_009_999))

    assert [p.name for p in _newest_files(root, 10)] == ["main.jsonl"]
    both = {p.name for p in _newest_files(root, 10, include_subagents=True)}
    assert both == {"main.jsonl", "agent-abc.jsonl"}


def test_scan_one_flags_subagent(tmp_path: Path):
    d = tmp_path / "sess" / "subagents"
    d.mkdir(parents=True)
    fp = d / "agent-x.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "s", "cwd": "/p"}])
    assert scan_one("Claude Code", fp).is_subagent is True


# ── 目录扫描与并行 ────────────────────────────────────────────────────

def test_newest_files_respects_limit_and_order(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()
    made = []
    for i in range(5):
        fp = root / f"s{i}.jsonl"
        fp.write_text("{}\n", encoding="utf-8")
        os.utime(fp, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
        made.append(fp)
    got = _newest_files(root, 3)
    assert got == [made[4], made[3], made[2]]


def test_newest_files_recurses_and_ignores_other_suffixes(tmp_path: Path):
    root = tmp_path / "s"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "deep.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "a" / "note.txt").write_text("x\n", encoding="utf-8")
    got = _newest_files(root, 10)
    assert [p.name for p in got] == ["deep.jsonl"]


def test_scan_session_vitals_sorts_by_risk(tmp_path: Path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    small = root / "small.jsonl"
    _write_jsonl(small, [{"type": "system", "sessionId": "small", "cwd": "/p"}])
    big = root / "big.jsonl"
    _write_jsonl(big, [{"type": "system", "sessionId": "big", "cwd": "/p"}])
    _pad(big, 8_500_000)

    monkeypatch.setattr(
        "agent_handoff.core.vitals.agent_session_roots", lambda: [("Claude Code", root)]
    )
    clear_cache()
    rows = scan_session_vitals(limit=10)
    assert [r.band for r in rows] == ["critical", "ok"]
    assert rows[0].session_id == "big"


def test_scan_session_vitals_parallel_equals_serial(tmp_path: Path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    for i in range(6):
        _write_jsonl(root / f"s{i}.jsonl", [
            {"type": "system", "sessionId": f"id{i}", "cwd": "/p"},
            {"type": "user", "message": {"content": f"q{i}"}},
            {"type": "x", "note": "content-blocked"} if i % 2 else {"type": "ok"},
        ])
    monkeypatch.setattr(
        "agent_handoff.core.vitals.agent_session_roots", lambda: [("Claude Code", root)]
    )
    clear_cache()
    serial = [r.to_dict() for r in scan_session_vitals(limit=10, jobs=1)]
    clear_cache()
    par = [r.to_dict() for r in scan_session_vitals(limit=10, jobs=6)]
    assert serial == par


def test_scan_session_vitals_no_roots(monkeypatch):
    monkeypatch.setattr("agent_handoff.core.vitals.agent_session_roots", lambda: [])
    assert scan_session_vitals() == []


def test_cache_reuses_unchanged_file(tmp_path: Path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    fp = root / "s.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "x", "cwd": "/p"}])
    monkeypatch.setattr(
        "agent_handoff.core.vitals.agent_session_roots", lambda: [("Claude Code", root)]
    )
    clear_cache()
    first = scan_session_vitals(limit=5)[0]
    second = scan_session_vitals(limit=5)[0]
    assert first is second, "未变动的转录应复用缓存结果"


def test_cache_is_bounded(tmp_path: Path, monkeypatch):
    """缓存必须有上限，否则网页界面长驻时会无限增长。

    键里含 mtime，而转录是持续追加的：每次 `/api/vitals` 都会因 mtime 变化
    生成新键，旧条目永不失效。每个 SessionRow 现在还带着完整压缩摘要
    （实测单份可达 96 KB），不逐出就是几百 MB 的泄漏。
    """
    from agent_handoff.core.vitals import _CACHE_MAX, _cache, _cached_scan

    clear_cache()
    # 造出比上限更多的不同转录，逐个扫描。
    for i in range(_CACHE_MAX + 20):
        fp = tmp_path / f"s{i}.jsonl"
        _write_jsonl(fp, [{"type": "system", "sessionId": f"x{i}", "cwd": "/p"}])
        assert _cached_scan("Claude Code", fp, True) is not None
    assert len(_cache) <= _CACHE_MAX, f"缓存无上限：{len(_cache)} 条"
    clear_cache()


def test_to_dict_is_json_serializable(tmp_path: Path):
    fp = tmp_path / "c.jsonl"
    _write_jsonl(fp, [{"type": "system", "sessionId": "s", "cwd": "/p"}])
    d = scan_one("Claude Code", fp).to_dict()
    json.dumps(d)  # 不抛异常即通过
    assert isinstance(d["path"], str)
    assert isinstance(d["mtime"], str)
