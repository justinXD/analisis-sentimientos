"""Handler de transporte de la Lambda ``analyze_llm`` (tarea 9.2).

Adaptador **thin** (sección 2.1 del diseño) entre un estado ``Task`` del Map
interno de Step Functions —una invocación por Ítem (criterio 7.1)— y el caso de
uso puro
:class:`~sentiment.services.analyze_item_emotions.AnalyzeItemEmotionsService`.
No contiene lógica de negocio: la relectura del ITEM, el guard de idempotencia,
la única invocación a Bedrock, la normalización y la transición transaccional
viven en el servicio y sus puertos (sección 16.3 del diseño).

Forma del evento de entrada (``LlmItemInput``, sección 15.4)::

    {
      "analysis_id": "01JDQ...",
      "item_id": "01JDQ...#00000",
      "correlation_id": "7f3c1e2a-..."
    }

El evento **no** trae ``indificador`` ni ``respuesta``: el servicio los relee de
DynamoDB con el ``item_id`` (decisión deliberada de la sección 15.4), lo que
mantiene la PII fuera del historial de la ejecución de Step Functions.

Resultado (``LlmItemOutput``)
-----------------------------
El handler devuelve el :class:`~sentiment.services.analyze_item_emotions.ItemOutcome`
del servicio como ``{"item_id": ..., "outcome": "DONE" | "ALREADY_TERMINAL"}``.
Ambos valores son **éxito**: ``DONE`` significa que esta invocación cerró el Ítem;
``ALREADY_TERMINAL`` significa que ya estaba terminal (reintento idempotente, sin
invocación a Bedrock). Ninguno es un fallo, por lo que no se expresan como
excepción.

Propagación de errores por el nombre de la excepción (criterios 7.1, 9.18)
--------------------------------------------------------------------------
El punto entero de este handler es **no** interceptar las excepciones del
análisis. Si el servicio propaga ``ThrottledError`` / ``TransientDependencyError``
(transitorias), ``LlmInputRejectedError`` / ``LlmSchemaError`` (permanentes) o
``ConfigError`` (``MODEL_ID`` ausente, criterio 7.15), **suben sin captura** a la
Maquina_De_Estados: ``ErrorEquals`` compara el **nombre de la clase** para decidir
reintento con backoff (clase transitoria, criterio 9.16) o ``FAILED`` sin
reintentos vía el ``Catch`` (clase permanente, criterio 9.17). Capturarlas y
traducirlas a un error genérico aquí rompería esa clasificación.

Reutilización por contenedor (criterios 9.18, 17.11)
----------------------------------------------------
El servicio y sus adaptadores se construyen **una sola vez** en el ámbito del
módulo, para reutilizar los clientes de boto3 —y sus conexiones TLS— entre
invocaciones calientes del mismo contenedor.
"""

from __future__ import annotations

from typing import Any

from sentiment.adapters.bedrock_adapter import BedrockAdapter
from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.observability.logging import get_logger
from sentiment.services.analyze_item_emotions import AnalyzeItemEmotionsService

__all__ = ["handler"]

logger = get_logger(service="analyze_llm")

#: Caché del servicio a nivel de módulo. Es ``None`` hasta la primera invocación:
#: así importar este módulo nunca construye los adaptadores ni resuelve
#: configuración (``DynamoRepository`` resuelve ``TABLE_NAME`` en su constructor),
#: preservando la importabilidad del paquete sin entorno (criterio 17.4).
_SERVICE: AnalyzeItemEmotionsService | None = None


def _get_service() -> AnalyzeItemEmotionsService:
    """Devuelve el servicio, construyéndolo una sola vez por contenedor.

    La construcción es perezosa (en la primera invocación, no al importar) y
    memoizada: las invocaciones calientes del mismo contenedor reutilizan el
    servicio y, con él, los clientes singleton de boto3 y sus conexiones TLS
    (criterios 9.18, 17.11). Importar el módulo no toca AWS ni la configuración.

    :return: el :class:`AnalyzeItemEmotionsService` del contenedor.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = AnalyzeItemEmotionsService(
            repository=DynamoRepository(),
            analyzer=BedrockAdapter(),
        )
    return _SERVICE


def handler(event: dict[str, Any], _context: object = None) -> dict[str, Any]:
    """Punto de entrada de la Lambda ``analyze_llm``.

    Extrae ``analysis_id`` e ``item_id`` del evento, invoca el caso de uso y
    devuelve el desenlace. No captura las excepciones del análisis ni de la
    configuración: suben sin modificar a la Maquina_De_Estados, que las clasifica
    por el **nombre** de la clase (criterios 7.1, 9.18).

    :param event: el evento ``LlmItemInput`` (sección 15.4) con ``analysis_id``,
        ``item_id`` y ``correlation_id``.
    :param _context: contexto de Lambda; no se usa (el handler es thin).
    :return: ``{"item_id": str, "outcome": "DONE" | "ALREADY_TERMINAL"}``.
    :raises ThrottledError: si Bedrock limita la petición (transitorio, 9.16).
    :raises TransientDependencyError: ante 5xx, timeout o fallo de red de Bedrock
        (transitorio, 9.16).
    :raises LlmInputRejectedError: si Bedrock rechaza la entrada (permanente,
        ``LLM_INPUT_REJECTED``, 9.17).
    :raises LlmSchemaError: si la salida del modelo no valida contra el esquema
        (permanente, ``LLM_OUTPUT_INVALID``, 9.17).
    :raises sentiment.domain.errors.NotFoundError: si el ITEM no existe (7.14).
    :raises sentiment.observability.config.ConfigError: si ``MODEL_ID`` falta
        (7.15). Todas suben sin captura para que ``ErrorEquals`` las clasifique
        por el nombre de la clase.
    """
    analysis_id = str(event["analysis_id"])
    item_id = str(event["item_id"])
    correlation_id = event.get("correlation_id")

    # Correlación extremo a extremo en el logger estructurado, sin PII (el
    # item_id es un identificador determinista, no dato sensible).
    logger.append_keys(correlation_id=correlation_id, analysis_id=analysis_id)
    logger.info("llm_item_received", extra={"item_id": item_id})

    # El servicio relee el ITEM, aplica el guard de idempotencia, invoca a Bedrock
    # una sola vez y persiste. Las excepciones NO se capturan: suben para que la
    # Maquina_De_Estados las clasifique por el nombre de la clase (9.18).
    outcome = _get_service().execute(analysis_id, item_id)

    logger.info("llm_item_outcome", extra={"item_id": item_id, "outcome": outcome.value})
    return {"item_id": item_id, "outcome": outcome.value}
