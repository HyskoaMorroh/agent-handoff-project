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
from .platform import clear_screen, force_utf8_io, open_in_browser

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
    # 说明文档按语言优先挑；找不到就退回项目根的任意一份。
    root = Path(__file__).resolve().parent.parent.parent
    names = [f"docs/guide.{tr.lang}.html", "docs/guide.zh-Hans.html", "docs/guide.html", "使用说明.html"]
    guide = next((root / n for n in names if (root / n).is_file()), None)
    if guide:
        print(f"  {guide}")
        print("\n" + (tr.t("menu.help.opened") if open_in_browser(str(guide)) else tr.t("menu.help.failed")))
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
        for n in ("1", "2", "3", "4", "5", "6", "7"):
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
            menu_help(tr)
        elif choice == "6":
            menu_gui(tr)
        elif choice == "7":
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
