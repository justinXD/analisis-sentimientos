"""Caso de uso ``analyze_item_emotions``: análisis profundo de UN Ítem con Bedrock.

Este módulo implementa el servicio de la sección 16.3 del diseño, consumido por
el handler ``analyze_llm`` (una invocación de Lambda por Ítem, criterio 7.1). Es
un caso de uso **puro**: depende exclusivamente de dos puertos del dominio
(:class:`~sentiment.domain.ports.AnalysisRepository` y
:class:`~sentiment.domain.ports.LlmAnalyzer`) inyectados en el constructor, no
importa ``boto3`` ni ningún adaptador concreto, y es importable sin el SDK de AWS
(Requirements 17.1 y 17.2). Toda su infraestructura (DynamoDB, Bedrock) entra por
los puertos; toda su política de reintento vive fuera, en la Maquina_De_Estados.

Responsabilidad y contrato
--------------------------
``execute(analysis_id, item_id)`` ejecuta, en este orden fijo y sin iteración:

1. **Relectura del ITEM** (criterio 7.2). Lee el registro ITEM por su ``item_id``
   —obteniendo ``indificador``, ``respuesta`` y ``status``— **antes** de emitir
   cualquier invocación al Modelo_Bedrock. Si el ITEM no existe o su lectura
   falla, el error sube a la Maquina_De_Estados sin invocar al modelo ni escribir
   nada (criterio 7.14): la excepción del puerto no se captura.

2. **Guard de idempotencia** (criterios 8.2, 8.3). Si el ITEM ya está en un
   Estado_Terminal (``DONE`` / ``FAILED`` / ``SKIPPED``), devuelve el resultado
   :data:`~sentiment.domain.errors.ALREADY_TERMINAL` **sin** invocar a Bedrock y
   **sin** modificar ningún registro ni contador. Es lo que hace que reintentar un
   Ítem ya procesado sea gratis (no cuesta una invocación del modelo) y seguro (no
   descuadra los contadores del META). ``ALREADY_TERMINAL`` es un valor de
   *resultado*, no un ``error_code`` ni un fallo.

3. **Una sola invocación** (criterios 7.1, 7.3). Invoca ``LlmAnalyzer.analyze``
   exactamente una vez. El adaptador no reintenta internamente; cualquier
   excepción tipada que lance (``ThrottledError`` / ``TransientDependencyError``
   transitorias, ``LlmInputRejectedError`` / ``LlmSchemaError`` permanentes, o
   ``ConfigError`` por ``MODEL_ID`` ausente, criterio 7.15) **se propaga sin
   captura** a la Maquina_De_Estados, que clasifica transitorio/permanente por el
   nombre de la excepción y decide reintentar o llevar el Ítem a ``FAILED``
   (criterios 7.8, 7.10, 9.16, 9.17). El servicio nunca las atrapa: no hace
   ``try/except`` alrededor del análisis ni de la escritura.

4. **Normalización** (criterios 7.6, 7.7). Reconstruye el ``EmotionAnalysis`` con
   el ``item_id`` correcto del Ítem y vuelve a aplicar la normalización de la
   lista ``sentimientos`` (recorte, minúsculas, descarte de inválidos, dedup, tope
   de 10) con :func:`~sentiment.domain.llm_parsing.normalize_sentimientos`. Si la
   lista queda vacía, la ``explicacion`` se fuerza a cadena vacía (criterio 7.7).

5. **Persistencia transaccional e idempotente** (criterios 7.8, 8.3, 8.4). Llama
   a ``AnalysisRepository.complete_item``, que ejecuta un ``TransactWriteItems``
   condicionado a ``status == PENDING`` e incrementa ``done_count`` del META en la
   misma transacción. El método devuelve ``False`` si la condición falló porque
   otro intento ya cerró el Ítem (carrera de reintentos): no es un error, es la
   idempotencia funcionando, y el servicio lo traduce al mismo resultado
   ``ALREADY_TERMINAL``.

El servicio es deliberadamente **no iterativo** (sin invariantes de bucle): toda
la repetición y todo el reintento son declarativos en Step Functions, no un
bucle ``for attempt in range(...)`` con ``time.sleep`` como en el código legado.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sentiment.domain.errors import ALREADY_TERMINAL
from sentiment.domain.llm_parsing import normalize_sentimientos
from sentiment.domain.models import TERMINAL_ITEM_STATUSES, EmotionAnalysis

if TYPE_CHECKING:
    from sentiment.domain.ports import AnalysisRepository, LlmAnalyzer

__all__ = ["AnalyzeItemEmotionsService", "ItemOutcome"]


class ItemOutcome(StrEnum):
    """Desenlace observable de una invocación de ``analyze_item_emotions``.

    Es el valor que el handler ``analyze_llm`` devuelve a la Maquina_De_Estados.
    No pertenece al Catalogo_De_Errores: ambos miembros son resultados de éxito,
    no fallos (los fallos se expresan propagando una excepción, nunca con este
    enum).

    Miembros:
        DONE: el análisis profundo se completó en esta invocación y el Ítem quedó
            en ``status = DONE`` con sus resultados persistidos y ``done_count``
            incrementado exactamente una vez (criterios 7.8, 8.3).
        ALREADY_TERMINAL: el Ítem ya estaba en un Estado_Terminal (o lo alcanzó
            otro intento que ganó la carrera de escritura), por lo que esta
            invocación no invocó a Bedrock ni modificó nada (criterios 8.2, 8.5).
            Su valor coincide con la constante de resultado
            :data:`~sentiment.domain.errors.ALREADY_TERMINAL`.
    """

    DONE = "DONE"
    ALREADY_TERMINAL = ALREADY_TERMINAL


class AnalyzeItemEmotionsService:
    """Análisis profundo de UN Ítem con el Modelo_Bedrock, sin reintentos internos.

    Caso de uso puro con dependencias inyectadas (inversión de dependencias): no
    conoce DynamoDB ni Bedrock, solo los puertos :class:`AnalysisRepository` y
    :class:`LlmAnalyzer`. Esto permite probarlo con dobles en memoria y mantiene
    la frontera hexagonal (``handlers`` → ``services`` → ``domain``).
    """

    def __init__(
        self, repository: AnalysisRepository, analyzer: LlmAnalyzer
    ) -> None:
        """Inyecta los puertos que el caso de uso consume.

        :param repository: puerto de persistencia del Lote y sus Ítems. Se usa
            para releer el ITEM (:meth:`AnalysisRepository.get_item`) y para la
            transición idempotente a ``DONE``
            (:meth:`AnalysisRepository.complete_item`).
        :param analyzer: puerto del análisis profundo (Modelo_Bedrock). Se invoca
            exactamente una vez por ejecución exitosa.
        """
        self._repository = repository
        self._analyzer = analyzer

    def execute(self, analysis_id: str, item_id: str) -> ItemOutcome:
        """Procesa el análisis profundo de un único Ítem.

        El orden de los pasos es normativo: la relectura precede siempre a la
        invocación del modelo (criterio 7.2), y el guard de idempotencia precede
        siempre a cualquier gasto en Bedrock (criterio 8.2).

        :param analysis_id: ULID del Lote al que pertenece el Ítem.
        :param item_id: ``{analysis_id}#{index:05d}`` del Ítem a procesar.
        :return: :attr:`ItemOutcome.DONE` si esta invocación cerró el Ítem;
            :attr:`ItemOutcome.ALREADY_TERMINAL` si el Ítem ya era terminal o si
            otro intento ganó la carrera de escritura (ambos son éxito idempotente).
        :raises sentiment.domain.errors.NotFoundError: si el ITEM no existe; sube
            sin invocar al modelo ni escribir nada (criterio 7.14).
        :raises sentiment.domain.errors.SentimentError: cualquier excepción tipada
            del análisis (``ThrottledError``, ``TransientDependencyError``,
            ``LlmInputRejectedError``, ``LlmSchemaError``) o de configuración
            (``ConfigError`` por ``MODEL_ID`` ausente, criterio 7.15) se propaga
            sin captura a la Maquina_De_Estados (criterios 7.8, 7.10, 7.14).
        """
        # Paso 1 (criterio 7.2): relectura del ITEM ANTES de tocar Bedrock. Si no
        # existe o su lectura falla, la excepción del puerto sube sin captura y el
        # modelo no llega a invocarse (criterio 7.14).
        item = self._repository.get_item(analysis_id, item_id)

        # Paso 2 (criterios 8.2, 8.3): guard de idempotencia. Un Ítem ya terminal
        # no se reanaliza: ni invocación al modelo ni escritura ni incremento de
        # contadores. Es la razón por la que reintentar es gratis y seguro.
        if item.status in TERMINAL_ITEM_STATUSES:
            return ItemOutcome.ALREADY_TERMINAL

        # Paso 3 (criterios 7.1, 7.3): UNA sola invocación. Las excepciones SUBEN
        # sin captura: el retry y el catch son declarativos en Step Functions, no
        # imperativos aquí. No hay try/except alrededor de esta llamada.
        emotions = self._analyzer.analyze(item.indificador, item.respuesta)

        # Paso 4 (criterios 7.6, 7.7): normalización y fijación del item_id. Se
        # reaplica la normalización de sentimientos en el dominio (no se confía en
        # que la salida del modelo venga normalizada) y la explicacion se vacía
        # cuando no hay sentimientos.
        normalized = self._build_result(item_id, emotions)

        # Paso 5 (criterios 7.8, 8.3, 8.4): transición transaccional e idempotente.
        # complete_item condiciona a status == PENDING y hace ADD done_count en la
        # misma transacción. Devuelve False si otro intento ya cerró el Ítem: no es
        # un error, es idempotencia; se traduce al mismo resultado terminal.
        applied = self._repository.complete_item(analysis_id, item_id, normalized)
        return ItemOutcome.DONE if applied else ItemOutcome.ALREADY_TERMINAL

    @staticmethod
    def _build_result(item_id: str, emotions: EmotionAnalysis) -> EmotionAnalysis:
        """Reconstruye el resultado con el ``item_id`` correcto y renormalizado.

        El puerto ``LlmAnalyzer`` devuelve un ``EmotionAnalysis`` con ``item_id``
        vacío (el contrato del puerto no lo recibe, diseño 16.3); el servicio es
        quien conoce el ``item_id`` del Ítem y lo fija. Además reaplica la
        normalización del criterio 7.6 sobre ``sentimientos`` y fuerza la
        ``explicacion`` a cadena vacía cuando la lista normalizada queda vacía
        (criterio 7.7). Los metadatos de la invocación (``model_id`` y los
        recuentos de tokens) se conservan tal cual los informó el adaptador.

        :param item_id: ``item_id`` del Ítem al que pertenece el resultado.
        :param emotions: resultado crudo devuelto por el puerto de análisis.
        :return: un :class:`EmotionAnalysis` con el ``item_id`` fijado,
            ``sentimientos`` normalizado y ``explicacion`` coherente con él.
        """
        sentimientos = normalize_sentimientos(list(emotions.sentimientos))
        explicacion = emotions.explicacion if sentimientos else ""
        return EmotionAnalysis(
            item_id=item_id,
            sentimientos=sentimientos,
            explicacion=explicacion,
            model_id=emotions.model_id,
            input_tokens=emotions.input_tokens,
            output_tokens=emotions.output_tokens,
        )
