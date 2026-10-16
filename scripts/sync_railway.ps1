# Sincroniza la base local con la de Railway (respaldo/espejo). 2026-10-15.
#
# Lo lanza el Programador de tareas (ver scripts\instalar_sync_railway.ps1) o se corre a mano:
#     powershell -ExecutionPolicy Bypass -File scripts\sync_railway.ps1                  (sincroniza)
#     powershell -ExecutionPolicy Bypass -File scripts\sync_railway.ps1 -Simulacion      (no escribe)
#     powershell -ExecutionPolicy Bypass -File scripts\sync_railway.ps1 -MigrarRailway   (alembic upgrade head en Railway)
#
# Que hace: se asegura de que Docker Desktop este listo, lee la contrasena de Railway del archivo
# CIFRADO con tu usuario de Windows (DPAPI) que creo scripts\instalar_sync_railway.ps1, arma las URL
# SOLO en memoria (variables de entorno de este proceso) y ejecuta scripts.copy_database dentro del
# contenedor. Esa herramienta es re-ejecutable, escribe todo en UNA transaccion y solo confirma si
# el destino queda como superconjunto del origen (ver README). Las dos bases deben estar en la misma
# migracion: si no, aborta sin escribir nada (usa -MigrarRailway para migrar Railway a mano).
#
# Seguridad: la contrasena nunca se escribe en el proyecto, ni en el registro, ni en la linea de
# comandos de la tarea; las URL nunca se imprimen. Si algo falla avisa UNA vez por Telegram (usa el
# .env solo para leer el token, igual que el vigia). Registro: logs\sync_railway_<fecha>.log.
# Solo ASCII a proposito.

param(
    [switch]$Simulacion,
    [switch]$MigrarRailway,
    [string]$Servidor = "iriguchi.proxy.rlwy.net:11001"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location -Path (Join-Path $PSScriptRoot "..")

$logDir = "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("sync_railway_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$archivoCred = Join-Path $env:LOCALAPPDATA "CryptoTradingPlatform\railway_db.cred"

function Write-Log {
    param([string]$Message)
    $linea = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    $linea | Out-File -FilePath $logFile -Append -Encoding utf8
    Write-Host $linea
}

function Write-TeeUtf8 {
    param([Parameter(ValueFromPipeline)][string]$Linea)
    process {
        Write-Host $Linea
        $Linea | Out-File -FilePath $logFile -Append -Encoding utf8
    }
}

function Invoke-Native {
    param([Parameter(Mandatory)][scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Test-DockerReady {
    Invoke-Native { docker info *> $null }
    return ($LASTEXITCODE -eq 0)
}

# --- Aviso por Telegram (mismo metodo que el vigia; nunca muestra el token) ---
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

function Send-Aviso([string]$texto) {
    $tk = Read-DotEnvValue "TELEGRAM_BOT_TOKEN"
    $chat = Read-DotEnvValue "TELEGRAM_CHAT_ID"
    if ([string]::IsNullOrWhiteSpace($tk) -or [string]::IsNullOrWhiteSpace($chat)) {
        Write-Log "Telegram sin configurar; no se pudo avisar."
        return
    }
    $body = @{ chat_id = $chat; text = $texto; disable_web_page_preview = $true } | ConvertTo-Json
    try {
        Invoke-RestMethod -Uri "https://api.telegram.org/bot$tk/sendMessage" -Method Post `
            -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 15 | Out-Null
    } catch {
        # Nunca mostrar la excepcion cruda: la URL lleva el token.
        Write-Log "Fallo el envio del aviso a Telegram ($($_.Exception.GetType().Name))."
    }
}

function Fallar([string]$motivo) {
    Write-Log "=== SINCRONIZACION CON RAILWAY FALLIDA: $motivo ==="
    Send-Aviso "ALERTA respaldo Railway: la sincronizacion fallo ($motivo). Revisa la carpeta logs\ (sync_railway_*.log)."
    exit 1
}

$dockerDesktopExe = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"
$modo = if ($MigrarRailway) { "migrar Railway" } elseif ($Simulacion) { "simulacion" } else { "sincronizar" }

try {
    Write-Log "=== Railway: $modo ==="

    if (-not (Test-DockerReady)) {
        Write-Log "Docker no responde. Intentando iniciar Docker Desktop."
        if (-not (Test-Path $dockerDesktopExe)) { Fallar "no se encontro Docker Desktop" }
        Start-Process -FilePath $dockerDesktopExe
        $elapsed = 0
        $ready = $false
        while (-not $ready -and $elapsed -lt 180) {
            Start-Sleep -Seconds 5
            $elapsed += 5
            $ready = Test-DockerReady
        }
        if (-not $ready) { Fallar "Docker Desktop no respondio tras 180 segundos" }
        Write-Log "Docker Desktop listo tras $elapsed segundos."
    }

    if (-not (Test-Path $archivoCred)) {
        Fallar "falta el archivo de credenciales (ejecuta scripts\instalar_sync_railway.ps1)"
    }
    try {
        $seguro = Get-Content -Path $archivoCred | ConvertTo-SecureString
        $clave = [System.Net.NetworkCredential]::new("", $seguro).Password
    } catch {
        Fallar "no se pudo descifrar la contrasena (cambio de usuario o de equipo; repite scripts\instalar_sync_railway.ps1)"
    }

    $claveUrl = [uri]::EscapeDataString($clave)
    $destino = "postgresql+psycopg://crypto:" + $claveUrl + "@" + $Servidor + "/crypto_trading?sslmode=verify-ca&sslrootcert=/app/docker/railway-db-server.crt"
    $clave = $null
    $claveUrl = $null

    $codigo = 1
    try {
        if ($MigrarRailway) {
            $env:DATABASE_URL = $destino
            Write-Log "En ejecucion: alembic upgrade head (en Railway) ..."
            Invoke-Native { docker compose run --rm -T -e DATABASE_URL app alembic upgrade head 2>&1 } | Write-TeeUtf8
            $codigo = $LASTEXITCODE
        } else {
            $env:SOURCE_DATABASE_URL = "postgresql+psycopg://crypto:crypto@postgres:5432/crypto_trading"
            $env:DEST_DATABASE_URL = $destino
            if ($Simulacion) {
                Write-Log "En ejecucion: scripts.copy_database (simulacion, no escribe) ..."
                Invoke-Native { docker compose run --rm -T -e SOURCE_DATABASE_URL -e DEST_DATABASE_URL app python -m scripts.copy_database 2>&1 } | Write-TeeUtf8
            } else {
                Write-Log "En ejecucion: scripts.copy_database --ejecutar ..."
                Invoke-Native { docker compose run --rm -T -e SOURCE_DATABASE_URL -e DEST_DATABASE_URL app python -m scripts.copy_database --ejecutar 2>&1 } | Write-TeeUtf8
            }
            $codigo = $LASTEXITCODE
        }
    }
    finally {
        Remove-Item Env:DATABASE_URL, Env:SOURCE_DATABASE_URL, Env:DEST_DATABASE_URL -ErrorAction SilentlyContinue
        $destino = $null
    }

    if ($codigo -ne 0) { Fallar "la herramienta termino con codigo $codigo" }

    if (-not $Simulacion -and -not $MigrarRailway) {
        (Get-Date -Format "yyyy-MM-dd HH:mm:ss") | Out-File -FilePath (Join-Path $logDir "sync_railway_ultimo_ok.txt") -Encoding ascii
    }
    Write-Log "=== RAILWAY: $modo TERMINADO CON EXITO (codigo 0) ==="
    exit 0
}
catch {
    Fallar "error no controlado ($($_.Exception.GetType().Name))"
}
