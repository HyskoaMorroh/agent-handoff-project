#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划文档解析与回填。重点覆盖原版的两个静默 BUG：
  1. 缩进过的 `**Constraints:**` 无法结束 Files 段（section 重置用了 raw 而非 strip 后的行）
  2. CRLF 文件回填后整份文件的每一行都变了（读写换行不对称）
"""
from __future__ import annotations

from pathlib import Path

from agent_handoff.core.plan import (
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
