"""Pruebas de contrato de los publicadores de SQS y EventBridge (tarea 6.6).

Verifican el contrato de los dos adaptadores de salida de la ingesta y de la
finalización, usando AWS simulado (``moto``) y datos sintéticos:

* ``SqsPublisher.publish_analysis_queued``: encola **exactamente un** mensaje con
  los punteros y metadatos del Lote (``analysis_id``, ``tenant_id``, bucket/clave
  del input, ``item_count``, ``correlation_id``) y **sin PII** (criterio 4.8).
* ``EventBridgePublisher.publish_analysis_completed``: publica un evento con
  ``detail-type = analysis.completed``, ``event_id = {analysis_id}:{status}``
  (criterios 13.1, 10.20) y un detalle **sin PII ni Presigned_URL** (criterio
  13.3).

Como EventBridge no entrega a ningún consumidor observable en ``moto`` sin una
regla + destino, el detalle del evento se verifica enrutándolo a una cola SQS
mediante una regla con un ``InputPath`` que reenvía el evento completo. Esto
ejercita el camino real de ``PutEvents`` y permite inspeccionar lo publicado.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from sentiment.adapters.eventbridge_publisher import (
    EventBridgePublisher,
    EventPublishError,
)
from sentiment.adapters.sqs_publisher import SqsPublisher
from sentiment.domain.models import AnalysisMeta, AnalysisStatus
from sentiment.observability.config import ConfigError

_REGION = "us-east-1"

# PII sintética que NUNCA debe aparecer en un mensaje ni en un evento.
_SECRET_RESPUESTA = "me siento comodo pero quisiera mejor salario"
_SECRET_EXPLICACION = "el empleado expresa satisfaccion con reservas salariales"
_SECRET_EMAIL = "rh@ejemplo.com"


@pytest.fixture
def _aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Activa el simulador de AWS y fija la región para toda la prueba."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield


def _make_meta(status: AnalysisStatus = AnalysisStatus.PENDING) -> AnalysisMeta:
    """Construye un META sintético con marcas de tiempo deterministas."""
    created = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
    completed = datetime(2025, 6, 14, 10, 12, 43, tzinfo=UTC)
    return AnalysisMeta(
        analysis_id="01JDQZ8F3K7XWQ2M5N8P4R6T9V",
        tenant_id="acme-corp",
        owner_sub="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        status=status,
        item_count=2,
        done_count=1,
        failed_count=1,
        skipped_count=0,
        input_s3_key="inputs/acme-corp/2025/06/14/01JDQZ8F3K7XWQ2M5N8P4R6T9V.json",
        output_s3_key="outputs/acme-corp/2025/06/14/01JDQZ8F3K7XWQ2M5N8P4R6T9V.json",
        created_at=created,
        updated_at=completed,
        completed_at=completed,
    )


# ===========================================================================
# SqsPublisher
# ===========================================================================


def _create_queue() -> str:
    """Crea una cola SQS de prueba y devuelve su URL."""
    client = boto3.client("sqs", region_name=_REGION)
    url = client.create_queue(QueueName="analysis-requests")["QueueUrl"]
    assert isinstance(url, str)
    return url


def test_sqs_publishes_exactly_one_message_with_pointers(
    _aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Encola un único mensaje con los punteros y metadatos exigidos (criterio 4.8)."""
    client = boto3.client("sqs", region_name=_REGION)
    queue_url = _create_queue()
    monkeypatch.setenv("QUEUE_URL", queue_url)
    monkeypatch.setenv("BUCKET_NAME", "sentiment-dev-analysis-123456789012")

    meta = _make_meta()
    publisher = SqsPublisher(client=client)
    publisher.publish_analysis_queued(meta, correlation_id="7f3c1e2a-corr")

    received = client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
    messages = received.get("Messages", [])
    assert len(messages) == 1

    body = json.loads(messages[0]["Body"])
    assert body["analysis_id"] == meta.analysis_id
    assert body["tenant_id"] == meta.tenant_id
    assert body["owner_sub"] == meta.owner_sub
    assert body["input_s3_bucket"] == "sentiment-dev-analysis-123456789012"
    assert body["input_s3_key"] == meta.input_s3_key
    assert body["item_count"] == meta.item_count
    assert body["correlation_id"] == "7f3c1e2a-corr"
    assert body["schema_version"] == 1


def test_sqs_message_contains_no_pii(
    _aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El cuerpo del mensaje no transporta texto de respuesta ni correo (sin PII)."""
    client = boto3.client("sqs", region_name=_REGION)
    queue_url = _create_queue()
    monkeypatch.setenv("QUEUE_URL", queue_url)
    monkeypatch.setenv("BUCKET_NAME", "sentiment-dev-analysis-123456789012")

    publisher = SqsPublisher(client=client)
    publisher.publish_analysis_queued(_make_meta(), correlation_id="corr-1")

    raw_body = client.receive_message(QueueUrl=queue_url)["Messages"][0]["Body"]
    assert _SECRET_RESPUESTA not in raw_body
    assert _SECRET_EXPLICACION not in raw_body
    assert _SECRET_EMAIL not in raw_body


def test_sqs_missing_queue_url_raises_config_error(
    _aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin ``QUEUE_URL`` configurada, la publicación falla con mensaje accionable."""
    monkeypatch.delenv("QUEUE_URL", raising=False)
    monkeypatch.setenv("BUCKET_NAME", "sentiment-dev-analysis-123456789012")
    client = boto3.client("sqs", region_name=_REGION)

    publisher = SqsPublisher(client=client)
    with pytest.raises(ConfigError) as excinfo:
        publisher.publish_analysis_queued(_make_meta(), correlation_id="corr-1")
    assert "QUEUE_URL" in str(excinfo.value)


def test_sqs_missing_bucket_name_raises_config_error(
    _aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin ``BUCKET_NAME`` configurada, la publicación falla con mensaje accionable."""
    client = boto3.client("sqs", region_name=_REGION)
    queue_url = _create_queue()
    monkeypatch.setenv("QUEUE_URL", queue_url)
    monkeypatch.delenv("BUCKET_NAME", raising=False)

    publisher = SqsPublisher(client=client)
    with pytest.raises(ConfigError) as excinfo:
        publisher.publish_analysis_queued(_make_meta(), correlation_id="corr-1")
    assert "BUCKET_NAME" in str(excinfo.value)


# ===========================================================================
# EventBridgePublisher
#
# El enrutado de EventBridge → SQS de ``moto`` depende del paquete opcional
# ``jsonpath_ng``, que no está en el conjunto pineado del proyecto. Para no
# acoplar la prueba a esa dependencia y, a la vez, aseverar con precisión EL
# contrato de lo que el adaptador publica, se usa un cliente espía que registra
# las ``Entries`` pasadas a ``put_events``. Esto ejercita el camino real del
# adaptador (construcción del detalle, serialización, inspección de la respuesta)
# y permite verificar byte a byte el detalle emitido.
# ===========================================================================


class _RecordingEventsClient:
    """Cliente de EventBridge de prueba que registra las entradas publicadas.

    Imita la respuesta de ``PutEvents`` sin fallos de entrada y conserva las
    ``Entries`` recibidas para que la prueba inspeccione el contrato publicado.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def put_events(
        self, *, Entries: Sequence[dict[str, str]]  # noqa: N803
    ) -> dict[str, object]:
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "evt-1"}]}


def test_eventbridge_publishes_analysis_completed_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publica un evento ``analysis.completed`` con el detalle y ``event_id`` correctos."""
    monkeypatch.setenv("EVENT_BUS_NAME", "sentiment-pipeline")
    client = _RecordingEventsClient()
    meta = _make_meta(status=AnalysisStatus.COMPLETED_WITH_ERRORS)

    EventBridgePublisher(client=client).publish_analysis_completed(meta)

    assert len(client.entries) == 1
    entry = client.entries[0]
    assert entry["Source"] == "sentiment.pipeline"
    assert entry["DetailType"] == "analysis.completed"
    assert entry["EventBusName"] == "sentiment-pipeline"

    detail = json.loads(entry["Detail"])
    assert detail["event_id"] == f"{meta.analysis_id}:COMPLETED_WITH_ERRORS"
    assert detail["analysis_id"] == meta.analysis_id
    assert detail["tenant_id"] == meta.tenant_id
    assert detail["owner_sub"] == meta.owner_sub
    assert detail["status"] == "COMPLETED_WITH_ERRORS"
    assert detail["item_count"] == 2
    assert detail["done_count"] == 1
    assert detail["failed_count"] == 1
    assert detail["output_s3_key"] == meta.output_s3_key
    assert detail["started_at"] == "2025-06-14T10:00:00+00:00"
    assert detail["completed_at"] == "2025-06-14T10:12:43+00:00"
    assert detail["duration_seconds"] == 763


def test_eventbridge_event_id_is_deterministic_per_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``event_id`` es ``{analysis_id}:{status}`` y cambia con el status (criterio 10.20)."""
    monkeypatch.setenv("EVENT_BUS_NAME", "sentiment-pipeline")
    client = _RecordingEventsClient()
    publisher = EventBridgePublisher(client=client)

    completed = _make_meta(status=AnalysisStatus.COMPLETED)
    failed = _make_meta(status=AnalysisStatus.FAILED)
    publisher.publish_analysis_completed(completed)
    publisher.publish_analysis_completed(failed)

    event_ids = [json.loads(e["Detail"])["event_id"] for e in client.entries]
    assert event_ids == [
        f"{completed.analysis_id}:COMPLETED",
        f"{failed.analysis_id}:FAILED",
    ]


def test_eventbridge_detail_contains_no_pii_or_presigned_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El detalle no contiene PII ni Presigned_URL (criterio 13.3)."""
    monkeypatch.setenv("EVENT_BUS_NAME", "sentiment-pipeline")
    client = _RecordingEventsClient()

    EventBridgePublisher(client=client).publish_analysis_completed(
        _make_meta(status=AnalysisStatus.COMPLETED)
    )

    raw = client.entries[0]["Detail"]
    assert _SECRET_RESPUESTA not in raw
    assert _SECRET_EXPLICACION not in raw
    assert _SECRET_EMAIL not in raw
    # Un Presigned_URL de S3 lleva siempre estos parámetros de firma; su ausencia
    # confirma que el detalle transporta solo el puntero `output_s3_key`.
    assert "X-Amz-Signature" not in raw
    assert "X-Amz-Credential" not in raw


def test_eventbridge_missing_bus_name_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin ``EVENT_BUS_NAME`` configurada, la publicación falla con mensaje accionable."""
    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)
    publisher = EventBridgePublisher(client=_RecordingEventsClient())
    with pytest.raises(ConfigError) as excinfo:
        publisher.publish_analysis_completed(_make_meta())
    assert "EVENT_BUS_NAME" in str(excinfo.value)


def test_eventbridge_raises_on_failed_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si ``PutEvents`` reporta una entrada fallida, se propaga ``EventPublishError``."""
    monkeypatch.setenv("EVENT_BUS_NAME", "sentiment-pipeline")

    class _FailingClient:
        def put_events(
            self, *, Entries: Sequence[dict[str, str]]  # noqa: N803
        ) -> dict[str, object]:
            return {
                "FailedEntryCount": 1,
                "Entries": [{"ErrorCode": "InternalException"}],
            }

    publisher = EventBridgePublisher(client=_FailingClient())
    with pytest.raises(EventPublishError):
        publisher.publish_analysis_completed(_make_meta())
