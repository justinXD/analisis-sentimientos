"""Pruebas unitarias del handler ``notify_webhook`` (tareas 9.3 y 9.4).

Cubren el flujo completo del webhook:

* **Lectura y preparación (9.3):** parseo del ``detail`` del evento
  ``analysis.completed`` a la vista tipada, con rechazo de eventos malformados
  (``WebhookEventError``); preparación de la :class:`WebhookDelivery` (``POST``,
  cuerpo JSON determinista, cabeceras base, rechazo de URLs no HTTPS).
* **Firma HMAC-SHA256 (9.4, criterios 13.6/13.7):** :func:`sign_delivery` añade
  ``X-Timestamp`` (epoch) y ``X-Signature`` (``sha256=<hexdigest>`` sobre
  ``timestamp.body`` con el secreto del Tenant), reproducible y sin exponer el
  secreto; el proveedor de secretos cachea por contenedor.
* **Entrega con reintentos (9.4, criterio 9.9):** :class:`HttpWebhookSender`
  hace hasta 3 intentos con *backoff*; a la primera respuesta 2xx retorna, y al
  agotar los intentos lanza :class:`WebhookDeliveryError`.
* **Traslado a la DLQ_Notificaciones:** el handler propaga
  :class:`WebhookDeliveryError`/`WebhookEventError` (no las traga) para que el
  *redrive* asíncrono traslade el evento a la DLQ (criterios 9.9, 9.10).
* El cuerpo preparado y los logs nunca contienen PII (13.3) ni el secreto/firma.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import pytest

from sentiment.handlers import notify_webhook as handler

if TYPE_CHECKING:
    from collections.abc import Callable
from sentiment.handlers.notify_webhook import (
    AnalysisCompletedEvent,
    HttpWebhookSender,
    WebhookDelivery,
    WebhookDeliveryError,
    WebhookEventError,
    WebhookTarget,
    parse_event,
    prepare_delivery,
    sign_delivery,
)

ANALYSIS_ID = "01JDQZ8F3K7XWQ2M5N8P4R6T9V"
TENANT = "acme-corp"
SECRET = b"super-secret-shared-key"
SECRET_ID = "arn:aws:secretsmanager:us-east-1:111122223333:secret:webhook-acme"


def _event_detail() -> dict[str, object]:
    """``detail`` de un evento ``analysis.completed`` (sección 9.3), sin PII."""
    return {
        "event_id": f"{ANALYSIS_ID}:COMPLETED_WITH_ERRORS",
        "analysis_id": ANALYSIS_ID,
        "tenant_id": TENANT,
        "owner_sub": "a1b2c3d4",
        "status": "COMPLETED_WITH_ERRORS",
        "correlation_id": "7f3b2c1a-0d4e-4f6a-8b9c-1a2b3c4d5e6f",
        "item_count": 5500,
        "done_count": 5497,
        "failed_count": 3,
        "output_s3_key": "outputs/acme-corp/2025/06/14/01JDQZ.json",
        "started_at": "2025-06-14T10:00:00Z",
        "completed_at": "2025-06-14T10:12:43Z",
        "duration_seconds": 763,
    }


def _event() -> dict[str, object]:
    return {
        "source": "sentiment.pipeline",
        "detail-type": "analysis.completed",
        "detail": _event_detail(),
    }


class _RecordingResolver:
    """Resolutor de destino que devuelve un objetivo fijo (o ``None``)."""

    def __init__(self, target: WebhookTarget | None) -> None:
        self._target = target
        self.seen: list[AnalysisCompletedEvent] = []

    def resolve(self, event: AnalysisCompletedEvent) -> WebhookTarget | None:
        self.seen.append(event)
        return self._target


class _StaticSecretProvider:
    """Proveedor de secretos en memoria para pruebas (no toca Secrets_Manager)."""

    def __init__(self, secret: bytes = SECRET) -> None:
        self._secret = secret
        self.requested: list[str] = []

    def get_secret(self, secret_id: str) -> bytes:
        self.requested.append(secret_id)
        return self._secret


class _RecordingSender:
    """Emisor que registra las entregas recibidas sin tocar la red."""

    def __init__(self) -> None:
        self.sent: list[WebhookDelivery] = []

    def send(self, delivery: WebhookDelivery) -> None:
        self.sent.append(delivery)


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restaura resolutor, proveedor de secretos y emisor del módulo entre casos."""
    monkeypatch.setattr(handler, "_RESOLVER", None)
    monkeypatch.setattr(handler, "_SECRET_PROVIDER", _StaticSecretProvider())
    monkeypatch.setattr(handler, "_SENDER", _RecordingSender())


# --------------------------------------------------------------------------- #
# parse_event                                                                 #
# --------------------------------------------------------------------------- #


def test_parse_event_extracts_identifiers() -> None:
    parsed = parse_event(_event())

    assert parsed.analysis_id == ANALYSIS_ID
    assert parsed.tenant_id == TENANT
    assert parsed.status == "COMPLETED_WITH_ERRORS"
    assert parsed.event_id == f"{ANALYSIS_ID}:COMPLETED_WITH_ERRORS"
    assert parsed.correlation_id == "7f3b2c1a-0d4e-4f6a-8b9c-1a2b3c4d5e6f"
    assert parsed.raw_detail["output_s3_key"] == "outputs/acme-corp/2025/06/14/01JDQZ.json"


def test_parse_event_defaults_missing_correlation_id_to_empty() -> None:
    detail = _event_detail()
    del detail["correlation_id"]
    parsed = parse_event({"detail-type": "analysis.completed", "detail": detail})

    assert parsed.correlation_id == ""


def test_parse_event_rejects_wrong_detail_type() -> None:
    event = _event()
    event["detail-type"] = "something.else"

    with pytest.raises(WebhookEventError):
        parse_event(event)


def test_parse_event_rejects_non_object_detail() -> None:
    with pytest.raises(WebhookEventError):
        parse_event({"detail-type": "analysis.completed", "detail": "not-an-object"})


def test_parse_event_rejects_missing_analysis_id() -> None:
    detail = _event_detail()
    del detail["analysis_id"]
    event = {"detail-type": "analysis.completed", "detail": detail}

    with pytest.raises(WebhookEventError):
        parse_event(event)


def test_parse_event_rejects_missing_tenant_id() -> None:
    detail = _event_detail()
    del detail["tenant_id"]
    event = {"detail-type": "analysis.completed", "detail": detail}

    with pytest.raises(WebhookEventError):
        parse_event(event)


def test_webhook_event_error_is_permanent() -> None:
    # Un evento malformado no se arregla reintentando; va a la DLQ por redrive.
    assert WebhookEventError.is_transient is False
    assert WebhookEventError.http_status is None


def test_webhook_delivery_error_is_transient_without_http_status() -> None:
    # El fallo de entrega es transitorio: el redrive asíncrono lo lleva a la DLQ.
    assert WebhookDeliveryError.is_transient is True
    assert WebhookDeliveryError.http_status is None


# --------------------------------------------------------------------------- #
# prepare_delivery                                                            #
# --------------------------------------------------------------------------- #


def test_prepare_delivery_builds_post_with_json_body() -> None:
    parsed = parse_event(_event())
    target = WebhookTarget(url="https://cliente.ejemplo.com/hooks/sentiment")

    delivery = prepare_delivery(parsed, target)

    assert delivery.method == "POST"
    assert delivery.url == target.url
    assert delivery.headers == {"Content-Type": "application/json"}
    # Sin firma todavía: la firma se añade con sign_delivery.
    assert "X-Signature" not in delivery.headers
    assert "X-Timestamp" not in delivery.headers


def test_prepare_delivery_body_is_deterministic_and_matches_detail() -> None:
    parsed = parse_event(_event())
    target = WebhookTarget(url="https://cliente.ejemplo.com/hooks")

    delivery = prepare_delivery(parsed, target)

    decoded = json.loads(delivery.body)
    assert decoded == _event_detail()
    expected = json.dumps(
        _event_detail(), separators=(",", ":"), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    assert delivery.body == expected


def test_prepare_delivery_rejects_non_https_url() -> None:
    parsed = parse_event(_event())
    target = WebhookTarget(url="http://inseguro.ejemplo.com/hook")

    with pytest.raises(WebhookEventError):
        prepare_delivery(parsed, target)


def test_prepare_delivery_body_has_no_pii_fields() -> None:
    parsed = parse_event(_event())
    delivery = prepare_delivery(
        parsed, WebhookTarget(url="https://cliente.ejemplo.com/hooks")
    )

    decoded = json.loads(delivery.body)
    for pii_field in ("respuesta", "explicacion", "email"):
        assert pii_field not in decoded


# --------------------------------------------------------------------------- #
# sign_delivery (criterios 13.6, 13.7)                                        #
# --------------------------------------------------------------------------- #


def _base_delivery() -> WebhookDelivery:
    return WebhookDelivery(
        method="POST",
        url="https://cliente.ejemplo.com/hooks",
        body=b'{"analysis_id":"01JDQZ"}',
        headers={"Content-Type": "application/json"},
    )


def test_sign_delivery_adds_timestamp_and_signature_headers() -> None:
    delivery = _base_delivery()

    signed = sign_delivery(delivery, SECRET, timestamp=1_718_360_000)

    assert signed.headers["X-Timestamp"] == "1718360000"
    assert signed.headers["X-Signature"].startswith("sha256=")
    # El cuerpo y las cabeceras base se preservan.
    assert signed.body == delivery.body
    assert signed.headers["Content-Type"] == "application/json"


def test_sign_delivery_signature_matches_hmac_of_timestamp_dot_body() -> None:
    delivery = _base_delivery()
    ts = 1_718_360_000

    signed = sign_delivery(delivery, SECRET, timestamp=ts)

    expected_digest = hmac.new(
        SECRET, f"{ts}.".encode() + delivery.body, hashlib.sha256
    ).hexdigest()
    assert signed.headers["X-Signature"] == f"sha256={expected_digest}"
    assert len(expected_digest) == 64


def test_sign_delivery_is_deterministic_for_same_inputs() -> None:
    delivery = _base_delivery()

    first = sign_delivery(delivery, SECRET, timestamp=42)
    second = sign_delivery(delivery, SECRET, timestamp=42)

    assert first.headers["X-Signature"] == second.headers["X-Signature"]


def test_sign_delivery_signature_changes_with_timestamp() -> None:
    delivery = _base_delivery()

    sig_a = sign_delivery(delivery, SECRET, timestamp=1).headers["X-Signature"]
    sig_b = sign_delivery(delivery, SECRET, timestamp=2).headers["X-Signature"]

    # El timestamp forma parte de la cadena firmada (anti-replay): cambia la firma.
    assert sig_a != sig_b


def test_sign_delivery_does_not_leak_secret_in_headers() -> None:
    signed = sign_delivery(_base_delivery(), SECRET, timestamp=1)

    serialized = json.dumps(signed.headers)
    assert SECRET.decode() not in serialized


# --------------------------------------------------------------------------- #
# WebhookDelivery.with_signature_headers                                      #
# --------------------------------------------------------------------------- #


def test_with_signature_headers_merges_without_mutating() -> None:
    base = _base_delivery()

    signed = base.with_signature_headers(
        {"X-Signature": "sha256=abc", "X-Timestamp": "1718360000"}
    )

    assert signed.headers == {
        "Content-Type": "application/json",
        "X-Signature": "sha256=abc",
        "X-Timestamp": "1718360000",
    }
    assert signed.body == base.body
    assert base.headers == {"Content-Type": "application/json"}


# --------------------------------------------------------------------------- #
# HttpWebhookSender (criterio 9.9)                                            #
# --------------------------------------------------------------------------- #


class _Response:
    """Doble de respuesta de ``urlopen`` usable como context manager."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _sender(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int | Exception]
) -> tuple[HttpWebhookSender, list[str], list[float]]:
    """Construye un HttpWebhookSender con urlopen simulado y sleep no-op.

    Devuelve ``(sender, calls, slept)`` donde ``calls`` registra las URLs invocadas
    y ``slept`` las esperas de *backoff* solicitadas.
    """
    calls: list[str] = []
    iterator = iter(statuses)

    def fake_urlopen(request: object, timeout: float) -> _Response:
        calls.append(getattr(request, "full_url", ""))
        outcome = next(iterator)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    slept: list[float] = []
    sleep: Callable[[float], None] = slept.append
    sender = HttpWebhookSender(sleep=sleep)
    return sender, calls, slept


def _signed_https_delivery() -> WebhookDelivery:
    return WebhookDelivery(
        method="POST",
        url="https://cliente.ejemplo.com/hooks",
        body=b"{}",
        headers={"Content-Type": "application/json", "X-Signature": "sha256=x"},
    )


def test_sender_succeeds_on_first_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    sender, calls, slept = _sender(monkeypatch, [200])

    sender.send(_signed_https_delivery())

    assert len(calls) == 1
    assert slept == []  # no espera cuando el primer intento acierta


def test_sender_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sender, calls, slept = _sender(monkeypatch, [500, 200])

    sender.send(_signed_https_delivery())

    assert len(calls) == 2
    assert len(slept) == 1  # una espera entre el intento fallido y el exitoso


def test_sender_raises_after_three_failed_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender, calls, slept = _sender(monkeypatch, [500, 503, 500])

    with pytest.raises(WebhookDeliveryError):
        sender.send(_signed_https_delivery())

    assert len(calls) == 3  # exactamente 3 intentos (criterio 9.9)
    assert len(slept) == 2  # no duerme tras el último intento fallido


def test_sender_retries_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sender, calls, _ = _sender(
        monkeypatch, [TimeoutError("slow"), urllib.error.URLError("down"), 200]
    )

    sender.send(_signed_https_delivery())

    assert len(calls) == 3


def test_sender_rejects_non_https_url() -> None:
    sender = HttpWebhookSender(sleep=lambda _: None)
    delivery = WebhookDelivery(
        method="POST", url="http://inseguro.ejemplo.com/h", body=b"{}", headers={}
    )

    with pytest.raises(WebhookDeliveryError):
        sender.send(delivery)


# --------------------------------------------------------------------------- #
# lambda_handler                                                              #
# --------------------------------------------------------------------------- #


def test_handler_unconfigured_resolver_skips_without_error() -> None:
    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "unconfigured", "analysis_id": ANALYSIS_ID}


def test_handler_skips_when_no_webhook_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _RecordingResolver(target=None)
    monkeypatch.setattr(handler, "_RESOLVER", resolver)

    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "skipped", "analysis_id": ANALYSIS_ID}
    assert resolver.seen[0].analysis_id == ANALYSIS_ID


def test_handler_signs_and_delivers_when_target_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _RecordingResolver(
        WebhookTarget(url="https://cliente.ejemplo.com/hooks", secret_id=SECRET_ID)
    )
    provider = _StaticSecretProvider()
    sender = _RecordingSender()
    monkeypatch.setattr(handler, "_RESOLVER", resolver)
    monkeypatch.setattr(handler, "_SECRET_PROVIDER", provider)
    monkeypatch.setattr(handler, "_SENDER", sender)

    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "delivered", "analysis_id": ANALYSIS_ID}
    assert provider.requested == [SECRET_ID]
    # La entrega enviada va firmada (X-Signature y X-Timestamp presentes).
    assert len(sender.sent) == 1
    sent = sender.sent[0]
    assert sent.headers["X-Signature"].startswith("sha256=")
    assert "X-Timestamp" in sent.headers


def test_handler_skips_signing_when_no_secret_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _RecordingResolver(
        WebhookTarget(url="https://cliente.ejemplo.com/hooks", secret_id=None)
    )
    sender = _RecordingSender()
    monkeypatch.setattr(handler, "_RESOLVER", resolver)
    monkeypatch.setattr(handler, "_SENDER", sender)

    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "unsigned", "analysis_id": ANALYSIS_ID}
    assert sender.sent == []  # no se entrega una carga sin firmar


def test_handler_propagates_delivery_failure_for_dlq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El handler NO traga el fallo de entrega: lo propaga para el redrive a la DLQ.
    resolver = _RecordingResolver(
        WebhookTarget(url="https://cliente.ejemplo.com/hooks", secret_id=SECRET_ID)
    )

    class _FailingSender:
        def send(self, delivery: WebhookDelivery) -> None:
            raise WebhookDeliveryError("agotó reintentos")

    monkeypatch.setattr(handler, "_RESOLVER", resolver)
    monkeypatch.setattr(handler, "_SENDER", _FailingSender())

    with pytest.raises(WebhookDeliveryError):
        handler.lambda_handler(_event(), None)


def test_handler_propagates_malformed_event() -> None:
    bad = {"detail-type": "analysis.completed", "detail": {}}

    with pytest.raises(WebhookEventError):
        handler.lambda_handler(bad, None)
