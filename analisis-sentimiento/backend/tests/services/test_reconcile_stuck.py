"""Pruebas unitarias de ``ReconcileStuckService`` (tarea 7.11).

Cubren el sweeper sobre los criterios 10.11, 10.12, 10.13, 10.14, 10.15 y 10.19,
con dobles en memoria de los puertos ``AnalysisRepository``, ``ExecutionInspector``
y ``EventPublisher`` más un emisor de métrica (sin ``boto3`` ni red):

* **Doble condición de abandono (criterios 10.11, 10.12, 10.13):** un Lote no
  terminal solo se expira si su ejecución no corre (no ``RUNNING``, o inexistente);
  si sigue ``RUNNING`` se deja intacto.
* **Cierre como EXPIRED (criterios 10.11, 10.14):** Ítems ``PENDING`` →
  ``FAILED``/``EXECUTION_ABANDONED``, META → ``EXPIRED``, evento publicado.
* **Terminal sin evento publicado (criterio 10.19):** se publica el evento sin
  modificar el estado.
* **Métrica siempre (criterio 10.15):** ``StuckAnalyses`` se emite incluso en 0.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from sentiment.domain.errors import EXECUTION_ABANDONED
from sentiment.domain.ids import make_item_id, new_analysis_id
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ItemStatus,
)
from sentiment.domain.ports import (
    AnalysisRepository,
    EventPublisher,
    ExecutionInspector,
)
from sentiment.services.reconcile_stuck import (
    STUCK_THRESHOLD_SECONDS,
    ReconcileStuckService,
)

TENANT = "acme-corp"
NOW = datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC)
#: Un instante suficientemente antiguo para superar el umbral (lo que el barrido
#: ya garantiza al acotar por ``older_than``).
STALE = NOW - timedelta(seconds=STUCK_THRESHOLD_SECONDS + 60)


class FakeRepository:
    """Doble en memoria del puerto ``AnalysisRepository`` para el sweeper."""

    def __init__(self) -> None:
        self._metas: dict[str, AnalysisMeta] = {}
        self._items: dict[str, list[AnalysisItem]] = {}
        self.candidates: list[AnalysisMeta] = []
        self.sweep_older_than: datetime | None = None
        self.finalize_calls: list[tuple[str, AnalysisStatus, tuple[int, int, int]]] = []
        self.failed_items: list[tuple[str, str, str]] = []
        self.event_marked: list[str] = []

    def add(self, meta: AnalysisMeta, items: list[AnalysisItem] | None = None) -> None:
        self._metas[meta.analysis_id] = meta
        self._items[meta.analysis_id] = items or []
        self.candidates.append(meta)

    def list_sweep_candidates(self, older_than: datetime) -> Iterator[AnalysisMeta]:
        self.sweep_older_than = older_than
        yield from self.candidates

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        return self._metas[analysis_id]

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        yield from self._items.get(analysis_id, [])

    def fail_item(self, analysis_id: str, item_id: str, error_code: str) -> bool:
        self.failed_items.append((analysis_id, item_id, error_code))
        for item in self._items.get(analysis_id, []):
            if item.item_id == item_id and item.status == ItemStatus.PENDING:
                item.status = ItemStatus.FAILED
                item.error_code = error_code
                return True
        return False

    def finalize_analysis(
        self,
        analysis_id: str,
        status: AnalysisStatus,
        counts: tuple[int, int, int],
        output_s3_key: str | None,
    ) -> None:
        self.finalize_calls.append((analysis_id, status, counts))
        meta = self._metas[analysis_id]
        meta.status = status
        meta.done_count, meta.failed_count, meta.skipped_count = counts
        meta.output_s3_key = output_s3_key

    def mark_event_published(self, analysis_id: str) -> None:
        self.event_marked.append(analysis_id)


class FakeInspector:
    """Doble del puerto ``ExecutionInspector``: estado por ``analysis_id``."""

    def __init__(self, statuses: dict[str, str | None]) -> None:
        self._statuses = statuses
        self.described: list[str] = []

    def describe_status(self, analysis_id: str) -> str | None:
        self.described.append(analysis_id)
        return self._statuses.get(analysis_id)


class FakeEvents:
    """Doble del puerto ``EventPublisher``: captura los Lotes publicados."""

    def __init__(self) -> None:
        self.published: list[tuple[str, AnalysisStatus]] = []

    def publish_analysis_completed(self, meta: AnalysisMeta) -> None:
        self.published.append((meta.analysis_id, meta.status))


class FakeMetrics:
    """Doble del emisor de la métrica ``StuckAnalyses``."""

    def __init__(self) -> None:
        self.emitted: list[int] = []

    def emit_stuck_analyses(self, count: int) -> None:
        self.emitted.append(count)


def _clock() -> datetime:
    return NOW


def _meta(
    *,
    analysis_id: str | None = None,
    status: AnalysisStatus = AnalysisStatus.RUNNING,
    output_s3_key: str | None = None,
) -> AnalysisMeta:
    return AnalysisMeta(
        analysis_id=analysis_id or new_analysis_id(),
        tenant_id=TENANT,
        owner_sub="user-sub-1",
        status=status,
        item_count=2,
        output_s3_key=output_s3_key,
        created_at=STALE,
        updated_at=STALE,
    )


def _item(analysis_id: str, index: int, status: ItemStatus) -> AnalysisItem:
    return AnalysisItem(
        analysis_id=analysis_id,
        item_id=make_item_id(analysis_id, index),
        index=index,
        indificador="Servicio",
        respuesta="texto",
        status=status,
    )


def _service(
    repo: FakeRepository,
    inspector: FakeInspector,
    events: FakeEvents,
    metrics: FakeMetrics,
) -> ReconcileStuckService:
    return ReconcileStuckService(
        cast(AnalysisRepository, repo),
        cast(ExecutionInspector, inspector),
        cast(EventPublisher, events),
        metrics,
        _clock,
    )


# --------------------------------------------------------------------------- #
# Barrido acotado por el umbral (criterio 10.10)                              #
# --------------------------------------------------------------------------- #


def test_sweep_queries_candidates_older_than_threshold() -> None:
    repo, inspector = FakeRepository(), FakeInspector({})
    events, metrics = FakeEvents(), FakeMetrics()
    service = _service(repo, inspector, events, metrics)

    service.reconcile()

    assert repo.sweep_older_than == NOW - timedelta(seconds=STUCK_THRESHOLD_SECONDS)


# --------------------------------------------------------------------------- #
# Métrica siempre, incluso en 0 (criterio 10.15)                              #
# --------------------------------------------------------------------------- #


def test_metric_emitted_even_when_zero() -> None:
    repo, inspector = FakeRepository(), FakeInspector({})
    events, metrics = FakeEvents(), FakeMetrics()
    service = _service(repo, inspector, events, metrics)

    report = service.reconcile()

    assert metrics.emitted == [0]
    assert report.expired == 0
    assert report.candidates_examined == 0


# --------------------------------------------------------------------------- #
# Doble condición de abandono (criterios 10.11, 10.12, 10.13)                 #
# --------------------------------------------------------------------------- #


def test_running_execution_is_left_intact() -> None:
    # Condición (a) la garantiza el barrido; (b) NO se cumple => no se toca.
    repo = FakeRepository()
    meta = _meta(status=AnalysisStatus.RUNNING)
    repo.add(meta, [_item(meta.analysis_id, 0, ItemStatus.PENDING)])
    inspector = FakeInspector({meta.analysis_id: "RUNNING"})
    events, metrics = FakeEvents(), FakeMetrics()
    service = _service(repo, inspector, events, metrics)

    report = service.reconcile()

    assert report.skipped == 1
    assert report.expired == 0
    assert repo.finalize_calls == []
    assert repo.failed_items == []
    assert events.published == []
    assert metrics.emitted == [0]


@pytest.mark.parametrize(
    "exec_status",
    ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED", None],
)
def test_abandoned_batch_is_expired(exec_status: str | None) -> None:
    repo = FakeRepository()
    meta = _meta(status=AnalysisStatus.RUNNING)
    items = [
        _item(meta.analysis_id, 0, ItemStatus.DONE),
        _item(meta.analysis_id, 1, ItemStatus.PENDING),
    ]
    repo.add(meta, items)
    inspector = FakeInspector({meta.analysis_id: exec_status})
    events, metrics = FakeEvents(), FakeMetrics()
    service = _service(repo, inspector, events, metrics)

    report = service.reconcile()

    assert report.expired == 1
    # Ítem PENDING -> FAILED/EXECUTION_ABANDONED (criterio 10.11).
    assert repo.failed_items == [
        (meta.analysis_id, make_item_id(meta.analysis_id, 1), EXECUTION_ABANDONED)
    ]
    # META -> EXPIRED con recuentos reconciliados (done=1, failed=1, skipped=0).
    assert repo.finalize_calls == [
        (meta.analysis_id, AnalysisStatus.EXPIRED, (1, 1, 0))
    ]
    # Evento publicado antes de terminar (criterio 10.14) y marcado.
    assert events.published == [(meta.analysis_id, AnalysisStatus.EXPIRED)]
    assert repo.event_marked == [meta.analysis_id]
    assert metrics.emitted == [1]


def test_pending_batch_without_execution_is_expired() -> None:
    # Lote PENDING cuyo mensaje nunca inició ejecución (criterio 10.12).
    repo = FakeRepository()
    meta = _meta(status=AnalysisStatus.PENDING)
    repo.add(meta, [_item(meta.analysis_id, 0, ItemStatus.PENDING)])
    inspector = FakeInspector({})  # ExecutionDoesNotExist -> None
    events, metrics = FakeEvents(), FakeMetrics()
    service = _service(repo, inspector, events, metrics)

    report = service.reconcile()

    assert report.expired == 1
    assert repo.finalize_calls[0][1] == AnalysisStatus.EXPIRED
    assert events.published == [(meta.analysis_id, AnalysisStatus.EXPIRED)]


# --------------------------------------------------------------------------- #
# Terminal sin evento publicado (criterio 10.19)                              #
# --------------------------------------------------------------------------- #


def test_terminal_without_event_only_republishes() -> None:
    repo = FakeRepository()
    meta = _meta(
        status=AnalysisStatus.COMPLETED,
        output_s3_key="outputs/acme/x.json",
    )
    meta.done_count = 2
    repo.add(meta, [])
    inspector = FakeInspector({})
    events, metrics = FakeEvents(), FakeMetrics()
    service = _service(repo, inspector, events, metrics)

    report = service.reconcile()

    assert report.events_republished == 1
    assert report.expired == 0
    # No se recalcula ni se cierra: status, contadores y output intactos.
    assert repo.finalize_calls == []
    assert repo.failed_items == []
    assert inspector.described == []  # no se consulta la ejecución de un terminal
    assert events.published == [(meta.analysis_id, AnalysisStatus.COMPLETED)]
    assert repo.event_marked == [meta.analysis_id]
    # No cuenta como atascado en la métrica (criterio 10.15 cuenta expirados).
    assert metrics.emitted == [0]


# --------------------------------------------------------------------------- #
# Pasada mixta: varios candidatos con desenlaces distintos                    #
# --------------------------------------------------------------------------- #


def test_mixed_pass_reports_each_outcome() -> None:
    repo = FakeRepository()
    abandoned = _meta(status=AnalysisStatus.RUNNING)
    running = _meta(status=AnalysisStatus.RUNNING)
    terminal = _meta(status=AnalysisStatus.FAILED, output_s3_key="outputs/f.json")
    repo.add(abandoned, [_item(abandoned.analysis_id, 0, ItemStatus.PENDING)])
    repo.add(running, [_item(running.analysis_id, 0, ItemStatus.PENDING)])
    repo.add(terminal, [])
    inspector = FakeInspector(
        {
            abandoned.analysis_id: None,
            running.analysis_id: "RUNNING",
            terminal.analysis_id: "FAILED",
        }
    )
    events, metrics = FakeEvents(), FakeMetrics()
    service = _service(repo, inspector, events, metrics)

    report = service.reconcile()

    assert report.candidates_examined == 3
    assert report.expired == 1
    assert report.events_republished == 1
    assert report.skipped == 1
    assert metrics.emitted == [1]
