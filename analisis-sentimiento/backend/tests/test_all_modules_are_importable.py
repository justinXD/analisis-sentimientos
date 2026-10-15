"""Prueba de importabilidad de todo el paquete ``sentiment`` (tarea 1.5).

Esta prueba materializa el criterio 17.4: *el Pipeline_CI importa todos los
módulos del paquete ``sentiment`` y termina en fallo si alguno de ellos no es
importable*. Es la red de seguridad que convierte en un fallo de CI cualquier
regresión que vuelva no importable un módulo del paquete.

El fallo más importante que esta prueba está diseñada para atrapar es el
antipatrón de **leer configuración a nivel de módulo** —el que la tarea 1.4
eliminó—, por ejemplo ``MODEL_ID = os.environ["MODEL_ID"]`` en el cuerpo de un
módulo. Un import así solo tiene éxito si ``MODEL_ID`` está definido en el
entorno, lo que provoca cold starts fallidos y opacos en runtime. Para que la
prueba detecte esa clase de regresión, el recorrido se ejecuta con las variables
de configuración (``MODEL_ID`` y compañía) **deliberadamente ausentes**: importar
cualquier módulo de ``sentiment`` debe tener éxito sin que ninguna de ellas esté
definida. La resolución de la configuración ocurre de forma perezosa, dentro de
las funciones, no durante el import (ver :mod:`sentiment.observability.config`).

El descubrimiento de módulos usa :func:`pkgutil.walk_packages`, que recorre el
paquete y todos sus subpaquetes de forma recursiva, de modo que no hay que
mantener una lista manual de módulos: cualquier módulo nuevo que se añada al
paquete queda cubierto automáticamente.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import sentiment

# Variables de entorno de configuración del Sistema. El recorrido de
# importabilidad se ejecuta con todas ellas ausentes para garantizar que ningún
# módulo del paquete lee configuración a nivel de módulo (criterio 17.4, en
# coordinación con la tarea 1.4). La fuente de estos nombres es
# :mod:`sentiment.observability.config`.
_CONFIG_ENV_VARS = ("MODEL_ID", "MAX_ITEMS", "TTL_DAYS", "MAX_CONCURRENCY", "CONFIG_ENV")


def _discover_module_names() -> list[str]:
    """Descubre, de forma recursiva, el nombre de todos los módulos de ``sentiment``.

    Recorre el paquete ``sentiment`` y sus subpaquetes con
    :func:`pkgutil.walk_packages`, devolviendo los nombres totalmente cualificados
    (p. ej. ``sentiment.adapters.bedrock_adapter``). Incluye el propio paquete
    raíz, que ``walk_packages`` no emite por sí mismo.

    :return: lista ordenada de nombres de módulo totalmente cualificados.
    """
    names = [sentiment.__name__]
    for module_info in pkgutil.walk_packages(
        sentiment.__path__, prefix=f"{sentiment.__name__}."
    ):
        names.append(module_info.name)
    return sorted(names)


# Se descubren una sola vez, al cargar el módulo de prueba, para parametrizar un
# caso por módulo: así el informe de pytest nombra exactamente qué módulo falló.
_MODULE_NAMES = _discover_module_names()


@pytest.fixture(autouse=True)
def _config_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garantiza que ninguna variable de configuración esté definida al importar.

    Si estuvieran presentes, un módulo que (indebidamente) leyera configuración a
    nivel de módulo podría importarse con éxito y la regresión pasaría
    inadvertida. Al eliminarlas, el import de todo módulo debe seguir teniendo
    éxito por sí mismo (criterio 17.4).
    """
    for name in _CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_package_discovery_is_non_empty() -> None:
    """El recorrido descubre módulos; una lista vacía indicaría un fallo del propio
    recorrido (p. ej. un ``__path__`` mal resuelto) que invalidaría la prueba."""
    assert _MODULE_NAMES, "pkgutil.walk_packages no descubrió ningún módulo de 'sentiment'"
    # El paquete raíz y al menos un submódulo conocido deben estar presentes.
    assert "sentiment" in _MODULE_NAMES
    assert "sentiment.observability.config" in _MODULE_NAMES


@pytest.mark.parametrize("module_name", _MODULE_NAMES)
def test_module_is_importable_without_config_env(module_name: str) -> None:
    """Cada módulo del paquete ``sentiment`` es importable sin configuración previa.

    Importar el módulo no debe lanzar ninguna excepción con las variables de
    configuración ausentes. Un fallo aquí señala, típicamente, una lectura de
    configuración o un efecto secundario a nivel de módulo que debería ser
    perezoso (criterio 17.4).

    :param module_name: nombre totalmente cualificado del módulo a importar.
    """
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(
            f"El módulo '{module_name}' no es importable sin configuración en el "
            f"entorno: {exc.__class__.__name__}: {exc}"
        )
