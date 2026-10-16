# Detiene la interfaz local (solo el contenedor `ui`).
# No toca la base de datos ni el scheduler: si el scheduler esta corriendo, sigue
# corriendo (monitor, ciclos y avisos de Telegram no se interrumpen).
# Archivo solo ASCII a proposito (ver abrir_interfaz.ps1).

$ErrorActionPreference = "Continue"
Set-Location -Path (Join-Path $PSScriptRoot "..")

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "No se encontro el comando 'docker'."
    exit 1
}

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker no esta corriendo: no hay nada que detener."
    exit 0
}

docker compose stop ui 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Host "docker compose stop ui fallo (codigo $LASTEXITCODE)."
    exit 1
}
Write-Host "Interfaz detenida."
exit 0
