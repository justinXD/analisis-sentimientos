"""Pruebas unitarias del handler ``notify_email`` (tarea 9.5).

Cubren la notificación por correo del Evento_Finalizacion vía SES:

* **Lectura (criterio 13.1):** parseo del ``detail`` del evento
  ``analysis.completed`` a la vista tipada, con rechazo de eventos malformados
  (:class:`EmailEventError`), que **sí** se propagan (contrato de evento roto).
* **Resolución del destinatario (criterios 13.3, 15.1):** el correo es PII y se
  resuelve vía un puerto inyectable, nunca del ``detail`` del evento.
* **Enlace de descarga, no Presigned_URL (criterio 13.4):** el cuerpo del correo
  apunta a ``GET /v1/analyses/{analysis_id}/download``; el endpoint re-autentica.
* **Configuration set de rebotes/quejas (criterio 13.10):** el emisor de SES pasa
  ``ConfigurationSetName``.
* **Mejor esfuerzo (criterio 13.8):** un fallo de SES se captura y el handler
  retorna ``failed`` sin relanzar; nunca falla el Lote ni el consumidor.
* **Sin PII en logs/retorno (criterios 15.1, 15.2):** el correo se enmascara.
"""

from __future__ import annotations

import pytest

from sentiment.handlers import notify_email as handler
from sentiment.handlers.notify_email import (
    EmailCompletedEvent,
    EmailEventError,
    EmailMessage,
    EmailTarget,
    SesEmailSender,
    build_download_link,
    compose_email,
    mask_email,
    parse_event,
)

ANALYSIS_ID = "01JDQZ8F3K7XWQ2M5N8P4R6T9V"
TENANT = "acme-corp"
RECIPIENT = "ana.perez@ejemplo.com"
FROM_ADDRESS = "no-reply@sentiment.ejemplo.com"
DOWNLOAD_BASE = "https://api.ejemplo.com/v1/analyses"
CONFIG_SET = "sentiment-bounces-complaints"


def _event_detail() -> dict[str, object]:
    """``detail`` de un evento ``analysis.completed`` (sección 9.3), sin PII."""
    return {
        "event_id": f"{ANALYSIS_ID}:COMPLETED",
        "analysis_id": ANALYSIS_ID,
        "tenant_id": TENANT,
        "status": "COMPLETED",
        "correlation_id": "7f3b2c1a-0d4e-4f6a-8b9c-1a2b3c4d5e6f",
        "item_count": 10,
        "done_count": 10,
        "failed_count": 0,
        "output_s3_key": "outputs/acme-corp/2025/06/14/01JDQZ.json",
    }


def _event() -> dict[str, object]:
    return {
        "source": "sentiment.pipeline",
        "detail-type": "analysis.completed",
        "detail": _event_detail(),
    }


class _RecordingResolver:
    """Resolutor de destinatario que devuelve un objetivo fijo (o ``None``)."""

    def __init__(self, target: EmailTarget | None) -> None:
        self._target = target
        self.seen: list[EmailCompletedEvent] = []

    def resolve(self, event: EmailCompletedEvent) -> EmailTarget | None:
        self.seen.append(event)
        return self._target


class _RecordingSender:
    """Emisor que registra los correos recibidos sin tocar SES."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


class _FailingSender:
    """Emisor que simula un fallo de SES."""

    def send(self, message: EmailMessage) -> None:
        raise RuntimeError("SES rechazó el envío")


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restaura resolutor y emisor del módulo entre casos, y la configuración SES."""
    monkeypatch.setattr(handler, "_RESOLVER", None)
    monkeypatch.setattr(handler, "_SENDER", _RecordingSender())
    monkeypatch.setenv("SES_FROM_ADDRESS", FROM_ADDRESS)
    monkeypatch.setenv("DOWNLOAD_URL_BASE", DOWNLOAD_BASE)
    monkeypatch.setenv("SES_CONFIGURATION_SET", CONFIG_SET)


# --------------------------------------------------------------------------- #
# parse_event                                                                 #
# --------------------------------------------------------------------------- #


def test_parse_event_extracts_identifiers() -> None:
    parsed = parse_event(_event())

    assert parsed.analysis_id == ANALYSIS_ID
    assert parsed.tenant_id == TENANT
    assert parsed.status == "COMPLETED"
    assert parsed.correlation_id == "7f3b2c1a-0d4e-4f6a-8b9c-1a2b3c4d5e6f"


def test_parse_event_defaults_missing_fields() -> None:
    detail = _event_detail()
    del detail["correlation_id"]
    del detail["status"]
    parsed = parse_event({"detail-type": "analysis.completed", "detail": detail})

    assert parsed.correlation_id == ""
    assert parsed.status == ""


def test_parse_event_detail_carries_no_notify_email() -> None:
    # El contrato del evento (13.3) excluye el notify_email: la vista tipada no lo
    # expone y el detail de prueba no lo trae.
    assert "notify_email" not in _event_detail()
    assert "email" not in _event_detail()


def test_parse_event_rejects_wrong_detail_type() -> None:
    event = _event()
    event["detail-type"] = "something.else"

    with pytest.raises(EmailEventError):
        parse_event(event)


def test_parse_event_rejects_non_object_detail() -> None:
    with pytest.raises(EmailEventError):
        parse_event({"detail-type": "analysis.completed", "detail": "not-an-object"})


def test_parse_event_rejects_missing_analysis_id() -> None:
    detail = _event_detail()
    del detail["analysis_id"]

    with pytest.raises(EmailEventError):
        parse_event({"detail-type": "analysis.completed", "detail": detail})


def test_parse_event_rejects_missing_tenant_id() -> None:
    detail = _event_detail()
    del detail["tenant_id"]

    with pytest.raises(EmailEventError):
        parse_event({"detail-type": "analysis.completed", "detail": detail})


def test_email_event_error_is_permanent() -> None:
    assert EmailEventError.is_transient is False
    assert EmailEventError.http_status is None


# --------------------------------------------------------------------------- #
# mask_email (criterios 15.1, 15.2)                                           #
# --------------------------------------------------------------------------- #


def test_mask_email_keeps_first_char_and_domain() -> None:
    assert mask_email("ana.perez@ejemplo.com") == "a***@ejemplo.com"


def test_mask_email_single_char_local() -> None:
    assert mask_email("x@ejemplo.com") == "x***@ejemplo.com"


def test_mask_email_without_at_sign_is_fully_masked() -> None:
    assert mask_email("no-es-un-correo") == "***"


def test_mask_email_does_not_leak_full_address() -> None:
    masked = mask_email("confidencial@secreto.com")
    assert "confidencial" not in masked
    assert masked == "c***@secreto.com"


# --------------------------------------------------------------------------- #
# build_download_link (criterio 13.4)                                         #
# --------------------------------------------------------------------------- #


def test_build_download_link_points_to_download_endpoint() -> None:
    link = build_download_link(DOWNLOAD_BASE, ANALYSIS_ID)
    assert link == f"{DOWNLOAD_BASE}/{ANALYSIS_ID}/download"


def test_build_download_link_tolerates_trailing_slash() -> None:
    link = build_download_link(DOWNLOAD_BASE + "/", ANALYSIS_ID)
    assert link == f"{DOWNLOAD_BASE}/{ANALYSIS_ID}/download"


def test_build_download_link_is_not_a_presigned_url() -> None:
    # Es un enlace al endpoint autenticado, no una Presigned_URL de S3.
    link = build_download_link(DOWNLOAD_BASE, ANALYSIS_ID)
    assert "X-Amz-Signature" not in link
    assert ".s3." not in link
    assert link.endswith("/download")


# --------------------------------------------------------------------------- #
# compose_email (criterios 13.4, 13.10)                                       #
# --------------------------------------------------------------------------- #


def test_compose_email_includes_download_link_and_config_set() -> None:
    parsed = parse_event(_event())
    message = compose_email(parsed, EmailTarget(email=RECIPIENT))

    assert message.sender == FROM_ADDRESS
    assert message.recipient == RECIPIENT
    assert message.configuration_set == CONFIG_SET
    assert f"{DOWNLOAD_BASE}/{ANALYSIS_ID}/download" in message.body
    assert ANALYSIS_ID in message.subject


def test_compose_email_body_has_no_presigned_url() -> None:
    parsed = parse_event(_event())
    message = compose_email(parsed, EmailTarget(email=RECIPIENT))

    assert "X-Amz-Signature" not in message.body
    assert ".s3." not in message.body


def test_compose_email_raises_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SES_FROM_ADDRESS", raising=False)
    parsed = parse_event(_event())

    with pytest.raises(EmailEventError):
        compose_email(parsed, EmailTarget(email=RECIPIENT))


# --------------------------------------------------------------------------- #
# SesEmailSender (criterios 13.4, 13.10)                                      #
# --------------------------------------------------------------------------- #


class _FakeSesClient:
    """Doble del cliente de SES que registra la llamada a ``send_email``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_email(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"MessageId": "fake-message-id"}


def test_ses_sender_passes_configuration_set_and_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSesClient()
    monkeypatch.setattr(handler, "ses_client", lambda: fake)

    message = EmailMessage(
        sender=FROM_ADDRESS,
        recipient=RECIPIENT,
        subject="asunto",
        body="cuerpo con enlace",
        configuration_set=CONFIG_SET,
    )
    SesEmailSender().send(message)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["Source"] == FROM_ADDRESS
    assert call["ConfigurationSetName"] == CONFIG_SET
    assert call["Destination"] == {"ToAddresses": [RECIPIENT]}
    message_payload = call["Message"]
    assert isinstance(message_payload, dict)
    assert message_payload["Subject"]["Data"] == "asunto"
    assert message_payload["Body"]["Text"]["Data"] == "cuerpo con enlace"


# --------------------------------------------------------------------------- #
# lambda_handler                                                              #
# --------------------------------------------------------------------------- #


def test_handler_unconfigured_resolver_skips_without_error() -> None:
    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "unconfigured", "analysis_id": ANALYSIS_ID}


def test_handler_skips_when_no_email_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _RecordingResolver(target=None)
    monkeypatch.setattr(handler, "_RESOLVER", resolver)

    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "skipped", "analysis_id": ANALYSIS_ID}
    assert resolver.seen[0].analysis_id == ANALYSIS_ID


def test_handler_sends_email_with_download_link(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _RecordingResolver(EmailTarget(email=RECIPIENT))
    sender = _RecordingSender()
    monkeypatch.setattr(handler, "_RESOLVER", resolver)
    monkeypatch.setattr(handler, "_SENDER", sender)

    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "sent", "analysis_id": ANALYSIS_ID}
    assert len(sender.sent) == 1
    sent = sender.sent[0]
    assert sent.recipient == RECIPIENT
    assert sent.configuration_set == CONFIG_SET
    assert f"{DOWNLOAD_BASE}/{ANALYSIS_ID}/download" in sent.body


def test_handler_does_not_block_pipeline_on_ses_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterio 13.8: un fallo de SES no debe propagarse ni fallar el consumidor.
    resolver = _RecordingResolver(EmailTarget(email=RECIPIENT))
    monkeypatch.setattr(handler, "_RESOLVER", resolver)
    monkeypatch.setattr(handler, "_SENDER", _FailingSender())

    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "failed", "analysis_id": ANALYSIS_ID}


def test_handler_does_not_block_pipeline_on_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Una configuración de envío ausente tampoco debe tumbar al consumidor (13.8).
    resolver = _RecordingResolver(EmailTarget(email=RECIPIENT))
    monkeypatch.setattr(handler, "_RESOLVER", resolver)
    monkeypatch.delenv("SES_CONFIGURATION_SET", raising=False)

    result = handler.lambda_handler(_event(), None)

    assert result == {"status": "failed", "analysis_id": ANALYSIS_ID}


def test_handler_propagates_malformed_event() -> None:
    # Un evento malformado SÍ se propaga (contrato roto), a diferencia de SES.
    bad = {"detail-type": "analysis.completed", "detail": {}}

    with pytest.raises(EmailEventError):
        handler.lambda_handler(bad, None)


def test_handler_return_value_has_no_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _RecordingResolver(EmailTarget(email=RECIPIENT))
    monkeypatch.setattr(handler, "_RESOLVER", resolver)
    monkeypatch.setattr(handler, "_SENDER", _RecordingSender())

    result = handler.lambda_handler(_event(), None)

    assert RECIPIENT not in result.values()
    assert set(result.keys()) == {"status", "analysis_id"}
