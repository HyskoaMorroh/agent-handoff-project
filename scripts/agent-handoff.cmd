@echo off
rem agent-handoff - session handoff generator (Windows wrapper)
rem Usage: agent-handoff [repo-path] [options]
rem   agent-handoff .
rem   agent-handoff E:\output\myproj --skip-tests
rem
rem ASCII only on purpose: non-ASCII comments in a .cmd break under a GBK console.
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

set "PY=python"
where python >nul 2>&1 || set "PY=py -3"
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
%PY% -m agent_handoff.cli %*
exit /b %errorlevel%
