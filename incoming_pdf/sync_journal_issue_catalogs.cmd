@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%incoming_pdf\run_powerlit_command.ps1" -CommandName sync-journal-issue-catalogs %*
exit /b %ERRORLEVEL%
