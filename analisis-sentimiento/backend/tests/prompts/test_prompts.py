"""Pruebas del paquete de prompts versionados (tarea 1.3).

Verifican que:

* la lista de Indificadores es la fuente única con los 11 valores verbatim y la
  grafía ``indificador`` conservada (Requirement 17.14, 17.15);
* el enum del JSON_Schema y el fragmento del prompt derivan de esa fuente única
  (Requirement 17.14);
* el ``system_prompt`` versionado se resuelve sin dejar marcadores y conserva su
  contenido verbatim respecto del código legado (Requirement 17.13).
"""

from __future__ import annotations

from sentiment.prompts import (
    EXPECTED_INDIFICADOR_COUNT,
    INDIFICADORES,
    LIST_OF_SENTIMENTS,
    as_prompt_fragment,
    build_system_prompt,
    enum_values,
    is_valid_indificador,
    load_system_prompt_template,
)

# Los 11 Indificadores esperados, en el orden canónico del código legado.
EXPECTED_INDIFICADORES = (
    "Calidad de Vida y Trabajo",
    "Compensación y Beneficios",
    "Comunicación",
    "Confianza en la Organización",
    "Desarrollo Profesional",
    "Efectividad de Liderazgo",
    "Entorno Físico de Trabajo",
    "Espíritu de Equipo y Colaboración",
    "Identidad y Compromiso",
    "Reconocimiento",
    "Satisfacción en el Puesto",
)


class TestIndificadoresSource:
    def test_has_exactly_eleven_values(self) -> None:
        assert len(INDIFICADORES) == EXPECTED_INDIFICADOR_COUNT == 11

    def test_values_are_verbatim_and_ordered(self) -> None:
        assert INDIFICADORES == EXPECTED_INDIFICADORES

    def test_values_are_unique(self) -> None:
        assert len(set(INDIFICADORES)) == len(INDIFICADORES)

    def test_indificador_spelling_is_preserved(self) -> None:
        # Grafía 'indificador' conservada deliberadamente (Requirement 17.15):
        # el valor con acento usa 'Satisfacción' pero el término de dominio es
        # 'indificador', no 'indicador'. Verificamos presencia del acento y de
        # la grafía exacta de un valor representativo.
        assert "Comunicación" in INDIFICADORES
        assert "Confianza en la Organización" in INDIFICADORES


class TestEnumDerivation:
    def test_enum_values_match_single_source(self) -> None:
        assert enum_values() == list(INDIFICADORES)

    def test_enum_values_returns_fresh_copy(self) -> None:
        first = enum_values()
        first.append("mutado")
        assert enum_values() == list(INDIFICADORES)

    def test_is_valid_indificador_is_case_sensitive(self) -> None:
        assert is_valid_indificador("Comunicación") is True
        assert is_valid_indificador("comunicación") is False
        assert is_valid_indificador("Desconocido") is False


class TestPromptFragment:
    def test_fragment_contains_every_indificador_quoted(self) -> None:
        fragment = as_prompt_fragment()
        for indificador in INDIFICADORES:
            assert f"'{indificador}'" in fragment

    def test_fragment_matches_legacy_format(self) -> None:
        # Reproduce exactamente el bloque @@INDIFICADORES del prompt legado.
        joined = ",".join(f"'{i}'" for i in EXPECTED_INDIFICADORES)
        assert as_prompt_fragment() == f"\n{joined}\n"


class TestSystemPrompt:
    def test_template_still_contains_placeholders(self) -> None:
        template = load_system_prompt_template()
        assert "@@INDIFICADORES" in template
        assert "@@LIST_OF_SENTIMENTS" in template

    def test_built_prompt_resolves_all_placeholders(self) -> None:
        prompt = build_system_prompt()
        assert "@@INDIFICADORES" not in prompt
        assert "@@LIST_OF_SENTIMENTS" not in prompt

    def test_built_prompt_embeds_sources(self) -> None:
        prompt = build_system_prompt()
        assert as_prompt_fragment() in prompt
        assert LIST_OF_SENTIMENTS in prompt

    def test_built_prompt_preserves_legacy_text(self) -> None:
        prompt = build_system_prompt()
        # Fragmentos verbatim del prompt legado (sin cambios semánticos).
        assert "Eres un agente especializado en la identificación de emociones" in prompt
        # En el prompt legado la clave 'sentimientos' del bloque de estructura
        # de salida NO va entrecomillada; se conserva verbatim.
        assert "sentimientos: [\"sentimiento1\", \"sentimiento2\", \"sentimiento3\"]" in prompt
        assert 'Si el atributo "Respuesta" esta vacio, es nulo o "Sin comentarios"' in prompt
