"""Adaptador del Bus_Eventos (EventBridge): implementación del puerto ``EventPublisher``.

Este adaptador materializa el puerto :class:`~sentiment.domain.ports.EventPublisher`
(contrato normativo en ``domain/ports.py``) publicando el Evento_Finalizacion de
un Lote en el Bus_Eventos personalizado cuando el Lote alcanza un Estado_Terminal
(criterio 13.1). El evento es el *backbone* de integración del Sistema: cualquier
consumidor (SES, webhook, un bus cross-account) reacciona a él sin tocar el
pipeline.

Contrato del evento (sección 9.3 del diseño; criterios 13.1, 13.2, 13.3, 10.20)
-------------------------------------------------------------------------------
Cada entrada publicada con ``PutEvents`` lleva:

- ``Source``: ``"sentiment.pipeline"`` (acotado al proyecto).
- ``DetailType``: ``"analysis.completed"`` (criterio 13.1).
- ``EventBusName``: el Bus_Eventos personalizado, leído de ``EVENT_BUS_NAME``.
- ``Detail``: objeto JSON con **solo punteros e identificadores**, nunca PII ni
  Presigned_URL (criterio 13.3):

  - ``event_id`` = ``"{analysis_id}:{status}"``: identificador **determinista** de
    desduplicación (criterio 10.20). La publicación es de **entrega al menos una
    vez** (criterio 13.11): la agregación publica-y-luego-marca, y el sweeper
    puede republicar un Lote terminal sin evento confirmado; por eso el consumidor
    descarta duplicados comparando ``event_id``. Como el ``status`` terminal de un
    Lote no vuelve a cambiar, el par ``{analysis_id}:{status}`` identifica de forma
    estable cada publicación lógica.
  - ``analysis_id``, ``tenant_id``, ``owner_sub``, ``status``.
  - ``item_count``, ``done_count``, ``failed_count``.
  - ``output_s3_key``: puntero al Documento_Consolidado (o ``None`` si no se
    produjo salida). **No** es un Presigned_URL: quien quiera el contenido debe
    autenticarse y pedirlo por el endpoint de descarga.
  - ``started_at`` / ``completed_at``: marcas de tiempo de inicio y fin del Lote,
    en ISO 8601 UTC (criterio 13.2).
  - ``duration_seconds``: duración del Lote, derivada de las dos marcas.

Decisiones de diseño
--------------------
- **Cero PII (criterios 13.3, 15.1).** El detalle nunca incluye ``respuesta`` ni
  ``explicacion`` ni ningún texto de encuesta, y nunca un Presigned_URL. Esto
  mantiene la PII fuera del bus, de los logs de EventBridge y de cualquier regla
  mal configurada.
- **Precondición de terminalidad.** El puerto se invoca únicamente cuando el META
  ya es terminal y su ``output_s3_key`` (si lo hay) está registrado (criterio
  13.1 y orden "registrar salida → publicar evento" de la sección 10.6). El
  adaptador no reabre esa decisión: refleja fielmente el META que recibe.
- **Resolución perezosa de la configuración.** ``EVENT_BUS_NAME`` se lee en
  tiempo de publicación, no al importar ni al construir, para no romper la
  importabilidad del paquete (criterio 17.7).
- **Cliente reutilizado (criterios 17.11, 9.18).** El cliente de EventBridge
  procede del singleton de :mod:`sentiment.adapters.aws_clients`, sin reintentos
  del SDK.
- **Detección de fallo de publicación.** ``PutEvents`` puede devolver
  ``FailedEntryCount > 0`` sin lanzar excepción (fallo parcial a nivel de
  entrada). Se inspecciona la respuesta y, si la entrada no se aceptó, se lanza
  :class:`EventPublishError` para que el llamador (o la Maquina_De_Estados)
  reaccione en lugar de dar por publicado un evento que se perdió.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol, cast

from sentiment.adapters.aws_clients import eventbridge_client
from sentiment.domain.errors import SentimentError
from sentiment.observability.config import ConfigError
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from sentiment.domain.models import AnalysisMeta

__all__ = ["EventBridgePublisher", "EventPublishError"]


class _EventBridgePutEventsClient(Protocol):
    """Superficie mínima del cliente de EventBridge que este adaptador consume.

    Se tipa contra este Protocol en lugar de ``botocore.client.BaseClient``, que
    no expone ``put_events`` sin los *extras* por servicio de ``boto3-stubs``.
    Facilita además inyectar un doble de prueba que implemente solo este método.
    """

    def put_events(
        self, *, Entries: Sequence[dict[str, str]]  # noqa: N803
    ) -> dict[str, object]:
        """Publica entradas en el bus. Firma parcial de ``boto3`` EventBridge."""
        ...

logger = get_logger(service="aggregate_results")

#: ``source`` de todos los eventos del Sistema, acotado al proyecto (sección 9.3).
_EVENT_SOURCE: Final = "sentiment.pipeline"

#: ``detail-type`` del Evento_Finalizacion (criterio 13.1).
_DETAIL_TYPE: Final = "analysis.completed"

#: Variable de entorno con el nombre del Bus_Eventos personalizado, inyectada por
#: la Infraestructura (``notifications_stack``, tarea 13.3).
_ENV_EVENT_BUS_NAME: Final = "EVENT_BUS_NAME"


class EventPublishError(SentimentError):
    """La publicación del Evento_Finalizacion no fue aceptada por EventBridge.

    Es un error interno de dependencia: ``PutEvents`` devolvió un
    ``FailedEntryCount`` mayor que 0 para la entrada del evento. No cruza la
    frontera HTTP (``http_status`` es ``None``) y permite al llamador reintentar
    la publicación (la desduplicación por ``event_id`` hace segura la reemisión).
    """

    error_code = "EVENT_PUBLISH_FAILED"
    http_status = None


def _require_event_bus_name() -> str:
    """Devuelve el nombre del Bus_Eventos desde el entorno, o lanza :class:`ConfigError`.

    Lectura perezosa (en publicación, no en import) para preservar la
    importabilidad del paquete sin la variable definida (criterio 17.7).

    :return: el nombre del bus, recortado.
    :raises ConfigError: si ``EVENT_BUS_NAME`` falta o está en blanco.
    """
    raw = os.environ.get(_ENV_EVENT_BUS_NAME)
    if raw is None or not raw.strip():
        raise ConfigError(
            f"Falta la configuración requerida '{_ENV_EVENT_BUS_NAME}'. Debe "
            f"inyectarse como variable de entorno en la Lambda que publica el "
            f"Evento_Finalizacion."
        )
    return raw.strip()


def _iso_utc(moment: datetime | None) -> str | None:
    """Formatea un instante en ISO 8601 UTC, o ``None`` si no hay instante.

    :param moment: instante a formatear, o ``None``.
    :return: la representación ISO 8601 (p. ej. ``2025-06-14T10:12:43+00:00``), o
        ``None`` si ``moment`` es ``None``.
    """
    return moment.isoformat() if moment is not None else None


def _duration_seconds(start: datetime | None, end: datetime | None) -> int | None:
    """Calcula la duración del Lote en segundos enteros, o ``None`` si falta un extremo.

    Se acota a ``0`` como mínimo para que un desajuste de relojes nunca produzca
    una duración negativa en el evento.

    :param start: instante de inicio del Lote (``created_at``).
    :param end: instante de fin del Lote (``completed_at``).
    :return: la duración en segundos (``>= 0``), o ``None`` si falta ``start`` o
        ``end``.
    """
    if start is None or end is None:
        return None
    return max(int((end - start).total_seconds()), 0)


class EventBridgePublisher:
    """Publica el Evento_Finalizacion en el Bus_Eventos (puerto ``EventPublisher``).

    Implementa estructuralmente :class:`~sentiment.domain.ports.EventPublisher`.
    El cliente de EventBridge se inyecta por constructor; por defecto, el singleton
    reutilizable del módulo de clientes.
    """

    def __init__(self, client: _EventBridgePutEventsClient | None = None) -> None:
        """Construye el adaptador.

        :param client: cliente de EventBridge a usar; si es ``None``, se toma el
            singleton de :func:`sentiment.adapters.aws_clients.eventbridge_client`.
            No se lee configuración ni se llama a AWS en la construcción.
        """
        if client is None:
            # El singleton se tipa como ``BaseClient`` (stubs sin extras por
            # servicio), que no expone ``put_events`` estáticamente; se estrecha
            # al Protocol que este adaptador realmente usa.
            client = cast("_EventBridgePutEventsClient", eventbridge_client())
        self._client: _EventBridgePutEventsClient = client

    def publish_analysis_completed(self, meta: AnalysisMeta) -> None:
        """Publica el Evento_Finalizacion de un Lote terminal en el Bus_Eventos.

        :param meta: META del Lote, ya en estado terminal y con su ``output_s3_key``
            registrado (si lo hubo). Aporta todos los identificadores, el estado y
            los recuentos del detalle del evento.
        :raises ConfigError: si ``EVENT_BUS_NAME`` no está configurada.
        :raises EventPublishError: si ``PutEvents`` no acepta la entrada del evento.

        Postcondiciones:
            - Publica una entrada con ``detail-type == "analysis.completed"`` y
              ``event_id == "{analysis_id}:{status}"`` (criterios 13.1, 10.20).
            - El detalle **no** contiene PII ni Presigned_URL; solo identificadores,
              estado, recuentos, puntero de salida y marcas de tiempo (criterios
              13.2, 13.3).
        """
        event_bus_name = _require_event_bus_name()

        status = str(meta.status.value)
        event_id = f"{meta.analysis_id}:{status}"

        detail: dict[str, object | None] = {
            "event_id": event_id,
            "analysis_id": meta.analysis_id,
            "tenant_id": meta.tenant_id,
            "owner_sub": meta.owner_sub,
            "status": status,
            "item_count": meta.item_count,
            "done_count": meta.done_count,
            "failed_count": meta.failed_count,
            "output_s3_key": meta.output_s3_key,
            "started_at": _iso_utc(meta.created_at),
            "completed_at": _iso_utc(meta.completed_at),
            "duration_seconds": _duration_seconds(
                meta.created_at, meta.completed_at
            ),
        }

        response = self._client.put_events(
            Entries=[
                {
                    "Source": _EVENT_SOURCE,
                    "DetailType": _DETAIL_TYPE,
                    "EventBusName": event_bus_name,
                    "Detail": json.dumps(
                        detail, separators=(",", ":"), ensure_ascii=False
                    ),
                }
            ]
        )

        raw_failed = response.get("FailedEntryCount", 0)
        failed = raw_failed if isinstance(raw_failed, int) else 0
        if failed > 0:
            # No se registra el detalle de la entrada fallida (nombre de error de
            # EventBridge en `response["Entries"]`) más allá del recuento, para
            # mantener el log acotado a metadatos.
            logger.error(
                "event_publish_failed",
                extra={
                    "analysis_id": meta.analysis_id,
                    "status": status,
                    "failed_entry_count": failed,
                },
            )
            raise EventPublishError(
                "EventBridge rechazó la entrada del Evento_Finalizacion.",
                analysis_id=meta.analysis_id,
                status=status,
            )

        logger.info(
            "analysis_completed_published",
            extra={
                "analysis_id": meta.analysis_id,
                "tenant_id": meta.tenant_id,
                "status": status,
                "event_id": event_id,
            },
        )
