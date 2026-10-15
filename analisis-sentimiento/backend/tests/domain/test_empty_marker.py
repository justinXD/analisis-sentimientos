"""Pruebas unitarias de la detección del Marcador_Vacio (tarea 7.3).

Verifican que :func:`sentiment.domain.empty_marker.is_empty_marker` reconoce
exactamente el conjunto cerrado del glosario de ``requirements.md``, tras
normalizar a minúsculas y recortar espacios (criterios 6.6, 18.6), y que es total
(no lanza) para cualquier cadena.
"""

from __future__ import annotations

import pytest

from sentiment.domain.empty_marker import EMPTY_MARKERS, is_empty_marker


@pytest.mark.parametrize(
    "respuesta",
    [
        "",
        "   ",
        "\t\n ",
        "sin comentarios",
        "Sin comentarios",
        "SIN COMENTARIOS",
        "  Sin Comentarios  ",
        "n/a",
        "N/A",
        " n/a ",
        "ninguno",
        "NINGUNO",
        "-",
        " - ",
    ],
)
def test_recognized_empty_markers(respuesta: str) -> None:
    assert is_empty_marker(respuesta) is True


@pytest.mark.parametrize(
    "respuesta",
    [
        "me gusta el liderazgo",
        "no",  # 'no' NO es un marcador del conjunto cerrado
        "sin comentario",  # singular: no pertenece al conjunto
        "na",  # sin la barra
        "ningún comentario",
        "n / a",
        "--",
    ],
)
def test_analyzable_responses_are_not_markers(respuesta: str) -> None:
    assert is_empty_marker(respuesta) is False


def test_empty_markers_set_is_exactly_the_glossary() -> None:
    """El conjunto cerrado coincide con el glosario de los requisitos."""
    assert frozenset({"", "sin comentarios", "n/a", "ninguno", "-"}) == EMPTY_MARKERS
