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

exec "$PY" -m agent_handoff.menu
