# Vigia local del scheduler (fuera de Docker). 2026-10-12.
#
# Que hace: lee el archivo de latido (.scheduler_heartbeat.json) que el
# scheduler escribe cada 5 minutos y, si tiene mas de 15 minutos (o no existe),
# avisa UNA vez por Telegram que el scheduler no da senales. Cuando el latido
# vuelve a estar fresco, avisa que se recupero. No vuelve a avisar en cada
# corrida: solo cuando el estado CAMBIA (usa un pequeno archivo de estado).
#
# Por que PowerShell y no Python: este vigia debe correr FUERA de Docker para
# detectar tambien que Docker se detuvo. Windows trae PowerShell; no hace falta
# instalar Python en el equipo. Corre todo el tiempo? No: lo lanza el
# Programador de tareas de Windows cada 5 minutos (ver README, seccion
# "Vigilancia del scheduler").
#
# Que NO cubre: que el computador este apagado o sin energia (entonces el
# Programador de tareas tampoco corre). Ese caso lo cubre el dead-man's-switch
# externo (SCHEDULER_DEADMAN_URL en .env).
#
# Uso: powershell -ExecutionPolicy Bypass -File scripts\scheduler_watchdog.ps1
#
# El umbral de 15 minutos es el mismo de la regla de dominio
# (app/domain/rules/heartbeat.py, UMBRAL_LATIDO_VENCIDO_MINUTOS). Si se cambia
# alla, cambiarlo aqui tambien.
#
# Este archivo es solo ASCII a proposito, como los otros .ps1 del proyecto.

param(
    [int]$StaleMinutes = 15,
    [string]$HeartbeatFile = ".scheduler_heartbeat.json",
    [string]$StateFile = ".scheduler_watchdog_state.json"
)

$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")

# --- Leer token y chat_id de Telegram desde .env (sin exponerlos) ---
function Read-DotEnvValue([string]$key) {
    if (-not (Test-Path ".env")) { return $null }
    foreach ($line in Get-Content ".env") {
        $t = $line.Trim()
        if ($t.StartsWith("#") -or (-not $t.Contains("="))) { continue }
        $i = $t.IndexOf("=")
        $k = $t.Substring(0, $i).Trim()
        if ($k -ieq $key) { return $t.Substring($i + 1).Trim().Trim('"') }
    }
    return $null
}

$token = Read-DotEnvValue "TELEGRAM_BOT_TOKEN"
$chatId = Read-DotEnvValue "TELEGRAM_CHAT_ID"

function Send-Telegram([string]$text) {
    if ([string]::IsNullOrWhiteSpace($token) -or [string]::IsNullOrWhiteSpace($chatId)) {
        # Sin Telegram configurado no hay a donde avisar; se registra y se sale.
        Write-Output "Watchdog: Telegram sin configurar; no se pudo avisar: $text"
        return
    }
    $uri = "https://api.telegram.org/bot$token/sendMessage"
    $body = @{ chat_id = $chatId; text = $text; disable_web_page_preview = $true } | ConvertTo-Json
    try {
        Invoke-RestMethod -Uri $uri -Method Post -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 15 | Out-Null
    } catch {
        # Nunca mostrar la excepcion cruda: la URL lleva el token.
        Write-Output "Watchdog: fallo el envio a Telegram ($($_.Exception.GetType().Name))"
    }
}

# --- Estado del latido: fresco o vencido ---
$now = (Get-Date).ToUniversalTime()
$vencido = $true
$detalle = "el archivo de latido no existe"

if (Test-Path $HeartbeatFile) {
    try {
        $hb = Get-Content $HeartbeatFile -Raw | ConvertFrom-Json
        if ($hb.timestamp) {
            $ts = ([datetimeoffset]::Parse($hb.timestamp)).UtcDateTime
            $edadMin = [math]::Round(($now - $ts).TotalMinutes, 1)
            if ($edadMin -le $StaleMinutes) {
                $vencido = $false
                $detalle = "ultimo latido hace $edadMin min"
            } else {
                $detalle = "ultimo latido hace $edadMin min (umbral $StaleMinutes)"
            }
        } else {
            $detalle = "el archivo de latido no tiene timestamp"
        }
    } catch {
        $detalle = "el archivo de latido no se pudo leer"
    }
}

$estadoActual = if ($vencido) { "down" } else { "alive" }

# --- Estado previo (para avisar solo en los cambios) ---
$estadoPrevio = $null
if (Test-Path $StateFile) {
    try { $estadoPrevio = (Get-Content $StateFile -Raw | ConvertFrom-Json).estado } catch { $estadoPrevio = $null }
}

# --- Decidir y avisar solo en la transicion ---
if ($estadoActual -eq "down" -and $estadoPrevio -ne "down") {
    Send-Telegram "ALERTA scheduler: no da senales de vida ($detalle). Revisa Docker en tu equipo."
} elseif ($estadoActual -eq "alive" -and $estadoPrevio -eq "down") {
    Send-Telegram "Scheduler recuperado: $detalle."
}

# --- Guardar el estado actual ---
try {
    @{ estado = $estadoActual; desde = $now.ToString("o") } | ConvertTo-Json | Set-Content $StateFile -Encoding UTF8
} catch {
    Write-Output "Watchdog: no se pudo guardar el estado"
}
