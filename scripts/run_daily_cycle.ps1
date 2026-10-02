# Wrapper para el Programador de tareas de Windows (Task Scheduler).
#
# Por qué existe: el ciclo diario (scripts/run_daily_cycle.py) es un script
# 100% determinista en su orquestación — no necesita que ninguna IA lo
# arranque ni lo supervise (principio P2 del plan). Registrarlo en el
# Programador de tareas nativo de Windows, en vez de depender de una tarea
# programada de un asistente de IA, evita que la ejecución diaria dependa de
# que una sesión de Claude esté abierta y vinculada a esta máquina a esa
# hora exacta.
#
# Qué hace: se asegura de que Docker Desktop esté corriendo (lo levanta y
# espera si hace falta), levanta los contenedores y corre
# scripts.run_daily_pipeline dentro del contenedor `app` (ingesta
# incremental + indicadores + el ciclo real, en ese orden) — ver
# docker-compose.yml y README.md, sección "Ejecutar comandos dentro de
# Docker" (necesario en esta máquina por el bloqueo de Smart App Control
# con las extensiones compiladas nativas de Windows, ver Evento 3077 de
# CodeIntegrity).
#
# Uso manual (recomendado para probar, en vez de `schtasks /run`, que es
# "fire and forget": lanza la tarea en un proceso aparte y la consola donde
# escribiste el comando nunca ve su salida — ver BUG REAL 2026-10-02 (b)
# más abajo):
#     powershell -File scripts\run_daily_cycle.ps1
# Registro en Task Scheduler: ver README.md, sección
# "Tarea diaria programada (Windows)".

$ErrorActionPreference = "Stop"

# La raíz del proyecto es el directorio padre de scripts\ — así el script
# funciona sin importar desde dónde lo invoque el Programador de tareas.
Set-Location -Path (Join-Path $PSScriptRoot "..")

$logDir = "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("daily_cycle_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

# BUG REAL (2026-10-02, b): el script solo escribía al archivo de log, nunca
# a la consola — pensado para la tarea programada desatendida, donde no hay
# nadie mirando. Pero al probarlo a mano (`schtasks /run` o corriendo este
# script directo) la consola se quedaba sin ningún indicio de si seguía
# corriendo, si terminó bien o si falló, obligando a abrir una segunda
# ventana para seguir el log con `Get-Content -Wait`. Ahora Write-Log
# también imprime por consola (Write-Host) — sigue escribiendo al archivo
# igual que antes, para la tarea programada sin nadie mirando.
function Write-Log {
    param([string]$Message)
    $linea = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    $linea | Out-File -FilePath $logFile -Append -Encoding utf8
    Write-Host $linea
}

# BUG REAL (2026-10-02): `docker compose` escribe su progreso normal
# ("Container ... Running", "Creating", etc.) por stderr, no por stdout.
# Con $ErrorActionPreference = "Stop" activo, Windows PowerShell convierte
# CUALQUIER línea de stderr de un comando nativo en un error terminante —
# el script se cortaba a los dos segundos de arrancar `docker compose up -d`
# aunque el comando en sí no hubiera fallado, confundiendo progreso normal
# con una excepción real. Por eso todo comando nativo (docker, no cmdlets de
# PowerShell) corre con $ErrorActionPreference = "Continue" y se valida a
# mano con $LASTEXITCODE, que es la forma correcta de detectar un fallo real
# de un ejecutable externo en PowerShell.
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

# Docker Desktop puede instalarse a nivel de máquina (Program Files) o por
# usuario (AppData\Local\Programs) — en esta máquina es una instalación por
# usuario, confirmada en la práctica (2026-10-01): "C:\Program Files\
# Docker\Docker\Docker Desktop.exe" no existía; el ejecutable real estaba en
# "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe". Se usa la
# variable de entorno en vez de la ruta fija con el usuario escrito a mano,
# para que no dependa de "juang" específicamente. Si tu instalación es
# distinta, ajusta esta línea (o habilita en Docker Desktop, Settings ->
# General, "Start Docker Desktop when you sign in" para no depender nunca
# de que este script lo lance).
$dockerDesktopExe = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"

function Test-DockerReady {
    Invoke-Native { docker info *> $null }
    return ($LASTEXITCODE -eq 0)
}

try {
    Write-Log "=== Iniciando ejecucion del ciclo diario ==="

    # BUG REAL (2026-10-01): la tarea programada se disparó a las 10:00 sin
    # que Docker Desktop estuviera abierto todavía (el usuario lo arranca
    # manualmente al sentarse a trabajar). `docker compose` falló de
    # inmediato, y ese tipo de error concreto no quedaba capturado por la
    # redirección de streams del script original — el log quedaba en 0
    # bytes, sin ninguna pista. Por eso ahora se verifica explícitamente
    # antes de intentar nada, y además todo el script queda envuelto en
    # try/catch con su propio log (Write-Log), independiente de que la
    # redirección de streams capture o no un error puntual.
    if (-not (Test-DockerReady)) {
        Write-Log "Docker no responde. Intentando iniciar Docker Desktop ($dockerDesktopExe)."

        if (-not (Test-Path $dockerDesktopExe)) {
            Write-Log "No se encontro Docker Desktop en la ruta esperada. Abortando sin correr el ciclo."
            Write-Log "=== PROCESO FINALIZADO CON ERROR (codigo 1) ==="
            exit 1
        }

        Start-Process -FilePath $dockerDesktopExe

        $maxWaitSeconds = 120
        $elapsed = 0
        $ready = $false
        while (-not $ready -and $elapsed -lt $maxWaitSeconds) {
            Start-Sleep -Seconds 5
            $elapsed += 5
            $ready = Test-DockerReady
        }

        if (-not $ready) {
            Write-Log "Docker Desktop no respondio tras $maxWaitSeconds segundos. Abortando sin correr el ciclo."
            Write-Log "=== PROCESO FINALIZADO CON ERROR (codigo 1) ==="
            exit 1
        }
        Write-Log "Docker Desktop listo tras $elapsed segundos de espera."
    } else {
        Write-Log "Docker ya estaba listo."
    }

    Write-Log "En ejecucion: docker compose up -d ..."
    # 2>&1 + Tee-Object en vez de `*>> $logFile`: lo de antes solo mandaba la
    # salida al archivo, nunca a la consola (mismo BUG REAL de arriba, b).
    # Tee-Object manda cada línea a AMBOS lados sin perder la detección de
    # fallo real: Tee-Object es un cmdlet de PowerShell, no un ejecutable
    # nativo, así que no toca $LASTEXITCODE — sigue siendo el código de
    # salida real de `docker`, el mismo que ya se valida a mano de abajo.
    Invoke-Native { docker compose up -d 2>&1 | Tee-Object -FilePath $logFile -Append }
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose up -d salio con codigo $LASTEXITCODE"
    }

    # scripts.run_daily_pipeline encadena ingesta incremental de velas +
    # recálculo de indicadores + el ciclo real (en ese orden) — agregado
    # tras detectar en producción (2026-10-01/02) que el ciclo llevaba dos
    # días corriendo sobre velas con más de 50 horas de antigüedad, porque
    # nada mantenía `velas`/`indicadores` al día automáticamente.
    Write-Log "En ejecucion: scripts.run_daily_pipeline (ingesta + indicadores + ciclo) ..."
    Invoke-Native { docker compose run --rm app python -m scripts.run_daily_pipeline 2>&1 | Tee-Object -FilePath $logFile -Append }
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        Write-Log "=== PROCESO FINALIZADO CON EXITO (codigo 0) ==="
    } else {
        Write-Log "=== PROCESO FINALIZADO CON ERROR (codigo $exitCode) ==="
    }
    exit $exitCode
}
catch {
    # Cualquier error no previsto (incluyendo uno que la redirección de
    # streams no hubiera capturado) queda explícitamente en el log, nunca
    # en silencio.
    Write-Log "ERROR no controlado: $($_.Exception.Message)"
    Write-Log ($_.ScriptStackTrace)
    Write-Log "=== PROCESO FINALIZADO CON ERROR (codigo 1) ==="
    exit 1
}
