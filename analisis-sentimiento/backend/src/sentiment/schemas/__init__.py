"""Contratos de datos expresados como JSON Schema (Draft 2020-12).

El paquete contiene los cuatro contratos declarativos del Sistema y el cargador
que los expone de forma segura y cacheada:

* ``submit_request.schema.json`` — entrada de ``POST /v1/analyses`` (allow-list
  estricta; enum de ``indificador`` sincronizado con la fuente única).
* ``llm_output.schema.json`` — salida del Modelo_Bedrock para un Ítem.
* ``item_result.schema.json`` — proyección exportable del resultado de un Ítem.
* ``consolidated_output.schema.json`` — el Documento_Consolidado de un Lote.

Los consumidores deben usar :mod:`sentiment.schemas.loader` (reexportado aquí)
en lugar de leer los archivos directamente.
"""

from sentiment.schemas.loader import (
    CONSOLIDATED_OUTPUT_SCHEMA_FILE,
    ITEM_RESULT_SCHEMA_FILE,
    LLM_OUTPUT_SCHEMA_FILE,
    SUBMIT_REQUEST_SCHEMA_FILE,
    get_validator,
    load_schema,
    submit_request_indificador_enum,
)

__all__ = [
    "CONSOLIDATED_OUTPUT_SCHEMA_FILE",
    "ITEM_RESULT_SCHEMA_FILE",
    "LLM_OUTPUT_SCHEMA_FILE",
    "SUBMIT_REQUEST_SCHEMA_FILE",
    "get_validator",
    "load_schema",
    "submit_request_indificador_enum",
]
