"""Pruebas de contrato del ``ComprehendAdapter`` con el SDK simulado (tarea 6.8).

La tarea 6.8 exige verificar sobre AWS simulado la **asociación por ``Index``**
del adaptador de Comprehend (criterio 6.8): cada resultado del batch se reconcilia
por el campo ``Index`` que devuelve la API, **no** por su posición en
``ResultList``. Confundir ambas cosas es el bug clásico de las APIs batch de AWS
—``len(ResultList) != len(TextList)`` y el orden no está garantizado— y es
exactamente lo que estas pruebas cierran.

AWS simulado con ``botocore.stub.Stubber``
------------------------------------------
Las operaciones batch de Comprehend (``batch_detect_sentiment`` /
``batch_detect_dominant_language``) no están implementadas en ``moto`` 5.x. Para
simular AWS de forma fiel al contrato real se usa ``botocore.stub.Stubber``, el
simulador que trae el propio SDK de AWS: encola respuestas que **deben** ajustarse
al modelo de la API (botocore valida la forma de la respuesta y los parámetros de
la petición contra su definición del servicio) y las sirve sin tocar la red. Es
la simulación de AWS idónea para un servicio que ``moto`` no cubre, y ejerce el
adaptador contra un cliente de boto3 real con la forma de respuesta real.

Todos los textos son **datos sintéticos** (criterio 17.10): respuestas de encuesta
inventadas, sin PII real.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.stub import Stubber

if TYPE_CHECKING:
    from botocore.client import BaseClient

from sentiment.adapters.comprehend_adapter import (
    COMPREHEND_ITEM_FAILED,
    DEFAULT_LANGUAGE_CODE,
    ComprehendAdapter,
)
from sentiment.domain.models import SentimientoGeneral

_REGION = "us-east-1"


@pytest.fixture
def comprehend_client() -> Iterator[BaseClient]:
    """Cede un cliente de Comprehend real sobre el que opera el ``Stubber``."""
    yield boto3.client("comprehend", region_name=_REGION)


def _sentiment_result(index: int, label: str) -> dict[str, Any]:
    """Entrada sintética de ``ResultList`` de ``batch_detect_sentiment``."""
    return {
        "Index": index,
        "Sentiment": label,
        "SentimentScore": {
            "Positive": 0.7,
            "Negative": 0.1,
            "Neutral": 0.15,
            "Mixed": 0.05,
        },
    }


# ---------------------------------------------------------------------------
# detect_sentiments: asociación por Index (criterio 6.8)
# ---------------------------------------------------------------------------


def test_detect_sentiments_associates_by_index_when_api_reorders(
    comprehend_client: BaseClient,
) -> None:
    """La API devuelve los resultados reordenados; el adaptador los asocia por ``Index`` (6.8).

    ``ResultList`` llega en orden inverso al de ``TextList``. Si el adaptador
    asociara por posición, el texto 0 recibiría el sentimiento del texto 2 y
    viceversa; la aserción comprueba que cada ``Index`` conserva su etiqueta.
    """
    texts = ["me siento valorado", "clima neutro", "jornadas agotadoras"]
    response = {
        "ResultList": [
            _sentiment_result(2, "NEGATIVE"),
            _sentiment_result(0, "POSITIVE"),
            _sentiment_result(1, "NEUTRAL"),
        ],
        "ErrorList": [],
    }
    stubber = Stubber(comprehend_client)
    stubber.add_response(
        "batch_detect_sentiment",
        response,
        {"TextList": texts, "LanguageCode": "es"},
    )

    with stubber:
        successes, errors = ComprehendAdapter(
            client=comprehend_client
        ).detect_sentiments(texts, "es")

    assert errors == {}
    assert set(successes) == {0, 1, 2}
    assert successes[0].sentimiento_general is SentimientoGeneral.POSITIVO
    assert successes[1].sentimiento_general is SentimientoGeneral.NEUTRAL
    assert successes[2].sentimiento_general is SentimientoGeneral.NEGATIVO
    stubber.assert_no_pending_responses()


def test_detect_sentiments_reconciles_result_and_error_lists_by_index(
    comprehend_client: BaseClient,
) -> None:
    """Éxitos y errores se reconcilian por ``Index``; las claves son disjuntas (6.7, 6.8).

    De 4 textos, Comprehend devuelve éxito para los índices 3 y 1 (reordenados),
    un error permanente para el índice 0 (``COMPREHEND_ITEM_FAILED``) y **omite**
    el índice 2. El adaptador no infiere identidad por posición: 2 queda fuera de
    ambos mapas (el servicio lo clasificará como ``COMPREHEND_RESULT_MISSING``).
    """
    texts = ["uno", "dos", "tres", "cuatro"]
    response = {
        "ResultList": [
            _sentiment_result(3, "POSITIVE"),
            _sentiment_result(1, "NEGATIVE"),
        ],
        "ErrorList": [
            {"Index": 0, "ErrorCode": "TEXT_SIZE_LIMIT_EXCEEDED", "ErrorMessage": "x"}
        ],
    }
    stubber = Stubber(comprehend_client)
    stubber.add_response(
        "batch_detect_sentiment",
        response,
        {"TextList": texts, "LanguageCode": "es"},
    )

    with stubber:
        successes, errors = ComprehendAdapter(
            client=comprehend_client
        ).detect_sentiments(texts, "es")

    assert set(successes) == {1, 3}
    assert errors == {0: COMPREHEND_ITEM_FAILED}
    assert set(successes).isdisjoint(errors)
    # El índice 2, ausente de ambas listas, no se inventa por posición (6.13).
    assert 2 not in successes
    assert 2 not in errors
    stubber.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# detect_languages: asociación por Index y fallback (criterios 6.1, 6.14)
# ---------------------------------------------------------------------------


def test_detect_languages_associates_by_index_and_falls_back(
    comprehend_client: BaseClient,
) -> None:
    """``detect_languages`` ubica cada idioma por ``Index`` y aplica el fallback ``es`` (6.1, 6.14).

    La respuesta llega reordenada y omite el índice 1 (que cae en ``ErrorList``):
    la posición 1 recibe el fallback ``DEFAULT_LANGUAGE_CODE`` sin desplazar las
    demás, probando que la reconstrucción es por ``Index`` y conserva la longitud.
    """
    texts = ["buen trato", "???", "excelente ambiente"]
    response = {
        "ResultList": [
            {"Index": 2, "Languages": [{"LanguageCode": "es", "Score": 0.99}]},
            {"Index": 0, "Languages": [{"LanguageCode": "en", "Score": 0.80}]},
        ],
        "ErrorList": [
            {"Index": 1, "ErrorCode": "INTERNAL", "ErrorMessage": "x"}
        ],
    }
    stubber = Stubber(comprehend_client)
    stubber.add_response(
        "batch_detect_dominant_language", response, {"TextList": texts}
    )

    with stubber:
        languages = ComprehendAdapter(
            client=comprehend_client
        ).detect_languages(texts)

    assert languages == ["en", DEFAULT_LANGUAGE_CODE, "es"]
    assert len(languages) == len(texts)
    stubber.assert_no_pending_responses()
