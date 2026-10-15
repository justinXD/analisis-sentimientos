"""Pruebas de la capa de observabilidad (tarea 1.4).

Cubren las tres piezas introducidas en esta tarea:

* ``config.py``: carga validada y perezosa de la configuración, con fallo
  explícito y accionable cuando falta un valor requerido, sin leer nada a nivel
  de módulo (criterios 7.9, 7.15, 15.5, 17.7 operativo).
* ``logging.py``: redacción de PII de los campos ``respuesta``, ``explicacion``,
  ``email``, ``Text`` y ``TextList``, tanto en la función pura :func:`redact`
  como de extremo a extremo en la salida JSON del logger (criterios 15.1, 15.2).
* ``metrics.py``: registro de tokens de Bedrock sin PII, con 0 para el recuento
  no informado (criterio 7.11).
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from aws_lambda_powertools import Logger

from sentiment.observability import config
from sentiment.observability.logging import (
    REDACTED_FIELDS,
    REDACTION_PLACEHOLDER,
    get_logger,
    log_item_processed,
    redact,
)
from sentiment.observability.metrics import MetricName, get_metrics, record_bedrock_tokens


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Aísla cada prueba del entorno real de configuración.

    Elimina las variables de entorno relevantes y vacía la caché del módulo
    ``config`` antes y después de cada caso, para que las pruebas no dependan del
    orden ni de valores filtrados entre casos.
    """
    for name in ("MODEL_ID", "MAX_ITEMS", "TTL_DAYS", "MAX_CONCURRENCY", "CONFIG_ENV"):
        monkeypatch.delenv(name, raising=False)
    config.reset_cache()
    yield
    config.reset_cache()


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


def test_model_id_missing_raises_actionable_config_error() -> None:
    """Sin ``MODEL_ID`` en el entorno, ``model_id()`` falla con mensaje útil."""
    with pytest.raises(config.ConfigError) as excinfo:
        config.model_id()
    assert "MODEL_ID" in str(excinfo.value)


def test_model_id_reads_env_and_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    """``model_id()`` lee la variable de entorno y recorta espacios."""
    monkeypatch.setenv("MODEL_ID", "  amazon.nova-lite-v1:0  ")
    assert config.model_id() == "amazon.nova-lite-v1:0"


def test_model_id_blank_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un ``MODEL_ID`` compuesto solo de espacios cuenta como ausente (7.15)."""
    monkeypatch.setenv("MODEL_ID", "   ")
    with pytest.raises(config.ConfigError):
        config.model_id()


def test_model_id_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """El valor se memoiza: un cambio posterior del entorno no lo altera."""
    monkeypatch.setenv("MODEL_ID", "model-a")
    assert config.model_id() == "model-a"
    monkeypatch.setenv("MODEL_ID", "model-b")
    assert config.model_id() == "model-a"  # cacheado
    config.reset_cache()
    assert config.model_id() == "model-b"  # tras reset, se vuelve a leer


def test_optional_int_defaults() -> None:
    """Sin variables definidas, los límites usan sus valores por defecto."""
    assert config.max_items() == 1000
    assert config.ttl_days() == 90
    assert config.max_concurrency() == 4


def test_optional_int_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un valor válido en el entorno sustituye al valor por defecto."""
    monkeypatch.setenv("MAX_ITEMS", "500")
    assert config.max_items() == 500


@pytest.mark.parametrize("bad_value", ["abc", "0", "-3", "1.5"])
def test_optional_int_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, bad_value: str
) -> None:
    """Un entero inválido o no positivo falla con ``ConfigError`` nombrando la clave."""
    monkeypatch.setenv("MAX_ITEMS", bad_value)
    with pytest.raises(config.ConfigError) as excinfo:
        config.max_items()
    assert "MAX_ITEMS" in str(excinfo.value)


# ---------------------------------------------------------------------------
# logging.py — función pura de redacción
# ---------------------------------------------------------------------------


def test_redacted_fields_are_exactly_the_pii_fields() -> None:
    """El conjunto de campos redactados es exactamente el definido por el diseño."""
    assert frozenset(
        {"respuesta", "explicacion", "email", "Text", "TextList"}
    ) == REDACTED_FIELDS


def test_redact_masks_each_pii_field() -> None:
    """Cada campo PII, en el nivel superior, se sustituye por el placeholder."""
    payload = {field: "sensible" for field in REDACTED_FIELDS}
    red = redact(payload)
    for field in REDACTED_FIELDS:
        assert red[field] == REDACTION_PLACEHOLDER


def test_redact_preserves_non_pii_fields() -> None:
    """Los campos que no son PII se conservan intactos."""
    payload = {"indificador": "Comunicación", "respuesta_length": 42, "index": 3}
    assert redact(payload) == payload


def test_redact_is_recursive() -> None:
    """La redacción alcanza diccionarios y listas anidados."""
    payload = {
        "nested": {"explicacion": "razon", "ok": 1},
        "items": [{"respuesta": "x", "index": 0}, {"respuesta": "y", "index": 1}],
    }
    red = redact(payload)
    assert red["nested"]["explicacion"] == REDACTION_PLACEHOLDER
    assert red["nested"]["ok"] == 1
    assert red["items"][0]["respuesta"] == REDACTION_PLACEHOLDER
    assert red["items"][1]["respuesta"] == REDACTION_PLACEHOLDER
    assert red["items"][0]["index"] == 0


def test_redact_does_not_mutate_input() -> None:
    """``redact`` es pura: no modifica la estructura recibida."""
    payload = {"respuesta": "texto original"}
    redact(payload)
    assert payload["respuesta"] == "texto original"


def test_redact_is_case_sensitive() -> None:
    """La comparación de claves es sensible a mayúsculas: ``text`` no es ``Text``."""
    payload = {"text": "no es PII declarada", "Text": "sí lo es"}
    red = redact(payload)
    assert red["text"] == "no es PII declarada"
    assert red["Text"] == REDACTION_PLACEHOLDER


# ---------------------------------------------------------------------------
# logging.py — redacción de extremo a extremo en la salida del logger
# ---------------------------------------------------------------------------


def _capture_logger_output() -> tuple[Logger, io.StringIO]:
    """Crea un logger cuyo stream se redirige a un buffer en memoria."""
    buffer = io.StringIO()
    logger = get_logger(service="test")
    for handler in logger.handlers:
        handler.stream = buffer  # type: ignore[attr-defined]
    return logger, buffer


def test_logger_redacts_pii_passed_via_extra() -> None:
    """La PII pasada por error en ``extra`` nunca sale en el JSON del log."""
    logger, buffer = _capture_logger_output()
    logger.info("oops", extra={"respuesta": "FUGA", "email": "x@y.com", "ok": 1})
    record = json.loads(buffer.getvalue().strip())
    assert "FUGA" not in buffer.getvalue()
    assert record["respuesta"] == REDACTION_PLACEHOLDER
    assert record["email"] == REDACTION_PLACEHOLDER
    assert record["ok"] == 1


def test_log_item_processed_emits_only_metadata() -> None:
    """El helper de Ítem registra longitud y etiquetas, nunca el texto."""
    logger, buffer = _capture_logger_output()
    log_item_processed(
        logger,
        analysis_id="01HQ",
        item_id="01HQ#00001",
        indificador="Comunicación",
        respuesta_length=42,
        language_code="es",
        sentimiento_general="Positivo",
        sentimientos_count=3,
    )
    record = json.loads(buffer.getvalue().strip())
    assert record["message"] == "item_processed"
    assert record["respuesta_length"] == 42
    assert record["indificador"] == "Comunicación"
    assert record["sentimiento_general"] == "Positivo"
    assert "respuesta" not in record
    assert "explicacion" not in record


def test_log_item_processed_omits_optional_fields_when_absent() -> None:
    """Los campos opcionales no informados no aparecen en el registro."""
    logger, buffer = _capture_logger_output()
    log_item_processed(
        logger,
        analysis_id="01HQ",
        item_id="01HQ#00002",
        indificador="Reconocimiento",
        respuesta_length=0,
    )
    record = json.loads(buffer.getvalue().strip())
    assert "language_code" not in record
    assert "sentimientos_count" not in record
    assert "model_id" not in record


# ---------------------------------------------------------------------------
# metrics.py
# ---------------------------------------------------------------------------


def _single_value(value: object) -> float:
    """Normaliza el valor serializado de una métrica a un único número.

    Powertools representa el valor de una métrica como un número o como una lista
    de números (cuando hay varias muestras). Para las aserciones de estas pruebas
    basta con el único valor registrado.
    """
    if isinstance(value, list):
        assert len(value) == 1
        return float(value[0])
    assert isinstance(value, int | float)
    return float(value)


def test_record_bedrock_tokens_uses_zero_for_missing_counts() -> None:
    """Un recuento ``None`` se publica como 0 (criterio 7.11)."""
    metrics = get_metrics(namespace="TestNamespace")
    record_bedrock_tokens(metrics, input_tokens=None, output_tokens=120)
    serialized = cast(dict[str, Any], metrics.serialize_metric_set())
    emitted = {m["Name"] for m in serialized["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert MetricName.BEDROCK_INPUT_TOKENS.value in emitted
    assert MetricName.BEDROCK_OUTPUT_TOKENS.value in emitted
    # Powertools serializa el valor de cada métrica como un número o una lista de
    # números; se normaliza a un único valor para la aserción.
    assert _single_value(serialized[MetricName.BEDROCK_INPUT_TOKENS.value]) == 0
    assert _single_value(serialized[MetricName.BEDROCK_OUTPUT_TOKENS.value]) == 120
    metrics.clear_metrics()


def test_metric_names_are_stable_strings() -> None:
    """Los nombres de métrica son cadenas estables (contrato con las alarmas)."""
    assert MetricName.STUCK_ANALYSES.value == "StuckAnalyses"
    assert MetricName.ITEMS_FAILED.value == "ItemsFailed"
