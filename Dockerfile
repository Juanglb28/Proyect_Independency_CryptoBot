# Imagen del servicio "app" en docker-compose.yml.
#
# Por qué existe: Windows 11 con Smart App Control activado bloquea la
# carga de extensiones compiladas (.pyd) sin firma ni reputación reconocida
# — y eso es exactamente lo que son psycopg[binary], pydantic-core y las
# extensiones Cython de SQLAlchemy cuando pip las descarga. Se confirmó en
# la práctica (2026-09-30): tres paquetes distintos, con un venv nuevo y
# Python recién instalado, bloqueados uno tras otro con el mismo error
# ("Una directiva de Control de aplicaciones bloqueó este archivo"). Smart
# App Control gobierna ejecutables de Windows (PE/DLL); no tiene ningún
# efecto dentro de un contenedor Linux, así que correr la app aquí evita el
# problema de raíz en vez de perseguirlo paquete por paquete.
#
# Ver README, sección "Ejecutar comandos dentro de Docker".
FROM python:3.12-slim

WORKDIR /app

# Solo lo necesario para resolver dependencias (pyproject.toml declara
# `include = ["app*"]` como el único paquete instalable). Copiar esto antes
# del resto del repo permite que Docker reutilice esta capa entre rebuilds
# mientras las dependencias no cambien — el resto del código llega en
# tiempo de ejecución vía el volumen montado en docker-compose.yml, no
# desde esta imagen.
COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir -e ".[dev]"

# Sin CMD: este contenedor no es un proceso de fondo con trabajo propio
# (sección 6 del plan — el primer entregable es un esqueleto, no un bot
# corriendo). docker-compose.yml le da un comando que solo lo mantiene vivo
# ("tail -f /dev/null") para poder ejecutar comandos puntuales dentro de él
# con `docker compose run --rm app <comando>`.
