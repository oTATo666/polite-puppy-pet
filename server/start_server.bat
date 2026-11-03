@echo off
rem ============================================================
rem PolitePuppy server-mode launcher
rem Use this as the run command in BT panel process guard / NSSM.
rem All runtime config lives in app\.env (loaded by main.py).
rem ============================================================
cd /d "%~dp0app"

set "PYTHON_CMD="
where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD where py >nul 2>nul && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    echo [ERROR] Python not found. Install Python 3.10+ and tick "Add to PATH".
    exit /b 1
)

echo [PolitePuppy] starting server with %PYTHON_CMD% ...
%PYTHON_CMD% main.py --server
