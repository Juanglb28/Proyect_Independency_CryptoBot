"""Interruptor de emergencia (`app/execution/emergency_stop.py`): solo mira si
el archivo `PARAR` existe."""

from pathlib import Path

from app.execution import emergency_stop
from app.execution.emergency_stop import PARAR_FILENAME, FileEmergencyStop


def test_el_archivo_se_llama_parar_y_vive_en_la_raiz_del_proyecto():
    # (El conftest redirige `PARAR_PATH` a una ruta temporal; se recalcula el real.)
    real = Path(emergency_stop.__file__).resolve().parents[2] / PARAR_FILENAME
    assert PARAR_FILENAME == "PARAR"
    assert (real.parent / "app").is_dir() and (real.parent / "pyproject.toml").is_file()


def test_sin_archivo_no_esta_activo(tmp_path):
    assert FileEmergencyStop(tmp_path / "PARAR").is_active() is False


def test_con_archivo_esta_activo_aunque_este_vacio(tmp_path):
    archivo = tmp_path / "PARAR"
    archivo.write_text("")
    assert FileEmergencyStop(archivo).is_active() is True


def test_al_borrar_el_archivo_se_suelta_el_freno_sin_reiniciar(tmp_path):
    archivo = tmp_path / "PARAR"
    parada = FileEmergencyStop(archivo)
    archivo.write_text("")
    assert parada.is_active() is True
    archivo.unlink()
    assert parada.is_active() is False


def test_sin_ruta_explicita_usa_la_ruta_por_defecto_en_cada_consulta():
    parada = FileEmergencyStop()
    assert parada.is_active() is False  # el conftest apunta a una ruta temporal vacía
    emergency_stop.PARAR_PATH.write_text("")
    assert parada.is_active() is True
