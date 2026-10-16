# Registra (o vuelve a registrar) la tarea "CryptoTradingPlatform_K1_Noche" del Programador
# de tareas de Windows: una tanda de la generacion de K1 de lunes a viernes a las 00:00,
# sin ventana. 2026-10-15.
#
# Uso (una sola vez, en una consola PowerShell, en la carpeta del proyecto):
#     powershell -ExecutionPolicy Bypass -File scripts\instalar_k1_noche.ps1
#
# Opciones:  -Hora 00:00   -MaxMinutos 480   -Quitar
# Para quitarla (cuando la generacion este completa):
#     powershell -ExecutionPolicy Bypass -File scripts\instalar_k1_noche.ps1 -Quitar
#
# Detalles a proposito:
#  - NO usa "ejecutar lo antes posible si se perdio el inicio": si el equipo estaba apagado
#    a las 00:00, esa noche se omite en lugar de arrancar a media manana mientras trabajas.
#  - Una instancia a la vez; tope de 10 h por si algo se cuelga.
#  - Corre con tu sesion de Windows iniciada (puede estar bloqueada). El equipo debe estar
#    encendido y con Docker Desktop instalado; la tanda lo abre si hace falta.
#  - Es idempotente: si la tarea existe, la reemplaza. Solo ASCII a proposito.

param(
    [string]$TaskName = "CryptoTradingPlatform_K1_Noche",
    [string]$Hora = "00:00",
    [int]$MaxMinutos = 480,
    [switch]$Quitar
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$lanzador = Join-Path $proj "scripts\k1_noche_oculto.vbs"
if (-not (Test-Path $lanzador)) {
    throw "No se encontro $lanzador"
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

$accion = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "//B //Nologo `"$lanzador`" $MaxMinutos" -WorkingDirectory $proj
$disparador = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Hora
$ajustes = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 10)
$usuario = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $accion -Trigger $disparador `
    -Principal $principal -Settings $ajustes `
    -Description "Tanda nocturna de la generacion de K1 (Kronos), lunes a viernes, sin ventana" | Out-Null

Write-Output "Listo: '$TaskName' corre de lunes a viernes a las $Hora (maximo $MaxMinutos min por tanda), sin ventana."
Write-Output "Proxima ejecucion:  (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime"
Write-Output "Ultimo resultado:   (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult   (0 = bien)"
Write-Output "Registro de cada tanda: carpeta logs\ (archivos kronos_k1_*.log)"
