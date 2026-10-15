"""Pruebas unitarias de ``IssueDownloadUrlService`` (tarea 7.9).

Cubren el caso de uso de Api_Download sobre los criterios 2.7, 12.1, 12.3, 12.4,
12.5, 12.6 y 12.10, con dobles en memoria de los puertos ``AnalysisRepository`` y
``ObjectStorage`` (sin ``boto3`` ni red):

* **404 idéntico (criterios 2.7, 12.6):** id inválido, inexistente o ajeno.
* **409 si no está listo (criterios 12.3, 12.4):** lote no terminal, o terminal
  sin ``output_s3_key`` (incluido ``EXPIRED`` sin consolidación), sin emitir URL
  y sin escribir en el repositorio.
* **404 si el objeto no existe (criterio 12.5):** ``output_s3_key`` presente pero
  el objeto fue expirado del bucket.
* **Éxito (criterios 12.1, 12.2, 12.10):** Presigned_URL de 900 s acotada al
  único ``output_s3_key``, ``expires_at = ahora + 900 s``, URL no persistida.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from sentiment.domain.errors import NotFoundError, NotReadyError
from sentiment.domain.ids import new_analysis_id
from sentiment.domain.models import AnalysisMeta, AnalysisStatus
from sentiment.domain.ports import AnalysisRepository, ObjectStorage
from sentiment.services.issue_download_url import (
    PRESIGN_EXPIRY_SECONDS,
    IssueDownloadUrlService,
)

TENANT = "acme-corp"
OTHER_TENANT = "globex-inc"
NOW = datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC)
OUTPUT_KEY = "outputs/acme-corp/2025/06/14/analysis.json"


class FakeRepository:
    """Doble en memoria del puerto ``AnalysisRepository`` para Api_Download."""

    def __init__(self) -> None:
        self._metas: dict[tuple[str, str], AnalysisMeta] = {}

    def add(self, meta: AnalysisMeta) -> None:
        self._metas[(meta.analysis_id, meta.tenant_id)] = meta

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        try:
            return self._metas[(analysis_id, tenant_id)]
        except KeyError:
            raise NotFoundError(analysis_id=analysis_id) from None


class FakeStorage:
    """Doble en memoria del puerto ``ObjectStorage`` para Api_Download.

    Registra cada emisión de Presigned_URL para asertar su alcance y validez, y
    permite declarar qué claves existen para ejercitar el criterio 12.5.
    """

    def __init__(self, existing: set[str] | None = None) -> None:
        self._existing = existing if existing is not None else {OUTPUT_KEY}
        self.presign_calls: list[tuple[str, int]] = []
        self.exists_calls: list[str] = []

    def object_exists(self, key: str) -> bool:
        self.exists_calls.append(key)
        return key in self._existing

    def presign_get(self, key: str, expires_in: int) -> str:
        self.presign_calls.append((key, expires_in))
        return f"https://signed.example/{key}?e={expires_in}"


def _clock() -> datetime:
    return NOW


def _meta(
    *,
    analysis_id: str | None = None,
    tenant_id: str = TENANT,
    status: AnalysisStatus = AnalysisStatus.COMPLETED,
    output_s3_key: str | None = OUTPUT_KEY,
) -> AnalysisMeta:
    return AnalysisMeta(
        analysis_id=analysis_id or new_analysis_id(),
        tenant_id=tenant_id,
        owner_sub="user-sub-1",
        status=status,
        item_count=2,
        done_count=2,
        output_s3_key=output_s3_key,
        created_at=datetime(2025, 6, 14, 10, 0, tzinfo=UTC),
    )


def _service(repo: FakeRepository, storage: FakeStorage) -> IssueDownloadUrlService:
    """Construye el servicio tratando los dobles como sus puertos respectivos.

    Los ``cast`` documentan que ``FakeRepository`` y ``FakeStorage`` satisfacen
    estructuralmente el subconjunto de los puertos ``AnalysisRepository`` y
    ``ObjectStorage`` que Api_Download consume, sin implementar métodos que este
    caso de uso no invoca.
    """
    return IssueDownloadUrlService(
        cast(AnalysisRepository, repo), cast(ObjectStorage, storage), _clock
    )


# --------------------------------------------------------------------------- #
# 404: id inválido / inexistente / ajeno (criterios 2.7, 12.6)                #
# --------------------------------------------------------------------------- #


def test_invalid_ulid_raises_not_found_without_touching_storage() -> None:
    storage = FakeStorage()
    service = _service(FakeRepository(), storage)

    with pytest.raises(NotFoundError):
        service.issue("not-a-ulid", TENANT)

    assert storage.exists_calls == []
    assert storage.presign_calls == []


def test_missing_analysis_raises_not_found() -> None:
    storage = FakeStorage()
    service = _service(FakeRepository(), storage)

    with pytest.raises(NotFoundError):
        service.issue(new_analysis_id(), TENANT)

    assert storage.presign_calls == []


def test_foreign_tenant_is_404_regardless_of_object_existence() -> None:
    # El objeto del Lote ajeno existe, pero para TENANT es un 404 idéntico al de
    # uno inexistente y nunca se emite Presigned_URL (criterios 2.3, 12.6).
    repo = FakeRepository()
    meta = _meta(tenant_id=OTHER_TENANT)
    repo.add(meta)
    storage = FakeStorage(existing={OUTPUT_KEY})
    service = _service(repo, storage)

    with pytest.raises(NotFoundError):
        service.issue(meta.analysis_id, TENANT)

    assert storage.presign_calls == []


# --------------------------------------------------------------------------- #
# 409: no terminal, o terminal sin salida (criterios 12.3, 12.4)              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [AnalysisStatus.PENDING, AnalysisStatus.RUNNING])
def test_non_terminal_raises_not_ready(status: AnalysisStatus) -> None:
    repo = FakeRepository()
    meta = _meta(status=status, output_s3_key=None)
    repo.add(meta)
    storage = FakeStorage()
    service = _service(repo, storage)

    with pytest.raises(NotReadyError) as exc:
        service.issue(meta.analysis_id, TENANT)

    assert exc.value.error_code == "ANALYSIS_NOT_READY"
    assert exc.value.http_status == 409
    # Criterio 12.4: no se consulta S3 ni se emite URL.
    assert storage.exists_calls == []
    assert storage.presign_calls == []


@pytest.mark.parametrize("missing_output", [None, ""])
def test_terminal_without_output_raises_not_ready(missing_output: str | None) -> None:
    repo = FakeRepository()
    meta = _meta(status=AnalysisStatus.EXPIRED, output_s3_key=missing_output)
    repo.add(meta)
    storage = FakeStorage()
    service = _service(repo, storage)

    with pytest.raises(NotReadyError):
        service.issue(meta.analysis_id, TENANT)

    assert storage.presign_calls == []


# --------------------------------------------------------------------------- #
# 404: output_s3_key presente pero objeto inexistente (criterio 12.5)         #
# --------------------------------------------------------------------------- #


def test_output_object_missing_raises_not_found() -> None:
    repo = FakeRepository()
    meta = _meta(status=AnalysisStatus.COMPLETED, output_s3_key=OUTPUT_KEY)
    repo.add(meta)
    storage = FakeStorage(existing=set())  # el objeto ya no existe (lifecycle)
    service = _service(repo, storage)

    with pytest.raises(NotFoundError):
        service.issue(meta.analysis_id, TENANT)

    assert storage.exists_calls == [OUTPUT_KEY]
    assert storage.presign_calls == []


# --------------------------------------------------------------------------- #
# Éxito: Presigned_URL de 900 s acotada al objeto (criterios 12.1, 12.2)      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status", [AnalysisStatus.COMPLETED, AnalysisStatus.COMPLETED_WITH_ERRORS]
)
def test_success_emits_900s_url_scoped_to_output(status: AnalysisStatus) -> None:
    repo = FakeRepository()
    meta = _meta(status=status, output_s3_key=OUTPUT_KEY)
    repo.add(meta)
    storage = FakeStorage(existing={OUTPUT_KEY})
    service = _service(repo, storage)

    view = service.issue(meta.analysis_id, TENANT)

    assert view.analysis_id == meta.analysis_id
    assert view.status is status
    assert view.url.startswith("https://signed.example/")
    # Criterio 12.2: 900 s exactos, acotado al único output_s3_key.
    assert storage.presign_calls == [(OUTPUT_KEY, PRESIGN_EXPIRY_SECONDS)]
    assert view.expires_at == NOW + timedelta(seconds=PRESIGN_EXPIRY_SECONDS)


def test_expiry_is_computed_from_injected_clock() -> None:
    assert PRESIGN_EXPIRY_SECONDS == 900
    repo = FakeRepository()
    meta = _meta()
    repo.add(meta)
    service = _service(repo, FakeStorage())

    view = service.issue(meta.analysis_id, TENANT)

    assert (view.expires_at - NOW).total_seconds() == 900
