#!/usr/bin/env sh
# agent-handoff - session handoff generator (POSIX wrapper)
# Usage: ./agent-handoff.sh [repo-path] [options]
#   ./agent-handoff.sh .
#   ./agent-handoff.sh ~/proj/myapp --skip-tests
#
# Mirrors agent-handoff.cmd: same env vars, same fallback order.
#
# Nothing here is tied to one machine or user name, so this file survives being
# copied to another computer: the checkout is found from this script's own
# location, and AGENT_HANDOFF_HOME can override it.
set -eu

# 与 Windows 包装器保持同样的两个环境变量。Python 3.7+ 认 PYTHONUTF8；
# PYTHONIOENCODING 兼顾更老的解释器和被重定向的管道。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# 显式指定的检出优先于一切：迁移到新机器、或同时留着多个版本时用它。
if [ -n "${AGENT_HANDOFF_HOME:-}" ] && [ -f "$AGENT_HANDOFF_HOME/src/agent_handoff/cli.py" ]; then
  ROOT=$AGENT_HANDOFF_HOME
else
  # 按这个脚本自己的位置推断检出根，不写死任何用户名或盘符。
  #
  # 不去 exec PATH 上的 `agent-handoff`：那看着更整洁，其实错两次——它可能指向
  # **另一个**检出（或一个旧包装器），于是用户刚 cd 进来的这份代码被静默绕过；
  # 而目标若是 shell 包装器，嵌套调用还会让退出码变得不可靠。
  # 装过 console script 的人直接敲 `agent-handoff` 就是了，不必经过本文件。
  HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
  ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# 检出自带的 venv 优先：包就装在那里。找不到才退回 PATH 上的解释器。
for cand in "$ROOT/.venv/bin/python" "$ROOT/.venv/bin/python3" "$ROOT/.venv/Scripts/python.exe"; do
  if [ -x "$cand" ]; then
    exec "$cand" -m agent_handoff.cli "$@"
  fi
done

# python3 优先：某些发行版的 `python` 仍是 2.x。
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
# 一个都没有时给出可操作的提示，而不是让 shell 报 "command not found"。
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "agent-handoff: 需要 Python 3.9 或更新版本，但没找到解释器。" >&2
  echo "  Debian/Ubuntu: sudo apt install python3" >&2
  echo "  macOS:         brew install python" >&2
  exit 127
fi
# 版本太旧时也早退：否则用户看到的是包内部抛出的 SyntaxError 堆栈。
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "agent-handoff: 需要 Python 3.9 或更新版本，当前解释器过旧：" >&2
  "$PY" -c 'import sys; print("  " + sys.version)' >&2 || true
  exit 1
fi

exec "$PY" -m agent_handoff.cli "$@"
