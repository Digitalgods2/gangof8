@echo off
REM Stop whatever is serving the Gang of 8 dashboard on port 8790.
title Stop Gang of 8
set "found="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8790 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
    set "found=1"
)
if defined found (echo Gang of 8 stopped.) else (echo Nothing was running on port 8790.)
timeout /t 2 /nobreak >nul
