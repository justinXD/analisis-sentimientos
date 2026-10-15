"""Carga validada de configuración no secreta (variables de entorno y SSM).

Este módulo es la **fuente única** de la configuración de runtime de las Lambdas:
el identificador del Modelo_Bedrock (`MODEL_ID`), los límites de ingesta y el
periodo de retención. La Infraestructura (CDK) inyecta estos valores como
variables de entorno en cada Lambda y, para los que deben poder cambiarse sin
redeploy (p. ej. `MODEL_ID`), los publica además en Parameter_Store.

Reglas de diseño que este módulo respeta (criterios 15.5, 17.4, 17.7, 7.9, 7.15):

- **Nada se lee a nivel de módulo.** Importar este módulo nunca toca el entorno,
  nunca llama a AWS y nunca lanza una excepción. Esto es lo que permite que el
  recorrido de importabilidad (`pkgutil.walk_packages`) importe todos los módulos
  del paquete sin necesidad de que `MODEL_ID` esté definido. El antipatrón que
  esto sustituye era ``MODEL_ID = os.environ["MODEL_ID"]`` a nivel de módulo, que
  provocaba un ``KeyError`` en el import y un cold start fallido sin mensaje útil.
- **Fallo explícito y accionable.** Si un valor requerido falta o es inválido en
  el momento en que se solicita, se lanza :class:`ConfigError` con un mensaje que
  nombra la clave ausente, no un ``KeyError`` opaco.
- **Acceso perezoso y memoizado.** Los valores se resuelven la primera vez que se
  piden y se cachean en el ámbito del módulo para reutilizarlos entre
  invocaciones del mismo contenedor (reduce latencia y llamadas a SSM).

El fallback a Parameter_Store es opcional y perezoso: solo se intenta si la
variable de entorno no está presente, y requiere que ``boto3`` esté instalado en
el runtime (lo está, vía la Lambda Layer). La ausencia de ``boto3`` en un entorno
de solo-importación nunca rompe el import, porque la importación de ``boto3`` se
hace dentro de la función, no a nivel de módulo.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Final

__all__ = [
    "ConfigError",
    "model_id",
    "max_items",
    "ttl_days",
    "max_concurrency",
    "reset_cache",
]

# Nombres de las variables de entorno que inyecta la Infraestructura. También son
# los sufijos de los parámetros de Parameter_Store bajo el prefijo del proyecto.
_ENV_MODEL_ID: Final = "MODEL_ID"
_ENV_MAX_ITEMS: Final = "MAX_ITEMS"
_ENV_TTL_DAYS: Final = "TTL_DAYS"
_ENV_MAX_CONCURRENCY: Final = "MAX_CONCURRENCY"

# Prefijo del nombre del parámetro en SSM Parameter_Store. El entorno concreto
# (`dev`/`stage`/`prod`) se antepone vía la variable `CONFIG_ENV` que inyecta la
# Infraestructura, dando p. ej. `/sentiment/dev/MODEL_ID`.
_SSM_PREFIX: Final = "/sentiment"
_ENV_STAGE: Final = "CONFIG_ENV"

# Límites por defecto. Son los del diseño (criterios 3.6, 16.1) y solo se usan
# cuando la variable de entorno correspondiente no está definida; nunca ocultan
# un valor inyectado por la Infraestructura.
_DEFAULT_MAX_ITEMS: Final = 1000
_DEFAULT_TTL_DAYS: Final = 90
_DEFAULT_MAX_CONCURRENCY: Final = 4


class ConfigError(RuntimeError):
    """Error de configuración de runtime con un mensaje accionable.

    Se lanza cuando un valor requerido falta o no puede interpretarse. El mensaje
    nombra siempre la clave implicada para que el fallo sea diagnosticable en los
    logs sin exponer ningún secreto ni PII.
    """


# Caché de valores ya resueltos. La clave es el nombre de la variable de entorno.
_cache: dict[str, object] = {}


def reset_cache() -> None:
    """Vacía la caché de configuración.

    Pensado para las pruebas, que modifican el entorno entre casos. En runtime no
    es necesario llamarlo: la caché vive lo que vive el contenedor de la Lambda.
    """
    _cache.clear()


def _ssm_lookup(parameter_suffix: str) -> str | None:
    """Resuelve un parámetro desde Parameter_Store, o ``None`` si no procede.

    Devuelve ``None`` (en lugar de lanzar) cuando no hay entorno configurado,
    cuando ``boto3`` no está disponible o cuando el parámetro no existe. El
    llamador decide entonces si la ausencia es un error. La importación de
    ``boto3`` es local a la función para no romper la importabilidad del paquete
    en entornos sin el SDK.

    :param parameter_suffix: sufijo del nombre del parámetro (p. ej. ``MODEL_ID``).
    :return: el valor del parámetro, o ``None`` si no se pudo resolver.
    """
    stage = os.environ.get(_ENV_STAGE)
    if not stage:
        return None
    name = f"{_SSM_PREFIX}/{stage}/{parameter_suffix}"
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return None
    try:
        client = boto3.client("ssm")
        response = client.get_parameter(Name=name)
        value = response["Parameter"]["Value"]
    except (BotoCoreError, ClientError, KeyError):
        return None
    return value if isinstance(value, str) else None


def _require_str(env_name: str) -> str:
    """Devuelve una cadena no vacía desde el entorno o SSM, o lanza ConfigError.

    El orden de resolución es: variable de entorno, luego Parameter_Store. Un
    valor presente pero vacío o compuesto solo de espacios se trata como ausente,
    conforme a los criterios 7.15 y 1.15.

    :param env_name: nombre de la variable de entorno / sufijo del parámetro SSM.
    :raises ConfigError: si el valor falta o es una cadena en blanco.
    """
    cached = _cache.get(env_name)
    if isinstance(cached, str):
        return cached

    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        raw = _ssm_lookup(env_name)

    if raw is None or not raw.strip():
        raise ConfigError(
            f"Falta la configuración requerida '{env_name}'. Debe inyectarse como "
            f"variable de entorno o publicarse en Parameter_Store "
            f"('{_SSM_PREFIX}/<env>/{env_name}')."
        )

    value = raw.strip()
    _cache[env_name] = value
    return value


def _optional_int(env_name: str, default: int) -> int:
    """Devuelve un entero positivo desde el entorno, o un valor por defecto.

    :param env_name: nombre de la variable de entorno.
    :param default: valor a usar si la variable no está definida.
    :raises ConfigError: si la variable está definida pero no es un entero > 0.
    """
    cached = _cache.get(env_name)
    if isinstance(cached, int):
        return cached

    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        _cache[env_name] = default
        return default

    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"La configuración '{env_name}' debe ser un entero; se recibió un valor "
            f"no numérico."
        ) from exc
    if value <= 0:
        raise ConfigError(
            f"La configuración '{env_name}' debe ser un entero positivo; "
            f"se recibió {value}."
        )
    _cache[env_name] = value
    return value


def model_id() -> str:
    """Identificador del Modelo_Bedrock, leído de forma validada y perezosa.

    Resuelve ``MODEL_ID`` desde la variable de entorno y, en su defecto, desde
    Parameter_Store, cumpliendo el criterio 7.9. Nunca se lee a nivel de módulo
    (criterio 17.7 operativo: el import no debe depender del entorno).

    :return: el ``model_id`` como cadena no vacía y recortada.
    :raises ConfigError: si no hay un ``MODEL_ID`` válido disponible
        (cumple el criterio 7.15: ausente, vacío o solo espacios es un error).
    """
    return _require_str(_ENV_MODEL_ID)


def max_items() -> int:
    """Número máximo de Ítems admitidos por Lote (criterio 3.6).

    :return: el límite configurado, o 1000 por defecto.
    :raises ConfigError: si ``MAX_ITEMS`` está definido pero es inválido.
    """
    return _optional_int(_ENV_MAX_ITEMS, _DEFAULT_MAX_ITEMS)


def ttl_days() -> int:
    """Periodo de retención en días para el atributo ``ttl`` (criterio 16.1).

    :return: los días de retención configurados, o 90 por defecto.
    :raises ConfigError: si ``TTL_DAYS`` está definido pero es inválido.
    """
    return _optional_int(_ENV_TTL_DAYS, _DEFAULT_TTL_DAYS)


def max_concurrency() -> int:
    """Concurrencia inicial del Distributed_Map (criterio 5.5).

    :return: la concurrencia configurada, o 4 por defecto.
    :raises ConfigError: si ``MAX_CONCURRENCY`` está definido pero es inválido.
    """
    return _optional_int(_ENV_MAX_CONCURRENCY, _DEFAULT_MAX_CONCURRENCY)


# Mapa de accesores exportado para introspección en pruebas; no es parte de la
# API de runtime, pero mantiene la lista de accesores en un único lugar.
_ACCESSORS: Final[dict[str, Callable[[], object]]] = {
    _ENV_MODEL_ID: model_id,
    _ENV_MAX_ITEMS: max_items,
    _ENV_TTL_DAYS: ttl_days,
    _ENV_MAX_CONCURRENCY: max_concurrency,
}
