"""Lambda ``api_submit``: adaptador *thin* de ``POST /v1/analyses``.

Este handler es el *entry point* de la Lambda que la Infraestructura referencia
con ``handler="sentiment.handlers.api_submit.handler"``. No contiene lógica de
negocio: traduce el evento de API Gateway (REST, integración *proxy*) en un
:class:`~sentiment.services.submit_analysis.SubmitCommand`, invoca
:class:`~sentiment.services.submit_analysis.SubmitAnalysisService` y traduce la
vista resultante —o el :class:`~sentiment.domain.errors.SentimentError`— en una
respuesta HTTP. El patrón *thin adapter* es el de la sección 16.6 del diseño.

Responsabilidades (criterios 1.11, 1.14, 2.8, 2.9, 3.14, 3.15):

* Deriva ``tenant_id`` (claim ``custom:tenant_id``) y ``owner_sub`` (claim
  ``sub``) **exclusivamente** del contexto del autorizador, nunca del cuerpo ni
  de las cabeceras (criterio 1.14). Un claim ``custom:tenant_id`` ausente o en
  blanco produce ``403 FORBIDDEN`` **antes** de tocar DynamoDB o S3 (criterio
  2.9). Un campo ``tenant_id`` en el cuerpo lo rechaza el propio servicio como
  ``VALIDATION_FAILED`` vía el ``additionalProperties: false`` del esquema
  (criterio 1.11); el handler no lo inspecciona.
* Pasa el cuerpo HTTP **crudo, sin parsear** al servicio: toda la validación
  (tamaño, esquema, precedencia de ``error_code``) vive en el caso de uso, que
  además degrada cualquier entrada malformada a ``VALIDATION_FAILED`` (criterio
  3.19), de modo que una entrada inválida nunca produce un 5xx.
* Genera un ``correlation_id`` UUIDv4 cuando la solicitud no lo aporta (criterio
  3.15) y lo propaga al comando y a toda respuesta.
* En éxito responde ``202 Accepted`` con el ``analysis_id``, el ``status``, el
  ``item_count``, el ``created_at`` ISO 8601 UTC y la URL de estado del Lote
  (criterio 4.9). Ante un :class:`SentimentError` responde con su ``http_status``
  y un cuerpo compuesto solo por ``error_code`` + ``correlation_id`` (criterios
  3.14, 15.5); ante un fallo de infraestructura no previsto, ``500 INTERNAL_ERROR``
  (criterios 4.10, 4.11).

Rendimiento (criterio 17.11, sección 16.6): el servicio y sus adaptadores se
construyen **una sola vez** en el ámbito del módulo (``_SERVICE``), fuera del
handler, y el contenedor de Lambda los reutiliza entre invocaciones calientes.
Nunca se registra el ``event`` ni el cuerpo: contienen ``respuesta``, que es PII
(criterios 15.1, 15.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.adapters.eventbridge_publisher import EventBridgePublisher
from sentiment.adapters.s3_storage import S3Storage
from sentiment.adapters.sqs_publisher import SqsPublisher
from sentiment.domain.errors import SentimentError
from sentiment.domain.ids import new_analysis_id
from sentiment.domain.models import AnalysisMeta
from sentiment.handlers._http import (
    error_response,
    extract_identity,
    internal_error_response,
    json_response,
    resolve_correlation_id,
)
from sentiment.observability.logging import get_logger
from sentiment.services.submit_analysis import SubmitAnalysisService, SubmitCommand

__all__ = ["handler"]

logger = get_logger(service="api_submit")

#: Plantilla de la URL (relativa) de estado del Lote que se devuelve en el 202
#: (criterio 4.9). Se emite como ruta relativa a la raíz de la API para no
#: depender de la URL base del *stage*, que la Infraestructura resuelve y que un
#: adaptador *thin* no debe reconstruir a partir del evento.
_STATUS_URL_TEMPLATE: Final = "/v1/analyses/{analysis_id}"


def _utcnow() -> datetime:
    """Instante actual en UTC. Aislado para inyectarlo como ``clock`` del servicio."""
    return datetime.now(tz=UTC)


def build_service() -> SubmitAnalysisService:
    """Construye el :class:`SubmitAnalysisService` con sus adaptadores de producción.

    Cablea el caso de uso con los adaptadores concretos de DynamoDB, S3, SQS y
    EventBridge y los *callables* de reloj e identificadores. Se invoca **una vez**
    en el ámbito del módulo (ver :data:`_SERVICE`): los adaptadores comparten los
    clientes singleton de boto3, reutilizados entre invocaciones calientes
    (criterio 17.11). No lee configuración obligatoria ni llama a AWS en la
    construcción, por lo que importar este módulo nunca depende del entorno
    (criterio 17.7).

    :return: una instancia lista del servicio de ingesta.
    """
    return SubmitAnalysisService(
        repository=DynamoRepository(),
        storage=S3Storage(),
        queue=SqsPublisher(),
        events=EventBridgePublisher(),
        clock=_utcnow,
        id_factory=new_analysis_id,
    )


#: Caché del servicio a nivel de módulo. Es ``None`` hasta la primera invocación.
#: La construcción se difiere a :func:`_get_service` —no al import— porque
#: ``DynamoRepository`` resuelve ``TABLE_NAME`` en su constructor; hacerla perezosa
#: preserva la importabilidad del paquete sin entorno (criterio 17.4) sin renunciar
#: a construir una sola vez por contenedor (criterio 17.11).
_SERVICE: SubmitAnalysisService | None = None


def _get_service() -> SubmitAnalysisService:
    """Devuelve el servicio, construyéndolo una sola vez por contenedor.

    La construcción es perezosa (en la primera invocación, no al importar) y
    memoizada: las invocaciones calientes del mismo contenedor reutilizan el
    servicio y, con él, los clientes singleton de boto3 y sus conexiones TLS
    (criterio 17.11). Importar el módulo no toca AWS ni la configuración
    (criterio 17.4), igual que el resto de handlers del paquete.

    :return: el :class:`SubmitAnalysisService` del contenedor.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build_service()
    return _SERVICE


def _accepted_body(meta: AnalysisMeta) -> dict[str, Any]:
    """Construye el cuerpo del ``202 Accepted`` a partir del META del Lote creado.

    Expone solo metadatos no sensibles (criterio 4.9): ``analysis_id``, ``status``
    (``PENDING``), ``item_count``, ``created_at`` en ISO 8601 UTC y la URL de
    estado del Lote. Nunca incluye el texto de las respuestas.

    :param meta: el META del Lote recién sembrado.
    :return: el cuerpo serializable del 202.
    """
    return {
        "analysis_id": meta.analysis_id,
        "status": str(meta.status.value),
        "item_count": meta.item_count,
        "created_at": meta.created_at.astimezone(UTC).isoformat(),
        "status_url": _STATUS_URL_TEMPLATE.format(analysis_id=meta.analysis_id),
    }


def _decode_body(event: dict[str, Any]) -> bytes:
    """Devuelve el cuerpo HTTP crudo del evento en ``bytes``, sin parsearlo.

    API Gateway puede entregar el cuerpo como texto o, con ``isBase64Encoded``, en
    base64; en ambos casos se devuelve el cuerpo original en bytes para que el
    servicio mida su tamaño y lo valide (criterios 3.4, 3.1). Un cuerpo ausente se
    representa como ``b""``, que el servicio rechaza como ``VALIDATION_FAILED``
    (criterio 3.2). La decodificación de base64 malformado nunca lanza aquí: se
    degrada a ``b""`` para que el servicio produzca ``VALIDATION_FAILED`` en lugar
    de un 5xx (criterio 3.19).

    :param event: evento de API Gateway (integración *proxy*).
    :return: el cuerpo HTTP crudo en ``bytes`` (``b""`` si no hay cuerpo).
    """
    raw = event.get("body")
    if raw is None:
        return b""

    if event.get("isBase64Encoded") and isinstance(raw, str):
        import base64
        import binascii

        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            # Cuerpo base64 malformado: se trata como vacío para que el servicio
            # lo clasifique como VALIDATION_FAILED (criterio 3.19), nunca 5xx.
            return b""

    if isinstance(raw, bytes):
        return raw
    return str(raw).encode("utf-8")


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Adaptador HTTP de ``POST /v1/analyses``. Sin lógica de negocio.

    Flujo: resuelve el ``correlation_id`` (criterio 3.15), deriva la identidad del
    autorizador (criterios 1.14, 2.9), construye el :class:`SubmitCommand` con el
    cuerpo crudo e invoca el servicio. Traduce el resultado a ``202`` o el error a
    su respuesta HTTP sin PII (criterios 3.14, 15.5).

    :param event: evento de API Gateway (integración *proxy*).
    :param context: contexto de ejecución de Lambda (no usado; parte de la firma).
    :return: la respuesta de integración *proxy* ``{statusCode, headers, body}``.
    """
    correlation_id = resolve_correlation_id(event)

    try:
        # Identidad SOLO del token (criterio 1.14). Un tenant_id ausente/vacío es
        # 403 antes de cualquier escritura (criterio 2.9).
        identity = extract_identity(event)
        command = SubmitCommand(
            tenant_id=identity.tenant_id,
            owner_sub=identity.principal_sub,
            raw_body=_decode_body(event),
            correlation_id=correlation_id,
        )
        meta = _get_service().execute(command)
    except Exception as exc:
        return _handle_exception(exc, correlation_id)

    logger.info(
        "submit_accepted",
        extra={
            "analysis_id": meta.analysis_id,
            "tenant_id": meta.tenant_id,
            "item_count": meta.item_count,
            "correlation_id": correlation_id,
        },
    )
    return json_response(202, _accepted_body(meta))


def _handle_exception(exc: Exception, correlation_id: str) -> dict[str, Any]:
    """Traduce una excepción del flujo de ingesta a una respuesta HTTP sin PII.

    Un :class:`SentimentError` (validación, cuota, 403, o el ``INTERNAL_ERROR`` ya
    envuelto por el servicio ante un fallo de S3/DynamoDB) se traduce a su
    ``http_status`` con el cuerpo seguro (criterios 3.14, 15.5). Cualquier otra
    excepción es un fallo de infraestructura no previsto: ``500 INTERNAL_ERROR``,
    con la traza solo en el log (criterio 15.5). El ``event`` y el cuerpo nunca se
    registran (PII, criterio 15.1).

    :param exc: la excepción capturada en el handler.
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta de integración *proxy* con el cuerpo de error.
    """
    if isinstance(exc, SentimentError):
        return _handle_sentiment_error(exc, correlation_id)

    logger.exception(
        "submit_unhandled",
        extra={"correlation_id": correlation_id},
    )
    return internal_error_response(correlation_id)


def _handle_sentiment_error(
    exc: SentimentError, correlation_id: str
) -> dict[str, Any]:
    """Registra y traduce un :class:`SentimentError` del flujo de ingesta.

    :param exc: el error del dominio.
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta HTTP con ``error_code`` + ``correlation_id`` únicamente.
    """
    # El detalle va al log (nunca al cliente); el detail del dominio no contiene
    # PII, solo identificadores internos.
    logger.warning(
        "submit_rejected",
        extra={
            "error_code": exc.error_code,
            "correlation_id": correlation_id,
            **exc.detail,
        },
    )
    return error_response(exc, correlation_id)
