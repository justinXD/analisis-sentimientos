"""Caso de uso de consulta de estado y progreso de un Lote (Api_Status).

Este módulo implementa la lógica de negocio **de solo lectura** del endpoint
``GET /v1/analyses/{analysis_id}`` (estado de un Lote) y de ``GET /v1/analyses``
(listado paginado de los Lotes de un Tenant), conforme a la sección 16.5 del
diseño y al Requirement 11, con el aislamiento multi-tenant del Requirement 2.

Rol en la arquitectura (hexagonal, diseño 2.1):

* El servicio es **puro respecto de la infraestructura**: no importa ``boto3``
  ni ningún adaptador concreto. Depende únicamente del puerto
  :class:`~sentiment.domain.ports.AnalysisRepository`, inyectado en el
  constructor (inversión de dependencias). Esto lo hace testeable con un doble
  en memoria y mantiene la frontera handler → service → domain.
* El handler ``api_status`` (tarea 9.1) es un adaptador *thin* que traduce el
  evento HTTP en una llamada a este servicio y la vista resultante o el
  :class:`~sentiment.domain.errors.SentimentError` en una respuesta HTTP.

Garantías de diseño que materializa:

* **Aislamiento estructural (criterios 2.2, 2.4, 11.10, Property 6).** La
  partición de la Dynamo_Table incluye el ``tenant_id``, de modo que una lectura
  con el ``tenant_id`` del token **no puede** devolver un Lote de otro Tenant.
  Un ``analysis_id`` inexistente, ajeno o sintácticamente inválido producen el
  **mismo** :class:`~sentiment.domain.errors.NotFoundError` (HTTP 404 idéntico),
  cerrando el vector de enumeración entre Tenants.
* **Progreso monótono y acotado (criterios 11.1-11.4, Property 8).** El
  ``progress`` se deriva de :pyattr:`AnalysisMeta.progress_ratio`, acotado a
  ``[0, 1]``, y se redondea a 4 decimales en la vista.
* **Sin PII (criterios 11.5, 11.6).** La vista expone solo metadatos. El detalle
  opcional de Ítems fallidos (``include_failures``) devuelve únicamente el
  ``item_id`` y el ``error_code`` del Catalogo_De_Errores, nunca el texto de
  ``respuesta`` o ``explicacion``.
* **Listado paginado por cursor opaco (criterios 2.4, 11.7-11.9, 11.11-11.13).**
  La paginación delega en :meth:`AnalysisRepository.list_by_tenant`, que acota el
  tamaño de la consulta y devuelve un cursor de continuación opaco; nunca carga
  el conjunto completo en memoria.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sentiment.domain.errors import NotFoundError, ValidationError
from sentiment.domain.ids import is_valid_ulid
from sentiment.domain.models import AnalysisStatus, ItemStatus

if TYPE_CHECKING:
    from datetime import datetime

    from sentiment.domain.ports import AnalysisRepository

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MAX_FAILURES_IN_STATUS",
    "FailureView",
    "StatusView",
    "AnalysisSummaryView",
    "AnalysisListView",
    "GetAnalysisStatusService",
]

#: Número de Lotes por página cuando el cliente no envía ``page_size`` (criterio
#: 11.7).
DEFAULT_PAGE_SIZE: Final = 20

#: Tope duro de Lotes por página. Un ``page_size`` mayor se acota a este valor y
#: la respuesta indica el tamaño efectivamente aplicado (criterio 11.11).
MAX_PAGE_SIZE: Final = 100

#: Máximo de Ítems fallidos que se detallan en la respuesta de estado cuando
#: ``include_failures`` está activo (criterio 11.5).
MAX_FAILURES_IN_STATUS: Final = 100


@dataclass(frozen=True, slots=True)
class FailureView:
    """Detalle mínimo de un Ítem fallido expuesto por ``include_failures``.

    Contiene **exclusivamente** el ``item_id`` y el ``error_code`` del
    Catalogo_De_Errores (criterio 11.5): nunca el texto de ``respuesta`` ni de
    ``explicacion``, que son PII y solo se obtienen por la descarga autenticada
    del Documento_Consolidado (criterio 11.6).

    Attributes:
        item_id: identificador del Ítem fallido (``{analysis_id}#{index:05d}``).
        error_code: código del Catalogo_De_Errores con el que se cerró el Ítem.
    """

    item_id: str
    error_code: str


@dataclass(frozen=True, slots=True)
class StatusView:
    """Vista de solo lectura del estado de un Lote devuelta por Api_Status.

    Es la proyección segura (sin PII) del registro META más el detalle opcional
    de Ítems fallidos. El handler la serializa directamente hacia el cliente.

    Attributes:
        analysis_id: ULID del Lote.
        status: estado del Lote (ver :class:`AnalysisStatus`).
        item_count: número total de Ítems del Lote (``>= 0``).
        done_count: Ítems en estado ``DONE`` (``>= 0``).
        failed_count: Ítems en estado ``FAILED`` (``>= 0``).
        skipped_count: Ítems en estado ``SKIPPED`` (``>= 0``).
        progress: fracción procesada en ``[0.0, 1.0]``, redondeada a 4 decimales
            (criterios 11.2, 11.4).
        created_at: instante UTC de creación del Lote.
        completed_at: instante UTC en que el Lote alcanzó un estado terminal, o
            ``None`` si aún no lo ha alcanzado.
        download_available: ``True`` si el META tiene ``output_s3_key`` (criterio
            11.9).
        failures: tupla de hasta :data:`MAX_FAILURES_IN_STATUS`
            :class:`FailureView`, ordenada por ``item_id`` ascendente; vacía
            salvo que ``include_failures`` esté activo y el Lote esté en
            ``COMPLETED_WITH_ERRORS`` (criterio 11.5).
    """

    analysis_id: str
    status: AnalysisStatus
    item_count: int
    done_count: int
    failed_count: int
    skipped_count: int
    progress: float
    created_at: datetime
    completed_at: datetime | None
    download_available: bool
    failures: tuple[FailureView, ...]


@dataclass(frozen=True, slots=True)
class AnalysisSummaryView:
    """Resumen de un Lote en el listado ``GET /v1/analyses`` (una fila de la página).

    Attributes:
        analysis_id: ULID del Lote.
        status: estado del Lote.
        item_count: número total de Ítems del Lote.
        done_count: Ítems en estado ``DONE``.
        failed_count: Ítems en estado ``FAILED``.
        skipped_count: Ítems en estado ``SKIPPED``.
        progress: fracción procesada en ``[0.0, 1.0]``, redondeada a 4 decimales.
        created_at: instante UTC de creación del Lote.
        completed_at: instante UTC de cierre terminal, o ``None``.
        download_available: ``True`` si el META tiene ``output_s3_key``.
    """

    analysis_id: str
    status: AnalysisStatus
    item_count: int
    done_count: int
    failed_count: int
    skipped_count: int
    progress: float
    created_at: datetime
    completed_at: datetime | None
    download_available: bool


@dataclass(frozen=True, slots=True)
class AnalysisListView:
    """Página del listado de Lotes de un Tenant.

    Attributes:
        analyses: Lotes de la página, ordenados por ``created_at`` descendente y,
            ante igualdad, por ``analysis_id`` descendente (criterio 11.7). Es una
            tupla vacía cuando el Tenant no tiene Lotes (criterio 2.4).
        next_cursor: cursor opaco de continuación cuando hay Lotes posteriores a
            la página devuelta, o ``None`` si no los hay (criterio 11.7).
        page_size: tamaño de página efectivamente aplicado tras acotarlo al tope
            :data:`MAX_PAGE_SIZE` (criterio 11.11).
    """

    analyses: tuple[AnalysisSummaryView, ...]
    next_cursor: str | None
    page_size: int


def _round_progress(ratio: float) -> float:
    """Redondea la fracción de progreso a 4 decimales (criterios 11.2, 11.4).

    :param ratio: fracción ya acotada a ``[0.0, 1.0]`` por
        :pyattr:`AnalysisMeta.progress_ratio`.
    :return: la fracción redondeada a 4 decimales.
    """
    return round(ratio, 4)


class GetAnalysisStatusService:
    """Caso de uso de solo lectura del estado y el listado de Lotes (Api_Status).

    Depende únicamente del puerto :class:`AnalysisRepository`, inyectado en el
    constructor. No realiza ninguna escritura (criterio 11.8): todas sus
    operaciones son consultas.
    """

    def __init__(self, repository: AnalysisRepository) -> None:
        """Inicializa el servicio con el repositorio de persistencia del Lote.

        :param repository: implementación del puerto :class:`AnalysisRepository`
            (en producción, el adaptador de DynamoDB; en pruebas, un doble en
            memoria).
        """
        self._repository = repository

    def get_status(
        self, analysis_id: str, tenant_id: str, *, include_failures: bool = False
    ) -> StatusView:
        """Devuelve el estado de un Lote del Tenant solicitante.

        Operación de solo lectura. El aislamiento multi-tenant es estructural: la
        lectura del META va acotada por ``tenant_id``, de modo que un Lote de otro
        Tenant es indistinguible de uno inexistente.

        :param analysis_id: ULID del Lote recibido en la ruta HTTP.
        :param tenant_id: Tenant del solicitante, derivado exclusivamente del
            claim ``custom:tenant_id`` del Access_Token verificado.
        :param include_failures: si es ``True`` y el Lote está en
            ``COMPLETED_WITH_ERRORS``, la vista incluye hasta
            :data:`MAX_FAILURES_IN_STATUS` Ítems fallidos (``item_id`` +
            ``error_code``, sin PII; criterio 11.5).
        :return: la :class:`StatusView` del Lote.
        :raises NotFoundError: si ``analysis_id`` no es un ULID válido, no existe
            o pertenece a otro Tenant; los tres casos producen un HTTP 404
            idéntico (criterios 2.2, 2.7, 11.10).
        """
        if not is_valid_ulid(analysis_id):
            # Un id malformado no es una consulta válida: se rechaza ANTES de
            # tocar la base, con el mismo 404 que un id inexistente (criterio
            # 2.7). No se consulta la Dynamo_Table.
            raise NotFoundError(reason="invalid_ulid")

        # La pk incluye el tenant_id: este GetItem no puede devolver el Lote de
        # otro Tenant. Si no existe o es ajeno, el repositorio lanza NotFoundError
        # (mismo 404 en ambos casos, criterios 2.2 y 11.10).
        meta = self._repository.get_meta(analysis_id, tenant_id)

        failures: tuple[FailureView, ...] = ()
        if include_failures and meta.status is AnalysisStatus.COMPLETED_WITH_ERRORS:
            failures = self._collect_failures(analysis_id)

        return StatusView(
            analysis_id=meta.analysis_id,
            status=meta.status,
            item_count=meta.item_count,
            done_count=meta.done_count,
            failed_count=meta.failed_count,
            skipped_count=meta.skipped_count,
            progress=_round_progress(meta.progress_ratio),
            created_at=meta.created_at,
            completed_at=meta.completed_at,
            download_available=meta.output_s3_key is not None,
            failures=failures,
        )

    def _collect_failures(self, analysis_id: str) -> tuple[FailureView, ...]:
        """Reúne hasta 100 Ítems fallidos ordenados por ``item_id``, sin PII.

        Recorre los Ítems del Lote a través del generador paginado
        :meth:`AnalysisRepository.iter_items` (no carga el Lote completo en
        memoria), se queda con los que están en ``FAILED`` y proyecta cada uno a
        un :class:`FailureView` con solo ``item_id`` y ``error_code`` (criterio
        11.5). El resultado se ordena por ``item_id`` ascendente —que coincide con
        el orden por ``index`` por el padding del ``item_id``— y se trunca a
        :data:`MAX_FAILURES_IN_STATUS`.

        :param analysis_id: Lote cuyos Ítems fallidos se detallan.
        :return: tupla ordenada de hasta 100 :class:`FailureView`.
        """
        failed = [
            FailureView(
                item_id=item.item_id,
                # Un Ítem FAILED siempre tiene error_code; el fallback preserva la
                # totalidad de la función ante un registro inconsistente.
                error_code=item.error_code or "INTERNAL_ERROR",
            )
            for item in self._repository.iter_items(analysis_id)
            if item.status is ItemStatus.FAILED
        ]
        failed.sort(key=lambda failure: failure.item_id)
        return tuple(failed[:MAX_FAILURES_IN_STATUS])

    def list_analyses(
        self, tenant_id: str, *, page_size: int | None = None, cursor: str | None = None
    ) -> AnalysisListView:
        """Lista los Lotes del Tenant solicitante, paginados por cursor opaco.

        Operación de solo lectura y acotada por Tenant: solo devuelve Lotes cuyo
        ``tenant_id`` coincide con el del token (criterio 2.4). Devuelve una
        colección vacía cuando el Tenant no tiene Lotes.

        :param tenant_id: Tenant del solicitante, del claim ``custom:tenant_id``.
        :param page_size: tamaño de página solicitado. ``None`` aplica
            :data:`DEFAULT_PAGE_SIZE` (criterio 11.7). Un valor mayor que
            :data:`MAX_PAGE_SIZE` se acota a ese tope y se informa en la vista
            (criterio 11.11). Un valor menor que 1 es inválido (criterio 11.12).
        :param cursor: cursor opaco de continuación de una página anterior, o
            ``None`` para la primera página.
        :return: la :class:`AnalysisListView` con la página, el ``next_cursor`` y
            el ``page_size`` efectivo.
        :raises ValidationError: si ``page_size`` es un entero menor que 1
            (criterio 11.12). Un ``cursor`` inválido lo rechaza el repositorio con
            el mismo ``VALIDATION_FAILED`` (criterio 11.13).
        """
        effective_page_size = self._resolve_page_size(page_size)

        metas, next_cursor = self._repository.list_by_tenant(
            tenant_id, effective_page_size, cursor
        )

        analyses = tuple(
            AnalysisSummaryView(
                analysis_id=meta.analysis_id,
                status=meta.status,
                item_count=meta.item_count,
                done_count=meta.done_count,
                failed_count=meta.failed_count,
                skipped_count=meta.skipped_count,
                progress=_round_progress(meta.progress_ratio),
                created_at=meta.created_at,
                completed_at=meta.completed_at,
                download_available=meta.output_s3_key is not None,
            )
            for meta in metas
        )
        return AnalysisListView(
            analyses=analyses, next_cursor=next_cursor, page_size=effective_page_size
        )

    @staticmethod
    def _resolve_page_size(requested: int | None) -> int:
        """Resuelve y valida el tamaño de página efectivo (criterios 11.7, 11.11, 11.12).

        :param requested: ``page_size`` solicitado o ``None``.
        :return: :data:`DEFAULT_PAGE_SIZE` si no se solicitó; el valor acotado a
            :data:`MAX_PAGE_SIZE` si excede el tope; o el valor tal cual si es un
            entero válido dentro de rango.
        :raises ValidationError: si ``requested`` es un entero menor que 1
            (``VALIDATION_FAILED``, criterio 11.12).
        """
        if requested is None:
            return DEFAULT_PAGE_SIZE
        if requested < 1:
            raise ValidationError(reason="page_size_out_of_range", page_size=requested)
        return min(requested, MAX_PAGE_SIZE)
