"""Pruebas unitarias y de propiedad de las funciones puras de carga (tarea 15.4).

Estas pruebas NO tocan la red ni requieren stack: validan el cálculo de
percentiles, la evaluación de objetivos de latencia y la verificación de
conservación de ítems de :mod:`tests.load._metrics`. Se ejecutan siempre (no se
saltan), de modo que el núcleo de cálculo de las pruebas de carga queda cubierto
con independencia de que haya o no un entorno ``dev`` desplegado.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from load._metrics import (
    BatchOutcome,
    LatencyObjective,
    LatencySummary,
    batch_outcome_from_status,
    evaluate_objective,
    percentile,
    summarize_latencies,
    verify_conservation,
)

# --------------------------------------------------------------------------- #
# percentile                                                                   #
# --------------------------------------------------------------------------- #


def test_percentile_known_values() -> None:
    """El rango más cercano devuelve valores observados en una muestra conocida."""
    samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    assert percentile(samples, 50) == 50.0
    assert percentile(samples, 95) == 100.0
    assert percentile(samples, 99) == 100.0
    assert percentile(samples, 10) == 10.0


def test_percentile_is_order_independent() -> None:
    """El resultado no depende del orden de entrada (la función ordena)."""
    ascending = [1.0, 2.0, 3.0, 4.0]
    descending = list(reversed(ascending))
    assert percentile(ascending, 95) == percentile(descending, 95)


def test_percentile_single_sample() -> None:
    """Con una sola observación, cualquier percentil es ese valor."""
    assert percentile([7.5], 50) == 7.5
    assert percentile([7.5], 95) == 7.5
    assert percentile([7.5], 99) == 7.5


def test_percentile_rejects_empty() -> None:
    """Una muestra vacía es un error de uso, no un valor por defecto silencioso."""
    with pytest.raises(ValueError, match="al menos una muestra"):
        percentile([], 95)


@pytest.mark.parametrize("pct", [0, -1, 100.1, 150])
def test_percentile_rejects_out_of_range(pct: float) -> None:
    """El percentil debe estar en ``(0, 100]``."""
    with pytest.raises(ValueError, match="fuera de rango"):
        percentile([1.0, 2.0], pct)


@given(
    samples=st.lists(
        st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=200,
    ),
    pct=st.floats(min_value=0.001, max_value=100, allow_nan=False),
)
def test_percentile_returns_observed_value_and_is_bounded(
    samples: list[float], pct: float
) -> None:
    """Propiedad: el percentil es siempre un valor observado y está acotado.

    Verifica que el resultado pertenece a la muestra y queda entre el mínimo y el
    máximo observados (el método del rango más cercano no interpola).
    """
    result = percentile(samples, pct)
    assert result in samples
    assert min(samples) <= result <= max(samples)


@given(
    samples=st.lists(
        st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=200,
    )
)
def test_percentiles_are_monotonic(samples: list[float]) -> None:
    """Propiedad: p50 <= p95 <= p99 <= max para cualquier muestra no vacía."""
    summary = summarize_latencies(samples)
    assert summary.p50 <= summary.p95 <= summary.p99 <= summary.maximum


# --------------------------------------------------------------------------- #
# summarize_latencies / evaluate_objective                                     #
# --------------------------------------------------------------------------- #


def test_summarize_latencies_shape() -> None:
    """El resumen reporta el recuento y los percentiles esperados."""
    summary = summarize_latencies([0.1, 0.2, 0.3, 0.4, 0.5])
    assert summary.count == 5
    assert summary.p50 == 0.3
    assert summary.maximum == 0.5


def test_evaluate_objective_pass_under_target() -> None:
    """Un p95 por debajo del objetivo y bajo el techo duro cuenta como aprobado."""
    summary = LatencySummary(count=10, p50=0.2, p95=0.8, p99=0.95, maximum=0.95)
    objective = LatencyObjective("api_submit", p95_target_seconds=1.0, hard_ceiling_seconds=29.0)
    result = evaluate_objective(summary, objective)
    assert result.passed
    assert result.p95_met
    assert result.ceiling_met


def test_evaluate_objective_fails_over_p95() -> None:
    """Un p95 por encima del objetivo falla aunque respete el techo duro."""
    summary = LatencySummary(count=10, p50=0.5, p95=1.5, p99=2.0, maximum=2.0)
    objective = LatencyObjective("api_submit", p95_target_seconds=1.0, hard_ceiling_seconds=29.0)
    result = evaluate_objective(summary, objective)
    assert not result.passed
    assert not result.p95_met
    assert result.ceiling_met
    assert "FALLO" in result.describe()


def test_evaluate_objective_fails_over_hard_ceiling() -> None:
    """Una observación por encima del techo duro falla aunque el p95 cumpla."""
    summary = LatencySummary(count=10, p50=0.2, p95=0.9, p99=30.0, maximum=31.0)
    objective = LatencyObjective("api_submit", p95_target_seconds=1.0, hard_ceiling_seconds=29.0)
    result = evaluate_objective(summary, objective)
    assert not result.passed
    assert result.p95_met
    assert not result.ceiling_met


def test_evaluate_objective_without_ceiling() -> None:
    """``api_status`` no define techo duro: solo se evalúa el objetivo de p95."""
    summary = LatencySummary(count=10, p50=0.1, p95=0.25, p99=0.4, maximum=0.9)
    objective = LatencyObjective("api_status", p95_target_seconds=0.300)
    result = evaluate_objective(summary, objective)
    assert result.passed
    assert result.ceiling_met  # True por ausencia de techo


# --------------------------------------------------------------------------- #
# conservación de ítems                                                        #
# --------------------------------------------------------------------------- #


def _outcome(
    analysis_id: str,
    tenant: str,
    item_count: int,
    done: int,
    failed: int,
    skipped: int,
) -> BatchOutcome:
    return BatchOutcome(
        analysis_id=analysis_id,
        tenant_id=tenant,
        item_count=item_count,
        done=done,
        failed=failed,
        skipped=skipped,
    )


def test_conservation_all_batches_conserved() -> None:
    """5 Lotes de Tenants distintos sin pérdida: conservación total."""
    outcomes = [
        _outcome(f"A{i}", f"t{i}", 20, done=18, failed=1, skipped=1) for i in range(5)
    ]
    report = verify_conservation(outcomes)
    assert report.all_conserved
    assert report.total_items_expected == 100
    assert report.total_items_terminal == 100
    assert report.lost_batches == ()
    assert report.duplicate_analysis_ids == ()


def test_conservation_detects_lost_items() -> None:
    """Un Lote cuya suma terminal no iguala item_count se marca como pérdida."""
    outcomes = [
        _outcome("A0", "t0", 20, done=18, failed=1, skipped=1),
        _outcome("A1", "t1", 20, done=15, failed=0, skipped=0),  # pierde 5
    ]
    report = verify_conservation(outcomes)
    assert not report.all_conserved
    assert len(report.lost_batches) == 1
    assert report.lost_batches[0].analysis_id == "A1"
    assert "A1" in report.describe()


def test_conservation_detects_duplicate_analysis_ids() -> None:
    """Dos Lotes con el mismo analysis_id rompen el aislamiento entre Tenants."""
    outcomes = [
        _outcome("DUP", "t0", 10, done=10, failed=0, skipped=0),
        _outcome("DUP", "t1", 10, done=10, failed=0, skipped=0),
    ]
    report = verify_conservation(outcomes)
    assert not report.all_conserved
    assert report.duplicate_analysis_ids == ("DUP",)


def test_batch_outcome_from_status_extracts_counters() -> None:
    """El desenlace se construye desde el cuerpo de GET de estado."""
    body = {
        "status": "COMPLETED",
        "item_count": 20,
        "done_count": 19,
        "failed_count": 0,
        "skipped_count": 1,
    }
    outcome = batch_outcome_from_status("A0", "t0", body)
    assert outcome.item_count == 20
    assert outcome.terminal_total == 20
    assert outcome.is_conserved


@given(
    item_count=st.integers(min_value=1, max_value=1000),
    done=st.integers(min_value=0, max_value=1000),
    failed=st.integers(min_value=0, max_value=1000),
    skipped=st.integers(min_value=0, max_value=1000),
)
def test_is_conserved_matches_sum(
    item_count: int, done: int, failed: int, skipped: int
) -> None:
    """Propiedad: ``is_conserved`` equivale a ``done+failed+skipped == item_count``."""
    outcome = _outcome("A", "t", item_count, done, failed, skipped)
    assert outcome.is_conserved == (done + failed + skipped == item_count)
