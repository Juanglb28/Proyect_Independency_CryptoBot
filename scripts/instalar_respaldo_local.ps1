# Deja programado el respaldo local semanal de la base. 2026-11-10.
#
# Registra la tarea "CryptoTradingPlatform_RespaldoLocal": una vez por semana (por defecto el
# domingo a las 21:00, hora de Windows), sin ventana, ejecutando scripts\respaldo_local.ps1
# (via respaldo_local_oculto.vbs). Si el equipo estaba apagado a esa hora, se pone al dia en
# cuanto Windows pueda (StartWhenAvailable). No guarda contrasenas: pg_dump corre DENTRO del
# contenedor de PostgreSQL con su usuario local.
#
# Uso (en una consola PowerShell, en la carpeta del proyecto):
#     powershell -ExecutionPolicy Bypass -File scripts\instalar_respaldo_local.ps1
#
# Opciones:  -Dia Sunday   -Hora 21:00   -Quitar
# Es idempotente. NO corre un respaldo al terminar: para probarlo ahora,
#     powershell -ExecutionPolicy Bypass -File scripts\respaldo_local.ps1
# Solo ASCII a proposito.

param(
    [string]$TaskName = "CryptoTradingPlatform_RespaldoLocal",
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$Dia = "Sunday",
    [string]$Hora = "21:00",
    [switch]$Quitar
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$lanzador = Join-Path $proj "scripts\respaldo_local_oculto.vbs"
$respaldo = Join-Path $proj "scripts\respaldo_local.ps1"

if (-not (Test-Path $lanzador) -or -not (Test-Path $respaldo)) {
    throw "Faltan scripts\respaldo_local.ps1 o scripts\respaldo_local_oculto.vbs en $proj"
}

$existente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Quitar) {
    if ($existente) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    Write-Output "Listo: la tarea '$TaskName' ya no esta registrada. (Los respaldos ya guardados no se tocan.)"
    return
}

if ($existente) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Tarea anterior '$TaskName' eliminada."
}
$accion = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$lanzador`"" -WorkingDirectory $proj
$disparador = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Dia -At $Hora
# StartWhenAvailable: si el equipo estaba apagado a la hora, corre en cuanto Windows pueda. Es seguro:
# el respaldo solo lee la base y una sola instancia a la vez.
$ajustes = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$usuario = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $accion -Trigger $disparador `
    -Principal $principal -Settings $ajustes `
    -Description "Respaldo semanal (pg_dump) de la base local hacia otra unidad, sin ventana" | Out-Null
Write-Output "Listo: '$TaskName' corre cada $Dia a las $Hora, sin ventana."
Write-Output ""
Write-Output "Probar ahora:       powershell -ExecutionPolicy Bypass -File scripts\respaldo_local.ps1"
Write-Output "Proxima ejecucion:  (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime"
Write-Output "Ultimo resultado:   (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult   (0 = bien)"
Write-Output "Ultimo exito:       Get-Content logs\respaldo_local_ultimo_ok.txt"
