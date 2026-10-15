"""Propiedades P1, P4 y P11 de ``aggregate_analysis`` (tarea 7.8).

Librería: **Hypothesis** (sección 19 del diseño). Estas tres propiedades
describen el comportamiento invariante del cierre de un Lote tal como lo ejecuta
:class:`~sentiment.services.aggregate_analysis.AggregateAnalysisService`, el
código producido en la tarea 7.7. Se verifican con **dobles en memoria** (fakes,
no mocks) que satisfacen los puertos del dominio
(:class:`~sentiment.domain.ports.AnalysisRepository`,
:class:`~sentiment.domain.ports.ObjectStorage`,
:class:`~sentiment.domain.ports.EventPublisher`), siguiendo la sección 21.2 del
diseño: un fake permite verificar **estado** final, que es justo lo que estas
propiedades afirman, mientras que un mock solo verificaría interacciones.

Enunciados (design.md, sección 19):

* **Property 1 — Conservación de ítems (P1).** *Validates: Requirements 4.3,
  4.4, 10.2, 10.3, 10.4.* Para todo Lote, al finalizar la agregación
  ``done + failed + skipped == item_count``, ningún Ítem se pierde y todo Ítem
  queda en un Estado_Terminal.

* **Property 4 — Terminación garantizada (P4).** *Validates: Requirements 9.5,
  10.1, 10.2, 10.11, 10.12.* Ningún Ítem permanece en estado no terminal: todo
  Ítem que siga en ``PENDING`` al cerrar el Map se cierra como ``FAILED`` con
  ``error_code == ORPHANED_AFTER_MAP``, y el Lote siempre alcanza un
  Estado_Terminal.

* **Property 11 — Reconciliación de contadores (P11).** *Validates:
  Requirements 8.3, 8.11, 10.4.* Los contadores finales del META son iguales al
  conteo real de Ítems por estado, **aunque algún incremento atómico se haya
  perdido**. Para refutar esta propiedad se siembra el META con contadores
  acumulados arbitrariamente corruptos (``done_count``/``failed_count``/
  ``skipped_count`` sin relación con la verdad): el cierre debe recontar desde
  los registros ITEM reales e ignorar por completo esos acumulados.

Diseño de las estrategias
-------------------------
``item_batches`` genera lotes de Ítems con mezclas arbitrarias de los cuatro
estados (``DONE``/``FAILED``/``SKIPPED``/``PENDING``), con ``index`` consecutivo
sin huecos (como los produce la siembra, criterio 4.5) e identificadores
deterministas ``{analysis_id}#{index:05d}``. Un Ítem ``DONE`` lleva su resultado
clásico y profundo completos; un ``FAILED`` lleva un ``error_code`` del catálogo;
un ``SKIPPED`` va sin análisis; un ``PENDING`` es un huérfano del Map. La mezcla
cubre el espacio relevante: lotes todos-``DONE``, todos-``PENDING``,
todos-``FAILED`` y cualquier combinación intermedia.

El reloj se fija (``_FIXED_CLOCK``) para que ``completed_at`` sea determinista y
no introduzca ``flakiness`` en las propiedades.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sentiment.domain.errors import ORPHANED_AFTER_MAP
from sentiment.domain.models import (
    TERMINAL_ANALYSIS_STATUSES,
    TERMINAL_ITEM_STATUSES,
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
from sentiment.services.aggregate_analysis import AggregateAnalysisService

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_TENANT_ID = "acme-corp"
_OWNER_SUB = "sub-123"
_CREATED_AT = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
_COMPLETED_AT = datetime(2025, 6, 14, 10, 0, 24, tzinfo=UTC)

#: Los cuatro estados de Ítem que el generador puede asignar, no terminal
#: incluido para ejercitar el cierre de huérfanos (P4).
_ITEM_STATUSES: tuple[ItemStatus, ...] = (
    ItemStatus.DONE,
    ItemStatus.FAILED,
    ItemStatus.SKIPPED,
    ItemStatus.PENDING,
)


def _fixed_clock() -> datetime:
    """Reloj determinista: devuelve siempre el mismo instante de cierre."""
    return _COMPLETED_AT


def _item_id(index: int) -> str:
    """Deriva el ``item_id`` canónico ``{analysis_id}#{index:05d}``."""
    return f"{_ANALYSIS_ID}#{index:05d}"


# ---------------------------------------------------------------------------
# Dobles en memoria de los puertos (fakes, no mocks). Verifican estado final.
# ---------------------------------------------------------------------------


class FakeRepository:
    """Doble en memoria de :class:`~sentiment.domain.ports.AnalysisRepository`.

    Reproduce la semántica que ``aggregate_analysis`` necesita y, en particular,
    la de :meth:`fail_item` y :meth:`finalize_analysis`, que son las piezas sobre
    las que descansan P4 y P11:

    * :meth:`fail_item` es **atómica e idempotente**: solo transiciona desde
      ``PENDING`` (devuelve ``True``) y es ``no-op`` sobre un Ítem ya terminal
      (devuelve ``False``), igual que la implementación transaccional real.
    * :meth:`finalize_analysis` fija los contadores **a partir de la tripleta
      recontada** que recibe, sobrescribiendo los acumulados previos. Esto es lo
      que permite que la reconciliación de P11 sea observable: por corruptos que
      estuvieran ``done_count``/``failed_count``/``skipped_count`` antes del
      cierre, acaban igualando el conteo real.
    """

    def __init__(self, meta: AnalysisMeta, items: list[AnalysisItem]) -> None:
        self._meta = meta
        self._items = {item.item_id: item for item in items}

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        return self._meta

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        # Orden de almacenamiento (de inserción), no por index: el servicio es el
        # responsable de ordenar por index, de modo que el fake no se lo regala.
        yield from self._items.values()

    def fail_item(self, analysis_id: str, item_id: str, error_code: str) -> bool:
        item = self._items[item_id]
        if item.status != ItemStatus.PENDING:
            return False
        item.status = ItemStatus.FAILED
        item.error_code = error_code
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

    def mark_event_published(self, analysis_id: str) -> None:
        pass

    # --- Resto del puerto: no lo consume aggregate_analysis. -----------------
    # Se implementan para satisfacer el protocolo completo; su invocación aquí
    # sería un error de contrato, de ahí el raise.

    def create_analysis(
        self, meta: AnalysisMeta, answers: list[SurveyAnswer]
    ) -> None:  # pragma: no cover - no usado
        raise NotImplementedError

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:  # pragma: no cover - no usado
        raise NotImplementedError

    def save_classic_results(
        self, results: list[ClassicSentiment]
    ) -> None:  # pragma: no cover - no usado
        raise NotImplementedError

    def complete_item(
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:  # pragma: no cover - no usado
        raise NotImplementedError

    def mark_analysis_failed(
        self, analysis_id: str, error_code: str
    ) -> None:  # pragma: no cover - no usado
        raise NotImplementedError

    def list_sweep_candidates(
        self, older_than: datetime
    ) -> Iterator[AnalysisMeta]:  # pragma: no cover - no usado
        raise NotImplementedError

    def get_item(
        self, analysis_id: str, item_id: str
    ) -> AnalysisItem:  # pragma: no cover - no usado
        raise NotImplementedError

    def skip_item(
        self, analysis_id: str, item_id: str
    ) -> bool:  # pragma: no cover - no usado
        raise NotImplementedError


class FakeStorage:
    """Doble en memoria de :class:`~sentiment.domain.ports.ObjectStorage`.

    Conserva el último documento escrito para poder inspeccionar la invariante de
    conservación directamente sobre el Documento_Consolidado (P1).
    """

    def __init__(self) -> None:
        self.puts: list[tuple[str, object]] = []

    def put_json(self, key: str, payload: object) -> None:
        self.puts.append((key, payload))

    def get_json(self, key: str) -> object:  # pragma: no cover - no usado
        raise NotImplementedError

    def object_exists(self, key: str) -> bool:  # pragma: no cover - no usado
        raise NotImplementedError

    def presign_get(
        self, key: str, expires_in: int
    ) -> str:  # pragma: no cover - no usado
        raise NotImplementedError


class FakeEvents:
    """Doble de :class:`~sentiment.domain.ports.EventPublisher`.

    Registra el META con el que se publicó el Evento_Finalizacion, para asertar
    que el cierre llega a publicar exactamente una vez.
    """

    def __init__(self) -> None:
        self.published: list[AnalysisMeta] = []

    def publish_analysis_completed(self, meta: AnalysisMeta) -> None:
        self.published.append(meta)


# ---------------------------------------------------------------------------
# Estrategias de Hypothesis.
# ---------------------------------------------------------------------------


def _done_item(index: int, indificador: str) -> AnalysisItem:
    """Ítem ``DONE`` con resultado clásico y profundo completos."""
    item_id = _item_id(index)
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=item_id,
        index=index,
        indificador=indificador,
        respuesta="respuesta analizada",
        status=ItemStatus.DONE,
        classic=ClassicSentiment(
            item_id=item_id,
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
            item_id=item_id,
            sentimientos=("satisfacción",),
            explicacion="el texto expresa satisfacción",
            model_id="amazon.nova-lite-v1:0",
            input_tokens=10,
            output_tokens=20,
        ),
        processed_at=_COMPLETED_AT,
    )


def _failed_item(index: int, indificador: str) -> AnalysisItem:
    """Ítem ``FAILED`` con un ``error_code`` del catálogo."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_item_id(index),
        index=index,
        indificador=indificador,
        respuesta="respuesta que falló",
        status=ItemStatus.FAILED,
        error_code="LLM_OUTPUT_INVALID",
    )


def _skipped_item(index: int, indificador: str) -> AnalysisItem:
    """Ítem ``SKIPPED``: respuesta sin contenido analizable."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_item_id(index),
        index=index,
        indificador=indificador,
        respuesta="Sin comentarios",
        status=ItemStatus.SKIPPED,
    )


def _pending_item(index: int, indificador: str) -> AnalysisItem:
    """Ítem huérfano: quedó ``PENDING`` tras cerrarse el Map."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_item_id(index),
        index=index,
        indificador=indificador,
        respuesta="respuesta no procesada",
        status=ItemStatus.PENDING,
    )


_ITEM_BUILDERS = {
    ItemStatus.DONE: _done_item,
    ItemStatus.FAILED: _failed_item,
    ItemStatus.SKIPPED: _skipped_item,
    ItemStatus.PENDING: _pending_item,
}

#: Indificadores sintéticos; su valor concreto no influye en las propiedades,
#: solo reparte los Ítems entre entradas del resumen por indificador.
_indificadores = st.sampled_from(("Comunicación", "Reconocimiento", "Liderazgo"))

#: Estados de Ítem con sesgo hacia ``PENDING`` para que los lotes con huérfanos
#: (el caso que ejercita P4) aparezcan con frecuencia suficiente.
_item_status = st.sampled_from(_ITEM_STATUSES)


@st.composite
def item_batches(
    draw: st.DrawFn, *, min_size: int = 1, max_size: int = 60
) -> list[AnalysisItem]:
    """Genera un Lote de Ítems con una mezcla arbitraria de estados.

    Los ``index`` son ``0..n-1`` sin huecos (como la siembra real), y a cada
    posición se le asigna uno de los cuatro :class:`ItemStatus` de forma
    independiente. Así el espacio generado incluye lotes homogéneos
    (todos-``DONE``, todos-``PENDING``, todos-``FAILED``) y cualquier mezcla
    intermedia, que es lo que las tres propiedades necesitan ejercitar.

    :return: lista de :class:`AnalysisItem` de tamaño ``[min_size, max_size]``.
    """
    size = draw(st.integers(min_value=min_size, max_value=max_size))
    items: list[AnalysisItem] = []
    for index in range(size):
        status = draw(_item_status)
        indificador = draw(_indificadores)
        items.append(_ITEM_BUILDERS[status](index, indificador))
    return items


def _meta(
    item_count: int,
    *,
    done_count: int = 0,
    failed_count: int = 0,
    skipped_count: int = 0,
) -> AnalysisMeta:
    """Construye un META ``RUNNING`` con los contadores acumulados indicados.

    Los contadores por defecto son ``0``; las pruebas de P11 los siembran con
    valores arbitrarios (y en general incorrectos) para comprobar que el cierre
    los ignora y recuenta desde los Ítems reales.
    """
    return AnalysisMeta(
        analysis_id=_ANALYSIS_ID,
        tenant_id=_TENANT_ID,
        owner_sub=_OWNER_SUB,
        status=AnalysisStatus.RUNNING,
        item_count=item_count,
        done_count=done_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        input_s3_key="inputs/acme-corp/2025/06/14/01ARZ3NDEKTSV4RRFFQ69G5FAV.json",
        created_at=_CREATED_AT,
    )


def _build(
    meta: AnalysisMeta, items: list[AnalysisItem]
) -> tuple[AggregateAnalysisService, FakeRepository, FakeStorage, FakeEvents]:
    """Arma el servicio bajo prueba con sus tres fakes y el reloj fijo."""
    repo = FakeRepository(meta, items)
    storage = FakeStorage()
    events = FakeEvents()
    service = AggregateAnalysisService(repo, storage, events, _fixed_clock)
    return service, repo, storage, events


# ---------------------------------------------------------------------------
# Property 1: Conservación de ítems.
# Validates: Requirements 4.3, 4.4, 10.2, 10.3, 10.4.
# ---------------------------------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(items=item_batches())
def test_item_count_is_conserved(items: list[AnalysisItem]) -> None:
    """``done + failed + skipped == item_count`` y ningún Ítem se pierde.

    Tras la agregación de cualquier Lote: (a) el META queda con
    ``done_count + failed_count + skipped_count == item_count``; (b) el conteo
    real de Ítems iguala ``item_count`` (ninguno se pierde ni se duplica); y
    (c) la suma de los tres contadores del Documento_Consolidado también cuadra
    con ``item_count``. Es la invariante central de P1 (criterios 10.3, 10.4).
    """
    meta = _meta(item_count=len(items))
    service, repo, storage, _events = _build(meta, items)

    final = service.execute(_ANALYSIS_ID, _TENANT_ID)

    assert final.item_count == len(items)
    assert (
        final.done_count + final.failed_count + final.skipped_count
        == final.item_count
    )

    real_items = list(repo.iter_items(_ANALYSIS_ID))
    assert len(real_items) == len(items)

    _key, document = storage.puts[0]
    assert isinstance(document, dict)
    assert (
        document["done_count"] + document["failed_count"] + document["skipped_count"]
        == document["item_count"]
        == len(items)
    )


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(items=item_batches())
def test_every_item_ends_terminal(items: list[AnalysisItem]) -> None:
    """Al cerrar el Lote, todo Ítem persistido queda en un Estado_Terminal.

    Complementa la conservación numérica: no basta con que los contadores sumen,
    cada registro ITEM debe haber salido de ``PENDING`` (criterio 10.2). Esta es
    la cara "no se pierde ni se queda a medias ningún Ítem" de P1, y a la vez el
    puente con P4.
    """
    meta = _meta(item_count=len(items))
    service, repo, _storage, _events = _build(meta, items)

    service.execute(_ANALYSIS_ID, _TENANT_ID)

    for item in repo.iter_items(_ANALYSIS_ID):
        assert item.status in TERMINAL_ITEM_STATUSES


# ---------------------------------------------------------------------------
# Property 4: Terminación garantizada.
# Validates: Requirements 9.5, 10.1, 10.2, 10.11, 10.12.
# ---------------------------------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(items=item_batches())
def test_no_item_stays_pending(items: list[AnalysisItem]) -> None:
    """Ningún Ítem permanece en ``PENDING``; los huérfanos cierran como ORPHANED.

    Con mezclas arbitrarias de estados (incluidos lotes enteros de huérfanos),
    tras la agregación: (a) ningún Ítem queda en ``PENDING``; (b) cada Ítem que
    estaba en ``PENDING`` acaba en ``FAILED`` con ``error_code ==
    ORPHANED_AFTER_MAP``; y (c) el Lote alcanza un Estado_Terminal. Es el
    enunciado directo de P4 (criterios 10.1, 10.2).
    """
    stranded_ids = {
        item.item_id for item in items if item.status == ItemStatus.PENDING
    }
    meta = _meta(item_count=len(items))
    service, repo, _storage, _events = _build(meta, items)

    final = service.execute(_ANALYSIS_ID, _TENANT_ID)

    for item in repo.iter_items(_ANALYSIS_ID):
        assert item.status != ItemStatus.PENDING
        if item.item_id in stranded_ids:
            assert item.status == ItemStatus.FAILED
            assert item.error_code == ORPHANED_AFTER_MAP

    assert final.status in TERMINAL_ANALYSIS_STATUSES


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(size=st.integers(min_value=1, max_value=60))
def test_all_pending_batch_terminates_as_failed(size: int) -> None:
    """Un Lote enteramente huérfano cierra en ``FAILED`` con todo Ítem terminal.

    Caso límite de P4: si el Map muere antes de procesar nada, todos los Ítems
    quedan ``PENDING``. La agregación debe cerrarlos como ``FAILED`` y decidir el
    Estado_Terminal ``FAILED`` (``failed_count == item_count``), sin dejar nada
    pendiente (criterios 9.5, 10.1, 10.2).
    """
    items = [_pending_item(index, "Comunicación") for index in range(size)]
    meta = _meta(item_count=size)
    service, repo, _storage, _events = _build(meta, items)

    final = service.execute(_ANALYSIS_ID, _TENANT_ID)

    assert final.status == AnalysisStatus.FAILED
    assert final.failed_count == size
    for item in repo.iter_items(_ANALYSIS_ID):
        assert item.status == ItemStatus.FAILED
        assert item.error_code == ORPHANED_AFTER_MAP


# ---------------------------------------------------------------------------
# Property 11: Reconciliación de contadores.
# Validates: Requirements 8.3, 8.11, 10.4.
# ---------------------------------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(items=item_batches(), data=st.data())
def test_aggregation_reconciles_counters(
    items: list[AnalysisItem], data: st.DataObject
) -> None:
    """Los contadores finales igualan el conteo real, pese a acumulados corruptos.

    Simula los incrementos atómicos perdidos (o duplicados) del enunciado de P11
    sembrando el META con ``done_count``/``failed_count``/``skipped_count``
    arbitrarios y en general incorrectos. Tras la agregación, los tres contadores
    del META deben igualar el conteo real de Ítems por estado —contando los
    huérfanos ya cerrados como ``FAILED``—, lo que demuestra que el cierre
    recuenta desde los registros ITEM y descarta el acumulado (criterios 8.11,
    10.4).
    """
    size = len(items)
    bogus = st.integers(min_value=0, max_value=max(size, 1) * 3)
    meta = _meta(
        item_count=size,
        done_count=data.draw(bogus),
        failed_count=data.draw(bogus),
        skipped_count=data.draw(bogus),
    )
    service, repo, _storage, _events = _build(meta, items)

    final = service.execute(_ANALYSIS_ID, _TENANT_ID)

    # Conteo real tras el cierre: los huérfanos PENDING ya son FAILED.
    real: Counter[ItemStatus] = Counter(
        item.status for item in repo.iter_items(_ANALYSIS_ID)
    )
    assert final.done_count == real[ItemStatus.DONE]
    assert final.failed_count == real[ItemStatus.FAILED]
    assert final.skipped_count == real[ItemStatus.SKIPPED]
    # Y la reconciliación respeta la conservación (puente con P1).
    assert (
        final.done_count + final.failed_count + final.skipped_count == size
    )


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(items=item_batches())
def test_consolidated_counters_match_real_counts(
    items: list[AnalysisItem],
) -> None:
    """Los contadores del Documento_Consolidado igualan el conteo real de Ítems.

    La reconciliación de P11 no solo cierra el META: el Documento_Consolidado
    escrito en ``outputs/`` debe exponer los mismos recuentos recontados, de modo
    que la verdad que ve el cliente (criterio 10.4) coincida con la de la tabla.
    """
    meta = _meta(item_count=len(items))
    service, repo, storage, _events = _build(meta, items)

    service.execute(_ANALYSIS_ID, _TENANT_ID)

    real: Counter[ItemStatus] = Counter(
        item.status for item in repo.iter_items(_ANALYSIS_ID)
    )
    _key, document = storage.puts[0]
    assert isinstance(document, dict)
    assert document["done_count"] == real[ItemStatus.DONE]
    assert document["failed_count"] == real[ItemStatus.FAILED]
    assert document["skipped_count"] == real[ItemStatus.SKIPPED]
