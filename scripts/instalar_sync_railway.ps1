# Deja programada la sincronizacion diaria con Railway. 2026-10-15.
#
# Hace dos cosas, una sola vez:
#   1. Te pide la contrasena de la base de Railway (usuario crypto) y la guarda CIFRADA con tu
#      usuario de Windows (DPAPI) en %LOCALAPPDATA%\CryptoTradingPlatform\railway_db.cred. Ese
#      archivo esta fuera del proyecto (nunca llega a Git) y solo tu usuario, en este equipo,
#      puede descifrarlo. La contrasena no se escribe en la tarea ni en ningun registro.
#   2. Registra la tarea "CryptoTradingPlatform_SyncRailway": cada dia a las 20:00 (hora de
#      Windows), sin ventana, ejecutando scripts\sync_railway.ps1 (via sync_railway_oculto.vbs).
#
# Uso (en una consola PowerShell, en la carpeta del proyecto):
#     powershell -ExecutionPolicy Bypass -File scripts\instalar_sync_railway.ps1
#
# Opciones:  -Hora 20:00   -SoloContrasena (renueva la contrasena sin tocar la tarea)   -Quitar
# Es idempotente. Al terminar corre una SIMULACION (no escribe en Railway) para comprobar la
# conexion. Si dice que las migraciones son distintas, migra Railway una vez con:
#     powershell -ExecutionPolicy Bypass -File scripts\sync_railway.ps1 -MigrarRailway
# Solo ASCII a proposito.

param(
    [string]$TaskName = "CryptoTradingPlatform_SyncRailway",
    [string]$Hora = "20:00",
    [switch]$SoloContrasena,
    [switch]$Quitar
)

$ErrorActionPreference = "Stop"
$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$lanzador = Join-Path $proj "scripts\sync_railway_oculto.vbs"
$sync = Join-Path $proj "scripts\sync_railway.ps1"
$carpetaCred = Join-Path $env:LOCALAPPDATA "CryptoTradingPlatform"
$archivoCred = Join-Path $carpetaCred "railway_db.cred"

if (-not (Test-Path $lanzador) -or -not (Test-Path $sync)) {
    throw "Faltan scripts\sync_railway.ps1 o scripts\sync_railway_oculto.vbs en $proj"
}

$existente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Quitar) {
    if ($existente) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    Write-Output "Listo: la tarea '$TaskName' ya no esta registrada. (La contrasena cifrada sigue en $archivoCred; borra ese archivo si no la necesitas.)"
    return
}

# 1) Contrasena cifrada con DPAPI
if ($SoloContrasena -or -not (Test-Path $archivoCred)) {
    $pw = Read-Host "Contrasena de la base de Railway (usuario crypto)" -AsSecureString
    if ($pw.Length -eq 0) { throw "La contrasena esta vacia." }
    New-Item -ItemType Directory -Force -Path $carpetaCred | Out-Null
    $pw | ConvertFrom-SecureString | Out-File -FilePath $archivoCred -Encoding ascii
    Write-Output "Contrasena guardada cifrada en $archivoCred (solo tu usuario puede descifrarla)."
} else {
    Write-Output "Ya hay una contrasena guardada ($archivoCred); se conserva. Usa -SoloContrasena para cambiarla."
}
if ($SoloContrasena) { return }

# 2) Tarea programada
if ($existente) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Tarea anterior '$TaskName' eliminada."
}
$accion = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$lanzador`"" -WorkingDirectory $proj
$disparador = New-ScheduledTaskTrigger -Daily -At $Hora
$ajustes = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$usuario = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $accion -Trigger $disparador `
    -Principal $principal -Settings $ajustes `
    -Description "Sincroniza la base local con Railway (respaldo), sin ventana" | Out-Null
Write-Output "Listo: '$TaskName' corre cada dia a las $Hora, sin ventana."

# 3) Comprobacion: simulacion (no escribe en Railway)
Write-Output "Comprobando la conexion con una simulacion (no escribe nada en Railway) ..."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sync -Simulacion
Write-Output ""
Write-Output "Proxima ejecucion:  (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime"
Write-Output "Ultimo resultado:   (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult   (0 = bien)"
Write-Output "Ultimo exito:       Get-Content logs\sync_railway_ultimo_ok.txt"
