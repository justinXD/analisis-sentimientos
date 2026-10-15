"""Paquete `sentiment`: núcleo de la aplicación del pipeline de análisis de sentimiento.

Organizado en capas explícitas (arquitectura hexagonal / puertos y adaptadores):

- ``domain``: núcleo puro de negocio, importable sin el SDK de AWS.
- ``services``: casos de uso, con sus dependencias inyectadas.
- ``adapters``: implementaciones concretas de los puertos del dominio.
- ``handlers``: adaptadores de transporte finos (entry points de las Lambdas).
- ``prompts``: prompt, catálogo de sentimientos e Indificadores versionados.
- ``schemas``: contratos de datos expresados como JSON Schema.
- ``observability``: logging con redacción de PII, métricas y configuración.

Consulta ``docs/ARCHITECTURE.md`` y ``.kiro/specs/serverless-sentiment-pipeline``
para el diseño completo.
"""
