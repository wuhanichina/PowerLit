@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%workflow_scheduler.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if /I not "%POWERLIT_NO_PAUSE%"=="1" (
  echo.
  pause
)

exit /b %EXIT_CODE%
