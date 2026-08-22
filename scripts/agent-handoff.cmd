@echo off
rem agent-handoff - session handoff generator (Windows wrapper)
rem Usage: agent-handoff [repo-path] [options]
rem   agent-handoff .
rem   agent-handoff E:\output\myproj --skip-tests
rem
rem ASCII only on purpose: non-ASCII comments in a .cmd break under a GBK console.
rem Switch the console to UTF-8 first: the tool prints CJK in two of its three
rem languages, and a default GBK console renders that as mojibake.
rem
rem Nothing here is tied to one machine or user name: the checkout is found from
rem this script's own location (%~dp0), and AGENT_HANDOFF_HOME can override that.
rem So the file survives being copied to another computer unchanged.
chcp 65001 >nul 2>&1
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem An explicit checkout wins over everything: useful right after moving to a new
rem machine, or when several versions are kept side by side.
set "ROOT="
if defined AGENT_HANDOFF_HOME if exist "%AGENT_HANDOFF_HOME%\src\agent_handoff\cli.py" set "ROOT=%AGENT_HANDOFF_HOME%"
if not defined ROOT set "ROOT=%~dp0.."

rem Run the checkout this script belongs to, not some other copy. Delegating to
rem whatever `agent-handoff` is on PATH looks tidier but is wrong twice over:
rem it can find a *different* checkout (or an old wrapper) and silently run that
rem instead of the one the user just cd'd into, and if that target is itself a
rem .cmd, the nested call makes the exit code unreliable.
rem An installed console script is still useful -- but then the user types
rem `agent-handoff`, not this file.

rem Prefer the checkout's own virtualenv: that is where the package is installed,
rem and it keeps the run independent of whatever Python happens to be on PATH.
if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
  "%ROOT%\.venv\Scripts\python.exe" -m agent_handoff.cli %*
  goto :done
)

rem Probe by running, not by `where`: the Microsoft Store ships a python.exe stub
rem that answers `where` but only opens the Store.
set "PY="
python -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  echo Python 3.9 or newer is required, and none was found on PATH.
  echo Install it from https://www.python.org/downloads/ and tick
  echo "Add python.exe to PATH". The Microsoft Store stub does not count.
  exit /b 127
)

set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
%PY% -m agent_handoff.cli %*

:done
rem Read the level outside any parenthesised block: cmd expands %errorlevel% at
rem parse time, so reading it inside a block yields the value from before it ran.
exit /b %errorlevel%
