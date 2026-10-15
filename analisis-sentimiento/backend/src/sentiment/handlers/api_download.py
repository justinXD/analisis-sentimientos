"""Lambda ``api_download``: adaptador *thin* de ``GET /v1/analyses/{analysis_id}/download``.

Este handler es el *entry point* que la Infraestructura referencia con
``handler="sentiment.handlers.api_download.handler"``. Es un adaptador de
transporte fino: traduce el evento de API Gateway (REST, integración *proxy*) en
una llamada a
:class:`~sentiment.services.issue_download_url.IssueDownloadUrlService` y la
:class:`~sentiment.services.issue_download_url.DownloadView` resultante —o el
:class:`~sentiment.domain.errors.SentimentError`— en una respuesta HTTP
(sección 12 del diseño, Requirement 12).

Garantías (criterios 1.14, 2.9, 12.1, 12.6, 15.5):

* La identidad (``tenant_id``) se deriva **exclusivamente** del contexto del
  autorizador (criterio 1.14); un ``custom:tenant_id`` ausente o en blanco es
  ``403 FORBIDDEN`` antes de leer la Dynamo_Table o el Bucket_Analisis (criterio
  2.9).
* En éxito responde ``200`` con el ``analysis_id``, el ``status``, la
  Presigned_URL y el instante de expiración ISO 8601 UTC (criterio 12.1). La
  Presigned_URL se devuelve al cliente pero **nunca** se registra en logs: es un
  secreto portador (criterio 12.10 del diseño).
* El servicio decide la precedencia de códigos: ``404 NOT_FOUND`` para un
  ``analysis_id`` malformado, inexistente, ajeno o con el objeto ya expirado
  (criterios 2.7, 12.5, 12.6), y ``409 ANALYSIS_NOT_READY`` para un Lote no
  terminal o terminal sin salida (criterios 12.3, 12.4). El handler solo traduce
  esos errores a su ``http_status`` con el cuerpo seguro (``error_code`` +
  ``correlation_id``), sin PII ni *stack traces* (criterio 15.5).

Rendimiento: el servicio se construye **una vez** por contenedor (``_SERVICE``),
fuera del handler, reutilizando los clientes singleton de DynamoDB y S3 entre
invocaciones calientes (criterio 17.11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
from sentiment.services.issue_download_url import DownloadView, IssueDownloadUrlService

__all__ = ["handler"]

logger = get_logger(service="api_download")


def _utcnow() -> datetime:
    """Instante actual en UTC. Aislado para inyectarlo como ``clock`` del servicio."""
    return datetime.now(tz=UTC)


def build_service() -> IssueDownloadUrlService:
    """Construye el :class:`IssueDownloadUrlService` con sus adaptadores de producción.

    Se invoca **una vez** en el ámbito del módulo (ver :data:`_SERVICE`): los
    adaptadores de DynamoDB y S3 comparten los clientes singleton de boto3,
    reutilizados entre invocaciones calientes (criterio 17.11). No lee
    configuración ni llama a AWS en la construcción (criterio 17.7).

    :return: una instancia lista del servicio de emisión de Presigned_URL.
    """
    return IssueDownloadUrlService(
        repository=DynamoRepository(),
        storage=S3Storage(),
        clock=_utcnow,
    )


#: Caché del servicio a nivel de módulo. Es ``None`` hasta la primera invocación.
#: La construcción se difiere a :func:`_get_service` —no al import— porque
#: ``DynamoRepository`` resuelve ``TABLE_NAME`` en su constructor; hacerla perezosa
#: preserva la importabilidad del paquete sin entorno (criterio 17.4) sin renunciar
#: a construir una sola vez por contenedor (criterio 17.11).
_SERVICE: IssueDownloadUrlService | None = None


def _get_service() -> IssueDownloadUrlService:
    """Devuelve el servicio, construyéndolo una sola vez por contenedor.

    Construcción perezosa (en la primera invocación, no al importar) y memoizada:
    reutiliza los clientes singleton de DynamoDB y S3 entre invocaciones calientes
    (criterio 17.11) sin romper la importabilidad del paquete sin entorno
    (criterio 17.4).

    :return: el :class:`IssueDownloadUrlService` del contenedor.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build_service()
    return _SERVICE


def _download_body(view: DownloadView) -> dict[str, Any]:
    """Serializa una :class:`DownloadView` al cuerpo JSON del ``200`` (criterio 12.1).

    Incluye la Presigned_URL (``download_url``) y su expiración ISO 8601 UTC. El
    enum ``status`` se emite como su valor de cadena. **No** se registra la URL en
    ningún log (secreto portador, criterio 12.10); solo se serializa hacia el
    cliente, que es su destinatario legítimo.

    :param view: la vista de descarga devuelta por el servicio.
    :return: el cuerpo serializable del 200.
    """
    return {
        "analysis_id": view.analysis_id,
        "status": str(view.status.value),
        "download_url": view.url,
        "expires_at": view.expires_at.astimezone(UTC).isoformat(),
    }


def _path_analysis_id(event: dict[str, Any]) -> str:
    """Extrae el parámetro de ruta ``analysis_id`` del evento.

    Un ``analysis_id`` ausente o malformado no se distingue aquí de uno inexistente:
    se devuelve ``""`` (o el valor crudo), que el servicio valida como ULID y trata
    como ``404`` idéntico al de un recurso inexistente (criterio 2.7).

    :param event: evento de API Gateway (integración *proxy*).
    :return: el ``analysis_id`` de la ruta, o ``""`` si no está presente.
    """
    params = event.get("pathParameters")
    if not isinstance(params, dict):
        return ""
    value = params.get("analysis_id")
    return value if isinstance(value, str) else ""


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Adaptador HTTP de ``GET /v1/analyses/{analysis_id}/download``. Sin lógica de negocio.

    Resuelve el ``correlation_id``, deriva la identidad del autorizador (criterios
    1.14, 2.9) e invoca el servicio con el ``analysis_id`` de la ruta y el
    ``tenant_id`` del token. Traduce la vista a ``200`` o el error a su respuesta
    HTTP sin PII (criterios 12.1, 15.5).

    :param event: evento de API Gateway (integración *proxy*).
    :param context: contexto de ejecución de Lambda (no usado; parte de la firma).
    :return: la respuesta de integración *proxy* ``{statusCode, headers, body}``.
    """
    correlation_id = resolve_correlation_id(event)

    try:
        identity = extract_identity(event)
        analysis_id = _path_analysis_id(event)
        view = _get_service().issue(analysis_id, identity.tenant_id)
    except Exception as exc:
        return _handle_exception(exc, correlation_id)

    # Metadatos no sensibles; NUNCA la Presigned_URL (criterio 12.10).
    logger.info(
        "download_url_issued",
        extra={
            "analysis_id": view.analysis_id,
            "tenant_id": identity.tenant_id,
            "status": str(view.status.value),
            "correlation_id": correlation_id,
        },
    )
    return json_response(200, _download_body(view))


def _handle_exception(exc: Exception, correlation_id: str) -> dict[str, Any]:
    """Traduce una excepción del flujo de descarga a una respuesta HTTP sin PII.

    Un :class:`SentimentError` (403, 404, 409) se traduce a su ``http_status`` con
    el cuerpo seguro; cualquier otra excepción es un fallo de infraestructura:
    ``500 INTERNAL_ERROR`` con la traza solo en el log (criterio 15.5).

    :param exc: la excepción capturada en el handler.
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta de integración *proxy* con el cuerpo de error.
    """
    if isinstance(exc, SentimentError):
        logger.warning(
            "download_rejected",
            extra={
                "error_code": exc.error_code,
                "correlation_id": correlation_id,
                **exc.detail,
            },
        )
        return error_response(exc, correlation_id)

    logger.exception("download_unhandled", extra={"correlation_id": correlation_id})
    return internal_error_response(correlation_id)
