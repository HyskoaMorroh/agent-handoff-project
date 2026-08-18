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

exec "$PY" -m agent_handoff.cli "$@"
