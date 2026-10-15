"""Pruebas unitarias de las utilidades compartidas de la capa de API (tarea 9.1).

Verifican :mod:`sentiment.handlers._http`, el módulo que centraliza la identidad
desde el autorizador, el ``correlation_id`` y la traducción de errores sin PII
que comparten los cuatro handlers de la capa de API. Son pruebas puras: no tocan
AWS ni requieren configuración de entorno.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from sentiment.domain.errors import ForbiddenError, NotFoundError, ValidationError
from sentiment.handlers import _http


def _event_with_claims(claims: dict[str, Any], *, native_jwt: bool = True) -> dict[str, Any]:
    """Construye un evento con los claims en el *shape* del autorizador indicado."""
    authorizer = {"jwt": {"claims": claims}} if native_jwt else {"claims": claims}
    return {"requestContext": {"requestId": "req-123", "authorizer": authorizer}}


# --- extract_identity -------------------------------------------------------


@pytest.mark.parametrize("native_jwt", [True, False])
def test_extract_identity_reads_claims_from_authorizer(native_jwt: bool) -> None:
    """Lee tenant_id y sub de ambos shapes del autorizador (criterio 1.14)."""
    event = _event_with_claims(
        {"custom:tenant_id": "acme-corp", "sub": "user-7"}, native_jwt=native_jwt
    )

    identity = _http.extract_identity(event)

    assert identity.tenant_id == "acme-corp"
    assert identity.principal_sub == "user-7"


def test_extract_identity_trims_claim_values() -> None:
    """Recorta los espacios de los claims antes de usarlos."""
    event = _event_with_claims({"custom:tenant_id": " acme ", "sub": " user-7 "})

    identity = _http.extract_identity(event)

    assert identity.tenant_id == "acme"
    assert identity.principal_sub == "user-7"


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "user-7"},  # falta tenant_id
        {"custom:tenant_id": "", "sub": "user-7"},  # tenant_id vacío
        {"custom:tenant_id": "   ", "sub": "user-7"},  # tenant_id en blanco
        {"custom:tenant_id": "acme"},  # falta sub
        {"custom:tenant_id": "acme", "sub": ""},  # sub vacío
        {},  # sin claims
    ],
)
def test_extract_identity_forbidden_when_identity_incomplete(
    claims: dict[str, Any],
) -> None:
    """Un tenant_id o sub ausente/en blanco es ForbiddenError (403, criterio 2.9)."""
    with pytest.raises(ForbiddenError) as exc_info:
        _http.extract_identity(_event_with_claims(claims))

    assert exc_info.value.http_status == 403
    assert exc_info.value.error_code == "FORBIDDEN"


def test_extract_identity_forbidden_when_authorizer_absent() -> None:
    """Un evento sin contexto de autorizador es 403, no un 5xx."""
    with pytest.raises(ForbiddenError):
        _http.extract_identity({"requestContext": {}})


def test_extract_identity_ignores_body_and_query_tenant_id() -> None:
    """El tenant_id del cuerpo/query NUNCA se usa como identidad (criterio 2.10)."""
    event = _event_with_claims({"custom:tenant_id": "acme", "sub": "user-7"})
    event["body"] = json.dumps({"tenant_id": "evil-corp"})
    event["queryStringParameters"] = {"tenant_id": "evil-corp"}
    event["headers"] = {"tenant_id": "evil-corp"}

    identity = _http.extract_identity(event)

    assert identity.tenant_id == "acme"


# --- resolve_correlation_id -------------------------------------------------


def test_resolve_correlation_id_uses_request_id() -> None:
    """Usa el requestId de API Gateway cuando está presente."""
    event = {"requestContext": {"requestId": "req-abc"}}
    assert _http.resolve_correlation_id(event) == "req-abc"


def test_resolve_correlation_id_generates_uuid_when_absent() -> None:
    """Genera un UUIDv4 cuando no hay requestId (criterio 3.15)."""
    generated = _http.resolve_correlation_id({})

    # Debe ser un UUID versión 4 válido.
    parsed = uuid.UUID(generated)
    assert parsed.version == 4


def test_resolve_correlation_id_generates_uuid_when_blank() -> None:
    """Un requestId en blanco se trata como ausente y se genera un UUIDv4."""
    generated = _http.resolve_correlation_id(
        {"requestContext": {"requestId": "   "}}
    )
    assert uuid.UUID(generated).version == 4


# --- json_response / error_response -----------------------------------------


def test_json_response_shape_and_encoding() -> None:
    """json_response devuelve el shape proxy y preserva caracteres no ASCII."""
    response = _http.json_response(202, {"mensaje": "añadido"})

    assert response["statusCode"] == 202
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert body == {"mensaje": "añadido"}
    # ensure_ascii=False preserva la ñ sin escaparla.
    assert "añadido" in response["body"]


def test_error_response_contains_only_error_code_and_correlation_id() -> None:
    """El cuerpo de error expone solo error_code + correlation_id (criterios 3.14, 15.5)."""
    exc = ValidationError("detalle interno", index=3, internal="secreto")
    response = _http.error_response(exc, "corr-1")

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body == {"error_code": "VALIDATION_FAILED", "correlation_id": "corr-1"}
    # Ni el detail ni el mensaje interno se filtran al cliente.
    assert "detalle interno" not in response["body"]
    assert "secreto" not in response["body"]


def test_error_response_uses_error_http_status() -> None:
    """El código HTTP sale del propio error del dominio."""
    response = _http.error_response(NotFoundError(), "corr-2")
    assert response["statusCode"] == 404


def test_internal_error_response_is_generic_500() -> None:
    """El fallo de infraestructura es un 500 genérico sin detalle (criterio 15.5)."""
    response = _http.internal_error_response("corr-3")

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body == {"error_code": "INTERNAL_ERROR", "correlation_id": "corr-3"}
