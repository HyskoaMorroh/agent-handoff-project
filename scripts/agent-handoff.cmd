@echo off
rem agent-handoff - session handoff generator (Windows wrapper)
rem Usage: agent-handoff [repo-path] [options]
rem   agent-handoff .
rem   agent-handoff E:\output\myproj --skip-tests
rem
rem ASCII only on purpose: non-ASCII comments in a .cmd break under a GBK console.
rem Switch the console to UTF-8 first: the tool prints CJK in two of its three
rem languages, and a default GBK console renders that as mojibake.
chcp 65001 >nul 2>&1
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HERE=%~dp0"
set "ROOT=%HERE%.."

rem Prefer an installed console script; fall back to running from source.
where agent-handoff >nul 2>&1
if %errorlevel%==0 (
  agent-handoff %*
  exit /b %errorlevel%
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
exit /b %errorlevel%
