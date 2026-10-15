"""Caso de uso del análisis clásico por Batch: ``AnalyzeBatchSentimentService``.

Este servicio es la lógica de negocio **pura** de la Lambda Analyze_Comprehend
(sección 11.4 del diseño). Recibe un Batch de hasta 25 Ítems de un mismo Lote y,
apoyándose en los puertos :class:`~sentiment.domain.ports.SentimentDetector` y
:class:`~sentiment.domain.ports.AnalysisRepository` inyectados, clasifica cada
Ítem en exactamente uno de tres conjuntos y persiste los desenlaces:

* ``skipped``: Ítems cuya ``respuesta`` es un Marcador_Vacio. No se envían ni a
  Comprehend ni al Modelo_Bedrock; se transicionan a ``SKIPPED`` (criterios 6.6,
  18.6).
* ``to_analyze``: Ítems para los que Comprehend devolvió un sentimiento válido.
  Se persiste su resultado clásico pero **permanecen en ``PENDING``** para el
  análisis profundo posterior (criterio 6.10). Esta capa nunca los marca ``DONE``.
* ``failed``: Ítems que Comprehend reportó en su ``ErrorList``
  (``COMPREHEND_ITEM_FAILED``, criterio 6.7) o que no aparecieron ni en los
  éxitos ni en los errores de su llamada (``COMPREHEND_RESULT_MISSING``,
  criterio 6.13). Se transicionan a ``FAILED``.

Pureza y frontera hexagonal
---------------------------
El servicio **no** importa ``boto3`` ni ningún adaptador: depende solo de los
puertos (``typing.Protocol``) y del modelo de dominio. Las implementaciones
concretas (``ComprehendAdapter``, ``DynamoRepository``) se inyectan por
constructor, lo que permite probar el caso de uso con dobles en memoria sin red
(arquitectura hexagonal, sección 2.1 del diseño).

Conservación de ítems (Property 10 / P1)
----------------------------------------
La postcondición central del servicio es la **conservación de ítems**: todo Ítem
de entrada termina en exactamente uno de los tres conjuntos, y la suma de sus
tamaños es igual al número de Ítems del Batch, con independencia del orden en que
Comprehend devuelva sus resultados y de que falte el resultado de alguno
(criterio 6.5). El servicio verifica esa invariante explícitamente antes de
devolver y, si no se cumpliera, falla con :class:`AssertionError` en vez de
perder un Ítem en silencio. La asociación resultado → Ítem es siempre por el
``Index`` que devuelve Comprehend, nunca por la posición en la lista devuelta
(criterio 6.8).

Reconciliación idioma + sentimiento
-----------------------------------
El puerto ``detect_sentiments`` construye cada ``ClassicSentiment`` con
``item_id = ""`` y ``language_confidence = 0`` (no dispone de esa información).
El servicio, que sí conoce la correspondencia posición → Ítem y el idioma
detectado de cada Ítem, reconstruye cada ``ClassicSentiment`` con el ``item_id``
real antes de persistirlo (criterio 6.8).

Fallo de la llamada entera (criterio 6.15)
------------------------------------------
Si una llamada a Comprehend falla por completo, el puerto propaga una excepción
transitoria. El servicio **no** la captura: la deja subir a la Maquina_De_Estados
sin haber escrito ningún resultado de sentimiento de ese grupo de idioma y
dejando esos Ítems en ``PENDING`` (criterio 6.15). Para que no haya resultado
parcial de la llamada que falla, los resultados clásicos se persisten
**por grupo de idioma** solo tras completar su llamada con éxito.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sentiment.domain.empty_marker import is_empty_marker
from sentiment.domain.errors import (
    ComprehendResultMissingError,
    TaskInputInvalidError,
)
from sentiment.domain.models import AnalysisItem, ClassicSentiment, ItemStatus
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from sentiment.domain.ports import AnalysisRepository, SentimentDetector

__all__ = ["BatchSentimentResult", "AnalyzeBatchSentimentService"]

logger = get_logger(service="analyze_comprehend")

#: Tope de Ítems por invocación de Analyze_Comprehend (criterios 6.11, 6.12). Es
#: el mismo límite que impone la API batch de Comprehend y que respeta el
#: ``ItemBatcher`` del Distributed_Map.
MAX_ITEMS_PER_BATCH = 25

#: ``error_code`` que se registra en un Ítem analizable que Comprehend no devolvió
#: ni en su lista de éxitos ni en la de errores (criterio 6.13).
COMPREHEND_RESULT_MISSING = ComprehendResultMissingError.error_code


@dataclass(frozen=True, slots=True)
class BatchSentimentResult:
    """Desenlace de clasificar un Batch, con los ``item_id`` de cada conjunto.

    Los tres conjuntos son disjuntos y su unión cubre todos los Ítems del Batch
    (conservación de ítems, criterio 6.5). Se expresan como tuplas de ``item_id``
    —no de Ítems completos— para no transportar PII (el texto de ``respuesta``)
    fuera del servicio: el llamante (la Lambda Analyze_Comprehend) emite métricas
    y continúa la orquestación a partir de identificadores, nunca del texto.

    Attributes:
        to_analyze: ``item_id`` de los Ítems con sentimiento clásico válido que
            siguen en ``PENDING`` para el análisis profundo (criterio 6.10).
        skipped: ``item_id`` de los Ítems transicionados a ``SKIPPED`` por ser su
            ``respuesta`` un Marcador_Vacio (criterios 6.6, 18.6).
        failed: ``item_id`` de los Ítems transicionados a ``FAILED``, ya sea por
            el ``ErrorList`` de Comprehend (``COMPREHEND_ITEM_FAILED``) o por
            ausencia de resultado (``COMPREHEND_RESULT_MISSING``).
    """

    to_analyze: tuple[str, ...] = field(default_factory=tuple)
    skipped: tuple[str, ...] = field(default_factory=tuple)
    failed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        """Número total de Ítems clasificados en los tres conjuntos."""
        return len(self.to_analyze) + len(self.skipped) + len(self.failed)


class AnalyzeBatchSentimentService:
    """Caso de uso del análisis clásico por Batch (sección 11.4 del diseño).

    Depende de dos puertos inyectados y de ninguna implementación concreta:

    * ``detector`` (:class:`~sentiment.domain.ports.SentimentDetector`): detecta
      idioma y sentimiento en lote.
    * ``repository`` (:class:`~sentiment.domain.ports.AnalysisRepository`):
      persiste los resultados clásicos y las transiciones ``SKIPPED``/``FAILED``.

    El servicio no mantiene estado entre invocaciones; una instancia puede
    reutilizarse por contenedor de Lambda caliente.
    """

    def __init__(
        self, detector: SentimentDetector, repository: AnalysisRepository
    ) -> None:
        """Inicializa el servicio con los puertos que consume.

        :param detector: implementación del puerto de análisis clásico (Comprehend).
        :param repository: implementación del puerto de persistencia del Lote.
        """
        self._detector = detector
        self._repository = repository

    def analyze(self, items: list[AnalysisItem]) -> BatchSentimentResult:
        """Clasifica y persiste el desenlace clásico de un Batch de hasta 25 Ítems.

        Flujo (sección 11.4 del diseño):

        1. Valida el tope de 25 Ítems; si se excede, falla con
           ``TaskInputInvalidError`` **sin** invocar a Comprehend ni escribir en
           la Dynamo_Table (criterios 6.11, 6.12).
        2. Separa los Marcadores_Vacios y los transiciona a ``SKIPPED`` sin
           invocar a Comprehend (criterios 6.6, 18.6).
        3. Detecta el idioma de los Ítems analizables en una única llamada batch
           (criterio 6.1) y los agrupa por idioma (criterio 6.2).
        4. Por cada grupo de idioma, invoca ``detect_sentiments`` (<= 25),
           reconcilia por ``Index`` (criterio 6.8), persiste los éxitos y
           transiciona los errores a ``FAILED`` (criterios 6.7, 6.10). Un Ítem
           ausente de ambas listas se transiciona a ``FAILED`` con
           ``COMPREHEND_RESULT_MISSING`` (criterio 6.13).
        5. Verifica la conservación de ítems (criterio 6.5) y devuelve el
           desenlace.

        :param items: Ítems del Batch, todos en ``status = PENDING``. El orden es
            el del Batch; ``items[i].index`` es su índice 0-based en el Lote.
        :return: el :class:`BatchSentimentResult` con los tres conjuntos disjuntos.
        :raises TaskInputInvalidError: si el Batch excede los 25 Ítems (6.12).
        :raises TransientDependencyError: si una llamada a Comprehend falla por
            completo; en ese caso no se escribe resultado parcial de ese grupo de
            idioma y sus Ítems quedan en ``PENDING`` (criterio 6.15). La excepción
            se propaga sin captura a la Maquina_De_Estados.
        """
        if len(items) > MAX_ITEMS_PER_BATCH:
            # Defecto de orquestación, no del contenido del cliente: ni se llama a
            # Comprehend ni se toca ningún registro ITEM (criterio 6.12).
            logger.warning(
                "comprehend_batch_too_large",
                extra={"item_count": len(items), "max": MAX_ITEMS_PER_BATCH},
            )
            raise TaskInputInvalidError(
                "comprehend batch exceeds the 25-item limit",
                item_count=len(items),
            )

        skipped: list[str] = []
        analyzable: list[AnalysisItem] = []
        for item in items:
            if is_empty_marker(item.respuesta):
                self._repository.skip_item(item.analysis_id, item.item_id)
                item.status = ItemStatus.SKIPPED
                skipped.append(item.item_id)
            else:
                analyzable.append(item)

        to_analyze: list[str] = []
        failed: list[str] = []
        if analyzable:
            self._classify_analyzable(analyzable, to_analyze, failed)

        result = BatchSentimentResult(
            to_analyze=tuple(to_analyze),
            skipped=tuple(skipped),
            failed=tuple(failed),
        )

        # Conservación de ítems (criterio 6.5): la suma de los tres conjuntos
        # disjuntos debe ser exactamente el número de Ítems del Batch. Se verifica
        # explícitamente para no perder nunca un Ítem en silencio.
        if result.total != len(items):  # pragma: no cover - invariante defensiva
            raise AssertionError(
                "La clasificación del Batch no conserva los Ítems: "
                f"{result.total} clasificados de {len(items)} recibidos."
            )

        logger.info(
            "comprehend_batch_classified",
            extra={
                "item_count": len(items),
                "to_analyze": len(to_analyze),
                "skipped": len(skipped),
                "failed": len(failed),
            },
        )
        return result

    def _classify_analyzable(
        self,
        analyzable: list[AnalysisItem],
        to_analyze: list[str],
        failed: list[str],
    ) -> None:
        """Detecta idioma, agrupa y reconcilia el sentimiento de los Ítems analizables.

        Muta ``to_analyze`` y ``failed`` añadiéndoles los ``item_id`` de cada
        desenlace, y persiste los resultados clásicos y las transiciones ``FAILED``
        a través del repositorio. Los Ítems con sentimiento válido quedan en
        ``PENDING`` (criterio 6.10): esta capa solo persiste su resultado clásico,
        nunca los marca terminales.

        :param analyzable: Ítems del Batch que no son Marcadores_Vacios.
        :param to_analyze: lista acumuladora de ``item_id`` con éxito (mutada).
        :param failed: lista acumuladora de ``item_id`` fallidos (mutada).
        """
        # Detección de idioma en una única llamada batch (criterio 6.1). La
        # postcondición de longitud del puerto garantiza un idioma por Ítem.
        languages = self._detector.detect_languages(
            [item.respuesta for item in analyzable]
        )

        # Agrupación por idioma preservando el orden de aparición (criterio 6.2).
        # Cada grupo guarda los Ítems y conserva su posición local, que es el
        # ``Index`` con el que Comprehend referirá sus resultados.
        groups: dict[str, list[AnalysisItem]] = {}
        for item, language in zip(analyzable, languages, strict=True):
            groups.setdefault(language, []).append(item)

        for language, group in groups.items():
            self._reconcile_language_group(language, group, to_analyze, failed)

    def _reconcile_language_group(
        self,
        language: str,
        group: list[AnalysisItem],
        to_analyze: list[str],
        failed: list[str],
    ) -> None:
        """Invoca Comprehend para un grupo de un mismo idioma y reconcilia por ``Index``.

        Emite una única llamada ``detect_sentiments`` para el grupo (criterio
        6.2) y asocia cada resultado por el ``Index`` que devuelve Comprehend
        —la posición local dentro de ``group``—, nunca por la posición en la
        lista devuelta (criterio 6.8). Clasifica cada Ítem del grupo en exactamente
        un desenlace:

        * ``Index`` en los éxitos → se persiste su ``ClassicSentiment`` (con el
          ``item_id`` real) y el Ítem queda en ``PENDING`` para el LLM (6.10).
        * ``Index`` en los errores → ``FAILED`` con ``COMPREHEND_ITEM_FAILED`` (6.7).
        * ``Index`` ausente de ambas listas → ``FAILED`` con
          ``COMPREHEND_RESULT_MISSING`` (6.13).

        El fallo de la llamada entera propaga sin captura (criterio 6.15): como
        ``detect_sentiments`` se invoca antes de cualquier escritura de este grupo,
        no queda resultado parcial y los Ítems del grupo siguen en ``PENDING``.

        :param language: código de idioma común a todos los Ítems del grupo.
        :param group: Ítems de ese idioma; su posición es el ``Index`` esperado.
        :param to_analyze: lista acumuladora de ``item_id`` con éxito (mutada).
        :param failed: lista acumuladora de ``item_id`` fallidos (mutada).
        """
        successes, errors = self._detector.detect_sentiments(
            [item.respuesta for item in group], language
        )

        # Reconstrucción de los resultados con el ``item_id`` real: el puerto los
        # construye con ``item_id = ""`` porque no conoce la identidad del Ítem
        # (criterio 6.8). Aquí se asocian por ``Index`` (posición en el grupo).
        classic_results: list[ClassicSentiment] = []
        for index, sentiment in successes.items():
            if not 0 <= index < len(group):
                # Un ``Index`` fuera de rango es una anomalía de la API; se ignora
                # para no romper la reconciliación. El Ítem correspondiente, si
                # existe, caerá en el camino de "resultado ausente" más abajo.
                continue
            item = group[index]
            classic_results.append(
                self._with_item_id(sentiment, item.item_id)
            )

        # Persistencia de los éxitos del grupo tras completar la llamada: así, si
        # la llamada hubiera fallado, no habría resultado parcial (criterio 6.15).
        if classic_results:
            self._repository.save_classic_results(classic_results)

        # Clasificación final de cada Ítem del grupo por su ``Index`` local.
        for index, item in enumerate(group):
            if index in successes:
                # Éxito clásico: permanece en PENDING para el análisis profundo.
                to_analyze.append(item.item_id)
            elif index in errors:
                # Fallo permanente del Ítem reportado por Comprehend (6.7).
                self._repository.fail_item(
                    item.analysis_id, item.item_id, errors[index]
                )
                item.status = ItemStatus.FAILED
                item.error_code = errors[index]
                failed.append(item.item_id)
            else:
                # Ausente de éxitos y errores: nunca se omite un Ítem (6.13).
                self._repository.fail_item(
                    item.analysis_id, item.item_id, COMPREHEND_RESULT_MISSING
                )
                item.status = ItemStatus.FAILED
                item.error_code = COMPREHEND_RESULT_MISSING
                failed.append(item.item_id)

    @staticmethod
    def _with_item_id(
        sentiment: ClassicSentiment, item_id: str
    ) -> ClassicSentiment:
        """Devuelve una copia del ``ClassicSentiment`` con el ``item_id`` real asignado.

        El puerto construye los resultados con ``item_id = ""`` porque no conoce la
        identidad del Ítem; el servicio, que sí la conoce por la asociación por
        ``Index``, la fija aquí antes de persistir (criterio 6.8). Como
        ``ClassicSentiment`` es inmutable (``frozen``), se crea una instancia nueva.

        :param sentiment: resultado clásico devuelto por el puerto, con ``item_id``
            vacío.
        :param item_id: identificador real del Ítem al que pertenece el resultado.
        :return: un ``ClassicSentiment`` idéntico pero con ``item_id`` asignado.
        """
        return ClassicSentiment(
            item_id=item_id,
            language_code=sentiment.language_code,
            language_confidence=sentiment.language_confidence,
            sentimiento_general=sentiment.sentimiento_general,
            scores=sentiment.scores,
        )
