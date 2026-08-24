#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从项目元数据推断测试命令与环境陷阱。不硬编码任何项目知识。"""
from __future__ import annotations

import json
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..i18n import Translator
from ..platform import shell_quote, venv_interpreters
from .gitops import dirty_submodules, run

PY_PASS = re.compile(r"(\d+) passed")
PY_FAIL = re.compile(r"(\d+) failed")
PY_ERROR = re.compile(r"(\d+) error")
PY_SKIP = re.compile(r"(\d+) skipped")
PY_FAILED_ID = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
VITEST_TESTS = re.compile(r"Tests\s+(?P<n>\d+)\s+passed", re.I)
VITEST_FAIL = re.compile(r"Tests\s+(?P<f>\d+)\s+failed", re.I)
JEST_SUMMARY = re.compile(r"Tests:\s+(?:(?P<f>\d+) failed,\s+)?(?:\d+ skipped,\s+)?(?P<p>\d+) passed", re.I)
CARGO_RESULT = re.compile(r"test result: \w+\. (?P<p>\d+) passed; (?P<f>\d+) failed")
GO_FAIL = re.compile(r"^--- FAIL: (\S+)", re.M)


def detect_test_commands(repo: Path) -> dict[str, str]:
    """从项目元数据推断测试命令。不硬编码任何项目知识。

    解释器路径统一走 shell_quote：含空格的路径（`C:\\Program Files\\...`）
    在原版里会被 shell 拆成两个参数，命令直接失败。
    """
    cmds: dict[str, str] = {}

    if (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file() or (repo / "tests").is_dir():
        py_rel = ""
        # 同一个仓库可能同时有 Windows 与 POSIX 的 venv；按当前平台优先。
        for name in (".venv-win", ".venv", "venv", ".virtualenv", "env"):
            venv = repo / name
            if not venv.is_dir():
                continue
            exe, rel = venv_interpreters(venv)
            if exe:
                py_rel = f"{name}/{rel}"
                break
        target = "./tests" if (repo / "tests").is_dir() else "."
        if py_rel:
            cmds["backend"] = f"{shell_quote(py_rel)} -m pytest {target} -q"
        elif shutil.which("uv"):
            cmds["backend"] = f"uv run --isolated --frozen python -m pytest {target} -q"
        else:
            # 用完整路径而不是原版的 `Path(sys.executable).name`：裸名字依赖 PATH，
            # 而本工具常被某个 venv 的解释器启动，那个解释器往往不在 PATH 里——
            # 于是记录进交接文件的命令跑起来会换成另一个 Python。
            cmds["backend"] = f"{shell_quote(sys.executable)} -m pytest {target} -q"

    pkgs = sorted(repo.glob("*/package.json"))
    if (repo / "package.json").is_file():
        pkgs.append(repo / "package.json")
    for pkg in pkgs:
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        scripts = data.get("scripts") or {}
        if not isinstance(scripts, dict):
            continue
        sub = pkg.parent.relative_to(repo).as_posix()
        prefix = f"npm --prefix {sub} run " if sub != "." else "npm run "
        # 挑真实存在的脚本名，而不是假设叫 "test"。
        for key in ("test:unit", "test", "vitest", "jest"):
            if key in scripts:
                cmds[f"frontend-test ({sub})"] = f"{prefix}{key} -- --run"
                break
        if "type-check" in scripts:
            cmds[f"frontend-types ({sub})"] = f"{prefix}type-check"
        elif "typecheck" in scripts:
            cmds[f"frontend-types ({sub})"] = f"{prefix}typecheck"
        if "lint:check" in scripts:
            cmds[f"frontend-lint ({sub})"] = f"{prefix}lint:check"
        elif "lint" in scripts:
            cmds[f"frontend-lint ({sub})"] = f"{prefix}lint"

    # Rust / Go：有清单文件就问，跟 Python / Node 一视同仁。
    if (repo / "Cargo.toml").is_file() and shutil.which("cargo"):
        cmds["rust"] = "cargo test --quiet"
    if (repo / "go.mod").is_file() and shutil.which("go"):
        cmds["go"] = "go test ./..."
    return cmds


def summarize_test_output(name: str, out: str, tr: Translator) -> str:
    """把一次测试运行压成一行，保留失败标识符。

    `name` 用来在多框架输出难以区分时给出提示——原版收了这个参数却从没用过。
    """
    # 超时要**最先**判定，在任何框架摘要之前。
    #
    # 为什么顺序是关键：超时是把进程杀掉，被杀之前它已经打印了一部分结果。
    # 实测 `"…3 passed\n<timeout after 900s>"` 这种输出，若先匹配 pytest 摘要，
    # 摘要分支立刻 return `"3 passed"`——交接文档于是声称测试通过，
    # 而真相是测试被杀、剩下的用例一个都没跑。
    #
    # 这是本工具最不该出的错：它存在的意义就是给出可信的实测证据，
    # 而「把超时报成通过」比不报测试结果糟得多——后者只是缺信息，
    # 前者是给出了假信息，接续会话会据此认为代码是好的。
    #
    # 保留已跑出的部分结果：它有诊断价值（知道跑到哪一步卡住），
    # 但必须和超时标记一起出现，让读者看到这是不完整的结果。
    if "<timeout after" in out:
        m = re.search(r"timeout after (\d+)s", out)
        head = tr.t("cli.tests.timeout", sec=m.group(1)) if m else tr.t("cli.tests.no_output")
        partial = PY_PASS.search(out)
        if partial:
            head += tr.t("cli.tests.timeout.partial", n=partial.group(1))
        return head
    if out.strip() == "<command not found>":
        return tr.t("cli.tests.not_found")

    passed = PY_PASS.search(out)
    failed = PY_FAIL.search(out)
    errored = PY_ERROR.search(out)
    if passed or failed or errored:
        parts = []
        if failed:
            parts.append(f"{failed.group(1)} failed")
        if errored:
            parts.append(f"{errored.group(1)} error")
        if passed:
            parts.append(f"{passed.group(1)} passed")
        skipped = PY_SKIP.search(out)
        if skipped:
            parts.append(f"{skipped.group(1)} skipped")
        line = ", ".join(parts)
        ids = PY_FAILED_ID.findall(out)
        if ids:
            line += "\n" + "\n".join(f"      FAILED {i}" for i in dict.fromkeys(ids[:12]))
        return line

    v_fail = VITEST_FAIL.search(out)
    v_pass = VITEST_TESTS.search(out)
    if v_fail or v_pass:
        bits = []
        if v_fail:
            bits.append(f"{v_fail.group('f')} failed")
        if v_pass:
            bits.append(f"{v_pass.group('n')} passed")
        return ", ".join(bits)

    j = JEST_SUMMARY.search(out)
    if j:
        bits = []
        if j.group("f"):
            bits.append(f"{j.group('f')} failed")
        bits.append(f"{j.group('p')} passed")
        return ", ".join(bits)

    c = CARGO_RESULT.search(out)
    if c:
        return f"{c.group('f')} failed, {c.group('p')} passed"

    go_fails = GO_FAIL.findall(out)
    if go_fails:
        return f"{len(go_fails)} failed\n" + "\n".join(f"      FAILED {x}" for x in go_fails[:12])
    if re.search(r"^ok\s+\S+", out, re.M):
        return "ok"

    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-2:]
    joined = " / ".join(t.strip()[:120] for t in tail)
    return joined or f"{name}: {tr.t('cli.tests.no_output')}"


def run_tests(
    repo: Path,
    commands: dict[str, str],
    timeout: int,
    tr: Translator,
    on_start=None,
    on_done=None,
    parallel: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """跑全部测试命令，返回 (每条一行的摘要, 失败标识符列表)。

    前端类型检查、后端 pytest、lint 之间互不依赖，可以并行——一个大项目里
    这能把五分钟压到最慢那一条的时长。但只在命令数 > 1 且没人要看流式输出
    时才并行；单条命令并行没有收益，只增加输出乱序的风险。

    并行安全性：这些命令都只读源码，不写工作树。唯一的例外是可能生成
    coverage / .pytest_cache，那些落在各自子目录，不会互相覆盖。
    """
    results: dict[str, str] = {}
    failing: list[str] = []
    if not commands:
        return results, failing

    def one(item: tuple[str, str]) -> tuple[str, str, str]:
        name, cmd = item
        p = run(cmd, repo, timeout)
        return name, cmd, p.out

    items = list(commands.items())
    if parallel and len(items) > 1:
        for name, cmd in items:
            if on_start:
                on_start(name, cmd)
        with ThreadPoolExecutor(max_workers=min(len(items), 4)) as pool:
            for name, _cmd, out in pool.map(one, items):
                line = summarize_test_output(name, out, tr)
                results[name] = line
                if on_done:
                    on_done(name, line)
                failing += [f"FAILED {i}" for i in PY_FAILED_ID.findall(out)[:6]]
    else:
        for item in items:
            if on_start:
                on_start(item[0], item[1])
            name, cmd, out = one(item)
            line = summarize_test_output(name, out, tr)
            results[name] = line
            if on_done:
                on_done(name, line)
            failing += [f"FAILED {i}" for i in PY_FAILED_ID.findall(out)[:6]]
    return results, failing


def detect_env_pitfalls(repo: Path, tr: Translator) -> list[str]:
    """新会话否则要重新踩一遍的环境陷阱。"""
    notes: list[str] = []

    for venv in sorted(repo.glob(".venv*")) + sorted(repo.glob("venv")):
        if not venv.is_dir():
            continue
        exe, rel = venv_interpreters(venv)
        if exe is None:
            try:
                detail = ", ".join(sorted(p.name for p in venv.iterdir())[:6]) or tr.t("env.venv_empty")
            except OSError:
                detail = tr.t("env.venv_empty")
            notes.append(tr.t("env.venv_broken", name=venv.name, detail=detail))
        else:
            notes.append(tr.t("env.venv_ok", path=f"{venv.name}/{rel}"))

    for pkg in sorted(repo.glob("*/package.json")):
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        scripts = data.get("scripts") or {}
        if not isinstance(scripts, dict):
            continue
        sub = pkg.parent.relative_to(repo).as_posix()
        if "test" not in scripts:
            avail = [k for k in scripts if "test" in k.lower()]
            if avail:
                notes.append(
                    tr.t("env.no_test_script", pkg=sub, names=", ".join(f"`{a}`" for a in avail))
                )

    # 锁文件里混入非官方源会破坏跨环境可复现性。原版硬编码了 webui/yarn.lock，
    # 这里改成扫所有一级子目录，仍然不硬编码任何项目名。
    locks = [repo / "yarn.lock", repo / "package-lock.json", repo / "pnpm-lock.yaml"]
    locks += sorted(repo.glob("*/yarn.lock")) + sorted(repo.glob("*/package-lock.json"))
    locks += sorted(repo.glob("*/pnpm-lock.yaml"))
    seen: set[str] = set()
    for lf in locks:
        if not lf.is_file() or str(lf) in seen:
            continue
        seen.add(str(lf))
        try:
            body = lf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        mirrors = sorted(set(re.findall(r"https://(registry\.[a-z0-9.\-]+)/", body)))
        foreign = [m for m in mirrors if "npmjs.org" not in m]
        if foreign:
            notes.append(
                tr.t(
                    "env.foreign_registry",
                    lock=lf.relative_to(repo).as_posix(),
                    mirrors=", ".join(foreign),
                )
            )

    # 子模块脏了是一类静默丢工作：父仓库提交不带它，接续会话看到不一致的树。
    for sub in dirty_submodules(repo):
        notes.append(tr.t("env.git_dirty_submodule", path=sub))
    return notes
