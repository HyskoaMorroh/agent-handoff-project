@echo off
rem Launcher for the interactive menu. ASCII only on purpose:
rem non-ASCII comments in a .cmd break under a GBK console.
chcp 65001 >nul 2>&1
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Quote the expansion: a clone under a path containing & or ^ breaks a bare set.
set "HERE=%~dp0"
set "ROOT=%HERE%.."
set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"

rem Find a usable interpreter. `where python` is not enough on Windows: the
rem Microsoft Store ships a stub named python.exe that only opens the Store,
rem and it answers `where` just fine. So probe by actually running it -- a real
rem interpreter prints its version and exits 0; the stub does not.
set "PY="
python -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(0)" >nul 2>&1 && set "PY=py -3"

if not defined PY (
  echo.
  echo   Python 3.9 or newer is required, and none was found.
  echo.
  echo   Install it from https://www.python.org/downloads/ and tick
  echo   "Add python.exe to PATH" in the installer.
  echo.
  echo   Note: the Microsoft Store stub does not count -- if typing `python`
  echo   opens the Store instead of a prompt, install from python.org instead.
  echo.
  pause
  exit /b 1
)

rem Refuse early on an interpreter too old to parse the source. Without this the
rem user gets a SyntaxError traceback from deep inside the package.
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   This tool needs Python 3.9 or newer. The interpreter found is older:
  %PY% -c "import sys; print('   ', sys.version)"
  echo.
  echo   Install a newer one from https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

%PY% -m agent_handoff.menu
rem Always pause. Exiting straight back to Explorer closes the window instantly,
rem so a run that finished normally leaves nothing on screen to read.
echo.
pause
