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
# Qué hace: levanta los contenedores (por si Docker Desktop se reinició) y
# corre el ciclo dentro del contenedor `app` — ver docker-compose.yml y
# README.md, sección "Ejecutar comandos dentro de Docker" (necesario en esta
# máquina por el bloqueo de Smart App Control con las extensiones
# compiladas nativas de Windows, ver Evento 3077 de CodeIntegrity).
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

docker compose up -d *>> $logFile
docker compose run --rm app python -m scripts.run_daily_cycle *>> $logFile

# Código de salida no-cero si el ciclo falló, para que Task Scheduler lo
# registre como ejecución fallida en su historial (visible en
# "Ver historial" de la tarea) en vez de reportar éxito siempre.
exit $LASTEXITCODE
