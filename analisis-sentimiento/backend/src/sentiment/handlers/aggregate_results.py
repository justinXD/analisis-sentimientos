"""Handler de transporte de la Lambda ``aggregate_results`` (tarea 9.2).

Adaptador **thin** (sección 2.1 del diseño) entre el estado posterior al
Distributed_Map de Step Functions y el caso de uso puro
:class:`~sentiment.services.aggregate_analysis.AggregateAnalysisService`. No
contiene lógica de negocio: el recuento desde los Ítems reales, el cierre de
huérfanos, la decisión del Estado_Terminal, la escritura del Documento_Consolidado
y la publicación del Evento_Finalizacion viven en el servicio (sección 16.4).

Forma del evento de entrada (``AggregateInput``, sección 15.4)::

    {
      "analysis_id": "01JDQ...",
      "tenant_id": "acme-corp",
      "execution_arn": "arn:aws:states:...:execution:sentiment-analysis:01JDQ...",
      "map_result": { "ResultWriterDetails": { "Bucket": "...", "Key": "..." } }
    }

El handler solo necesita ``analysis_id`` y ``tenant_id``; ``execution_arn`` y
``map_result`` forman parte del contrato del estado pero no los consume este caso
de uso (el servicio recuenta desde la verdad en DynamoDB, no desde el manifiesto
del Map).

Resultado
---------
Devuelve el Estado_Terminal del Lote como ``{"analysis_id": ..., "status": ...}``,
que el estado ``Succeed`` del workflow padre expone como salida de la ejecución.

Propagación de errores (criterios 5.11, 9.18)
---------------------------------------------
El handler **no** captura :class:`~sentiment.domain.errors.AggregationError`. Si
la lectura de Ítems no recupera los ``item_count`` registros, el servicio la
propaga y aquí **sube sin captura** a la Maquina_De_Estados: su ``Retry`` reintenta
la agregación (3 intentos, criterio 9.11) y, agotados, el ``Catch`` →
``MarkFailed`` cierra el META como ``FAILED`` con ``error_code = AGGREGATION_FAILED``
y publica el Evento_Finalizacion (criterio 10.18). ``ErrorEquals`` identifica la
condición por el **nombre** de la clase, igual que en el resto del pipeline.

Reutilización por contenedor (criterios 9.18, 17.11)
----------------------------------------------------
El servicio y sus adaptadores se construyen **una sola vez** en el ámbito del
módulo, para reutilizar los clientes de boto3 entre invocaciones calientes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.adapters.eventbridge_publisher import EventBridgePublisher
from sentiment.adapters.s3_storage import S3Storage
from sentiment.observability.logging import get_logger
from sentiment.services.aggregate_analysis import AggregateAnalysisService

__all__ = ["handler"]

logger = get_logger(service="aggregate_results")


def _utc_now() -> datetime:
    """Reloj del servicio: instante UTC consciente de zona para ``completed_at``.

    Se inyecta en el servicio en lugar de usar ``datetime.now`` directamente para
    conservar la frontera hexagonal (el servicio no conoce el reloj del sistema) y
    mantener el handler como el único punto que ata el caso de uso al tiempo real.

    :return: el instante actual en UTC, consciente de zona.
    """
    return datetime.now(UTC)


#: Caché del servicio a nivel de módulo. Es ``None`` hasta la primera invocación:
#: así importar este módulo nunca construye los adaptadores ni resuelve
#: configuración (``DynamoRepository`` resuelve ``TABLE_NAME`` en su constructor),
#: preservando la importabilidad del paquete sin entorno (criterio 17.4).
_SERVICE: AggregateAnalysisService | None = None


def _get_service() -> AggregateAnalysisService:
    """Devuelve el servicio, construyéndolo una sola vez por contenedor.

    La construcción es perezosa (en la primera invocación, no al importar) y
    memoizada: las invocaciones calientes del mismo contenedor reutilizan el
    servicio y, con él, los clientes singleton de boto3 y sus conexiones TLS
    (criterios 9.18, 17.11). Importar el módulo no toca AWS ni la configuración.

    :return: el :class:`AggregateAnalysisService` del contenedor.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = AggregateAnalysisService(
            repository=DynamoRepository(),
            storage=S3Storage(),
            events=EventBridgePublisher(),
            clock=_utc_now,
        )
    return _SERVICE


def handler(event: dict[str, Any], _context: object = None) -> dict[str, Any]:
    """Punto de entrada de la Lambda ``aggregate_results``.

    Extrae ``analysis_id`` y ``tenant_id`` del evento, invoca el caso de uso y
    devuelve el Estado_Terminal del Lote. No captura ``AggregationError``: sube sin
    modificar a la Maquina_De_Estados para su ``Retry``/``Catch`` → ``MarkFailed``
    (criterios 9.11, 10.18, 9.18).

    :param event: el evento ``AggregateInput`` (sección 15.4) con ``analysis_id``,
        ``tenant_id`` y, opcionalmente, ``execution_arn``/``map_result`` (no
        consumidos por este caso de uso).
    :param _context: contexto de Lambda; no se usa (el handler es thin).
    :return: ``{"analysis_id": str, "status": str}`` con el Estado_Terminal.
    :raises AggregationError: si la lectura de Ítems no recupera los ``item_count``
        registros esperados (``AGGREGATION_FAILED``, criterio 9.11); propagada sin
        captura por el servicio para que la Maquina_De_Estados la reintente y,
        agotados los reintentos, cierre el META como ``FAILED``.
    """
    analysis_id = str(event["analysis_id"])
    tenant_id = str(event["tenant_id"])

    logger.append_keys(analysis_id=analysis_id)
    logger.info("aggregate_received", extra={"tenant_id": tenant_id})

    # El servicio recuenta desde la verdad, cierra huérfanos, escribe el
    # Documento_Consolidado, cierra el META y publica el evento. AggregationError
    # NO se captura: sube para el Retry/Catch de la Maquina_De_Estados (9.11, 9.18).
    meta = _get_service().execute(analysis_id, tenant_id)

    status = meta.status.value
    logger.info("aggregate_completed", extra={"status": status})
    return {"analysis_id": analysis_id, "status": status}
