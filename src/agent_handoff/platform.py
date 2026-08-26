#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台差异的唯一收容所。

原版脚本把 Windows 假设散落在各处（`os.startfile`、只匹配盘符的路径正则、
只在 nt 上做的斜杠归一化），Linux 上会静默降级而不是报错——那比崩溃更糟。
所有分支集中在这里，其他模块只调函数，不问自己跑在哪个系统上。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# 原版只认 `C:\...` 形式，Linux/macOS 的 `/home/me/proj` 一个都匹配不到，
# guess_repos 在非 Windows 上等于永远返回空。两种形态都要认。
WIN_PATH_RX = re.compile(r'[A-Za-z]:[\\/][^\s"\'<>|*?\n]{3,140}')
POSIX_PATH_RX = re.compile(r'(?:/[\w.\-+@]+){2,12}/?')
# 路径尾部要削掉的半角标点。
PATH_TRIM = '\\/",。、）)]}>;:='
# 全角标点是句子里的分隔符，不可能出现在路径中间——出现即意味着路径已经结束。
# 用户几乎总是写「仓库 E:/output/proj，分支 main」，而全角逗号不是空白字符，
# WIN_PATH_RX 的 `[^\s...]` 会把它连同后面的「分支」一起吃进候选路径，于是
# `nearest_repo` 去找 `E:/output/proj，分支` 这个不存在的目录，仓库推断静默
# 失败——实测本机 5/6 个最新 Codex 转录的 repos 因此为空。
# 只在这里截断而不是把所有中日韩字符都排除：中文目录名（`E:/项目/前端`）是
# 合法路径，一并排除会引入新的漏检。
_FULLWIDTH_CUT = re.compile(r"[，。、：；！？（）【】《》「」“”…]")


def iter_path_candidates(text: str):
    """从任意文本里捞出「看起来像绝对路径」的片段，两种平台形态都认。

    先扫盘符形态，再扫 POSIX 形态。跨平台捞取而不是按当前系统二选一：
    转录可能是在另一台机器上产生的（WSL 写的记录在 Windows 上读，或反之）。

    命中片段先在第一个全角标点处截断，再削尾部标点——见 `_FULLWIDTH_CUT`。
    """
    seen = set()
    for rx in (WIN_PATH_RX, POSIX_PATH_RX):
        for m in rx.finditer(text):
            cand = m.group(0)
            cut = _FULLWIDTH_CUT.search(cand)
            if cut:
                cand = cand[: cut.start()]
            cand = cand.rstrip(PATH_TRIM)
            if len(cand) < 4 or cand in seen:
                continue
            seen.add(cand)
            yield cand


def venv_interpreters(venv: Path) -> tuple[Path | None, str]:
    """返回 (可执行 Python 路径, 相对写法)；没有就 (None, "")。

    Windows 放在 `Scripts/python.exe`，POSIX 放在 `bin/python`（有时只有
    `bin/python3`）。三种都查，不假设当前系统的布局就是唯一布局——同一个仓库
    可能被 Windows 和 WSL 交替使用，两种 venv 目录会同时存在。
    """
    for rel in ("Scripts/python.exe", "bin/python", "bin/python3", "Scripts/python3.exe"):
        cand = venv / rel
        if cand.is_file():
            return cand, rel
    return None, ""


def shell_quote(path: str) -> str:
    """把路径转成能直接塞进 shell 命令行的形态。

    Windows 的 cmd.exe 无法执行 `.venv-win/Scripts/python.exe` 这种带正斜杠的
    相对路径——它会把第一段当命令名。POSIX shell 反过来：反斜杠是转义符。
    另外含空格的路径两边都要加引号，原版漏了这点，装在 `C:\\Program Files`
    下的解释器会被拆成两个参数。
    """
    p = path.replace("/", "\\") if IS_WINDOWS else path.replace("\\", "/")
    return f'"{p}"' if " " in p else p


def normalize_shell_paths(cmd: str) -> str:
    """归一化 shell 字符串里的可执行文件路径分隔符。

    原版只在 nt 上做，且只认 `.exe` 结尾。POSIX 上 `.venv\\bin\\python` 这种
    （从 Windows 配置里抄来的）命令会因为反斜杠被当成转义符而失败。
    """
    if IS_WINDOWS:
        return re.sub(
            r"(?<![\w:])((?:\.[\w.\-]+|[\w.\-]+)(?:/[\w.\-]+)+\.exe)",
            lambda m: m.group(1).replace("/", "\\"),
            cmd,
        )
    return re.sub(
        r"(?<![\w:])((?:\.[\w.\-]+|[\w.\-]+)(?:\\[\w.\-]+)+)(?=\s|$)",
        lambda m: m.group(1).replace("\\", "/"),
        cmd,
    )


def open_in_browser(target: str) -> bool:
    """在系统默认应用里打开文件或 URL。成功返回 True。

    `os.startfile` 只有 Windows 有，原版在 Linux 上会抛 AttributeError 并被
    宽泛的 except 吞掉，用户只看到「自动打开失败」。三平台各走各的路。
    """
    try:
        if IS_WINDOWS:
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
            return True
        if IS_MACOS:
            return subprocess.run(["open", target], check=False).returncode == 0
        for opener in ("xdg-open", "gio", "wslview"):
            if shutil.which(opener):
                args = [opener, "open", target] if opener == "gio" else [opener, target]
                return subprocess.run(args, check=False).returncode == 0
        # 无桌面环境（纯 SSH 会话）时退回 webbrowser，它还能试 BROWSER 变量。
        import webbrowser

        return webbrowser.open(target)
    except OSError:
        return False


def clear_screen() -> None:
    """清屏。终端不支持时静默跳过，不要把控制字符吐到日志里。"""
    if not sys.stdout.isatty():
        return
    os.system("cls" if IS_WINDOWS else "clear")  # noqa: S605


def force_utf8_io() -> None:
    """把 stdout 与 stderr 都钉到 UTF-8。

    原版只处理了 stdout，于是 GBK 控制台下 stderr 上的中文报错会变成
    UnicodeEncodeError——错误信息本身把错误吃掉了。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def agent_session_roots() -> list[tuple[str, Path]]:
    """各智能体存放转录的位置。不存在的目录直接跳过。

    Windows 的 Claude Code / Codex 装在 %USERPROFILE% 下；在 WSL 里跑时，
    真正的记录往往在 Windows 侧的用户目录，所以两处都找。

    转录**不会**因为项目在别的盘而跟着搬家：实测本机 E 盘上的项目，转录仍在
    C 盘家目录下，盘符只出现在转录内容记录的 `cwd` 里。但两个应用都允许用
    环境变量把整个数据目录挪走（`CLAUDE_CONFIG_DIR` / `CODEX_HOME`），
    笔记本上把它们指向 D 盘是常见做法。不读这两个变量的后果不是少扫几个文件，
    而是「一个转录都找不到」——所以它们优先于家目录。

    Codex 还多一层：`CODEX_SESSIONS_ROOT` 直接指向**会话目录本身**，比
    `CODEX_HOME` 更靠前（见 codex-rs 的会话路径解析顺序：CODEX_SESSIONS_ROOT
    → CODEX_HOME/sessions → ~/.codex/sessions）。它与 CODEX_HOME 不是同一层，
    不能拼 `sessions` 子目录上去。
    """
    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(name: str, p: Path) -> None:
        key = norm_path(str(p))
        if key in seen:
            return
        seen.add(key)
        if p.is_dir():
            roots.append((name, p))

    # 会话目录被直接指定时最优先：它已经是终点，不再拼子目录。
    raw_sessions = (os.environ.get("CODEX_SESSIONS_ROOT") or "").strip().strip('"')
    if raw_sessions:
        add("Codex", Path(os.path.expanduser(os.path.expandvars(raw_sessions))))

    # 显式指定的数据目录优先。`CLAUDE_CONFIG_DIR` 指向 `.claude` 本身，
    # `CODEX_HOME` 指向 `.codex` 本身——都是目录，不是它们的父目录。
    for env, name, subs in (
        ("CLAUDE_CONFIG_DIR", "Claude Code", ("projects",)),
        ("CODEX_HOME", "Codex", ("sessions", "archived_sessions")),
    ):
        raw = (os.environ.get(env) or "").strip().strip('"')
        if not raw:
            continue
        base = Path(os.path.expanduser(os.path.expandvars(raw)))
        for sub in subs:
            add(name, base / sub)

    homes = [Path(os.path.expanduser("~"))]

    # WSL：/mnt/c/Users/<name> 才是宿主机的家目录，转录在那边。
    if not IS_WINDOWS:
        for base in (Path("/mnt/c/Users"), Path("/c/Users")):
            if not base.is_dir():
                continue
            try:
                for entry in base.iterdir():
                    if entry.is_dir() and entry.name.lower() not in {"public", "default", "all users"}:
                        homes.append(entry)
            except OSError:
                pass

    for home in homes:
        # `.codex/archived_sessions` 也要扫：在 Codex 里归档一个会话只是把它移出
        # 活动列表，转录本身还在，而「归档了但还想把结论带走」恰恰是交接的典型
        # 场景——实测用户勾中的会话就在这里，工具却报「找不到这个转录」。
        for name, rel in (
            ("Claude Code", ".claude/projects"),
            ("Codex", ".codex/sessions"),
            ("Codex", ".codex/archived_sessions"),
        ):
            add(name, home / rel)
    return roots


def norm_path(s: str) -> str:
    """比较路径时不在乎斜杠方向与盘符大小写。"""
    return re.sub(r"[\\/]+", "/", str(s)).rstrip("/").lower()


def agent_for(path: Path | str) -> str:
    """这份转录属于哪个 APP。**唯一的判定处。**

    判据按可靠性排序：
      1. 文件名前缀 —— Codex 的 rollout 有 `rollout-<时间戳>-` 前缀，
         Claude 的是裸 UUID。这是两边各自的命名契约。
      2. 路径里的数据目录名 —— 文件被拷到别处时前缀可能被改，
         但 `.codex` / `.claude` 这一段通常还在。

    为什么必须收拢成一个函数：认错的后果是**续接命令给错**——对 Claude 会话
    发 `codex resume` 必然失败，而用户拿到一条注定不工作的命令，会以为是自己
    环境的问题。此前三处各写一份判断（`handoff.py`、`portable.py`、
    `transcript.py`），其中两处只看前缀、一处还看 `.codex`，迟早分叉。
    """
    p = Path(path)
    name = p.name.lower()
    if name.startswith("rollout-"):
        return "Codex"
    parts = {x.lower() for x in p.parts}
    if ".codex" in parts or "archived_sessions" in parts:
        return "Codex"
    if ".claude" in parts:
        return "Claude Code"
    # 认不出时按 Claude 走：它的解析器对未知结构更宽容（逐行读 message），
    # 而 `read_turns` 认不出内容还会再试另一个解析器。
    return "Claude Code"


def agent_evidence(path: Path | str) -> str:
    """凭什么判定是这个 APP。给人看的一句话。

    为什么要把依据摆出来：卡片上会同时出现「这个会话是谁写的」与「这个会话在
    谈论谁」，而后者常常是另一个 APP 的东西——一个 Claude Code 会话完全可以
    整篇在分析某个 Codex 会话，它的开场提问里就有 `codex://threads/...`。
    读者看到那个链接会以为标注错了。

    实测本机就发生过：会话 `081c400c` 由 Claude Code 写（转录在
    `~/.claude/projects/`、客户端 2.1.241），内容却是让它去排查一个 Codex
    会话的报错，用户因此认定「明明用 codex 跑的，为什么标成 Claude Code」。

    判定本身没错，错在没把依据说出来。这一行让读者能自己核实，
    而不是在两个互相矛盾的信号之间猜。

    返回的是**语言中性的路径片段**（`~/.claude/projects/`、`rollout-*`），
    不是一句中文说明：这个模块不该知道界面语言，而路径本身各语言读者都认得。
    """
    p = Path(path)
    if p.name.lower().startswith("rollout-"):
        return "rollout-*"
    parts = {x.lower() for x in p.parts}
    if "archived_sessions" in parts:
        return "~/.codex/archived_sessions/"
    if ".codex" in parts:
        return "~/.codex/"
    if ".claude" in parts:
        return "~/.claude/projects/"
    return ""


def split_multi(text: str) -> list[str]:
    """把一段「多个值」的输入拆成去重后的列表。

    命令行与网页界面共用同一份实现：`--find a,b` 与在网页搜索框里粘
    `a, b` 必须给出相同结果，两处各写一份迟早会在细节上分叉。

    分隔符含全角逗号、顿号与空白：中文输入法下敲出来的是 `，` 和 `、`，
    而从别处复制一串会话 ID 时最常见的分隔就是换行或空格。只认半角逗号
    会让「粘进来一串 ID」变成一个找不到的长字符串。

    顺带去掉包裹的引号——shell 里带空格的路径常被整段引起来。
    """
    out: list[str] = []
    for part in re.split(r"[,，、;；\s]+", str(text or "")):
        cleaned = part.strip().strip('"').strip("'")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def nearest_repo(start: str) -> str:
    """从一个目录往上走，直到看见 .git。都没有就返回 ''。"""
    try:
        p = Path(start).resolve()
    except (OSError, ValueError):
        return ""
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return str(cand)
    return ""


# 「另一台电脑上的家目录」的结构特征。用结构而不是猜用户名：
# 硬编码用户名列表在换机器时必然失效，而 `<盘符>:\Users\<任意名>` 与
# `/home/<任意名>` 这两种形状在所有 Windows / Linux 机器上都成立。
# 用于两件事：判断转录是不是本机产生的，以及脱敏时连别人的用户名一起处理。
FOREIGN_HOME_RX = re.compile(
    r"(?:^|(?<=[\s\"'`(=]))"
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/home/|/Users/|/mnt/[a-z]/Users/)"
    r"([^\\/\s\"'`,;:)]+)",
    re.I,
)


def local_home_names() -> set[str]:
    """本机家目录的名字。用来把「别人的用户名」与「我自己的」区分开。

    返回集合而不是单个字符串：WSL 里 `~` 是 Linux 侧的名字，而转录可能来自
    Windows 侧，两个名字都算本机。
    """
    names: set[str] = set()
    try:
        home = Path(os.path.expanduser("~"))
    except (OSError, RuntimeError):
        return names
    if home.name:
        names.add(home.name.lower())
    # WSL 下 /mnt/c/Users/<name> 也是「本机」，名字可能与 Linux 侧不同。
    for env in ("USERNAME", "USER", "LOGNAME"):
        val = (os.environ.get(env) or "").strip()
        if val:
            names.add(val.lower())
    return names


def is_foreign_path(raw: str) -> bool:
    """这个路径确定属于另一台电脑吗？

    只认一条强判据：**路径形如某个家目录，而其中的用户名不是本机的**。
    不猜用户名清单（那换台机器就失效），靠 `FOREIGN_HOME_RX` 的结构匹配。

    刻意**不**把「绝对路径但不存在」算进来，那是弱信号：本机删掉一个项目目录
    是常事，而这个判断会关掉原生续接——那个会话在应用索引里还在，续接本来
    是可用的，误判等于把能用的功能关掉。路径失效那种情况用 `path_is_stale`
    单独表达，两者后果不同：一个说「续接不了」，一个只说「这条路径打不开」。
    """
    raw = (raw or "").strip()
    if not raw:
        return False
    m = FOREIGN_HOME_RX.search(raw)
    if not m:
        return False
    return m.group(1).lower() not in local_home_names()


def path_is_stale(raw: str) -> bool:
    """这条路径在本机打不开吗？

    与 `is_foreign_path` 分开：本机删掉的目录也会命中这里，但那**不**意味着
    会话不能续接。用来提示「照这条路径找不到东西」，不用来决定续接可行性。

    只对绝对路径成立——相对片段无从判断，报了只会给正常会话加噪声。
    """
    raw = (raw or "").strip()
    if not raw or not re.match(r"^(?:[A-Za-z]:[\\/]|[\\/])", raw):
        return False
    try:
        return not Path(raw).exists()
    except (OSError, ValueError):
        return False


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """把字节写进 path，要么全写进去，要么原文件一个字节不动。

    为什么交接文件需要这个：`write_bytes` 是「截断到 0，然后写」。在这两步之间
    读它的人看到的是空文件或半截文档——而这个工具的整个用途就是「上一个会话
    死了，把现场交给下一个」。现场文档自己在交付途中被截断，是最不能接受的
    失败方式。触发条件也不稀奇：磁盘满、进程被杀、另一个会话同时在跑同一条
    命令（并发检测只警告不阻断，所以这是允许发生的）。

    做法是业界标准的 temp + rename：
      1. 同目录建唯一临时文件（必须同目录——跨文件系统的 rename 不是原子的）
      2. 写完 flush + fsync，落到盘上，不只是落到页缓存
      3. `os.replace` 原子替换目标；POSIX 与 Windows 都保证这一步不会留下
         「半个文件」
      4. POSIX 上再 fsync 父目录，让目录项本身也落盘——否则崩溃后可能出现
         「文件内容在、但目录里还指向旧的」。Windows 没有目录 fd 的概念，
         跳过这一步（`os.replace` 在 NTFS 上本身走事务性元数据更新）。

    临时名带 pid 与计数器：同一秒里两个进程各写一份时不会撞车。撞车时
    `O_EXCL` 会失败而不是覆盖，重试若干次；都失败就把异常抛出去——写不成
    必须让调用方知道，静默失败比崩掉更坏。

    失败路径一律删掉临时文件，不在用户仓库里留垃圾。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    last_err: OSError | None = None
    for attempt in range(16):
        tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{attempt}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            last_err = exc
            continue
        except OSError as exc:
            last_err = exc
            break
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        _fsync_dir(path.parent)
        return
    # 16 次都没拿到一个可用的临时名：不正常，交给调用方而不是假装写成了。
    raise last_err if last_err is not None else OSError(f"cannot create temp file next to {path}")


def _fsync_dir(directory: Path) -> None:
    """让目录项落盘。Windows 上没有这个概念，静默跳过。

    目录 fsync 失败不该让一次成功的写变成失败：内容已经在盘上了，最坏情况是
    崩溃后目录项还指着旧文件——比「文档没写出来」轻得多。
    """
    if IS_WINDOWS:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# 转录文件的两种扩展名。
#
# Codex 会把**7 天前**的 rollout 原地 zstd 压缩成 `.jsonl.zst`（level 3，压缩
# 期间用 `rollout-compression.lock` 加锁）。只认 `.jsonl` 的后果不是少读几行，
# 而是「一周以前的 Codex 会话在这个工具里完全不存在」——而且随时间推移越来越
# 严重，因为每天都有新的会话跨过 7 天线。这是沉默失效：列表看起来正常，只是
# 短了一截，用户没有任何线索知道自己漏掉了什么。
TRANSCRIPT_SUFFIXES = (".jsonl", ".jsonl.zst")


def is_transcript_name(name: str) -> bool:
    """文件名看起来是转录吗？只看名字，不碰磁盘。

    用在「决定读哪些文件」之前的目录遍历里，所以必须便宜。
    """
    low = name.lower()
    return low.endswith(".jsonl") or low.endswith(".jsonl.zst")


def is_compressed_transcript(path: Path | str) -> bool:
    """这份转录是压缩过的吗？"""
    return str(path).lower().endswith(".zst")


def zstd_opener():
    """返回一个能打开 `.jsonl.zst` 的函数，没有可用实现时返回 None。

    本项目**不引入运行时依赖**（见 pyproject.toml 里的说明：工具运行在刚崩掉的
    环境里，任何需要 pip install 的东西都是一条失败路径）。所以这里只**利用**
    恰好已经装好的实现，按可信度顺序尝试：

      1. `compression.zstd` —— Python 3.14 起的标准库，最可靠
      2. `zstandard` —— 事实标准的第三方绑定
      3. `pyzstd` —— 另一个常见实现

    三个都没有时返回 None。**调用方必须降级而不是报错**：会话照旧出现在列表里，
    只是标注「已压缩归档，读不到正文」。这比假装它不存在强得多——用户至少知道
    那里有东西，也知道装什么能读到它。
    """
    try:  # 1. 标准库（3.14+）
        from compression import zstd as _czstd

        return lambda p: _czstd.open(p, "rt", encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:  # 2. zstandard
        import io

        import zstandard as _zstd

        def _open_zstandard(p):
            # 这里不能用 `with`：句柄要活到调用方关闭外层 TextIOWrapper 为止。
            # 提前关掉底层文件会让解压流在第一次读取时就失败。
            fh = open(p, "rb")  # noqa: SIM115
            reader = _zstd.ZstdDecompressor().stream_reader(fh)
            return io.TextIOWrapper(reader, encoding="utf-8", errors="replace")

        return _open_zstandard
    except Exception:
        pass
    try:  # 3. pyzstd
        import pyzstd as _pyzstd

        return lambda p: _pyzstd.open(p, "rt", encoding="utf-8", errors="replace")
    except Exception:
        pass
    return None


def open_transcript(path: Path):
    """打开一份转录用于逐行读取，压缩与未压缩一视同仁。

    压缩转录且没有 zstd 实现时抛 `TranscriptCompressedError`，让调用方决定
    怎么降级——不同调用方的降级方式不一样（扫描要标注，渲染要提示装什么）。
    """
    if is_compressed_transcript(path):
        opener = zstd_opener()
        if opener is None:
            raise TranscriptCompressedError(str(path))
        return opener(path)
    return path.open("r", encoding="utf-8", errors="replace")


class TranscriptCompressedError(RuntimeError):
    """转录是 zstd 压缩的，而本机没有任何可用的 zstd 实现。

    单独一个类型而不是复用 OSError：调用方要区分「文件读不了」（真的坏了）和
    「文件好着但缺解压能力」（装个包就能读），两者给用户的话完全不同。
    """


# 从文件尾部往前读多少字节去找最后一条记录的时间戳。
#
# 为什么要尾读而不是整份解析：一份转录可以有 79 MB（本机实测最大值），而
# 「最后活动是什么时候」只需要最后一行。实测本机 63 份 Claude 转录：8 KB
# 尾读只有 57 份命中，32 KB 全部命中（63/63）；Codex 的 rollout 同样在这个
# 窗口内稳定命中。取 32 KB 而不是更小，是因为单条记录可以很长——本机实测
# 单行最大 3.4 MB（一次大文件读取的结果），窗口太小会整窗都落在一行中间。
TAIL_PROBE_BYTES = 32768
# 首窗没命中时加倍重试的上限。超过这个量级还找不到时间戳，那份文件的形状
# 已经不是「JSONL 每行带时间戳」了，继续加倍只是把整个文件读进来。
TAIL_PROBE_MAX = 524288
# 顶层时间戳。Claude 写 `"timestamp":"2026-…"`，Codex 的 RolloutLine 信封
# 同样是顶层 `timestamp` —— 两边同一套取法。
#
# 用正则在文本上找而不是逐行 json.loads：尾读窗口的第一行几乎总是残行，
# json.loads 会在它上面抛异常；而这里要的只是一个 ISO 时间串，找到最后一个
# 就够。
#
# 刻意**不**限定「只认信封层」：有些记录整条没有信封 timestamp，时间只写在
# 内层（实测 Claude 的 `isSnapshotUpdate` 快照记录就是这样，最后一行的
# 信封无 timestamp、内层有）。只认信封会把这类记录当不存在，报出更早的时间。
# 实测 463 份转录：按信封逐行解析与本函数的结果有 462 份完全一致，
# 唯一那份差 2.2 秒——差的正是最后那条快照记录，本函数报的那个更接近事实。
_TS_RX = re.compile(r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2}[T ][^"]{4,32})"')


def last_record_time(path: Path) -> tuple[float, str]:
    """转录里**最后一条记录**的时间。返回 `(unix 秒, 来源)`。

    为什么不能用文件 mtime：mtime 会被任何后续触碰推后或提前，与「最后一条
    记录什么时候写的」大面积脱钩。本机实测——

      · Claude 侧 63 份转录：22 份偏差超过 60 秒，10 份超过 1 小时，
        3 份超过 24 小时，最差 241243 秒（约 2.8 天）。子代理边车写入、
        云同步、备份程序都会把 mtime 推后。
      · Codex 侧 324 份 rollout 更糟：287 份的 mtime **早于**最后一条记录
        （中位 −222 秒，最差 −15228 秒），而其中 268 份的 mtime 与文件名里
        的会话**开始**时间相差不到 2 秒——Codex 的 mtime 实质是创建时刻，
        跟最后活动无关。

    影响不止显示：mtime 同时是排序键与「最差会话」推荐的输入。实测 492 份
    会话按两种口径排序，位次相同的只有 122 份，最大偏移 93 位——一个 2.8 天
    的偏差足以把真正该交接的会话排到看不见的地方。

    来源字符串有三种，界面要如实说出是哪一种：
      · `record`            —— 从转录正文读到的，可信
      · `mtime`             —— 尾读没找到时间戳，回落文件时间
      · `mtime-compressed`  —— 压缩转录不能 seek 尾部，直接回落

    压缩转录刻意不解压：为一个时间戳把整条 zstd 流解一遍，代价与收益完全
    不成比例（本机压缩转录数为 0，这条路径优先保证不劣化）。
    """
    try:
        st = path.stat()
    except OSError:
        return 0.0, "mtime"

    if is_compressed_transcript(path):
        return st.st_mtime, "mtime-compressed"

    size = st.st_size
    if size <= 0:
        return st.st_mtime, "mtime"

    window = TAIL_PROBE_BYTES
    while True:
        start = max(0, size - window)
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read(size - start)
        except OSError:
            return st.st_mtime, "mtime"
        text = chunk.decode("utf-8", errors="replace")
        # 窗口不是从文件头开始时，第一行必然是残行——丢掉它。不丢的话，
        # 一条被切成两半的记录可能刚好露出内部某个更早的时间戳。
        if start > 0:
            cut = text.find("\n")
            text = text[cut + 1:] if cut >= 0 else ""
        hits = _TS_RX.findall(text)
        if hits:
            ts = _parse_iso(hits[-1])
            if ts is not None:
                return ts, "record"
        if start == 0 or window >= TAIL_PROBE_MAX:
            return st.st_mtime, "mtime"
        window *= 2


def _parse_iso(raw: str) -> float | None:
    """把 ISO 8601 时间串转成 unix 秒。转不了返回 None。

    转录里的写法不止一种：Claude 写 `2026-08-25T10:32:21.504Z`，Codex 写
    带偏移的形态，也见过用空格代替 `T` 的。`Z` 要换成 `+00:00`——
    Python 3.9/3.10 的 `fromisoformat` 不认 `Z`，而本项目最低支持 3.9。

    没有时区信息时按**本地时间**解释：那与 `datetime.fromtimestamp` 的既有
    行为一致，换成 UTC 会让同一份转录在不同口径下差出几个小时。
    """
    txt = raw.strip().replace(" ", "T")
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        # 小数秒位数不是 3 或 6 时 3.9/3.10 的 fromisoformat 会拒绝。
        # 砍掉小数秒再试一次：秒级精度对「最后活动」完全够用。
        base = re.sub(r"\.\d+", "", txt)
        try:
            dt = datetime.fromisoformat(base)
        except ValueError:
            return None
    try:
        return dt.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


# 图文说明的文件名。历史上还叫过 `使用说明.html`，留着兼容旧检出。
_GUIDE_NAMES = ("guide.html", "使用说明.html")


def find_guide() -> Path | None:
    """找到图文说明，找不到返回 None。

    为什么要一个共用函数：这个文件此前在两处各自推断路径——`gui/server.py`
    从自己往上数四层，`menu.py` 数三层（两者深度不同，所以层数本来就该不同，
    各自都对）。但两处都只认「源码检出」这一种布局，于是 `pip install` 之后
    都找不到它：`docs/` 从来没有进过 wheel，往上数几层都是 `site-packages`。

    找的顺序，从最可靠到最兜底：
      1. 包内的 `gui/static/guide.html` —— 装进 wheel 的那一份，随包走，
         `pip install` 之后唯一还在的副本
      2. 源码检出的 `<repo>/docs/guide.html` —— 开发时用的那份，也是
         `build_guide.py` 实际生成的位置
      3. `AGENT_HANDOFF_HOME` 指向的检出 —— 用户显式声明工具装在哪

    返回 `None` 时调用方要降级（网页界面隐藏入口、菜单打印「找不到」），
    不能报错：图文说明缺失不影响工具的任何实际功能。
    """
    here = Path(__file__).resolve().parent

    # 1. 包内副本（wheel 场景）。
    for name in _GUIDE_NAMES:
        cand = here / "gui" / "static" / name
        if cand.is_file():
            return cand

    # 2. 源码检出。`platform.py` 在 `<repo>/src/agent_handoff/` 下，
    #    所以 `<repo>` 是往上两层。
    repo = here.parent.parent
    for name in _GUIDE_NAMES:
        for cand in (repo / "docs" / name, repo / name):
            if cand.is_file():
                return cand

    # 3. 用户声明的检出位置。
    declared = os.environ.get("AGENT_HANDOFF_HOME", "").strip()
    if declared:
        try:
            base = Path(declared).expanduser()
        except (OSError, ValueError):
            return None
        for name in _GUIDE_NAMES:
            for cand in (base / "docs" / name, base / name):
                try:
                    if cand.is_file():
                        return cand
                except OSError:
                    continue
    return None
