"""Carga y validación de los JSON_Schema del Sistema.

Este módulo es el único punto de acceso a los contratos declarativos
(``*.schema.json``) del paquete :mod:`sentiment.schemas`. Centraliza tres
responsabilidades para que ningún consumidor tenga que conocer rutas de archivo
ni detalles de ``jsonschema``:

* **Carga perezosa y cacheada** de cada documento de esquema desde el propio
  paquete (``importlib.resources``), de modo que funcione tanto en desarrollo
  como empaquetado en una Lambda Layer.
* **Construcción de validadores** Draft 2020-12 con un :class:`referencing.Registry`
  compartido, para que el ``$ref`` entre
  :data:`CONSOLIDATED_OUTPUT_SCHEMA` e :data:`ITEM_RESULT_SCHEMA` resuelva sin
  acceso a red.
* **Exposición del enum de ``indificador``** embebido en ``submit_request`` para
  que la prueba de deriva lo contraste contra la fuente única
  :func:`sentiment.prompts.indificadores.enum_values` (Requirement 17.14).

El ``submit_request`` se construye con el ``format`` activado (comprobación de
``email`` y ``uri``) porque la validación de ``metadata.notify_email`` y
``metadata.webhook_url`` (criterios 3.11, 3.12) depende de ello; por defecto
``jsonschema`` ignora ``format`` como mera anotación.

Side effects: lectura de los archivos de esquema del paquete en la primera
llamada (cacheada después). Sin I/O de red ni mutación de estado global
observable fuera de la caché interna.
"""

from __future__ import annotations

import json
from functools import cache, lru_cache
from importlib import resources
from typing import Any, Final

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

# Nombres de archivo de los cuatro contratos del paquete. Son la fuente de la
# verdad de qué esquemas existen; cualquier otro módulo debe referirse a ellos
# por estas constantes y nunca por una cadena literal dispersa.
SUBMIT_REQUEST_SCHEMA_FILE: Final[str] = "submit_request.schema.json"
LLM_OUTPUT_SCHEMA_FILE: Final[str] = "llm_output.schema.json"
ITEM_RESULT_SCHEMA_FILE: Final[str] = "item_result.schema.json"
CONSOLIDATED_OUTPUT_SCHEMA_FILE: Final[str] = "consolidated_output.schema.json"

# Todos los esquemas que participan en resolución de ``$ref`` entre sí. El
# registro compartido se construye con todos ellos para que
# ``consolidated_output`` pueda referenciar ``item_result`` por su ``$id``.
_ALL_SCHEMA_FILES: Final[tuple[str, ...]] = (
    SUBMIT_REQUEST_SCHEMA_FILE,
    LLM_OUTPUT_SCHEMA_FILE,
    ITEM_RESULT_SCHEMA_FILE,
    CONSOLIDATED_OUTPUT_SCHEMA_FILE,
)


@cache
def load_schema(filename: str) -> dict[str, Any]:
    """Carga y parsea un documento de esquema del paquete por su nombre de archivo.

    El resultado se cachea: las llamadas sucesivas con el mismo ``filename``
    devuelven el objeto ya parseado sin volver a leer del disco.

    Args:
        filename: Nombre del archivo de esquema dentro del paquete
            ``sentiment.schemas`` (p.ej. ``submit_request.schema.json``). Debe
            ser uno de los declarados en :data:`_ALL_SCHEMA_FILES`.

    Returns:
        El documento de esquema como ``dict`` (no debe mutarse; es compartido).

    Raises:
        ValueError: si ``filename`` no es un esquema conocido del paquete.
        FileNotFoundError: si el archivo declarado no está empaquetado.
        json.JSONDecodeError: si el contenido no es JSON válido.
    """
    if filename not in _ALL_SCHEMA_FILES:
        raise ValueError(
            f"Esquema desconocido: {filename!r}. "
            f"Debe ser uno de {_ALL_SCHEMA_FILES}."
        )
    raw = resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    """Construye el registro de referencias compartido con los cuatro esquemas.

    Cada esquema se indexa por su ``$id``, que es la URI que usan los ``$ref``
    (p.ej. ``consolidated_output`` referencia a ``item_result`` por su ``$id``).
    El registro se cachea porque es inmutable y reutilizable entre validadores.

    Returns:
        Un :class:`referencing.Registry` poblado con los cuatro contratos.
    """
    pairs: list[tuple[str, Resource[Any]]] = []
    for filename in _ALL_SCHEMA_FILES:
        resource: Resource[Any] = Resource.from_contents(load_schema(filename))
        resource_id = resource.id()
        if resource_id is None:  # pragma: no cover - todos los esquemas fijan $id
            raise ValueError(f"El esquema {filename!r} carece de '$id'.")
        pairs.append((resource_id, resource))
    registry: Registry[Any] = Registry().with_resources(pairs)
    return registry


@cache
def get_validator(filename: str) -> Draft202012Validator:
    """Devuelve un validador ``Draft202012Validator`` cacheado para el esquema.

    El validador se construye con el :func:`_registry` compartido (para resolver
    ``$ref`` entre esquemas) y con el ``format_checker`` por defecto de
    Draft 2020-12 activado, de modo que ``format: email`` y ``format: uri`` se
    verifican de verdad (criterios 3.11, 3.12) en lugar de ignorarse.

    Args:
        filename: Nombre del archivo de esquema (ver :func:`load_schema`).

    Returns:
        Un validador listo para ``validate`` / ``iter_errors`` / ``is_valid``.

    Raises:
        ValueError: si ``filename`` no es un esquema conocido.
        jsonschema.exceptions.SchemaError: si el esquema en sí es inválido.
    """
    schema = load_schema(filename)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def submit_request_indificador_enum() -> list[str]:
    """Extrae el enum de ``indificador`` embebido en ``submit_request``.

    Es el valor que la prueba de deriva contrasta contra la fuente única
    :func:`sentiment.prompts.indificadores.enum_values`, garantizando que el
    enum del contrato JSON y la lista canónica de Indificadores nunca divergen
    (Requirement 17.14).

    Returns:
        La lista de valores del ``enum`` del campo ``indificador`` tal como
        aparece en el documento ``submit_request.schema.json``.

    Raises:
        KeyError: si la estructura del esquema no contiene el enum esperado,
            lo que indica una edición que rompe el contrato.
    """
    schema = load_schema(SUBMIT_REQUEST_SCHEMA_FILE)
    item_properties = schema["properties"]["items"]["items"]["properties"]
    enum: list[str] = item_properties["indificador"]["enum"]
    return list(enum)
