#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多根工作区的发现，以及它如何让 `cwd` 降权。

这些测试盯着一件事：**`cwd` 什么时候可信、什么时候不可信**。

背景（从 Claude Code 的 VSCode 扩展代码读出，官方文档未记载）：多根工作区下
扩展固定取 `workspaceFolders[0]` 作 cwd，其余根转成 `--add-dir`。切换活动编辑器
不改它，也没有任何配置项能覆盖。所以「在 A 目录启动、整场改 B 仓库」时转录里的
`cwd` 一直指着 A。
"""
from __future__ import annotations

import json

from agent_handoff.core.attribution import (
    ATTRIBUTION_LINE_BUDGET,
    AttributionCollector,
)
from agent_handoff.core.workspace import (
    WorkspaceMap,
    _parse_workspace_file,
    ide_workspace_groups,
)


def _repo(base, name):
    """建一个空 git 仓库（只要 `.git` 存在，`nearest_repo` 就认）。"""
    d = base / name
    (d / ".git").mkdir(parents=True)
    return d


def _claude_cwd(cwd: str) -> str:
    return json.dumps({"type": "user", "cwd": cwd, "sessionId": "s1"})


def _claude_edit(path: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": path}}]},
    })


# ── WorkspaceMap 的构造与查询 ─────────────────────────────────


def test_single_root_window_is_not_a_workspace(tmp_path):
    """单根窗口不进索引。那种 cwd 是可信的，而这个类只标不可信的。"""
    a = _repo(tmp_path, "aa")
    wm = WorkspaceMap([[str(a)]])
    assert not wm
    assert wm.is_multi_root(str(a)) is False
    assert wm.is_workspace_cwd(str(a)) is False
    assert wm.groups() == []


def test_multi_root_marks_only_the_first_as_cwd(tmp_path):
    """只有排第一的根会成为那个窗口的 cwd。

    这是整个模块最关键的一条区分：扩展取 `folders[0]`，所以靠后的根**不可能**
    是那个窗口的 cwd。当 cwd 恰好等于某个靠后的根时，它一定来自别的（单根）
    窗口，此时可信。无条件降权会把那种正确的 cwd 也误标。
    """
    a = _repo(tmp_path, "aa")
    b = _repo(tmp_path, "bb")
    wm = WorkspaceMap([[str(a), str(b)]])

    assert bool(wm) is True
    # 两个都在同一个工作区里
    assert wm.is_multi_root(str(a)) is True
    assert wm.is_multi_root(str(b)) is True
    # 但只有第一个会以 cwd 身份出现
    assert wm.is_workspace_cwd(str(a)) is True
    assert wm.is_workspace_cwd(str(b)) is False


def test_siblings_excludes_itself(tmp_path):
    """兄弟清单不含自己——那是给用户看的「你可能实际在改这些」。"""
    a = _repo(tmp_path, "aa")
    b = _repo(tmp_path, "bb")
    c = _repo(tmp_path, "cc")
    wm = WorkspaceMap([[str(a), str(b), str(c)]])

    sibs = wm.siblings(str(a))
    assert len(sibs) == 2
    assert all("aa" not in s for s in sibs)
    # 排序稳定，两次运行输出一致。
    assert sibs == sorted(sibs)


def test_roots_outside_any_repo_are_dropped(tmp_path):
    """不在任何 git 仓库里的根丢掉：它不可能是「在改哪个仓库」的答案。"""
    a = _repo(tmp_path, "aa")
    plain = tmp_path / "notarepo"
    plain.mkdir()
    wm = WorkspaceMap([[str(a), str(plain)]])

    # 只剩一个仓库 → 退化成单根，不进索引。
    assert not wm
    assert wm.is_workspace_cwd(str(a)) is False


def test_root_inside_a_repo_is_lifted_to_the_repo(tmp_path):
    """`folders` 指向仓库子目录是常见写法（monorepo 的 packages/web）。

    问题是仓库级的，所以根要先提升到仓库根再比较。
    """
    a = _repo(tmp_path, "aa")
    sub = a / "packages" / "web"
    sub.mkdir(parents=True)
    b = _repo(tmp_path, "bb")
    wm = WorkspaceMap([[str(sub), str(b)]])

    # 子目录被提升成了仓库 aa，所以它是首根。
    assert wm.is_workspace_cwd(str(a)) is True
    assert wm.siblings(str(a)) == [x for x in wm.siblings(str(a)) if "bb" in x]


def test_duplicate_groups_collapse(tmp_path):
    """同一个工作区可能同时被锁文件与 `.code-workspace` 发现。不该算两组。"""
    a = _repo(tmp_path, "aa")
    b = _repo(tmp_path, "bb")
    wm = WorkspaceMap([[str(a), str(b)], [str(a), str(b)]])
    assert len(wm.groups()) == 1


def test_case_only_difference_is_one_root(tmp_path):
    """盘符大小写不同不是两个根。锁文件写小写、工作区文件写大写是实测现象。"""
    a = _repo(tmp_path, "aa")
    b = _repo(tmp_path, "bb")
    wm = WorkspaceMap([[str(a).upper(), str(b)], [str(a), str(b)]])
    assert len(wm.groups()) == 1


# ── `.code-workspace` 解析 ─────────────────────────────────────


def test_workspace_file_relative_paths_resolve(tmp_path):
    """`folders[].path` 可以是相对路径，相对于工作区文件所在目录。

    实测本机那份用的正是 `../agent-handoff-project`——不解析就完全对不上。
    """
    ws_dir = tmp_path / "cfg"
    ws_dir.mkdir()
    a = _repo(tmp_path, "aa")
    fp = ws_dir / "two.code-workspace"
    fp.write_text(json.dumps({"folders": [{"path": "../aa"}, {"path": str(a)}]}), encoding="utf-8")

    roots = _parse_workspace_file(fp)
    assert len(roots) == 2
    assert all(r.endswith("aa") for r in roots)


def test_workspace_file_tolerates_comments_and_trailing_commas(tmp_path):
    """`.code-workspace` 是 JSONC。标准库没有 JSONC 解析器，所以做最小清理。"""
    a = _repo(tmp_path, "aa")
    b = _repo(tmp_path, "bb")
    fp = tmp_path / "x.code-workspace"
    fp.write_text(
        "{\n"
        "  // 主仓在前，这样 Claude 的 cwd 落在它上面\n"
        '  "folders": [\n'
        f'    {{ "path": {json.dumps(str(a))} }},\n'
        f'    {{ "path": {json.dumps(str(b))} }},\n'   # 尾逗号
        "  ],\n"
        '  "settings": {}\n'
        "}\n",
        encoding="utf-8",
    )
    roots = _parse_workspace_file(fp)
    assert len(roots) == 2


def test_broken_workspace_file_is_skipped(tmp_path):
    """读不到一个工作区定义只是少一条线索，不该让扫描失败。"""
    fp = tmp_path / "bad.code-workspace"
    fp.write_text('{"folders": [', encoding="utf-8")
    assert _parse_workspace_file(fp) == []

    fp2 = tmp_path / "notdict.code-workspace"
    fp2.write_text("[]", encoding="utf-8")
    assert _parse_workspace_file(fp2) == []


def test_workspace_file_without_folders_yields_nothing(tmp_path):
    fp = tmp_path / "empty.code-workspace"
    fp.write_text('{"settings": {"editor.fontSize": 14}}', encoding="utf-8")
    assert _parse_workspace_file(fp) == []


# ── 锁文件读取 ─────────────────────────────────────────────────


def test_ide_lock_groups_read_workspace_folders(tmp_path, monkeypatch):
    """扩展给每个 VSCode 窗口写一份锁文件，内含该窗口的全部根。

    这是**上游自己写下的事实**，不是推断。进程结束后锁文件常常留着，那反而
    有用：它记录了一个曾存在的分组，而历史会话正是在那种分组下跑的。
    """
    home = tmp_path / "home"
    (home / ".claude" / "ide").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))

    lock = home / ".claude" / "ide" / "1234.lock"
    lock.write_text(json.dumps({
        "pid": 999,
        "workspaceFolders": ["E:\\\\proj\\\\one", "C:\\\\proj\\\\two"],
        "ideName": "Visual Studio Code",
    }), encoding="utf-8")

    groups = ide_workspace_groups()
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_ide_lock_bad_json_is_skipped(tmp_path, monkeypatch):
    """锁文件可能正在被写、也可能换了格式。跳过，不崩。"""
    home = tmp_path / "home"
    (home / ".claude" / "ide").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    (home / ".claude" / "ide" / "bad.lock").write_text("{oops", encoding="utf-8")
    (home / ".claude" / "ide" / "nolist.lock").write_text(
        json.dumps({"pid": 1, "workspaceFolders": "notalist"}), encoding="utf-8")
    assert ide_workspace_groups() == []


def test_missing_lock_dir_is_not_an_error(tmp_path, monkeypatch):
    """没装 VSCode 扩展时锁目录不存在。少一个来源，不影响别的判定。"""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    assert ide_workspace_groups() == []


# ── 归属判定如何受工作区影响 ───────────────────────────────────


def test_workspace_cwd_ranks_below_plain_cwd(tmp_path):
    """工作区首根的 cwd 降到 `workspace_cwd`，排在普通 cwd 之后。

    两个仓库都只有 cwd 证据时，来自单根窗口的那个应该胜出——它携带真实信息，
    而工作区首根只说明「排第一」。
    """
    ws_lead = _repo(tmp_path, "lead")
    other = _repo(tmp_path, "other")
    plain = _repo(tmp_path, "plain")
    wm = WorkspaceMap([[str(ws_lead), str(other)]])

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    # 工作区首根出现很多次，单根窗口的目录只出现一次。
    for _ in range(50):
        at.feed(_claude_cwd(str(ws_lead)))
    at.feed(_claude_cwd(str(plain)))
    v = at.verdict(workspaces=wm)

    # 命中数远少，但等级更高 → 胜出。等级永远先于次数。
    assert v.primary == str(plain)
    assert v.basis == "cwd"
    levels = [e.level for e in v.evidence]
    assert levels.index("cwd") < levels.index("workspace_cwd")


def test_non_lead_root_cwd_is_still_trusted(tmp_path):
    """cwd 等于工作区里靠后的根时不降权——那种 cwd 来自单根窗口。

    扩展只会把 `folders[0]` 设成 cwd，所以靠后的根出现在 cwd 位置意味着
    这个会话根本不是从那个多根窗口开的。
    """
    lead = _repo(tmp_path, "lead")
    second = _repo(tmp_path, "second")
    wm = WorkspaceMap([[str(lead), str(second)]])

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_cwd(str(second)))
    v = at.verdict(workspaces=wm)

    assert v.basis == "cwd"          # 没被降级
    assert v.primary == str(second)


def test_edit_evidence_still_wins_over_workspace_cwd(tmp_path):
    """行为证据永远压过任何 cwd。这是整套分层的基石，工作区不改变它。"""
    lead = _repo(tmp_path, "lead")
    work = _repo(tmp_path, "work")
    wm = WorkspaceMap([[str(lead), str(work)]])

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    for _ in range(200):
        at.feed(_claude_cwd(str(lead)))
    for _ in range(3):
        at.feed(_claude_edit(str(work / "a.py")))
    v = at.verdict(workspaces=wm)

    assert v.primary == str(work)
    assert v.confidence == "certain"
    assert v.basis == "edit"
    assert v.cwd_in_workspace is True


def test_siblings_surface_when_only_cwd_evidence(tmp_path):
    """没有行为证据时把同工作区的其他根摆出来。

    纯讨论、纯搜索、早期被打断的会话没有文件证据，那时这份清单是唯一能帮用户
    认出「哦是那个项目」的线索。
    """
    lead = _repo(tmp_path, "lead")
    other = _repo(tmp_path, "other")
    wm = WorkspaceMap([[str(lead), str(other)]])

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_cwd(str(lead)))
    v = at.verdict(workspaces=wm)

    assert v.cwd_in_workspace is True
    assert len(v.workspace_siblings) == 1
    assert "other" in v.workspace_siblings[0]


def test_siblings_omit_repos_already_in_evidence(tmp_path):
    """已经在证据里的仓库不再重复列——证据条目信息更全（带等级与命中数）。"""
    lead = _repo(tmp_path, "lead")
    work = _repo(tmp_path, "work")
    wm = WorkspaceMap([[str(lead), str(work)]])

    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_cwd(str(lead)))
    for _ in range(3):
        at.feed(_claude_edit(str(work / "a.py")))
    v = at.verdict(workspaces=wm)

    assert v.workspace_siblings == []


def test_no_workspace_map_keeps_old_behaviour(tmp_path):
    """不传工作区映射时行为与改动前完全一致：cwd 按原有权重处理。"""
    a = _repo(tmp_path, "aa")
    at = AttributionCollector("Claude Code", ATTRIBUTION_LINE_BUDGET)
    at.feed(_claude_cwd(str(a)))
    v = at.verdict()

    assert v.basis == "cwd"
    assert v.cwd_in_workspace is False
    assert v.workspace_siblings == []
