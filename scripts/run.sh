#!/usr/bin/env sh
# Interactive menu launcher for Linux / macOS.
# The Windows equivalent is 双击运行.cmd.
set -eu

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/.." && pwd)
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
# 与 agent-handoff.sh 同一套检查：没有解释器或版本过旧时给可操作的提示，
# 而不是让用户面对 "command not found" 或包内部的 SyntaxError 堆栈。
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "agent-handoff: 需要 Python 3.9 或更新版本，但没找到解释器。" >&2
  echo "  Debian/Ubuntu: sudo apt install python3" >&2
  echo "  macOS:         brew install python" >&2
  exit 127
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "agent-handoff: 需要 Python 3.9 或更新版本，当前解释器过旧：" >&2
  "$PY" -c 'import sys; print("  " + sys.version)' >&2 || true
  exit 1
fi

exec "$PY" -m agent_handoff.menu
