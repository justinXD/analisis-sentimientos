"""Configuración tipada por entorno de la Infraestructura (diseño 14.4.1).

Este módulo define :class:`EnvConfig`, la dataclass inmutable que transporta
todos los parámetros de despliegue de un entorno (``dev`` / ``stage`` / ``prod``)
desde los archivos ``config/{env}.json`` hasta los stacks de CDK.

Diseño:
    - La cuenta y la región **no** viven en los archivos: se resuelven de las
      credenciales del despliegue (``CDK_DEFAULT_ACCOUNT`` / ``CDK_DEFAULT_REGION``).
    - Los archivos ``config/*.json`` **no contienen ningún valor secreto**
      (Requirement 17.20): ni claves, ni ``client_secret``, ni el secreto HMAC.
      Los secretos viven en Secrets Manager y la aplicación los lee en runtime.

La validación de invariantes entre parámetros vive en
:meth:`EnvConfig.__post_init__` (tarea 1.7): el umbral del sweeper debe cubrir la
ventana máxima de reintentos de la Cola_Solicitudes más un margen, de modo que
un entorno mal configurado rompa ``cdk synth`` de inmediato (criterio 10.12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# Directorio que contiene los archivos de configuración por entorno.
_CONFIG_DIR = Path(__file__).resolve().parent

# Entornos de despliegue soportados (Requirement 17.20).
VALID_ENVIRONMENTS: tuple[str, ...] = ("dev", "stage", "prod")

# Margen de seguridad, en segundos, que el umbral del sweeper debe añadir por
# encima de la ventana máxima de reintentos de la Cola_Solicitudes.
#
# El criterio 10.12 fija el `STUCK_THRESHOLD` de modo que cubra la ventana máxima
# de reintentos de la cola (``queue_max_receive_count × queue_visibility_timeout_seconds``,
# p. ej. 3 × 180 = 540 s) MÁS este margen (360 s), lo que da el valor nominal de
# 900 s. Sin el margen, el sweeper podría expirar un Lote en ``PENDING`` que la
# cola todavía tiene derecho a reentregar, cerrándolo por error antes de que la
# orquestación llegue siquiera a arrancar.
SWEEPER_MARGIN_SECONDS: int = 360


@dataclass(frozen=True, slots=True)
class EnvConfig:
    """Parámetros de despliegue de un entorno, inmutables y tipados.

    Cada campo corresponde a una clave del archivo ``config/{env}.json``. La
    instancia es inmutable (``frozen=True``) para que ningún stack pueda mutar
    la configuración una vez cargada, y usa ``slots=True`` para evitar atributos
    accidentales fuera del contrato declarado.

    Attributes:
        project: Prefijo de nombre del proyecto para todos los recursos
            (p. ej. ``"sentiment"``). Se combina con ``env_name`` para formar
            los nombres de stack ``sentiment-{env}-...``.
        env_name: Nombre del entorno; uno de :data:`VALID_ENVIRONMENTS`.
        log_retention_days: Días de retención de los log groups de CloudWatch.
        cors_origins: Orígenes permitidos por CORS en API Gateway, por entorno.
        bedrock_model_id: Identificador del modelo de Bedrock a invocar. En
            ``dev`` puede ser un marcador hasta la calibración (tarea 15.3).
        ses_sender_email: Dirección de correo remitente que SES debe verificar
            como identidad (``From`` de las notificaciones de fin de proceso,
            criterio 13.4). Es un valor **no secreto** por entorno; se usa un
            marcador por entorno hasta que el operador verifique el buzón real
            en SES (steering de seguridad: nada de direcciones reales en el
            repositorio).
        distributed_map_max_concurrency: ``MaxConcurrency`` del Distributed Map
            padre (lotes de 25 ítems procesados en paralelo).
        llm_fanout_max_concurrency: ``MaxConcurrency`` del Map interno que hace
            fan-out por ítem hacia ``analyze_llm``.
        analyze_llm_reserved_concurrency: Concurrencia reservada de la Lambda
            ``analyze_llm``; es el dial anti-throttle de Bedrock.
        queue_visibility_timeout_seconds: Visibility timeout de la cola de
            solicitudes, en segundos.
        queue_max_receive_count: Número máximo de recepciones antes de enviar un
            mensaje a la DLQ de solicitudes.
        stuck_threshold_seconds: Antigüedad, en segundos, a partir de la cual el
            sweeper considera un lote como atascado/abandonado.
        data_ttl_days: TTL por defecto, en días, de los registros en DynamoDB y
            del lifecycle de S3.
        waf_mode: Modo del WAF (``"COUNT"`` para observar, ``"BLOCK"`` para
            bloquear).

    Invariante (criterio 10.12):
        ``stuck_threshold_seconds`` debe cubrir la ventana máxima de reintentos
        de la Cola_Solicitudes más :data:`SWEEPER_MARGIN_SECONDS`, es decir::

            stuck_threshold_seconds
                >= queue_max_receive_count * queue_visibility_timeout_seconds
                   + SWEEPER_MARGIN_SECONDS

        :meth:`__post_init__` la verifica en el momento de construir el objeto,
        de modo que un entorno mal configurado rompe ``cdk synth`` de inmediato
        y nunca llega a desplegarse.
    """

    project: str
    env_name: str
    log_retention_days: int
    cors_origins: tuple[str, ...]
    bedrock_model_id: str
    ses_sender_email: str
    distributed_map_max_concurrency: int
    llm_fanout_max_concurrency: int
    analyze_llm_reserved_concurrency: int
    queue_visibility_timeout_seconds: int
    queue_max_receive_count: int
    stuck_threshold_seconds: int
    data_ttl_days: int
    waf_mode: str

    def __post_init__(self) -> None:
        """Valida la invariante del umbral del sweeper frente a la ventana de la cola.

        Garantiza que el sweeper (Reconcile_Stuck) nunca expire un Lote en
        ``PENDING`` que la Cola_Solicitudes todavía puede reentregar. El umbral
        debe cubrir la ventana máxima de reintentos de la cola
        (``queue_max_receive_count × queue_visibility_timeout_seconds``) más el
        margen de :data:`SWEEPER_MARGIN_SECONDS` (criterio 10.12).

        Raises:
            ValueError: Si ``stuck_threshold_seconds`` es menor que el mínimo
                requerido. El mensaje nombra los valores en conflicto para que el
                fallo de ``cdk synth`` sea autoexplicativo.
        """

        retry_window = (
            self.queue_max_receive_count * self.queue_visibility_timeout_seconds
        )
        minimum_threshold = retry_window + SWEEPER_MARGIN_SECONDS

        if self.stuck_threshold_seconds < minimum_threshold:
            raise ValueError(
                f"Configuración de entorno {self.env_name!r} inválida (criterio "
                f"10.12): stuck_threshold_seconds={self.stuck_threshold_seconds} es "
                f"menor que el mínimo requerido {minimum_threshold} = "
                f"queue_max_receive_count({self.queue_max_receive_count}) × "
                f"queue_visibility_timeout_seconds({self.queue_visibility_timeout_seconds}) "
                f"+ SWEEPER_MARGIN_SECONDS({SWEEPER_MARGIN_SECONDS}). El umbral del "
                f"sweeper debe cubrir la ventana máxima de reintentos de la cola "
                f"({retry_window}s) más el margen, o Reconcile_Stuck podría expirar "
                f"un Lote que la cola aún puede reentregar."
            )


def _config_path(env_name: str) -> Path:
    """Devuelve la ruta del archivo JSON de un entorno.

    Args:
        env_name: Nombre del entorno (``dev`` / ``stage`` / ``prod``).

    Returns:
        Ruta absoluta al archivo ``config/{env_name}.json``.
    """

    return _CONFIG_DIR / f"{env_name}.json"


def load_env_config(env_name: str) -> EnvConfig:
    """Carga y construye el :class:`EnvConfig` de un entorno desde su JSON.

    Lee ``config/{env_name}.json`` y lo materializa en un :class:`EnvConfig`.
    Falla de forma explícita (fail-fast) ante un entorno desconocido, un archivo
    inexistente, un JSON inválido, o claves que no coinciden exactamente con el
    contrato de la dataclass. Esto hace que un error de configuración rompa
    ``cdk synth`` y nunca llegue a AWS.

    Args:
        env_name: Nombre del entorno a cargar; debe estar en
            :data:`VALID_ENVIRONMENTS`.

    Returns:
        La instancia de :class:`EnvConfig` con los valores del entorno.

    Raises:
        ValueError: Si ``env_name`` no es un entorno soportado, o si el JSON no
            contiene exactamente las claves esperadas por :class:`EnvConfig`.
        FileNotFoundError: Si no existe el archivo de configuración del entorno.
        json.JSONDecodeError: Si el archivo no es JSON válido.
    """

    if env_name not in VALID_ENVIRONMENTS:
        raise ValueError(
            f"Entorno desconocido: {env_name!r}. "
            f"Entornos válidos: {', '.join(VALID_ENVIRONMENTS)}"
        )

    path = _config_path(env_name)
    if not path.is_file():
        raise FileNotFoundError(
            f"No existe el archivo de configuración del entorno {env_name!r}: {path}"
        )

    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    expected_keys = {f.name for f in fields(EnvConfig)}
    actual_keys = set(raw.keys())

    missing = expected_keys - actual_keys
    unexpected = actual_keys - expected_keys
    if missing or unexpected:
        problems: list[str] = []
        if missing:
            problems.append(f"faltan claves: {', '.join(sorted(missing))}")
        if unexpected:
            problems.append(f"claves no reconocidas: {', '.join(sorted(unexpected))}")
        raise ValueError(
            f"La configuración de {env_name!r} ({path.name}) no coincide con "
            f"EnvConfig: {'; '.join(problems)}"
        )

    # Normaliza las listas de JSON a tuplas inmutables para respetar frozen/slots.
    cors_origins = tuple(raw["cors_origins"])

    return EnvConfig(
        project=raw["project"],
        env_name=raw["env_name"],
        log_retention_days=raw["log_retention_days"],
        cors_origins=cors_origins,
        bedrock_model_id=raw["bedrock_model_id"],
        ses_sender_email=raw["ses_sender_email"],
        distributed_map_max_concurrency=raw["distributed_map_max_concurrency"],
        llm_fanout_max_concurrency=raw["llm_fanout_max_concurrency"],
        analyze_llm_reserved_concurrency=raw["analyze_llm_reserved_concurrency"],
        queue_visibility_timeout_seconds=raw["queue_visibility_timeout_seconds"],
        queue_max_receive_count=raw["queue_max_receive_count"],
        stuck_threshold_seconds=raw["stuck_threshold_seconds"],
        data_ttl_days=raw["data_ttl_days"],
        waf_mode=raw["waf_mode"],
    )
