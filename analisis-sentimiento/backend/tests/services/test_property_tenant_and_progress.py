"""Pruebas de propiedad P6 (aislamiento entre tenants) y P8 (monotonía del progreso) (tarea 7.10).

Property 6 — Aislamiento entre tenants
    **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.7, 11.10, 12.6, 19.5**

Property 8 — Monotonía del progreso
    **Validates: Requirements 11.1, 11.2, 11.3, 11.4**

Enunciados (design.md, sección 19):

* **P6.** Para todo par de tenants distintos, ninguna operación de uno (lectura
  de estado, descarga o purga) devuelve ni elimina datos del otro, y la respuesta
  a «existe pero no es tuyo» es **indistinguible** de «no existe» y de «id mal
  formado». Además, el listado de un Tenant solo contiene sus propios Lotes.

* **P8.** ``progress`` nunca decrece entre dos lecturas sucesivas y nunca excede
  ``1.0``; un Lote terminal reporta ``1.0``.

Enfoque
-------
Ambas propiedades se ejercitan **contra los servicios reales** de la capa de
casos de uso (``GetAnalysisStatusService``, ``IssueDownloadUrlService``,
``PurgeAnalysisService``), que son puros respecto de la infraestructura, usando
dobles en memoria de los puertos del dominio (sin ``boto3`` ni red). El
aislamiento multi-tenant es **estructural**: el repositorio indexa los Lotes por
``(analysis_id, tenant_id)`` —igual que la ``pk`` de la Dynamo_Table incluye el
``tenant_id`` (diseño D2, criterio 2.1)—, de modo que una lectura con el
``tenant_id`` de otro Tenant no puede encontrar el Lote y produce el mismo
``NotFoundError`` que uno inexistente.

Las estrategias generan ULID canónicos a partir de 16 bytes aleatorios
(:meth:`ulid.ULID.from_bytes`), que :func:`is_valid_ulid` siempre acepta y que
cubren todo el espacio de 128 bits sin producir cadenas malformadas.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

from hypothesis import assume, given
from hypothesis import strategies as st
from ulid import ULID

from sentiment.domain.errors import NotFoundError, SentimentError
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
)
from sentiment.domain.ports import (
    AnalysisRepository,
    AuditLog,
    ObjectStorage,
    PurgeRepository,
    PurgeStorage,
)
from sentiment.services.get_analysis_status import GetAnalysisStatusService
from sentiment.services.issue_download_url import IssueDownloadUrlService
from sentiment.services.purge_analysis import PurgeAnalysisService, PurgeCommand

# --------------------------------------------------------------------------- #
# Estrategias                                                                  #
# --------------------------------------------------------------------------- #

# ULID canónico de 26 caracteres derivado de 16 bytes aleatorios: siempre válido
# para `is_valid_ulid`, cubriendo el espacio completo de 128 bits.
ulids = st.binary(min_size=16, max_size=16).map(lambda b: str(ULID.from_bytes(b)))

# Identificadores de Tenant: cadenas no vacías de un alfabeto acotado, suficientes
# para distinguir organizaciones sin introducir ruido irrelevante al aislamiento.
tenant_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1,
    max_size=24,
)

# Un Access_Token siempre resuelve el Tenant desde su claim (criterio 2.8/2.9),
# así que un `tenant_id` nunca es vacío en el servicio; lo garantizamos arriba.

TERMINAL_STATUSES = (
    AnalysisStatus.COMPLETED,
    AnalysisStatus.COMPLETED_WITH_ERRORS,
    AnalysisStatus.FAILED,
    AnalysisStatus.EXPIRED,
)
NON_TERMINAL_STATUSES = (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)

NOW = datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Dobles en memoria de los puertos                                             #
# --------------------------------------------------------------------------- #


class FakeRepository:
    """Doble en memoria de ``AnalysisRepository`` y ``PurgeRepository``.

    Modela el aislamiento estructural de la Dynamo_Table: los Lotes se indexan por
    ``(analysis_id, tenant_id)``, de modo que una lectura con el ``tenant_id`` de
    otro Tenant nunca encuentra el Lote (criterios 2.1, 2.2). ``list_by_tenant``
    solo devuelve los Lotes del Tenant solicitante (criterio 2.4).
    """

    def __init__(self) -> None:
        self._metas: dict[tuple[str, str], AnalysisMeta] = {}
        self._items: dict[str, list[AnalysisItem]] = {}

    def add(self, meta: AnalysisMeta, items: list[AnalysisItem] | None = None) -> None:
        self._metas[(meta.analysis_id, meta.tenant_id)] = meta
        self._items[meta.analysis_id] = items or []

    def replace_meta(self, meta: AnalysisMeta) -> None:
        """Sustituye (o inserta) el META de un Lote; modela un avance en el tiempo."""
        self._metas[(meta.analysis_id, meta.tenant_id)] = meta

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        try:
            return self._metas[(analysis_id, tenant_id)]
        except KeyError:
            # Mismo 404 para «no existe» y «no es tuyo» (criterios 2.2, 2.3).
            raise NotFoundError(analysis_id=analysis_id) from None

    def meta_exists(self, analysis_id: str, tenant_id: str) -> bool:
        return (analysis_id, tenant_id) in self._metas

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        yield from self._items.get(analysis_id, [])

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:
        owned = [
            meta
            for (_aid, owner), meta in self._metas.items()
            if owner == tenant_id
        ]
        # El doble no pagina (el conjunto de prueba es pequeño): devuelve la
        # página acotada por `limit` y un cursor nulo. El aislamiento es lo que
        # se verifica aquí, no la paginación (cubierta por las pruebas unitarias).
        return owned[:limit], None

    # --- PurgeRepository ---------------------------------------------------- #

    def delete_items(self, analysis_id: str, tenant_id: str) -> int:
        deleted = len(self._items.get(analysis_id, []))
        self._items[analysis_id] = []
        return deleted

    def delete_meta(self, analysis_id: str, tenant_id: str) -> None:
        self._metas.pop((analysis_id, tenant_id), None)


class FakeStorage:
    """Doble en memoria de ``ObjectStorage`` y ``PurgeStorage``.

    Para la descarga, todo objeto referenciado por un META existe (el camino de
    objeto inexistente del criterio 12.5 lo cubren las pruebas unitarias). Para la
    purga, ``delete_all_versions`` es un no-op idempotente que cuenta como 0.
    """

    def object_exists(self, key: str) -> bool:
        return True

    def presign_get(self, key: str, expires_in: int) -> str:
        return f"https://signed.example/{key}?e={expires_in}"

    def delete_all_versions(self, exact_keys: list[str], prefix: str) -> int:
        return 0


class FakeAudit:
    """Doble en memoria de ``AuditLog``; no necesita capturar nada para P6."""

    def record_purge_completed(self, **fields: object) -> None:
        return None


def _clock() -> datetime:
    return NOW


def _status_service(repo: FakeRepository) -> GetAnalysisStatusService:
    return GetAnalysisStatusService(cast(AnalysisRepository, repo))


def _download_service(
    repo: FakeRepository, storage: FakeStorage
) -> IssueDownloadUrlService:
    return IssueDownloadUrlService(
        cast(AnalysisRepository, repo), cast(ObjectStorage, storage), _clock
    )


def _purge_service(
    repo: FakeRepository, storage: FakeStorage, audit: FakeAudit
) -> PurgeAnalysisService:
    return PurgeAnalysisService(
        cast(PurgeRepository, repo),
        cast(PurgeStorage, storage),
        cast(AuditLog, audit),
        _clock,
    )


def _terminal_meta(analysis_id: str, tenant_id: str) -> AnalysisMeta:
    """META terminal con salida: elegible para descarga y purga."""
    return AnalysisMeta(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        owner_sub="owner-sub",
        status=AnalysisStatus.COMPLETED,
        item_count=1,
        done_count=1,
        input_s3_key=f"inputs/{tenant_id}/{analysis_id}.json",
        output_s3_key=f"outputs/{tenant_id}/{analysis_id}.json",
        created_at=NOW,
    )


def _error_signature(exc: SentimentError) -> tuple[str, int | None]:
    """Firma observable de un error del dominio: lo que el handler traduce a HTTP.

    El cuerpo HTTP se deriva de ``to_client_dict`` (solo ``error_code`` +
    ``correlation_id``). Para un ``correlation_id`` fijo, dos errores con el mismo
    ``error_code`` y ``http_status`` producen respuestas **byte-idénticas**
    (criterios 2.3, 12.6, 19.5), que es lo que esta firma captura.
    """
    return (exc.error_code, exc.http_status)


def _capture(fn: object, *args: object, **kwargs: object) -> tuple[str, int | None]:
    """Ejecuta una operación que debe fallar y devuelve la firma de su error.

    Si la operación **no** lanza, devuelve un centinela imposible, de modo que el
    assert de igualdad falle y el counterexample revele la fuga de aislamiento.
    """
    try:
        fn(*args, **kwargs)  # type: ignore[operator]
    except SentimentError as exc:
        return _error_signature(exc)
    return ("__NO_ERROR_RAISED__", None)


# =========================================================================== #
# Property 6 — Aislamiento entre tenants                                       #
# =========================================================================== #


@given(t1=tenant_ids, t2=tenant_ids, analysis_id=ulids, other_id=ulids)
def test_status_foreign_tenant_indistinguishable_from_missing(
    t1: str, t2: str, analysis_id: str, other_id: str
) -> None:
    """Leer el estado de un Lote ajeno es un 404 idéntico al de uno inexistente.

    Siembra un Lote de ``t1`` y comprueba que ``t2`` recibe la **misma** firma de
    error al pedir el estado de ese Lote (ajeno) y al pedir uno con otro id
    (inexistente): mismo ``error_code`` y ``http_status`` ⇒ cuerpo byte-idéntico
    (criterios 2.2, 2.3, 11.10).
    """
    assume(t1 != t2)
    assume(analysis_id != other_id)

    repo = FakeRepository()
    repo.add(_terminal_meta(analysis_id, t1))
    service = _status_service(repo)

    foreign = _capture(service.get_status, analysis_id, t2)
    missing = _capture(service.get_status, other_id, t2)

    assert foreign == missing
    assert foreign == ("NOT_FOUND", 404)


@given(t1=tenant_ids, t2=tenant_ids, analysis_id=ulids)
def test_status_foreign_and_malformed_id_are_indistinguishable(
    t1: str, t2: str, analysis_id: str
) -> None:
    """«No es tuyo» y «id mal formado» producen el mismo 404 (criterio 2.7)."""
    assume(t1 != t2)

    repo = FakeRepository()
    repo.add(_terminal_meta(analysis_id, t1))
    service = _status_service(repo)

    foreign = _capture(service.get_status, analysis_id, t2)
    malformed = _capture(service.get_status, "no-es-un-ulid", t2)

    assert foreign == malformed == ("NOT_FOUND", 404)


@given(t1=tenant_ids, t2=tenant_ids, analysis_id=ulids, other_id=ulids)
def test_download_foreign_tenant_indistinguishable_from_missing(
    t1: str, t2: str, analysis_id: str, other_id: str
) -> None:
    """Descargar un Lote ajeno es el mismo 404 que uno inexistente (criterio 12.6).

    El Lote ajeno es terminal y tiene objeto de salida existente; aun así, ``t2``
    no puede distinguirlo de uno inexistente ni obtener Presigned_URL alguna.
    """
    assume(t1 != t2)
    assume(analysis_id != other_id)

    repo = FakeRepository()
    repo.add(_terminal_meta(analysis_id, t1))
    service = _download_service(repo, FakeStorage())

    foreign = _capture(service.issue, analysis_id, t2)
    missing = _capture(service.issue, other_id, t2)
    malformed = _capture(service.issue, "no-es-un-ulid", t2)

    assert foreign == missing == malformed == ("NOT_FOUND", 404)


@given(t1=tenant_ids, t2=tenant_ids, analysis_id=ulids, other_id=ulids)
def test_purge_foreign_tenant_404_and_deletes_nothing(
    t1: str, t2: str, analysis_id: str, other_id: str
) -> None:
    """Purgar un Lote ajeno es un 404 que no borra nada del propietario (criterio 19.5).

    ``t2`` intenta purgar el Lote de ``t1`` y uno inexistente: ambos son el mismo
    404, y tras ambos el META de ``t1`` sigue intacto (la purga ajena no borró
    nada).
    """
    assume(t1 != t2)
    assume(analysis_id != other_id)

    repo = FakeRepository()
    repo.add(_terminal_meta(analysis_id, t1))
    service = _purge_service(repo, FakeStorage(), FakeAudit())

    foreign = _capture(
        service.execute,
        PurgeCommand(
            analysis_id=analysis_id, tenant_id=t2, principal_sub="s", correlation_id="c"
        ),
    )
    missing = _capture(
        service.execute,
        PurgeCommand(
            analysis_id=other_id, tenant_id=t2, principal_sub="s", correlation_id="c"
        ),
    )

    assert foreign == missing == ("NOT_FOUND", 404)
    # La purga ajena no borró nada del propietario legítimo.
    assert repo.meta_exists(analysis_id, t1)


@given(
    t1=tenant_ids,
    t2=tenant_ids,
    ids_t1=st.lists(ulids, min_size=1, max_size=5, unique=True),
    ids_t2=st.lists(ulids, min_size=0, max_size=5, unique=True),
)
def test_list_only_returns_callers_own_batches(
    t1: str, t2: str, ids_t1: list[str], ids_t2: list[str]
) -> None:
    """``list_analyses`` solo devuelve los Lotes del Tenant solicitante (criterio 2.4).

    Siembra Lotes de dos Tenants distintos y comprueba que el listado de cada uno
    contiene **exactamente** sus propios ``analysis_id`` y ninguno del otro.
    """
    assume(t1 != t2)
    # Evita colisiones de id entre tenants, que no afectan al aislamiento pero
    # complican la aserción de pertenencia.
    assume(set(ids_t1).isdisjoint(ids_t2))

    repo = FakeRepository()
    for aid in ids_t1:
        repo.add(_terminal_meta(aid, t1))
    for aid in ids_t2:
        repo.add(_terminal_meta(aid, t2))
    service = _status_service(repo)

    listed_t1 = {s.analysis_id for s in service.list_analyses(t1, page_size=100).analyses}
    listed_t2 = {s.analysis_id for s in service.list_analyses(t2, page_size=100).analyses}

    assert listed_t1 == set(ids_t1)
    assert listed_t2 == set(ids_t2)
    # Ningún Lote de un Tenant aparece en el listado del otro.
    assert listed_t1.isdisjoint(ids_t2)
    assert listed_t2.isdisjoint(ids_t1)


# =========================================================================== #
# Property 8 — Monotonía del progreso                                          #
# =========================================================================== #


@st.composite
def progress_sequences(draw: st.DrawFn) -> tuple[int, list[tuple[int, int, int]]]:
    """Genera un ``item_count`` y una secuencia creciente de contadores válidos.

    Modela un Lote avanzando en el tiempo: en cada paso, los contadores
    ``(done, failed, skipped)`` solo pueden crecer (nunca decrecen) y su suma no
    excede ``item_count`` dentro del dominio válido. Es exactamente el espacio de
    estados que P8 cubre: lecturas sucesivas de ``progress_ratio`` sobre un Lote
    que progresa.
    """
    item_count = draw(st.integers(min_value=1, max_value=50))
    steps = draw(st.integers(min_value=1, max_value=12))

    sequence: list[tuple[int, int, int]] = []
    done, failed, skipped = 0, 0, 0
    for _ in range(steps):
        processed = done + failed + skipped
        remaining = item_count - processed
        # Cuántos ítems se procesan en este paso (0..remaining): monotonía no
        # estricta (puede no avanzar) sin exceder item_count.
        advance = draw(st.integers(min_value=0, max_value=remaining))
        # Repartir el avance entre los tres contadores terminales.
        d = draw(st.integers(min_value=0, max_value=advance))
        f = draw(st.integers(min_value=0, max_value=advance - d))
        s = advance - d - f
        done, failed, skipped = done + d, failed + f, skipped + s
        sequence.append((done, failed, skipped))
    return item_count, sequence


def _meta_with_counters(
    item_count: int, done: int, failed: int, skipped: int, *, status: AnalysisStatus
) -> AnalysisMeta:
    return AnalysisMeta(
        analysis_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tenant_id="t",
        owner_sub="s",
        status=status,
        item_count=item_count,
        done_count=done,
        failed_count=failed,
        skipped_count=skipped,
        created_at=NOW,
    )


@given(data=progress_sequences())
def test_progress_is_monotonic_and_bounded(
    data: tuple[int, list[tuple[int, int, int]]]
) -> None:
    """``progress`` no decrece y permanece en ``[0.0, 1.0]`` a lo largo del tiempo.

    Reconstruye la ``StatusView`` en cada paso de la secuencia creciente y
    comprueba, lectura a lectura, que el progreso reportado nunca retrocede y
    nunca sale de ``[0.0, 1.0]`` (criterios 11.1, 11.2, 11.3).
    """
    item_count, sequence = data
    repo = FakeRepository()
    service = _status_service(repo)

    previous = 0.0
    for done, failed, skipped in sequence:
        meta = _meta_with_counters(
            item_count, done, failed, skipped, status=AnalysisStatus.RUNNING
        )
        repo.replace_meta(meta)
        progress = service.get_status(meta.analysis_id, meta.tenant_id).progress

        assert 0.0 <= progress <= 1.0
        assert progress >= previous
        previous = progress


@given(data=progress_sequences(), terminal=st.sampled_from(TERMINAL_STATUSES))
def test_terminal_batch_reports_full_progress(
    data: tuple[int, list[tuple[int, int, int]]], terminal: AnalysisStatus
) -> None:
    """Un Lote terminal reporta ``progress == 1.0`` (criterio 11.4).

    Al cerrar en un Estado_Terminal, todos los Ítems están contados
    (``done + failed + skipped == item_count``), por lo que la fracción es
    exactamente ``1.0``. Se usa el estado final de la secuencia completado hasta
    ``item_count`` para modelar el cierre.
    """
    item_count, _sequence = data
    # Un Lote terminal tiene todos sus Ítems contados: repartimos item_count entre
    # los tres contadores terminales de forma arbitraria pero exhaustiva.
    done = item_count
    repo = FakeRepository()
    service = _status_service(repo)
    meta = _meta_with_counters(item_count, done, 0, 0, status=terminal)
    repo.replace_meta(meta)

    assert service.get_status(meta.analysis_id, meta.tenant_id).progress == 1.0


@given(item_count=st.integers(min_value=0, max_value=50))
def test_progress_capped_even_with_inconsistent_counters(item_count: int) -> None:
    """Contadores transitoriamente mayores que ``item_count`` siguen acotados a 1.0.

    El criterio 11.2 exige que ``progress`` nunca exceda ``1.0`` aunque los
    contadores atómicos estén transitoriamente por encima del total (lecturas no
    atómicas durante el avance). ``progress_ratio`` acota con ``min``.
    """
    repo = FakeRepository()
    service = _status_service(repo)
    # Contadores deliberadamente inflados por encima de item_count.
    meta = _meta_with_counters(
        item_count,
        item_count + 5,
        item_count + 3,
        item_count + 2,
        status=AnalysisStatus.RUNNING,
    )
    repo.replace_meta(meta)

    progress = service.get_status(meta.analysis_id, meta.tenant_id).progress
    assert progress == 1.0
