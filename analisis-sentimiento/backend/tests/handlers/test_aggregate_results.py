"""Pruebas de transporte del handler ``aggregate_results`` (tarea 9.2).

Verifican el adaptador thin :func:`sentiment.handlers.aggregate_results.handler`
(secciones 15.4 y 16.4 del diseño) sustituyendo el servicio del módulo por un
**doble en memoria** (fake). Se comprueba:

* la extracción de ``analysis_id``/``tenant_id`` del evento ``AggregateInput`` y
  la invocación del caso de uso con esos argumentos;
* la traducción del META terminal devuelto a la salida ``{analysis_id, status}``;
* la propagación **sin captura** de :class:`AggregationError`, para que la
  Maquina_De_Estados la reintente y, agotados los reintentos, cierre el META como
  ``FAILED`` vía su ``Catch`` → ``MarkFailed`` (criterios 9.11, 10.18, 9.18).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from sentiment.domain.errors import AggregationError
from sentiment.domain.models import AnalysisMeta, AnalysisStatus
from sentiment.handlers import aggregate_results

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_TENANT_ID = "acme-corp"


def _event() -> dict[str, Any]:
    """Construye un evento ``AggregateInput`` (sección 15.4)."""
    return {
        "analysis_id": _ANALYSIS_ID,
        "tenant_id": _TENANT_ID,
        "execution_arn": "arn:aws:states:us-east-1:111122223333:execution:sa:01ARZ",
        "map_result": {
            "ResultWriterDetails": {"Bucket": "bucket", "Key": "map-results/x.json"}
        },
    }


def _meta(status: AnalysisStatus) -> AnalysisMeta:
    """META terminal de ejemplo que devuelve el servicio."""
    return AnalysisMeta(
        analysis_id=_ANALYSIS_ID,
        tenant_id=_TENANT_ID,
        owner_sub="sub-1",
        status=status,
        item_count=2,
        done_count=1,
        skipped_count=1,
    )


class FakeService:
    """Doble del servicio de agregación: devuelve un META o lanza un error."""

    def __init__(
        self,
        meta: AnalysisMeta | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._meta = meta
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def execute(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        self.calls.append((analysis_id, tenant_id))
        if self._error is not None:
            raise self._error
        assert self._meta is not None
        return self._meta


@pytest.fixture
def install_service(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeService], FakeService]:
    """Sustituye el servicio del módulo por el fake proporcionado."""

    def _install(service: FakeService) -> FakeService:
        monkeypatch.setattr(aggregate_results, "_SERVICE", service)
        return service

    return _install


@pytest.mark.parametrize(
    "status",
    [
        AnalysisStatus.COMPLETED,
        AnalysisStatus.COMPLETED_WITH_ERRORS,
        AnalysisStatus.FAILED,
    ],
)
def test_handler_returns_terminal_status(
    install_service: Callable[[FakeService], FakeService], status: AnalysisStatus
) -> None:
    """El handler invoca el caso de uso y devuelve el Estado_Terminal del Lote."""
    service = install_service(FakeService(_meta(status)))

    result = aggregate_results.handler(_event())

    assert service.calls == [(_ANALYSIS_ID, _TENANT_ID)]
    assert result == {"analysis_id": _ANALYSIS_ID, "status": status.value}


def test_handler_propagates_aggregation_error(
    install_service: Callable[[FakeService], FakeService],
) -> None:
    """AggregationError sube sin captura para el Retry/Catch de la máquina (9.11, 9.18)."""
    install_service(FakeService(error=AggregationError("item_records_missing")))

    with pytest.raises(AggregationError):
        aggregate_results.handler(_event())
