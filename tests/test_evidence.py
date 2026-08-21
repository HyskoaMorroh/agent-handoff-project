#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""完成度证据。重点：批量符号检索的结果必须与"每符号单独查"逐字一致，
且在 ripgrep 缺席时三条退路都给同一个答案。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_handoff.core.evidence import (
    _defined_in_text,
    _git_grep_batch,
    _python_scan,
    _rg_batch,
    resolve_symbols,
    score_tasks,
)
from agent_handoff.core.plan import parse_plan


def _tasks(repo: Path):
    return parse_plan((repo / "docs" / "plan.md").read_text(encoding="utf-8"))[0]


def test_defined_in_text_finds_python_and_js():
    src = """
def build_thing():
    pass
class ThingBuilder: pass
export const render_ui = () => 0
function helper_fn() {}
"""
    got = _defined_in_text(src, {"build_thing", "ThingBuilder", "render_ui", "helper_fn", "absent_sym"})
    assert got == {"build_thing", "ThingBuilder", "render_ui", "helper_fn"}


def test_defined_in_text_ignores_mere_usage():
    """只被调用不算定义——原版的判据也是这个，不能因为批量化而放宽。"""
    assert _defined_in_text("x = build_thing()\nbuild_thing()", {"build_thing"}) == set()


def test_resolve_symbols_hits_and_misses(repo: Path):
    got, trusted = resolve_symbols(repo, _tasks(repo))
    assert trusted is True
    assert got["build_thing"] is True
    assert got["ThingBuilder"] is True
    assert got["render_ui"] is False


def test_score_tasks_complete_and_incomplete(repo: Path):
    rep = score_tasks(repo, _tasks(repo))
    assert rep[1]["complete"] is True
    assert rep[1]["files_missing"] == []
    assert rep[1]["symbols_missing"] == []
    assert rep[2]["complete"] is False
    assert rep[2]["files_missing"] == ["pkg/ui.py"]
    assert rep[2]["symbols_missing"] == ["render_ui"]


def test_score_tasks_ratios(repo: Path):
    rep = score_tasks(repo, _tasks(repo))
    assert rep[1]["file_ratio"] == 1.0 and rep[1]["symbol_ratio"] == 1.0
    assert rep[2]["file_ratio"] == 0.0 and rep[2]["symbol_ratio"] == 0.0


def test_score_tasks_partial(repo: Path):
    """Task 2 的文件到了但符号没定义 -> 部分完成，不能判定为完成。"""
    (repo / "pkg" / "ui.py").write_text("# todo\n", encoding="utf-8")
    rep = score_tasks(repo, _tasks(repo))
    assert rep[2]["files_missing"] == []
    assert rep[2]["symbols_missing"] == ["render_ui"]
    assert rep[2]["complete"] is False


def test_score_tasks_becomes_complete_when_symbol_lands(repo: Path):
    (repo / "pkg" / "ui.py").write_text("def render_ui():\n    return 0\n", encoding="utf-8")
    rep = score_tasks(repo, _tasks(repo))
    assert rep[2]["complete"] is True


def test_score_tasks_no_files_no_symbols_is_not_complete():
    """既没声明文件也没声明符号的任务不能凭空算完成——原版的保守判定，保留。"""
    tasks, _ = parse_plan("### Task 1: x\n\n**Steps:**\n- [ ] **Step 1** do\n")
    rep = score_tasks(Path("."), tasks)
    assert rep[1]["complete"] is False


def test_score_tasks_empty_input():
    assert score_tasks(Path("."), []) == {}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_rg_batch_matches_python_scan(repo: Path):
    syms = ["build_thing", "ThingBuilder", "render_ui"]
    rg_found, ok = _rg_batch(repo, syms)
    assert ok
    assert rg_found == _python_scan(repo, syms)


def test_git_grep_batch_matches_python_scan(repo: Path):
    syms = ["build_thing", "ThingBuilder", "render_ui"]
    gg_found, ok = _git_grep_batch(repo, syms)
    assert ok
    assert gg_found == _python_scan(repo, syms) == {"build_thing", "ThingBuilder"}


def test_all_three_backends_agree(repo: Path, monkeypatch):
    """三条退路必须给同一个答案，否则"有没有装 ripgrep"会改变判定结果。"""
    syms = ["build_thing", "ThingBuilder", "render_ui"]
    expected = {"build_thing", "ThingBuilder"}
    assert _python_scan(repo, syms) == expected
    assert _git_grep_batch(repo, syms)[0] == expected
    if shutil.which("rg"):
        assert _rg_batch(repo, syms)[0] == expected

    # 强制走 Python 兜底：模拟 rg 与 git grep 都不可用。
    monkeypatch.setattr("agent_handoff.core.evidence.shutil.which", lambda _n: None)
    monkeypatch.setattr(
        "agent_handoff.core.evidence._git_grep_batch", lambda *_a, **_k: (set(), False)
    )
    got, _trusted = resolve_symbols(repo, _tasks(repo))
    assert got["build_thing"] and got["ThingBuilder"] and not got["render_ui"]


def test_backends_agree_on_untracked_files(repo: Path):
    """交接的典型时刻是「刚写完、还没 commit」。

    `git grep` 与 `git ls-files` 默认只看已跟踪文件，而 ripgrep 扫工作树。
    不对齐的话，同一个仓库在装了 ripgrep 的机器上判「已定义」、没装的机器上
    判「缺失」——完成度取决于工具链而不是代码。
    """
    (repo / "pkg" / "fresh.py").write_text("def just_written():\n    pass\n", encoding="utf-8")
    syms = ["just_written"]
    gg, ok = _git_grep_batch(repo, syms)
    assert ok
    assert gg == {"just_written"}, "git grep 漏了未跟踪文件"
    assert _python_scan(repo, syms) == {"just_written"}, "python 兜底漏了未跟踪文件"
    if shutil.which("rg"):
        rg, ok = _rg_batch(repo, syms)
        assert ok and rg == {"just_written"}


def test_symbol_with_regex_metacharacters_is_escaped(repo: Path, monkeypatch):
    """符号名里出现正则元字符时不能把整条批量正则搞坏。"""
    src = "def normal_sym():\n    pass\n"
    (repo / "pkg" / "core.py").write_text(src, encoding="utf-8")
    weird = ["normal_sym", "a.b*c", "x[1]"]
    found = _python_scan(repo, weird)
    assert found == {"normal_sym"}


def test_batching_over_limit(repo: Path, monkeypatch):
    """符号数超过单批上限时要分批，且结果与不分批一致。"""
    monkeypatch.setattr("agent_handoff.core.evidence.SYMBOL_BATCH", 2)
    syms = ["build_thing", "ThingBuilder", "render_ui", "nope_a", "nope_b"]
    assert _git_grep_batch(repo, syms)[0] == {"build_thing", "ThingBuilder"}


def test_hint_files_short_circuit_avoids_full_scan(repo: Path, monkeypatch):
    """任务自己声明的文件里就能找到全部符号时，不该再跑全库检索。"""
    calls = []
    monkeypatch.setattr(
        "agent_handoff.core.evidence._rg_batch",
        lambda r, s, *_a: (calls.append(s), (set(), True))[1],
    )
    monkeypatch.setattr(
        "agent_handoff.core.evidence._git_grep_batch",
        lambda r, s, *_a: (calls.append(s), (set(), True))[1],
    )
    tasks, _ = parse_plan(
        "### Task 1: x\n\n**Files:**\n- Create: `pkg/core.py`\n- Create: `pkg/util.py`\n\n"
        "**Interfaces:**\n- Produces `build_thing()`, `ThingBuilder`\n"
    )
    got, trusted = resolve_symbols(repo, tasks)
    assert got == {"build_thing": True, "ThingBuilder": True}
    assert trusted is True
    assert calls == [], "命中提示文件后不该再做全库检索"


# --- 现代 TS / JS 定义形态 -------------------------------------------------
# 这些写法一个关键字前缀都没有。原版（以及 v2.0.0）只认「关键字 + 空格 + 名字」，
# 于是一个写满 `undo: () => void` 的 TS 仓库会被判成「符号全缺」，接续会话
# 重做已经做完的工作。下面每一条都对应一处真实误判。


@pytest.mark.parametrize(
    ("label", "src"),
    [
        ("interface 成员", "export interface Intent {\n  undo: () => void\n}"),
        ("type 成员", "type T = {\n  undo: () => void\n}"),
        ("对象字面量箭头函数", "const intent = {\n  undo: () => {\n    step()\n  },\n}"),
        ("方法简写带参数", "class M {\n  undo(action: () => void) {\n  }\n}"),
        ("方法简写无参数", "class M {\n  undo() {\n  }\n}"),
        ("带返回类型注解", "class M {\n  undo(): Promise<void> {\n  }\n}"),
        ("async 方法", "class M {\n  async undo(): Promise<void> {\n  }\n}"),
        ("get 访问器", "class M {\n  get undo(): Fn {\n  }\n}"),
        ("注释之后的定义", "// 说明\n  undo: () => void"),
    ],
)
def test_defined_in_text_finds_modern_ts_forms(label: str, src: str):
    assert _defined_in_text(src, {"undo"}) == {"undo"}, label


@pytest.mark.parametrize(
    ("label", "src"),
    [
        ("属性访问调用", "intent.undo()"),
        ("this 方法调用", "    this.undo(() => {\n"),
        ("模型方法调用", "workflowEditorModel.undo(() => {\n"),
        ("裸调用语句", "  undo()"),
        ("表达式里使用", "const x = undo() + 1"),
        ("import 命名导入", 'import { undo } from "./store"'),
        ("三元表达式", "const f = flag ? undo : redo"),
        ("行注释里的关键字", "// interface undo 由 store 提供"),
        ("JSDoc 续行", " * @param type undo 撤销类型"),
        ("块注释", "/* type undo 在别处 */"),
        ("Python 注释", "# 保存当前状态到 undo 栈"),
        ("URL 里的双斜杠", 'const u = "https://x/interface undo"'),
    ],
)
def test_defined_in_text_rejects_references(label: str, src: str):
    """引用不是定义。假阳性会让 update_plan 勾掉从未实现的步骤，比假阴性更危险。"""
    assert _defined_in_text(src, {"undo", "redo"}) == set(), label


def test_plan_document_does_not_satisfy_itself(repo: Path):
    """计划文档里的代码片段不能成为「已实现」的证据。

    计划文档写着 ``- Produces `render_ui` ``，而 `pkg/ui.py` 根本不存在。
    不排除计划文档时，全库检索会搜到计划文档自己，把符号判成已定义，
    然后 update_plan 勾掉从未实现的步骤。
    """
    plan = repo / "docs" / "plan.md"
    plan.write_text(
        plan.read_text(encoding="utf-8") + "\n<!-- def render_ui 出现在计划文档里 -->\n",
        encoding="utf-8",
    )
    rep = score_tasks(repo, _tasks(repo), plan_rel="docs/plan.md")
    assert rep[2]["symbols_missing"] == ["render_ui"]
    assert rep[2]["complete"] is False


def test_score_tasks_marks_untrusted_search(repo: Path, monkeypatch):
    """三条后端全失败时不能判「完成」——那是「没查成」，不是「查过、没有」。"""
    monkeypatch.setattr(
        "agent_handoff.core.evidence.resolve_symbols",
        lambda *_a, **_k: ({"build_thing": False, "ThingBuilder": False, "render_ui": False}, False),
    )
    rep = score_tasks(repo, _tasks(repo))
    assert rep[1]["symbols_trusted"] is False
    assert rep[1]["complete"] is False
