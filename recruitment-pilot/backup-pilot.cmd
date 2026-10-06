@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0backup-pilot.ps1"
set "taskBackupExit=%ERRORLEVEL%"
pause
exit /b %taskBackupExit%
