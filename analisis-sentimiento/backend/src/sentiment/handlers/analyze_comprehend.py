"""Handler de transporte de la Lambda ``analyze_comprehend`` (tarea 9.2).

Adaptador **thin** (sección 2.1 del diseño) entre un estado ``Task`` del
Distributed_Map de Step Functions y el caso de uso puro
:class:`~sentiment.services.analyze_batch_sentiment.AnalyzeBatchSentimentService`.
Su única responsabilidad es de transporte: traducir el evento
``ComprehendBatchInput`` que produce el ``ItemBatcher`` (sección 15.4 del diseño)
al modelo de dominio, invocar el servicio y traducir su resultado a la forma
``ComprehendBatchOutput`` que el estado espera. **No contiene lógica de negocio**:
toda la clasificación idioma/sentimiento, la conservación de Ítems y la
persistencia viven en el servicio y sus puertos.

Forma del evento de entrada (``ComprehendBatchInput``, sección 15.4)::

    {
      "BatchInput": {
        "analysis_id": "01JDQ...",
        "tenant_id": "acme-corp",
        "correlation_id": "7f3c1e2a-..."
      },
      "Items": [
        {"indificador": "Comunicación", "respuesta": "...", "index": 0},
        ...
      ]
    }

Forma del resultado (``ComprehendBatchOutput``, sección 15.4): solo punteros y
etiquetas; **el texto de ``respuesta`` nunca vuelve a viajar** por el estado de la
ejecución (el historial de Step Functions se retiene 90 días y es otro lugar
donde no debe aparecer PII).

Propagación de errores por el nombre de la excepción (criterios 5.11, 6.11, 9.18)
---------------------------------------------------------------------------------
El handler **no** envuelve la invocación del servicio en un ``try/except`` de
procesamiento. Si el servicio propaga una excepción del Catalogo_De_Errores
(``ThrottledError`` / ``TransientDependencyError`` transitorias;
``TaskInputInvalidError`` permanente por un batch de más de 25), **sube sin
captura** a la Maquina_De_Estados: es el **nombre de la clase** lo que ``ErrorEquals``
compara para decidir si reintenta (clase transitoria, criterio 9.16) o lleva el
Ítem a ``FAILED`` sin reintentos (clase permanente, criterio 9.17). Traducir aquí
la excepción a un error genérico rompería esa clasificación; por eso el punto
entero de este handler es **no** interceptarla.

Reutilización por contenedor (criterios 9.18, 17.11)
----------------------------------------------------
El servicio y sus adaptadores se construyen **una sola vez** en el ámbito del
módulo (:func:`_build_service`), de modo que las invocaciones calientes del mismo
contenedor de Lambda reutilizan los clientes de boto3 —y sus conexiones TLS— en
lugar de reconstruirlos en cada evento.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sentiment.adapters.comprehend_adapter import ComprehendAdapter
from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.domain.ids import make_item_id
from sentiment.domain.models import AnalysisItem, ItemStatus
from sentiment.observability.logging import get_logger
from sentiment.services.analyze_batch_sentiment import AnalyzeBatchSentimentService

if TYPE_CHECKING:
    from sentiment.services.analyze_batch_sentiment import BatchSentimentResult

__all__ = ["handler"]

logger = get_logger(service="analyze_comprehend")

#: ``reason`` que acompaña a cada Ítem omitido en la salida (sección 15.4). El
#: servicio marca ``SKIPPED`` los Marcadores_Vacios; el motivo es siempre el
#: mismo y no transporta PII.
_SKIPPED_REASON = "EMPTY_RESPONSE"

#: Caché del servicio a nivel de módulo. Es ``None`` hasta la primera invocación:
#: así importar este módulo nunca construye los adaptadores ni resuelve
#: configuración (``DynamoRepository`` resuelve ``TABLE_NAME`` en su constructor),
#: preservando la importabilidad del paquete sin entorno (criterio 17.4).
_SERVICE: AnalyzeBatchSentimentService | None = None


def _get_service() -> AnalyzeBatchSentimentService:
    """Devuelve el servicio, construyéndolo una sola vez por contenedor.

    La construcción es perezosa (en la primera invocación, no al importar) y
    memoizada: las invocaciones calientes del mismo contenedor reutilizan el
    servicio y, con él, los clientes singleton de boto3 y sus conexiones TLS
    (criterios 9.18, 17.11). Importar el módulo no toca AWS ni la configuración.

    :return: el :class:`AnalyzeBatchSentimentService` del contenedor.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = AnalyzeBatchSentimentService(
            detector=ComprehendAdapter(),
            repository=DynamoRepository(),
        )
    return _SERVICE


def _coerce_items(
    analysis_id: str, raw_items: list[dict[str, Any]]
) -> list[AnalysisItem]:
    """Traduce la lista ``Items`` del evento a Ítems de dominio en ``PENDING``.

    Cada entrada del ``ItemBatcher`` trae ``indificador``, ``respuesta`` e
    ``index``. El ``item_id`` se deriva de forma determinista del ``analysis_id``
    y el ``index`` (:func:`~sentiment.domain.ids.make_item_id`), de modo que
    coincide con el que persistió la ingesta (``{analysis_id}#{index:05d}``). El
    ``status`` entra siempre como ``PENDING``: el servicio es quien decide el
    desenlace terminal de cada Ítem.

    :param analysis_id: ULID del Lote al que pertenece el batch.
    :param raw_items: la lista ``Items`` del evento ``ComprehendBatchInput``.
    :return: los Ítems de dominio, en el mismo orden que el evento.
    """
    items: list[AnalysisItem] = []
    for raw in raw_items:
        index = int(raw["index"])
        items.append(
            AnalysisItem(
                analysis_id=analysis_id,
                item_id=make_item_id(analysis_id, index),
                index=index,
                indificador=str(raw["indificador"]),
                respuesta=str(raw["respuesta"]),
                status=ItemStatus.PENDING,
            )
        )
    return items


def _render_output(
    analysis_id: str,
    items: list[AnalysisItem],
    result: BatchSentimentResult,
) -> dict[str, Any]:
    """Construye el ``ComprehendBatchOutput`` a partir del desenlace del servicio.

    Devuelve solo punteros (``item_id``, ``index``) y etiquetas, nunca el texto de
    ``respuesta`` (sección 15.4): el resultado viaja por el estado de la ejecución,
    que no debe contener PII. El ``index`` de cada ``item_id`` se recupera del mapa
    construido a partir de los Ítems de entrada, sin reparsear el ``item_id``.

    :param analysis_id: ULID del Lote.
    :param items: Ítems de entrada, fuente del ``index`` de cada ``item_id``.
    :param result: los tres conjuntos disjuntos que devolvió el servicio.
    :return: el ``dict`` ``ComprehendBatchOutput`` serializable para el estado.
    """
    index_by_id = {item.item_id: item.index for item in items}
    return {
        "analysis_id": analysis_id,
        "to_analyze": [
            {"item_id": item_id, "index": index_by_id[item_id], "needs_llm": True}
            for item_id in result.to_analyze
        ],
        "skipped": [
            {"item_id": item_id, "index": index_by_id[item_id], "reason": _SKIPPED_REASON}
            for item_id in result.skipped
        ],
        "failed": [
            {"item_id": item_id, "index": index_by_id[item_id]}
            for item_id in result.failed
        ],
    }


def handler(event: dict[str, Any], _context: object = None) -> dict[str, Any]:
    """Punto de entrada de la Lambda ``analyze_comprehend``.

    Traduce el evento del ``ItemBatcher`` al dominio, invoca el caso de uso y
    devuelve el ``ComprehendBatchOutput``. No captura las excepciones de
    procesamiento: cualquier error del servicio sube sin modificar a la
    Maquina_De_Estados, que lo clasifica por el **nombre** de la clase (criterios
    5.11, 6.11, 9.18).

    :param event: el evento ``ComprehendBatchInput`` (sección 15.4) con las claves
        ``BatchInput`` (``analysis_id``, ``tenant_id``, ``correlation_id``) e
        ``Items`` (lista de ``{indificador, respuesta, index}``).
    :param _context: contexto de Lambda; no se usa (el handler es thin).
    :return: el ``ComprehendBatchOutput`` con los tres conjuntos de ``item_id``.
    :raises TaskInputInvalidError: si el batch excede los 25 Ítems (permanente,
        criterio 6.12); propagada sin captura por el servicio.
    :raises ThrottledError: si Comprehend limita la llamada (transitorio, 9.16).
    :raises TransientDependencyError: ante indisponibilidad de Comprehend
        (transitorio, 9.16). Ambas suben sin captura para que ``ErrorEquals`` las
        clasifique por el nombre de la clase.
    """
    batch_input: dict[str, Any] = event["BatchInput"]
    analysis_id = str(batch_input["analysis_id"])
    correlation_id = batch_input.get("correlation_id")
    raw_items: list[dict[str, Any]] = event.get("Items", [])

    # Correlación extremo a extremo en el logger estructurado, sin PII.
    logger.append_keys(correlation_id=correlation_id, analysis_id=analysis_id)
    logger.info("comprehend_batch_received", extra={"item_count": len(raw_items)})

    items = _coerce_items(analysis_id, raw_items)

    # El servicio clasifica, persiste y verifica la conservación de Ítems. Las
    # excepciones de procesamiento NO se capturan: suben para que la
    # Maquina_De_Estados las clasifique por el nombre de la clase (9.18).
    result = _get_service().analyze(items)

    return _render_output(analysis_id, items, result)
