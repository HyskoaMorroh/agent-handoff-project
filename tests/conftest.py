#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享 fixture：临时 git 仓库 + 计划文档。

真跑 git 而不是 mock：这个工具的全部价值在于它对真实 git 状态的判断，
mock 掉 git 等于把被测行为换掉。临时目录里跑，永不碰用户仓库。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

PLAN = """\
# Demo Plan

**Goal:** 建一个能跑的东西。

## Global Constraints

- `docs/LOGO.jpg` 是用户私有文件，不得提交。
- `secret.key` user-owned, must not be staged.

### Task 1: 建核心模块

**Files:**
- Create: `pkg/core.py`
- Create: `pkg/util.py`

**Interfaces:**
- Produces `build_thing()`, `ThingBuilder`

**Steps:**
- [ ] **Step 1** 写 core
- [ ] **Step 2** 写 util

### Task 2: 建界面

**Files:**
- Create: `pkg/ui.py`

**Interfaces:**
- Produces `render_ui()`

**Steps:**
- [ ] **Step 1** 写 ui
- [ ] **Step 2** 接线
"""


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (p.stdout or "") + (p.stderr or "")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个干净的 git 仓库，带计划文档和 Task 1 的产物（Task 2 缺）。"""
    r = tmp_path / "proj"
    (r / "pkg").mkdir(parents=True)
    (r / "docs").mkdir()

    git(r, "init", "-q")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    git(r, "config", "commit.gpgsign", "false")

    (r / "docs" / "plan.md").write_text(PLAN, encoding="utf-8")
    (r / "pkg" / "core.py").write_text("def build_thing():\n    return 1\n", encoding="utf-8")
    (r / "pkg" / "util.py").write_text("class ThingBuilder:\n    pass\n", encoding="utf-8")
    (r / "README.md").write_text("# demo\n", encoding="utf-8")

    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def tr():
    from agent_handoff.i18n import Translator

    return Translator("en")


@pytest.fixture(autouse=True)
def _no_user_env(monkeypatch, tmp_path):
    """把 HOME 指到临时目录，确保测试永远扫不到用户真实的会话转录。"""
    fake = tmp_path / "fakehome"
    fake.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.delenv("AGENT_HANDOFF_LANG", raising=False)
    yield
