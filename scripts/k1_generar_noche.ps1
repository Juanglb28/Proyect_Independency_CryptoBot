# Una tanda NOCTURNA de la generacion de K1 (enmienda E6). 2026-10-15.
#
# Lo lanza el Programador de tareas de Windows (ver scripts\instalar_k1_noche.ps1),
# pero tambien se puede correr a mano:
#     powershell -ExecutionPolicy Bypass -File scripts\k1_generar_noche.ps1
#
# Que hace: se asegura de que Docker Desktop este listo, comprueba que NO haya ya un
# contenedor de Kronos corriendo (dos generadores a la vez no duplican datos, pero cada
# uno iria a la mitad de velocidad) y lanza una tanda de scripts.kronos_k1_generar con
# tope de tiempo (-MaxMinutos). La generacion es reanudable: lo ya guardado se salta, y
# cuando esta completa la tanda termina en segundos sin hacer nada.
#
# Mientras corre pide a Windows que no suspenda el equipo. La salida se guarda en
# logs\kronos_k1_<fecha>.log. No usa claves ni toca el scheduler, el monitor ni el ciclo
# diario: solo el contenedor aislado de Kronos (perfil kronos), que escribe unicamente en
# la tabla kronos_forecasts. Solo ASCII a proposito.

param(
    [int]$MaxMinutos = 480
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location -Path (Join-Path $PSScriptRoot "..")

$logDir = "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("kronos_k1_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

function Write-Log {
    param([string]$Message)
    $linea = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    $linea | Out-File -FilePath $logFile -Append -Encoding utf8
    Write-Host $linea
}

# Muestra y guarda cada linea en UTF-8 sin tocar $LASTEXITCODE (misma razon que en
# run_daily_cycle.ps1: Tee-Object escribiria en otra codificacion).
function Write-TeeUtf8 {
    param([Parameter(ValueFromPipeline)][string]$Linea)
    process {
        Write-Host $Linea
        $Linea | Out-File -FilePath $logFile -Append -Encoding utf8
    }
}

# docker escribe su progreso normal por stderr; con "Stop" PowerShell 5.1 lo tomaria por un
# error. Los comandos nativos corren con "Continue" y se validan con $LASTEXITCODE.
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

# Pide a Windows que no suspenda el equipo mientras este script viva (ES_CONTINUOUS |
# ES_SYSTEM_REQUIRED). Si falla, solo se avisa: la tanda sigue.
function Set-MantenerDespierto {
    param([bool]$Activo)
    try {
        if (-not ("Reposo.Nativo" -as [type])) {
            Add-Type -Namespace Reposo -Name Nativo -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
        }
        if ($Activo) {
            [void][Reposo.Nativo]::SetThreadExecutionState([uint32]2147483649)
        } else {
            [void][Reposo.Nativo]::SetThreadExecutionState([uint32]2147483648)
        }
    }
    catch {
        Write-Log "Aviso: no se pudo pedir que el equipo no se suspenda ($($_.Exception.Message))."
    }
}

$dockerDesktopExe = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"

try {
    Write-Log "=== K1: tanda nocturna (maximo $MaxMinutos min) ==="

    if (-not (Test-DockerReady)) {
        Write-Log "Docker no responde. Intentando iniciar Docker Desktop ($dockerDesktopExe)."
        if (-not (Test-Path $dockerDesktopExe)) {
            Write-Log "No se encontro Docker Desktop en la ruta esperada. Se omite la tanda."
            Write-Log "=== TANDA OMITIDA CON ERROR (codigo 1) ==="
            exit 1
        }
        Start-Process -FilePath $dockerDesktopExe
        $maxWaitSeconds = 180
        $elapsed = 0
        $ready = $false
        while (-not $ready -and $elapsed -lt $maxWaitSeconds) {
            Start-Sleep -Seconds 5
            $elapsed += 5
            $ready = Test-DockerReady
        }
        if (-not $ready) {
            Write-Log "Docker Desktop no respondio tras $maxWaitSeconds segundos. Se omite la tanda."
            Write-Log "=== TANDA OMITIDA CON ERROR (codigo 1) ==="
            exit 1
        }
        Write-Log "Docker Desktop listo tras $elapsed segundos de espera."
    } else {
        Write-Log "Docker ya estaba listo."
    }

    # Un solo generador a la vez (por ejemplo, si una tanda manual sigue en curso).
    $ids = @(Invoke-Native { docker ps --filter "ancestor=crypto-trading-platform-kronos:dev" --format "{{.ID}}" } | Where-Object { $_ })
    if ($ids.Count -gt 0) {
        Write-Log "Ya hay un contenedor de Kronos en ejecucion ($($ids -join ', ')). No se lanza otra tanda."
        Write-Log "=== TANDA OMITIDA (codigo 0) ==="
        exit 0
    }

    Set-MantenerDespierto $true
    Write-Log "En ejecucion: scripts.kronos_k1_generar --max-minutos $MaxMinutos ..."
    Invoke-Native { docker compose --profile kronos run --rm -T kronos python -m scripts.kronos_k1_generar --max-minutos $MaxMinutos 2>&1 } | Write-TeeUtf8
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        Write-Log "=== TANDA FINALIZADA (codigo 0) ==="
    } else {
        Write-Log "=== TANDA FINALIZADA CON ERROR (codigo $exitCode) ==="
    }
    exit $exitCode
}
catch {
    Write-Log "ERROR no controlado: $($_.Exception.Message)"
    Write-Log ($_.ScriptStackTrace)
    Write-Log "=== TANDA FINALIZADA CON ERROR (codigo 1) ==="
    exit 1
}
finally {
    Set-MantenerDespierto $false
}
