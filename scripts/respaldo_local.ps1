# Respaldo local de la base (pg_dump) hacia otra unidad. 2026-11-10.
#
# Lo lanza el Programador de tareas (ver scripts\instalar_respaldo_local.ps1) o se corre a mano:
#     powershell -ExecutionPolicy Bypass -File scripts\respaldo_local.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\respaldo_local.ps1 -Destino "F:\Otra" -Conservar 12
#
# Que hace (solo LEE la base; no la modifica ni detiene nada, se puede correr con el scheduler encendido):
#   1. Se asegura de que Docker este listo y de que el contenedor de PostgreSQL este corriendo.
#   2. Comprueba que la unidad de destino exista (si F: no esta conectada, falla y avisa).
#   3. pg_dump -Fc dentro del contenedor a /tmp, y docker cp al destino con extension .partial.
#   4. Compara el SHA-256 del contenedor con el del archivo copiado y comprueba que pg_restore
#      pueda leerlo (--list). SOLO si todo coincide renombra el .partial a .dump.
#   5. Borra el temporal del contenedor y rota: conserva los N respaldos mas recientes
#      (solo archivos crypto_trading_*.dump de la carpeta de destino).
# Si algo falla avisa UNA vez por Telegram (usa el .env solo para leer el token, como el vigia),
# no deja un .dump a medias y termina con codigo 1. Registro: logs\respaldo_local_<fecha>.log.
# La marca de exito queda en logs\respaldo_local_ultimo_ok.txt.
# Restaurar: ver README ("Respaldo local") y scripts\probar_restauracion.ps1.
# Solo ASCII a proposito.

param(
    [string]$Destino = "F:\Respaldos_Trading",
    [int]$Conservar = 8
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location -Path (Join-Path $PSScriptRoot "..")

$logDir = "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("respaldo_local_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$contenedor = "crypto_trading_postgres"
$script:parcial = $null
$script:temporal = $null

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

function Limpiar-Temporales {
    # Nunca deja un .partial en el destino ni el temporal dentro del contenedor.
    if ($script:parcial -and (Test-Path $script:parcial)) {
        Remove-Item -Path $script:parcial -Force -ErrorAction SilentlyContinue
    }
    if ($script:temporal) {
        Invoke-Native { docker exec $contenedor rm -f $script:temporal 2>$null } | Out-Null
    }
}

function Fallar([string]$motivo) {
    Limpiar-Temporales
    Write-Log "=== RESPALDO LOCAL FALLIDO: $motivo ==="
    Send-Aviso "ALERTA respaldo local: el respaldo de la base fallo ($motivo). Revisa la carpeta logs\ (respaldo_local_*.log)."
    exit 1
}

$dockerDesktopExe = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"

try {
    Write-Log "=== Respaldo local hacia $Destino (conservar $Conservar) ==="

    if ($Conservar -lt 1) { Fallar "el parametro -Conservar debe ser 1 o mayor" }

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

    $corriendo = Invoke-Native { docker inspect -f "{{.State.Running}}" $contenedor 2>$null }
    if ("$corriendo".Trim() -ne "true") { Fallar "el contenedor $contenedor no esta corriendo" }

    if ($Destino -notmatch '^[A-Za-z]:\\') { Fallar "el destino debe empezar por una letra de unidad (por ejemplo F:\...)" }
    $unidad = $Destino.Substring(0, 2)
    if (-not (Test-Path ($unidad + "\"))) {
        Fallar "la unidad de destino ($unidad) no esta disponible"
    }
    New-Item -ItemType Directory -Force -Path $Destino | Out-Null

    $nombre = "crypto_trading_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".dump"
    $script:temporal = "/tmp/$nombre"
    $final = Join-Path $Destino $nombre
    $script:parcial = $final + ".partial"
    if (Test-Path $final) { Fallar "ya existe $nombre en el destino (se reintento en el mismo minuto)" }

    Write-Log "En ejecucion: pg_dump -Fc (solo lectura) ..."
    Invoke-Native { docker exec $contenedor pg_dump -U crypto -d crypto_trading -Fc -f $script:temporal 2>&1 } | Write-TeeUtf8
    if ($LASTEXITCODE -ne 0) { Fallar "pg_dump termino con codigo $LASTEXITCODE" }

    Write-Log "Copiando al destino ..."
    Invoke-Native { docker cp "${contenedor}:$($script:temporal)" $script:parcial 2>&1 } | Write-TeeUtf8
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $script:parcial)) { Fallar "docker cp no pudo copiar el archivo" }

    $salidaHash = Invoke-Native { docker exec $contenedor sha256sum $script:temporal 2>$null } | Select-Object -First 1
    $hashContenedor = "$salidaHash".Split(" ")[0].Trim().ToLower()
    $hashCopia = (Get-FileHash -Path $script:parcial -Algorithm SHA256).Hash.ToLower()
    if ([string]::IsNullOrWhiteSpace($hashContenedor) -or ($hashContenedor -ne $hashCopia)) {
        Fallar "el SHA-256 de la copia no coincide con el del original"
    }
    Write-Log "SHA-256 verificado: $hashCopia"

    $toc = Invoke-Native { docker exec $contenedor pg_restore --list $script:temporal 2>&1 }
    $codigoToc = $LASTEXITCODE
    $tocOk = @($toc | Where-Object { "$_" -match "TOC Entries" }).Count -gt 0
    if ($codigoToc -ne 0 -or -not $tocOk) { Fallar "pg_restore no pudo leer el respaldo (--list)" }

    Rename-Item -Path $script:parcial -NewName $nombre
    $script:parcial = $null
    $tamanoMb = [math]::Round((Get-Item $final).Length / 1MB, 1)
    Write-Log "Respaldo guardado: $final ($tamanoMb MB)"

    Invoke-Native { docker exec $contenedor rm -f $script:temporal 2>$null } | Out-Null
    $script:temporal = $null

    # Rotacion: solo archivos crypto_trading_*.dump (el nombre lleva fecha y hora: orden por nombre = cronologico)
    $viejos = Get-ChildItem -Path $Destino -Filter "crypto_trading_*.dump" | Sort-Object Name -Descending | Select-Object -Skip $Conservar
    foreach ($v in $viejos) {
        Remove-Item -Path $v.FullName -Force
        Write-Log "Rotacion: eliminado $($v.Name)"
    }

    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $nombre" | Out-File -FilePath (Join-Path $logDir "respaldo_local_ultimo_ok.txt") -Encoding ascii
    Write-Log "=== RESPALDO LOCAL TERMINADO CON EXITO (codigo 0) ==="
    exit 0
}
catch {
    Fallar "error no controlado ($($_.Exception.GetType().Name))"
}
