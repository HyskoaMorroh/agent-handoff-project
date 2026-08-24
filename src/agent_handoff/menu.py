#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交互式菜单 —— 给不想记命令行参数的人用。

由 双击运行.cmd（Windows）或 run.sh（Linux/macOS）启动。所有文案都走 i18n，
.cmd 里只留 ASCII，避免 Windows 批处理在 GBK 终端下把中文注释当命令执行。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .i18n import LANG_NAMES, Translator, available, detect, normalize
from .platform import clear_screen, find_guide, force_utf8_io, open_in_browser

LINE = "─" * 62
# 语言选择记在这里，下次启动沿用。放在用户配置目录而不是包目录：
# 包可能装在只读位置（系统 site-packages）。
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "agent-handoff" / "lang"


def _load_lang() -> str:
    try:
        return normalize(CONFIG.read_text(encoding="utf-8").strip())
    except OSError:
        return detect()


def _save_lang(lang: str) -> None:
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(lang, encoding="utf-8")
    except OSError:
        pass  # 存不下就算了，不该因为写配置失败而中断菜单


def _is_yes(tr: Translator, answer: str) -> bool:
    """这个回答算「是」吗。

    确认词必须从文案表来，不能写死在代码里。原版写死成
    `("y", "yes", "是", "好")`——只覆盖英文与简体，繁中用户输入「是」凑巧能中，
    但输入繁体习惯的「要」「確定」就落到 else 分支，`--by-repo` 静默不生效，
    用户以为功能坏了。而 `scripts/check_i18n.py` 的白名单以**整个文件**为粒度
    把 menu.py 排除在 CJK 字面量检查之外，所以 CI 一直看不见这处。

    英文 `y`/`yes` 始终接受：它们是终端交互的通用惯例，任何语言的用户都可能
    习惯性地敲 y，把它们排除掉只会制造新的挫败。
    """
    val = answer.strip().lower()
    if val in ("y", "yes"):
        return True
    # 文案表里是逗号分隔的候选词，方便译者按自己语言的习惯补充多个说法。
    words = [w.strip().lower() for w in tr.t("menu.confirm.yes").split(",")]
    return val in [w for w in words if w]


def run_tool(tr: Translator, *args: str) -> int:
    """以子进程跑 CLI。用 -m 而不是脚本路径，装在哪都能跑。"""
    return subprocess.call([sys.executable, "-m", "agent_handoff.cli", "--lang", tr.lang, *args])


def pause(tr: Translator, key: str = "menu.pause") -> None:
    try:
        input(f"\n  {tr.t(key)}")
    except (EOFError, KeyboardInterrupt):
        pass


def ask_repo(tr: Translator) -> Path | None:
    from .core.gitops import is_repo

    print(f"\n  {LINE}")
    print(tr.t("menu.ask_repo1"))
    print(tr.t("menu.ask_repo2"))
    print(f"  {LINE}\n")
    try:
        raw = input(tr.t("menu.ask_repo.input")).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        print("\n" + tr.t("menu.ask_repo.empty"))
        return None
    # 拖放会自动加引号；POSIX 的拖放还可能转义空格。
    raw = raw.strip().strip('"').strip("'")
    if os.name != "nt":
        raw = raw.replace("\\ ", " ")
    p = Path(raw).expanduser()
    if not p.is_dir():
        print("\n" + tr.t("menu.ask_repo.bad") + f"\n    {p}")
        return None
    if not is_repo(p):
        # 提示但不拦。缺 git 只让「提交快照」这一步做不了；会话传承、
        # 完成度评估、测试取证照旧，而那往往正是用户要的。
        print("\n" + tr.t("menu.ask_repo.not_git") + f"\n    {p}\n")
        print(tr.t("menu.ask_repo.not_git2"))
        print(tr.t("menu.ask_repo.not_git3"))
    return p


def menu_vitals(tr: Translator) -> None:
    clear_screen()
    print(f"\n  {tr.t('menu.vitals.title')}\n  {LINE}")
    print(tr.t("menu.vitals.lead1"))
    print(tr.t("menu.vitals.lead2"))
    print(tr.t("menu.vitals.lead3"))
    print(f"  {LINE}\n")
    run_tool(tr, "--vitals")
    print(f"\n  {LINE}")
    print(tr.t("menu.vitals.after"))
    print(f"  {LINE}")
    pause(tr)


def menu_sweep(tr: Translator) -> None:
    """磁盘占用报告。这一屏只看不改，所以不问任何问题，直接跑。

    按仓库聚合要读转录内容（慢得多），所以做成一个问句而不是默认打开——
    多数时候用户只想知道「占了多少、哪些能扔」，那只需要 stat。
    """
    clear_screen()
    print(f"\n  {tr.t('menu.sweep.title')}\n  {LINE}")
    print(tr.t("menu.sweep.lead"))
    print(tr.t("menu.sweep.lead2"))
    print(f"  {LINE}\n")
    try:
        want = input(tr.t("menu.sweep.ask_repo")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    print()
    if _is_yes(tr, want):
        run_tool(tr, "--sweep", "--by-repo")
    else:
        run_tool(tr, "--sweep")
    print(f"\n  {LINE}")
    print(tr.t("menu.sweep.after"))
    print(f"  {LINE}")
    pause(tr)


def menu_find(tr: Translator) -> None:
    clear_screen()
    print(f"\n  {tr.t('menu.find.title')}\n  {LINE}")
    print(tr.t("menu.find.lead"))
    print(tr.t("menu.find.opt1"))
    print(tr.t("menu.find.opt2"))
    print(tr.t("menu.find.opt3"))
    print(f"  {LINE}\n")
    try:
        kw = input(tr.t("menu.find.input")).strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not kw:
        print("\n" + tr.t("menu.ask_repo.empty"))
        pause(tr)
        return
    print()
    run_tool(tr, "--find", kw)
    print(f"\n  {LINE}")
    print(tr.t("menu.find.after"))
    print(f"  {LINE}")
    pause(tr)


def menu_handoff(tr: Translator) -> None:
    clear_screen()
    print(f"\n  {tr.t('menu.handoff.title')}\n  {LINE}")
    print(tr.t("menu.handoff.lead"))
    print(f"  {LINE}")
    repo = ask_repo(tr)
    if repo is None:
        pause(tr)
        return

    clear_screen()
    print(f"\n  {tr.t('menu.handoff.dry_title')}\n  {LINE}")
    print(tr.t("menu.handoff.dry_lead"))
    print(f"  {LINE}\n")
    run_tool(tr, str(repo), "--dry-run", "--skip-tests")
    print(f"\n  {LINE}")
    print(tr.t("menu.handoff.dry_after"))
    print(f"  {LINE}")
    pause(tr, "menu.pause_run")

    clear_screen()
    print(f"\n  {tr.t('menu.handoff.run_title')}\n  {LINE}")
    print(tr.t("menu.handoff.run_lead"))
    print(f"  {LINE}\n")
    # --pick-sessions：让用户勾选要传承的会话。预演那一遍不带它——预演的目的是
    # 看清将要发生什么，中间插一个需要输入的问答会打断阅读。
    code = run_tool(tr, str(repo), "--pick-sessions")
    print(f"\n  {LINE}")
    if code == 0:
        print(tr.t("menu.handoff.ok1"))
        print(tr.t("menu.handoff.ok2"))
    else:
        print(tr.t("menu.handoff.fail", code=code))
    print(f"  {LINE}")
    pause(tr)


def menu_quick(tr: Translator) -> None:
    clear_screen()
    print(f"\n  {tr.t('menu.quick.title')}\n  {LINE}")
    print(tr.t("menu.quick.lead1"))
    print(tr.t("menu.quick.lead2"))
    print(f"  {LINE}")
    repo = ask_repo(tr)
    if repo is None:
        pause(tr)
        return
    print()
    code = run_tool(tr, str(repo), "--skip-tests", "--pick-sessions")
    print(f"\n  {LINE}")
    print(tr.t("menu.quick.ok") if code == 0 else tr.t("menu.quick.fail", code=code))
    print(f"  {LINE}")
    pause(tr)


def menu_help(tr: Translator) -> None:
    clear_screen()
    print(f"\n  {tr.t('menu.help.title')}\n  {LINE}")
    # 指南只有一份 guide.html，三种语言的文案都在里面，靠 #lang= 挑。
    # 原先按 guide.{lang}.html 找文件：build_guide.py 从不产出这种文件名，
    # 于是英文和繁中用户必然落到简体那一档。
    #
    # 查找交给 platform.find_guide()：此前这里自己往上数三层，只认源码检出，
    # `pip install` 之后必然落到「找不到」那一档。
    guide = find_guide()
    if guide:
        print(f"  {guide}")
        target = f"{guide.as_uri()}#lang={tr.lang}"
        print("\n" + (tr.t("menu.help.opened") if open_in_browser(target) else tr.t("menu.help.failed")))
    else:
        print(tr.t("menu.help.missing"))
    print(f"  {LINE}")
    pause(tr)


def menu_gui(tr: Translator) -> None:
    clear_screen()
    print(f"\n  {tr.t('menu.gui.title')}\n  {LINE}")
    print(tr.t("menu.gui.lead"))
    print(tr.t("menu.gui.stop"))
    print(f"  {LINE}\n")
    from .gui.server import serve

    try:
        serve(lang=tr.lang)
    except KeyboardInterrupt:
        pass
    pause(tr)


def menu_lang(tr: Translator) -> Translator:
    clear_screen()
    langs = list(available())
    print(f"\n  {tr.t('menu.lang.title')}\n  {LINE}")
    print(tr.t("menu.lang.current", lang=LANG_NAMES.get(tr.lang, tr.lang)))
    print()
    for i, code in enumerate(langs, 1):
        mark = " ←" if code == tr.lang else ""
        print(f"     {i}    {LANG_NAMES.get(code, code)}{mark}")
    print(f"  {LINE}")
    try:
        raw = input(tr.t("menu.lang.input")).strip()
    except (EOFError, KeyboardInterrupt):
        return tr
    if raw.isdigit() and 1 <= int(raw) <= len(langs):
        chosen = langs[int(raw) - 1]
        _save_lang(chosen)
        tr = Translator(chosen)
        print("\n" + tr.t("menu.lang.saved", lang=LANG_NAMES.get(chosen, chosen)))
        pause(tr)
    return tr


def main() -> int:
    force_utf8_io()
    tr = Translator(_load_lang())

    while True:
        clear_screen()
        print()
        print(f"  {'═' * 62}")
        print(f"     {tr.t('menu.title')}")
        print(f"     {tr.t('gui.subtitle')}")
        print(f"  {'═' * 62}")
        print()
        for n in ("1", "2", "3", "4", "5", "6", "7", "8"):
            print(f"     {n}    {tr.t('menu.' + n)}")
        print(f"     0    {tr.t('menu.0')}")
        print()
        print(f"  {'─' * 62}")
        print(f"     {tr.t('menu.hint1')}")
        print(f"     {tr.t('menu.hint2')}")
        print(f"  {'─' * 62}")
        print()
        try:
            choice = input(tr.t("menu.choose")).strip()
        except (EOFError, KeyboardInterrupt):
            return 0

        if choice == "1":
            menu_vitals(tr)
        elif choice == "2":
            menu_handoff(tr)
        elif choice == "3":
            menu_quick(tr)
        elif choice == "4":
            menu_find(tr)
        elif choice == "5":
            # 磁盘报告插在「找会话」之后：两者都是只读的查看类操作，
            # 排在一起比夹在「说明」和「网页界面」之间好找。
            menu_sweep(tr)
        elif choice == "6":
            menu_help(tr)
        elif choice == "7":
            menu_gui(tr)
        elif choice == "8":
            tr = menu_lang(tr)
        elif choice in ("0", "q", "Q", ""):
            return 0


def main_entry() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main_entry()
