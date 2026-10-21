@echo off
REM ============================================================================
REM  WUP Grid Explorer - double-click to run.
REM  First run: creates a private Python environment in .venv and installs the
REM  explorer's packages (a few minutes, needs internet). Later runs start in
REM  seconds. Everything you compute or save goes to WUP_Explorer_data\ here.
REM  Options are passed through, e.g.:  WUP_Explorer.bat --source synthetic
REM  (If you have WUP_Explorer.exe you do not need Python at all.)
REM ============================================================================
setlocal
cd /d "%~dp0"
title WUP Grid Explorer

set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" goto :deps

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
echo Creating a private Python environment in .venv (first run only) ...
%PY% -m venv .venv
if errorlevel 1 goto :nopython

:deps
REM (re)install only when requirements-explorer.txt changed since the last install
fc /b requirements-explorer.txt .venv\requirements.stamp >nul 2>nul
if not errorlevel 1 goto :run
echo Installing the explorer's packages (first run, or the requirements changed) ...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
set "CONSTR="
if exist requirements-tested.txt set "CONSTR=-c requirements-tested.txt"
"%VPY%" -m pip install --disable-pip-version-check -r requirements-explorer.txt %CONSTR%
if errorlevel 1 if defined CONSTR (
  echo The tested package versions are not available for this Python; installing the latest compatible ones ...
  "%VPY%" -m pip install --disable-pip-version-check -r requirements-explorer.txt
)
if errorlevel 1 goto :pipfail
"%VPY%" -m pip install --disable-pip-version-check -q --no-deps -e .
if errorlevel 1 goto :pipfail
copy /y requirements-explorer.txt .venv\requirements.stamp >nul

:run
"%VPY%" -m aorc_tools.explorer %*
if errorlevel 1 pause
exit /b

:nopython
echo.
echo Python 3.10 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo or use WUP_Explorer.exe, which needs no Python.
pause
exit /b 1

:pipfail
echo.
echo Package installation failed (no internet, or a proxy?). Fix that and double-click again;
echo nothing is lost. Delete the .venv folder to start the environment from scratch.
pause
exit /b 1
