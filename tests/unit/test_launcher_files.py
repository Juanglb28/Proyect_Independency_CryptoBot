"""Los lanzadores de la interfaz (doble clic en Windows) hacen solo lo que prometen.

No se ejecutan (son para Windows/Docker Desktop); se vigila su contenido: que arranquen SOLO
la interfaz y no el scheduler ni ciclos de IA, que usen la dirección local, que no contengan
secretos y que sigan siendo ASCII con fin de línea de Windows (para que cmd/PowerShell 5.1 los lean igual).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
ARCHIVOS = [
    RAIZ / "Abrir interfaz.bat",
    RAIZ / "Detener interfaz.bat",
    RAIZ / "scripts" / "abrir_interfaz.ps1",
    RAIZ / "scripts" / "detener_interfaz.ps1",
    RAIZ / "scripts" / "k1_generar_noche.ps1",
    RAIZ / "scripts" / "k1_noche_oculto.vbs",
    RAIZ / "scripts" / "instalar_k1_noche.ps1",
    RAIZ / "scripts" / "sync_railway.ps1",
    RAIZ / "scripts" / "sync_railway_oculto.vbs",
    RAIZ / "scripts" / "instalar_sync_railway.ps1",
    RAIZ / "scripts" / "respaldo_local.ps1",
    RAIZ / "scripts" / "respaldo_local_oculto.vbs",
    RAIZ / "scripts" / "instalar_respaldo_local.ps1",
    RAIZ / "scripts" / "probar_restauracion.ps1",
]
# Los scripts de la sincronizacion con Railway manejan una contrasena y leen el token de Telegram
# del .env (como el vigia): tienen sus propias pruebas y no pasan por la busqueda generica de palabras.
SINCRONIZACION_RAILWAY = {"sync_railway.ps1", "sync_railway_oculto.vbs", "instalar_sync_railway.ps1"}
# El respaldo local tambien lee el token de Telegram del .env para avisar de un fallo (igual que la
# sincronizacion): tiene sus propias pruebas.
RESPALDO_LOCAL_CON_AVISO = {"respaldo_local.ps1"}


def _texto(ruta: Path) -> str:
    return ruta.read_bytes().decode("ascii")  # falla si hay un carácter no ASCII


def _codigo(ruta: Path) -> str:
    """Solo las líneas que se ejecutan (sin comentarios `#` ni `rem`)."""
    lineas = [
        linea
        for linea in _texto(ruta).splitlines()
        if linea.strip()
        and not linea.lstrip().startswith(("#", "'"))
        and not linea.lstrip().lower().startswith("rem ")
    ]
    return "\n".join(lineas)


@pytest.mark.parametrize("ruta", ARCHIVOS, ids=lambda r: r.name)
def test_existe_es_ascii_y_usa_fin_de_linea_de_windows(ruta):
    assert ruta.exists(), f"falta {ruta}"
    crudo = ruta.read_bytes()
    assert b"\r\n" in crudo and b"\n" not in crudo.replace(b"\r\n", b"")
    _texto(ruta)


def test_abrir_arranca_solo_la_interfaz_y_nunca_el_scheduler_ni_un_ciclo():
    codigo = _codigo(RAIZ / "scripts" / "abrir_interfaz.ps1")
    assert "docker compose up -d ui" in codigo
    for prohibido in ("scheduler", "run_daily", "docker compose run"):
        assert prohibido not in codigo, prohibido
    # ningún `docker compose up -d` sin nombrar el servicio (levantaría todo, scheduler incluido)
    apariciones = [m.end() for m in re.finditer(r"docker compose up", codigo)]
    assert apariciones
    for fin in apariciones:
        assert codigo[fin : fin + len(" -d ui")] == " -d ui", codigo[fin - 17 : fin + 30]


def test_abrir_usa_la_direccion_local_de_la_interfaz():
    texto = _texto(RAIZ / "scripts" / "abrir_interfaz.ps1")
    assert "http://localhost:8501" in texto and "_stcore/health" in texto


def test_detener_solo_detiene_la_interfaz():
    codigo = _codigo(RAIZ / "scripts" / "detener_interfaz.ps1")
    assert "docker compose stop ui" in codigo
    assert "docker compose down" not in codigo and "scheduler" not in codigo and "postgres" not in codigo


@pytest.mark.parametrize("ruta", ARCHIVOS, ids=lambda r: r.name)
def test_sin_secretos(ruta):
    if ruta.name in SINCRONIZACION_RAILWAY:
        pytest.skip("cubierto por las pruebas de la sincronizacion con Railway")
    if ruta.name in RESPALDO_LOCAL_CON_AVISO:
        pytest.skip("cubierto por las pruebas del respaldo local")
    texto = _codigo(ruta).upper()
    for palabra in ("API_KEY", "API_SECRET", "TOKEN", "PASSWORD", "TELEGRAM"):
        assert palabra not in texto


@pytest.mark.parametrize("bat", ["Abrir interfaz.bat", "Detener interfaz.bat"])
def test_el_bat_llama_a_su_script_de_powershell(bat):
    texto = _texto(RAIZ / bat)
    script = "abrir_interfaz.ps1" if bat.startswith("Abrir") else "detener_interfaz.ps1"
    assert f"scripts\\{script}" in texto and "-ExecutionPolicy Bypass" in texto


def test_el_puerto_publicado_sigue_siendo_solo_local():
    compose = (RAIZ / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8501:8501"' in compose


# ── Tanda nocturna de la generacion de K1 (2026-10-15) ───────────────────────


def test_la_tanda_nocturna_solo_lanza_el_generador_de_k1_en_el_contenedor_aislado():
    codigo = _codigo(RAIZ / "scripts" / "k1_generar_noche.ps1")
    assert "docker compose --profile kronos run --rm -T kronos python -m scripts.kronos_k1_generar" in codigo
    assert "--max-minutos $MaxMinutos" in codigo
    for prohibido in ("scheduler", "run_daily", "docker compose up", "docker compose down", "kronos_k0"):
        assert prohibido not in codigo, prohibido


def test_la_tanda_nocturna_no_lanza_un_segundo_generador_si_ya_hay_uno():
    codigo = _codigo(RAIZ / "scripts" / "k1_generar_noche.ps1")
    assert 'docker ps --filter "ancestor=crypto-trading-platform-kronos:dev"' in codigo
    # la comprobacion va ANTES de lanzar el generador
    assert codigo.index("docker ps") < codigo.index("scripts.kronos_k1_generar")


def test_el_lanzador_nocturno_es_oculto_espera_y_devuelve_el_codigo():
    codigo = _codigo(RAIZ / "scripts" / "k1_noche_oculto.vbs")
    assert 'k1_generar_noche.ps1"" -MaxMinutos " & minutos, 0, True)' in codigo
    assert "WScript.Quit codigo" in codigo


def test_el_instalador_nocturno_programa_de_lunes_a_viernes_sin_ponerse_al_dia():
    codigo = _codigo(RAIZ / "scripts" / "instalar_k1_noche.ps1")
    assert "-DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday" in codigo
    assert '[string]$Hora = "00:00"' in codigo
    assert "-MultipleInstances IgnoreNew" in codigo
    # si el equipo estaba apagado a las 00:00, esa noche se omite (no arranca a media manana)
    assert "StartWhenAvailable" not in codigo
    assert "wscript.exe" in codigo and "k1_noche_oculto.vbs" in codigo


# ── Sincronizacion diaria con Railway (2026-10-15) ───────────────────────────

SYNC = RAIZ / "scripts" / "sync_railway.ps1"
INSTALAR_SYNC = RAIZ / "scripts" / "instalar_sync_railway.ps1"
LANZADOR_SYNC = RAIZ / "scripts" / "sync_railway_oculto.vbs"


def test_la_contrasena_se_guarda_cifrada_con_dpapi_y_fuera_del_proyecto():
    instalar = _codigo(INSTALAR_SYNC)
    sync = _codigo(SYNC)
    assert "ConvertFrom-SecureString" in instalar and "Read-Host" in instalar and "-AsSecureString" in instalar
    assert "ConvertTo-SecureString" in sync
    # sin -Key: DPAPI de Windows (solo este usuario, en este equipo)
    assert "-Key" not in instalar and "-Key" not in sync and "SecureKey" not in sync
    for codigo in (instalar, sync):
        assert "$env:LOCALAPPDATA" in codigo and "railway_db.cred" in codigo
        assert "$PSScriptRoot" not in codigo.split("railway_db.cred")[0].splitlines()[-1]


def test_la_contrasena_y_las_url_nunca_se_imprimen_ni_van_en_la_tarea():
    sync = _codigo(SYNC)
    instalar = _codigo(INSTALAR_SYNC)
    for linea in sync.splitlines():
        if re.search(r"Write-(Log|Host|Output)|Out-File|Send-Aviso", linea):
            for variable in ("$clave", "$destino", "$claveUrl", "$seguro", "DEST_DATABASE_URL", "$env:DATABASE_URL"):
                assert variable not in linea, linea
    # la tarea solo lleva la ruta del lanzador, ninguna contrasena
    assert "Register-ScheduledTask" in instalar and "-Argument" in instalar
    for linea in instalar.splitlines():
        if "-Argument" in linea:
            assert "clave" not in linea.lower() and "password" not in linea.lower()
    # las variables con la URL viven solo en este proceso y se borran siempre
    assert "finally" in sync and "Remove-Item Env:DATABASE_URL, Env:SOURCE_DATABASE_URL, Env:DEST_DATABASE_URL" in sync
    assert "[uri]::EscapeDataString" in sync


def test_solo_la_rama_de_sincronizar_escribe_en_railway():
    sync = _codigo(SYNC)
    escriben = [x for x in sync.splitlines() if "--ejecutar" in x and "docker compose run" in x]
    assert len(escriben) == 1
    linea = escriben[0]
    assert "scripts.copy_database" in linea
    # esa linea va despues del `else` de la simulacion
    assert sync.index("if ($Simulacion)") < sync.index(linea)
    # solo hace de origen la base local en Docker y solo alembic upgrade head toca el esquema de Railway
    assert "postgres:5432/crypto_trading" in sync
    for prohibido in ("DROP ", "TRUNCATE", "DELETE ", "docker compose down", "scheduler"):
        assert prohibido not in sync, prohibido


def test_un_fallo_avisa_por_telegram_y_devuelve_un_codigo_distinto_de_cero():
    sync = _codigo(SYNC)
    assert "function Fallar" in sync and "Send-Aviso" in sync and "exit 1" in sync
    assert "exit 0" in sync
    # el exito solo se anota cuando se sincroniza de verdad
    assert "sync_railway_ultimo_ok.txt" in sync
    assert sync.index("if ($codigo -ne 0)") < sync.index("sync_railway_ultimo_ok.txt")
    # nunca se muestra la excepcion cruda (la URL de Telegram lleva el token)
    assert "$_.Exception.Message" not in sync and "Out-String" not in sync


def test_el_lanzador_de_la_sincronizacion_es_oculto_espera_y_devuelve_el_codigo():
    codigo = _codigo(LANZADOR_SYNC)
    assert 'sync_railway.ps1""", 0, True)' in codigo
    assert "WScript.Quit codigo" in codigo


def test_el_instalador_de_la_sincronizacion_programa_cada_dia_a_las_20_sin_ventana():
    codigo = _codigo(INSTALAR_SYNC)
    assert '[string]$Hora = "20:00"' in codigo
    assert "New-ScheduledTaskTrigger -Daily -At $Hora" in codigo
    assert "wscript.exe" in codigo and "sync_railway_oculto.vbs" in codigo
    assert "-MultipleInstances IgnoreNew" in codigo
    # si el equipo estaba apagado a las 20:00, se pone al dia al encender (2026-10-25; antes: sin ponerse al dia)
    assert "-StartWhenAvailable" in codigo
    # la comprobacion final es una simulacion: no escribe en Railway
    assert "-Simulacion" in codigo and "--ejecutar" not in codigo


# --- Respaldo local semanal y prueba de restauracion (2026-11-10) -----------------------------------
RESPALDO = RAIZ / "scripts" / "respaldo_local.ps1"
INSTALAR_RESPALDO = RAIZ / "scripts" / "instalar_respaldo_local.ps1"
LANZADOR_RESPALDO = RAIZ / "scripts" / "respaldo_local_oculto.vbs"
PROBAR_RESTAURACION = RAIZ / "scripts" / "probar_restauracion.ps1"


def test_el_respaldo_solo_lee_la_base_y_no_toca_nada_mas():
    codigo = _codigo(RESPALDO)
    assert "pg_dump" in codigo and "-Fc" in codigo
    # no escribe en la base ni para el sistema; no maneja contrasenas ni URL de bases
    for prohibido in (
        "psql", "DROP ", "TRUNCATE", "DELETE ", "INSERT ", "docker compose", "scheduler",
        "DATABASE_URL", "PGPASSWORD", "SecureString", "--clean",
    ):
        assert prohibido not in codigo, prohibido
    # la rotacion solo toca los respaldos de la propia carpeta de destino
    borrados = [x for x in codigo.splitlines() if "Remove-Item" in x]
    assert borrados, "debe haber rotacion y limpieza del .partial"
    for linea in borrados:
        assert "-Recurse" not in linea, linea
        assert "$v.FullName" in linea or "$script:parcial" in linea, linea
    assert '-Filter "crypto_trading_*.dump"' in codigo and "-Skip $Conservar" in codigo


def test_el_respaldo_solo_deja_el_dump_definitivo_tras_verificar_hash_y_lectura():
    codigo = _codigo(RESPALDO)
    assert ".partial" in codigo
    # orden: copiar -> comparar SHA-256 -> comprobar que pg_restore lo lee -> renombrar -> rotar
    pasos = ["docker cp", "Get-FileHash", "pg_restore --list", "Rename-Item", "-Skip $Conservar"]
    posiciones = [codigo.index(p) for p in pasos]
    assert posiciones == sorted(posiciones)
    assert "sha256sum" in codigo
    # la rotacion y la marca de exito solo ocurren despues de un respaldo bueno
    assert codigo.index("Rename-Item") < codigo.index("respaldo_local_ultimo_ok.txt")


def test_el_respaldo_falla_con_aviso_y_codigo_distinto_de_cero_si_la_unidad_no_esta():
    codigo = _codigo(RESPALDO)
    assert "function Fallar" in codigo and "Send-Aviso" in codigo and "exit 1" in codigo and "exit 0" in codigo
    assert "no esta disponible" in codigo and "Test-Path ($unidad" in codigo
    assert codigo.index("Test-Path ($unidad") < codigo.index("pg_dump")
    # un fallo limpia el .partial y el temporal del contenedor
    assert "Limpiar-Temporales" in codigo.split("function Fallar")[1].split("}")[0]
    # nunca se muestra la excepcion cruda (la URL de Telegram lleva el token)
    assert "$_.Exception.Message" not in codigo and "Out-String" not in codigo
    assert "$Conservar -lt 1" in codigo


def test_el_lanzador_del_respaldo_es_oculto_espera_y_devuelve_el_codigo():
    codigo = _codigo(LANZADOR_RESPALDO)
    assert 'respaldo_local.ps1""", 0, True)' in codigo
    assert "WScript.Quit codigo" in codigo


def test_el_instalador_del_respaldo_programa_una_vez_por_semana_sin_ventana_y_sin_contrasenas():
    codigo = _codigo(INSTALAR_RESPALDO)
    assert '[string]$Dia = "Sunday"' in codigo and '[string]$Hora = "21:00"' in codigo
    assert "New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Dia -At $Hora" in codigo
    assert "wscript.exe" in codigo and "respaldo_local_oculto.vbs" in codigo
    assert "-MultipleInstances IgnoreNew" in codigo and "-StartWhenAvailable" in codigo
    assert "-Quitar" in codigo or "$Quitar" in codigo
    # no maneja contrasenas ni ejecuta el respaldo al instalar
    for prohibido in ("SecureString", "Read-Host", "DATABASE_URL", "docker "):
        assert prohibido not in codigo, prohibido


def test_la_prueba_de_restauracion_solo_escribe_en_la_base_temporal():
    codigo = _codigo(PROBAR_RESTAURACION)
    assert '$bdPrueba = "restore_test"' in codigo and '$bdViva = "crypto_trading"' in codigo
    assert "if ($bdPrueba -eq $bdViva)" in codigo
    # toda orden que crea o borra bases usa la variable de la base temporal, nunca la viva
    for linea in codigo.splitlines():
        if "DROP DATABASE" in linea or "CREATE DATABASE" in linea:
            assert "$bdPrueba" in linea and "$bdViva" not in linea and "crypto_trading" not in linea, linea
    # pg_restore apunta a la temporal y sin --clean; a la viva solo se le piden conteos de lectura
    restore = [x for x in codigo.splitlines() if "pg_restore" in x and "docker exec" in x]
    assert len(restore) == 1 and "-d $bdPrueba" in restore[0] and "--clean" not in restore[0]
    for linea in codigo.splitlines():
        if "$bdViva" in linea:
            for prohibido in ("DROP", "CREATE", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "pg_restore", "pre_restore", "post_restore"):
                assert prohibido not in linea, linea
    # receta de TimescaleDB, en orden
    pasos = ["CREATE EXTENSION IF NOT EXISTS timescaledb", "timescaledb_pre_restore", "pg_restore -U", "timescaledb_post_restore"]
    posiciones = [codigo.index(p) for p in pasos]
    assert posiciones == sorted(posiciones)
    # siempre limpia la temporal y no maneja secretos
    assert "function Limpiar-Prueba" in codigo and codigo.count("Limpiar-Prueba") >= 3
    for prohibido in ("PGPASSWORD", "DATABASE_URL", "SecureString", "TELEGRAM", "TOKEN"):
        assert prohibido not in codigo.upper(), prohibido


@pytest.mark.parametrize("ruta", [RESPALDO, INSTALAR_RESPALDO, PROBAR_RESTAURACION], ids=lambda r: r.name)
def test_ninguna_cadena_pone_dos_puntos_pegados_a_una_variable(ruta):
    """En PowerShell "$tabla: x" se lee como una variable con unidad (`$tabla:`) y el script ni
    arranca (ParserError, visto en la primera corrida real). Solo se permiten `$env:` y `$script:`;
    en el resto hay que escribir ${tabla}: ."""
    for linea in _codigo(ruta).splitlines():
        for m in re.finditer(r"\$([A-Za-z_]\w*):(?!:)", linea):
            assert m.group(1).lower() in ("env", "script"), linea

