@echo off
rem Abre la interfaz local del proyecto (doble clic). Ver scripts\abrir_interfaz.ps1
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\abrir_interfaz.ps1"
if errorlevel 1 (
  echo.
  echo No se pudo abrir la interfaz. Revisa los mensajes de arriba.
  pause
)
