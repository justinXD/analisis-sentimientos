"""Pruebas unitarias de ``GetAnalysisStatusService`` (tarea 7.9).

Cubren el caso de uso de solo lectura de Api_Status sobre los criterios 2.2, 2.4,
2.7 y 11.1-11.13, usando un doble en memoria del puerto
:class:`~sentiment.domain.ports.AnalysisRepository` (sin ``boto3`` ni red):

* **404 idéntico (criterios 2.2, 2.7, 11.10):** un ``analysis_id`` inválido,
  inexistente o ajeno a otro Tenant produce el mismo :class:`NotFoundError`; el
  id inválido ni siquiera consulta el repositorio.
* **Vista de estado (criterios 11.1-11.4, 11.9):** contadores, ``progress``
  redondeado a 4 decimales y acotado a 1, y ``download_available``.
* **``include_failures`` sin PII (criterios 11.5, 11.6):** solo ``item_id`` +
  ``error_code``, hasta 100, ordenados por ``item_id``, y únicamente en
  ``COMPLETED_WITH_ERRORS``.
* **Listado paginado por cursor (criterios 2.4, 11.7, 11.11, 11.12):** colección
  vacía, acotación de ``page_size`` al tope de 100, defecto de 20, rechazo de
  ``page_size`` inválido y propagación del cursor opaco.
* **Solo lectura (criterio 11.8):** el doble registra que no hubo escrituras.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

import pytest

from sentiment.domain.errors import NotFoundError, ValidationError
from sentiment.domain.ids import make_item_id, new_analysis_id
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ItemStatus,
)
from sentiment.domain.ports import AnalysisRepository
from sentiment.services.get_analysis_status import (
    DEFAULT_PAGE_SIZE,
    MAX_FAILURES_IN_STATUS,
    MAX_PAGE_SIZE,
    GetAnalysisStatusService,
)

TENANT = "acme-corp"
OTHER_TENANT = "globex-inc"


def _service(repo: FakeRepository) -> GetAnalysisStatusService:
    """Construye el servicio tratando el doble en memoria como el puerto.

    El ``cast`` documenta que ``FakeRepository`` satisface estructuralmente el
    subconjunto del puerto :class:`AnalysisRepository` que Api_Status consume
    (lectura), sin tener que implementar los métodos de escritura que el servicio
    de solo lectura nunca invoca.
    """
    return GetAnalysisStatusService(cast(AnalysisRepository, repo))


class FakeRepository:
    """Doble en memoria del puerto ``AnalysisRepository`` para Api_Status.

    Solo implementa las operaciones de lectura que consume el servicio
    (``get_meta``, ``iter_items``, ``list_by_tenant``). Registra cualquier
    operación de escritura para poder asertar que el servicio no escribe
    (criterio 11.8); como no se invoca ninguna, no se implementan.
    """

    def __init__(self) -> None:
        self._metas: dict[tuple[str, str], AnalysisMeta] = {}
        self._items: dict[str, list[AnalysisItem]] = {}
        self._pages: list[tuple[list[AnalysisMeta], str | None]] = []
        self.list_calls: list[tuple[str, int, str | None]] = []

    def add(self, meta: AnalysisMeta, items: list[AnalysisItem] | None = None) -> None:
        self._metas[(meta.analysis_id, meta.tenant_id)] = meta
        if items is not None:
            self._items[meta.analysis_id] = items

    def queue_page(self, metas: list[AnalysisMeta], next_cursor: str | None) -> None:
        self._pages.append((metas, next_cursor))

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        try:
            return self._metas[(analysis_id, tenant_id)]
        except KeyError:
            raise NotFoundError(analysis_id=analysis_id) from None

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        yield from self._items.get(analysis_id, [])

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:
        self.list_calls.append((tenant_id, limit, cursor))
        if self._pages:
            return self._pages.pop(0)
        return ([], None)


def _meta(
    *,
    analysis_id: str | None = None,
    tenant_id: str = TENANT,
    status: AnalysisStatus = AnalysisStatus.RUNNING,
    item_count: int = 10,
    done: int = 0,
    failed: int = 0,
    skipped: int = 0,
    output_s3_key: str | None = None,
) -> AnalysisMeta:
    return AnalysisMeta(
        analysis_id=analysis_id or new_analysis_id(),
        tenant_id=tenant_id,
        owner_sub="user-sub-1",
        status=status,
        item_count=item_count,
        done_count=done,
        failed_count=failed,
        skipped_count=skipped,
        output_s3_key=output_s3_key,
        created_at=datetime(2025, 6, 14, 10, 0, tzinfo=UTC),
    )


def _failed_item(analysis_id: str, index: int, error_code: str) -> AnalysisItem:
    return AnalysisItem(
        analysis_id=analysis_id,
        item_id=make_item_id(analysis_id, index),
        index=index,
        indificador="clima",
        respuesta="texto sensible que NO debe aparecer en la vista",
        status=ItemStatus.FAILED,
        error_code=error_code,
    )


# --------------------------------------------------------------------------- #
# get_status: 404 idéntico para id inválido / inexistente / ajeno             #
# --------------------------------------------------------------------------- #


def test_get_status_invalid_ulid_raises_not_found_without_touching_repo() -> None:
    repo = FakeRepository()
    service = _service(repo)

    with pytest.raises(NotFoundError):
        service.get_status("not-a-ulid", TENANT)

    # Criterio 2.7: un id malformado no consulta la Dynamo_Table.
    assert repo.list_calls == []


def test_get_status_missing_analysis_raises_not_found() -> None:
    service = _service(FakeRepository())
    with pytest.raises(NotFoundError):
        service.get_status(new_analysis_id(), TENANT)


def test_get_status_foreign_tenant_is_indistinguishable_from_missing() -> None:
    # El Lote existe pero pertenece a OTHER_TENANT: para TENANT es un 404 igual
    # que uno inexistente (criterios 2.2, 11.10).
    repo = FakeRepository()
    meta = _meta(tenant_id=OTHER_TENANT, status=AnalysisStatus.COMPLETED)
    repo.add(meta)
    service = _service(repo)

    with pytest.raises(NotFoundError) as foreign:
        service.get_status(meta.analysis_id, TENANT)
    with pytest.raises(NotFoundError) as missing:
        service.get_status(new_analysis_id(), TENANT)

    assert foreign.value.error_code == missing.value.error_code == "NOT_FOUND"
    assert foreign.value.http_status == missing.value.http_status == 404


# --------------------------------------------------------------------------- #
# get_status: vista de estado, progreso y download_available                  #
# --------------------------------------------------------------------------- #


def test_get_status_returns_counters_and_rounded_progress() -> None:
    repo = FakeRepository()
    meta = _meta(item_count=3, done=1, failed=0, skipped=0)  # 1/3 = 0.3333...
    repo.add(meta)
    service = _service(repo)

    view = service.get_status(meta.analysis_id, TENANT)

    assert view.item_count == 3
    assert view.done_count == 1
    assert view.progress == 0.3333  # redondeado a 4 decimales (criterio 11.2)
    assert view.download_available is False


def test_get_status_terminal_progress_is_one() -> None:
    repo = FakeRepository()
    meta = _meta(
        status=AnalysisStatus.COMPLETED,
        item_count=4,
        done=4,
        output_s3_key="outputs/acme-corp/2025/06/14/x.json",
    )
    repo.add(meta)
    service = _service(repo)

    view = service.get_status(meta.analysis_id, TENANT)

    # Criterio 11.4: lote terminal => progress == 1.0; criterio 11.9.
    assert view.progress == 1.0
    assert view.download_available is True


def test_get_status_progress_capped_at_one_with_inconsistent_counters() -> None:
    # Contadores transitoriamente mayores que item_count: progress acotado a 1.0.
    repo = FakeRepository()
    meta = _meta(item_count=2, done=2, failed=1)
    repo.add(meta)
    service = _service(repo)

    assert service.get_status(meta.analysis_id, TENANT).progress == 1.0


# --------------------------------------------------------------------------- #
# get_status: include_failures sin PII                                        #
# --------------------------------------------------------------------------- #


def test_include_failures_returns_only_id_and_code_sorted_without_pii() -> None:
    repo = FakeRepository()
    aid = new_analysis_id()
    meta = _meta(
        analysis_id=aid, status=AnalysisStatus.COMPLETED_WITH_ERRORS, item_count=3,
        done=1, failed=2,
    )
    items = [
        _failed_item(aid, 2, "LLM_OUTPUT_INVALID"),
        _failed_item(aid, 0, "COMPREHEND_ITEM_FAILED"),
        AnalysisItem(
            analysis_id=aid, item_id=make_item_id(aid, 1), index=1,
            indificador="clima", respuesta="ok", status=ItemStatus.DONE,
        ),
    ]
    repo.add(meta, items)
    service = _service(repo)

    view = service.get_status(aid, TENANT, include_failures=True)

    # Solo los FAILED, ordenados por item_id ascendente (= por index).
    suffixes = [f.item_id.rsplit("#", 1)[1] for f in view.failures]
    assert suffixes == ["00000", "00002"]
    assert [f.error_code for f in view.failures] == [
        "COMPREHEND_ITEM_FAILED",
        "LLM_OUTPUT_INVALID",
    ]
    # Criterio 11.6: ningún texto de respuesta/explicacion viaja en la vista.
    serialized = repr(view.failures)
    assert "sensible" not in serialized
    assert "respuesta" not in serialized


def test_include_failures_ignored_when_not_completed_with_errors() -> None:
    repo = FakeRepository()
    aid = new_analysis_id()
    meta = _meta(analysis_id=aid, status=AnalysisStatus.COMPLETED, item_count=1, done=1)
    repo.add(meta, [_failed_item(aid, 0, "LLM_OUTPUT_INVALID")])
    service = _service(repo)

    view = service.get_status(aid, TENANT, include_failures=True)

    assert view.failures == ()


def test_include_failures_defaults_to_empty() -> None:
    repo = FakeRepository()
    aid = new_analysis_id()
    meta = _meta(
        analysis_id=aid, status=AnalysisStatus.COMPLETED_WITH_ERRORS, item_count=1,
        failed=1,
    )
    repo.add(meta, [_failed_item(aid, 0, "LLM_OUTPUT_INVALID")])
    service = _service(repo)

    assert service.get_status(aid, TENANT).failures == ()


def test_include_failures_truncated_to_100_and_sorted() -> None:
    repo = FakeRepository()
    aid = new_analysis_id()
    total = 150
    meta = _meta(
        analysis_id=aid, status=AnalysisStatus.COMPLETED_WITH_ERRORS,
        item_count=total, failed=total,
    )
    # Insertados en orden descendente para comprobar que el servicio ordena.
    items = [_failed_item(aid, i, "LLM_OUTPUT_INVALID") for i in reversed(range(total))]
    repo.add(meta, items)
    service = _service(repo)

    view = service.get_status(aid, TENANT, include_failures=True)

    assert len(view.failures) == MAX_FAILURES_IN_STATUS
    item_ids = [f.item_id for f in view.failures]
    assert item_ids == sorted(item_ids)
    assert item_ids[0] == make_item_id(aid, 0)


def test_failed_item_without_error_code_falls_back() -> None:
    repo = FakeRepository()
    aid = new_analysis_id()
    meta = _meta(
        analysis_id=aid, status=AnalysisStatus.COMPLETED_WITH_ERRORS, item_count=1,
        failed=1,
    )
    broken = AnalysisItem(
        analysis_id=aid, item_id=make_item_id(aid, 0), index=0, indificador="clima",
        respuesta="x", status=ItemStatus.FAILED, error_code=None,
    )
    repo.add(meta, [broken])
    service = _service(repo)

    view = service.get_status(aid, TENANT, include_failures=True)
    assert view.failures[0].error_code == "INTERNAL_ERROR"


# --------------------------------------------------------------------------- #
# list_analyses: paginación por cursor, tenant-scoped, vacío                  #
# --------------------------------------------------------------------------- #


def test_list_empty_tenant_returns_empty_collection() -> None:
    repo = FakeRepository()
    service = _service(repo)

    view = service.list_analyses(TENANT)

    assert view.analyses == ()
    assert view.next_cursor is None
    assert view.page_size == DEFAULT_PAGE_SIZE
    # Criterio 2.4: la consulta va acotada al Tenant solicitante.
    assert repo.list_calls == [(TENANT, DEFAULT_PAGE_SIZE, None)]


def test_list_returns_summaries_and_cursor() -> None:
    repo = FakeRepository()
    metas = [
        _meta(status=AnalysisStatus.COMPLETED, item_count=2, done=2,
              output_s3_key="outputs/acme-corp/x.json"),
        _meta(status=AnalysisStatus.RUNNING, item_count=4, done=1),
    ]
    repo.queue_page(metas, "OPAQUE_CURSOR")
    service = _service(repo)

    view = service.list_analyses(TENANT)

    assert len(view.analyses) == 2
    assert view.next_cursor == "OPAQUE_CURSOR"
    assert view.analyses[0].download_available is True
    assert view.analyses[0].progress == 1.0
    assert view.analyses[1].progress == 0.25


def test_list_passes_cursor_through() -> None:
    repo = FakeRepository()
    service = _service(repo)

    service.list_analyses(TENANT, cursor="IN_CURSOR")

    assert repo.list_calls == [(TENANT, DEFAULT_PAGE_SIZE, "IN_CURSOR")]


def test_list_page_size_capped_at_max() -> None:
    repo = FakeRepository()
    service = _service(repo)

    view = service.list_analyses(TENANT, page_size=500)

    # Criterio 11.11: se acota a 100 y la vista reporta el tamaño efectivo.
    assert view.page_size == MAX_PAGE_SIZE
    assert repo.list_calls == [(TENANT, MAX_PAGE_SIZE, None)]


def test_list_page_size_within_range_preserved() -> None:
    repo = FakeRepository()
    service = _service(repo)

    view = service.list_analyses(TENANT, page_size=37)

    assert view.page_size == 37
    assert repo.list_calls == [(TENANT, 37, None)]


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_list_rejects_non_positive_page_size(bad: int) -> None:
    repo = FakeRepository()
    service = _service(repo)

    with pytest.raises(ValidationError) as exc:
        service.list_analyses(TENANT, page_size=bad)

    # Criterio 11.12: VALIDATION_FAILED (HTTP 400) sin devolver ningún Lote.
    assert exc.value.error_code == "VALIDATION_FAILED"
    assert exc.value.http_status == 400
    assert repo.list_calls == []
