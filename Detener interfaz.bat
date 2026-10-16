@echo off
rem Detiene la interfaz local (no toca la base de datos ni el scheduler). Ver scripts\detener_interfaz.ps1
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\detener_interfaz.ps1"
echo.
pause
