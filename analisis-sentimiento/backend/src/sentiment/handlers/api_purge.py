"""Lambda ``api_purge``: adaptador *thin* de ``DELETE /v1/analyses/{analysis_id}``.

Este handler es el *entry point* que la Infraestructura referencia con
``handler="sentiment.handlers.api_purge.handler"``. Es un adaptador de transporte
fino: traduce el evento de API Gateway (REST, integración *proxy*) en un
:class:`~sentiment.services.purge_analysis.PurgeCommand`, invoca
:class:`~sentiment.services.purge_analysis.PurgeAnalysisService` y traduce el
:class:`~sentiment.services.purge_analysis.PurgeResult` resultante —o el
:class:`~sentiment.domain.errors.SentimentError`— en una respuesta HTTP
(sección 16.7 del diseño, Requirement 19).

El scope ``sentiment/analyses.purge`` lo valida API Gateway antes de invocar la
Lambda (criterio 19.2); el handler no reabre esa comprobación.

Garantías (criterios 1.14, 2.9, 19.1, 19.5, 19.6, 15.5):

* La identidad (``tenant_id`` y ``principal_sub``) se deriva **exclusivamente**
  del contexto del autorizador (criterio 1.14); un ``custom:tenant_id`` o ``sub``
  ausente o en blanco es ``403 FORBIDDEN`` antes de borrar nada (criterio 2.9).
* En éxito responde ``200`` con el ``analysis_id`` y el instante de finalización
  de la Purga en ISO 8601 UTC (criterio 19.1).
* El servicio decide la precedencia: ``404 NOT_FOUND`` para un ``analysis_id``
  malformado, inexistente o ajeno (criterios 19.5, 2.7) —incluida una
  reejecución sobre un Lote ya purgado, cuyo META ya no existe—, y ``409
  ANALYSIS_NOT_READY`` para un Lote propio no terminal (criterio 19.6). El handler
  solo los traduce a su ``http_status`` con el cuerpo seguro (``error_code`` +
  ``correlation_id``), sin PII ni *stack traces* (criterio 15.5).

Rendimiento: el servicio se construye **una vez** por contenedor (``_SERVICE``),
fuera del handler, reutilizando los clientes singleton de DynamoDB y S3 entre
invocaciones calientes (criterio 17.11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sentiment.adapters.audit_log import AuditLog
from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.adapters.s3_storage import S3Storage
from sentiment.domain.errors import SentimentError
from sentiment.handlers._http import (
    error_response,
    extract_identity,
    internal_error_response,
    json_response,
    resolve_correlation_id,
)
from sentiment.observability.logging import get_logger
from sentiment.services.purge_analysis import PurgeAnalysisService, PurgeCommand, PurgeResult

__all__ = ["handler"]

logger = get_logger(service="api_purge")


def _utcnow() -> datetime:
    """Instante actual en UTC. Aislado para inyectarlo como ``clock`` del servicio."""
    return datetime.now(tz=UTC)


def build_service() -> PurgeAnalysisService:
    """Construye el :class:`PurgeAnalysisService` con sus adaptadores de producción.

    Se invoca **una vez** en el ámbito del módulo (ver :data:`_SERVICE`): los
    adaptadores de DynamoDB (puerto de purga), S3 (puerto de purga) y el logger de
    auditoría comparten los clientes singleton de boto3, reutilizados entre
    invocaciones calientes (criterio 17.11). No lee configuración ni llama a AWS
    en la construcción (criterio 17.7).

    :return: una instancia lista del servicio de purga.
    """
    return PurgeAnalysisService(
        repository=DynamoRepository(),
        storage=S3Storage(),
        audit=AuditLog(),
        clock=_utcnow,
    )


#: Caché del servicio a nivel de módulo. Es ``None`` hasta la primera invocación.
#: La construcción se difiere a :func:`_get_service` —no al import— porque
#: ``DynamoRepository`` resuelve ``TABLE_NAME`` en su constructor; hacerla perezosa
#: preserva la importabilidad del paquete sin entorno (criterio 17.4) sin renunciar
#: a construir una sola vez por contenedor (criterio 17.11).
_SERVICE: PurgeAnalysisService | None = None


def _get_service() -> PurgeAnalysisService:
    """Devuelve el servicio, construyéndolo una sola vez por contenedor.

    Construcción perezosa (en la primera invocación, no al importar) y memoizada:
    reutiliza los clientes singleton de DynamoDB y S3 entre invocaciones calientes
    (criterio 17.11) sin romper la importabilidad del paquete sin entorno
    (criterio 17.4).

    :return: el :class:`PurgeAnalysisService` del contenedor.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build_service()
    return _SERVICE


def _purge_body(result: PurgeResult) -> dict[str, Any]:
    """Serializa un :class:`PurgeResult` al cuerpo JSON del ``200`` (criterio 19.1).

    Expone solo el ``analysis_id`` y el ``purged_at`` en ISO 8601 UTC: nunca PII
    (``respuesta``, ``explicacion``, correos) ni la Presigned_URL (criterio 19.10).

    :param result: el resultado de la purga completada.
    :return: el cuerpo serializable del 200.
    """
    return {
        "analysis_id": result.analysis_id,
        "purged_at": result.purged_at.astimezone(UTC).isoformat(),
    }


def _path_analysis_id(event: dict[str, Any]) -> str:
    """Extrae el parámetro de ruta ``analysis_id`` del evento.

    Un ``analysis_id`` ausente o malformado se devuelve como ``""`` (o el valor
    crudo), que el servicio valida como ULID y trata como ``404`` idéntico al de un
    recurso inexistente (criterios 19.5, 2.7).

    :param event: evento de API Gateway (integración *proxy*).
    :return: el ``analysis_id`` de la ruta, o ``""`` si no está presente.
    """
    params = event.get("pathParameters")
    if not isinstance(params, dict):
        return ""
    value = params.get("analysis_id")
    return value if isinstance(value, str) else ""


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Adaptador HTTP de ``DELETE /v1/analyses/{analysis_id}``. Sin lógica de negocio.

    Resuelve el ``correlation_id``, deriva la identidad del autorizador (criterios
    1.14, 2.9), construye el :class:`PurgeCommand` con el ``analysis_id`` de la
    ruta, el ``tenant_id`` y el ``sub`` del token, e invoca el servicio. Traduce el
    resultado a ``200`` o el error a su respuesta HTTP sin PII (criterios 19.1,
    15.5).

    :param event: evento de API Gateway (integración *proxy*).
    :param context: contexto de ejecución de Lambda (no usado; parte de la firma).
    :return: la respuesta de integración *proxy* ``{statusCode, headers, body}``.
    """
    correlation_id = resolve_correlation_id(event)

    try:
        identity = extract_identity(event)
        command = PurgeCommand(
            analysis_id=_path_analysis_id(event),
            tenant_id=identity.tenant_id,
            principal_sub=identity.principal_sub,
            correlation_id=correlation_id,
        )
        result = _get_service().execute(command)
    except Exception as exc:
        return _handle_exception(exc, correlation_id)

    logger.info(
        "purge_served",
        extra={
            "analysis_id": result.analysis_id,
            "tenant_id": identity.tenant_id,
            "correlation_id": correlation_id,
        },
    )
    return json_response(200, _purge_body(result))


def _handle_exception(exc: Exception, correlation_id: str) -> dict[str, Any]:
    """Traduce una excepción del flujo de purga a una respuesta HTTP sin PII.

    Un :class:`SentimentError` (403, 404, 409) se traduce a su ``http_status`` con
    el cuerpo seguro; cualquier otra excepción es un fallo de infraestructura
    durante el borrado: ``500 INTERNAL_ERROR`` con la traza solo en el log
    (criterio 15.5). En ese 500 el META sigue existiendo y la purga es
    reintentable (criterio 19.7); el handler no necesita hacer nada especial, solo
    no filtrar el detalle.

    :param exc: la excepción capturada en el handler.
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta de integración *proxy* con el cuerpo de error.
    """
    if isinstance(exc, SentimentError):
        logger.warning(
            "purge_rejected",
            extra={
                "error_code": exc.error_code,
                "correlation_id": correlation_id,
                **exc.detail,
            },
        )
        return error_response(exc, correlation_id)

    logger.exception("purge_unhandled", extra={"correlation_id": correlation_id})
    return internal_error_response(correlation_id)
