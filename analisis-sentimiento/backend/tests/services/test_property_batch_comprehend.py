"""Prueba de propiedad **P10**: conservación de ítems en la capa batch de Comprehend.

Tarea 7.4 del plan de ``serverless-sentiment-pipeline``. Esta prueba verifica,
con **Hypothesis**, la propiedad de correctitud P10 de la sección 19 del diseño
(``.kiro/specs/serverless-sentiment-pipeline/design.md``, "Correctness Properties"):

> **Property 10 — Conservación de ítems en la capa batch de Comprehend.**
> Para todo batch, la unión disjunta de los conjuntos de éxitos (``to_analyze``),
> saltados (``skipped``) y fallidos (``failed``) es exactamente el conjunto de
> índices de entrada. Esta propiedad cubre el modo de fallo más sutil del diseño:
> perder silenciosamente un Ítem cuando Comprehend reordena, omite o reporta
> errores para parte de los Ítems enviados.

**Validates: Requirements 6.5, 6.6, 6.7, 6.13**

Qué se genera
-------------
Para **cualquier** Batch de 1 a 25 Ítems (criterio 6.11) y **cualquier**
respuesta posible de Comprehend, la propiedad debe sostenerse. El espacio de
entradas que se explora cubre, por construcción, los cuatro desenlaces y sus
combinaciones adversas:

* Marcadores_Vacios mezclados con texto analizable, en posiciones arbitrarias
  (criterio 6.6 → ``skipped``).
* Varios idiomas en el mismo Batch, de modo que la agrupación por idioma y la
  reconciliación por ``Index`` **local** de cada grupo se ejercitan (criterio 6.2).
* Una respuesta de Comprehend por grupo de idioma que es un subconjunto
  **arbitrario** de los índices del grupo, repartidos entre éxitos y errores de
  forma **disjunta**, con huecos (índices ausentes de ambas listas) y sin ningún
  orden particular en los diccionarios. Esto fuerza:
  * ``ErrorList`` no vacío → ``failed`` con ``COMPREHEND_ITEM_FAILED`` (criterio 6.7);
  * índices ausentes de éxitos y errores → ``failed`` con
    ``COMPREHEND_RESULT_MISSING`` (criterio 6.13);
  * reordenación y omisión sin pérdida de ningún Ítem (criterio 6.5).

El doble del ``SentimentDetector`` deriva su respuesta de un plan generado por
Hypothesis, por lo que es "arbitrario pero consistente" con el grupo real que el
servicio le pasa: las claves de éxitos/errores que devuelve son siempre índices
locales válidos del grupo, tal como lo garantiza el contrato del puerto
(:meth:`SentimentDetector.detect_sentiments`), pero su reparto y sus huecos los
elige el generador.

La invariante
-------------
``item_id`` es función determinista de ``(analysis_id, index)`` (``domain/ids``),
así que el índice de cada Ítem se recupera sin ambigüedad desde el ``item_id``
que devuelve :class:`BatchSentimentResult`. La aserción central es:

* la **unión** de los índices de ``to_analyze``, ``skipped`` y ``failed`` es
  exactamente el conjunto de índices de entrada (ninguno se pierde);
* los tres conjuntos son **disjuntos** (ningún Ítem se cuenta dos veces): se
  comprueba contando ``item_id`` con y sin deduplicar.

Pureza y aislamiento
--------------------
Como el resto de las pruebas de esta capa, se usan dobles en memoria de los
puertos ``SentimentDetector`` y ``AnalysisRepository`` (sin red, sin ``boto3``):
la propiedad valida la lógica de reconciliación del servicio, no la API real de
Comprehend (que se cubre con ``moto`` en las pruebas de contrato del adaptador).
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from hypothesis import given
from hypothesis import strategies as st

from sentiment.domain.empty_marker import EMPTY_MARKERS, is_empty_marker
from sentiment.domain.ids import make_item_id, new_analysis_id
from sentiment.domain.models import (
    AnalysisItem,
    ClassicSentiment,
    ItemStatus,
    SentimentScores,
    SentimientoGeneral,
)
from sentiment.domain.ports import AnalysisRepository, SentimentDetector
from sentiment.services.analyze_batch_sentiment import (
    MAX_ITEMS_PER_BATCH,
    AnalyzeBatchSentimentService,
)

# ``analysis_id`` fijo y válido para derivar ``item_id`` deterministas: la
# propiedad explora el espacio de Batches, no el de identificadores de Lote (eso
# lo cubre P7). Un único ULID es suficiente y mantiene la reversibilidad
# ``item_id -> index``.
_ANALYSIS_ID = new_analysis_id()
_INDIFICADOR = "liderazgo"
_COMPREHEND_ITEM_FAILED = "COMPREHEND_ITEM_FAILED"

# Idiomas que el doble puede asignar. Incluir más de uno obliga al servicio a
# agrupar por idioma y a reconciliar por el ``Index`` LOCAL de cada grupo, que es
# justamente donde un Batch con varios idiomas podría perder un Ítem si la
# asociación se hiciera por posición global.
_LANGUAGES = ("es", "en", "pt")


# ---------------------------------------------------------------------------
# Dobles en memoria de los puertos
# ---------------------------------------------------------------------------
class _PlannedDetector:
    """Doble del ``SentimentDetector`` cuya respuesta la dicta un plan generado.

    El plan asigna a cada **texto** (clave) un idioma, y a cada **grupo de idioma**
    la decisión por posición local: ``"success"``, ``"error"`` u omisión. Como el
    servicio invoca ``detect_sentiments`` con los textos de un grupo en su orden
    local, el doble reconstruye, para cada texto recibido, su posición en el grupo
    y aplica la decisión planificada, devolviendo diccionarios indexados por el
    ``Index`` local —tal como exige el contrato del puerto— con huecos y sin orden
    garantizado.

    Attributes:
        language_calls: textos pasados a cada ``detect_languages`` (para auditar).
        sentiment_calls: ``(textos, idioma)`` de cada ``detect_sentiments``.
    """

    def __init__(
        self,
        *,
        language_of_text: dict[str, str],
        decision_of_text: dict[str, str],
    ) -> None:
        self._language_of_text = language_of_text
        self._decision_of_text = decision_of_text
        self.language_calls: list[list[str]] = []
        self.sentiment_calls: list[tuple[list[str], str]] = []

    def detect_languages(self, texts: list[str]) -> list[str]:
        # Postcondición de longitud del puerto: un idioma por texto, mismo orden,
        # con fallback ``es`` (criterio 6.14). Base de la conservación (P10).
        self.language_calls.append(list(texts))
        return [self._language_of_text.get(text, "es") for text in texts]

    def detect_sentiments(
        self, texts: list[str], language_code: str
    ) -> tuple[dict[int, ClassicSentiment], dict[int, str]]:
        self.sentiment_calls.append((list(texts), language_code))
        successes: dict[int, ClassicSentiment] = {}
        errors: dict[int, str] = {}
        for local_index, text in enumerate(texts):
            decision = self._decision_of_text.get(text, "missing")
            if decision == "success":
                successes[local_index] = _classic_sentiment(language_code)
            elif decision == "error":
                errors[local_index] = _COMPREHEND_ITEM_FAILED
            # "missing": ni éxito ni error -> índice ausente de ambas listas, que
            # el servicio debe clasificar como COMPREHEND_RESULT_MISSING (6.13).
        return successes, errors


class _RecordingRepository:
    """Doble del ``AnalysisRepository`` que solo registra lo escrito.

    La propiedad P10 observa el resultado de la clasificación, no el estado
    persistido; aun así se registran las escrituras para no acoplar la prueba a
    efectos colaterales y permitir aserciones de apoyo (idempotencia de la cuenta).
    """

    def __init__(self) -> None:
        self.skipped: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.saved_classic: list[ClassicSentiment] = []

    def skip_item(self, analysis_id: str, item_id: str) -> bool:
        self.skipped.append(item_id)
        return True

    def fail_item(self, analysis_id: str, item_id: str, error_code: str) -> bool:
        self.failed.append((item_id, error_code))
        return True

    def save_classic_results(self, results: list[ClassicSentiment]) -> None:
        self.saved_classic.extend(results)


def _classic_sentiment(language: str) -> ClassicSentiment:
    """``ClassicSentiment`` con ``item_id`` vacío, como lo construye el puerto real."""
    return ClassicSentiment(
        item_id="",
        language_code=language,
        language_confidence=Decimal("0"),
        sentimiento_general=SentimientoGeneral.POSITIVO,
        scores=SentimentScores(
            positive=Decimal("0.9"),
            negative=Decimal("0.05"),
            neutral=Decimal("0.03"),
            mixed=Decimal("0.02"),
        ),
    )


def _service(
    detector: _PlannedDetector, repo: _RecordingRepository
) -> AnalyzeBatchSentimentService:
    return AnalyzeBatchSentimentService(
        detector=cast(SentimentDetector, detector),
        repository=cast(AnalysisRepository, repo),
    )


def _index_of(item_id: str) -> int:
    """Recupera el ``index`` 0-based desde un ``item_id`` ``{analysis_id}#{index:05d}``."""
    return int(item_id.rsplit("#", 1)[1])


# ---------------------------------------------------------------------------
# Estrategias
# ---------------------------------------------------------------------------
# Texto analizable: NO debe coincidir con ningún Marcador_Vacio tras normalizar.
# Se exige unicidad por texto en el Batch (``unique_by`` del generador del Batch)
# para que el plan ``texto -> idioma``/``texto -> decisión`` sea inequívoco.
_analyzable_text = st.text(min_size=1, max_size=40).filter(
    lambda s: not is_empty_marker(s)
)

# Un Marcador_Vacio: cualquiera del conjunto cerrado, con variación de caja y
# espacios para ejercitar la normalización del detector de marcadores (6.6).
_empty_marker_text = st.sampled_from(sorted(EMPTY_MARKERS)).flatmap(
    lambda m: st.sampled_from([m, m.upper(), f"  {m}  ", m.title()])
)


@st.composite
def _batch_plan(
    draw: st.DrawFn,
) -> tuple[list[AnalysisItem], dict[str, str], dict[str, str]]:
    """Genera ``(items, language_of_text, decision_of_text)`` para un Batch.

    Devuelve:
        * ``items``: de 1 a 25 ``AnalysisItem`` en ``PENDING``, con ``index``
          0-based consecutivo e ``item_id`` determinista; cada Ítem es un
          Marcador_Vacio o un texto analizable, en orden arbitrario.
        * ``language_of_text``: idioma asignado a cada texto analizable (varios
          idiomas posibles en el mismo Batch).
        * ``decision_of_text``: para cada texto analizable, el desenlace que
          Comprehend simulará (``success`` / ``error`` / ``missing``).

    Las respuestas analizables se fuerzan únicas dentro del Batch para que el plan
    por texto sea determinista; los Marcadores_Vacios pueden repetirse (no entran
    en el plan de Comprehend).
    """
    size = draw(st.integers(min_value=1, max_value=MAX_ITEMS_PER_BATCH))

    # Pool de textos analizables únicos, suficiente para el Batch; se mezclan con
    # marcadores por posición más abajo.
    analyzable_pool = draw(
        st.lists(_analyzable_text, min_size=0, max_size=size, unique=True)
    )

    items: list[AnalysisItem] = []
    language_of_text: dict[str, str] = {}
    decision_of_text: dict[str, str] = {}
    pool_cursor = 0
    for index in range(size):
        # ¿Marcador_Vacio o analizable? Si ya se agotó el pool de analizables en
        # esta posición, se usa un marcador para no repetir texto analizable.
        use_marker = draw(st.booleans()) or pool_cursor >= len(analyzable_pool)
        if use_marker:
            respuesta = draw(_empty_marker_text)
        else:
            respuesta = analyzable_pool[pool_cursor]
            pool_cursor += 1
            language_of_text[respuesta] = draw(st.sampled_from(_LANGUAGES))
            decision_of_text[respuesta] = draw(
                st.sampled_from(["success", "error", "missing"])
            )
        items.append(
            AnalysisItem(
                analysis_id=_ANALYSIS_ID,
                item_id=make_item_id(_ANALYSIS_ID, index),
                index=index,
                indificador=_INDIFICADOR,
                respuesta=respuesta,
                status=ItemStatus.PENDING,
            )
        )

    return items, language_of_text, decision_of_text


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------
@given(plan=_batch_plan())
def test_comprehend_batch_conserves_every_item(
    plan: tuple[list[AnalysisItem], dict[str, str], dict[str, str]],
) -> None:
    """P10: la unión disjunta de to_analyze/skipped/failed cubre todo el Batch.

    **Validates: Requirements 6.5, 6.6, 6.7, 6.13**

    Para cualquier Batch y cualquier respuesta de Comprehend (reordenada, parcial,
    con errores y con huecos), cada Ítem de entrada termina en exactamente uno de
    los tres conjuntos: ni se pierde ni se duplica ninguno (criterio 6.5).
    """
    items, language_of_text, decision_of_text = plan
    detector = _PlannedDetector(
        language_of_text=language_of_text,
        decision_of_text=decision_of_text,
    )
    repo = _RecordingRepository()

    result = _service(detector, repo).analyze(items)

    input_indices = {item.index for item in items}

    to_analyze_indices = {_index_of(item_id) for item_id in result.to_analyze}
    skipped_indices = {_index_of(item_id) for item_id in result.skipped}
    failed_indices = {_index_of(item_id) for item_id in result.failed}

    # (1) Conservación: la UNIÓN cubre exactamente los índices de entrada. Ningún
    # Ítem se pierde, aunque Comprehend reordene u omita resultados (6.5, 6.13).
    assert (
        to_analyze_indices | skipped_indices | failed_indices == input_indices
    )

    # (2) Disjunción / sin duplicados: contar los item_id con y sin deduplicar da
    # el mismo total, igual al número de Ítems del Batch. Ningún Ítem aparece en
    # dos conjuntos ni dos veces en el mismo.
    all_item_ids = list(result.to_analyze) + list(result.skipped) + list(result.failed)
    assert len(all_item_ids) == len(set(all_item_ids))
    assert len(all_item_ids) == len(items)
    assert result.total == len(items)

    # Los índices de los tres conjuntos son además disjuntos entre sí (la unión
    # con la cuenta exacta ya lo implica, pero se afirma explícitamente).
    assert to_analyze_indices.isdisjoint(skipped_indices)
    assert to_analyze_indices.isdisjoint(failed_indices)
    assert skipped_indices.isdisjoint(failed_indices)


@given(plan=_batch_plan())
def test_classification_matches_marker_and_comprehend_plan(
    plan: tuple[list[AnalysisItem], dict[str, str], dict[str, str]],
) -> None:
    """Refuerzo de P10: cada Ítem cae en el conjunto que su desenlace dicta.

    **Validates: Requirements 6.6, 6.7, 6.13**

    Más allá de la conservación agregada, se comprueba el destino concreto de cada
    Ítem según la regla del diseño:

    * Marcador_Vacio -> ``skipped`` (criterio 6.6);
    * analizable con éxito de Comprehend -> ``to_analyze`` (sigue PENDING, 6.10);
    * analizable en ``ErrorList`` -> ``failed`` con ``COMPREHEND_ITEM_FAILED`` (6.7);
    * analizable ausente de ambas listas -> ``failed`` con
      ``COMPREHEND_RESULT_MISSING`` (6.13).
    """
    items, language_of_text, decision_of_text = plan
    detector = _PlannedDetector(
        language_of_text=language_of_text,
        decision_of_text=decision_of_text,
    )
    repo = _RecordingRepository()

    result = _service(detector, repo).analyze(items)

    to_analyze_ids = set(result.to_analyze)
    skipped_ids = set(result.skipped)
    failed_ids = set(result.failed)
    failed_codes = dict(repo.failed)

    for item in items:
        if is_empty_marker(item.respuesta):
            assert item.item_id in skipped_ids
            assert item.status is ItemStatus.SKIPPED
            continue

        decision = decision_of_text[item.respuesta]
        if decision == "success":
            # Éxito clásico: permanece en PENDING para el análisis profundo (6.10).
            assert item.item_id in to_analyze_ids
            assert item.status is ItemStatus.PENDING
        elif decision == "error":
            assert item.item_id in failed_ids
            assert item.status is ItemStatus.FAILED
            assert failed_codes[item.item_id] == _COMPREHEND_ITEM_FAILED
        else:  # "missing": ausente de éxitos y errores (6.13)
            assert item.item_id in failed_ids
            assert item.status is ItemStatus.FAILED
            assert failed_codes[item.item_id] == "COMPREHEND_RESULT_MISSING"
