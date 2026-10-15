"""Pruebas unitarias de ``aggregate_analysis`` (tarea 7.7).

Ejercitan el caso de uso :class:`~sentiment.services.aggregate_analysis.AggregateAnalysisService`
con dobles en memoria que satisfacen los puertos del dominio, sin AWS. Cubren el
comportamiento observable que el plan de pruebas asigna a las pruebas unitarias
para los criterios 10.7-10.9 y 12.7-12.9, más la idempotencia (8.7), el recuento
reconciliado (8.11, 10.4), el cierre de huérfanos (10.2), el orden por ``index``
(10.6), el error ``AGGREGATION_FAILED`` (10.5) y el orden "cerrar META antes de
publicar el evento" (10.16). Las pruebas de propiedad P1/P4/P11 viven en la tarea
7.8; aquí sólo hay ejemplos y casos límite.

El Documento_Consolidado producido se valida contra el contrato real
``consolidated_output.schema.json`` mediante el validador del paquete, de modo
que la forma del documento no se verifica a mano sino contra su esquema.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentiment.domain.errors import ORPHANED_AFTER_MAP, AggregationError
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ClassicSentiment,
    EmotionAnalysis,
    ItemStatus,
    SentimentScores,
    SentimientoGeneral,
    SurveyAnswer,
)
from sentiment.schemas.loader import (
    CONSOLIDATED_OUTPUT_SCHEMA_FILE,
    get_validator,
)
from sentiment.services.aggregate_analysis import (
    AggregateAnalysisService,
    build_output_key,
    decide_final_status,
)

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_TENANT_ID = "acme-corp"
_OWNER_SUB = "sub-123"
_CREATED_AT = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
_COMPLETED_AT = datetime(2025, 6, 14, 10, 0, 24, tzinfo=UTC)


def _fixed_clock() -> datetime:
    """Reloj determinista: siempre devuelve el mismo instante de cierre."""
    return _COMPLETED_AT


class FakeRepository:
    """Doble en memoria de :class:`~sentiment.domain.ports.AnalysisRepository`.

    Reproduce las partes del contrato que ``aggregate_analysis`` consume:
    ``get_meta``, ``iter_items``, ``fail_item``, ``finalize_analysis`` y
    ``mark_event_published``. Mantiene el META y los Ítems en memoria y registra
    el orden de las llamadas con efectos para poder asertar la secuencia (criterio
    10.16: cerrar antes de publicar).
    """

    def __init__(self, meta: AnalysisMeta, items: list[AnalysisItem]) -> None:
        self._meta = meta
        self._items = {item.item_id: item for item in items}
        self.calls: list[str] = []
        self.event_published = False

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        assert analysis_id == self._meta.analysis_id
        assert tenant_id == self._meta.tenant_id
        return self._meta

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        assert analysis_id == self._meta.analysis_id
        # Se devuelven desordenados a propósito para probar el orden por index.
        yield from self._items.values()

    def fail_item(self, analysis_id: str, item_id: str, error_code: str) -> bool:
        item = self._items[item_id]
        if item.status != ItemStatus.PENDING:
            return False
        item.status = ItemStatus.FAILED
        item.error_code = error_code
        self.calls.append(f"fail_item:{item_id}")
        return True

    def finalize_analysis(
        self,
        analysis_id: str,
        status: AnalysisStatus,
        counts: tuple[int, int, int],
        output_s3_key: str | None,
    ) -> None:
        done, failed, skipped = counts
        self._meta.status = status
        self._meta.done_count = done
        self._meta.failed_count = failed
        self._meta.skipped_count = skipped
        self._meta.output_s3_key = output_s3_key
        self._meta.completed_at = _COMPLETED_AT
        self.calls.append("finalize_analysis")

    def mark_event_published(self, analysis_id: str) -> None:
        self.event_published = True
        self.calls.append("mark_event_published")

    # --- Resto del puerto AnalysisRepository: no lo consume este servicio. ---
    # Se implementan para satisfacer el protocolo completo bajo mypy --strict;
    # su invocación en estas pruebas sería un error de contrato, de ahí el raise.

    def create_analysis(
        self, meta: AnalysisMeta, answers: list[SurveyAnswer]
    ) -> None:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:  # pragma: no cover - no usado
        raise NotImplementedError

    def save_classic_results(
        self, results: list[ClassicSentiment]
    ) -> None:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def complete_item(
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def mark_analysis_failed(
        self, analysis_id: str, error_code: str
    ) -> None:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def list_sweep_candidates(
        self, older_than: datetime
    ) -> Iterator[AnalysisMeta]:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def get_item(
        self, analysis_id: str, item_id: str
    ) -> AnalysisItem:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def skip_item(
        self, analysis_id: str, item_id: str
    ) -> bool:  # pragma: no cover - no usado aquí
        raise NotImplementedError


class FakeStorage:
    """Doble en memoria de :class:`~sentiment.domain.ports.ObjectStorage`.

    Guarda el último ``put_json`` para inspeccionar la clave y el documento.
    """

    def __init__(self) -> None:
        self.puts: list[tuple[str, object]] = []

    def put_json(self, key: str, payload: object) -> None:
        self.puts.append((key, payload))

    def get_json(self, key: str) -> object:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def object_exists(self, key: str) -> bool:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def presign_get(
        self, key: str, expires_in: int
    ) -> str:  # pragma: no cover - no usado
        raise NotImplementedError


class FakeEvents:
    """Doble de :class:`~sentiment.domain.ports.EventPublisher`.

    Registra el META con el que se publicó el evento y colabora con el
    repositorio para dejar constancia del orden de las llamadas.
    """

    def __init__(self, repo: FakeRepository) -> None:
        self._repo = repo
        self.published: list[AnalysisMeta] = []

    def publish_analysis_completed(self, meta: AnalysisMeta) -> None:
        self.published.append(meta)
        self._repo.calls.append("publish")


def _meta(item_count: int, status: AnalysisStatus = AnalysisStatus.RUNNING) -> AnalysisMeta:
    """Construye un META mínimo en estado ``RUNNING`` (o el que se indique)."""
    return AnalysisMeta(
        analysis_id=_ANALYSIS_ID,
        tenant_id=_TENANT_ID,
        owner_sub=_OWNER_SUB,
        status=status,
        item_count=item_count,
        input_s3_key="inputs/acme-corp/2025/06/14/01ARZ3NDEKTSV4RRFFQ69G5FAV.json",
        created_at=_CREATED_AT,
    )


def _item_id(index: int) -> str:
    return f"{_ANALYSIS_ID}#{index:05d}"


def _done_item(index: int, indificador: str = "Comunicación") -> AnalysisItem:
    """Ítem ``DONE`` con resultado clásico y profundo completos."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_item_id(index),
        index=index,
        indificador=indificador,
        respuesta="texto de la respuesta",
        status=ItemStatus.DONE,
        classic=ClassicSentiment(
            item_id=_item_id(index),
            language_code="es",
            language_confidence=Decimal("0.99"),
            sentimiento_general=SentimientoGeneral.POSITIVO,
            scores=SentimentScores(
                positive=Decimal("0.8"),
                negative=Decimal("0.05"),
                neutral=Decimal("0.1"),
                mixed=Decimal("0.05"),
            ),
        ),
        emotions=EmotionAnalysis(
            item_id=_item_id(index),
            sentimientos=("satisfacción", "confianza"),
            explicacion="el texto expresa satisfacción",
            model_id="amazon.nova-lite-v1:0",
            input_tokens=10,
            output_tokens=20,
        ),
        processed_at=datetime(2025, 6, 14, 10, 0, 21, tzinfo=UTC),
    )


def _skipped_item(index: int, indificador: str = "Reconocimiento") -> AnalysisItem:
    """Ítem ``SKIPPED``: respuesta vacía o sin contenido analizable."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_item_id(index),
        index=index,
        indificador=indificador,
        respuesta="Sin comentarios",
        status=ItemStatus.SKIPPED,
    )


def _failed_item(index: int, indificador: str = "Comunicación") -> AnalysisItem:
    """Ítem ``FAILED`` con un ``error_code`` del catálogo."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_item_id(index),
        index=index,
        indificador=indificador,
        respuesta="texto que falló",
        status=ItemStatus.FAILED,
        error_code="LLM_OUTPUT_INVALID",
    )


def _pending_item(index: int, indificador: str = "Comunicación") -> AnalysisItem:
    """Ítem huérfano: quedó ``PENDING`` tras terminar el Map."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_item_id(index),
        index=index,
        indificador=indificador,
        respuesta="texto no procesado",
        status=ItemStatus.PENDING,
    )


def _build(items: list[AnalysisItem]) -> tuple[
    AggregateAnalysisService, FakeRepository, FakeStorage, FakeEvents
]:
    """Arma el servicio con dobles para una lista de Ítems dada."""
    meta = _meta(item_count=len(items))
    repo = FakeRepository(meta, items)
    storage = FakeStorage()
    events = FakeEvents(repo)
    service = AggregateAnalysisService(repo, storage, events, _fixed_clock)
    return service, repo, storage, events


def _validate_document(document: object) -> None:
    """Valida el documento contra el contrato ``consolidated_output.schema.json``."""
    get_validator(CONSOLIDATED_OUTPUT_SCHEMA_FILE).validate(document)


# ---------------------------------------------------------------------------
# decide_final_status (criterios 10.7, 10.8, 10.9) — función total.
# ---------------------------------------------------------------------------


def test_decide_status_completed_when_no_failures() -> None:
    counts: Counter[ItemStatus] = Counter({ItemStatus.DONE: 3, ItemStatus.SKIPPED: 1})
    assert decide_final_status(counts, item_count=4) == AnalysisStatus.COMPLETED


def test_decide_status_completed_with_errors_when_some_fail() -> None:
    counts: Counter[ItemStatus] = Counter({ItemStatus.DONE: 2, ItemStatus.FAILED: 1})
    assert (
        decide_final_status(counts, item_count=3)
        == AnalysisStatus.COMPLETED_WITH_ERRORS
    )


def test_decide_status_failed_when_all_fail() -> None:
    counts: Counter[ItemStatus] = Counter({ItemStatus.FAILED: 3})
    assert decide_final_status(counts, item_count=3) == AnalysisStatus.FAILED


def test_decide_status_empty_batch_is_completed() -> None:
    # item_count == 0: failed_count == 0 cae en COMPLETED, no en FAILED.
    assert decide_final_status(Counter(), item_count=0) == AnalysisStatus.COMPLETED


# ---------------------------------------------------------------------------
# build_output_key (criterio 10.3) — clave determinista con la fecha de creación.
# ---------------------------------------------------------------------------


def test_build_output_key_uses_created_at_date() -> None:
    key = build_output_key(_TENANT_ID, _CREATED_AT, _ANALYSIS_ID)
    assert key == f"outputs/{_TENANT_ID}/2025/06/14/{_ANALYSIS_ID}.json"


# ---------------------------------------------------------------------------
# Camino feliz: COMPLETED_WITH_ERRORS con DONE + SKIPPED + FAILED.
# ---------------------------------------------------------------------------


def test_execute_consolidates_and_closes_batch() -> None:
    items = [_done_item(0), _skipped_item(1), _failed_item(2)]
    service, repo, storage, events = _build(items)

    result = service.execute(_ANALYSIS_ID, _TENANT_ID)

    # Estado terminal unívoco (criterio 10.8).
    assert result.status == AnalysisStatus.COMPLETED_WITH_ERRORS
    # Contadores recontados desde los Ítems reales (criterios 8.11, 10.4).
    assert (result.done_count, result.failed_count, result.skipped_count) == (1, 1, 1)
    assert result.done_count + result.failed_count + result.skipped_count == 3
    # output_s3_key y completed_at registrados (criterios 10.9, 10.16).
    assert result.output_s3_key == build_output_key(
        _TENANT_ID, _CREATED_AT, _ANALYSIS_ID
    )
    assert result.completed_at == _COMPLETED_AT
    # Se publicó exactamente un evento.
    assert len(events.published) == 1


def test_execute_writes_valid_consolidated_document() -> None:
    items = [_done_item(0), _skipped_item(1), _failed_item(2)]
    service, _repo, storage, _events = _build(items)

    service.execute(_ANALYSIS_ID, _TENANT_ID)

    assert len(storage.puts) == 1
    key, document = storage.puts[0]
    assert key == build_output_key(_TENANT_ID, _CREATED_AT, _ANALYSIS_ID)
    # El documento valida contra el contrato real (criterios 12.7-12.9).
    _validate_document(document)


def test_items_are_ordered_by_index() -> None:
    # Entrada desordenada: 2, 0, 1. El documento debe salir 0, 1, 2.
    items = [_failed_item(2), _done_item(0), _skipped_item(1)]
    service, _repo, storage, _events = _build(items)

    service.execute(_ANALYSIS_ID, _TENANT_ID)

    _key, document = storage.puts[0]
    assert isinstance(document, dict)
    indexes = [entry["index"] for entry in document["items"]]
    assert indexes == [0, 1, 2]


# ---------------------------------------------------------------------------
# Resumen por indificador (criterio 12.8).
# ---------------------------------------------------------------------------


def test_summary_counts_sum_to_item_count() -> None:
    items = [
        _done_item(0, "Comunicación"),
        _done_item(1, "Comunicación"),
        _skipped_item(2, "Reconocimiento"),
        _failed_item(3, "Reconocimiento"),
    ]
    service, _repo, storage, _events = _build(items)

    service.execute(_ANALYSIS_ID, _TENANT_ID)

    _key, document = storage.puts[0]
    assert isinstance(document, dict)
    summary = document["resumen_por_indificador"]
    total = sum(entry["total"] for entry in summary.values())
    assert total == document["item_count"] == 4
    # Los dos DONE de "Comunicación" cuentan como Positivo; los otros no suman
    # a ninguna etiqueta de sentimiento_general (SKIPPED/FAILED sin clásico).
    assert summary["Comunicación"]["sentimiento_general"]["Positivo"] == 2
    assert summary["Reconocimiento"]["sentimiento_general"] == {
        "Positivo": 0,
        "Negativo": 0,
        "Neutral": 0,
        "Mixto": 0,
    }


# ---------------------------------------------------------------------------
# Cierre de huérfanos PENDING (criterio 10.2, Property P4).
# ---------------------------------------------------------------------------


def test_pending_items_are_closed_as_orphaned() -> None:
    items = [_done_item(0), _pending_item(1)]
    service, repo, storage, _events = _build(items)

    result = service.execute(_ANALYSIS_ID, _TENANT_ID)

    # El huérfano se cerró como FAILED/ORPHANED_AFTER_MAP vía fail_item.
    assert f"fail_item:{_item_id(1)}" in repo.calls
    assert (result.done_count, result.failed_count) == (1, 1)
    assert result.status == AnalysisStatus.COMPLETED_WITH_ERRORS
    # El documento refleja el estado final del huérfano.
    _key, document = storage.puts[0]
    assert isinstance(document, dict)
    orphan = next(e for e in document["items"] if e["index"] == 1)
    assert orphan["status"] == "FAILED"
    assert orphan["error_code"] == ORPHANED_AFTER_MAP
    _validate_document(document)


def test_all_orphans_closed_yields_failed_status() -> None:
    items = [_pending_item(0), _pending_item(1)]
    service, _repo, _storage, _events = _build(items)

    result = service.execute(_ANALYSIS_ID, _TENANT_ID)

    assert result.status == AnalysisStatus.FAILED
    assert result.failed_count == 2


# ---------------------------------------------------------------------------
# Idempotencia por estado terminal (criterio 8.7).
# ---------------------------------------------------------------------------


def test_execute_is_noop_when_meta_already_terminal() -> None:
    items = [_done_item(0)]
    meta = _meta(item_count=1, status=AnalysisStatus.COMPLETED)
    meta.done_count = 1
    meta.output_s3_key = "outputs/acme-corp/2025/06/14/01ARZ3NDEKTSV4RRFFQ69G5FAV.json"
    meta.completed_at = _COMPLETED_AT
    repo = FakeRepository(meta, items)
    storage = FakeStorage()
    events = FakeEvents(repo)
    service = AggregateAnalysisService(repo, storage, events, _fixed_clock)

    result = service.execute(_ANALYSIS_ID, _TENANT_ID)

    assert result is meta
    # Ni se reescribe el documento ni se republica el evento (criterio 8.7).
    assert storage.puts == []
    assert events.published == []
    assert repo.event_published is False
    assert "finalize_analysis" not in repo.calls


# ---------------------------------------------------------------------------
# Conservación de ítems: AGGREGATION_FAILED si faltan registros (criterios 10.5, 9.11).
# ---------------------------------------------------------------------------


def test_execute_raises_when_item_records_missing() -> None:
    # meta.item_count == 3, pero sólo se leen 2 Ítems reales.
    meta = _meta(item_count=3)
    repo = FakeRepository(meta, [_done_item(0), _done_item(1)])
    storage = FakeStorage()
    events = FakeEvents(repo)
    service = AggregateAnalysisService(repo, storage, events, _fixed_clock)

    with pytest.raises(AggregationError) as excinfo:
        service.execute(_ANALYSIS_ID, _TENANT_ID)

    assert excinfo.value.error_code == "AGGREGATION_FAILED"
    # No se fijó estado terminal ni se escribió nada (criterio 10.5).
    assert meta.status == AnalysisStatus.RUNNING
    assert storage.puts == []
    assert events.published == []


# ---------------------------------------------------------------------------
# Orden de escrituras: cerrar el META antes de publicar el evento (criterio 10.16).
# ---------------------------------------------------------------------------


def test_meta_is_finalized_before_event_is_published() -> None:
    items = [_done_item(0)]
    service, repo, _storage, _events = _build(items)

    service.execute(_ANALYSIS_ID, _TENANT_ID)

    # finalize_analysis ocurre antes de publish, y mark_event_published al final.
    assert repo.calls == ["finalize_analysis", "publish", "mark_event_published"]


# ---------------------------------------------------------------------------
# Determinismo del documento ante reconstrucción (criterio 8.8).
# ---------------------------------------------------------------------------


def test_document_is_deterministic_across_rebuilds() -> None:
    items_a = [_done_item(0), _failed_item(1), _skipped_item(2)]
    items_b = [_done_item(0), _failed_item(1), _skipped_item(2)]
    service_a, _ra, storage_a, _ea = _build(items_a)
    service_b, _rb, storage_b, _eb = _build(items_b)

    service_a.execute(_ANALYSIS_ID, _TENANT_ID)
    service_b.execute(_ANALYSIS_ID, _TENANT_ID)

    assert storage_a.puts[0] == storage_b.puts[0]
