"""Pruebas unitarias del parseo de la salida del Modelo_Bedrock (tarea 4.2).

Cubren los criterios 7.4, 7.5, 7.6, 7.7 y 7.13 sobre
:mod:`sentiment.domain.llm_parsing`:

* **Extracción tolerante (7.4):** fences ``` ```json ```, prosa alrededor,
  envoltura en array de un elemento, selección del primer candidato *conforme al
  esquema* cuando hay varios objetos.
* **Rechazo explícito (7.5):** textos sin ningún objeto conforme producen
  :class:`LlmSchemaError`.
* **Normalización (7.6):** trim, minúsculas, descarte de no-cadenas/vacíos/muy
  largos, dedup conservando el orden y tope de 10.
* **Regla de la lista vacía (7.7):** ``sentimientos`` vacía ⇒ ``explicacion``
  vacía.
* **Totalidad (7.13):** un conjunto representativo de entradas difíciles (cadena
  vacía, solo espacios, objeto truncado, comillas simples, objetos concatenados)
  termina en resultado válido o en :class:`LlmSchemaError`, nunca en otra
  excepción. La propiedad universal P12 es la tarea 4.3; aquí solo se fijan casos
  concretos.
"""

from __future__ import annotations

import json

import pytest

from sentiment.domain.errors import LlmSchemaError
from sentiment.domain.llm_parsing import (
    MAX_SCAN_CHARS,
    MAX_SENTIMIENTOS,
    extract_json_object,
    iter_candidate_objects,
    normalize_sentimientos,
    parse_llm_output,
)
from sentiment.domain.models import EmotionAnalysis

ITEM_ID = "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00007"


def _valid_object(
    sentimientos: list[str] | None = None,
    explicacion: str = "La persona expresa satisfacción con su equipo.",
) -> dict[str, object]:
    """Construye un objeto conforme al esquema ``llm_output``."""
    return {
        "Indificador": "Satisfacción en el Puesto",
        "Respuesta": "me siento comodo con el equipo",
        "sentimientos": ["alegría", "gratitud"] if sentimientos is None else sentimientos,
        "explicacion": explicacion,
    }


class TestExtractionTolerance:
    """Criterio 7.4: extracción tolerante a envoltura, fences y prosa."""

    def test_plain_object(self) -> None:
        obj = _valid_object()
        result = parse_llm_output(json.dumps(obj), ITEM_ID)
        assert result.sentimientos == ("alegría", "gratitud")

    def test_markdown_fenced_object(self) -> None:
        raw = "```json\n" + json.dumps(_valid_object()) + "\n```"
        result = parse_llm_output(raw, ITEM_ID)
        assert result.sentimientos == ("alegría", "gratitud")

    def test_prose_before_and_after(self) -> None:
        raw = (
            "Claro, aquí tienes el análisis solicitado:\n"
            + json.dumps(_valid_object())
            + "\nEspero que sea de utilidad."
        )
        result = parse_llm_output(raw, ITEM_ID)
        assert result.explicacion != ""

    def test_single_element_array_wrapping(self) -> None:
        raw = "[" + json.dumps(_valid_object()) + "]"
        result = parse_llm_output(raw, ITEM_ID)
        assert result.sentimientos == ("alegría", "gratitud")

    def test_first_schema_valid_candidate_is_adopted(self) -> None:
        # Un primer objeto que NO cumple el esquema (campos ausentes) seguido de
        # uno que sí. Debe adoptarse el segundo (criterio 7.4).
        non_conforming = {"foo": "bar"}
        raw = json.dumps(non_conforming) + "\n" + json.dumps(_valid_object(["enojo"]))
        result = parse_llm_output(raw, ITEM_ID)
        assert result.sentimientos == ("enojo",)

    def test_concatenated_objects_scanned_in_order(self) -> None:
        first = _valid_object(["calma"])
        second = _valid_object(["euforia"])
        raw = json.dumps(first) + json.dumps(second)
        result = parse_llm_output(raw, ITEM_ID)
        assert result.sentimientos == ("calma",)

    def test_braces_inside_string_values_do_not_break_scan(self) -> None:
        obj = _valid_object(explicacion="usa plantillas como {nombre} y {empresa}")
        result = parse_llm_output(json.dumps(obj), ITEM_ID)
        assert "{nombre}" in result.explicacion


class TestRejection:
    """Criterio 7.5: ningún objeto conforme ⇒ LlmSchemaError."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   \n\t ",
            "no hay ningún objeto json aquí",
            '{"Indificador": "x", "Respuesta": "y"',  # truncado
            "{'sentimientos': ['a']}",  # comillas simples: no parsea
            json.dumps({"foo": "bar"}),  # parsea pero no cumple el esquema
        ],
    )
    def test_rejects_as_llm_schema_error(self, raw: str) -> None:
        with pytest.raises(LlmSchemaError) as excinfo:
            parse_llm_output(raw, ITEM_ID)
        assert excinfo.value.error_code == "LLM_OUTPUT_INVALID"

    def test_schema_violating_object_is_rejected(self) -> None:
        # explicacion no vacía con sentimientos vacíos viola la regla del esquema
        # (criterio 7.6 en el esquema): el objeto no es conforme y, al ser el
        # único candidato, el parseo falla.
        obj = {
            "Indificador": "x",
            "Respuesta": "y",
            "sentimientos": [],
            "explicacion": "no debería tener texto",
        }
        with pytest.raises(LlmSchemaError):
            parse_llm_output(json.dumps(obj), ITEM_ID)


class TestNormalization:
    """Criterio 7.6: normalización de la lista ``sentimientos``."""

    def test_trim_and_lowercase(self) -> None:
        assert normalize_sentimientos(["  Alegría  ", "GRATITUD"]) == (
            "alegría",
            "gratitud",
        )

    def test_discards_non_strings_and_empty(self) -> None:
        assert normalize_sentimientos(["ok", 42, None, "   ", ""]) == ("ok",)

    def test_discards_over_length(self) -> None:
        long_value = "x" * 41
        assert normalize_sentimientos([long_value, "válido"]) == ("válido",)

    def test_dedup_preserves_first_occurrence(self) -> None:
        assert normalize_sentimientos(["calma", "Calma", "CALMA", "miedo"]) == (
            "calma",
            "miedo",
        )

    def test_caps_at_ten(self) -> None:
        many = [f"emo{i:02d}" for i in range(20)]
        result = normalize_sentimientos(many)
        assert len(result) == MAX_SENTIMIENTOS
        assert result == tuple(f"emo{i:02d}" for i in range(MAX_SENTIMIENTOS))

    def test_non_list_input_yields_empty(self) -> None:
        assert normalize_sentimientos("alegría") == ()
        assert normalize_sentimientos(None) == ()

    def test_normalization_applied_end_to_end(self) -> None:
        obj = _valid_object(["  Alegría ", "alegria", "ALEGRÍA", "gratitud"])
        result = parse_llm_output(json.dumps(obj), ITEM_ID)
        # "alegria" (sin tilde) y "alegría" (con tilde) son distintos.
        assert result.sentimientos == ("alegría", "alegria", "gratitud")


class TestEmptyListRule:
    """Criterio 7.7: ``sentimientos`` normalizada vacía ⇒ ``explicacion`` vacía."""

    def test_empty_sentimientos_forces_empty_explicacion(self) -> None:
        obj = _valid_object(sentimientos=[], explicacion="")
        result = parse_llm_output(json.dumps(obj), ITEM_ID)
        assert result.sentimientos == ()
        assert result.explicacion == ""

    def test_sentimientos_normalized_to_empty_forces_empty_explicacion(self) -> None:
        # El objeto cumple el esquema (minLength 2) pero tras normalizar todos los
        # elementos se descartan por exceder 40 caracteres; la lista queda vacía y
        # la explicación debe vaciarse pese a traer texto.
        obj = _valid_object(
            sentimientos=["x" * 50, "y" * 45],
            explicacion="había texto que debe desaparecer",
        )
        result = parse_llm_output(json.dumps(obj), ITEM_ID)
        assert result.sentimientos == ()
        assert result.explicacion == ""


class TestResultShape:
    """El resultado es un ``EmotionAnalysis`` con los metadatos aportados."""

    def test_metadata_is_carried_through(self) -> None:
        obj = _valid_object(["alegría"])
        result = parse_llm_output(
            json.dumps(obj),
            ITEM_ID,
            model_id="anthropic.claude-3-sonnet",
            input_tokens=120,
            output_tokens=45,
        )
        assert isinstance(result, EmotionAnalysis)
        assert result.item_id == ITEM_ID
        assert result.model_id == "anthropic.claude-3-sonnet"
        assert result.input_tokens == 120
        assert result.output_tokens == 45
        assert isinstance(result.sentimientos, tuple)

    def test_default_metadata_is_neutral(self) -> None:
        result = parse_llm_output(json.dumps(_valid_object(["calma"])), ITEM_ID)
        assert result.model_id == ""
        assert result.input_tokens == 0
        assert result.output_tokens == 0


class TestScanWindow:
    """Criterio 7.4: solo se inspeccionan los primeros 100.000 caracteres."""

    def test_object_beyond_window_is_ignored(self) -> None:
        padding = "." * MAX_SCAN_CHARS
        raw = padding + json.dumps(_valid_object(["alegría"]))
        with pytest.raises(LlmSchemaError):
            parse_llm_output(raw, ITEM_ID)

    def test_object_within_window_is_found(self) -> None:
        padding = "." * (MAX_SCAN_CHARS - 500)
        raw = padding + json.dumps(_valid_object(["alegría"]))
        result = parse_llm_output(raw, ITEM_ID)
        assert result.sentimientos == ("alegría",)


class TestExtractJsonObject:
    """El auxiliar ``extract_json_object`` devuelve el primer objeto parseable."""

    def test_returns_first_parseable_object(self) -> None:
        raw = "ruido " + json.dumps({"a": 1}) + " más ruido"
        assert extract_json_object(raw) == {"a": 1}

    def test_raises_when_no_object(self) -> None:
        with pytest.raises(LlmSchemaError):
            extract_json_object("sin json")

    def test_iter_candidate_objects_is_lazy_and_ordered(self) -> None:
        raw = json.dumps({"a": 1}) + json.dumps({"b": 2})
        candidates = list(iter_candidate_objects(raw))
        assert candidates == [{"a": 1}, {"b": 2}]
