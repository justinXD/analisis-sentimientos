"""Fuente única (single source of truth) de los Indificadores del Sistema.

Un `indificador` es la categoría de la pregunta de encuesta a la que responde un
Ítem. La grafía ``indificador`` se conserva deliberadamente por compatibilidad
con el prompt y los datos existentes (Requirement 17.15); no debe "corregirse" a
``indicador``.

Este módulo es la **única** definición de la lista de los 11 Indificadores
admitidos (Requirement 17.14). De él derivan, sin volver a declararse en ningún
otro sitio:

* el ``enum`` cerrado del campo ``indificador`` en el JSON_Schema
  ``submit_request`` (ver :func:`enum_values`), y
* el bloque de Indificadores incrustado en el ``system_prompt`` del
  Modelo_Bedrock (ver :func:`as_prompt_fragment` y el módulo
  :mod:`sentiment.prompts.system_prompt`).

El orden de la tupla se conserva verbatim respecto del código legado para no
alterar el enum ni la redacción del prompt (sin cambios semánticos).
"""

from __future__ import annotations

from typing import Final

# Lista verbatim de los 11 Indificadores, conservando acentos y grafía original.
# ESTA es la fuente de verdad. Cualquier otra representación (enum del JSON
# Schema, fragmento del prompt) debe derivarse de aquí, nunca duplicarse.
INDIFICADORES: Final[tuple[str, ...]] = (
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

# Cardinalidad esperada del enum cerrado (Glosario: "El enum cerrado tiene 11
# valores"). Se expone como constante para que las comprobaciones y las pruebas
# fallen de forma explícita si la lista cambia de tamaño por accidente.
EXPECTED_INDIFICADOR_COUNT: Final[int] = 11

# Invariante de carga del módulo: la fuente única debe tener exactamente 11
# valores distintos. Falla rápido y explícito (fail-fast) si se edita mal.
assert len(INDIFICADORES) == EXPECTED_INDIFICADOR_COUNT, (
    "La fuente única de Indificadores debe tener exactamente "
    f"{EXPECTED_INDIFICADOR_COUNT} valores; tiene {len(INDIFICADORES)}."
)
assert len(set(INDIFICADORES)) == len(INDIFICADORES), (
    "La fuente única de Indificadores no debe contener valores duplicados."
)


def enum_values() -> list[str]:
    """Devuelve los Indificadores como lista para el ``enum`` del JSON_Schema.

    Es el punto de derivación del enum cerrado del campo ``indificador`` del
    JSON_Schema ``submit_request`` (Requirement 17.14). Devuelve una copia nueva
    en cada llamada para que el consumidor no pueda mutar la fuente única.

    Returns:
        Lista de los 11 valores admitidos, en el orden canónico de
        :data:`INDIFICADORES`.
    """
    return list(INDIFICADORES)


def as_prompt_fragment() -> str:
    """Devuelve los Indificadores en la grafía exacta que espera el prompt.

    El ``system_prompt`` legado incrustaba la lista como una secuencia de
    literales entre comillas simples separados por comas. Se reproduce ese mismo
    formato, delimitado por saltos de línea, para derivar el prompt desde esta
    fuente única sin alterar su semántica (Requirement 17.13, 17.14).

    Returns:
        Fragmento de texto con los 11 Indificadores entrecomillados y separados
        por comas, idéntico al bloque ``@@INDIFICADORES`` del prompt legado.
    """
    joined = ",".join(f"'{indificador}'" for indificador in INDIFICADORES)
    return f"\n{joined}\n"


def is_valid_indificador(value: str) -> bool:
    """Indica si ``value`` pertenece al enum cerrado de Indificadores.

    La comparación es sensible a mayúsculas y minúsculas, conforme al criterio
    3.8 de los requisitos.

    Args:
        value: Valor candidato del campo ``indificador``.

    Returns:
        ``True`` si ``value`` es exactamente uno de los 11 Indificadores.
    """
    return value in INDIFICADORES
