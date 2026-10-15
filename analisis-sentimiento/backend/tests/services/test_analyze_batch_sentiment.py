"""Pruebas unitarias de ``AnalyzeBatchSentimentService`` (tarea 7.3).

Verifican el comportamiento observable del caso de uso del análisis clásico por
Batch con dobles **en memoria** de los puertos ``SentimentDetector`` y
``AnalysisRepository`` (sin red, sin boto3):

* el tope de 25 Ítems: más de 25 falla con ``TASK_INPUT_INVALID`` sin invocar a
  Comprehend ni tocar la persistencia (criterios 6.11, 6.12);
* los Marcadores_Vacios se transicionan a ``SKIPPED`` y no se envían a Comprehend
  (criterios 6.6, 18.6);
* la agrupación por idioma emite una llamada ``detect_sentiments`` por idioma
  distinto (criterio 6.2);
* la reconciliación por ``Index`` asocia cada resultado por el índice que devuelve
  Comprehend, no por la posición en la lista, aun cuando se reordenan u omiten
  resultados (criterios 6.8, 6.13);
* los éxitos clásicos persisten su ``ClassicSentiment`` con el ``item_id`` real y
  **permanecen en PENDING** para el LLM (criterio 6.10);
* los Ítems del ``ErrorList`` se marcan ``FAILED`` con ``COMPREHEND_ITEM_FAILED``
  (criterio 6.7) y los ausentes de ambas listas con ``COMPREHEND_RESULT_MISSING``
  (criterio 6.13);
* la conservación de ítems: los tres conjuntos son disjuntos y cubren el Batch
  (criterio 6.5);
* un fallo de la llamada entera propaga sin dejar resultado parcial (criterio 6.15).

La prueba de propiedad P10 (conservación con entradas generadas) es la tarea 7.4;
aquí se cubren ejemplos concretos y los bordes esenciales.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from sentiment.domain.errors import TaskInputInvalidError, TransientDependencyError
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
    COMPREHEND_RESULT_MISSING,
    AnalyzeBatchSentimentService,
)

_ANALYSIS_ID = new_analysis_id()
_INDIFICADOR = "liderazgo"
_COMPREHEND_ITEM_FAILED = "COMPREHEND_ITEM_FAILED"


# ---------------------------------------------------------------------------
# Dobles en memoria de los puertos
# ---------------------------------------------------------------------------
class _FakeDetector:
    """Doble en memoria del puerto ``SentimentDetector``.

    ``language_map`` asigna un idioma por texto (fallback ``"es"``);
    ``sentiment_by_language`` prescribe, por idioma, los diccionarios de éxitos y
    errores que ``detect_sentiments`` devuelve, indexados por el ``Index`` local
    (posición en el grupo). ``raise_for_language`` fuerza el fallo de la llamada
    entera de un idioma concreto (criterio 6.15).
    """

    def __init__(
        self,
        *,
        language_map: dict[str, str] | None = None,
        sentiment_by_language: dict[
            str, tuple[dict[int, ClassicSentiment], dict[int, str]]
        ]
        | None = None,
        raise_for_language: str | None = None,
    ) -> None:
        self._language_map = language_map or {}
        self._sentiment_by_language = sentiment_by_language or {}
        self._raise_for_language = raise_for_language
        self.language_calls: list[list[str]] = []
        self.sentiment_calls: list[tuple[list[str], str]] = []

    def detect_languages(self, texts: list[str]) -> list[str]:
        self.language_calls.append(list(texts))
        return [self._language_map.get(text, "es") for text in texts]

    def detect_sentiments(
        self, texts: list[str], language_code: str
    ) -> tuple[dict[int, ClassicSentiment], dict[int, str]]:
        self.sentiment_calls.append((list(texts), language_code))
        if self._raise_for_language == language_code:
            raise TransientDependencyError("comprehend unavailable")
        return self._sentiment_by_language.get(language_code, ({}, {}))


class _FakeRepository:
    """Doble en memoria del puerto ``AnalysisRepository`` para esta capa.

    Registra las transiciones ``SKIPPED``/``FAILED`` y los resultados clásicos
    persistidos, de modo que las aserciones comprueben qué se escribió y qué no.
    Solo implementa los tres métodos que el servicio usa.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _item(index: int, respuesta: str) -> AnalysisItem:
    """Construye un ``AnalysisItem`` en ``PENDING`` con ``item_id`` determinista."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=make_item_id(_ANALYSIS_ID, index),
        index=index,
        indificador=_INDIFICADOR,
        respuesta=respuesta,
        status=ItemStatus.PENDING,
    )


def _sentiment(label: SentimientoGeneral, language: str = "es") -> ClassicSentiment:
    """Construye un ``ClassicSentiment`` con ``item_id`` vacío, como lo hace el puerto."""
    return ClassicSentiment(
        item_id="",
        language_code=language,
        language_confidence=Decimal("0"),
        sentimiento_general=label,
        scores=SentimentScores(
            positive=Decimal("0.9"),
            negative=Decimal("0.05"),
            neutral=Decimal("0.03"),
            mixed=Decimal("0.02"),
        ),
    )


def _service(
    detector: _FakeDetector, repo: _FakeRepository
) -> AnalyzeBatchSentimentService:
    # Los dobles implementan solo los métodos que el servicio consume de cada
    # puerto; se tipan como el puerto para la inyección sin arrastrar el resto
    # del contrato (que este caso de uso no usa).
    return AnalyzeBatchSentimentService(
        detector=cast(SentimentDetector, detector),
        repository=cast(AnalysisRepository, repo),
    )


# ---------------------------------------------------------------------------
# Tope de 25 Ítems (criterios 6.11, 6.12)
# ---------------------------------------------------------------------------
def test_more_than_25_items_fails_without_touching_dependencies() -> None:
    detector = _FakeDetector()
    repo = _FakeRepository()
    items = [_item(i, f"respuesta {i}") for i in range(26)]

    with pytest.raises(TaskInputInvalidError) as exc_info:
        _service(detector, repo).analyze(items)

    assert exc_info.value.error_code == "TASK_INPUT_INVALID"
    # Ni Comprehend ni la persistencia se tocan (criterio 6.12).
    assert detector.language_calls == []
    assert detector.sentiment_calls == []
    assert repo.skipped == []
    assert repo.failed == []
    assert repo.saved_classic == []


def test_exactly_25_items_is_accepted() -> None:
    language = "es"
    successes = {i: _sentiment(SentimientoGeneral.POSITIVO) for i in range(25)}
    detector = _FakeDetector(
        sentiment_by_language={language: (successes, {})}
    )
    repo = _FakeRepository()
    items = [_item(i, f"respuesta {i}") for i in range(25)]

    result = _service(detector, repo).analyze(items)

    assert len(result.to_analyze) == 25
    assert result.total == 25


# ---------------------------------------------------------------------------
# Marcadores_Vacios -> SKIPPED (criterios 6.6, 18.6)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "respuesta",
    ["", "   ", "Sin comentarios", "SIN COMENTARIOS", " n/a ", "ninguno", "-"],
)
def test_empty_markers_are_skipped_without_comprehend(respuesta: str) -> None:
    detector = _FakeDetector()
    repo = _FakeRepository()
    items = [_item(0, respuesta)]

    result = _service(detector, repo).analyze(items)

    assert result.skipped == (items[0].item_id,)
    assert result.to_analyze == ()
    assert result.failed == ()
    assert items[0].status is ItemStatus.SKIPPED
    assert repo.skipped == [items[0].item_id]
    # Un Batch solo de marcadores no invoca a Comprehend en absoluto.
    assert detector.language_calls == []
    assert detector.sentiment_calls == []


def test_mixed_batch_only_sends_analyzable_to_comprehend() -> None:
    language = "es"
    # Dos analizables (índices 0 y 2); el 1 es un Marcador_Vacio.
    successes = {
        0: _sentiment(SentimientoGeneral.POSITIVO),
        1: _sentiment(SentimientoGeneral.NEGATIVO),
    }
    detector = _FakeDetector(sentiment_by_language={language: (successes, {})})
    repo = _FakeRepository()
    items = [
        _item(0, "me gusta el liderazgo"),
        _item(1, "sin comentarios"),
        _item(2, "podria mejorar"),
    ]

    result = _service(detector, repo).analyze(items)

    assert result.skipped == (items[1].item_id,)
    assert set(result.to_analyze) == {items[0].item_id, items[2].item_id}
    # Comprehend solo recibió los dos textos analizables, en orden.
    assert detector.language_calls == [["me gusta el liderazgo", "podria mejorar"]]
    assert len(detector.sentiment_calls) == 1
    assert detector.sentiment_calls[0][0] == [
        "me gusta el liderazgo",
        "podria mejorar",
    ]


# ---------------------------------------------------------------------------
# Agrupación por idioma (criterio 6.2)
# ---------------------------------------------------------------------------
def test_distinct_languages_produce_one_sentiment_call_each() -> None:
    detector = _FakeDetector(
        language_map={"hola": "es", "hello": "en"},
        sentiment_by_language={
            "es": ({0: _sentiment(SentimientoGeneral.POSITIVO, "es")}, {}),
            "en": ({0: _sentiment(SentimientoGeneral.NEUTRAL, "en")}, {}),
        },
    )
    repo = _FakeRepository()
    items = [_item(0, "hola"), _item(1, "hello")]

    result = _service(detector, repo).analyze(items)

    # Exactamente una llamada de detección de idioma y una de sentimiento por idioma.
    assert len(detector.language_calls) == 1
    assert len(detector.sentiment_calls) == 2
    called_languages = {lang for _, lang in detector.sentiment_calls}
    assert called_languages == {"es", "en"}
    assert set(result.to_analyze) == {items[0].item_id, items[1].item_id}
    # Cada resultado se persiste con el idioma correcto y el item_id real.
    by_item = {c.item_id: c for c in repo.saved_classic}
    assert by_item[items[0].item_id].language_code == "es"
    assert by_item[items[1].item_id].language_code == "en"


# ---------------------------------------------------------------------------
# Reconciliación por Index y persistencia (criterios 6.8, 6.10)
# ---------------------------------------------------------------------------
def test_success_items_stay_pending_and_persist_classic_with_real_item_id() -> None:
    language = "es"
    successes = {
        0: _sentiment(SentimientoGeneral.POSITIVO),
        1: _sentiment(SentimientoGeneral.MIXTO),
    }
    detector = _FakeDetector(sentiment_by_language={language: (successes, {})})
    repo = _FakeRepository()
    items = [_item(0, "texto a"), _item(1, "texto b")]

    result = _service(detector, repo).analyze(items)

    assert set(result.to_analyze) == {items[0].item_id, items[1].item_id}
    # Permanecen PENDING: esta capa no los marca DONE (criterio 6.10).
    assert all(it.status is ItemStatus.PENDING for it in items)
    # Se persistió el resultado clásico con el item_id real, no "".
    saved_ids = {c.item_id for c in repo.saved_classic}
    assert saved_ids == {items[0].item_id, items[1].item_id}
    assert all(c.item_id != "" for c in repo.saved_classic)


def test_reconciliation_is_by_index_not_list_position() -> None:
    """Comprehend devuelve el Index 1 como éxito y el 0 como error, en ese orden."""
    language = "es"
    successes = {1: _sentiment(SentimientoGeneral.POSITIVO)}
    errors = {0: _COMPREHEND_ITEM_FAILED}
    detector = _FakeDetector(sentiment_by_language={language: (successes, errors)})
    repo = _FakeRepository()
    items = [_item(0, "texto roto"), _item(1, "texto bueno")]

    result = _service(detector, repo).analyze(items)

    # El Index 1 (segundo Ítem) es el éxito; el Index 0 (primero) es el fallo.
    assert result.to_analyze == (items[1].item_id,)
    assert result.failed == (items[0].item_id,)
    assert repo.saved_classic[0].item_id == items[1].item_id
    assert repo.failed == [(items[0].item_id, _COMPREHEND_ITEM_FAILED)]


# ---------------------------------------------------------------------------
# ErrorList y resultado ausente (criterios 6.7, 6.13)
# ---------------------------------------------------------------------------
def test_error_list_items_fail_with_comprehend_item_failed() -> None:
    language = "es"
    detector = _FakeDetector(
        sentiment_by_language={language: ({}, {0: _COMPREHEND_ITEM_FAILED})}
    )
    repo = _FakeRepository()
    items = [_item(0, "texto no soportado")]

    result = _service(detector, repo).analyze(items)

    assert result.failed == (items[0].item_id,)
    assert items[0].status is ItemStatus.FAILED
    assert items[0].error_code == _COMPREHEND_ITEM_FAILED
    assert repo.failed == [(items[0].item_id, _COMPREHEND_ITEM_FAILED)]


def test_missing_from_both_lists_fails_with_result_missing() -> None:
    """Un Ítem analizable ausente de éxitos y errores no se pierde (criterio 6.13)."""
    language = "es"
    # Solo el Index 0 vuelve; el Index 1 falta en ambas listas.
    detector = _FakeDetector(
        sentiment_by_language={
            language: ({0: _sentiment(SentimientoGeneral.POSITIVO)}, {})
        }
    )
    repo = _FakeRepository()
    items = [_item(0, "texto a"), _item(1, "texto b")]

    result = _service(detector, repo).analyze(items)

    assert result.to_analyze == (items[0].item_id,)
    assert result.failed == (items[1].item_id,)
    assert items[1].status is ItemStatus.FAILED
    assert items[1].error_code == COMPREHEND_RESULT_MISSING
    assert repo.failed == [(items[1].item_id, COMPREHEND_RESULT_MISSING)]


# ---------------------------------------------------------------------------
# Conservación de ítems (criterio 6.5)
# ---------------------------------------------------------------------------
def test_item_conservation_across_all_three_outcomes() -> None:
    """Marcador, éxito, error y resultado ausente: los cuatro Ítems se conservan."""
    language = "es"
    # Analizables: índices 0,1,2 (el 3 es marcador). En el grupo: posiciones
    # locales 0->éxito, 1->error, 2->ausente.
    successes = {0: _sentiment(SentimientoGeneral.POSITIVO)}
    errors = {1: _COMPREHEND_ITEM_FAILED}
    detector = _FakeDetector(sentiment_by_language={language: (successes, errors)})
    repo = _FakeRepository()
    items = [
        _item(0, "bueno"),
        _item(1, "roto"),
        _item(2, "perdido"),
        _item(3, "n/a"),
    ]

    result = _service(detector, repo).analyze(items)

    assert result.total == 4
    all_ids = set(result.to_analyze) | set(result.skipped) | set(result.failed)
    assert all_ids == {it.item_id for it in items}
    # Conjuntos disjuntos.
    assert len(result.to_analyze) + len(result.skipped) + len(result.failed) == 4
    assert result.to_analyze == (items[0].item_id,)
    assert result.skipped == (items[3].item_id,)
    assert set(result.failed) == {items[1].item_id, items[2].item_id}


def test_empty_batch_returns_empty_result() -> None:
    detector = _FakeDetector()
    repo = _FakeRepository()

    result = _service(detector, repo).analyze([])

    assert result.total == 0
    assert detector.language_calls == []


# ---------------------------------------------------------------------------
# Fallo de la llamada entera (criterio 6.15)
# ---------------------------------------------------------------------------
def test_whole_call_failure_propagates_without_partial_result() -> None:
    detector = _FakeDetector(raise_for_language="es")
    repo = _FakeRepository()
    items = [_item(0, "texto a"), _item(1, "texto b")]

    with pytest.raises(TransientDependencyError):
        _service(detector, repo).analyze(items)

    # Ningún resultado parcial de sentimiento se persistió y los Ítems siguen PENDING.
    assert repo.saved_classic == []
    assert repo.failed == []
    assert all(it.status is ItemStatus.PENDING for it in items)
