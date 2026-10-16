' Lanzador sin ventana de la tanda nocturna de K1. 2026-10-15.
'
' wscript.exe no crea consola y Run(..., 0, True) arranca PowerShell oculto y espera a que
' termine, devolviendo su codigo (asi Get-ScheduledTaskInfo muestra en LastTaskResult si
' la tanda fallo). Argumento opcional: minutos maximos de la tanda (por defecto 480).
' Lo registra scripts\instalar_k1_noche.ps1. Solo ASCII a proposito.

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
carpetaScripts = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = fso.GetParentFolderName(carpetaScripts)
minutos = 480
If WScript.Arguments.Count > 0 Then minutos = CInt(WScript.Arguments(0))
codigo = shell.Run("powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & carpetaScripts & "\k1_generar_noche.ps1"" -MaxMinutos " & minutos, 0, True)
WScript.Quit codigo
