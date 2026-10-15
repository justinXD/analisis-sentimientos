"""Adaptador de registro auditable de la Purga (puerto ``AuditLog``).

Este módulo materializa el puerto :class:`~sentiment.domain.ports.AuditLog`
(contrato normativo en ``domain/ports.py``) dejando constancia **auditable** de
una Purga completada (criterio 19.11; diseño sección 12.8). Lo consume el caso
de uso ``purge_analysis`` **solo tras el éxito** de la Purga: una Purga fallida
nunca registra el evento de "purga completada" (criterio 19.7), únicamente un
log operativo ``purge_failed`` que emite el propio caso de uso.

Separación del log de auditoría (diseño 12.8)
---------------------------------------------
El evento auditable se emite a un canal de log de auditoría **separado** de los
logs operativos. En la Infraestructura ese canal es un log group propio
(``/sentiment/{env}/audit``, retención de 365 días, cifrado con la CMK), distinto
del log group operativo de 30 días de la Lambda. Para que CloudWatch Logs
encamine el evento a ese flujo diferenciado, el adaptador usa un
:class:`~aws_lambda_powertools.logging.logger.Logger` con un ``service`` propio
(``audit``), y marca cada registro con ``log_type = "audit"`` para que las
suscripciones y los filtros de métricas puedan distinguirlo sin ambigüedad.

Contrato del evento (criterio 19.11)
------------------------------------
El evento ``purge_completed`` contiene **exclusivamente** metadatos:

- ``principal_sub``: identificador (``sub``) del principal solicitante.
- ``tenant_id``: organización propietaria del Lote purgado.
- ``analysis_id``: ULID del Lote purgado.
- ``correlation_id``: identificador de correlación de la solicitud HTTP.
- ``dynamodb_records_deleted``: número de registros de la Dynamo_Table eliminados.
- ``s3_versions_deleted``: número de versiones de objetos de S3 eliminadas
  (incluidos los marcadores de eliminación).
- ``purged_at``: instante de finalización de la Purga, en ISO 8601 UTC. El
  criterio 19.11 lo nombra "marca de tiempo"; se emite bajo la clave ``purged_at``
  (la misma del cuerpo de la respuesta HTTP 200 de la sección 12.8) para no
  colisionar con el campo reservado ``timestamp`` que AWS Lambda Powertools
  escribe en **todo** registro con el instante de emisión. Si un llamador pasa el
  valor bajo la clave ``timestamp``, el adaptador lo renombra a ``purged_at``
  (ver :meth:`AuditLog.record_purge_completed`), de modo que el instante de la
  Purga nunca lo sobrescriba el sello temporal del logger.

**Sin PII (criterios 19.11, 15.1, 15.2).** El evento nunca incluye ``respuesta``,
``explicacion``, correos ni ninguna Presigned_URL. El logger de auditoría comparte
la red de seguridad de redacción de :mod:`sentiment.observability.logging`: aunque
un llamador pasara por error un campo PII en ``fields``, se emitiría enmascarado.
Aun así, el caso de uso debe pasar únicamente los metadatos del contrato.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger

__all__ = ["AuditLog", "AUDIT_LOG_SERVICE", "PURGE_COMPLETED_EVENT"]

#: ``service`` del logger de auditoría. Lo usa la Infraestructura para encaminar
#: el flujo de auditoría a su log group propio (``/sentiment/{env}/audit``),
#: separado de los logs operativos (diseño 12.8).
AUDIT_LOG_SERVICE: Final = "audit"

#: Mensaje (clave de evento estructurado) del registro de Purga completada. Es un
#: literal estable para que los filtros de métricas y las suscripciones puedan
#: seleccionarlo de forma determinista.
PURGE_COMPLETED_EVENT: Final = "purge_completed"

#: Marca que distingue los registros de auditoría de los operativos dentro del
#: mismo backend, por si comparten destino de logs. Permite construir un filtro
#: ``{ $.log_type = "audit" }`` sin depender del nombre del log group.
_LOG_TYPE_AUDIT: Final = "audit"

#: Clave reservada por AWS Lambda Powertools para el sello temporal de emisión de
#: cada registro. Un ``extra`` con esta clave sería sobrescrito silenciosamente,
#: así que el adaptador la renombra a :data:`_PURGE_TIMESTAMP_FIELD`.
_RESERVED_TIMESTAMP_FIELD: Final = "timestamp"

#: Clave bajo la que se emite el instante de finalización de la Purga, igual a la
#: del cuerpo de la respuesta HTTP 200 de la Api_Purge (diseño 12.8).
_PURGE_TIMESTAMP_FIELD: Final = "purged_at"


class AuditLog:
    """Registra el evento auditable de Purga completada (puerto ``AuditLog``).

    Implementa estructuralmente :class:`~sentiment.domain.ports.AuditLog`. El
    logger de auditoría se construye una vez por contenedor y se reutiliza entre
    invocaciones calientes; no lee configuración obligatoria del entorno, de modo
    que el adaptador puede construirse sin romper la importabilidad del paquete
    (criterio 17.7).
    """

    def __init__(self, logger: Logger | None = None) -> None:
        """Construye el adaptador con su logger de auditoría dedicado.

        No se lee configuración ni se llama a AWS en la construcción: el logger de
        Powertools se instancia con el ``service`` de auditoría y la redacción de
        PII heredada de :func:`sentiment.observability.logging.get_logger`.

        :param logger: logger de auditoría a usar; si es ``None`` se construye el
            logger de Powertools con el ``service`` de auditoría. El parámetro es
            un punto de inyección para las pruebas, que pueden pasar un logger
            dirigido a un flujo capturable.
        """
        self._logger = logger if logger is not None else get_logger(
            service=AUDIT_LOG_SERVICE
        )

    def record_purge_completed(self, **fields: object) -> None:
        """Registra un evento auditable de Purga completada, sin PII (criterio 19.11).

        Se invoca **solo** tras el éxito de la Purga. Emite un único registro
        estructurado ``purge_completed`` con los metadatos recibidos en
        ``fields`` y la marca ``log_type = "audit"`` que lo identifica como
        traza de auditoría.

        :param fields: metadatos del evento (``principal_sub``, ``tenant_id``,
            ``analysis_id``, ``correlation_id``, ``dynamodb_records_deleted``,
            ``s3_versions_deleted`` y la marca de tiempo, aceptada tanto como
            ``purged_at`` como ``timestamp`` —esta última se renombra a
            ``purged_at`` para no colisionar con el sello temporal reservado del
            logger). **No** debe incluir PII; el logger de auditoría redacta como
            red de seguridad cualquier campo PII que llegara por error, pero el
            llamador es responsable de pasar solo metadatos.

        Postcondiciones:
            - Deja constancia auditable de la Purga **solo** tras su éxito; el
              caso de uso no debe invocar este método en un fallo parcial
              (criterio 19.7).
            - El registro contiene exclusivamente metadatos (identificadores,
              recuentos y marca de tiempo), nunca PII (criterio 19.11).
        """
        extra: dict[str, object] = {"log_type": _LOG_TYPE_AUDIT, **fields}
        # Evita que el campo reservado del logger pise la marca de tiempo de la
        # Purga: se reexpone bajo `purged_at` si el llamador la pasó como
        # `timestamp`.
        if _RESERVED_TIMESTAMP_FIELD in extra:
            extra[_PURGE_TIMESTAMP_FIELD] = extra.pop(_RESERVED_TIMESTAMP_FIELD)
        self._logger.info(PURGE_COMPLETED_EVENT, extra=extra)
