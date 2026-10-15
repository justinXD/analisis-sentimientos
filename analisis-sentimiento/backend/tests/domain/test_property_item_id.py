"""Property 7 — Determinismo y conservación del orden de ``item_id`` (tarea 3.5).

**Validates: Requirements 4.5, 8.6, 10.6, 12.7**

Enunciado (design.md, sección 19, Property 7):

    ``item_id`` es función pura de ``(analysis_id, index)``, y ordenar los
    ítems por su clave de ordenación reproduce el orden del array original.

Esta propiedad es la base de dos garantías del Sistema:

* **Idempotencia (criterios 8.6, 4.5).** Como :func:`make_item_id` es una función
  pura y determinista de ``(analysis_id, index)`` —sin aleatoriedad ni reloj—, el
  reproceso de un mismo Lote deriva exactamente los mismos ``item_id``. Esa es la
  razón por la que la escritura condicionada en DynamoDB es idempotente sin una
  tabla auxiliar de idempotencia (diseño 7.3).

* **Conservación del orden original (criterios 10.6, 12.7).** El relleno a cinco
  dígitos del índice (``{index:05d}``) hace que el orden *lexicográfico* de los
  ``item_id`` —y por tanto de las sort keys ``ITEM#{index:05d}`` de DynamoDB—
  coincida con el orden *numérico* del índice. Así el Documento_Consolidado puede
  listar los ítems en el orden del array ``items`` enviado por el cliente con solo
  ordenar por clave, sin almacenar el orden explícitamente.

Las estrategias generan ULID canónicos a partir de 16 bytes aleatorios
(:meth:`ulid.ULID.from_bytes`), lo que cubre todo el espacio de 128 bits y produce
siempre identificadores que :func:`is_valid_ulid` acepta, e índices en el rango
válido ``[0, MAX_INDEX]`` acotado por el enunciado a ``[0, 1000]`` para los casos
de ordenación por lote.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from ulid import ULID

from sentiment.domain.ids import (
    INDEX_PADDING,
    ITEM_ID_SEPARATOR,
    MAX_INDEX,
    is_valid_ulid,
    make_item_id,
)

# --- Estrategias -----------------------------------------------------------

# Un ULID canónico de 26 caracteres derivado de 16 bytes aleatorios. Construirlo
# desde los bytes (en lugar de generar la cadena base32 directamente) garantiza
# que cada ejemplo es un ULID válido que `is_valid_ulid` acepta, cubriendo todo
# el espacio de 128 bits sin producir cadenas malformadas.
ulids = st.binary(min_size=16, max_size=16).map(lambda b: str(ULID.from_bytes(b)))

# Índices en el rango acotado por el enunciado de la tarea.
indices = st.integers(min_value=0, max_value=1000)

# Índices en todo el rango representable con el padding, para la invariante de
# determinismo pura (que debe valer también en los extremos del dominio).
indices_full_range = st.integers(min_value=0, max_value=MAX_INDEX)


def _recover_index(item_id: str) -> int:
    """Recupera el ``index`` numérico codificado en la cola de un ``item_id``.

    Es la operación inversa de :func:`make_item_id` sobre el campo de índice:
    separa por :data:`ITEM_ID_SEPARATOR` y reinterpreta los ``INDEX_PADDING``
    dígitos finales como entero en base 10.
    """
    _, _, suffix = item_id.partition(ITEM_ID_SEPARATOR)
    return int(suffix)


# --- (a) Determinismo ------------------------------------------------------


@given(analysis_id=ulids, index=indices_full_range)
def test_item_id_is_deterministic(analysis_id: str, index: int) -> None:
    """Llamadas repetidas con los mismos argumentos producen el mismo ``item_id``.

    Es la invariante central de P7: :func:`make_item_id` es una función pura, sin
    estado ni aleatoriedad, por lo que es la base de la idempotencia del reproceso
    (criterios 4.5, 8.6).
    """
    assert make_item_id(analysis_id, index) == make_item_id(analysis_id, index)


# --- (b) Inyectividad en el índice ----------------------------------------


@given(
    analysis_id=ulids,
    pair=st.lists(indices_full_range, min_size=2, max_size=2, unique=True),
)
def test_distinct_indices_yield_distinct_item_ids(
    analysis_id: str, pair: list[int]
) -> None:
    """Índices distintos del mismo Lote derivan ``item_id`` distintos.

    Sin esta inyectividad dos ítems del mismo Lote colisionarían en la sort key y
    uno sobrescribiría al otro, perdiendo un ítem (contra la conservación de
    ítems, criterio 10.6).
    """
    first, second = pair
    assert make_item_id(analysis_id, first) != make_item_id(analysis_id, second)


# --- (c) El orden lexicográfico coincide con el orden numérico -------------


@given(
    analysis_id=ulids,
    answer_count=st.integers(min_value=2, max_value=200),
)
def test_lexicographic_order_matches_numeric_index_order(
    analysis_id: str, answer_count: int
) -> None:
    """Ordenar los ``item_id`` lexicográficamente reproduce el orden del array.

    Modela el array ``items`` original como los índices ``0..answer_count-1``.
    Al derivar sus ``item_id``, barajarlos y reordenarlos por el valor de cadena
    (lo que hace DynamoDB con la sort key), el orden recuperado debe ser el orden
    numérico original. Esto es lo que permite reconstruir el orden del array sin
    almacenarlo (criterios 10.6, 12.7).
    """
    indices_originales = list(range(answer_count))
    item_ids = [make_item_id(analysis_id, i) for i in indices_originales]

    # Ordenar por la cadena del `item_id` (orden lexicográfico, el de la sort key).
    orden_recuperado = [_recover_index(iid) for iid in sorted(item_ids)]

    assert orden_recuperado == indices_originales


# --- (d) El índice se recupera (round-trip) --------------------------------


@given(analysis_id=ulids, index=indices)
def test_item_id_round_trips_to_recover_index(analysis_id: str, index: int) -> None:
    """El ``item_id`` conserva el ``index`` original y es recuperable sin pérdida.

    Comprueba además la forma del ``item_id``: ``{analysis_id}#{index:05d}``, con
    el ``analysis_id`` canónico (mayúsculas), un único separador y exactamente
    :data:`INDEX_PADDING` dígitos de índice. La reversibilidad del índice es lo
    que hace que ordenar por clave recupere el orden del array (criterios 10.6,
    12.7).
    """
    item_id = make_item_id(analysis_id, index)

    prefix, sep, suffix = item_id.partition(ITEM_ID_SEPARATOR)

    assert sep == ITEM_ID_SEPARATOR
    assert prefix == analysis_id.upper()
    assert is_valid_ulid(prefix)
    assert len(suffix) == INDEX_PADDING
    assert suffix.isdigit()
    assert _recover_index(item_id) == index
