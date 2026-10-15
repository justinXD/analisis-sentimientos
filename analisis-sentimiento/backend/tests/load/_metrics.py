"""Funciones puras para las pruebas de carga multi-tenant (tarea 15.4).

Este módulo aísla el cálculo determinista de las pruebas de carga del acceso a
la red: el **resumen de percentiles** de las latencias de ``api_submit`` y
``api_status`` (criterios 18.1/18.9), la **verificación de la conservación de
ítems** por Lote (criterios 5.8/18.3/18.5) y la **evaluación de los objetivos de
latencia** frente a sus umbrales iniciales.

Todas las funciones son puras (sin E/S, sin estado global, sin red) y por tanto
se prueban exhaustivamente sin un stack desplegado en
:mod:`tests.load.test_load_metrics`. La prueba de carga real
(:mod:`tests.load.test_load_multitenant`) consume estas funciones sobre las
latencias que mide contra la API desplegada.

Diseño del cálculo de percentiles
----------------------------------
Se usa el **método del rango más cercano** (*nearest-rank*) sobre la muestra
ordenada: para un percentil ``p`` y ``n`` observaciones, el rango es
``ceil(p/100 * n)`` (1-indexado), acotado a ``[1, n]``. Es el método que
describe el objetivo "percentil 95" de los criterios 18.1/18.9 de forma estable
para muestras pequeñas (las de una prueba de carga acotada), sin interpolar
valores que no se observaron. Devuelve siempre un valor realmente medido.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Percentiles                                                                  #
# --------------------------------------------------------------------------- #


def percentile(samples: Sequence[float], pct: float) -> float:
    """Calcula el percentil ``pct`` de ``samples`` por el método del rango más cercano.

    Args:
        samples: Observaciones de latencia (p. ej. segundos). No se asume ordenada.
        pct: Percentil deseado en el rango ``(0, 100]`` (p. ej. ``95`` para p95).

    Returns:
        El valor de la muestra en el rango más cercano a ``pct``. Siempre es uno de
        los valores observados (no interpola).

    Raises:
        ValueError: Si ``samples`` está vacía o ``pct`` no está en ``(0, 100]``.
    """
    if not samples:
        raise ValueError("percentile() requiere al menos una muestra")
    if not 0 < pct <= 100:
        raise ValueError(f"percentil fuera de rango (0, 100]: {pct!r}")
    ordered = sorted(samples)
    n = len(ordered)
    # Rango 1-indexado por nearest-rank, acotado a [1, n].
    rank = math.ceil((pct / 100.0) * n)
    index = min(max(rank, 1), n) - 1
    return ordered[index]


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """Resumen de percentiles de una serie de latencias (en segundos).

    Attributes:
        count: Número de observaciones.
        p50: Mediana (percentil 50).
        p95: Percentil 95, el objetivo de los criterios 18.1 y 18.9.
        p99: Percentil 99.
        maximum: Latencia máxima observada (relevante para el techo duro de 18.1).
    """

    count: int
    p50: float
    p95: float
    p99: float
    maximum: float


def summarize_latencies(samples: Sequence[float]) -> LatencySummary:
    """Resume una serie de latencias en p50/p95/p99 y el máximo.

    Args:
        samples: Latencias observadas en segundos.

    Returns:
        El :class:`LatencySummary` correspondiente.

    Raises:
        ValueError: Si ``samples`` está vacía.
    """
    if not samples:
        raise ValueError("summarize_latencies() requiere al menos una muestra")
    return LatencySummary(
        count=len(samples),
        p50=percentile(samples, 50),
        p95=percentile(samples, 95),
        p99=percentile(samples, 99),
        maximum=max(samples),
    )


# --------------------------------------------------------------------------- #
# Evaluación de objetivos de latencia                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LatencyObjective:
    """Objetivo de latencia inicial de una operación (criterios 18.1/18.9).

    Los valores son los **objetivos iniciales revisables** del diseño (sección
    22.1), no compromisos contractuales.

    Attributes:
        name: Nombre legible de la operación (p. ej. ``api_submit (<=100 items)``).
        p95_target_seconds: Umbral del percentil 95, en segundos.
        hard_ceiling_seconds: Techo duro que ninguna observación debe superar, o
            ``None`` si la operación no define un techo (p. ej. ``api_status``).
    """

    name: str
    p95_target_seconds: float
    hard_ceiling_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ObjectiveResult:
    """Resultado de evaluar un :class:`LatencySummary` contra un objetivo.

    Attributes:
        objective: El objetivo evaluado.
        summary: El resumen de latencias medido.
        p95_met: ``True`` si ``summary.p95 <= objective.p95_target_seconds``.
        ceiling_met: ``True`` si no hay techo o ninguna observación lo superó.
    """

    objective: LatencyObjective
    summary: LatencySummary
    p95_met: bool
    ceiling_met: bool

    @property
    def passed(self) -> bool:
        """``True`` solo si se cumplen tanto el objetivo de p95 como el techo duro."""
        return self.p95_met and self.ceiling_met

    def describe(self) -> str:
        """Devuelve una descripción legible para el mensaje de aserción/informe.

        No incluye PII ni valores sensibles: solo el nombre de la operación y
        cifras de latencia agregadas (steering de seguridad y observabilidad).
        """
        ceiling = (
            f", techo={self.objective.hard_ceiling_seconds:.3f}s"
            if self.objective.hard_ceiling_seconds is not None
            else ""
        )
        verdict = "OK" if self.passed else "FALLO"
        return (
            f"[{verdict}] {self.objective.name}: "
            f"n={self.summary.count} "
            f"p50={self.summary.p50:.3f}s "
            f"p95={self.summary.p95:.3f}s (objetivo<={self.objective.p95_target_seconds:.3f}s) "
            f"p99={self.summary.p99:.3f}s "
            f"max={self.summary.maximum:.3f}s{ceiling}"
        )


def evaluate_objective(
    summary: LatencySummary, objective: LatencyObjective
) -> ObjectiveResult:
    """Evalúa un resumen de latencias frente a un objetivo inicial.

    Args:
        summary: El resumen de percentiles medido.
        objective: El objetivo de latencia a comprobar.

    Returns:
        El :class:`ObjectiveResult` con el veredicto desglosado.
    """
    p95_met = summary.p95 <= objective.p95_target_seconds
    ceiling_met = (
        objective.hard_ceiling_seconds is None
        or summary.maximum <= objective.hard_ceiling_seconds
    )
    return ObjectiveResult(
        objective=objective,
        summary=summary,
        p95_met=p95_met,
        ceiling_met=ceiling_met,
    )


# --------------------------------------------------------------------------- #
# Conservación de ítems (criterios 5.8 / 18.3 / 18.5)                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Contadores terminales de un Lote tal como los reporta ``GET`` de estado.

    Attributes:
        analysis_id: Identificador del Lote.
        tenant_id: Tenant propietario del Lote (para verificar el aislamiento).
        item_count: Número de ítems con que se creó el Lote.
        done: Ítems en ``DONE``.
        failed: Ítems en ``FAILED``.
        skipped: Ítems en ``SKIPPED``.
    """

    analysis_id: str
    tenant_id: str
    item_count: int
    done: int
    failed: int
    skipped: int

    @property
    def terminal_total(self) -> int:
        """Suma de ítems en estado terminal: ``done + failed + skipped``."""
        return self.done + self.failed + self.skipped

    @property
    def is_conserved(self) -> bool:
        """``True`` si no se perdió ningún ítem: ``terminal_total == item_count``."""
        return self.terminal_total == self.item_count


def batch_outcome_from_status(
    analysis_id: str, tenant_id: str, status_body: Mapping[str, object]
) -> BatchOutcome:
    """Construye un :class:`BatchOutcome` desde el cuerpo de ``GET`` de estado.

    Args:
        analysis_id: Identificador del Lote.
        tenant_id: Tenant propietario (del token con que se consultó).
        status_body: Cuerpo del ``GET /v1/analyses/{analysis_id}`` ya terminal.

    Returns:
        El :class:`BatchOutcome` con los contadores extraídos.

    Raises:
        KeyError: Si falta algún contador obligatorio en ``status_body``.
    """
    return BatchOutcome(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        item_count=int(status_body["item_count"]),  # type: ignore[call-overload]
        done=int(status_body["done_count"]),  # type: ignore[call-overload]
        failed=int(status_body["failed_count"]),  # type: ignore[call-overload]
        skipped=int(status_body["skipped_count"]),  # type: ignore[call-overload]
    )


@dataclass(frozen=True, slots=True)
class ConservationReport:
    """Resultado de verificar la conservación de ítems de un conjunto de Lotes.

    Attributes:
        total_items_expected: Suma de ``item_count`` de todos los Lotes.
        total_items_terminal: Suma de ``terminal_total`` de todos los Lotes.
        lost_batches: Lotes cuya suma terminal no iguala su ``item_count``.
        duplicate_analysis_ids: ``analysis_id`` repetidos entre los Lotes (romperían
            el aislamiento: dos Tenants no deben compartir identificador de Lote).
    """

    total_items_expected: int
    total_items_terminal: int
    lost_batches: tuple[BatchOutcome, ...]
    duplicate_analysis_ids: tuple[str, ...]

    @property
    def all_conserved(self) -> bool:
        """``True`` si ningún Lote perdió ítems y no hay identificadores repetidos."""
        return (
            not self.lost_batches
            and not self.duplicate_analysis_ids
            and self.total_items_expected == self.total_items_terminal
        )

    def describe(self) -> str:
        """Mensaje legible del veredicto de conservación, sin PII."""
        lost = [
            f"{b.analysis_id} (esperado={b.item_count}, terminal={b.terminal_total})"
            for b in self.lost_batches
        ]
        parts = [
            f"esperados={self.total_items_expected}",
            f"terminales={self.total_items_terminal}",
        ]
        if lost:
            parts.append(f"lotes_con_perdida={lost}")
        if self.duplicate_analysis_ids:
            parts.append(f"analysis_ids_duplicados={list(self.duplicate_analysis_ids)}")
        return "conservacion de items: " + ", ".join(parts)


def verify_conservation(outcomes: Sequence[BatchOutcome]) -> ConservationReport:
    """Verifica que ningún Lote perdió ítems y que los Lotes están aislados.

    Materializa los criterios 5.8/18.3/18.5: con varios Lotes de Tenants distintos
    procesándose en paralelo, cada Lote debe conservar la totalidad de sus ítems
    (``done + failed + skipped == item_count``) y ningún Lote debe compartir
    ``analysis_id`` con otro (aislamiento entre Tenants).

    Args:
        outcomes: Los contadores terminales de cada Lote.

    Returns:
        El :class:`ConservationReport` agregado.
    """
    lost = tuple(o for o in outcomes if not o.is_conserved)
    seen: set[str] = set()
    duplicates: list[str] = []
    for outcome in outcomes:
        if outcome.analysis_id in seen and outcome.analysis_id not in duplicates:
            duplicates.append(outcome.analysis_id)
        seen.add(outcome.analysis_id)
    return ConservationReport(
        total_items_expected=sum(o.item_count for o in outcomes),
        total_items_terminal=sum(o.terminal_total for o in outcomes),
        lost_batches=lost,
        duplicate_analysis_ids=tuple(duplicates),
    )
