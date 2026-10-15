"""Pruebas de transporte del handler ``analyze_llm`` (tarea 9.2).

Verifican el adaptador thin :func:`sentiment.handlers.analyze_llm.handler`
(secciones 15.4 y 16.3 del diseño) sustituyendo el servicio del módulo por un
**doble en memoria** (fake). Se comprueba:

* la extracción de ``analysis_id``/``item_id`` del evento ``LlmItemInput`` y la
  invocación del caso de uso con esos argumentos;
* la traducción del :class:`ItemOutcome` a la salida ``LlmItemOutput``, para
  ``DONE`` y ``ALREADY_TERMINAL`` (ambos son éxito, criterios 8.2, 8.5);
* la propagación **sin captura** de las excepciones del análisis (transitorias y
  permanentes) y de la configuración, para que ``ErrorEquals`` las clasifique por
  el nombre de la clase (criterios 7.1, 7.15, 9.18).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from sentiment.domain.errors import (
    LlmSchemaError,
    NotFoundError,
    ThrottledError,
    TransientDependencyError,
)
from sentiment.domain.ids import make_item_id
from sentiment.handlers import analyze_llm
from sentiment.observability.config import ConfigError
from sentiment.services.analyze_item_emotions import ItemOutcome

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_ITEM_ID = make_item_id(_ANALYSIS_ID, 7)
_CORRELATION_ID = "7f3c1e2a-0000-4000-8000-000000000000"


def _event() -> dict[str, Any]:
    """Construye un evento ``LlmItemInput`` (sección 15.4)."""
    return {
        "analysis_id": _ANALYSIS_ID,
        "item_id": _ITEM_ID,
        "correlation_id": _CORRELATION_ID,
    }


class FakeService:
    """Doble del servicio de ítem: devuelve un desenlace fijo o lanza un error."""

    def __init__(
        self,
        outcome: ItemOutcome | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._outcome = outcome
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def execute(self, analysis_id: str, item_id: str) -> ItemOutcome:
        self.calls.append((analysis_id, item_id))
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome


@pytest.fixture
def install_service(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeService], FakeService]:
    """Sustituye el servicio del módulo por el fake proporcionado."""

    def _install(service: FakeService) -> FakeService:
        monkeypatch.setattr(analyze_llm, "_SERVICE", service)
        return service

    return _install


@pytest.mark.parametrize(
    "outcome",
    [ItemOutcome.DONE, ItemOutcome.ALREADY_TERMINAL],
)
def test_handler_returns_outcome(
    install_service: Callable[[FakeService], FakeService], outcome: ItemOutcome
) -> None:
    """El handler invoca el caso de uso y devuelve el desenlace (DONE/ALREADY_TERMINAL)."""
    service = install_service(FakeService(outcome))

    result = analyze_llm.handler(_event())

    assert service.calls == [(_ANALYSIS_ID, _ITEM_ID)]
    assert result == {"item_id": _ITEM_ID, "outcome": outcome.value}


@pytest.mark.parametrize(
    "error",
    [
        ThrottledError("bedrock throttled"),
        TransientDependencyError("bedrock 5xx"),
        LlmSchemaError("output invalid"),
        NotFoundError(),
        ConfigError("Falta la configuración requerida 'MODEL_ID'."),
    ],
)
def test_handler_propagates_errors_uncaught(
    install_service: Callable[[FakeService], FakeService], error: Exception
) -> None:
    """Toda excepción del análisis/config sube sin captura (7.1, 7.15, 9.18)."""
    install_service(FakeService(error=error))

    with pytest.raises(type(error)):
        analyze_llm.handler(_event())
