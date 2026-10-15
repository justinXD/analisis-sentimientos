"""Pruebas unitarias del handler ``reconcile_stuck`` (tarea 9.3).

Cubren el adaptador *thin* del sweeper sobre su contrato de transporte, sin AWS
ni red: la construcción perezosa del servicio una vez por contenedor, la emisión
de la métrica ``StuckAnalyses`` incluso en 0 (criterio 10.15), el vuelco de
métricas en ``finally`` aun ante error, y la traducción del resumen de la pasada
a un ``dict`` de solo recuentos (sin PII).

La lógica de negocio del barrido vive en ``services/reconcile_stuck.py`` y se
prueba en ``tests/services/test_reconcile_stuck.py``; aquí solo se verifica el
adaptador.
"""

from __future__ import annotations

import pytest

from sentiment.handlers import reconcile_stuck as handler
from sentiment.handlers.reconcile_stuck import EmfStuckMetricEmitter
from sentiment.observability.metrics import MetricName, get_metrics
from sentiment.services.reconcile_stuck import ReconcileReport


class _FakeMetrics:
    """Doble mínimo del objeto Powertools ``Metrics`` para el emisor."""

    def __init__(self) -> None:
        self.added: list[tuple[str, float]] = []
        self.flushed = 0

    def add_metric(self, *, name: str, unit: object, value: float) -> None:
        self.added.append((name, value))

    def flush_metrics(self, raise_on_empty_metrics: bool = False) -> None:
        self.flushed += 1


class _FakeService:
    """Doble del ``ReconcileStuckService`` que devuelve un informe fijo."""

    def __init__(self, report: ReconcileReport) -> None:
        self._report = report
        self.calls = 0

    def reconcile(self) -> ReconcileReport:
        self.calls += 1
        return self._report


class _ExplodingService:
    """Doble que falla al reconciliar, para verificar el ``finally`` del vuelco."""

    def reconcile(self) -> ReconcileReport:
        raise RuntimeError("boom")


@pytest.fixture
def fake_metrics(monkeypatch: pytest.MonkeyPatch) -> _FakeMetrics:
    """Sustituye el objeto de métricas del módulo por un doble capturador."""
    metrics = _FakeMetrics()
    monkeypatch.setattr(handler, "_metrics", metrics)
    return metrics


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resetea el servicio memoizado entre casos (estado de contenedor)."""
    monkeypatch.setattr(handler, "_service", None)


# --------------------------------------------------------------------------- #
# EmfStuckMetricEmitter (criterio 10.15)                                      #
# --------------------------------------------------------------------------- #


def test_emitter_records_stuck_analyses_metric() -> None:
    metrics = _FakeMetrics()
    emitter = EmfStuckMetricEmitter(metrics)  # type: ignore[arg-type]

    emitter.emit_stuck_analyses(3)

    assert metrics.added == [(MetricName.STUCK_ANALYSES.value, 3.0)]


def test_emitter_records_zero_to_keep_series_continuous() -> None:
    metrics = _FakeMetrics()
    emitter = EmfStuckMetricEmitter(metrics)  # type: ignore[arg-type]

    emitter.emit_stuck_analyses(0)

    # Se registra incluso en 0 (criterio 10.15): la serie no debe tener huecos.
    assert metrics.added == [(MetricName.STUCK_ANALYSES.value, 0.0)]


def test_emitter_defaults_to_module_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics = _FakeMetrics()
    monkeypatch.setattr(handler, "_metrics", metrics)

    EmfStuckMetricEmitter().emit_stuck_analyses(1)

    assert metrics.added == [(MetricName.STUCK_ANALYSES.value, 1.0)]


# --------------------------------------------------------------------------- #
# lambda_handler: traducción y vuelco                                         #
# --------------------------------------------------------------------------- #


def test_handler_returns_counts_only(
    monkeypatch: pytest.MonkeyPatch, fake_metrics: _FakeMetrics
) -> None:
    report = ReconcileReport(
        candidates_examined=5, expired=2, events_republished=1, skipped=2
    )
    service = _FakeService(report)
    monkeypatch.setattr(handler, "_service", service)

    result = handler.lambda_handler({"detail-type": "Scheduled Event"}, None)

    assert result == {
        "candidates_examined": 5,
        "expired": 2,
        "events_republished": 1,
        "skipped": 2,
    }
    assert service.calls == 1
    # La métrica se vuelca una vez por invocación.
    assert fake_metrics.flushed == 1


def test_handler_flushes_metrics_even_on_error(
    monkeypatch: pytest.MonkeyPatch, fake_metrics: _FakeMetrics
) -> None:
    monkeypatch.setattr(handler, "_service", _ExplodingService())

    with pytest.raises(RuntimeError, match="boom"):
        handler.lambda_handler({}, None)

    # El vuelco ocurre en `finally`: un fallo a mitad de pasada no pierde métricas.
    assert fake_metrics.flushed == 1


def test_handler_builds_service_once_per_container(
    monkeypatch: pytest.MonkeyPatch, fake_metrics: _FakeMetrics
) -> None:
    report = ReconcileReport(
        candidates_examined=0, expired=0, events_republished=0, skipped=0
    )
    build_calls = 0

    def _fake_build() -> _FakeService:
        nonlocal build_calls
        build_calls += 1
        return _FakeService(report)

    monkeypatch.setattr(handler, "_build_service", _fake_build)

    handler.lambda_handler({}, None)
    handler.lambda_handler({}, None)

    # Construido una sola vez (memoizado), reutilizado en la segunda invocación.
    assert build_calls == 1


def test_module_metrics_is_a_powertools_metrics_object() -> None:
    # El objeto de métricas del módulo es el de Powertools; no se lee config al
    # construirlo, de modo que importar el handler es seguro (criterio 17.7).
    assert type(handler._metrics) is type(get_metrics())
