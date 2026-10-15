"""Pruebas de humo de los adaptadores de soporte (tarea 6.7).

Cubren el camino feliz y los bordes esenciales de los tres adaptadores de
soporte; las pruebas de contrato exhaustivas son la tarea 6.8.

* :class:`~sentiment.adapters.stepfunctions_adapter.StepFunctionsAdapter`:
  ``describe_status`` deriva el ARN determinista de la ejecución a partir de
  ``STATE_MACHINE_ARN``, devuelve el estado de la ejecución y traduce
  ``ExecutionDoesNotExist`` a ``None`` (criterios 10.11, 10.12).
* :meth:`~sentiment.adapters.s3_storage.S3Storage.delete_all_versions`: borra
  todas las versiones y marcadores de eliminación de las claves exactas y de todo
  lo que cuelga de un prefijo, sobre un bucket **versionado** simulado con
  ``moto``, y es reintentable (criterios 19.4, 19.8).
* :class:`~sentiment.adapters.audit_log.AuditLog`: ``record_purge_completed``
  emite un único evento ``purge_completed`` con solo metadatos y sin PII
  (criterio 19.11).
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from itertools import count
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger
    from botocore.client import BaseClient

from sentiment.adapters.audit_log import AuditLog
from sentiment.adapters.s3_storage import S3Storage
from sentiment.adapters.stepfunctions_adapter import StepFunctionsAdapter
from sentiment.observability.config import ConfigError
from sentiment.observability.logging import get_logger

_REGION = "us-east-1"
_ACCOUNT = "123456789012"
_STATE_MACHINE_ARN = (
    f"arn:aws:states:{_REGION}:{_ACCOUNT}:stateMachine:sentiment-analysis"
)
_ANALYSIS_ID = "01JDQZ8F3K7XWQ2M5N8P4R6T9V"
_BUCKET = "sentiment-test-analysis-123456789012"

# PII sintética que NUNCA debe aparecer en un evento de auditoría.
_SECRET_RESPUESTA = "me siento comodo pero quisiera mejor salario"
_SECRET_EMAIL = "rh@ejemplo.com"


# ===========================================================================
# StepFunctionsAdapter
# ===========================================================================


class _DescribeExecutionSpy:
    """Cliente de Step Functions de prueba que registra el ARN consultado.

    Devuelve un estado fijo o, si ``raise_not_found`` es ``True``, simula la
    respuesta ``ExecutionDoesNotExist`` que da ``DescribeExecution`` cuando no hay
    ejecución con ese nombre.
    """

    def __init__(self, *, status: str = "RUNNING", raise_not_found: bool = False) -> None:
        self._status = status
        self._raise_not_found = raise_not_found
        self.seen_arn: str | None = None

    def describe_execution(self, *, executionArn: str) -> dict[str, object]:  # noqa: N803
        self.seen_arn = executionArn
        if self._raise_not_found:
            raise ClientError(
                {"Error": {"Code": "ExecutionDoesNotExist", "Message": "nope"}},
                "DescribeExecution",
            )
        return {"status": self._status}


def test_describe_status_derives_execution_arn_and_returns_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deriva el ARN ``:execution:...:{analysis_id}`` y devuelve el estado (criterio 10.11)."""
    monkeypatch.setenv("STATE_MACHINE_ARN", _STATE_MACHINE_ARN)
    spy = _DescribeExecutionSpy(status="SUCCEEDED")

    status = StepFunctionsAdapter(client=spy).describe_status(_ANALYSIS_ID)

    assert status == "SUCCEEDED"
    assert spy.seen_arn == (
        f"arn:aws:states:{_REGION}:{_ACCOUNT}:execution:"
        f"sentiment-analysis:{_ANALYSIS_ID}"
    )


def test_describe_status_returns_none_when_execution_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ExecutionDoesNotExist`` se traduce a ``None``, nunca a un error (criterio 10.12)."""
    monkeypatch.setenv("STATE_MACHINE_ARN", _STATE_MACHINE_ARN)
    spy = _DescribeExecutionSpy(raise_not_found=True)

    assert StepFunctionsAdapter(client=spy).describe_status(_ANALYSIS_ID) is None


def test_describe_status_propagates_other_client_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un error distinto de ``ExecutionDoesNotExist`` se propaga, no se trata como ``None``."""
    monkeypatch.setenv("STATE_MACHINE_ARN", _STATE_MACHINE_ARN)

    class _ThrottledClient:
        def describe_execution(self, *, executionArn: str) -> dict[str, object]:  # noqa: N803
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "DescribeExecution",
            )

    with pytest.raises(ClientError):
        StepFunctionsAdapter(client=_ThrottledClient()).describe_status(_ANALYSIS_ID)


def test_describe_status_missing_env_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin ``STATE_MACHINE_ARN`` configurada, falla con mensaje accionable (criterio 17.7)."""
    monkeypatch.delenv("STATE_MACHINE_ARN", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        StepFunctionsAdapter(client=_DescribeExecutionSpy()).describe_status(_ANALYSIS_ID)
    assert "STATE_MACHINE_ARN" in str(excinfo.value)


# ===========================================================================
# S3Storage.delete_all_versions (PurgeStorage)
# ===========================================================================


@pytest.fixture
def versioned_bucket() -> Iterator[BaseClient]:
    """Crea un bucket S3 **versionado** simulado y cede un cliente apuntando a él."""
    with mock_aws():
        client = boto3.client("s3", region_name=_REGION)
        client.create_bucket(Bucket=_BUCKET)
        client.put_bucket_versioning(
            Bucket=_BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        yield client


def _put(client: BaseClient, key: str, body: bytes) -> None:
    """Escribe un objeto en el bucket de prueba (helper tipado para los raw calls).

    ``boto3-stubs`` sin los *extras* por servicio no expone ``put_object`` sobre
    ``BaseClient``; el ``type: ignore`` se concentra aquí, no en cada llamada.
    """
    client.put_object(Bucket=_BUCKET, Key=key, Body=body)  # type: ignore[attr-defined]


def _delete(client: BaseClient, key: str) -> None:
    """Pone un marcador de eliminación sobre ``key`` (``DeleteObject`` simple)."""
    client.delete_object(Bucket=_BUCKET, Key=key)  # type: ignore[attr-defined]


def _count_versions(client: BaseClient, prefix: str = "") -> int:
    """Cuenta las versiones y marcadores de eliminación bajo ``prefix``."""
    response = client.list_object_versions(  # type: ignore[attr-defined]
        Bucket=_BUCKET, Prefix=prefix
    )
    return len(response.get("Versions", [])) + len(response.get("DeleteMarkers", []))


def test_delete_all_versions_removes_every_version_and_marker(
    versioned_bucket: BaseClient,
) -> None:
    """Borra todas las versiones de las claves exactas y del prefijo (criterio 19.4)."""
    input_key = f"inputs/acme/2025/06/14/{_ANALYSIS_ID}.json"
    output_key = f"outputs/acme/2025/06/14/{_ANALYSIS_ID}.json"
    map_prefix = f"map-results/{_ANALYSIS_ID}/"

    # Escrituras múltiples para generar varias versiones de la misma clave, y un
    # delete marker (el DeleteObject simple que la purga debe superar).
    _put(versioned_bucket, input_key, b'{"v":1}')
    _put(versioned_bucket, input_key, b'{"v":2}')
    _put(versioned_bucket, output_key, b'{"out":1}')
    _delete(versioned_bucket, output_key)  # delete marker
    _put(versioned_bucket, f"{map_prefix}result-0.json", b"{}")
    _put(versioned_bucket, f"{map_prefix}result-1.json", b"{}")

    assert _count_versions(versioned_bucket) > 0

    storage = S3Storage(client=versioned_bucket, bucket_name=_BUCKET)
    deleted = storage.delete_all_versions(
        exact_keys=[input_key, output_key], prefix=map_prefix
    )

    assert deleted >= 5
    assert _count_versions(versioned_bucket) == 0


def test_delete_all_versions_is_retryable_and_returns_zero_when_empty(
    versioned_bucket: BaseClient,
) -> None:
    """Una segunda llamada sin versiones devuelve 0 sin error (criterio 19.8)."""
    input_key = f"inputs/acme/2025/06/14/{_ANALYSIS_ID}.json"
    _put(versioned_bucket, input_key, b"{}")
    storage = S3Storage(client=versioned_bucket, bucket_name=_BUCKET)

    first = storage.delete_all_versions(exact_keys=[input_key], prefix="")
    second = storage.delete_all_versions(exact_keys=[input_key], prefix="")

    assert first >= 1
    assert second == 0


def test_delete_all_versions_ignores_empty_prefix(
    versioned_bucket: BaseClient,
) -> None:
    """Un prefijo vacío no enumera el bucket completo: solo se tocan las claves exactas."""
    kept_key = "inputs/other-tenant/2025/06/14/01OTHER.json"
    target_key = f"inputs/acme/2025/06/14/{_ANALYSIS_ID}.json"
    _put(versioned_bucket, kept_key, b"{}")
    _put(versioned_bucket, target_key, b"{}")
    storage = S3Storage(client=versioned_bucket, bucket_name=_BUCKET)

    deleted = storage.delete_all_versions(exact_keys=[target_key], prefix="")

    assert deleted == 1
    assert _count_versions(versioned_bucket, prefix=kept_key) == 1


# ===========================================================================
# AuditLog.record_purge_completed
# ===========================================================================


_audit_logger_seq = count()


def _audit_logger_to(stream: io.StringIO) -> Logger:
    """Construye el logger de auditoría real dirigido a un flujo capturable.

    Usa el mismo formateador con redacción de PII que el adaptador en producción
    (vía :func:`sentiment.observability.logging.get_logger`), pero escribe a un
    ``StringIO`` en lugar de a ``sys.stdout``. AWS Lambda Powertools reutiliza
    (deduplica) y enlaza el handler de un logger por nombre de servicio al primer
    flujo con el que se construye; para que cada prueba capture su propia salida
    de forma determinista, se usa un nombre de servicio único por invocación. Esto
    no cambia el contrato observado —el campo ``service`` no se asevera—; lo que la
    prueba verifica es el formateador con redacción y los campos del evento que fija
    el adaptador (``log_type``, identificadores, recuentos, ``purged_at``).
    """
    return get_logger(service=f"audit-test-{next(_audit_logger_seq)}", stream=stream)


def _audit_events(captured: str) -> list[dict[str, object]]:
    """Parsea las líneas JSON de ``purge_completed`` del flujo del logger."""
    events: list[dict[str, object]] = []
    for raw in captured.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("message") == "purge_completed":
            events.append(record)
    return events


def test_record_purge_completed_emits_single_audit_event() -> None:
    """Emite un único evento ``purge_completed`` con los metadatos del contrato (criterio 19.11)."""
    stream = io.StringIO()
    AuditLog(logger=_audit_logger_to(stream)).record_purge_completed(
        principal_sub="a1b2c3d4-sub",
        tenant_id="acme-corp",
        analysis_id=_ANALYSIS_ID,
        correlation_id="corr-1",
        dynamodb_records_deleted=42,
        s3_versions_deleted=7,
        timestamp="2025-06-14T10:12:43+00:00",
    )

    events = _audit_events(stream.getvalue())
    assert len(events) == 1
    event = events[0]
    assert event["log_type"] == "audit"
    assert event["principal_sub"] == "a1b2c3d4-sub"
    assert event["tenant_id"] == "acme-corp"
    assert event["analysis_id"] == _ANALYSIS_ID
    assert event["correlation_id"] == "corr-1"
    assert event["dynamodb_records_deleted"] == 42
    assert event["s3_versions_deleted"] == 7
    # El llamador pasó `timestamp`; el adaptador lo reexpone como `purged_at` para
    # no colisionar con el sello temporal reservado del logger.
    assert event["purged_at"] == "2025-06-14T10:12:43+00:00"


def test_record_purge_completed_has_no_pii() -> None:
    """El evento auditable no contiene PII, aunque llegue por error (criterio 19.11)."""
    stream = io.StringIO()
    # Se pasa PII a propósito para verificar la red de seguridad de redacción.
    AuditLog(logger=_audit_logger_to(stream)).record_purge_completed(
        analysis_id=_ANALYSIS_ID,
        tenant_id="acme-corp",
        respuesta=_SECRET_RESPUESTA,
        email=_SECRET_EMAIL,
    )

    out = stream.getvalue()
    assert _SECRET_RESPUESTA not in out
    assert _SECRET_EMAIL not in out
    # El evento se emite igualmente, con los campos PII enmascarados.
    events = _audit_events(out)
    assert len(events) == 1
    assert events[0]["respuesta"] == "[REDACTED]"
    assert events[0]["email"] == "[REDACTED]"
