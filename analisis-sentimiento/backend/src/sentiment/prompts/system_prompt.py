"""Carga y derivación del ``system_prompt`` del Modelo_Bedrock.

El texto base del prompt vive versionado en :data:`SYSTEM_PROMPT_TEMPLATE_PATH`
(``system_prompt_v1.md``) con dos marcadores que se resuelven en tiempo de
ejecución, de forma que ni la lista de Indificadores ni el catálogo de
sentimientos se dupliquen en el texto del prompt (Requirement 17.13, 17.14):

* ``@@INDIFICADORES`` -> derivado de :mod:`sentiment.prompts.indificadores`
  (fuente única de los 11 Indificadores).
* ``@@LIST_OF_SENTIMENTS`` -> derivado de
  :mod:`sentiment.prompts.sentiment_catalog`.

:func:`build_system_prompt` reproduce, sin cambios semánticos, la misma
sustitución que hacía el código legado (dos ``str.replace`` encadenados sobre el
texto del prompt).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from sentiment.prompts.indificadores import as_prompt_fragment
from sentiment.prompts.sentiment_catalog import LIST_OF_SENTIMENTS

# Marcadores de sustitución, idénticos a los del prompt legado.
INDIFICADORES_PLACEHOLDER: Final[str] = "@@INDIFICADORES"
SENTIMENTS_PLACEHOLDER: Final[str] = "@@LIST_OF_SENTIMENTS"

# Ruta del archivo versionado con el texto base del prompt (v1).
SYSTEM_PROMPT_TEMPLATE_PATH: Final[Path] = Path(__file__).parent / "system_prompt_v1.md"


def load_system_prompt_template() -> str:
    """Lee el texto base del ``system_prompt`` versionado.

    Returns:
        El contenido completo de ``system_prompt_v1.md``, con los marcadores
        ``@@INDIFICADORES`` y ``@@LIST_OF_SENTIMENTS`` todavía sin resolver.

    Raises:
        FileNotFoundError: si el archivo versionado del prompt no existe.
    """
    return SYSTEM_PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")


def build_system_prompt() -> str:
    """Construye el ``system_prompt`` final resolviendo sus marcadores.

    Deriva el prompt desde las fuentes únicas: incrusta los 11 Indificadores
    (en la grafía ``indificador`` conservada) y el catálogo de sentimientos.
    Reproduce exactamente la sustitución del código legado, sin alterar la
    semántica del texto enviado al Modelo_Bedrock.

    Returns:
        El ``system_prompt`` completo, listo para enviarse al modelo.
    """
    template = load_system_prompt_template()
    return template.replace(
        INDIFICADORES_PLACEHOLDER, as_prompt_fragment()
    ).replace(SENTIMENTS_PLACEHOLDER, LIST_OF_SENTIMENTS)
