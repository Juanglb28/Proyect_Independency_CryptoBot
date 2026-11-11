# Prueba que un respaldo (pg_dump) realmente se puede RESTAURAR. 2026-11-10.
#
#     powershell -ExecutionPolicy Bypass -File scripts\probar_restauracion.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\probar_restauracion.ps1 -Archivo "F:\Respaldos_Trading\crypto_trading_20261110_2148.dump"
#
# Que hace: restaura el respaldo (por defecto el mas reciente de la carpeta de destino) en una base
# TEMPORAL llamada restore_test, dentro del mismo contenedor de PostgreSQL, y compara las filas de
# cada tabla con las de la base viva. La base real (crypto_trading) solo se LEE (conteos); nunca se
# escribe en ella. Al terminar borra restore_test y el archivo temporal del contenedor.
#
# Receta de TimescaleDB: CREATE EXTENSION -> timescaledb_pre_restore() -> pg_restore ->
# timescaledb_post_restore().
#
# Criterio (el respaldo es de un instante pasado y la base viva sigue escribiendo, asi que las
# tablas que crecen pueden tener MAS filas en la viva): FALLA si una tabla falta en la restaurada,
# si la restaurada tiene MAS filas que la viva, o si esta vacia cuando la viva no lo esta; tambien
# si el numero de hipertablas difiere. Las diferencias menores a favor de la viva se muestran y se
# aceptan. Registro: logs\probar_restauracion_<fecha>.log. Solo ASCII a proposito.

param(
    [string]$Archivo = "",
    [string]$Destino = "F:\Respaldos_Trading"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location -Path (Join-Path $PSScriptRoot "..")

$logDir = "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("probar_restauracion_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$contenedor = "crypto_trading_postgres"
$bdViva = "crypto_trading"
$bdPrueba = "restore_test"
$temporal = "/tmp/restore_test.dump"

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

# Ejecuta una orden SQL y devuelve la salida (stdout) para leer resultados.
function Invoke-PsqlLeer([string]$bd, [string]$sql) {
    $salida = Invoke-Native { docker exec $contenedor psql -U crypto -d $bd -v ON_ERROR_STOP=1 -At -F "|" -c $sql 2>$null }
    if ($LASTEXITCODE -ne 0) { throw "psql fallo en $bd" }
    return $salida
}

# Ejecuta una orden SQL y copia lo que diga al registro.
function Invoke-PsqlOrden([string]$bd, [string]$sql) {
    Invoke-Native { docker exec $contenedor psql -U crypto -d $bd -v ON_ERROR_STOP=1 -c $sql 2>&1 } | Write-TeeUtf8
    if ($LASTEXITCODE -ne 0) { throw "psql fallo en $bd" }
}

function Limpiar-Prueba {
    # Solo toca la base temporal restore_test y el archivo temporal del contenedor.
    try {
        Invoke-Native { docker exec $contenedor psql -U crypto -d postgres -c "DROP DATABASE IF EXISTS $bdPrueba WITH (FORCE);" 2>&1 } | Out-Null
        Invoke-Native { docker exec $contenedor rm -f $temporal 2>$null } | Out-Null
    } catch {
        Write-Log "No se pudo limpiar del todo ($($_.Exception.GetType().Name)); revisa que no quede la base $bdPrueba."
    }
}

function Terminar([string]$motivo) {
    Limpiar-Prueba
    Write-Log "=== PRUEBA DE RESTAURACION FALLIDA: $motivo ==="
    exit 1
}

$sqlConteos = "select table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1;"
$sqlHiper = "select count(*) from timescaledb_information.hypertables;"

function Get-Conteos([string]$bd) {
    $h = @{}
    foreach ($linea in (Invoke-PsqlLeer $bd $sqlConteos)) {
        $p = "$linea".Split("|")
        if ($p.Count -eq 2) { $h[$p[0]] = [int64]$p[1] }
    }
    return $h
}

$dockerDesktopExe = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"

try {
    Write-Log "=== Prueba de restauracion en la base temporal $bdPrueba ==="
    if ($bdPrueba -eq $bdViva) { Terminar "la base de prueba no puede ser la base viva" }

    if (-not (Test-DockerReady)) {
        Write-Log "Docker no responde. Intentando iniciar Docker Desktop."
        if (-not (Test-Path $dockerDesktopExe)) { Terminar "no se encontro Docker Desktop" }
        Start-Process -FilePath $dockerDesktopExe
        $elapsed = 0
        $ready = $false
        while (-not $ready -and $elapsed -lt 180) {
            Start-Sleep -Seconds 5
            $elapsed += 5
            $ready = Test-DockerReady
        }
        if (-not $ready) { Terminar "Docker Desktop no respondio tras 180 segundos" }
    }

    $corriendo = Invoke-Native { docker inspect -f "{{.State.Running}}" $contenedor 2>$null }
    if ("$corriendo".Trim() -ne "true") { Terminar "el contenedor $contenedor no esta corriendo" }

    if ([string]::IsNullOrWhiteSpace($Archivo)) {
        $ultimo = Get-ChildItem -Path $Destino -Filter "crypto_trading_*.dump" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $ultimo) { Terminar "no hay respaldos crypto_trading_*.dump en $Destino" }
        $Archivo = $ultimo.FullName
    }
    if (-not (Test-Path $Archivo)) { Terminar "no existe el archivo $Archivo" }
    Write-Log "Respaldo a probar: $Archivo"

    # Base temporal limpia
    Invoke-PsqlOrden "postgres" "DROP DATABASE IF EXISTS $bdPrueba WITH (FORCE);"
    Invoke-Native { docker cp $Archivo "${contenedor}:$temporal" 2>&1 } | Write-TeeUtf8
    if ($LASTEXITCODE -ne 0) { Terminar "docker cp no pudo copiar el respaldo al contenedor" }

    Invoke-PsqlOrden "postgres" "CREATE DATABASE $bdPrueba;"
    Invoke-PsqlOrden $bdPrueba "CREATE EXTENSION IF NOT EXISTS timescaledb;"
    Invoke-PsqlOrden $bdPrueba "SELECT timescaledb_pre_restore();"

    Write-Log "En ejecucion: pg_restore (puede tardar unos minutos) ..."
    $salidaRestore = Invoke-Native { docker exec $contenedor pg_restore -U crypto -d $bdPrueba --no-owner --no-privileges $temporal 2>&1 }
    $codigoRestore = $LASTEXITCODE
    $errores = @($salidaRestore | Where-Object { "$_" -match "error" })
    Write-Log "pg_restore termino con codigo $codigoRestore y $($errores.Count) linea(s) con 'error'."
    foreach ($e in ($errores | Select-Object -First 15)) { Write-Log "  $e" }

    Invoke-PsqlOrden $bdPrueba "SELECT timescaledb_post_restore();"

    Write-Log "Contando filas (base viva: solo lectura) ..."
    $viva = Get-Conteos $bdViva
    $restaurada = Get-Conteos $bdPrueba
    if ($viva.Count -eq 0) { Terminar "no se pudieron leer las tablas de la base viva" }

    $fallas = @()
    foreach ($tabla in ($viva.Keys | Sort-Object)) {
        $v = $viva[$tabla]
        if (-not $restaurada.ContainsKey($tabla)) {
            $fallas += "${tabla}: falta en la base restaurada"
            Write-Log ("{0,-34} viva={1,12} restaurada=FALTA" -f $tabla, $v)
            continue
        }
        $r = $restaurada[$tabla]
        $nota = ""
        if ($r -gt $v) { $fallas += "${tabla}: la restaurada tiene MAS filas ($r) que la viva ($v)"; $nota = "  <-- FALLA" }
        elseif ($r -eq 0 -and $v -gt 0) { $fallas += "${tabla}: restaurada vacia"; $nota = "  <-- FALLA" }
        elseif ($r -lt $v) { $nota = "  (la viva siguio escribiendo: +$($v - $r))" }
        Write-Log ("{0,-34} viva={1,12} restaurada={2,12}{3}" -f $tabla, $v, $r, $nota)
    }
    foreach ($tabla in $restaurada.Keys) {
        if (-not $viva.ContainsKey($tabla)) { $fallas += "${tabla}: sobra en la restaurada" }
    }

    $hiperViva = "$(Invoke-PsqlLeer $bdViva $sqlHiper)".Trim()
    $hiperRest = "$(Invoke-PsqlLeer $bdPrueba $sqlHiper)".Trim()
    Write-Log "Hipertablas: viva=$hiperViva restaurada=$hiperRest"
    if ($hiperViva -ne $hiperRest) { $fallas += "numero de hipertablas distinto ($hiperViva vs $hiperRest)" }

    Limpiar-Prueba

    if ($fallas.Count -gt 0) {
        foreach ($f in $fallas) { Write-Log "FALLA: $f" }
        Terminar "$($fallas.Count) comprobacion(es) no pasaron"
    }
    Write-Log "=== RESTAURACION VERIFICADA: todas las tablas e hipertablas coinciden (codigo 0) ==="
    exit 0
}
catch {
    Terminar "error no controlado ($($_.Exception.GetType().Name))"
}
