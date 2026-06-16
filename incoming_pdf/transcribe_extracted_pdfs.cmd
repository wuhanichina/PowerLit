@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run_powerlit_command.ps1" -CommandName transcribe-extracted-pdfs %*
exit /b %ERRORLEVEL%
