"""Pruebas de los JSON_Schema del Sistema y de su cargador (tarea 4.1).

Cubren tres garantías del contrato de ingesta y de salida:

* **Deriva del enum (Requirement 17.14, criterio 3.8):** el enum de
  ``indificador`` embebido en ``submit_request.schema.json`` debe coincidir
  exactamente, y en orden, con la fuente única
  :func:`sentiment.prompts.indificadores.enum_values`. Esta prueba falla si
  alguien edita uno sin el otro, que es precisamente lo que impide que haya dos
  fuentes de verdad.
* **Validez formal:** los cuatro documentos son JSON Schema Draft 2020-12
  válidos y el cargador construye validadores (incluida la resolución del
  ``$ref`` de ``consolidated_output`` → ``item_result``).
* **Comportamiento de validación:** casos representativos de aceptación y
  rechazo que verifican los límites y la allow-list estricta de los criterios
  3.1, 3.3, 3.8, 3.11 y 7.4.
"""

from __future__ import annotations

from typing import Any

import pytest

from sentiment.prompts import enum_values
from sentiment.schemas import (
    CONSOLIDATED_OUTPUT_SCHEMA_FILE,
    ITEM_RESULT_SCHEMA_FILE,
    LLM_OUTPUT_SCHEMA_FILE,
    SUBMIT_REQUEST_SCHEMA_FILE,
    get_validator,
    load_schema,
    submit_request_indificador_enum,
)

ALL_SCHEMA_FILES = (
    SUBMIT_REQUEST_SCHEMA_FILE,
    LLM_OUTPUT_SCHEMA_FILE,
    ITEM_RESULT_SCHEMA_FILE,
    CONSOLIDATED_OUTPUT_SCHEMA_FILE,
)

VALID_INDIFICADOR = "Satisfacción en el Puesto"


def _valid_submit_body() -> dict[str, Any]:
    return {
        "items": [
            {"indificador": VALID_INDIFICADOR, "respuesta": "me siento comodo"},
        ],
        "metadata": {
            "survey_name": "Clima 2025 Q2",
            "notify_email": "rh@ejemplo.com",
            "webhook_url": "https://hooks.ejemplo.com/sentiment",
        },
    }


class TestEnumSingleSource:
    """El enum del JSON Schema deriva de la única fuente de Indificadores."""

    def test_submit_request_enum_matches_single_source(self) -> None:
        assert submit_request_indificador_enum() == enum_values()

    def test_enum_has_eleven_values(self) -> None:
        assert len(submit_request_indificador_enum()) == 11

    def test_enum_order_is_preserved(self) -> None:
        # No basta con el mismo conjunto: el orden también debe coincidir, para
        # que el contrato sea estable y diffeable frente a la fuente única.
        assert submit_request_indificador_enum() == list(enum_values())


class TestSchemasAreValid:
    @pytest.mark.parametrize("filename", ALL_SCHEMA_FILES)
    def test_schema_is_valid_draft_2020_12(self, filename: str) -> None:
        # get_validator ejecuta check_schema internamente; si el documento no es
        # un JSON Schema válido, aquí lanza SchemaError y la prueba falla.
        validator = get_validator(filename)
        assert validator is not None

    @pytest.mark.parametrize("filename", ALL_SCHEMA_FILES)
    def test_schema_declares_2020_12_dialect(self, filename: str) -> None:
        schema = load_schema(filename)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    def test_load_unknown_schema_rejected(self) -> None:
        with pytest.raises(ValueError, match="Esquema desconocido"):
            load_schema("no_existe.schema.json")


class TestSubmitRequestValidation:
    def test_valid_body_is_accepted(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        assert validator.is_valid(_valid_submit_body())

    def test_minimal_body_without_metadata_is_accepted(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = {"items": [{"indificador": VALID_INDIFICADOR, "respuesta": ""}]}
        assert validator.is_valid(body)

    def test_unknown_top_level_field_is_rejected(self) -> None:
        # Allow-list estricta (criterio 3.3): un campo no declarado se rechaza,
        # p.ej. un tenant_id inyectado en el cuerpo.
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = _valid_submit_body()
        body["tenant_id"] = "acme-corp"
        assert not validator.is_valid(body)

    def test_unknown_item_field_is_rejected(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = _valid_submit_body()
        body["items"][0]["extra"] = "x"
        assert not validator.is_valid(body)

    def test_empty_items_array_is_rejected(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        assert not validator.is_valid({"items": []})

    def test_too_many_items_is_rejected(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = {
            "items": [
                {"indificador": VALID_INDIFICADOR, "respuesta": "x"} for _ in range(1001)
            ]
        }
        assert not validator.is_valid(body)

    def test_exactly_1000_items_is_accepted(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = {
            "items": [
                {"indificador": VALID_INDIFICADOR, "respuesta": "x"} for _ in range(1000)
            ]
        }
        assert validator.is_valid(body)

    def test_unknown_indificador_is_rejected(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = {"items": [{"indificador": "Desconocido", "respuesta": "x"}]}
        assert not validator.is_valid(body)

    def test_indificador_is_case_sensitive(self) -> None:
        # Criterio 3.8: comparación sensible a mayúsculas y minúsculas.
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = {"items": [{"indificador": "satisfacción en el puesto", "respuesta": "x"}]}
        assert not validator.is_valid(body)

    def test_respuesta_over_5000_chars_is_rejected(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = {"items": [{"indificador": VALID_INDIFICADOR, "respuesta": "a" * 5001}]}
        assert not validator.is_valid(body)

    def test_webhook_url_must_be_https(self) -> None:
        # Criterio 3.11: solo https.
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = _valid_submit_body()
        body["metadata"]["webhook_url"] = "http://hooks.ejemplo.com/x"
        assert not validator.is_valid(body)

    def test_invalid_notify_email_is_rejected(self) -> None:
        # Criterio 3.12: formato de email verificado por el format-checker.
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        body = _valid_submit_body()
        body["metadata"]["notify_email"] = "no-es-un-email"
        assert not validator.is_valid(body)

    def test_missing_items_is_rejected(self) -> None:
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        assert not validator.is_valid({"metadata": {}})


class TestLlmOutputValidation:
    def test_valid_output_is_accepted(self) -> None:
        validator = get_validator(LLM_OUTPUT_SCHEMA_FILE)
        output = {
            "Indificador": VALID_INDIFICADOR,
            "Respuesta": "me siento comodo",
            "sentimientos": ["satisfacción", "frustración"],
            "explicacion": "El empleado expresa comodidad con reservas.",
        }
        assert validator.is_valid(output)

    def test_empty_sentimientos_requires_empty_explicacion(self) -> None:
        # Criterio 7.6: sentimientos vacío implica explicacion vacía.
        validator = get_validator(LLM_OUTPUT_SCHEMA_FILE)
        valid = {
            "Indificador": VALID_INDIFICADOR,
            "Respuesta": "",
            "sentimientos": [],
            "explicacion": "",
        }
        invalid = {
            "Indificador": VALID_INDIFICADOR,
            "Respuesta": "",
            "sentimientos": [],
            "explicacion": "no deberia haber texto",
        }
        assert validator.is_valid(valid)
        assert not validator.is_valid(invalid)

    def test_more_than_ten_sentimientos_is_rejected(self) -> None:
        validator = get_validator(LLM_OUTPUT_SCHEMA_FILE)
        output = {
            "Indificador": VALID_INDIFICADOR,
            "Respuesta": "x",
            "sentimientos": [f"emo{i}" for i in range(11)],
            "explicacion": "demasiadas emociones",
        }
        assert not validator.is_valid(output)

    def test_duplicate_sentimientos_is_rejected(self) -> None:
        validator = get_validator(LLM_OUTPUT_SCHEMA_FILE)
        output = {
            "Indificador": VALID_INDIFICADOR,
            "Respuesta": "x",
            "sentimientos": ["satisfacción", "satisfacción"],
            "explicacion": "duplicado",
        }
        assert not validator.is_valid(output)

    def test_unknown_field_is_rejected(self) -> None:
        validator = get_validator(LLM_OUTPUT_SCHEMA_FILE)
        output = {
            "Indificador": VALID_INDIFICADOR,
            "Respuesta": "x",
            "sentimientos": ["satisfacción"],
            "explicacion": "ok",
            "extra": "no",
        }
        assert not validator.is_valid(output)


class TestItemResultValidation:
    def test_done_item_is_accepted(self) -> None:
        validator = get_validator(ITEM_RESULT_SCHEMA_FILE)
        item: dict[str, Any] = {
            "item_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00000",
            "index": 0,
            "indificador": VALID_INDIFICADOR,
            "respuesta": "me siento comodo",
            "status": "DONE",
            "language_code": "es",
            "sentimientoGeneral": "Mixto",
            "scores": {"positive": 0.62, "negative": 0.11, "neutral": 0.05, "mixed": 0.22},
            "sentimientos": ["satisfacción", "frustración"],
            "explicacion": "...",
            "model_id": "amazon.nova-lite-v1:0",
            "processed_at": "2025-06-14T10:00:21Z",
        }
        assert validator.is_valid(item)

    def test_skipped_item_is_accepted(self) -> None:
        validator = get_validator(ITEM_RESULT_SCHEMA_FILE)
        item: dict[str, Any] = {
            "item_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00001",
            "index": 1,
            "indificador": "Comunicación",
            "respuesta": "Sin comentarios",
            "status": "SKIPPED",
            "sentimientos": [],
            "explicacion": "",
        }
        assert validator.is_valid(item)

    def test_failed_item_with_error_code_is_accepted(self) -> None:
        validator = get_validator(ITEM_RESULT_SCHEMA_FILE)
        item: dict[str, Any] = {
            "item_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00002",
            "index": 2,
            "indificador": "Reconocimiento",
            "respuesta": "texto",
            "status": "FAILED",
            "error_code": "LLM_OUTPUT_INVALID",
        }
        assert validator.is_valid(item)

    def test_non_terminal_status_is_rejected(self) -> None:
        validator = get_validator(ITEM_RESULT_SCHEMA_FILE)
        item: dict[str, Any] = {
            "item_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00000",
            "index": 0,
            "indificador": VALID_INDIFICADOR,
            "respuesta": "x",
            "status": "PENDING",
        }
        assert not validator.is_valid(item)

    def test_error_code_outside_catalog_is_rejected(self) -> None:
        validator = get_validator(ITEM_RESULT_SCHEMA_FILE)
        item: dict[str, Any] = {
            "item_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00000",
            "index": 0,
            "indificador": VALID_INDIFICADOR,
            "respuesta": "x",
            "status": "FAILED",
            "error_code": "NOT_A_REAL_CODE",
        }
        assert not validator.is_valid(item)


class TestConsolidatedOutputValidation:
    def _consolidated(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "analysis_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V",
            "tenant_id": "acme-corp",
            "survey_name": "Clima 2025 Q2",
            "status": "COMPLETED",
            "item_count": 2,
            "done_count": 1,
            "failed_count": 0,
            "skipped_count": 1,
            "started_at": "2025-06-14T10:00:00Z",
            "completed_at": "2025-06-14T10:00:24Z",
            "resumen_por_indificador": {
                VALID_INDIFICADOR: {
                    "total": 1,
                    "sentimiento_general": {
                        "Positivo": 0,
                        "Negativo": 0,
                        "Neutral": 0,
                        "Mixto": 1,
                    },
                    "sentimientos_top": [{"sentimiento": "satisfacción", "count": 1}],
                }
            },
            "items": [
                {
                    "item_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00000",
                    "index": 0,
                    "indificador": VALID_INDIFICADOR,
                    "respuesta": "me siento comodo",
                    "status": "DONE",
                    "language_code": "es",
                    "sentimientoGeneral": "Mixto",
                    "scores": {
                        "positive": 0.62,
                        "negative": 0.11,
                        "neutral": 0.05,
                        "mixed": 0.22,
                    },
                    "sentimientos": ["satisfacción", "frustración"],
                    "explicacion": "...",
                    "model_id": "amazon.nova-lite-v1:0",
                    "processed_at": "2025-06-14T10:00:21Z",
                },
                {
                    "item_id": "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00001",
                    "index": 1,
                    "indificador": "Comunicación",
                    "respuesta": "Sin comentarios",
                    "status": "SKIPPED",
                    "sentimientos": [],
                    "explicacion": "",
                },
            ],
        }

    def test_design_example_is_accepted(self) -> None:
        # El ejemplo canónico de la sección 15.4 del diseño debe validar: así se
        # garantiza que el esquema y el productor (aggregate_results) coinciden.
        validator = get_validator(CONSOLIDATED_OUTPUT_SCHEMA_FILE)
        assert validator.is_valid(self._consolidated())

    def test_ref_to_item_result_is_enforced(self) -> None:
        # El $ref a item_result debe resolver y aplicar: un item con estado no
        # terminal invalida todo el documento consolidado.
        validator = get_validator(CONSOLIDATED_OUTPUT_SCHEMA_FILE)
        doc = self._consolidated()
        doc["items"][0]["status"] = "PENDING"
        assert not validator.is_valid(doc)

    def test_wrong_schema_version_is_rejected(self) -> None:
        validator = get_validator(CONSOLIDATED_OUTPUT_SCHEMA_FILE)
        doc = self._consolidated()
        doc["schema_version"] = 2
        assert not validator.is_valid(doc)

    def test_non_terminal_batch_status_is_rejected(self) -> None:
        validator = get_validator(CONSOLIDATED_OUTPUT_SCHEMA_FILE)
        doc = self._consolidated()
        doc["status"] = "RUNNING"
        assert not validator.is_valid(doc)

    def test_unknown_top_level_field_is_rejected(self) -> None:
        validator = get_validator(CONSOLIDATED_OUTPUT_SCHEMA_FILE)
        doc = self._consolidated()
        doc["unexpected"] = True
        assert not validator.is_valid(doc)
