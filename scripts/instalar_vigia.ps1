# Registra (o vuelve a registrar) la tarea "VigiaScheduler" del Programador de
# tareas de Windows para que el vigia local corra cada 5 minutos SIN MOSTRAR
# NINGUNA VENTANA. 2026-10-12.
#
# Por que existe: lanzar powershell.exe desde una tarea normal abre una consola
# que parpadea cada vez, aunque se use -WindowStyle Hidden (la ventana se crea
# antes de poder ocultarla). Dos cosas lo evitan de verdad:
#   1. Registrar la tarea con -LogonType S4U ("ejecutar tanto si el usuario inicio
#      sesion como si no"): Windows la corre en segundo plano, sin consola y sin
#      guardar tu contrasena.
#   2. Ademas, -WindowStyle Hidden como segunda barrera.
#
# Uso (una sola vez, en una consola PowerShell; si Windows pide permisos, abrela
# como administrador):
#     powershell -ExecutionPolicy Bypass -File scripts\instalar_vigia.ps1
#
# Para quitarla:  powershell -ExecutionPolicy Bypass -File scripts\instalar_vigia.ps1 -Quitar
#
# Es idempotente: si la tarea ya existe la reemplaza (asi se corrige una tarea
# creada antes con ventana visible). Solo ASCII a proposito, como los otros .ps1.

param(
    [string]$TaskName = "VigiaScheduler",
    [int]$CadaMinutos = 5,
    [switch]$Quitar
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$vigia = Join-Path $proj "scripts\scheduler_watchdog.ps1"

if (-not (Test-Path $vigia)) {
    throw "No se encontro $vigia"
}

$existente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existente) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Tarea anterior '$TaskName' eliminada."
}
if ($Quitar) {
    Write-Output "Listo: '$TaskName' ya no esta registrada."
    return
}

$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$vigia`"" `
    -WorkingDirectory $proj
$disparador = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $CadaMinutos)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
# Si una corrida sigue viva, la siguiente no se apila; si el equipo estaba
# dormido, se pone al dia al despertar; y nunca queda colgada mas de 2 minutos.
$ajustes = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $accion -Trigger $disparador `
    -Principal $principal -Settings $ajustes `
    -Description "Avisa por Telegram si el scheduler de trading deja de dar senales (sin ventana)" | Out-Null

Write-Output "Listo: '$TaskName' corre cada $CadaMinutos min en segundo plano, sin ventana."
Write-Output "Comprobar:  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
