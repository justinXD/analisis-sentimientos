"""Pruebas de transporte de los handlers de la capa de API (tarea 9.1).

Verifican los cuatro adaptadores thin (``api_submit``, ``api_status``,
``api_download``, ``api_purge``) sustituyendo su servicio de módulo por un
**doble en memoria** (fake), igual que el resto de pruebas de handlers del
paquete. No tocan AWS ni requieren configuración de entorno: las pruebas de
contrato con AWS simulado son la tarea 9.6. Aquí se cubre la lógica de traducción
del propio adaptador: identidad desde el autorizador (403), ``correlation_id``,
enrutado, serialización de las vistas y traducción de ``SentimentError`` →
código HTTP + cuerpo sin PII (criterios 1.14, 2.9, 3.14, 3.15, 11.1, 12.1, 15.5,
19.1).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from sentiment.domain.errors import NotFoundError, NotReadyError, ValidationError
from sentiment.domain.models import AnalysisMeta, AnalysisStatus
from sentiment.handlers import api_download, api_purge, api_status, api_submit
from sentiment.services.get_analysis_status import (
    AnalysisListView,
    AnalysisSummaryView,
    FailureView,
    StatusView,
)
from sentiment.services.issue_download_url import DownloadView
from sentiment.services.purge_analysis import PurgeResult

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_NOW = datetime(2025, 6, 14, 10, 12, 43, tzinfo=UTC)


def _event(
    *,
    claims: dict[str, Any] | None = None,
    path: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: str | None = None,
    request_id: str = "req-123",
) -> dict[str, Any]:
    """Construye un evento de API Gateway (integración proxy) con identidad válida."""
    if claims is None:
        claims = {"custom:tenant_id": "acme", "sub": "user-7"}
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


class _FakeService:
    """Doble genérico: registra la última llamada y devuelve/lanza lo configurado."""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


# ===========================================================================
# api_submit
# ===========================================================================


class _FakeSubmitService(_FakeService):
    def execute(self, command: Any) -> Any:
        return self._invoke(command)


def _meta() -> AnalysisMeta:
    return AnalysisMeta(
        analysis_id=_ANALYSIS_ID,
        tenant_id="acme",
        owner_sub="user-7",
        status=AnalysisStatus.PENDING,
        item_count=3,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_submit_accepts_and_returns_202(monkeypatch: pytest.MonkeyPatch) -> None:
    """Éxito: 202 con analysis_id, status PENDING, item_count, created_at y status_url (4.9)."""
    service = _FakeSubmitService(result=_meta())
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    response = api_submit.handler(_event(body='{"items": []}'), None)

    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert body["analysis_id"] == _ANALYSIS_ID
    assert body["status"] == "PENDING"
    assert body["item_count"] == 3
    assert body["created_at"] == "2025-06-14T10:12:43+00:00"
    assert body["status_url"] == f"/v1/analyses/{_ANALYSIS_ID}"


def test_submit_builds_command_with_identity_from_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El comando toma tenant_id/owner_sub del token y el cuerpo crudo en bytes (1.14)."""
    service = _FakeSubmitService(result=_meta())
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    api_submit.handler(_event(body='{"items": []}'), None)

    (command,), _ = service.calls[0]
    assert command.tenant_id == "acme"
    assert command.owner_sub == "user-7"
    assert command.raw_body == b'{"items": []}'
    assert command.correlation_id == "req-123"


def test_submit_forbidden_without_tenant_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin custom:tenant_id → 403 FORBIDDEN sin invocar el servicio (2.9)."""
    service = _FakeSubmitService(result=_meta())
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    response = api_submit.handler(_event(claims={"sub": "user-7"}, body="{}"), None)

    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error_code"] == "FORBIDDEN"
    assert service.calls == []  # no se tocó el servicio


def test_submit_translates_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un SentimentError de validación → 400 con error_code + correlation_id (3.14)."""
    service = _FakeSubmitService(error=ValidationError("x", internal="secreto"))
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    response = api_submit.handler(_event(body="not json"), None)

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body == {"error_code": "VALIDATION_FAILED", "correlation_id": "req-123"}
    assert "secreto" not in response["body"]


def test_submit_unexpected_error_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fallo de infraestructura no previsto → 500 INTERNAL_ERROR genérico (15.5)."""
    service = _FakeSubmitService(error=RuntimeError("boom traceback"))
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    response = api_submit.handler(_event(body="{}"), None)

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body == {"error_code": "INTERNAL_ERROR", "correlation_id": "req-123"}
    assert "boom traceback" not in response["body"]


def test_submit_generates_correlation_id_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin requestId se genera un correlation_id UUIDv4 en la respuesta de error (3.15)."""
    service = _FakeSubmitService(error=ValidationError("x"))
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    event = _event(body="{}")
    del event["requestContext"]["requestId"]
    response = api_submit.handler(event, None)

    correlation_id = json.loads(response["body"])["correlation_id"]
    import uuid

    assert uuid.UUID(correlation_id).version == 4


def test_submit_decodes_base64_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un cuerpo base64 se decodifica a los bytes crudos originales."""
    import base64

    service = _FakeSubmitService(result=_meta())
    monkeypatch.setattr(api_submit, "_SERVICE", service)

    raw = b'{"items": [1]}'
    event = _event(body=base64.b64encode(raw).decode("ascii"))
    event["isBase64Encoded"] = True
    api_submit.handler(event, None)

    (command,), _ = service.calls[0]
    assert command.raw_body == raw


# ===========================================================================
# api_status
# ===========================================================================


class _FakeStatusService(_FakeService):
    def __init__(
        self,
        *,
        status_result: Any = None,
        list_result: Any = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(error=error)
        self.status_result = status_result
        self.list_result = list_result

    def get_status(
        self, analysis_id: str, tenant_id: str, *, include_failures: bool = False
    ) -> Any:
        self.calls.append(((analysis_id, tenant_id), {"include_failures": include_failures}))
        if self.error is not None:
            raise self.error
        return self.status_result

    def list_analyses(
        self, tenant_id: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> Any:
        self.calls.append(((tenant_id,), {"page_size": page_size, "cursor": cursor}))
        if self.error is not None:
            raise self.error
        return self.list_result


def _status_view(**overrides: Any) -> StatusView:
    base: dict[str, Any] = {
        "analysis_id": _ANALYSIS_ID,
        "status": AnalysisStatus.RUNNING,
        "item_count": 10,
        "done_count": 4,
        "failed_count": 1,
        "skipped_count": 0,
        "progress": 0.5,
        "created_at": _NOW,
        "completed_at": None,
        "download_available": False,
        "failures": (),
    }
    base.update(overrides)
    return StatusView(**base)


def test_status_get_single_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /{id} serializa la StatusView con contadores y progreso (11.1)."""
    service = _FakeStatusService(status_result=_status_view())
    monkeypatch.setattr(api_status, "_SERVICE", service)

    response = api_status.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "RUNNING"
    assert body["done_count"] == 4
    assert body["failed_count"] == 1
    assert body["progress"] == 0.5
    assert body["failures"] == []


def test_status_include_failures_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_failures=true se pasa al servicio y los fallos se serializan sin PII (11.5)."""
    view = _status_view(
        status=AnalysisStatus.COMPLETED_WITH_ERRORS,
        failures=(FailureView(item_id=f"{_ANALYSIS_ID}#00002", error_code="LLM_OUTPUT_INVALID"),),
    )
    service = _FakeStatusService(status_result=view)
    monkeypatch.setattr(api_status, "_SERVICE", service)

    response = api_status.handler(
        _event(path={"analysis_id": _ANALYSIS_ID}, query={"include_failures": "true"}),
        None,
    )

    _, kwargs = service.calls[0]
    assert kwargs["include_failures"] is True
    body = json.loads(response["body"])
    assert body["failures"] == [
        {"item_id": f"{_ANALYSIS_ID}#00002", "error_code": "LLM_OUTPUT_INVALID"}
    ]


def test_status_not_found_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Lote inexistente/ajeno → 404 NOT_FOUND con cuerpo genérico (2.2, 11.10)."""
    service = _FakeStatusService(error=NotFoundError())
    monkeypatch.setattr(api_status, "_SERVICE", service)

    response = api_status.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {
        "error_code": "NOT_FOUND",
        "correlation_id": "req-123",
    }


def test_status_list_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /v1/analyses (sin id) enruta al listado y serializa la página (11.7)."""
    summary = AnalysisSummaryView(
        analysis_id=_ANALYSIS_ID,
        status=AnalysisStatus.COMPLETED,
        item_count=2,
        done_count=2,
        failed_count=0,
        skipped_count=0,
        progress=1.0,
        created_at=_NOW,
        completed_at=_NOW,
        download_available=True,
    )
    view = AnalysisListView(analyses=(summary,), next_cursor="opaque", page_size=20)
    service = _FakeStatusService(list_result=view)
    monkeypatch.setattr(api_status, "_SERVICE", service)

    response = api_status.handler(_event(query={"page_size": "20"}), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["page_size"] == 20
    assert body["next_cursor"] == "opaque"
    assert len(body["analyses"]) == 1
    assert body["analyses"][0]["download_available"] is True


def test_status_list_empty_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Tenant sin Lotes devuelve una colección vacía (criterio 2.4)."""
    view = AnalysisListView(analyses=(), next_cursor=None, page_size=20)
    service = _FakeStatusService(list_result=view)
    monkeypatch.setattr(api_status, "_SERVICE", service)

    response = api_status.handler(_event(), None)

    body = json.loads(response["body"])
    assert body["analyses"] == []
    assert body["next_cursor"] is None


def test_status_list_invalid_page_size_is_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """page_size no entero → 400 VALIDATION_FAILED sin tocar el servicio (11.12)."""
    service = _FakeStatusService(list_result=None)
    monkeypatch.setattr(api_status, "_SERVICE", service)

    response = api_status.handler(_event(query={"page_size": "abc"}), None)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error_code"] == "VALIDATION_FAILED"
    assert service.calls == []


# ===========================================================================
# api_download
# ===========================================================================


class _FakeDownloadService(_FakeService):
    def issue(self, analysis_id: str, tenant_id: str) -> Any:
        return self._invoke(analysis_id, tenant_id)


def test_download_success_returns_url_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Éxito: 200 con analysis_id, status, download_url y expires_at ISO (12.1)."""
    view = DownloadView(
        analysis_id=_ANALYSIS_ID,
        status=AnalysisStatus.COMPLETED,
        url="https://example.invalid/signed",
        expires_at=_NOW,
    )
    service = _FakeDownloadService(result=view)
    monkeypatch.setattr(api_download, "_SERVICE", service)

    response = api_download.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["analysis_id"] == _ANALYSIS_ID
    assert body["status"] == "COMPLETED"
    assert body["download_url"] == "https://example.invalid/signed"
    assert body["expires_at"] == "2025-06-14T10:12:43+00:00"


def test_download_not_ready_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Lote no terminal/sin salida → 409 ANALYSIS_NOT_READY (12.3, 12.4)."""
    service = _FakeDownloadService(
        error=NotReadyError(reason="not_terminal", status=AnalysisStatus.RUNNING)
    )
    monkeypatch.setattr(api_download, "_SERVICE", service)

    response = api_download.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["error_code"] == "ANALYSIS_NOT_READY"


def test_download_not_found_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Lote inexistente/ajeno/objeto expirado → 404 NOT_FOUND (12.5, 12.6)."""
    service = _FakeDownloadService(error=NotFoundError(reason="output_object_missing"))
    monkeypatch.setattr(api_download, "_SERVICE", service)

    response = api_download.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 404


def test_download_passes_tenant_from_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El servicio se invoca con el analysis_id de la ruta y el tenant del token."""
    view = DownloadView(
        analysis_id=_ANALYSIS_ID,
        status=AnalysisStatus.COMPLETED,
        url="https://example.invalid/x",
        expires_at=_NOW,
    )
    service = _FakeDownloadService(result=view)
    monkeypatch.setattr(api_download, "_SERVICE", service)

    api_download.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    (args, _) = service.calls[0]
    assert args == (_ANALYSIS_ID, "acme")


# ===========================================================================
# api_purge
# ===========================================================================


class _FakePurgeService(_FakeService):
    def execute(self, command: Any) -> Any:
        return self._invoke(command)


def test_purge_success_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """Éxito: 200 con analysis_id y purged_at ISO 8601 UTC (19.1)."""
    service = _FakePurgeService(
        result=PurgeResult(analysis_id=_ANALYSIS_ID, purged_at=_NOW)
    )
    monkeypatch.setattr(api_purge, "_SERVICE", service)

    response = api_purge.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body == {
        "analysis_id": _ANALYSIS_ID,
        "purged_at": "2025-06-14T10:12:43+00:00",
    }


def test_purge_builds_command_with_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El comando toma analysis_id de la ruta y tenant_id/principal_sub del token."""
    service = _FakePurgeService(
        result=PurgeResult(analysis_id=_ANALYSIS_ID, purged_at=_NOW)
    )
    monkeypatch.setattr(api_purge, "_SERVICE", service)

    api_purge.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    (command,), _ = service.calls[0]
    assert command.analysis_id == _ANALYSIS_ID
    assert command.tenant_id == "acme"
    assert command.principal_sub == "user-7"
    assert command.correlation_id == "req-123"


def test_purge_not_ready_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Lote propio no terminal → 409 ANALYSIS_NOT_READY (19.6)."""
    service = _FakePurgeService(
        error=NotReadyError(reason="not_terminal", status=AnalysisStatus.PENDING)
    )
    monkeypatch.setattr(api_purge, "_SERVICE", service)

    response = api_purge.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 409
    assert json.loads(response["body"])["error_code"] == "ANALYSIS_NOT_READY"


def test_purge_not_found_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un Lote inexistente/ajeno/ya purgado → 404 NOT_FOUND (19.5)."""
    service = _FakePurgeService(error=NotFoundError())
    monkeypatch.setattr(api_purge, "_SERVICE", service)

    response = api_purge.handler(_event(path={"analysis_id": _ANALYSIS_ID}), None)

    assert response["statusCode"] == 404


def test_purge_forbidden_without_sub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin claim sub → 403 FORBIDDEN sin borrar nada (2.9)."""
    service = _FakePurgeService(
        result=PurgeResult(analysis_id=_ANALYSIS_ID, purged_at=_NOW)
    )
    monkeypatch.setattr(api_purge, "_SERVICE", service)

    response = api_purge.handler(
        _event(claims={"custom:tenant_id": "acme"}, path={"analysis_id": _ANALYSIS_ID}),
        None,
    )

    assert response["statusCode"] == 403
    assert service.calls == []
