@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    set "RPP_PYTHON=python"
) else (
    set "RPP_PYTHON=py -3"
)
%RPP_PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)"
if errorlevel 1 (
    echo Install Python 3.12 or newer, then run this file again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    %RPP_PYTHON% -m venv .venv
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" run.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo Startup failed. See the message above.
pause
exit /b 1
