#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划文档解析与回填。重点覆盖原版的两个静默 BUG：
  1. 缩进过的 `**Constraints:**` 无法结束 Files 段（section 重置用了 raw 而非 strip 后的行）
  2. CRLF 文件回填后整份文件的每一行都变了（读写换行不对称）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_handoff.core.plan import (
    CHECKBOX,
    find_intent_sections,
    find_plan,
    parse_plan,
    update_plan,
)


def test_parse_tasks_files_symbols_steps(repo: Path):
    text = (repo / "docs" / "plan.md").read_text(encoding="utf-8")
    tasks, protected = parse_plan(text)
    assert [t.num for t in tasks] == [1, 2]
    assert tasks[0].files == ["pkg/core.py", "pkg/util.py"]
    assert set(tasks[0].symbols) == {"build_thing", "ThingBuilder"}
    assert [s.number for s in tasks[0].steps] == [1, 2]
    assert all(not s.done for s in tasks[0].steps)
    assert tasks[1].files == ["pkg/ui.py"]
    assert tasks[1].symbols == ["render_ui"]


def test_protected_paths_both_languages(repo: Path):
    text = (repo / "docs" / "plan.md").read_text(encoding="utf-8")
    _, protected = parse_plan(text)
    assert "docs/LOGO.jpg" in protected
    assert "secret.key" in protected


def test_indented_bold_section_ends_files_block():
    """原版用 raw.startswith('**') 判断小节结束，缩进过的粗体行判不出来，
    于是后面的 `- Create:` 会被继续算进 Files 段。"""
    text = """\
### Task 1: x

**Files:**
- Create: `a.py`

  **Constraints:**
- Create: `SHOULD_NOT_BE_A_FILE.py`

**Steps:**
- [ ] **Step 1** do
"""
    tasks, _ = parse_plan(text)
    assert tasks[0].files == ["a.py"], tasks[0].files


def test_intent_sections_detected(repo: Path):
    text = (repo / "docs" / "plan.md").read_text(encoding="utf-8")
    got = find_intent_sections(text)
    assert "Goal" in got
    assert "Global Constraints" in got


@pytest.mark.parametrize(
    "text, want",
    [
        ("# P\n\n## Goal\n\nx\n", "Goal"),
        ("# P\n\n### Goal\n\nx\n", "Goal"),
        ("# P\n\n**Goal:** x\n", "Goal"),
        ("# P\n\n## 目标\n\nx\n", "目标"),
        ("# P\n\n## 目標\n\nx\n", "目標"),
        ("# P\n\n## Non-Goals\n\nx\n", "Non-Goals"),
        ("# P\n\n## Scope\n\nx\n", "Scope"),
        ("# P\n\n## 红线\n\nx\n", "红线"),
    ],
)
def test_goal_is_found_as_a_heading_too(text: str, want: str):
    """`## Goal` 与 `**Goal:**` 必须都认得出。

    原版把词表拆在粗体分支与标题分支里各写一份，`Goal` 只出现在粗体那份，
    于是 `## Goal`——最常见的写法，本工具自己的计划文档用的就是它——认不出来。
    提示词因此不点名目标段落，新会话把计划当待办清单读，漏掉整体目标与红线，
    而那恰恰是交接最不能丢的东西。
    """
    assert want in find_intent_sections(text)


@pytest.mark.parametrize(
    "text",
    [
        "# P\n\n## Goalkeeper\n\nx\n",          # 词只是前缀，不是整个标题
        "# P\n\n本节 Goal 是内联词。\n",          # 正文里提到，不是标题
        "# P\n\n## Goal extra words\n\nx\n",    # 标题后还有别的词
    ],
)
def test_intent_detection_does_not_over_match(text: str):
    """宁可漏认一个奇怪写法，也不要把正文里的词当成段落标题。

    误认的代价是提示词点名一个不存在的段落，新会话去找找不到——
    那比不点名更糟，它会让人怀疑整份交接的可信度。
    """
    assert find_intent_sections(text) == []


def test_find_plan_prefers_checkbox_document(repo: Path):
    found = find_plan(repo, None)
    assert found is not None
    assert found.name == "plan.md"


def test_find_plan_skips_readme(repo: Path):
    """README 没有任务标题也没有复选框，不该被当成计划文档。"""
    (repo / "README.md").write_text("# demo\n\n" + "filler\n" * 400, encoding="utf-8")
    found = find_plan(repo, None)
    assert found is not None and found.name == "plan.md"


def test_find_plan_skips_node_modules(repo: Path):
    nm = repo / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    decoy = "### Task 9: fake\n\n**Steps:**\n" + "- [ ] **Step 1** x\n" * 5 + "z\n" * 500
    (nm / "PLAN.md").write_text(decoy, encoding="utf-8")
    found = find_plan(repo, None)
    assert found is not None and "node_modules" not in found.as_posix()


def test_find_plan_explicit_absolute_and_relative(repo: Path):
    assert find_plan(repo, "docs/plan.md") == repo / "docs" / "plan.md"
    assert find_plan(repo, str(repo / "docs" / "plan.md")) == repo / "docs" / "plan.md"
    assert find_plan(repo, "docs/nope.md") is None


# --- 说明文档不是计划文档 ---------------------------------------------------
# 这一组守的是一个实测事故：本仓库三份 README 都带 `### Task 1: …` 加四个
# `- [ ] **Step N**` 的**格式示例**，于是它们成了 find_plan 的唯一候选，最新
# 那份胜出，`out_dir = plan_path.parent` 随之从 `docs/` 变成仓库根。磁盘证据
# 吻合：8-24 那两份交接产物落在仓库根，8-22 / 8-23 的在 docs/ 下，中间只发生了
# 「README 被改过」这一件事。


def test_readme_with_a_plan_shaped_example_is_not_a_plan(repo: Path):
    """README 里演示计划格式，且比真计划更新——仍然不能胜出。

    这是原始事故的最小复现：不加名字过滤时，mtime 更新的 README 会赢。
    """
    import os
    import time

    demo = (
        "# 工具说明\n\n计划文档长这样：\n\n"
        "### Task 1: 建立数据层\n\n**Steps:**\n"
        "- [ ] **Step 1** 定义表结构\n- [ ] **Step 2** 写迁移脚本\n"
        "- [ ] **Step 3** 补索引\n" + "说明文字\n" * 200
    )
    for name in ("README.md", "README.en.md", "README.zh-Hant.md"):
        (repo / name).write_text(demo, encoding="utf-8")
        now = time.time() + 600  # 比 docs/plan.md 新
        os.utime(repo / name, (now, now))
    found = find_plan(repo, None)
    assert found is not None and found.name == "plan.md", found


@pytest.mark.parametrize(
    "name",
    ["README.md", "README.zh-Hant.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE.md"],
)
def test_documentation_names_never_win(repo: Path, name: str):
    """说明性文档按**名字**排除：它们的性质是说明，内容再像也不是计划。

    语言变体（README.zh-Hant.md）走同一条：比对第一个点之前的主干。
    """
    import os
    import time

    body = "### Task 1: x\n\n**Steps:**\n" + "- [ ] **Step 1** a\n- [ ] **Step 2** b\n- [ ] **Step 3** c\n"
    (repo / name).write_text(body + "填充\n" * 200, encoding="utf-8")
    now = time.time() + 600
    os.utime(repo / name, (now, now))
    found = find_plan(repo, None)
    assert found is not None and found.name == "plan.md", f"{name} 胜出了"


def test_checkboxes_inside_a_fence_do_not_count(repo: Path):
    """围栏代码块里的任务与复选框是**展示**，不是声明。

    围栏是作者自己标出的「这是示例」，比再叠一条启发式可靠。文件名不在
    否决表里（用 GUIDE.md），所以这一条单独验证围栏逻辑本身。
    """
    import os
    import time

    fenced = (
        "# 指南\n\n照这个格式写：\n\n```markdown\n"
        "### Task 1: 示例\n- [ ] **Step 1** a\n- [ ] **Step 2** b\n- [ ] **Step 3** c\n"
        "```\n" + "正文\n" * 200
    )
    (repo / "GUIDE.md").write_text(fenced, encoding="utf-8")
    now = time.time() + 600
    os.utime(repo / "GUIDE.md", (now, now))
    found = find_plan(repo, None)
    assert found is not None and found.name == "plan.md", found


def test_a_plan_without_an_intent_section_still_wins(repo: Path):
    """只有任务与步骤、没写目标段的计划文档是合法的，不能被挡掉。

    刻意不把「必须有意图段落」当作判据：`parse_plan` 对缺失的意图段本来就有
    兜底，加上去会把真计划挡在外面。挡示例要用围栏这种明确证据。
    """
    import os
    import time

    bare = repo / "docs" / "bare-plan.md"
    bare.write_text(
        "### Task 1: 只有任务\n\n**Files:**\n- Create: `x.py`\n\n**Steps:**\n"
        "- [ ] **Step 1** a\n- [ ] **Step 2** b\n- [ ] **Step 3** c\n" + "尾部\n" * 100,
        encoding="utf-8",
    )
    now = time.time() + 900
    os.utime(bare, (now, now))
    assert find_plan(repo, None) == bare


def test_no_candidate_returns_none_so_output_lands_in_docs(repo: Path):
    """一个候选都没有时返回 None——调用方据此把产物写进 `repo/docs`。

    这一条固定的是**回落行为**本身：`handoff.py` 的
    `out_dir = plan_path.parent if plan_path else (repo / "docs")` 依赖它。
    """
    (repo / "docs" / "plan.md").unlink()
    assert find_plan(repo, None) is None


def test_fence_stripping_preserves_line_numbers():
    """剥围栏时行号必须不变——回填靠行号定位复选框，错一行就打到别处。"""
    from agent_handoff.core.plan import _outside_fences

    src = "a\n```\n- [ ] inside\n```\n- [ ] outside\n"
    out = _outside_fences(src)
    assert out.count("\n") == src.count("\n")
    assert "inside" not in out
    assert "outside" in out


def test_unclosed_fence_swallows_the_rest():
    """未闭合的围栏之后也算代码块——markdown 渲染就是这么做的。"""
    from agent_handoff.core.plan import _outside_fences

    out = _outside_fences("a\n```\n- [ ] tail\n")
    assert "tail" not in out
    assert out.count("\n") == 3


def test_update_plan_ticks_only_complete_tasks(repo: Path):
    plan = repo / "docs" / "plan.md"
    tasks, _ = parse_plan(plan.read_text(encoding="utf-8"))
    report = {1: {"complete": True}, 2: {"complete": False}}
    added, total = update_plan(plan, tasks, report, dry=False)
    assert (added, total) == (2, 4)
    body = plan.read_text(encoding="utf-8")
    assert body.count("- [x]") == 2
    assert body.count("- [ ]") == 2


def test_update_plan_dry_run_writes_nothing(repo: Path):
    plan = repo / "docs" / "plan.md"
    before = plan.read_bytes()
    tasks, _ = parse_plan(plan.read_text(encoding="utf-8"))
    added, _ = update_plan(plan, tasks, {1: {"complete": True}}, dry=True)
    assert added == 2
    assert plan.read_bytes() == before


def test_update_plan_preserves_crlf(tmp_path: Path):
    """原版读时通用换行把 CRLF 折成 LF，写回时写 LF —— 于是勾一个复选框
    会把整份文件的每一行都改掉，git diff 变成几百行噪声。"""
    plan = tmp_path / "p.md"
    src = "### Task 1: x\r\n\r\n**Files:**\r\n- Create: `a.py`\r\n\r\n**Steps:**\r\n- [ ] **Step 1** do\r\n- [ ] **Step 2** more\r\n"
    plan.write_bytes(src.encode("utf-8"))
    tasks, _ = parse_plan(plan.read_text(encoding="utf-8"))
    update_plan(plan, tasks, {1: {"complete": True}}, dry=False)
    data = plan.read_bytes()
    assert b"- [x] **Step 1**" in data
    assert data.count(b"\r\n") == src.count("\r\n")
    assert b"\r\r" not in data
    # 唯一的字节差异就是两个复选框
    assert data == src.replace("- [ ]", "- [x]").encode("utf-8")


def test_update_plan_preserves_lf(tmp_path: Path):
    plan = tmp_path / "p.md"
    src = "### Task 1: x\n\n**Steps:**\n- [ ] **Step 1** do\n"
    plan.write_bytes(src.encode("utf-8"))
    tasks, _ = parse_plan(src)
    update_plan(plan, tasks, {1: {"complete": True}}, dry=False)
    assert plan.read_bytes() == src.replace("- [ ]", "- [x]").encode("utf-8")
    assert b"\r" not in plan.read_bytes()


def test_update_plan_survives_stale_line_index(tmp_path: Path):
    """计划文档在解析之后被截短了：不该写错行，也不该抛 IndexError。"""
    plan = tmp_path / "p.md"
    src = "### Task 1: x\n\n**Steps:**\n- [ ] **Step 1** do\n- [ ] **Step 2** more\n"
    plan.write_text(src, encoding="utf-8")
    tasks, _ = parse_plan(src)
    plan.write_text("### Task 1: x\n", encoding="utf-8")  # 外部截短
    added, _ = update_plan(plan, tasks, {1: {"complete": True}}, dry=False)
    assert added == 0
    assert plan.read_text(encoding="utf-8") == "### Task 1: x\n"


def test_traditional_chinese_task_header():
    text = "### 任務 3: x\n\n**Steps:**\n- [ ] **步驟 1** do\n"
    tasks, _ = parse_plan(text)
    assert tasks and tasks[0].num == 3
    assert tasks[0].steps and tasks[0].steps[0].number == 1


def test_full_width_colon_in_file_line():
    text = "### Task 1: x\n\n**Files:**\n- 新建：`a.py`\n"
    tasks, _ = parse_plan(text)
    assert tasks[0].files == ["a.py"]


# --- 真实 markdown 写法 -----------------------------------------------------
# 下面每一条都对应一种在真实计划文档里常见、而原版解析器整条丢弃的写法。
# 丢弃的后果不是报错，是静默判成「该任务没有文件/没有符号/没有步骤」，
# 于是完成度失真，接续会话按错误的地图行动。


@pytest.mark.parametrize(
    ("label", "line", "expected"),
    [
        ("动词加冒号", "- Create: `a/b.ts`", ["a/b.ts"]),
        ("粗体动词", "- **Modify**: `a/b.ts`", ["a/b.ts"]),
        ("无冒号", "- Modify `a/b.ts`", ["a/b.ts"]),
        ("无动词", "- `a/b.ts` — 加 undo", ["a/b.ts"]),
        ("Delete 动词", "- Delete: `x.ts`", ["x.ts"]),
        ("一行多路径", "- Create: `a.ts`, `b.ts`", ["a.ts", "b.ts"]),
        ("星号列表", "* Create: `c.ts`", ["c.ts"]),
        ("加号列表", "+ Create: `d.ts`", ["d.ts"]),
    ],
)
def test_file_line_forms(label: str, line: str, expected: list[str]):
    tasks, _ = parse_plan(f"### Task 1: x\n\n**Files:**\n{line}\n")
    assert tasks[0].files == expected, label


def test_leading_slash_path_stays_inside_repo():
    """`- Modify: /webui/src/x.ts` 不能逃出仓库。

    `PureWindowsPath("E:/repo") / "/webui/x.ts"` 会跳到 `E:\\webui\\x.ts`——
    盘符根，仓库之外。文件判定于是跑到错误位置，永远判缺失。
    """
    tasks, _ = parse_plan("### Task 1: x\n\n**Files:**\n- Modify: /webui/src/x.ts\n")
    assert tasks[0].files == ["webui/src/x.ts"]


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("中文散文", "- 这一步不涉及任何文件"),
        ("英文散文", "- Note: be careful here"),
        ("无路径的说明", "- Produces something"),
    ],
)
def test_file_section_ignores_prose(label: str, line: str):
    """放宽文件行匹配之后，散文不能被当成路径——否则 file_ratio 的分母被污染。"""
    tasks, _ = parse_plan(f"### Task 1: x\n\n**Files:**\n{line}\n")
    assert tasks[0].files == [], label


@pytest.mark.parametrize(
    ("label", "line", "mark"),
    [
        ("短横线", "- [ ] **Step 1** x", " "),
        ("星号", "* [ ] **Step 1** x", " "),
        ("加号", "+ [ ] **Step 1** x", " "),
        ("双空格", "-  [ ] **Step 1** x", " "),
        ("已完成", "- [x] **Step 1** x", "x"),
        ("部分完成", "- [~] **Step 1** x", "~"),
    ],
)
def test_checkbox_list_markers(label: str, line: str, mark: str):
    """CommonMark 的三种列表符都合法。只认 `-` 会让整份计划的复选框全部消失，
    连 find_plan 都会因为「复选框少于 3 个」而拒绝该文档。"""
    m = CHECKBOX.match(line)
    assert m is not None, label
    assert m.group("mark") == mark


def test_partial_mark_is_not_done():
    """`[~]` 是部分完成，按未完成算——勾选是不可逆的，宁可少勾。"""
    tasks, _ = parse_plan("### Task 1: x\n\n**Steps:**\n- [~] **Step 1** x\n")
    assert tasks[0].steps[0].done is False


def test_step_without_bold():
    """`- [ ] Step 1: x` 没有粗体，原版不生成 Step，进度显示成 0 / 0。"""
    tasks, _ = parse_plan("### Task 1: x\n\n**Steps:**\n- [ ] Step 1: plain\n")
    assert [s.number for s in tasks[0].steps] == [1]


@pytest.mark.parametrize(
    ("label", "line", "num"),
    [
        ("一级", "# Task 9: x", 9),
        ("五级", "##### Task 5: x", 5),
        ("缩进", "  ### Task 6: x", 6),
        ("Phase 组织", "## Phase 1: x", 1),
        ("中文阶段", "## 阶段 2: x", 2),
    ],
)
def test_task_heading_forms(label: str, line: str, num: int):
    tasks, _ = parse_plan(f"{line}\n\n**Steps:**\n- [ ] **Step 1** x\n")
    assert tasks and tasks[0].num == num, label


@pytest.mark.parametrize(
    ("label", "line", "expected"),
    [
        ("Produces", "- Produces `undo()`", ["undo"]),
        ("Exports", "- Exports: `undo`", ["undo"]),
        ("中文提供", "- 提供 `undo`", ["undo"]),
        ("Provides 多个", "- Provides `run()`, `add()`", ["run", "add"]),
    ],
)
def test_interface_declaration_verbs(label: str, line: str, expected: list[str]):
    """原版只认 Produces/产出，其余写法整行跳过，该任务符号证据为空。"""
    tasks, _ = parse_plan(f"### Task 1: x\n\n**Interfaces:**\n{line}\n")
    assert tasks[0].symbols == expected, label


def test_short_symbols_are_kept():
    """原版 `len(base) > 3` 丢掉 run / add / get / fn / id 这些真实接口名。"""
    tasks, _ = parse_plan("### Task 1: x\n\n**Interfaces:**\n- Produces `run()`, `add()`, `id`\n")
    assert tasks[0].symbols == ["run", "add", "id"]
