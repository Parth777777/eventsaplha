@echo off
REM EventAlpha Backend — auto-restart wrapper.
REM Restarts api.py if it crashes; ~10 crashes/hour kills the watchdog.

setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Prefer venv python, then PATH python.
set PY=
if exist "..\.venv\Scripts\python.exe" set PY=..\.venv\Scripts\python.exe
if "%PY%"=="" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Error: no Python found in ..\.venv or PATH
        exit /b 1
    )
    set PY=python
)
echo Using Python: %PY%

if not exist .deps_installed (
    echo Installing dependencies...
    %PY% -m pip install -r requirements.txt
    type nul > .deps_installed
)

if not exist ..\logs mkdir ..\logs

set BACKOFF=2
set CRASHES=0

:LOOP
echo [%date% %time%] Starting API server on http://localhost:5000
%PY% api.py >> ..\logs\backend.log 2>&1
set RC=%errorlevel%
echo [%date% %time%] api.py exited rc=%RC%

set /a CRASHES=CRASHES+1
if !CRASHES! GTR 10 (
    echo [%date% %time%] FATAL: api.py crashed too many times. Stopping.
    exit /b 1
)

echo [%date% %time%] Restarting in %BACKOFF%s (crash %CRASHES% of 10)...
timeout /t %BACKOFF% /nobreak >nul
set /a BACKOFF=BACKOFF*2
if %BACKOFF% GTR 60 set BACKOFF=60
goto LOOP
