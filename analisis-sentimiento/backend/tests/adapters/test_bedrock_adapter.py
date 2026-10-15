"""Pruebas unitarias del adaptador de Bedrock (tarea 6.3).

Cubren el contrato del puerto :class:`~sentiment.domain.ports.LlmAnalyzer`
implementado por :class:`~sentiment.adapters.bedrock_adapter.BedrockAdapter`,
sobre los criterios 7.1, 7.9, 7.11, 7.12 y 9.18:

* **Una sola invocación, sin reintentos internos (7.1, 9.18):** ``analyze`` llama
  a ``converse`` exactamente una vez; un error de dependencia sube en su primera
  ocurrencia sin ningún bucle de reintento.
* **``model_id`` desde Parameter_Store/entorno (7.9):** el modelo se resuelve de
  :func:`sentiment.observability.config.model_id`; su ausencia propaga
  ``ConfigError`` **sin** invocar a Bedrock (criterio 7.15).
* **Tokens como métrica, sin PII (7.11):** los tokens de ``usage`` se publican
  como métricas; el texto de ``respuesta``/``explicacion`` nunca aparece en los
  registros de log.
* **Salida total: nunca ``None`` (7.12):** ``analyze`` devuelve un
  :class:`EmotionAnalysis` válido o lanza una excepción tipada del catálogo.
* **Mapeo de errores (9.16/9.17):** throttling ⇒ ``ThrottledError``;
  5xx/timeout/red ⇒ ``TransientDependencyError``; ``ValidationException`` de
  entrada ⇒ ``LlmInputRejectedError``.

Las pruebas usan un doble ligero del cliente ``converse`` (sin ``moto`` ni red):
el adaptador acepta el cliente por inyección, de modo que no se simula AWS sino
que se sustituye la frontera por un contrato equivalente.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from sentiment.adapters import aws_clients, bedrock_adapter
from sentiment.adapters.bedrock_adapter import BedrockAdapter
from sentiment.domain.errors import (
    LlmInputRejectedError,
    LlmSchemaError,
    ThrottledError,
    TransientDependencyError,
)
from sentiment.observability import config
from sentiment.observability.metrics import get_metrics

RESPUESTA_PII = "me siento muy comodo con mi equipo y mi jefe directo"
EXPLICACION_PII = "La persona expresa satisfacción con su entorno laboral."


def _valid_llm_object(
    sentimientos: list[str] | None = None,
    explicacion: str = EXPLICACION_PII,
) -> dict[str, object]:
    """Objeto conforme al JSON_Schema ``llm_output``."""
    return {
        "Indificador": "Satisfacción en el Puesto",
        "Respuesta": "me siento comodo con el equipo",
        "sentimientos": ["alegría", "gratitud"] if sentimientos is None else sentimientos,
        "explicacion": explicacion,
    }


def _converse_response(
    text: str,
    *,
    input_tokens: int | None = 128,
    output_tokens: int | None = 64,
) -> dict[str, Any]:
    """Respuesta sintética de la API ``Converse`` con un bloque de texto y usage."""
    usage: dict[str, Any] = {}
    if input_tokens is not None:
        usage["inputTokens"] = input_tokens
    if output_tokens is not None:
        usage["outputTokens"] = output_tokens
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": usage,
    }


class _FakeConverseClient:
    """Doble del cliente de Bedrock Runtime: captura llamadas y devuelve lo pactado."""

    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _client_error(code: str) -> ClientError:
    """Construye un ``ClientError`` de boto3 con el ``Error.Code`` indicado."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": "x"}},
        operation_name="Converse",
    )


@pytest.fixture(autouse=True)
def _model_id_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inyecta un ``MODEL_ID`` válido y aísla la configuración y las métricas.

    El objeto ``Metrics`` de Powertools comparte un almacén a nivel de clase; se
    vacía antes y después de cada prueba para que las métricas que el adaptador
    publica (``BedrockInputTokens``/``BedrockOutputTokens``/``BedrockThrottles``)
    no contaminen otras pruebas ni acumulen estado entre casos.
    """
    config.reset_cache()
    monkeypatch.setenv("MODEL_ID", "amazon.nova-pro-v1:0")
    get_metrics().clear_metrics()
    yield
    get_metrics().clear_metrics()
    config.reset_cache()


class TestSingleInvocation:
    """Criterios 7.1 y 9.18: exactamente una invocación, sin reintentos internos."""

    def test_analyze_calls_converse_exactly_once(self) -> None:
        fake = _FakeConverseClient(
            response=_converse_response(json.dumps(_valid_llm_object()))
        )
        adapter = BedrockAdapter(client=fake)

        adapter.analyze("Satisfacción en el Puesto", RESPUESTA_PII)

        assert len(fake.calls) == 1

    def test_passes_resolved_model_id_and_single_system_prompt(self) -> None:
        fake = _FakeConverseClient(
            response=_converse_response(json.dumps(_valid_llm_object()))
        )
        adapter = BedrockAdapter(client=fake)

        adapter.analyze("Satisfacción en el Puesto", RESPUESTA_PII)

        call = fake.calls[0]
        assert call["modelId"] == "amazon.nova-pro-v1:0"
        assert isinstance(call["system"], list)
        assert call["system"][0]["text"]  # system_prompt no vacío
        # El texto del usuario es la respuesta; el prefill del assistant va aparte.
        assert call["messages"][0]["content"][0]["text"] == RESPUESTA_PII
        assert call["messages"][1]["role"] == "assistant"

    def test_no_retry_loop_on_transient_error(self) -> None:
        fake = _FakeConverseClient(error=_client_error("ThrottlingException"))
        adapter = BedrockAdapter(client=fake)

        with pytest.raises(ThrottledError):
            adapter.analyze("x", RESPUESTA_PII)

        # Una sola llamada: el reintento vive en Step Functions, no aquí.
        assert len(fake.calls) == 1


class TestTotalOutput:
    """Criterio 7.12: ``analyze`` devuelve EmotionAnalysis válido o lanza; nunca None."""

    def test_returns_normalized_emotion_analysis(self) -> None:
        obj = _valid_llm_object(["  Alegría ", "ALEGRÍA", "gratitud"])
        fake = _FakeConverseClient(response=_converse_response(json.dumps(obj)))
        adapter = BedrockAdapter(client=fake)

        result = adapter.analyze("Satisfacción en el Puesto", RESPUESTA_PII)

        assert result is not None
        # Normalización del parseo total (dedup, minúsculas, trim).
        assert result.sentimientos == ("alegría", "gratitud")
        assert result.model_id == "amazon.nova-pro-v1:0"
        assert result.input_tokens == 128
        assert result.output_tokens == 64

    def test_handles_fenced_output(self) -> None:
        obj = _valid_llm_object(["calma"])
        raw = "```json\n" + json.dumps(obj) + "\n```"
        fake = _FakeConverseClient(response=_converse_response(raw))
        adapter = BedrockAdapter(client=fake)

        result = adapter.analyze("x", RESPUESTA_PII)

        assert result.sentimientos == ("calma",)

    def test_unparseable_output_raises_schema_error(self) -> None:
        fake = _FakeConverseClient(
            response=_converse_response("no hay ningún json aquí")
        )
        adapter = BedrockAdapter(client=fake)

        with pytest.raises(LlmSchemaError) as excinfo:
            adapter.analyze("x", RESPUESTA_PII)
        assert excinfo.value.error_code == "LLM_OUTPUT_INVALID"

    def test_empty_output_raises_schema_error(self) -> None:
        # Respuesta sin bloque de texto: _extract_text -> "" -> LlmSchemaError.
        fake = _FakeConverseClient(response={"output": {}, "usage": {}})
        adapter = BedrockAdapter(client=fake)

        with pytest.raises(LlmSchemaError):
            adapter.analyze("x", RESPUESTA_PII)


class TestModelIdResolution:
    """Criterios 7.9 y 7.15: model_id desde config; ausencia no invoca a Bedrock."""

    def test_missing_model_id_raises_without_invoking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config.reset_cache()
        monkeypatch.delenv("MODEL_ID", raising=False)
        monkeypatch.setenv("MODEL_ID", "   ")  # en blanco: inválido (7.15)
        fake = _FakeConverseClient(
            response=_converse_response(json.dumps(_valid_llm_object()))
        )
        adapter = BedrockAdapter(client=fake)

        with pytest.raises(config.ConfigError):
            adapter.analyze("x", RESPUESTA_PII)

        # No se tocó a Bedrock (criterio 7.15).
        assert fake.calls == []


class TestErrorMapping:
    """Criterios 9.16/9.17: traducción de errores de boto3 al Catalogo_De_Errores."""

    @pytest.mark.parametrize(
        "code",
        ["ThrottlingException", "TooManyRequestsException"],
    )
    def test_throttling_maps_to_throttled_error(self, code: str) -> None:
        adapter = BedrockAdapter(client=_FakeConverseClient(error=_client_error(code)))
        with pytest.raises(ThrottledError) as excinfo:
            adapter.analyze("x", RESPUESTA_PII)
        assert excinfo.value.is_transient is True

    @pytest.mark.parametrize(
        "code",
        [
            "ServiceUnavailableException",
            "InternalServerException",
            "ModelTimeoutException",
        ],
    )
    def test_service_errors_map_to_transient(self, code: str) -> None:
        adapter = BedrockAdapter(client=_FakeConverseClient(error=_client_error(code)))
        with pytest.raises(TransientDependencyError) as excinfo:
            adapter.analyze("x", RESPUESTA_PII)
        assert excinfo.value.is_transient is True

    def test_validation_exception_maps_to_input_rejected(self) -> None:
        adapter = BedrockAdapter(
            client=_FakeConverseClient(error=_client_error("ValidationException"))
        )
        with pytest.raises(LlmInputRejectedError) as excinfo:
            adapter.analyze("x", RESPUESTA_PII)
        assert excinfo.value.error_code == "LLM_INPUT_REJECTED"
        assert excinfo.value.is_transient is False

    def test_network_error_maps_to_transient(self) -> None:
        err = ReadTimeoutError(endpoint_url="https://bedrock-runtime", request=None)
        adapter = BedrockAdapter(client=_FakeConverseClient(error=err))
        with pytest.raises(TransientDependencyError):
            adapter.analyze("x", RESPUESTA_PII)


class TestTokensAndPii:
    """Criterio 7.11: tokens como métrica (0 si faltan) y ausencia de PII en logs."""

    def test_missing_usage_counts_as_zero(self) -> None:
        obj = _valid_llm_object(["calma"])
        fake = _FakeConverseClient(
            response=_converse_response(
                json.dumps(obj), input_tokens=None, output_tokens=None
            )
        )
        adapter = BedrockAdapter(client=fake)

        result = adapter.analyze("x", RESPUESTA_PII)

        assert result.input_tokens == 0
        assert result.output_tokens == 0

    def test_no_pii_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        obj = _valid_llm_object(["calma"], explicacion=EXPLICACION_PII)
        fake = _FakeConverseClient(response=_converse_response(json.dumps(obj)))
        adapter = BedrockAdapter(client=fake)

        with caplog.at_level("INFO"):
            adapter.analyze("Satisfacción en el Puesto", RESPUESTA_PII)

        logged = caplog.text
        assert RESPUESTA_PII not in logged
        assert EXPLICACION_PII not in logged


def test_default_client_is_the_bedrock_singleton() -> None:
    """Sin inyección, el adaptador usa el singleton de aws_clients (criterio 17.11)."""
    adapter = BedrockAdapter()
    # ``cast`` es identidad en runtime: el cliente estrechado al protocolo sigue
    # siendo el mismo objeto singleton de aws_clients.
    assert adapter._client is cast(object, aws_clients.bedrock_runtime_client())
    assert bedrock_adapter.BedrockAdapter is BedrockAdapter
