#!/usr/bin/env sh
# agent-handoff - session handoff generator (POSIX wrapper)
# Usage: ./agent-handoff.sh [repo-path] [options]
#   ./agent-handoff.sh .
#   ./agent-handoff.sh ~/proj/myapp --skip-tests
#
# Mirrors agent-handoff.cmd: same env vars, same fallback order.
set -eu

# 与 Windows 包装器保持同样的两个环境变量。Python 3.7+ 认 PYTHONUTF8；
# PYTHONIOENCODING 兼顾更老的解释器和被重定向的管道。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# 优先用已安装的 console script。
if command -v agent-handoff >/dev/null 2>&1; then
  exec agent-handoff "$@"
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

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
