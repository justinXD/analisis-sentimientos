"""Caso de uso ``purge_analysis``: supresión completa de un Lote (derecho al olvido).

Este módulo implementa la lógica de negocio del endpoint
``DELETE /v1/analyses/{analysis_id}`` (Lambda ``api_purge``), conforme a la
sección 16.7 del diseño y al Requirement 19, con el aislamiento multi-tenant del
Requirement 2.

Rol en la arquitectura (hexagonal, diseño 2.1):

* El servicio es **puro respecto de la infraestructura**: no importa ``boto3`` ni
  ningún adaptador concreto. Depende de tres puertos inyectados en el constructor
  —:class:`~sentiment.domain.ports.PurgeRepository` (borrado de ITEMs y del META),
  :class:`~sentiment.domain.ports.PurgeStorage` (borrado de objetos de S3 con
  todas sus versiones) y :class:`~sentiment.domain.ports.AuditLog` (registro
  auditable sin PII)— más un ``clock`` inyectado para la marca de tiempo, de modo
  que es verificable con dobles en memoria.
* El handler ``api_purge`` (tarea 9.1) es un adaptador *thin* que lee ``tenant_id``
  y ``sub`` del contexto del autorizador, asume el rol de datos con las dos
  *session tags* (12.2.1), invoca :meth:`PurgeAnalysisService.execute` y traduce
  el :class:`~sentiment.domain.errors.SentimentError` a una respuesta HTTP.

Orden y recuperación ante fallos (criterios 19.3, 19.4, 19.7, 19.8)
-------------------------------------------------------------------
El orden de borrado es, deliberadamente, **ITEMs → objetos de S3 (todas las
versiones) → META al final**:

#. **ITEMs primero (criterio 19.3).** Se borran todos los registros ITEM del Lote.
#. **Objetos de S3 después (criterio 19.4).** Se borran el objeto de ``inputs/``,
   el Documento_Consolidado de ``outputs/`` y todo lo que cuelga de
   ``map-results/{analysis_id}/``, **incluidas todas las versiones y los
   marcadores de eliminación** (el bucket tiene versionado activado).
#. **META el último (criterios 19.3, 19.7, 19.8).** Mientras el META exista, el
   Lote sigue siendo direccionable y la purga es **reintentable**: si (1) o (2)
   fallan, una nueva invocación recupera el META, repite el borrado (lo ya
   ausente cuenta como eliminado) y completa lo que falte. Borrar el META antes
   dejaría objetos huérfanos con PII imposibles de direccionar.

Las claves exactas de S3 se derivan del META (``input_s3_key`` y ``output_s3_key``),
no de datos del cliente; el prefijo ``map-results/`` se deriva del ``analysis_id``.

Evento auditable (criterios 19.9, 19.11)
----------------------------------------
El evento auditable de "purga completada" se registra **solo tras el éxito
completo** de los tres pasos de borrado, nunca antes. Contiene exclusivamente
metadatos (``principal_sub``, ``tenant_id``, ``analysis_id``, ``correlation_id``,
recuentos de registros y versiones eliminados y la marca de tiempo), **nunca** PII
(``respuesta``, ``explicacion``, correos ni la Presigned_URL). Una purga que falla
en cualquier paso propaga la excepción sin registrar el evento (criterio 19.7).

Precedencia de las comprobaciones (importa para distinguir 404 de 409)
----------------------------------------------------------------------
#. **ULID + propiedad (criterios 19.5, 2.7).** Un ``analysis_id`` malformado,
   inexistente o ajeno produce el **mismo** HTTP 404
   (:class:`~sentiment.domain.errors.NotFoundError`), sin borrar nada.
#. **Estado terminal (criterio 19.6).** Un Lote propio en ``PENDING`` o
   ``RUNNING`` produce HTTP 409 (:class:`~sentiment.domain.errors.NotReadyError`),
   sin borrar nada: purgar un Lote en curso competiría con el pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sentiment.domain.errors import NotFoundError, NotReadyError
from sentiment.domain.ids import is_valid_ulid

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sentiment.domain.ports import AuditLog, PurgeRepository, PurgeStorage

__all__ = [
    "MAP_RESULTS_PREFIX_TEMPLATE",
    "PurgeCommand",
    "PurgeResult",
    "PurgeAnalysisService",
]

#: Plantilla del prefijo de S3 bajo el que viven los resultados parciales del
#: Distributed_Map de un Lote (``ResultWriter`` del workflow, diseño 17.x). La
#: purga elimina recursivamente todo lo que cuelga de este prefijo, incluidas
#: todas las versiones (criterio 19.4). Se deriva del ``analysis_id``, no de datos
#: del cliente.
MAP_RESULTS_PREFIX_TEMPLATE = "map-results/{analysis_id}/"


@dataclass(frozen=True, slots=True)
class PurgeCommand:
    """Entrada del caso de uso ``purge_analysis``, ya autenticada.

    El handler (``api_purge``) construye este comando a partir del evento de API
    Gateway: lee ``tenant_id`` y ``principal_sub`` **exclusivamente** del contexto
    del autorizador (claims ``custom:tenant_id`` y ``sub`` ya verificados; el scope
    ``sentiment/analyses.purge`` lo validó API Gateway antes de invocar la Lambda).

    Attributes:
        analysis_id: ULID del Lote a purgar, recibido en la ruta HTTP. Puede estar
            malformado: la validación sintáctica se hace en el servicio.
        tenant_id: organización propietaria del Lote, del claim verificado; acota
            la lectura del META y es la frontera de aislamiento multi-tenant.
        principal_sub: ``sub`` del solicitante, del claim verificado; se registra
            en el evento auditable (criterio 19.11), nunca como PII.
        correlation_id: identificador de correlación de la solicitud, propagado al
            evento auditable para la trazabilidad extremo a extremo.
    """

    analysis_id: str
    tenant_id: str
    principal_sub: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Resultado de una purga completada con éxito (cuerpo del HTTP 200, criterio 19.1).

    Attributes:
        analysis_id: ULID del Lote purgado.
        purged_at: instante UTC en que concluyó la purga; el handler lo serializa
            en ISO 8601 UTC.
    """

    analysis_id: str
    purged_at: datetime


class PurgeAnalysisService:
    """Elimina todos los datos de un Lote terminal propio (derecho al olvido a nivel de Lote).

    Encapsula el algoritmo de ``purge_analysis`` (sección 16.7 del diseño) con sus
    dependencias de infraestructura inyectadas como puertos. Es reutilizable y
    verificable sin AWS: basta inyectar dobles en memoria que satisfagan los
    protocolos :class:`PurgeRepository`, :class:`PurgeStorage` y :class:`AuditLog`.
    """

    def __init__(
        self,
        repository: PurgeRepository,
        storage: PurgeStorage,
        audit: AuditLog,
        clock: Callable[[], datetime],
    ) -> None:
        """Inyecta los puertos y el reloj del servicio.

        :param repository: puerto de borrado en DynamoDB (ITEMs y META). Acceso
            acotado al Tenant y al Lote mediante *session tags* (12.2.1).
        :param storage: puerto de borrado en S3; elimina objetos y todas sus
            versiones y marcadores de eliminación.
        :param audit: puerto de registro auditable; emite el evento de purga
            completada **sin PII**, solo tras el éxito.
        :param clock: fuente del instante UTC usado en la marca de tiempo del
            resultado y del evento auditable. Inyectable para pruebas deterministas.
        """
        self._repository = repository
        self._storage = storage
        self._audit = audit
        self._clock = clock

    def execute(self, command: PurgeCommand) -> PurgeResult:
        """Purga el Lote ``command.analysis_id`` del Tenant ``command.tenant_id``.

        Flujo (ver el docstring del módulo para el detalle de recuperación ante
        fallos y la precedencia de códigos HTTP):

        #. Valida el ULID y la propiedad; un id malformado, inexistente o ajeno es
           el **mismo** 404 (criterios 19.5, 2.7).
        #. Exige un Estado_Terminal; un Lote propio en ``PENDING``/``RUNNING`` es un
           409 (criterio 19.6).
        #. Borra los ITEMs (criterio 19.3), luego los objetos de S3 con todas sus
           versiones (criterio 19.4) y, **en último lugar**, el META (criterios
           19.3, 19.7, 19.8): así la purga es reintentable sin dejar huérfanos.
        #. **Solo tras el éxito completo**, registra el evento auditable sin PII
           (criterios 19.9, 19.11) y devuelve el :class:`PurgeResult`.

        :param command: comando autenticado con el ``analysis_id``, el
            ``tenant_id``, el ``principal_sub`` y el ``correlation_id``.
        :return: el :class:`PurgeResult` con el ``analysis_id`` y el ``purged_at``.
        :raises NotFoundError: si ``analysis_id`` no es un ULID válido, no existe o
            es de otro Tenant (criterios 19.5, 2.7); no se borra nada. Una
            reejecución sobre un Lote ya purgado también da 404 (el META ya no
            existe), nunca 200 (criterio 19.1 por contraste con 19.8).
        :raises NotReadyError: si el Lote es propio pero no es terminal (criterio
            19.6, HTTP 409); no se borra nada.
        """
        # 1) ULID + propiedad. Un id malformado se rechaza antes de tocar la base
        #    (criterio 2.7); uno inexistente o ajeno lanza NotFoundError desde el
        #    repositorio (criterio 19.5). Los tres son el MISMO 404.
        if not is_valid_ulid(command.analysis_id):
            raise NotFoundError(reason="invalid_ulid")

        meta = self._repository.get_meta(command.analysis_id, command.tenant_id)

        # 2) Solo Lotes terminales: purgar uno en curso competiría con el pipeline
        #    (criterio 19.6). No se borra nada.
        if not meta.is_terminal:
            raise NotReadyError(reason="not_terminal", status=meta.status)

        # 3a) ITEMs primero (criterio 19.3). Reintentable: un ITEM ya ausente
        #     cuenta como eliminado. Devuelve cuántos se borraron efectivamente.
        records_deleted = self._repository.delete_items(
            command.analysis_id, command.tenant_id
        )

        # 3b) Objetos de S3 con TODAS sus versiones y marcadores (criterio 19.4).
        #     Las claves exactas salen del META (nunca del cliente); el prefijo de
        #     resultados parciales se deriva del analysis_id.
        exact_keys = [
            key for key in (meta.input_s3_key, meta.output_s3_key) if key
        ]
        versions_deleted = self._storage.delete_all_versions(
            exact_keys=exact_keys,
            prefix=MAP_RESULTS_PREFIX_TEMPLATE.format(
                analysis_id=command.analysis_id
            ),
        )

        # 3c) El META SIEMPRE al final (criterios 19.3, 19.7, 19.8). Si 3a o 3b
        #     fallaron, el Lote sigue direccionable y la purga es reintentable;
        #     borrar el META antes dejaría objetos huérfanos con PII.
        self._repository.delete_meta(command.analysis_id, command.tenant_id)

        # 4) Evento auditable SOLO tras el éxito completo (criterio 19.9), sin PII
        #    (criterio 19.11). records_deleted incluye el META recién borrado.
        at = self._clock()
        self._audit.record_purge_completed(
            principal_sub=command.principal_sub,
            tenant_id=command.tenant_id,
            analysis_id=command.analysis_id,
            correlation_id=command.correlation_id,
            dynamodb_records_deleted=records_deleted + 1,
            s3_versions_deleted=versions_deleted,
            at=at,
        )
        return PurgeResult(analysis_id=command.analysis_id, purged_at=at)
