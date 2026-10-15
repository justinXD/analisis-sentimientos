"""Pruebas unitarias del adaptador de Comprehend (tarea 6.2).

Verifican el comportamiento observable de
:class:`sentiment.adapters.comprehend_adapter.ComprehendAdapter` como
implementación del puerto ``SentimentDetector``:

* ``detect_languages`` es *conservadora de longitud* y aplica el fallback ``"es"``
  a los textos sin idioma, asociando por ``Index`` y no por posición en la lista
  (criterios 6.1, 6.14);
* ``detect_sentiments`` asocia cada resultado por el ``Index`` que devuelve
  Comprehend —incluso cuando la API reordena o omite resultados— y mapea el
  ``ErrorList`` a ``COMPREHEND_ITEM_FAILED`` (criterios 6.2, 6.7, 6.8, 6.13);
* los scores se persisten como ``Decimal`` acotado a ``[0, 1]`` y redondeado a 6
  decimales (criterio 6.9);
* un fallo de la llamada entera se eleva a la clase transitoria del dominio, sin
  dejar resultado parcial (criterios 6.15, 9.16, 9.18).

La asociación con el ``item_id`` del Ítem es responsabilidad del servicio
(``AnalyzeBatchSentimentService``, tarea 7.3) y las pruebas de contrato con AWS
simulado completo son la tarea 6.8; aquí se usa un doble determinista del cliente
de Comprehend para ejercer la lógica de reconciliación sin depender de la red.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

if TYPE_CHECKING:
    from botocore.client import BaseClient

from sentiment.adapters.comprehend_adapter import (
    COMPREHEND_ITEM_FAILED,
    DEFAULT_LANGUAGE_CODE,
    ComprehendAdapter,
)
from sentiment.domain.errors import ThrottledError, TransientDependencyError
from sentiment.domain.models import ClassicSentiment, SentimientoGeneral


class _FakeComprehendClient:
    """Doble determinista del cliente de Comprehend.

    Devuelve respuestas prefijadas por operación, o lanza la excepción
    configurada, de modo que las pruebas controlen con exactitud el ``Index``, el
    orden y la presencia/ausencia de resultados sin depender de un servicio real.
    """

    def __init__(
        self,
        *,
        language_response: dict[str, Any] | None = None,
        sentiment_response: dict[str, Any] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._language_response = language_response or {}
        self._sentiment_response = sentiment_response or {}
        self._raise_exc = raise_exc
        self.language_calls: list[list[str]] = []
        self.sentiment_calls: list[tuple[list[str], str]] = []

    def batch_detect_dominant_language(
        self, *, TextList: list[str]
    ) -> dict[str, Any]:
        self.language_calls.append(TextList)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._language_response

    def batch_detect_sentiment(
        self, *, TextList: list[str], LanguageCode: str
    ) -> dict[str, Any]:
        self.sentiment_calls.append((TextList, LanguageCode))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._sentiment_response


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "x"}},
        operation_name="BatchDetectSentiment",
    )


def _adapter(fake: _FakeComprehendClient) -> ComprehendAdapter:
    """Construye el adaptador con el doble de prueba.

    El puerto recibe un ``BaseClient``; el doble satisface estructuralmente las
    dos operaciones usadas, por lo que se estrecha con ``cast`` para el tipado
    estático sin acoplar el test al tipo concreto de boto3.
    """
    return ComprehendAdapter(client=cast("BaseClient", fake))


# ---------------------------------------------------------------------------
# detect_languages
# ---------------------------------------------------------------------------


def test_detect_languages_preserves_length_and_order() -> None:
    """Devuelve un idioma por texto, en orden, tomando el de mayor score (6.1)."""
    fake = _FakeComprehendClient(
        language_response={
            "ResultList": [
                {
                    "Index": 0,
                    "Languages": [
                        {"LanguageCode": "en", "Score": 0.2},
                        {"LanguageCode": "es", "Score": 0.9},
                    ],
                },
                {"Index": 1, "Languages": [{"LanguageCode": "pt", "Score": 0.99}]},
            ],
            "ErrorList": [],
        }
    )
    adapter = _adapter(fake)

    result = adapter.detect_languages(["hola", "ola"])

    assert result == ["es", "pt"]
    # Una sola llamada batch para todo el lote (criterio 6.1).
    assert len(fake.language_calls) == 1


def test_detect_languages_uses_index_not_position() -> None:
    """Asocia el idioma por el ``Index`` devuelto, no por la posición en la lista."""
    fake = _FakeComprehendClient(
        language_response={
            # Comprehend devuelve los resultados en orden inverso.
            "ResultList": [
                {"Index": 1, "Languages": [{"LanguageCode": "fr", "Score": 0.8}]},
                {"Index": 0, "Languages": [{"LanguageCode": "es", "Score": 0.8}]},
            ],
            "ErrorList": [],
        }
    )
    adapter = _adapter(fake)

    assert adapter.detect_languages(["uno", "dos"]) == ["es", "fr"]


def test_detect_languages_falls_back_to_es_for_missing_and_errored() -> None:
    """Un texto sin idioma (error o lista vacía) recibe el fallback ``es`` (6.14)."""
    fake = _FakeComprehendClient(
        language_response={
            "ResultList": [
                {"Index": 0, "Languages": [{"LanguageCode": "en", "Score": 0.9}]},
                {"Index": 2, "Languages": []},  # sin idiomas -> fallback
            ],
            "ErrorList": [{"Index": 1, "ErrorCode": "INTERNAL"}],  # error -> fallback
        }
    )
    adapter = _adapter(fake)

    result = adapter.detect_languages(["a", "b", "c"])

    assert result == ["en", DEFAULT_LANGUAGE_CODE, DEFAULT_LANGUAGE_CODE]
    assert len(result) == 3


def test_detect_languages_empty_batch_makes_no_call() -> None:
    """Un batch vacío devuelve ``[]`` sin invocar a Comprehend."""
    fake = _FakeComprehendClient()
    adapter = _adapter(fake)

    assert adapter.detect_languages([]) == []
    assert fake.language_calls == []


# ---------------------------------------------------------------------------
# detect_sentiments
# ---------------------------------------------------------------------------


def test_detect_sentiments_associates_by_index() -> None:
    """Asocia éxitos por el ``Index`` de Comprehend aunque reordene (6.8)."""
    fake = _FakeComprehendClient(
        sentiment_response={
            "ResultList": [
                {
                    "Index": 1,
                    "Sentiment": "NEGATIVE",
                    "SentimentScore": {
                        "Positive": 0.01,
                        "Negative": 0.95,
                        "Neutral": 0.02,
                        "Mixed": 0.02,
                    },
                },
                {
                    "Index": 0,
                    "Sentiment": "POSITIVE",
                    "SentimentScore": {
                        "Positive": 0.9,
                        "Negative": 0.05,
                        "Neutral": 0.03,
                        "Mixed": 0.02,
                    },
                },
            ],
            "ErrorList": [],
        }
    )
    adapter = _adapter(fake)

    successes, errors = adapter.detect_sentiments(["bueno", "malo"], "es")

    assert errors == {}
    assert set(successes) == {0, 1}
    assert isinstance(successes[0], ClassicSentiment)
    assert successes[0].sentimiento_general is SentimientoGeneral.POSITIVO
    assert successes[1].sentimiento_general is SentimientoGeneral.NEGATIVO
    assert successes[0].language_code == "es"
    # Una sola llamada batch para el grupo de idioma (criterio 6.2).
    assert len(fake.sentiment_calls) == 1
    assert fake.sentiment_calls[0] == (["bueno", "malo"], "es")


def test_detect_sentiments_maps_error_list_to_item_failed() -> None:
    """Un Ítem del ``ErrorList`` se reporta como ``COMPREHEND_ITEM_FAILED`` (6.7)."""
    fake = _FakeComprehendClient(
        sentiment_response={
            "ResultList": [
                {
                    "Index": 0,
                    "Sentiment": "NEUTRAL",
                    "SentimentScore": {
                        "Positive": 0.1,
                        "Negative": 0.1,
                        "Neutral": 0.7,
                        "Mixed": 0.1,
                    },
                }
            ],
            "ErrorList": [{"Index": 1, "ErrorCode": "TEXT_SIZE_LIMIT_EXCEEDED"}],
        }
    )
    adapter = _adapter(fake)

    successes, errors = adapter.detect_sentiments(["ok", "x" * 10000], "es")

    assert set(successes) == {0}
    assert errors == {1: COMPREHEND_ITEM_FAILED}
    # Claves disjuntas: un índice no aparece en ambos.
    assert set(successes).isdisjoint(errors)


def test_detect_sentiments_missing_index_absent_from_both_maps() -> None:
    """Un Ítem ausente de ambas listas no aparece en ningún diccionario (6.13)."""
    fake = _FakeComprehendClient(
        sentiment_response={
            "ResultList": [
                {
                    "Index": 0,
                    "Sentiment": "POSITIVE",
                    "SentimentScore": {
                        "Positive": 0.8,
                        "Negative": 0.1,
                        "Neutral": 0.05,
                        "Mixed": 0.05,
                    },
                }
            ],
            "ErrorList": [],
        }
    )
    adapter = _adapter(fake)

    successes, errors = adapter.detect_sentiments(["a", "b", "c"], "es")

    # El servicio clasificará 1 y 2 como COMPREHEND_RESULT_MISSING: el adaptador
    # simplemente no los inventa.
    assert set(successes) == {0}
    assert errors == {}


def test_detect_sentiments_unknown_label_is_no_determinado() -> None:
    """Una etiqueta fuera del conjunto conocido cae en ``No determinado`` (6.4)."""
    fake = _FakeComprehendClient(
        sentiment_response={
            "ResultList": [
                {
                    "Index": 0,
                    "Sentiment": "UNEXPECTED",
                    "SentimentScore": {
                        "Positive": 0.25,
                        "Negative": 0.25,
                        "Neutral": 0.25,
                        "Mixed": 0.25,
                    },
                }
            ],
            "ErrorList": [],
        }
    )
    adapter = _adapter(fake)

    successes, _ = adapter.detect_sentiments(["raro"], "es")

    assert successes[0].sentimiento_general is SentimientoGeneral.NO_DETERMINADO


def test_detect_sentiments_scores_are_decimal_rounded_to_six_places() -> None:
    """Los scores se persisten como ``Decimal`` en [0,1] con 6 decimales (6.9)."""
    fake = _FakeComprehendClient(
        sentiment_response={
            "ResultList": [
                {
                    "Index": 0,
                    "Sentiment": "POSITIVE",
                    "SentimentScore": {
                        "Positive": 0.1234567,  # redondea a 6 decimales
                        "Negative": 0.0,
                        "Neutral": 0.0,
                        "Mixed": 0.0,
                    },
                }
            ],
            "ErrorList": [],
        }
    )
    adapter = _adapter(fake)

    successes, _ = adapter.detect_sentiments(["x"], "es")
    scores = successes[0].scores

    assert isinstance(scores.positive, Decimal)
    assert scores.positive == Decimal("0.123457")  # ROUND_HALF_EVEN
    assert scores.positive.as_tuple().exponent == -6
    assert Decimal("0") <= scores.positive <= Decimal("1")


def test_detect_sentiments_empty_batch_makes_no_call() -> None:
    """Un batch vacío no invoca a Comprehend y devuelve mapas vacíos."""
    fake = _FakeComprehendClient()
    adapter = _adapter(fake)

    successes, errors = adapter.detect_sentiments([], "es")

    assert successes == {}
    assert errors == {}
    assert fake.sentiment_calls == []


# ---------------------------------------------------------------------------
# Traducción de errores de la llamada entera (clase transitoria)
# ---------------------------------------------------------------------------


def test_throttling_error_raises_throttled_error() -> None:
    """Un throttling de Comprehend se eleva a ``ThrottledError`` (transitorio)."""
    fake = _FakeComprehendClient(raise_exc=_client_error("ThrottlingException"))
    adapter = _adapter(fake)

    with pytest.raises(ThrottledError):
        adapter.detect_sentiments(["x"], "es")


def test_generic_client_error_raises_transient_dependency_error() -> None:
    """Un ``ClientError`` no-throttling se eleva a ``TransientDependencyError``."""
    fake = _FakeComprehendClient(raise_exc=_client_error("InternalServerException"))
    adapter = _adapter(fake)

    with pytest.raises(TransientDependencyError):
        adapter.detect_languages(["x"])


def test_botocore_error_raises_transient_dependency_error() -> None:
    """Un timeout de red (``BotoCoreError``) se eleva a la clase transitoria (6.15)."""
    fake = _FakeComprehendClient(
        raise_exc=ReadTimeoutError(endpoint_url="https://comprehend")
    )
    adapter = _adapter(fake)

    with pytest.raises(TransientDependencyError):
        adapter.detect_sentiments(["x"], "es")
