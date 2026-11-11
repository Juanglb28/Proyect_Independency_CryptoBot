' Lanzador sin ventana del respaldo local semanal. 2026-11-10.
'
' wscript.exe no crea consola y Run(..., 0, True) arranca PowerShell oculto y espera a que
' termine, devolviendo su codigo (asi Get-ScheduledTaskInfo muestra en LastTaskResult si fallo).
' Lo registra scripts\instalar_respaldo_local.ps1. Solo ASCII a proposito.

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
carpetaScripts = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = fso.GetParentFolderName(carpetaScripts)
codigo = shell.Run("powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & carpetaScripts & "\respaldo_local.ps1""", 0, True)
WScript.Quit codigo
