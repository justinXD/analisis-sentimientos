"""Pruebas unitarias de ``PurgeAnalysisService`` (tarea 7.11).

Cubren el caso de uso de Api_Purge sobre los criterios 19.3, 19.4, 19.5, 19.6,
19.7, 19.8, 19.9 y 19.11, con dobles en memoria de los puertos
``PurgeRepository``, ``PurgeStorage`` y ``AuditLog`` (sin ``boto3`` ni red):

* **404 idéntico (criterios 19.5, 2.7):** id inválido, inexistente o ajeno; no se
  borra nada.
* **409 si no está listo (criterio 19.6):** Lote propio no terminal; no se borra
  nada.
* **Orden de borrado (criterios 19.3, 19.4):** ITEMs → objetos de S3 (todas las
  versiones) → META al final; claves exactas derivadas del META y prefijo
  ``map-results/`` derivado del ``analysis_id``.
* **Reintentable (criterios 19.7, 19.8):** un fallo parcial conserva el META y no
  registra el evento; una reejecución completa la purga.
* **Evento auditable solo tras éxito, sin PII (criterios 19.9, 19.11).**
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from sentiment.domain.errors import NotFoundError, NotReadyError
from sentiment.domain.ids import new_analysis_id
from sentiment.domain.models import AnalysisMeta, AnalysisStatus
from sentiment.domain.ports import AuditLog, PurgeRepository, PurgeStorage
from sentiment.services.purge_analysis import (
    MAP_RESULTS_PREFIX_TEMPLATE,
    PurgeAnalysisService,
    PurgeCommand,
)

TENANT = "acme-corp"
OTHER_TENANT = "globex-inc"
NOW = datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC)
INPUT_KEY = "inputs/acme-corp/2025/06/14/analysis.json"
OUTPUT_KEY = "outputs/acme-corp/2025/06/14/analysis.json"


class FakeRepository:
    """Doble en memoria del puerto ``PurgeRepository``.

    Registra el orden de las llamadas de borrado para asertar que el META se
    elimina en último lugar (criterio 19.3). Permite inyectar un fallo en
    ``delete_items`` para ejercitar la reintentabilidad (criterios 19.7, 19.8).
    """

    def __init__(self) -> None:
        self._metas: dict[tuple[str, str], AnalysisMeta] = {}
        self._items_remaining: dict[str, int] = {}
        self.calls: list[str] = []
        self.fail_delete_items_times = 0

    def add(self, meta: AnalysisMeta, item_count: int = 3) -> None:
        self._metas[(meta.analysis_id, meta.tenant_id)] = meta
        self._items_remaining[meta.analysis_id] = item_count

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        self.calls.append("get_meta")
        try:
            return self._metas[(analysis_id, tenant_id)]
        except KeyError:
            raise NotFoundError(analysis_id=analysis_id) from None

    def delete_items(self, analysis_id: str, tenant_id: str) -> int:
        self.calls.append("delete_items")
        if self.fail_delete_items_times > 0:
            self.fail_delete_items_times -= 1
            raise RuntimeError("transient dynamo failure")
        deleted = self._items_remaining.get(analysis_id, 0)
        self._items_remaining[analysis_id] = 0  # ya ausentes: idempotente
        return deleted

    def delete_meta(self, analysis_id: str, tenant_id: str) -> None:
        self.calls.append("delete_meta")
        self._metas.pop((analysis_id, tenant_id), None)

    def meta_exists(self, analysis_id: str, tenant_id: str) -> bool:
        return (analysis_id, tenant_id) in self._metas


class FakeStorage:
    """Doble en memoria del puerto ``PurgeStorage``.

    Registra los argumentos de ``delete_all_versions`` para asertar las claves
    exactas y el prefijo (criterio 19.4). Permite inyectar un fallo para la
    reintentabilidad.
    """

    def __init__(self, *, versions_to_delete: int = 5) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self._versions = versions_to_delete
        self.fail_times = 0

    def delete_all_versions(self, exact_keys: list[str], prefix: str) -> int:
        self.calls.append((tuple(exact_keys), prefix))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transient s3 failure")
        deleted = self._versions
        self._versions = 0  # ya ausentes: idempotente
        return deleted


class FakeAudit:
    """Doble en memoria del puerto ``AuditLog``; captura los campos registrados."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record_purge_completed(self, **fields: object) -> None:
        self.records.append(dict(fields))


def _clock() -> datetime:
    return NOW


def _meta(
    *,
    analysis_id: str | None = None,
    tenant_id: str = TENANT,
    status: AnalysisStatus = AnalysisStatus.COMPLETED,
    input_s3_key: str = INPUT_KEY,
    output_s3_key: str | None = OUTPUT_KEY,
) -> AnalysisMeta:
    return AnalysisMeta(
        analysis_id=analysis_id or new_analysis_id(),
        tenant_id=tenant_id,
        owner_sub="user-sub-1",
        status=status,
        item_count=3,
        input_s3_key=input_s3_key,
        output_s3_key=output_s3_key,
        created_at=datetime(2025, 6, 14, 10, 0, tzinfo=UTC),
    )


def _service(
    repo: FakeRepository, storage: FakeStorage, audit: FakeAudit
) -> PurgeAnalysisService:
    return PurgeAnalysisService(
        cast(PurgeRepository, repo),
        cast(PurgeStorage, storage),
        cast(AuditLog, audit),
        _clock,
    )


def _command(analysis_id: str, tenant_id: str = TENANT) -> PurgeCommand:
    return PurgeCommand(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        principal_sub="dpo-sub-9",
        correlation_id="corr-123",
    )


# --------------------------------------------------------------------------- #
# 404: id inválido / inexistente / ajeno (criterios 19.5, 2.7)                #
# --------------------------------------------------------------------------- #


def test_invalid_ulid_raises_not_found_without_deleting() -> None:
    repo, storage, audit = FakeRepository(), FakeStorage(), FakeAudit()
    service = _service(repo, storage, audit)

    with pytest.raises(NotFoundError):
        service.execute(_command("not-a-ulid"))

    assert repo.calls == []  # ni siquiera se lee el META
    assert storage.calls == []
    assert audit.records == []


def test_missing_analysis_raises_not_found() -> None:
    repo, storage, audit = FakeRepository(), FakeStorage(), FakeAudit()
    service = _service(repo, storage, audit)

    with pytest.raises(NotFoundError):
        service.execute(_command(new_analysis_id()))

    assert "delete_items" not in repo.calls
    assert storage.calls == []
    assert audit.records == []


def test_foreign_tenant_is_404_without_deleting() -> None:
    repo, storage, audit = FakeRepository(), FakeStorage(), FakeAudit()
    meta = _meta(tenant_id=OTHER_TENANT)
    repo.add(meta)
    service = _service(repo, storage, audit)

    with pytest.raises(NotFoundError):
        service.execute(_command(meta.analysis_id, tenant_id=TENANT))

    assert "delete_items" not in repo.calls
    assert storage.calls == []
    assert audit.records == []
    assert repo.meta_exists(meta.analysis_id, OTHER_TENANT)  # intacto


# --------------------------------------------------------------------------- #
# 409: Lote propio no terminal (criterio 19.6)                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [AnalysisStatus.PENDING, AnalysisStatus.RUNNING])
def test_non_terminal_raises_not_ready_without_deleting(
    status: AnalysisStatus,
) -> None:
    repo, storage, audit = FakeRepository(), FakeStorage(), FakeAudit()
    meta = _meta(status=status)
    repo.add(meta)
    service = _service(repo, storage, audit)

    with pytest.raises(NotReadyError) as exc:
        service.execute(_command(meta.analysis_id))

    assert exc.value.http_status == 409
    assert "delete_items" not in repo.calls
    assert storage.calls == []
    assert audit.records == []
    assert repo.meta_exists(meta.analysis_id, TENANT)


# --------------------------------------------------------------------------- #
# Éxito: orden de borrado y evento auditable (criterios 19.3, 19.4, 19.9)     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status",
    [
        AnalysisStatus.COMPLETED,
        AnalysisStatus.COMPLETED_WITH_ERRORS,
        AnalysisStatus.FAILED,
        AnalysisStatus.EXPIRED,
    ],
)
def test_purge_deletes_in_order_items_objects_then_meta(
    status: AnalysisStatus,
) -> None:
    repo = FakeRepository()
    storage = FakeStorage(versions_to_delete=5)
    audit = FakeAudit()
    meta = _meta(status=status)
    repo.add(meta, item_count=3)
    service = _service(repo, storage, audit)

    result = service.execute(_command(meta.analysis_id))

    # Orden: ITEMs -> (S3) -> META al final, con el META releído primero.
    assert repo.calls == ["get_meta", "delete_items", "delete_meta"]
    assert repo.calls.index("delete_items") < repo.calls.index("delete_meta")
    # S3: claves exactas del META y prefijo map-results derivado del analysis_id.
    assert storage.calls == [
        (
            (INPUT_KEY, OUTPUT_KEY),
            MAP_RESULTS_PREFIX_TEMPLATE.format(analysis_id=meta.analysis_id),
        )
    ]
    assert not repo.meta_exists(meta.analysis_id, TENANT)
    assert result.analysis_id == meta.analysis_id
    assert result.purged_at == NOW


def test_audit_event_only_after_success_without_pii() -> None:
    repo = FakeRepository()
    storage = FakeStorage(versions_to_delete=7)
    audit = FakeAudit()
    meta = _meta()
    repo.add(meta, item_count=4)
    service = _service(repo, storage, audit)

    service.execute(_command(meta.analysis_id))

    assert len(audit.records) == 1
    record = audit.records[0]
    # records_deleted = items (4) + META (1).
    assert record["dynamodb_records_deleted"] == 5
    assert record["s3_versions_deleted"] == 7
    assert record["principal_sub"] == "dpo-sub-9"
    assert record["tenant_id"] == TENANT
    assert record["analysis_id"] == meta.analysis_id
    assert record["correlation_id"] == "corr-123"
    assert record["at"] == NOW
    # Sin PII: ningún campo del catálogo prohibido (criterio 19.11).
    serialized = str(record)
    for forbidden in ("respuesta", "explicacion", "email", "@"):
        assert forbidden not in serialized


def test_output_key_absent_is_not_sent_as_exact_key() -> None:
    # Un Lote FAILED sin Documento_Consolidado solo tiene input_s3_key.
    repo = FakeRepository()
    storage = FakeStorage()
    audit = FakeAudit()
    meta = _meta(status=AnalysisStatus.FAILED, output_s3_key=None)
    repo.add(meta)
    service = _service(repo, storage, audit)

    service.execute(_command(meta.analysis_id))

    exact_keys, _prefix = storage.calls[0]
    assert exact_keys == (INPUT_KEY,)


# --------------------------------------------------------------------------- #
# Reintentable: fallo parcial conserva el META y no audita (criterios 19.7, 19.8)
# --------------------------------------------------------------------------- #


def test_s3_failure_preserves_meta_and_skips_audit() -> None:
    repo = FakeRepository()
    storage = FakeStorage()
    storage.fail_times = 1
    audit = FakeAudit()
    meta = _meta()
    repo.add(meta)
    service = _service(repo, storage, audit)

    with pytest.raises(RuntimeError):
        service.execute(_command(meta.analysis_id))

    # El META sigue existiendo (criterio 19.7): la purga es reintentable.
    assert repo.meta_exists(meta.analysis_id, TENANT)
    assert "delete_meta" not in repo.calls
    assert audit.records == []


def test_retry_completes_purge_after_partial_failure() -> None:
    repo = FakeRepository()
    storage = FakeStorage(versions_to_delete=5)
    storage.fail_times = 1
    audit = FakeAudit()
    meta = _meta()
    repo.add(meta, item_count=2)
    service = _service(repo, storage, audit)

    # Primer intento: S3 falla tras borrar los ITEMs.
    with pytest.raises(RuntimeError):
        service.execute(_command(meta.analysis_id))
    assert repo.meta_exists(meta.analysis_id, TENANT)

    # Segundo intento (reejecución): completa la purga. Los ITEMs ya ausentes
    # cuentan como eliminados (delete_items devuelve 0).
    result = service.execute(_command(meta.analysis_id))

    assert not repo.meta_exists(meta.analysis_id, TENANT)
    assert len(audit.records) == 1
    # En el reintento se borraron 0 items + el META => 1 registro.
    assert audit.records[0]["dynamodb_records_deleted"] == 1
    assert result.purged_at == NOW
