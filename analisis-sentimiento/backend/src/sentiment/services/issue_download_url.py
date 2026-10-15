"""Caso de uso de emisión de la Presigned_URL de descarga (Api_Download).

Este módulo implementa la lógica de negocio del endpoint
``GET /v1/analyses/{analysis_id}/download``, conforme a la sección 12 del diseño
y al Requirement 12, con el aislamiento multi-tenant del Requirement 2.

Rol en la arquitectura (hexagonal, diseño 2.1):

* El servicio es **puro respecto de la infraestructura**: no importa ``boto3``
  ni ningún adaptador concreto. Depende de dos puertos inyectados en el
  constructor —:class:`~sentiment.domain.ports.AnalysisRepository` (para leer el
  META y verificar propiedad) y :class:`~sentiment.domain.ports.ObjectStorage`
  (para comprobar la existencia del objeto y firmar la URL)—, de modo que es
  testeable con dobles en memoria.
* El handler ``api_download`` (tarea 9.1) es un adaptador *thin* que traduce el
  evento HTTP en una llamada a :meth:`IssueDownloadUrlService.issue` y la vista
  resultante o el :class:`~sentiment.domain.errors.SentimentError` en una
  respuesta HTTP.

Precedencia de las comprobaciones (importa para distinguir 404 de 409):

1. **ULID y propiedad (criterios 2.7, 12.6).** Un ``analysis_id`` malformado,
   inexistente o ajeno produce el **mismo** HTTP 404
   (:class:`~sentiment.domain.errors.NotFoundError`), sin emitir Presigned_URL,
   con independencia de que exista o no el objeto del Lote ajeno.
2. **Estado terminal con salida (criterios 12.3, 12.4).** Un Lote en ``PENDING``
   o ``RUNNING``, o terminal pero sin ``output_s3_key`` (incluido el ``EXPIRED``
   que cerró el sweeper sin consolidación), produce HTTP 409
   (:class:`~sentiment.domain.errors.NotReadyError`), sin tocar la Dynamo_Table.
3. **Existencia del objeto (criterio 12.5).** Si el ``output_s3_key`` referencia
   un objeto que ya no existe en el Bucket_Analisis (p. ej. lo expiró el ciclo de
   vida), produce HTTP 404 idéntico al de un recurso inexistente, sin emitir
   Presigned_URL.
4. **Éxito (criterios 12.1, 12.2, 12.10).** Se emite una Presigned_URL ``GET``
   acotada al único objeto ``output_s3_key``, con validez de exactamente 900
   segundos, generada en el instante de la solicitud y **nunca** persistida ni
   registrada.

La operación es de **solo lectura** sobre la Dynamo_Table (no modifica ningún
registro, criterio 12.4) y no almacena la URL en ningún sitio (criterio 12.10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sentiment.domain.errors import NotFoundError, NotReadyError
from sentiment.domain.ids import is_valid_ulid

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sentiment.domain.models import AnalysisStatus
    from sentiment.domain.ports import AnalysisRepository, ObjectStorage

__all__ = ["PRESIGN_EXPIRY_SECONDS", "DownloadView", "IssueDownloadUrlService"]

#: Validez de la Presigned_URL de descarga, en segundos. El criterio 12.2 fija
#: exactamente 900 s (15 min); se expone como constante para no duplicar el
#: literal y para que coincida con el valor por defecto del adaptador de S3.
PRESIGN_EXPIRY_SECONDS: Final = 900


@dataclass(frozen=True, slots=True)
class DownloadView:
    """Vista devuelta por Api_Download al emitir una Presigned_URL (criterio 12.1).

    Attributes:
        analysis_id: ULID del Lote cuyo Documento_Consolidado se descarga.
        status: estado terminal del Lote (``COMPLETED`` o
            ``COMPLETED_WITH_ERRORS``, dado que solo un Lote terminal con salida
            llega hasta aquí).
        url: Presigned_URL ``GET`` de solo lectura acotada al único objeto del
            Documento_Consolidado. Es un secreto portador: el handler la devuelve
            al cliente pero **no** debe registrarla (criterio 12.10).
        expires_at: instante UTC de expiración de la Presigned_URL, en formato
            ISO 8601 al serializarse; es ``issued_at + 900 s`` (criterio 12.2).
    """

    analysis_id: str
    status: AnalysisStatus
    url: str
    expires_at: datetime


class IssueDownloadUrlService:
    """Caso de uso de emisión de la Presigned_URL de descarga (Api_Download).

    Depende de los puertos :class:`AnalysisRepository` y :class:`ObjectStorage`,
    y de un reloj inyectado (``clock``) para calcular el instante de expiración
    de forma determinista en las pruebas. No realiza ninguna escritura en la
    Dynamo_Table (criterio 12.4) ni persiste la Presigned_URL (criterio 12.10).
    """

    def __init__(
        self,
        repository: AnalysisRepository,
        storage: ObjectStorage,
        clock: Callable[[], datetime],
    ) -> None:
        """Inicializa el servicio con sus dependencias inyectadas.

        :param repository: implementación de :class:`AnalysisRepository`, para
            leer el META y verificar propiedad acotada por Tenant.
        :param storage: implementación de :class:`ObjectStorage`, para comprobar
            la existencia del objeto y firmar la Presigned_URL.
        :param clock: función sin argumentos que devuelve el instante UTC actual;
            se usa para calcular ``expires_at`` (inyectada para pruebas
            deterministas).
        """
        self._repository = repository
        self._storage = storage
        self._clock = clock

    def issue(self, analysis_id: str, tenant_id: str) -> DownloadView:
        """Emite la Presigned_URL de descarga del Documento_Consolidado de un Lote.

        Aplica, en orden, las comprobaciones de propiedad, estado terminal con
        salida y existencia del objeto antes de firmar la URL (ver el docstring
        del módulo para la precedencia y los códigos HTTP resultantes).

        :param analysis_id: ULID del Lote recibido en la ruta HTTP.
        :param tenant_id: Tenant del solicitante, derivado exclusivamente del
            claim ``custom:tenant_id`` del Access_Token verificado.
        :return: la :class:`DownloadView` con la Presigned_URL y su expiración.
        :raises NotFoundError: si ``analysis_id`` no es un ULID válido, no existe,
            es de otro Tenant (criterios 2.7, 12.6) o su ``output_s3_key``
            referencia un objeto inexistente en el Bucket_Analisis (criterio
            12.5). Los casos producen un HTTP 404 idéntico.
        :raises NotReadyError: si el Lote no es terminal, o es terminal pero
            carece de ``output_s3_key`` (HTTP 409, criterios 12.3, 12.4).
        """
        # 1) ULID + propiedad. Un id malformado se rechaza antes de tocar la base
        #    (criterio 2.7); uno inexistente o ajeno lanza NotFoundError desde el
        #    repositorio (criterio 12.6). Los tres son el MISMO 404.
        if not is_valid_ulid(analysis_id):
            raise NotFoundError(reason="invalid_ulid")

        meta = self._repository.get_meta(analysis_id, tenant_id)

        # 2) Estado terminal + salida registrada. Solo lectura: no se modifica
        #    ningún registro (criterio 12.4). Un Lote no terminal o terminal sin
        #    Documento_Consolidado (incluido EXPIRED sin consolidación) => 409.
        if not meta.is_terminal or not meta.output_s3_key:
            raise NotReadyError(
                reason="not_terminal" if not meta.is_terminal else "no_output",
                status=meta.status,
            )

        output_key = meta.output_s3_key

        # 3) Existencia del objeto en S3. Con HeadObject (sin descargar PII): si
        #    el objeto ya no existe (p. ej. lo expiró el ciclo de vida), 404
        #    idéntico al de un recurso inexistente (criterio 12.5). Nunca se emite
        #    la Presigned_URL en ese caso.
        if not self._storage.object_exists(output_key):
            raise NotFoundError(reason="output_object_missing")

        # 4) Éxito. Presigned_URL GET acotada al único objeto, 900 s exactos,
        #    generada ahora y nunca persistida ni registrada (criterios 12.1,
        #    12.2, 12.10). expires_at se fija a partir del reloj inyectado.
        url = self._storage.presign_get(output_key, PRESIGN_EXPIRY_SECONDS)
        expires_at = self._clock() + timedelta(seconds=PRESIGN_EXPIRY_SECONDS)
        return DownloadView(
            analysis_id=meta.analysis_id,
            status=meta.status,
            url=url,
            expires_at=expires_at,
        )
