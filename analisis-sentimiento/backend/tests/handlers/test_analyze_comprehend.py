"""Pruebas de transporte del handler ``analyze_comprehend`` (tarea 9.2).

Verifican el comportamiento observable del adaptador thin
:func:`sentiment.handlers.analyze_comprehend.handler`, descrito en las secciones
15.4 y 16.2 del diseño, sustituyendo el servicio del módulo por un **doble en
memoria** (fake, no mock) que registra la lista de Ítems que recibe. Se comprueba:

* la traducción ``ComprehendBatchInput`` → Ítems de dominio con ``item_id``
  determinista y ``status = PENDING``;
* la traducción del desenlace del servicio a ``ComprehendBatchOutput`` con solo
  punteros y etiquetas, **sin texto de ``respuesta``** (criterio 15.4);
* que las excepciones de procesamiento del servicio **suben sin captura**, para
  que la Maquina_De_Estados las clasifique por el nombre de la clase (criterios
  5.11, 6.11, 9.18).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from sentiment.domain.errors import TaskInputInvalidError, ThrottledError
from sentiment.domain.ids import make_item_id
from sentiment.domain.models import AnalysisItem
from sentiment.handlers import analyze_comprehend
from sentiment.services.analyze_batch_sentiment import BatchSentimentResult

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_TENANT_ID = "acme-corp"
_CORRELATION_ID = "7f3c1e2a-0000-4000-8000-000000000000"


def _event(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Construye un evento ``ComprehendBatchInput`` como el del ``ItemBatcher``."""
    return {
        "BatchInput": {
            "analysis_id": _ANALYSIS_ID,
            "tenant_id": _TENANT_ID,
            "correlation_id": _CORRELATION_ID,
        },
        "Items": items,
    }


class FakeService:
    """Doble del servicio de batch: devuelve un desenlace fijo o lanza un error.

    Registra los Ítems recibidos para aseverar sobre la traducción del evento.
    """

    def __init__(
        self,
        result: BatchSentimentResult | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.received: list[AnalysisItem] = []

    def analyze(self, items: list[AnalysisItem]) -> BatchSentimentResult:
        self.received = items
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


@pytest.fixture
def install_service(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeService], FakeService]:
    """Sustituye el servicio del módulo por el fake proporcionado."""

    def _install(service: FakeService) -> FakeService:
        # Pre-poblar la caché del módulo hace que ``_get_service`` devuelva el
        # fake sin construir los adaptadores reales de AWS.
        monkeypatch.setattr(analyze_comprehend, "_SERVICE", service)
        return service

    return _install


def test_handler_translates_items_to_domain(
    install_service: Callable[[FakeService], FakeService],
) -> None:
    """El handler traduce los Items del evento a Ítems de dominio PENDING (15.4)."""
    service = install_service(FakeService(BatchSentimentResult()))

    analyze_comprehend.handler(
        _event(
            [
                {"indificador": "Comunicación", "respuesta": "hola", "index": 0},
                {"indificador": "Reconocimiento", "respuesta": "", "index": 1},
            ]
        )
    )

    assert [item.item_id for item in service.received] == [
        make_item_id(_ANALYSIS_ID, 0),
        make_item_id(_ANALYSIS_ID, 1),
    ]
    assert [item.index for item in service.received] == [0, 1]
    assert service.received[0].indificador == "Comunicación"
    assert service.received[0].respuesta == "hola"


def test_handler_renders_output_pointers_only(
    install_service: Callable[[FakeService], FakeService],
) -> None:
    """La salida lleva item_id/index/etiquetas, nunca el texto de respuesta (15.4)."""
    to_analyze = make_item_id(_ANALYSIS_ID, 0)
    skipped = make_item_id(_ANALYSIS_ID, 1)
    failed = make_item_id(_ANALYSIS_ID, 2)
    install_service(
        FakeService(
            BatchSentimentResult(
                to_analyze=(to_analyze,),
                skipped=(skipped,),
                failed=(failed,),
            )
        )
    )

    output = analyze_comprehend.handler(
        _event(
            [
                {"indificador": "Comunicación", "respuesta": "hola", "index": 0},
                {"indificador": "Reconocimiento", "respuesta": "", "index": 1},
                {"indificador": "Liderazgo", "respuesta": "texto muy largo", "index": 2},
            ]
        )
    )

    assert output["analysis_id"] == _ANALYSIS_ID
    assert output["to_analyze"] == [
        {"item_id": to_analyze, "index": 0, "needs_llm": True}
    ]
    assert output["skipped"] == [
        {"item_id": skipped, "index": 1, "reason": "EMPTY_RESPONSE"}
    ]
    assert output["failed"] == [{"item_id": failed, "index": 2}]
    # Ninguna cadena de la salida contiene el texto de respuesta (PII).
    assert "texto muy largo" not in str(output)
    assert "hola" not in str(output)


@pytest.mark.parametrize(
    "error",
    [
        ThrottledError("comprehend throttled"),
        TaskInputInvalidError("batch too large", item_count=26),
    ],
)
def test_handler_propagates_processing_errors(
    install_service: Callable[[FakeService], FakeService], error: Exception
) -> None:
    """Las excepciones del servicio suben sin captura para ErrorEquals (5.11/6.11/9.18)."""
    install_service(FakeService(error=error))

    with pytest.raises(type(error)):
        analyze_comprehend.handler(
            _event([{"indificador": "Comunicación", "respuesta": "x", "index": 0}])
        )
