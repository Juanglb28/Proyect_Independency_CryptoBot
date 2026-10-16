# Abre la interfaz local (Streamlit) con un solo paso.
#
# Que hace: se asegura de que Docker Desktop este corriendo (lo levanta y espera
# hasta 2 minutos), arranca SOLO la base de datos y la interfaz
# (`docker compose up -d ui`; no toca el scheduler ni lanza ningun ciclo de IA),
# espera a que la interfaz responda y abre http://localhost:8501 en el navegador.
#
# Uso: doble clic en "Abrir interfaz.bat" (en la raiz del proyecto), o
#     powershell -File scripts\abrir_interfaz.ps1
#
# La interfaz solo escucha en 127.0.0.1 (ver docker-compose.yml) y no recibe
# claves de Binance ni token de Telegram. Para detenerla: "Detener interfaz.bat".
#
# Este archivo es solo ASCII a proposito: asi Windows PowerShell 5.1 lo lee igual
# sin importar la codificacion.

$ErrorActionPreference = "Continue"
Set-Location -Path (Join-Path $PSScriptRoot "..")

$url = "http://localhost:8501"
$dockerDesktopExe = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"

function Test-DockerReady {
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

function Test-InterfazLista {
    try {
        $r = Invoke-WebRequest -Uri "$url/_stcore/health" -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "No se encontro el comando 'docker'. Instala Docker Desktop y vuelve a intentar."
    exit 1
}

if (-not (Test-DockerReady)) {
    Write-Host "Docker no responde. Iniciando Docker Desktop..."
    if (-not (Test-Path $dockerDesktopExe)) {
        Write-Host "No se encontro Docker Desktop en: $dockerDesktopExe"
        Write-Host "Abrelo a mano y vuelve a ejecutar este archivo."
        exit 1
    }
    Start-Process -FilePath $dockerDesktopExe
    $maxEspera = 120
    $espera = 0
    $listo = $false
    while (-not $listo -and $espera -lt $maxEspera) {
        Start-Sleep -Seconds 5
        $espera += 5
        $listo = Test-DockerReady
    }
    if (-not $listo) {
        Write-Host "Docker Desktop no respondio tras $maxEspera segundos. Intenta de nuevo."
        exit 1
    }
    Write-Host "Docker Desktop listo."
}

if (Test-InterfazLista) {
    Write-Host "La interfaz ya estaba en marcha."
} else {
    Write-Host "Arrancando la interfaz (la primera vez puede tardar unos minutos en construir la imagen)..."
    docker compose up -d ui 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "docker compose up -d ui fallo (codigo $LASTEXITCODE)."
        exit 1
    }

    $maxEspera = 120
    $espera = 0
    while (-not (Test-InterfazLista) -and $espera -lt $maxEspera) {
        Start-Sleep -Seconds 2
        $espera += 2
    }
    if (-not (Test-InterfazLista)) {
        Write-Host "La interfaz no respondio tras $maxEspera segundos. Revisa: docker compose logs ui"
        exit 1
    }
}

Write-Host "Abriendo $url"
Start-Process $url
exit 0
