"""Prompt del sistema, catálogo de sentimientos y lista de Indificadores versionados.

Punto de entrada del paquete de prompts. Reexporta la fuente única de los
Indificadores (:mod:`sentiment.prompts.indificadores`), el catálogo de
sentimientos (:mod:`sentiment.prompts.sentiment_catalog`) y la construcción del
``system_prompt`` derivado (:mod:`sentiment.prompts.system_prompt`).
"""

from sentiment.prompts.indificadores import (
    EXPECTED_INDIFICADOR_COUNT,
    INDIFICADORES,
    as_prompt_fragment,
    enum_values,
    is_valid_indificador,
)
from sentiment.prompts.sentiment_catalog import LIST_OF_SENTIMENTS
from sentiment.prompts.system_prompt import (
    build_system_prompt,
    load_system_prompt_template,
)

__all__ = [
    "EXPECTED_INDIFICADOR_COUNT",
    "INDIFICADORES",
    "LIST_OF_SENTIMENTS",
    "as_prompt_fragment",
    "build_system_prompt",
    "enum_values",
    "is_valid_indificador",
    "load_system_prompt_template",
]
