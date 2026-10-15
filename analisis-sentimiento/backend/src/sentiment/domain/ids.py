"""Generación y validación de los identificadores del dominio.

Este módulo es la **fuente única** de las reglas de nomenclatura de la
[sección 7 del diseño](../../../.kiro/specs/serverless-sentiment-pipeline/design.md).
Define cómo nace el identificador de un Lote (`analysis_id`), cómo se deriva el
identificador de cada Ítem (`item_id`) y cómo se valida sintácticamente un ULID.

Pertenece a la capa de dominio y es **puro**: no importa ``boto3`` ni ningún
adaptador, y es importable sin que el SDK de AWS esté instalado (criterio 17.2).
La única dependencia de terceros es ``python-ulid``, que se empaqueta en la
Lambda Layer compartida.

Reglas que este módulo materializa:

- **`analysis_id` — ULID de 26 caracteres (criterios 4.6, 4.7).** Un ULID
  codifica 48 bits de timestamp en milisegundos más 80 bits de aleatoriedad
  criptográfica, y se representa como 26 caracteres en Crockford base32. Se
  genera **del lado del servidor** a partir del reloj del sistema y de un CSPRNG;
  es, por construcción, independiente de todo dato controlado por el cliente
  (criterio 4.7). Es ordenable temporalmente —lo que hace que el `gsi1sk` ordene
  naturalmente— y no enumerable.

- **`item_id` — compuesto determinista (criterios 4.5, 8.6).** Se deriva como
  ``{analysis_id}#{index:05d}``: el `analysis_id` del Lote, el separador ``#`` y
  el `index` 0-based del Ítem rellenado a cinco dígitos. Al no ser aleatorio, un
  reproceso del mismo Lote produce exactamente los mismos `item_id`, lo que da
  idempotencia natural en la escritura sin tabla auxiliar (diseño 7.3). El padding
  a cinco dígitos es necesario porque DynamoDB ordena las sort keys
  lexicográficamente, y soporta hasta 100.000 Ítems por Lote, muy por encima del
  límite de 1.000 que impone la validación de ingesta.

- **`is_valid_ulid` — validación sintáctica total.** Decide si una cadena es un
  ULID bien formado sin lanzar nunca una excepción. Lo usa la capa de API para
  tratar un `analysis_id` malformado igual que uno inexistente o ajeno: un 404
  idéntico byte a byte, de modo que "no existe", "no es tuyo" y "está mal escrito"
  sean indistinguibles para un atacante (criterio 2.7, Property 6).

La generación del `analysis_id` se expone como una función concreta
(:func:`new_analysis_id`) que los servicios inyectan como ``id_factory`` para
poder sustituirla por una semilla determinista en las pruebas (ver, p. ej., el
caso de uso `submit_analysis`). Este módulo provee la implementación concreta;
no fuerza su uso directo desde la lógica de negocio.
"""

from __future__ import annotations

from typing import Final

from ulid import ULID

__all__ = [
    "ULID_LENGTH",
    "INDEX_PADDING",
    "MAX_INDEX",
    "ITEM_ID_SEPARATOR",
    "new_analysis_id",
    "make_item_id",
    "is_valid_ulid",
]

# Longitud canónica de un ULID codificado en Crockford base32 (128 bits).
ULID_LENGTH: Final = 26

# Separador entre el `analysis_id` y el índice dentro de un `item_id`. Coincide
# con el separador de las claves de DynamoDB; nunca aparece dentro de un ULID
# porque '#' no pertenece al alfabeto Crockford base32.
ITEM_ID_SEPARATOR: Final = "#"

# Ancho del campo de índice en el `item_id` (criterio 4.5: ``{index:05d}``).
INDEX_PADDING: Final = 5

# Índice máximo representable con el padding elegido. Cinco dígitos cubren de 0 a
# 99.999 (100.000 Ítems), holgadamente por encima del límite de 1.000 Ítems por
# Lote de la validación de ingesta (diseño 7.3).
MAX_INDEX: Final = 10**INDEX_PADDING - 1


def new_analysis_id() -> str:
    """Genera un `analysis_id` nuevo como ULID de 26 caracteres.

    El ULID se construye a partir del reloj del sistema (48 bits de timestamp en
    milisegundos) y de aleatoriedad criptográfica (80 bits), por lo que el valor
    es independiente de cualquier dato controlado por el cliente (criterios 4.6,
    4.7). Dos invocaciones distintas producen identificadores distintos.

    Es la implementación concreta que los servicios inyectan como ``id_factory``;
    inyectarla permite sustituirla por una fábrica determinista en las pruebas.

    :return: un ULID de 26 caracteres en Crockford base32 (mayúsculas).
    """
    return str(ULID())


def make_item_id(analysis_id: str, index: int) -> str:
    """Deriva el `item_id` determinista de un Ítem a partir de su Lote e índice.

    Aplica la regla ``{analysis_id}#{index:05d}`` (criterio 4.5). Al ser una
    función pura de ``(analysis_id, index)``, un mismo par produce siempre el
    mismo `item_id`, lo que hace idempotente el reproceso de un Lote sin tabla
    auxiliar de idempotencia (criterio 8.6, diseño 7.3).

    :param analysis_id: identificador del Lote. Debe ser un ULID sintácticamente
        válido; de lo contrario la derivación no tendría sentido y se rechaza.
    :param index: posición 0-based del Ítem en el array ``items`` original. Debe
        estar comprendida entre 0 y :data:`MAX_INDEX` (100.000 Ítems).
    :return: el `item_id` compuesto, p. ej. ``01JDQZ...6T9V#00042``.
    :raises ValueError: si ``analysis_id`` no es un ULID válido o si ``index``
        está fuera del rango representable.
    """
    if not is_valid_ulid(analysis_id):
        raise ValueError(
            "El 'analysis_id' debe ser un ULID de 26 caracteres válido para "
            "derivar un 'item_id'."
        )
    if index < 0 or index > MAX_INDEX:
        raise ValueError(
            f"El 'index' de un Ítem debe estar entre 0 y {MAX_INDEX} inclusive; "
            f"se recibió {index}."
        )
    # El `analysis_id` se normaliza a su forma canónica en mayúsculas para que el
    # `item_id` resultante sea estable con independencia de la caja de entrada.
    canonical_analysis_id = analysis_id.upper()
    return f"{canonical_analysis_id}{ITEM_ID_SEPARATOR}{index:0{INDEX_PADDING}d}"


def is_valid_ulid(value: object) -> bool:
    """Indica si ``value`` es un ULID de 26 caracteres sintácticamente válido.

    La validación es **total**: para cualquier entrada —incluidos valores que no
    son cadenas— devuelve ``True`` o ``False`` sin lanzar nunca una excepción.
    Esto permite que la capa de API trate un `analysis_id` malformado igual que
    uno inexistente, devolviendo un 404 idéntico (criterio 2.7, Property 6).

    Una cadena es un ULID válido si tiene exactamente 26 caracteres del alfabeto
    Crockford base32 y no desborda los 128 bits (el primer carácter codifica solo
    3 bits útiles del timestamp, por lo que debe estar en el rango ``0`` a ``7``).
    La comprobación de estas tres condiciones se delega en el decodificador de
    ``python-ulid``, que es la autoridad canónica.

    La validación es insensible a mayúsculas/minúsculas, conforme a la
    especificación Crockford base32 (``01ARZ...`` y ``01arz...`` denotan el mismo
    valor). La generación propia del Sistema produce siempre mayúsculas.

    :param value: cualquier objeto; típicamente el `analysis_id` recibido en la
        ruta de una solicitud HTTP.
    :return: ``True`` si ``value`` es un ULID bien formado; ``False`` en cualquier
        otro caso.
    """
    if not isinstance(value, str):
        return False
    if len(value) != ULID_LENGTH:
        return False
    try:
        ULID.from_str(value.upper())
    except (ValueError, TypeError):
        return False
    return True
