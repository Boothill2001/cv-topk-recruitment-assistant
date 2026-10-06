@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-pilot.ps1"
if errorlevel 1 pause
