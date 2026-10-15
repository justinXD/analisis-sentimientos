"""Pruebas de contrato *end-to-end* de los handlers de la capa de API (tarea 9.6).

A diferencia de :mod:`tests.handlers.test_api_handlers` (tarea 9.1), que sustituye
el servicio de cada handler por un **doble en memoria** para verificar sólo la
lógica de traducción del adaptador, estas pruebas cablean cada handler contra
**AWS simulado** (``moto`` para DynamoDB, S3, SQS y EventBridge) con los
adaptadores reales (``DynamoRepository``, ``S3Storage``, ``SqsPublisher``,
``EventBridgePublisher``, ``AuditLog``) y los servicios de producción. Así se
ejercita el camino completo evento → handler → servicio → adaptador → AWS
simulado, con **solo datos sintéticos** (criterio 17.10).

Cobertura (un test por handler y por código de respuesta):

* ``api_submit``: ``202`` (verifica el objeto en S3, el META en DynamoDB y el
  mensaje en SQS), ``400`` de validación y ``403`` sin claim de Tenant.
* ``api_status``: ``200``; ``404`` para un Lote inexistente y para uno de otro
  Tenant, con cuerpo **byte a byte idéntico** (criterios 2.2, 11.10); ``403`` sin
  claim de Tenant (criterio 2.9).
* ``api_download``: ``200``; ``409`` cuando el Lote no es terminal; ``404`` para
  un Lote inexistente y para uno de otro Tenant, con cuerpo **byte a byte
  idéntico** (criterios 2.2, 12.6).
* ``api_purge``: ``200`` (verifica META + ITEMs + objetos de S3 borrados); ``404``
  de Tenant ajeno con el META del dueño conservado (criterio 19.5); el ``500`` con
  META conservado tras un fallo de S3 y el reintento que completa con ``200``
  (criterios 19.7, 19.8).

El contrato del webhook (``notify_webhook``) se cubre en el paso estable de
**parseo + preparación** (tarea 9.3), sin firma HMAC ni envío HTTP (tarea 9.4):
se verifica que un Evento_Finalizacion válido produce la :class:`WebhookDelivery`
preparada y que un evento malformado se rechaza.

Aislamiento de estado entre casos: los handlers memorizan su servicio en el
singleton de módulo ``_SERVICE``; cada prueba lo reemplaza (``monkeypatch``) por
un servicio recién construido contra los recursos de ``moto`` de ese caso, de modo
que ninguna prueba herede clientes de otra.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from sentiment.adapters.audit_log import AuditLog
from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.adapters.eventbridge_publisher import EventBridgePublisher
from sentiment.adapters.s3_storage import S3Storage
from sentiment.adapters.sqs_publisher import SqsPublisher
from sentiment.domain.ids import new_analysis_id
from sentiment.domain.models import AnalysisMeta, AnalysisStatus, SurveyAnswer
from sentiment.handlers import api_download, api_purge, api_status, api_submit
from sentiment.handlers.notify_webhook import (
    WebhookEventError,
    WebhookTarget,
    parse_event,
    prepare_delivery,
)
from sentiment.services.get_analysis_status import GetAnalysisStatusService
from sentiment.services.issue_download_url import IssueDownloadUrlService
from sentiment.services.purge_analysis import (
    MAP_RESULTS_PREFIX_TEMPLATE,
    PurgeAnalysisService,
)
from sentiment.services.submit_analysis import SubmitAnalysisService

# --------------------------------------------------------------------------- #
# Constantes sintéticas (sin PII real, criterio 17.10)                        #
# --------------------------------------------------------------------------- #

_REGION = "us-east-1"
_TABLE = "sentiment-test-analysis"
_BUCKET = "sentiment-test-analysis-123456789012"
_QUEUE = "sentiment-test-requests"
_EVENT_BUS = "sentiment-test-events"

_TENANT = "acme"
_OTHER_TENANT = "globex"
_SUB = "user-7"
# Valor real del enum cerrado de Indificadores (fuente única en prompts/).
# api_submit valida el cuerpo contra el JSON_Schema, por lo que debe ser válido.
_INDIFICADOR = "Efectividad de Liderazgo"
_NOW = datetime(2025, 6, 14, 10, 12, 43, tzinfo=UTC)
_REQUEST_ID = "req-contract-1"


# --------------------------------------------------------------------------- #
# Infraestructura simulada (moto)                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def aws() -> Iterator[dict[str, Any]]:
    """Crea la single-table, el bucket versionado, la cola y el bus simulados.

    El bucket se crea con versionado activado para ejercitar el borrado de todas
    las versiones en la Purga (criterio 19.4). La tabla usa el mismo esquema de
    claves y GSIs que despliega el ``data_stack`` (tarea 11.2).
    """
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=_REGION)
        dynamodb.create_table(
            TableName=_TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
                {"AttributeName": "gsi2pk", "AttributeType": "S"},
                {"AttributeName": "gsi2sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2",
                    "KeySchema": [
                        {"AttributeName": "gsi2pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )

        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)
        s3.put_bucket_versioning(
            Bucket=_BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )

        sqs = boto3.client("sqs", region_name=_REGION)
        queue_url = sqs.create_queue(QueueName=_QUEUE)["QueueUrl"]

        events = boto3.client("events", region_name=_REGION)
        events.create_event_bus(Name=_EVENT_BUS)

        yield {
            "dynamodb": dynamodb,
            "s3": s3,
            "sqs": sqs,
            "events": events,
            "queue_url": queue_url,
        }


@pytest.fixture
def submit_env(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inyecta las variables de entorno que los publicadores de SQS/EventBridge leen.

    ``SqsPublisher`` y ``EventBridgePublisher`` resuelven ``QUEUE_URL``,
    ``BUCKET_NAME`` y ``EVENT_BUS_NAME`` de forma perezosa en tiempo de publicación
    (criterio 17.7); se fijan aquí a los recursos simulados del caso.
    """
    monkeypatch.setenv("QUEUE_URL", str(aws["queue_url"]))
    monkeypatch.setenv("BUCKET_NAME", _BUCKET)
    monkeypatch.setenv("EVENT_BUS_NAME", _EVENT_BUS)


# --------------------------------------------------------------------------- #
# Fábricas de servicios cableados contra moto                                 #
# --------------------------------------------------------------------------- #


def _repository(aws: dict[str, Any]) -> DynamoRepository:
    return DynamoRepository(client=aws["dynamodb"], table_name=_TABLE)


def _storage(aws: dict[str, Any]) -> S3Storage:
    return S3Storage(client=aws["s3"], bucket_name=_BUCKET)


def _fixed_clock() -> datetime:
    """Reloj determinista para instantes reproducibles en las vistas."""
    return _NOW


# --------------------------------------------------------------------------- #
# Construcción de eventos de API Gateway (integración proxy)                  #
# --------------------------------------------------------------------------- #


def _event(
    *,
    tenant_id: str | None = _TENANT,
    sub: str | None = _SUB,
    path: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: str | None = None,
    request_id: str = _REQUEST_ID,
) -> dict[str, Any]:
    """Arma un evento de API Gateway con los claims verificados del autorizador.

    Un ``tenant_id``/``sub`` en ``None`` omite ese claim, para ejercer el 403 de
    identidad incompleta (criterio 2.9).
    """
    claims: dict[str, Any] = {}
    if tenant_id is not None:
        claims["custom:tenant_id"] = tenant_id
    if sub is not None:
        claims["sub"] = sub
    event: dict[str, Any] = {
        "requestContext": {
            "requestId": request_id,
            "authorizer": {"jwt": {"claims": claims}},
        }
    }
    if path is not None:
        event["pathParameters"] = path
    if query is not None:
        event["queryStringParameters"] = query
    if body is not None:
        event["body"] = body
    return event


# --------------------------------------------------------------------------- #
# Siembra de Lotes sintéticos directamente por el repositorio                 #
# --------------------------------------------------------------------------- #


def _seed_meta(
    repository: DynamoRepository,
    *,
    analysis_id: str,
    tenant_id: str,
    status: AnalysisStatus,
    item_count: int,
    output_s3_key: str | None,
) -> AnalysisMeta:
    """Siembra un Lote (META + ITEMs) y lo cierra en el estado deseado.

    Crea el Lote en ``PENDING`` y, si ``status`` es terminal, lo cierra con
    :meth:`DynamoRepository.finalize_analysis`. Devuelve el META tal como queda
    persistido (releído), para que las aserciones comparen contra la fuente de
    verdad.
    """
    input_key = f"inputs/{tenant_id}/2025/06/14/{analysis_id}.json"
    meta = AnalysisMeta(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        owner_sub=_SUB,
        status=AnalysisStatus.PENDING,
        item_count=item_count,
        input_s3_key=input_key,
        created_at=_NOW,
        updated_at=_NOW,
    )
    answers = [
        SurveyAnswer(indificador=_INDIFICADOR, respuesta=f"sintetica {i}", index=i)
        for i in range(item_count)
    ]
    repository.create_analysis(meta, answers)
    if status in {
        AnalysisStatus.COMPLETED,
        AnalysisStatus.COMPLETED_WITH_ERRORS,
        AnalysisStatus.FAILED,
        AnalysisStatus.EXPIRED,
    }:
        repository.finalize_analysis(
            analysis_id, status, (item_count, 0, 0), output_s3_key
        )
    return repository.get_meta(analysis_id, tenant_id)


# =========================================================================== #
# api_submit                                                                  #
# =========================================================================== #


def test_submit_contract_accepts_and_persists_everything(
    aws: dict[str, Any], submit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """202: el objeto de S3, el META de DynamoDB y el mensaje de SQS quedan escritos.

    Verifica el camino completo de ingesta sobre AWS simulado: el handler escribe
    el payload canónico en S3, siembra el META en DynamoDB y encola el mensaje
    ``AnalysisQueued`` en SQS (orden S3 → DynamoDB → SQS, criterios 4.1, 4.3, 4.8).
    """
    analysis_id = new_analysis_id()
    service = SubmitAnalysisService(
        repository=_repository(aws),
        storage=_storage(aws),
        queue=SqsPublisher(client=aws["sqs"]),
        events=EventBridgePublisher(client=aws["events"]),
        clock=_fixed_clock,
        id_factory=lambda: analysis_id,
    )
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    body = json.dumps(
        {"items": [{"indificador": _INDIFICADOR, "respuesta": "excelente servicio"}]}
    )
    response = api_submit.handler(_event(body=body), None)

    assert response["statusCode"] == 202
    payload = json.loads(response["body"])
    assert payload["analysis_id"] == analysis_id
    assert payload["status"] == "PENDING"
    assert payload["item_count"] == 1

    # S3: el objeto de entrada existe con el payload canónico (sólo contrato).
    input_key = f"inputs/{_TENANT}/2025/06/14/{analysis_id}.json"
    stored = aws["s3"].get_object(Bucket=_BUCKET, Key=input_key)
    assert json.loads(stored["Body"].read()) == {
        "items": [{"indificador": _INDIFICADOR, "respuesta": "excelente servicio"}]
    }

    # DynamoDB: el META quedó sembrado en PENDING con el item_count correcto.
    meta = _repository(aws).get_meta(analysis_id, _TENANT)
    assert meta.status is AnalysisStatus.PENDING
    assert meta.item_count == 1
    assert meta.input_s3_key == input_key

    # SQS: hay exactamente un mensaje y referencia el Lote, sin PII.
    messages = aws["sqs"].receive_message(
        QueueUrl=aws["queue_url"], MaxNumberOfMessages=10
    )
    bodies = [json.loads(m["Body"]) for m in messages.get("Messages", [])]
    assert len(bodies) == 1
    queued = bodies[0]
    assert queued["analysis_id"] == analysis_id
    assert queued["tenant_id"] == _TENANT
    assert queued["item_count"] == 1
    assert "respuesta" not in response["body"]


def test_submit_contract_validation_is_400_without_writes(
    aws: dict[str, Any], submit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """400: un cuerpo malformado se rechaza como VALIDATION_FAILED sin escribir nada.

    La validación total precede a cualquier escritura (criterio 3.13): tras el
    rechazo no debe quedar ningún objeto en S3 ni ningún mensaje en SQS.
    """
    service = SubmitAnalysisService(
        repository=_repository(aws),
        storage=_storage(aws),
        queue=SqsPublisher(client=aws["sqs"]),
        events=EventBridgePublisher(client=aws["events"]),
        clock=_fixed_clock,
        id_factory=new_analysis_id,
    )
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    response = api_submit.handler(_event(body="not valid json"), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error_code"] == "VALIDATION_FAILED"

    listed = aws["s3"].list_objects_v2(Bucket=_BUCKET)
    assert listed.get("KeyCount", 0) == 0
    messages = aws["sqs"].receive_message(QueueUrl=aws["queue_url"])
    assert messages.get("Messages", []) == []


def test_submit_contract_forbidden_without_tenant_claim(
    aws: dict[str, Any], submit_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """403: sin claim custom:tenant_id no se toca S3/DynamoDB/SQS (criterio 2.9)."""
    service = SubmitAnalysisService(
        repository=_repository(aws),
        storage=_storage(aws),
        queue=SqsPublisher(client=aws["sqs"]),
        events=EventBridgePublisher(client=aws["events"]),
        clock=_fixed_clock,
        id_factory=new_analysis_id,
    )
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    body = json.dumps({"items": [{"indificador": _INDIFICADOR, "respuesta": "x"}]})
    response = api_submit.handler(_event(tenant_id=None, body=body), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error_code"] == "FORBIDDEN"
    listed = aws["s3"].list_objects_v2(Bucket=_BUCKET)
    assert listed.get("KeyCount", 0) == 0


# =========================================================================== #
# api_status                                                                  #
# =========================================================================== #


def test_status_contract_returns_200_for_own_batch(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """200: el estado de un Lote propio se sirve con contadores y progreso reales."""
    repository = _repository(aws)
    analysis_id = new_analysis_id()
    _seed_meta(
        repository,
        analysis_id=analysis_id,
        tenant_id=_TENANT,
        status=AnalysisStatus.COMPLETED,
        item_count=3,
        output_s3_key=f"outputs/{_TENANT}/2025/06/14/{analysis_id}.json",
    )
    monkeypatch.setattr(
        api_status, "_SERVICE", GetAnalysisStatusService(repository=repository)
    )

    response = api_status.handler(
        _event(path={"analysis_id": analysis_id}), None
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["analysis_id"] == analysis_id
    assert payload["status"] == "COMPLETED"
    assert payload["item_count"] == 3
    assert payload["progress"] == 1.0
    assert payload["download_available"] is True


def test_status_contract_404_identical_for_missing_and_foreign(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 byte a byte idéntico para un Lote inexistente y para uno ajeno (2.2, 11.10).

    Se siembra un Lote de ``_OTHER_TENANT`` y se consulta con el token de
    ``_TENANT``: la respuesta debe ser indistinguible de la de un ``analysis_id``
    que no existe, cerrando el vector de enumeración entre Tenants.
    """
    repository = _repository(aws)
    foreign_id = new_analysis_id()
    _seed_meta(
        repository,
        analysis_id=foreign_id,
        tenant_id=_OTHER_TENANT,
        status=AnalysisStatus.COMPLETED,
        item_count=2,
        output_s3_key=f"outputs/{_OTHER_TENANT}/2025/06/14/{foreign_id}.json",
    )
    monkeypatch.setattr(
        api_status, "_SERVICE", GetAnalysisStatusService(repository=repository)
    )

    missing_id = new_analysis_id()
    missing = api_status.handler(_event(path={"analysis_id": missing_id}), None)
    foreign = api_status.handler(_event(path={"analysis_id": foreign_id}), None)

    assert missing["statusCode"] == 404
    assert foreign["statusCode"] == 404
    # Mismo cuerpo byte a byte: el correlation_id es el mismo request_id en ambos.
    assert missing["body"] == foreign["body"]
    assert json.loads(missing["body"]) == {
        "error_code": "NOT_FOUND",
        "correlation_id": _REQUEST_ID,
    }


def test_status_contract_forbidden_without_tenant_claim(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """403: sin claim custom:tenant_id no se lee la Dynamo_Table (criterio 2.9)."""
    monkeypatch.setattr(
        api_status, "_SERVICE", GetAnalysisStatusService(repository=_repository(aws))
    )

    response = api_status.handler(
        _event(tenant_id=None, path={"analysis_id": new_analysis_id()}), None
    )

    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error_code"] == "FORBIDDEN"


# =========================================================================== #
# api_download                                                                #
# =========================================================================== #


def test_download_contract_returns_presigned_url_for_terminal_batch(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """200: un Lote terminal con Documento_Consolidado emite la Presigned_URL (12.1)."""
    repository = _repository(aws)
    analysis_id = new_analysis_id()
    output_key = f"outputs/{_TENANT}/2025/06/14/{analysis_id}.json"
    _seed_meta(
        repository,
        analysis_id=analysis_id,
        tenant_id=_TENANT,
        status=AnalysisStatus.COMPLETED,
        item_count=1,
        output_s3_key=output_key,
    )
    # El Documento_Consolidado debe existir para que issue() lo firme (12.5).
    aws["s3"].put_object(
        Bucket=_BUCKET, Key=output_key, Body=b'{"items": []}'
    )
    monkeypatch.setattr(
        api_download,
        "_SERVICE",
        IssueDownloadUrlService(
            repository=repository, storage=_storage(aws), clock=_fixed_clock
        ),
    )

    response = api_download.handler(
        _event(path={"analysis_id": analysis_id}), None
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["analysis_id"] == analysis_id
    assert payload["status"] == "COMPLETED"
    assert payload["download_url"].startswith("https://")
    assert output_key in payload["download_url"]


def test_download_contract_409_when_not_terminal(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """409: un Lote aún en PENDING no permite descargar (criterios 12.3, 12.4)."""
    repository = _repository(aws)
    analysis_id = new_analysis_id()
    _seed_meta(
        repository,
        analysis_id=analysis_id,
        tenant_id=_TENANT,
        status=AnalysisStatus.PENDING,
        item_count=2,
        output_s3_key=None,
    )
    monkeypatch.setattr(
        api_download,
        "_SERVICE",
        IssueDownloadUrlService(
            repository=repository, storage=_storage(aws), clock=_fixed_clock
        ),
    )

    response = api_download.handler(
        _event(path={"analysis_id": analysis_id}), None
    )

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["error_code"] == "ANALYSIS_NOT_READY"


def test_download_contract_404_identical_for_missing_and_foreign(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 byte a byte idéntico para un Lote inexistente y para uno ajeno (2.2, 12.6)."""
    repository = _repository(aws)
    foreign_id = new_analysis_id()
    foreign_key = f"outputs/{_OTHER_TENANT}/2025/06/14/{foreign_id}.json"
    _seed_meta(
        repository,
        analysis_id=foreign_id,
        tenant_id=_OTHER_TENANT,
        status=AnalysisStatus.COMPLETED,
        item_count=1,
        output_s3_key=foreign_key,
    )
    aws["s3"].put_object(Bucket=_BUCKET, Key=foreign_key, Body=b"{}")
    monkeypatch.setattr(
        api_download,
        "_SERVICE",
        IssueDownloadUrlService(
            repository=repository, storage=_storage(aws), clock=_fixed_clock
        ),
    )

    missing_id = new_analysis_id()
    missing = api_download.handler(_event(path={"analysis_id": missing_id}), None)
    foreign = api_download.handler(_event(path={"analysis_id": foreign_id}), None)

    assert missing["statusCode"] == 404
    assert foreign["statusCode"] == 404
    assert missing["body"] == foreign["body"]
    assert json.loads(missing["body"]) == {
        "error_code": "NOT_FOUND",
        "correlation_id": _REQUEST_ID,
    }


# =========================================================================== #
# api_purge                                                                   #
# =========================================================================== #


def _purge_service(
    aws: dict[str, Any], storage: S3Storage | Any
) -> PurgeAnalysisService:
    return PurgeAnalysisService(
        repository=_repository(aws),
        storage=storage,
        audit=AuditLog(),
        clock=_fixed_clock,
    )


def _dynamo_item_count(aws: dict[str, Any], analysis_id: str, tenant_id: str) -> int:
    """Cuenta los registros (META + ITEMs) de un Lote en la tabla simulada."""
    pk = f"TENANT#{tenant_id}#ANALYSIS#{analysis_id}"
    response = aws["dynamodb"].query(
        TableName=_TABLE,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": pk}},
    )
    return len(response.get("Items", []))


def test_purge_contract_deletes_meta_items_and_objects(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """200: la Purga borra el META, los ITEMs y los objetos de S3 del Lote (19.3, 19.4)."""
    repository = _repository(aws)
    analysis_id = new_analysis_id()
    input_key = f"inputs/{_TENANT}/2025/06/14/{analysis_id}.json"
    output_key = f"outputs/{_TENANT}/2025/06/14/{analysis_id}.json"
    _seed_meta(
        repository,
        analysis_id=analysis_id,
        tenant_id=_TENANT,
        status=AnalysisStatus.COMPLETED,
        item_count=3,
        output_s3_key=output_key,
    )
    aws["s3"].put_object(Bucket=_BUCKET, Key=input_key, Body=b"{}")
    aws["s3"].put_object(Bucket=_BUCKET, Key=output_key, Body=b"{}")
    map_key = MAP_RESULTS_PREFIX_TEMPLATE.format(analysis_id=analysis_id) + "r0.json"
    aws["s3"].put_object(Bucket=_BUCKET, Key=map_key, Body=b"{}")

    assert _dynamo_item_count(aws, analysis_id, _TENANT) == 4  # META + 3 ITEMs
    monkeypatch.setattr(api_purge, "_SERVICE", _purge_service(aws, _storage(aws)))

    response = api_purge.handler(_event(path={"analysis_id": analysis_id}), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["analysis_id"] == analysis_id
    # DynamoDB: no queda ningún registro del Lote.
    assert _dynamo_item_count(aws, analysis_id, _TENANT) == 0
    # S3: no queda ninguna versión viva ni marcador bajo las claves del Lote.
    versions = aws["s3"].list_object_versions(Bucket=_BUCKET)
    assert versions.get("Versions", []) == []
    assert versions.get("DeleteMarkers", []) == []


def test_purge_contract_404_foreign_tenant_preserves_owner_meta(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 para un Tenant ajeno; el META del dueño se conserva intacto (criterio 19.5)."""
    repository = _repository(aws)
    analysis_id = new_analysis_id()
    _seed_meta(
        repository,
        analysis_id=analysis_id,
        tenant_id=_OTHER_TENANT,
        status=AnalysisStatus.COMPLETED,
        item_count=2,
        output_s3_key=f"outputs/{_OTHER_TENANT}/2025/06/14/{analysis_id}.json",
    )
    before = _dynamo_item_count(aws, analysis_id, _OTHER_TENANT)
    monkeypatch.setattr(api_purge, "_SERVICE", _purge_service(aws, _storage(aws)))

    # El token es de _TENANT, pero el Lote es de _OTHER_TENANT.
    response = api_purge.handler(_event(path={"analysis_id": analysis_id}), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"])["error_code"] == "NOT_FOUND"
    # El META (y los ITEMs) del dueño legítimo siguen existiendo sin tocar.
    assert _dynamo_item_count(aws, analysis_id, _OTHER_TENANT) == before
    assert repository.get_meta(analysis_id, _OTHER_TENANT).analysis_id == analysis_id


class _FlakyPurgeStorage:
    """Doble de :class:`S3Storage` que falla el primer borrado y luego completa.

    Envuelve el adaptador real de S3: la **primera** llamada a
    ``delete_all_versions`` lanza una excepción (simula un fallo transitorio de
    S3 a mitad de la Purga), y la **segunda** delega en el adaptador real, que
    completa el borrado. Permite ejercer el contrato de los criterios 19.7
    (el META se conserva tras el 500) y 19.8 (la Purga es reintentable).
    """

    def __init__(self, inner: S3Storage) -> None:
        self._inner = inner
        self.calls = 0

    def delete_all_versions(self, exact_keys: list[str], prefix: str) -> int:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("fallo simulado de S3 durante la purga")
        return self._inner.delete_all_versions(exact_keys=exact_keys, prefix=prefix)


def test_purge_contract_500_preserves_meta_then_retry_completes(
    aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 con META conservado y reintento que completa con 200 (criterios 19.7, 19.8).

    Primera Purga: el borrado de S3 falla, el handler responde 500 INTERNAL_ERROR
    y, como el META se borra en último lugar, el Lote sigue direccionable. Segunda
    Purga (reintento): el borrado de S3 completa y el handler responde 200, dejando
    el Lote sin META, sin ITEMs y sin objetos.
    """
    repository = _repository(aws)
    analysis_id = new_analysis_id()
    input_key = f"inputs/{_TENANT}/2025/06/14/{analysis_id}.json"
    output_key = f"outputs/{_TENANT}/2025/06/14/{analysis_id}.json"
    _seed_meta(
        repository,
        analysis_id=analysis_id,
        tenant_id=_TENANT,
        status=AnalysisStatus.COMPLETED,
        item_count=2,
        output_s3_key=output_key,
    )
    aws["s3"].put_object(Bucket=_BUCKET, Key=input_key, Body=b"{}")
    aws["s3"].put_object(Bucket=_BUCKET, Key=output_key, Body=b"{}")

    flaky = _FlakyPurgeStorage(_storage(aws))
    monkeypatch.setattr(api_purge, "_SERVICE", _purge_service(aws, flaky))

    # 1) Primera purga: el fallo de S3 produce 500 y conserva el META (19.7).
    first = api_purge.handler(_event(path={"analysis_id": analysis_id}), None)
    assert first["statusCode"] == 500
    assert json.loads(first["body"]) == {
        "error_code": "INTERNAL_ERROR",
        "correlation_id": _REQUEST_ID,
    }
    # El META sobrevive: el Lote sigue direccionable para el reintento (19.8).
    assert repository.get_meta(analysis_id, _TENANT).analysis_id == analysis_id

    # 2) Reintento: ahora el borrado de S3 completa y la purga termina con 200.
    second = api_purge.handler(_event(path={"analysis_id": analysis_id}), None)
    assert second["statusCode"] == 200
    assert flaky.calls == 2
    assert _dynamo_item_count(aws, analysis_id, _TENANT) == 0
    versions = aws["s3"].list_object_versions(Bucket=_BUCKET)
    assert versions.get("Versions", []) == []
    assert versions.get("DeleteMarkers", []) == []


# =========================================================================== #
# notify_webhook: contrato del paso estable de parseo + preparación           #
# =========================================================================== #
#
# El envío HTTPS firmado con HMAC es la tarea 9.4; para no acoplar estas pruebas
# a esa capa (que puede estar aterrizando en paralelo y abre conexiones de red),
# el contrato del webhook se ejerce sobre las funciones puras y estables
# `parse_event` y `prepare_delivery`, no sobre `lambda_handler`. Son el *seam*
# que produce el artefacto preparado de la tarea 9.3 y sobre el que 9.4 construye.


def _completed_event() -> dict[str, Any]:
    """Evento ``analysis.completed`` sintético, sin PII (contrato 13.3)."""
    detail = {
        "event_id": f"{new_analysis_id()}:COMPLETED",
        "analysis_id": new_analysis_id(),
        "tenant_id": _TENANT,
        "owner_sub": _SUB,
        "status": "COMPLETED",
        "item_count": 2,
        "done_count": 2,
        "failed_count": 0,
        "output_s3_key": f"outputs/{_TENANT}/2025/06/14/x.json",
        "started_at": "2025-06-14T10:00:00+00:00",
        "completed_at": "2025-06-14T10:12:43+00:00",
        "duration_seconds": 763,
    }
    return {
        "source": "sentiment.pipeline",
        "detail-type": "analysis.completed",
        "detail": detail,
    }


def test_webhook_contract_prepares_delivery_for_valid_event() -> None:
    """El paso estable de parseo + preparación produce la entrega lista (tarea 9.3).

    Verifica el contrato *sin* firma HMAC ni envío HTTP (eso es la tarea 9.4): el
    ``detail`` se parsea a la vista tipada y :func:`prepare_delivery` arma un POST
    HTTPS cuyo cuerpo es el ``detail`` serializado byte a byte, con las cabeceras
    base y sin cabeceras de firma todavía.
    """
    event = _completed_event()
    detail = event["detail"]
    assert isinstance(detail, dict)

    parsed = parse_event(event)
    assert parsed.tenant_id == _TENANT
    assert parsed.analysis_id == detail["analysis_id"]

    target = WebhookTarget(
        url="https://cliente.ejemplo.com/hooks", secret_id="arn:secret"
    )
    delivery = prepare_delivery(parsed, target)

    assert delivery.method == "POST"
    assert delivery.url == target.url
    assert delivery.headers == {"Content-Type": "application/json"}
    # El cuerpo es el detail completo; sin firma todavía (seam de la tarea 9.4).
    assert json.loads(delivery.body) == detail
    assert "X-Signature" not in delivery.headers
    # Sin PII en el cuerpo preparado (contrato del evento, criterio 13.3).
    for pii_field in ("respuesta", "explicacion", "email"):
        assert pii_field not in json.loads(delivery.body)


def test_webhook_contract_rejects_malformed_event() -> None:
    """Un evento sin un detail válido se rechaza como WebhookEventError (tarea 9.3)."""
    with pytest.raises(WebhookEventError):
        parse_event({"detail-type": "analysis.completed", "detail": {}})
