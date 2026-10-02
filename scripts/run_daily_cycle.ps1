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
# espera si hace falta), levanta los contenedores y corre el ciclo dentro
# del contenedor `app` — ver docker-compose.yml y README.md, sección
# "Ejecutar comandos dentro de Docker" (necesario en esta máquina por el
# bloqueo de Smart App Control con las extensiones compiladas nativas de
# Windows, ver Evento 3077 de CodeIntegrity).
#
# Uso manual: powershell -File scripts\run_daily_cycle.ps1
# Registro en Task Scheduler: ver README.md, sección
# "Tarea diaria programada (Windows)".

$ErrorActionPreference = "Stop"

# La raíz del proyecto es el directorio padre de scripts\ — así el script
# funciona sin importar desde dónde lo invoque el Programador de tareas.
Set-Location -Path (Join-Path $PSScriptRoot "..")

$logDir = "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("daily_cycle_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log {
    param([string]$Message)
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" | Out-File -FilePath $logFile -Append -Encoding utf8
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
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

try {
    Write-Log "Iniciando ejecucion del ciclo diario."

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
            exit 1
        }
        Write-Log "Docker Desktop listo tras $elapsed segundos de espera."
    } else {
        Write-Log "Docker ya estaba listo."
    }

    docker compose up -d *>> $logFile
    docker compose run --rm app python -m scripts.run_daily_cycle *>> $logFile
    $exitCode = $LASTEXITCODE

    Write-Log "Ejecucion finalizada con codigo de salida $exitCode."
    exit $exitCode
}
catch {
    # Cualquier error no previsto (incluyendo uno que la redirección de
    # streams no hubiera capturado) queda explícitamente en el log, nunca
    # en silencio.
    Write-Log "ERROR no controlado: $($_.Exception.Message)"
    Write-Log ($_.ScriptStackTrace)
    exit 1
}
