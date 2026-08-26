#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归属判定：证据分层、结论强弱、以及「不下结论」这条路。

这些测试全部盯着一件事：**结论不能比证据更确定**。判错方向比判不出来严重
得多——判不出来时界面会说「依据是启动目录」，用户自己会看；而给一个看起来
确定的错答案，用户会照着它开新会话，然后在错的仓库里改代码。
"""
from __future__ import annotations

import json

from agent_handoff.core.attribution import (
    ATTRIBUTION_LINE_BUDGET,
    AttributionCollector,
    RepoVerdict,
)


def _claude_tool(name: str, path: str) -> str:
    """一行 Claude Code 转录，内含一次工具调用。"""
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": {"file_path": path}}]},
    })


def _codex_exec(workdir: str) -> str:
    """一行 Codex rollout，内含一次 exec_command。`arguments` 是 JSON 字符串。"""
    return json.dumps({
        "type": "response_item",
        "payload": {"name": "exec_command", "arguments": json.dumps({"cmd": "ls", "workdir": workdir})},
    })


def _codex_turn(cwd: str, roots: list[str] | None = None) -> str:
    p: dict = {"cwd": cwd}
    if roots is not None:
        p["workspace_roots"] = roots
    return json.dumps({"type": "turn_context", "payload": p})


def test_no_evidence_gives_empty_verdict():
    """一条证据都没有时不编结论。空 primary 让 `work_repo` 退回旧口径。"""
    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    v = at.verdict()
    assert v.primary == ""
    assert v.confidence == "none"
    assert v.evidence == []


def test_edit_beats_cwd(tmp_path):
    """写过文件的仓库胜过启动目录。这是整套分层要解决的核心分歧。"""
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    launch = tmp_path / "launch"
    (launch / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    for _ in range(4):
        at.feed(_claude_tool("Edit", str(work / "a.py")))
    v = at.verdict(cwd=str(launch))

    assert v.primary == str(work)
    assert v.confidence == "certain"
    assert v.basis == "edit"
    # 启动目录不一致时必须标出来：resume 仍然得在那里执行。
    assert v.conflict is True
    # 两个仓库都要留在证据里，不能因为一个胜出就把另一个删掉。
    assert {e.level for e in v.evidence} == {"edit", "cwd"}


def test_cwd_alone_is_weak(tmp_path):
    """只有启动目录时结论成立但标为说不准——它回答的不是「在改什么」。"""
    launch = tmp_path / "launch"
    (launch / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    v = at.verdict(cwd=str(launch))

    assert v.primary == str(launch)
    assert v.confidence == "weak"
    assert v.basis == "cwd"
    assert v.conflict is False


def test_two_read_hits_do_not_beat_cwd(tmp_path):
    """顺手读两个文件不算在那里工作。

    实测来源：一个整场在讨论 CLIProxyAPI 部署的会话，只因为读了 2 个
    agent-handoff-project 的文件，就被判成在改那个仓库。读别的仓库是常态
    ——对比参考实现、查文档、看依赖源码。
    """
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    launch = tmp_path / "launch"
    (launch / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_tool("Read", str(other / "x.py")))
    at.feed(_claude_tool("Read", str(other / "y.py")))
    v = at.verdict(cwd=str(launch))

    # 结论退回启动目录，而不是那个被读了两次的仓库。
    assert v.primary == str(launch)
    assert v.basis == "cwd"
    # 但读取证据仍然列出来——用户能看到读过什么，只是不拿它下结论。
    assert any(e.level == "read" and e.repo.endswith("other") for e in v.evidence)


def test_many_reads_do_win(tmp_path):
    """读了足够多次就够资格当结论：那不再像顺手参考。"""
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    launch = tmp_path / "launch"
    (launch / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    for i in range(9):
        at.feed(_claude_tool("Read", str(work / f"f{i}.py")))
    v = at.verdict(cwd=str(launch))

    assert v.primary == str(work)
    assert v.basis == "read"
    # 读取不是行为证据，所以最高只能到「说不准」。
    assert v.confidence == "weak"


def test_close_race_is_not_certain(tmp_path):
    """两个仓库都改了不少时不假装知道答案。跨仓库工作是真实场景。"""
    a = tmp_path / "aa"
    (a / ".git").mkdir(parents=True)
    b = tmp_path / "bb"
    (b / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    for _ in range(5):
        at.feed(_claude_tool("Edit", str(a / "x.py")))
    for _ in range(4):
        at.feed(_claude_tool("Write", str(b / "y.py")))
    v = at.verdict()

    assert v.primary == str(a)          # 仍然取命中最多的
    assert v.confidence == "weak"       # 但明说不确定
    assert v.basis == "edit"


def test_dominant_edit_is_likely(tmp_path):
    """一个仓库压倒性领先时给「大概是」，不给「确定」——毕竟还有别的候选。"""
    a = tmp_path / "aa"
    (a / ".git").mkdir(parents=True)
    b = tmp_path / "bb"
    (b / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    for _ in range(30):
        at.feed(_claude_tool("Edit", str(a / "x.py")))
    at.feed(_claude_tool("Edit", str(b / "y.py")))
    v = at.verdict()

    assert v.primary == str(a)
    assert v.confidence == "likely"


def test_bash_and_mcp_tools_are_ignored(tmp_path):
    """不认识的工具不参与。往强证据层里掺猜出来的东西会毁掉整套分层的价值。"""
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_tool("Bash", str(repo / "a.py")))
    at.feed(_claude_tool("mcp__something__do", str(repo / "b.py")))
    v = at.verdict()

    assert v.primary == ""
    assert v.evidence == []


def test_case_only_difference_is_one_candidate(tmp_path):
    """`e:\\x` 与 `E:\\x` 是同一个仓库，不该占两个候选位。

    实测：同一目录在转录里出现两种盘符写法时，未去重会挤掉真实候选的可见配额。
    """
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    p = str(repo / "a.py")

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_tool("Edit", p))
    at.feed(_claude_tool("Edit", p.upper() if p[1:2] == ":" else p))
    v = at.verdict()

    edits = [e for e in v.evidence if e.level == "edit"]
    assert len(edits) == 1
    # 显示用第一次见到的写法，不凭空改写用户的路径大小写。
    assert edits[0].display == str(repo)


def test_budget_marks_truncated(tmp_path):
    """超预算时结论仍然给，但要标出来——「没证据」与「没看到」是两回事。"""
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", 3)
    for _ in range(3):
        at.feed(_claude_tool("Edit", str(repo / "a.py")))
    assert at.verdict().truncated is False

    at.feed(_claude_tool("Edit", str(repo / "b.py")))
    v = at.verdict()
    assert v.truncated is True
    # 越界那一行不计入命中：预算是硬边界，不是软提示。
    assert [e.hits for e in v.evidence if e.level == "edit"] == [3]


def test_broken_line_does_not_crash():
    """坏行跳过就好。一份转录里有一行坏的，不该让整轮扫描失败。"""
    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed('{"type":"assistant","message":{"content":[{"type":"tool_use"')  # 截断
    at.feed('not json at all')
    at.feed('[]')  # 合法 JSON 但不是 dict
    assert at.verdict().primary == ""


def test_codex_workspace_roots_win(tmp_path):
    """Codex 侧：harness 声明的工作区根胜过一切推断。"""
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()  # 刻意不建 .git：Codex 的 cwd 常常不在任何仓库里

    at = AttributionCollector("Codex", ATTRIBUTION_LINE_BUDGET)
    at.feed(_codex_turn(str(sandbox), roots=[str(work)]))
    v = at.verdict(cwd=str(sandbox))

    assert v.primary == str(work)
    assert v.basis == "workspace"
    assert v.confidence == "certain"


def test_codex_exec_workdir_beats_sandbox_cwd(tmp_path):
    """没有 workspace_roots 时，命令实际在哪跑仍然胜过沙箱 cwd。

    实测：316 份 rollout 里 151 份有 workdir，其中 cwd 推出的仓库与它一致的
    只有 16 份——Codex 的 cwd 指向 `~/Documents/Codex/<日期>/<名字>`，
    那里根本没有 .git。
    """
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    at = AttributionCollector("Codex", ATTRIBUTION_LINE_BUDGET)
    for _ in range(3):
        at.feed(_codex_exec(str(work)))
    v = at.verdict(cwd=str(sandbox))

    assert v.primary == str(work)
    assert v.basis == "exec"
    # 沙箱目录不在任何仓库里，所以它连候选都进不去——不是被压下去，是没有。
    assert all(e.level != "cwd" for e in v.evidence)


def test_codex_bad_arguments_string_is_skipped(tmp_path):
    """`arguments` 不是合法 JSON 时跳过这一条，不抛异常。"""
    at = AttributionCollector("Codex", ATTRIBUTION_LINE_BUDGET)
    at.feed(json.dumps({
        "type": "response_item",
        "payload": {"name": "exec_command", "arguments": "{not json"},
    }))
    assert at.verdict().primary == ""


def test_mentioned_paths_are_weakest(tmp_path):
    """正文提到过的路径排在最后，且只在没有别的信号时才当结论。"""
    mentioned = tmp_path / "m"
    (mentioned / ".git").mkdir(parents=True)
    launch = tmp_path / "launch"
    (launch / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    v = at.verdict(cwd=str(launch), mentioned=[str(mentioned)])

    # cwd 比 mention 强，所以结论是 cwd。
    assert v.primary == str(launch)
    levels = [e.level for e in v.evidence]
    assert levels.index("cwd") < levels.index("mention")


def test_verdict_to_dict_is_json_safe(tmp_path):
    """网页界面拿到的必须是能 json.dumps 的纯数据。"""
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_tool("Edit", str(repo / "a.py")))
    d = at.verdict().to_dict()

    json.dumps(d)  # 不抛异常就算过
    assert d["basis"] == "edit"
    assert d["evidence"][0]["hits"] == 1
    assert d["evidence"][0]["samples"]


def test_empty_verdict_dict_has_all_keys():
    """空结论也要有完整字段：前端按键取值，缺键会渲染成 undefined。"""
    d = RepoVerdict().to_dict()
    assert set(d) == {
        "primary", "confidence", "basis", "conflict", "truncated",
        "cwd_moved", "cwd_in_workspace", "workspace_siblings", "evidence",
    }


def _claude_cwd(cwd: str, extra: str = "") -> str:
    """一行 Claude Code 转录，信封层带 cwd。"""
    return json.dumps({"type": "user", "cwd": cwd, "sessionId": "s1", "pad": extra})


def test_cwd_is_collected_per_line_not_just_first(tmp_path):
    """cwd 是逐行写的运行时快照，多个取值都要收，按出现次数排序。

    实测：本机 66 份含 cwd 的转录里 22 份出现过多个取值，7 份是真正不同的目录。
    某会话在两个目录之间切换 19 次。只取第一个（`_Extractor` 的做法）拿到的是
    「启动那一刻」的值，多根工作区下等于 VSCode 的 `folders[0]`。
    """
    a = tmp_path / "aa"
    (a / ".git").mkdir(parents=True)
    b = tmp_path / "bb"
    (b / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    # 先在 a 里一行，然后 b 里三行。first-wins 会选 a，按次数应选 b。
    at.feed(_claude_cwd(str(a)))
    for _ in range(3):
        at.feed(_claude_cwd(str(b)))
    v = at.verdict()

    cwds = [e for e in v.evidence if e.level == "cwd"]
    assert len(cwds) == 2
    assert cwds[0].display == str(b)     # 次数多的排前面
    assert cwds[0].hits == 3
    assert v.primary == str(b)
    # 指向过两个不同仓库，要说出来。
    assert v.cwd_moved is True


def test_cwd_moved_false_for_a_single_directory(tmp_path):
    """只待过一个目录时不谎报「换过目录」。"""
    a = tmp_path / "aa"
    (a / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    for _ in range(4):
        at.feed(_claude_cwd(str(a)))
    v = at.verdict()

    assert v.cwd_moved is False
    assert v.primary == str(a)


def test_cwd_case_variants_are_one_directory(tmp_path):
    """`e:\\x` 与 `E:\\x` 是同一个目录，不该被当成「换过目录」。

    实测 22 份多取值的转录里有 15 份只差盘符大小写——那是两个写入来源
    （VSCode 的 fsPath 与 realpath 归一化）造成的，不是真的换了目录。
    """
    a = tmp_path / "aa"
    (a / ".git").mkdir(parents=True)
    p = str(a)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_cwd(p))
    at.feed(_claude_cwd(p.replace("/", "\\") if "/" in p else p.upper()))
    v = at.verdict()

    assert v.cwd_moved is False


def test_cwd_beyond_the_head_window_is_skipped(tmp_path):
    """超出行首扫描窗口的 cwd 漏掉不算错，但不能崩、也不能误报。

    单行可以长到 3.4 MB（贴了大文件那种）。对整行跑正则等于把这条路径上最贵的
    一步付满，所以只扫行首 32 KB。cwd 是逐行重复写的，漏一次不影响按次数排序。
    """
    a = tmp_path / "aa"
    (a / ".git").mkdir(parents=True)

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    # 把 cwd 推到窗口之外：前面垫足够长的内容。
    at.feed(json.dumps({"pad": "x" * 40000, "cwd": str(a)}))
    v = at.verdict()

    assert v.primary == ""      # 一条证据都没收到
    assert v.cwd_moved is False


def test_codex_turn_cwd_counts_toward_cwd_moved(tmp_path):
    """Codex 的逐轮 `turn_context.cwd` 同样参与「换过目录」判定。"""
    a = tmp_path / "aa"
    (a / ".git").mkdir(parents=True)
    b = tmp_path / "bb"
    (b / ".git").mkdir(parents=True)

    at = AttributionCollector("Codex", ATTRIBUTION_LINE_BUDGET)
    at.feed(_codex_turn(str(a)))
    at.feed(_codex_turn(str(b)))
    v = at.verdict()

    assert v.cwd_moved is True
