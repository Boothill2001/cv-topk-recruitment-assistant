@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check-pilot.ps1"
set "taskCheckExit=%ERRORLEVEL%"
pause
exit /b %taskCheckExit%
