@echo off
rem Launcher for the interactive menu. ASCII only on purpose:
rem non-ASCII comments in a .cmd break under a GBK console.
chcp 65001 >nul 2>&1
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HERE=%~dp0"
set "ROOT=%HERE%.."

set "PY=python"
where python >nul 2>&1 || set "PY=py -3"
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
%PY% -m agent_handoff.menu
if errorlevel 1 pause
