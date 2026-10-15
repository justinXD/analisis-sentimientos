"""Detección del Marcador_Vacio: respuestas sin contenido analizable.

Un **Marcador_Vacio** es un valor de ``respuesta`` que el Sistema considera sin
contenido analizable y que, por tanto, no se envía ni a Amazon Comprehend ni al
Modelo_Bedrock: el Ítem se transiciona directamente a ``SKIPPED`` (criterios 6.6
y 18.6). El glosario de los requisitos fija el conjunto cerrado de marcadores, a
comparar **tras normalizar la ``respuesta`` a minúsculas y recortar sus espacios**:
cadena vacía, ``sin comentarios``, ``n/a``, ``ninguno`` y ``-``.

Este módulo es la **fuente única** de esa regla, de modo que la capa de servicio
(``AnalyzeBatchSentimentService``) y cualquier otro consumidor compartan la misma
definición y no la dupliquen con riesgo de divergencia.

Pureza (Requirement 17.2): el módulo sólo usa la biblioteca estándar; es
importable sin el SDK de AWS y no importa adaptadores ni servicios.
"""

from __future__ import annotations

from typing import Final

__all__ = ["EMPTY_MARKERS", "is_empty_marker"]

#: Conjunto cerrado de Marcadores_Vacios en su forma ya normalizada (minúsculas,
#: sin espacios inicial ni final). Es exactamente el enumerado por el glosario de
#: ``requirements.md``. La cadena vacía representa tanto una ``respuesta`` vacía
#: como una formada únicamente por espacios tras el recorte.
EMPTY_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "",
        "sin comentarios",
        "n/a",
        "ninguno",
        "-",
    }
)


def is_empty_marker(respuesta: str) -> bool:
    """Indica si una ``respuesta`` es un Marcador_Vacio (sin contenido analizable).

    Normaliza la entrada a minúsculas y recorta sus espacios inicial y final, y
    la compara contra el conjunto cerrado :data:`EMPTY_MARKERS`. La comparación en
    minúsculas hace que, por ejemplo, ``"Sin Comentarios"`` o ``"  N/A "`` también
    se reconozcan como marcadores (criterios 6.6, 18.6).

    La función es **total**: para cualquier cadena devuelve ``True`` o ``False``
    sin lanzar nunca una excepción.

    :param respuesta: el texto de la respuesta del Ítem, ya validado como cadena
        por la capa de ingesta.
    :return: ``True`` si la respuesta carece de contenido analizable y el Ítem
        debe transicionar a ``SKIPPED``; ``False`` si debe analizarse.
    """
    return respuesta.strip().lower() in EMPTY_MARKERS
