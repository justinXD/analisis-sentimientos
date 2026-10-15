"""Lambda ``api_status``: adaptador *thin* de ``GET /v1/analyses[/{analysis_id}]``.

Este handler es el *entry point* que la Infraestructura referencia con
``handler="sentiment.handlers.api_status.handler"``. Es un adaptador de
transporte fino, de **solo lectura**: traduce el evento de API Gateway (REST,
integración *proxy*) en una llamada a
:class:`~sentiment.services.get_analysis_status.GetAnalysisStatusService` y la
vista resultante —o el :class:`~sentiment.domain.errors.SentimentError`— en una
respuesta HTTP (sección 16.5/16.6 del diseño).

Enruta por la presencia del parámetro de ruta ``analysis_id``:

* ``GET /v1/analyses`` (sin ``analysis_id``) → listado paginado de los Lotes del
  Tenant (:meth:`GetAnalysisStatusService.list_analyses`), con los parámetros de
  consulta ``page_size`` y ``cursor`` (criterios 11.7, 11.11-11.13).
* ``GET /v1/analyses/{analysis_id}`` → estado de un Lote
  (:meth:`GetAnalysisStatusService.get_status`), con el parámetro de consulta
  ``include_failures`` (criterios 11.1, 11.5).

Garantías (criterios 1.14, 2.9, 2.2, 11.1, 11.10, 15.5):

* La identidad (``tenant_id``) se deriva **exclusivamente** del contexto del
  autorizador (criterio 1.14); un ``custom:tenant_id`` ausente o en blanco es
  ``403 FORBIDDEN`` antes de leer la Dynamo_Table (criterio 2.9).
* Un ``analysis_id`` inexistente, de otro Tenant o malformado produce el **mismo**
  ``404 NOT_FOUND`` con cuerpo idéntico (``error_code`` + ``correlation_id``),
  cerrando el vector de enumeración entre Tenants (criterios 2.2, 2.7, 11.10). El
  aislamiento es estructural: el servicio acota la lectura por ``tenant_id``.
* La respuesta expone solo metadatos: nunca el texto de ``respuesta`` ni de
  ``explicacion`` (criterios 11.5, 11.6). El detalle de Ítems fallidos opcional
  contiene solo ``item_id`` + ``error_code``.

Rendimiento: el servicio se construye **una vez** por contenedor (``_SERVICE``),
fuera del handler, reutilizando el cliente singleton de DynamoDB entre
invocaciones calientes (criterio 17.11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.domain.errors import SentimentError, ValidationError
from sentiment.handlers._http import (
    error_response,
    extract_identity,
    internal_error_response,
    json_response,
    resolve_correlation_id,
)
from sentiment.observability.logging import get_logger
from sentiment.services.get_analysis_status import (
    AnalysisListView,
    GetAnalysisStatusService,
    StatusView,
)

if TYPE_CHECKING:
    from sentiment.handlers._http import Identity

__all__ = ["handler"]

logger = get_logger(service="api_status")


def build_service() -> GetAnalysisStatusService:
    """Construye el :class:`GetAnalysisStatusService` con su repositorio de producción.

    Se invoca **una vez** en el ámbito del módulo (ver :data:`_SERVICE`): el
    adaptador de DynamoDB comparte el cliente singleton de boto3, reutilizado
    entre invocaciones calientes (criterio 17.11). No lee configuración ni llama a
    AWS en la construcción (criterio 17.7).

    :return: una instancia lista del servicio de consulta de estado.
    """
    return GetAnalysisStatusService(repository=DynamoRepository())


#: Caché del servicio a nivel de módulo. Es ``None`` hasta la primera invocación.
#: La construcción se difiere a :func:`_get_service` —no al import— porque
#: ``DynamoRepository`` resuelve ``TABLE_NAME`` en su constructor; hacerla perezosa
#: preserva la importabilidad del paquete sin entorno (criterio 17.4) sin renunciar
#: a construir una sola vez por contenedor (criterio 17.11).
_SERVICE: GetAnalysisStatusService | None = None


def _get_service() -> GetAnalysisStatusService:
    """Devuelve el servicio, construyéndolo una sola vez por contenedor.

    Construcción perezosa (en la primera invocación, no al importar) y memoizada:
    reutiliza el cliente singleton de DynamoDB entre invocaciones calientes
    (criterio 17.11) sin romper la importabilidad del paquete sin entorno
    (criterio 17.4).

    :return: el :class:`GetAnalysisStatusService` del contenedor.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build_service()
    return _SERVICE


def _iso(value: datetime | None) -> str | None:
    """Serializa un ``datetime`` a ISO 8601 UTC, o ``None`` si no hay instante.

    :param value: un ``datetime`` consciente de zona, o ``None``.
    :return: la representación ISO 8601 en UTC, o ``None``.
    """
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _status_body(view: StatusView) -> dict[str, Any]:
    """Serializa una :class:`StatusView` al cuerpo JSON de ``GET /{analysis_id}``.

    Expone solo metadatos (criterios 11.1, 11.5, 11.6): contadores, progreso,
    marcas de tiempo, ``download_available`` y el detalle opcional de Ítems
    fallidos (``item_id`` + ``error_code``, sin PII). El enum ``status`` se emite
    como su valor de cadena.

    :param view: la vista de estado devuelta por el servicio.
    :return: el cuerpo serializable del 200.
    """
    return {
        "analysis_id": view.analysis_id,
        "status": str(view.status.value),
        "item_count": view.item_count,
        "done_count": view.done_count,
        "failed_count": view.failed_count,
        "skipped_count": view.skipped_count,
        "progress": view.progress,
        "created_at": _iso(view.created_at),
        "completed_at": _iso(view.completed_at),
        "download_available": view.download_available,
        "failures": [
            {"item_id": failure.item_id, "error_code": failure.error_code}
            for failure in view.failures
        ],
    }


def _list_body(view: AnalysisListView) -> dict[str, Any]:
    """Serializa una :class:`AnalysisListView` al cuerpo JSON de ``GET /v1/analyses``.

    Devuelve la página de resúmenes (vacía si el Tenant no tiene Lotes, criterio
    2.4), el cursor opaco de continuación (``None`` si no hay más) y el
    ``page_size`` efectivamente aplicado (criterio 11.11).

    :param view: la vista de listado devuelta por el servicio.
    :return: el cuerpo serializable del 200.
    """
    return {
        "analyses": [
            {
                "analysis_id": summary.analysis_id,
                "status": str(summary.status.value),
                "item_count": summary.item_count,
                "done_count": summary.done_count,
                "failed_count": summary.failed_count,
                "skipped_count": summary.skipped_count,
                "progress": summary.progress,
                "created_at": _iso(summary.created_at),
                "completed_at": _iso(summary.completed_at),
                "download_available": summary.download_available,
            }
            for summary in view.analyses
        ],
        "next_cursor": view.next_cursor,
        "page_size": view.page_size,
    }


def _path_analysis_id(event: dict[str, Any]) -> str | None:
    """Extrae el parámetro de ruta ``analysis_id`` del evento, o ``None``.

    Un valor presente enruta hacia la consulta de un Lote; su ausencia, hacia el
    listado. Un mapa de parámetros de ruta ausente o nulo (``GET /v1/analyses``)
    devuelve ``None``.

    :param event: evento de API Gateway (integración *proxy*).
    :return: el ``analysis_id`` de la ruta, o ``None`` si no está presente.
    """
    params = event.get("pathParameters")
    if not isinstance(params, dict):
        return None
    value = params.get("analysis_id")
    return value if isinstance(value, str) else None


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    """Devuelve el mapa de parámetros de consulta del evento, nunca ``None``.

    :param event: evento de API Gateway (integración *proxy*).
    :return: el mapa de parámetros de consulta, o uno vacío si no hay ninguno.
    """
    params = event.get("queryStringParameters")
    if isinstance(params, dict):
        return {k: v for k, v in params.items() if isinstance(v, str)}
    return {}


def _parse_include_failures(query: dict[str, str]) -> bool:
    """Interpreta el parámetro de consulta ``include_failures`` (criterio 11.5).

    Solo el valor exacto ``"true"`` (insensible a mayúsculas) activa el detalle de
    fallos; cualquier otro valor o su ausencia lo desactiva.

    :param query: mapa de parámetros de consulta.
    :return: ``True`` si ``include_failures`` vale ``true``; ``False`` si no.
    """
    return query.get("include_failures", "").strip().lower() == "true"


def _parse_page_size(query: dict[str, str]) -> int | None:
    """Interpreta el parámetro de consulta ``page_size`` (criterios 11.11, 11.12).

    Un ``page_size`` ausente devuelve ``None`` (el servicio aplica el valor por
    defecto). Un valor que no es un entero decimal se rechaza como
    ``VALIDATION_FAILED`` (criterio 11.12); el servicio valida además el rango
    (``>= 1``) y el tope (criterio 11.11).

    :param query: mapa de parámetros de consulta.
    :return: el ``page_size`` solicitado como entero, o ``None`` si no se envió.
    :raises ValidationError: si ``page_size`` está presente pero no es un entero
        decimal (``VALIDATION_FAILED``, criterio 11.12).
    """
    raw = query.get("page_size")
    if raw is None or not raw.strip():
        return None
    trimmed = raw.strip()
    # `str.isdigit` acota a enteros decimales no negativos sin signo; descarta
    # "-1", "1.5", "abc" y notación con espacios, que el criterio 11.12 rechaza.
    if not trimmed.isdigit():
        raise ValidationError(reason="page_size_not_integer")
    return int(trimmed)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Adaptador HTTP de ``GET /v1/analyses[/{analysis_id}]``. Sin lógica de negocio.

    Resuelve el ``correlation_id``, deriva la identidad del autorizador (criterios
    1.14, 2.9) y enruta según la presencia de ``analysis_id`` en la ruta. Traduce
    la vista a ``200`` o el error a su respuesta HTTP sin PII (criterios 11.10,
    15.5).

    :param event: evento de API Gateway (integración *proxy*).
    :param context: contexto de ejecución de Lambda (no usado; parte de la firma).
    :return: la respuesta de integración *proxy* ``{statusCode, headers, body}``.
    """
    correlation_id = resolve_correlation_id(event)

    try:
        identity = extract_identity(event)
        analysis_id = _path_analysis_id(event)
        if analysis_id is None:
            return _handle_list(event, identity, correlation_id)
        return _handle_status(event, identity, analysis_id, correlation_id)
    except Exception as exc:
        return _handle_exception(exc, correlation_id)


def _handle_status(
    event: dict[str, Any],
    identity: Identity,
    analysis_id: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Resuelve ``GET /v1/analyses/{analysis_id}`` (estado de un Lote).

    :param event: evento de API Gateway.
    :param identity: identidad verificada del solicitante.
    :param analysis_id: ``analysis_id`` de la ruta (puede estar malformado: el
        servicio lo valida y lo trata como 404 si no es un ULID, criterio 2.7).
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta HTTP 200 con el estado del Lote.
    """
    include_failures = _parse_include_failures(_query_params(event))
    view = _get_service().get_status(
        analysis_id, identity.tenant_id, include_failures=include_failures
    )
    logger.info(
        "status_served",
        extra={
            "analysis_id": view.analysis_id,
            "tenant_id": identity.tenant_id,
            "status": str(view.status.value),
            "correlation_id": correlation_id,
        },
    )
    return json_response(200, _status_body(view))


def _handle_list(
    event: dict[str, Any], identity: Identity, correlation_id: str
) -> dict[str, Any]:
    """Resuelve ``GET /v1/analyses`` (listado paginado de los Lotes del Tenant).

    :param event: evento de API Gateway.
    :param identity: identidad verificada del solicitante.
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta HTTP 200 con la página de Lotes.
    """
    query = _query_params(event)
    page_size = _parse_page_size(query)
    cursor = query.get("cursor")
    view = _get_service().list_analyses(
        identity.tenant_id, page_size=page_size, cursor=cursor
    )
    logger.info(
        "list_served",
        extra={
            "tenant_id": identity.tenant_id,
            "count": len(view.analyses),
            "page_size": view.page_size,
            "correlation_id": correlation_id,
        },
    )
    return json_response(200, _list_body(view))


def _handle_exception(exc: Exception, correlation_id: str) -> dict[str, Any]:
    """Traduce una excepción del flujo de consulta a una respuesta HTTP sin PII.

    Un :class:`SentimentError` (403, 404, o ``VALIDATION_FAILED`` de ``page_size``
    / cursor) se traduce a su ``http_status`` con el cuerpo seguro; cualquier otra
    excepción es un fallo de infraestructura: ``500 INTERNAL_ERROR`` con la traza
    solo en el log (criterio 15.5).

    :param exc: la excepción capturada en el handler.
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta de integración *proxy* con el cuerpo de error.
    """
    if isinstance(exc, SentimentError):
        logger.warning(
            "status_rejected",
            extra={
                "error_code": exc.error_code,
                "correlation_id": correlation_id,
                **exc.detail,
            },
        )
        return error_response(exc, correlation_id)

    logger.exception("status_unhandled", extra={"correlation_id": correlation_id})
    return internal_error_response(correlation_id)
