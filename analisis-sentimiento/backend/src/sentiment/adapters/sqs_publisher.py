"""Adaptador de la Cola_Solicitudes (SQS): implementación del puerto ``QueuePublisher``.

Este adaptador materializa el puerto :class:`~sentiment.domain.ports.QueuePublisher`
(ver su contrato normativo en ``domain/ports.py``) publicando en la
Cola_Solicitudes el mensaje ``AnalysisQueued`` que, vía EventBridge Pipes, dispara
la ejecución de la Maquina_De_Estados. Encolar es **la última escritura de la
ingesta** (criterios 4.8 y 10.8): cuando el mensaje existe en la cola, S3 y
DynamoDB ya están escritos y la orquestación nunca leerá un Lote incompleto.

Contrato del mensaje (sección 9 del diseño, ``AnalysisQueued``)
---------------------------------------------------------------
El cuerpo del mensaje es un objeto JSON con exclusivamente **punteros y
metadatos**, nunca el texto de las respuestas (criterio 4.8; sin PII, igual que
el resto del pipeline):

- ``schema_version``: versión del esquema del mensaje, para evolución futura.
- ``analysis_id``: ULID del Lote (también el nombre de la ejecución de Step
  Functions, por lo que EventBridge Pipes lo usa para la desduplicación del
  arranque, criterio 5.14).
- ``tenant_id``: organización propietaria; frontera de aislamiento.
- ``owner_sub``: ``sub`` del usuario que creó el Lote.
- ``input_s3_bucket`` / ``input_s3_key``: ubicación del payload canonicalizado de
  entrada que el ``ItemReader`` del Distributed_Map leerá.
- ``item_count``: número de Ítems del Lote.
- ``correlation_id``: identificador de correlación para la trazabilidad extremo a
  extremo (criterio 15.4 del diseño).

Decisiones de diseño
--------------------
- **Cero PII (criterios 4.8, 15.1).** El texto de ``respuesta`` nunca viaja por la
  cola; solo la clave de S3 donde reside. El log de confirmación emite únicamente
  identificadores y recuentos, nunca el cuerpo del mensaje crudo (que, aun sin
  PII, se evita loguear como payload completo, criterio 15.2).
- **Resolución perezosa de la configuración.** La URL de la cola
  (``QUEUE_URL``) y el nombre del bucket (``BUCKET_NAME``) se leen del entorno la
  **primera vez que se publica**, no al construir el adaptador ni al importar el
  módulo, de modo que el paquete siga siendo importable sin esas variables
  (criterio 17.7) y el recorrido de importabilidad no falle.
- **Cliente reutilizado (criterios 17.11, 9.18).** El cliente de SQS procede del
  singleton de :mod:`sentiment.adapters.aws_clients`, con cero reintentos del SDK
  y conexión reutilizada entre invocaciones calientes.
- **Fallo explícito.** Si ``QUEUE_URL`` o ``BUCKET_NAME`` faltan, se lanza
  :class:`~sentiment.observability.config.ConfigError` con un mensaje accionable
  que nombra la clave ausente, nunca un ``KeyError`` opaco.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Final, Protocol, cast

from sentiment.adapters.aws_clients import sqs_client
from sentiment.observability.config import ConfigError
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from sentiment.domain.models import AnalysisMeta

__all__ = ["SqsPublisher"]


class _SqsSendMessageClient(Protocol):
    """Superficie mínima del cliente de SQS que este adaptador consume.

    Tipar contra este Protocol (y no contra ``botocore.client.BaseClient``, que
    no expone métodos por servicio sin los *extras* de ``boto3-stubs``) mantiene
    la verificación estricta de tipos y, de paso, facilita inyectar un doble de
    prueba que implemente solo este método.
    """

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> object:  # noqa: N803
        """Envía un mensaje a la cola indicada. Firma parcial de ``boto3`` SQS."""
        ...

logger = get_logger(service="api_submit")

#: Variable de entorno con la URL de la Cola_Solicitudes, inyectada por la
#: Infraestructura (``ingest_stack``, tarea 12.1).
_ENV_QUEUE_URL: Final = "QUEUE_URL"

#: Variable de entorno con el nombre del Bucket_Analisis, inyectada por la
#: Infraestructura (``storage_stack``). El mensaje transporta el bucket además de
#: la clave para que el consumidor (Distributed_Map ``ItemReader``) no tenga que
#: resolverlo por su cuenta.
_ENV_BUCKET_NAME: Final = "BUCKET_NAME"

#: Versión del esquema del cuerpo del mensaje ``AnalysisQueued`` (sección 9 del
#: diseño). Se incrementa si el contrato del mensaje cambia de forma incompatible.
_MESSAGE_SCHEMA_VERSION: Final = 1


def _require_env(name: str) -> str:
    """Devuelve una variable de entorno no vacía, o lanza :class:`ConfigError`.

    La lectura es perezosa (en tiempo de publicación, no de import) para no romper
    la importabilidad del paquete cuando la variable no está definida
    (criterio 17.7).

    :param name: nombre de la variable de entorno requerida.
    :return: el valor recortado de la variable.
    :raises ConfigError: si la variable falta o está en blanco.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raise ConfigError(
            f"Falta la configuración requerida '{name}'. Debe inyectarse como "
            f"variable de entorno en la Lambda de ingesta."
        )
    return raw.strip()


class SqsPublisher:
    """Publica el mensaje ``AnalysisQueued`` en la Cola_Solicitudes (puerto ``QueuePublisher``).

    Implementa estructuralmente :class:`~sentiment.domain.ports.QueuePublisher`.
    El cliente de SQS se inyecta por constructor (inversión de dependencias): por
    defecto, el singleton reutilizable del módulo de clientes; las pruebas pueden
    inyectar un cliente apuntando a un simulador (``moto``).
    """

    def __init__(self, client: _SqsSendMessageClient | None = None) -> None:
        """Construye el adaptador.

        :param client: cliente de SQS a usar; si es ``None``, se toma el singleton
            de :func:`sentiment.adapters.aws_clients.sqs_client`. No se lee
            configuración ni se llama a AWS en la construcción.
        """
        if client is None:
            # El singleton se tipa como ``BaseClient`` (stubs sin extras por
            # servicio), que no expone ``send_message`` estáticamente; se
            # estrecha al Protocol que este adaptador realmente usa.
            client = cast("_SqsSendMessageClient", sqs_client())
        self._client: _SqsSendMessageClient = client

    def publish_analysis_queued(
        self, meta: AnalysisMeta, correlation_id: str
    ) -> None:
        """Encola un mensaje ``AnalysisQueued`` que referencia un Lote ya persistido.

        Es la última escritura de la ingesta: debe invocarse solo después de que
        S3 y DynamoDB estén escritos (criterio 4.8), de modo que la orquestación
        nunca lea un Lote incompleto.

        :param meta: META del Lote recién creado; aporta ``analysis_id``,
            ``tenant_id``, ``owner_sub``, la clave del input (``input_s3_key``) y
            el ``item_count``.
        :param correlation_id: identificador de correlación para la trazabilidad
            extremo a extremo del Lote.
        :raises ConfigError: si ``QUEUE_URL`` o ``BUCKET_NAME`` no están
            configuradas.

        Postcondiciones:
            - Tras retornar, existe exactamente un mensaje en la Cola_Solicitudes
              que referencia ``meta.analysis_id``, con ``tenant_id``, bucket/clave
              del input, ``item_count`` y ``correlation_id``.
            - El mensaje **no** contiene PII: solo punteros y metadatos.
        """
        queue_url = _require_env(_ENV_QUEUE_URL)
        bucket_name = _require_env(_ENV_BUCKET_NAME)

        body: dict[str, object] = {
            "schema_version": _MESSAGE_SCHEMA_VERSION,
            "analysis_id": meta.analysis_id,
            "tenant_id": meta.tenant_id,
            "owner_sub": meta.owner_sub,
            "input_s3_bucket": bucket_name,
            "input_s3_key": meta.input_s3_key,
            "item_count": meta.item_count,
            "correlation_id": correlation_id,
        }

        self._client.send_message(
            QueueUrl=queue_url,
            # `separators` compacta el JSON (sin espacios superfluos) y
            # `ensure_ascii=False` preserva cualquier carácter no ASCII de los
            # identificadores sin escaparlo innecesariamente.
            MessageBody=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
        )

        # Confirmación con solo metadatos no sensibles (criterio 15.2): nunca el
        # cuerpo crudo del mensaje.
        logger.info(
            "analysis_queued",
            extra={
                "analysis_id": meta.analysis_id,
                "tenant_id": meta.tenant_id,
                "item_count": meta.item_count,
                "correlation_id": correlation_id,
            },
        )
