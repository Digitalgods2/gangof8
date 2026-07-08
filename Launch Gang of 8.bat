@echo off
title Gang of 8 launcher
cd /d "%~dp0"

REM ---- backend: "cli" = real local agents (costs tokens) | "mock" = free/offline
set "BACKEND=cli"

REM ---- use the project venv python if present, else system python
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Gang of 8 dashboard (backend: %BACKEND%) ...
echo Only one instance can use port 8790 at a time.
echo.

REM start the server in its own window (close that window to stop it)
start "Gang of 8 server" "%PY%" cli.py serve --backend %BACKEND%

REM give the server a moment to come up, then open the dashboard
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8790/"

echo Dashboard: http://127.0.0.1:8790/
echo (To run free/offline, change BACKEND to "mock" at the top of this file.)
timeout /t 4 /nobreak >nul
