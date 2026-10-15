"""Handler del sweeper ``Reconcile_Stuck`` (adaptador de EventBridge Scheduler).

Este módulo es el **adaptador de transporte *thin*** (diseño sección 2.1, tarea
9.3) entre la regla de EventBridge Scheduler que dispara el barrido cada 15
minutos (criterio 10.10) y el caso de uso puro
:class:`~sentiment.services.reconcile_stuck.ReconcileStuckService` (tarea 7.11,
sección 10.6 del diseño). Su única responsabilidad es:

#. Adaptar el evento del Scheduler —que no transporta ninguna carga útil
   significativa— a una llamada a :meth:`ReconcileStuckService.reconcile`.
#. Registrar el resumen de la pasada (:class:`ReconcileReport`) usando **solo
   recuentos** (nunca PII; criterios 15.1, 15.2).

El handler **no** contiene lógica de negocio: la doble condición de abandono, el
cierre de Lotes como ``EXPIRED``, la republicación de eventos pendientes y la
emisión de la métrica ``StuckAnalyses`` viven en el servicio. Aquí solo se
construyen los adaptadores reales una vez por contenedor (reutilización entre
invocaciones en caliente; criterios 9.18, 17.11) y se traduce el evento.

Construcción una vez por contenedor, perezosa (reutilización + importabilidad)
------------------------------------------------------------------------------
El servicio y sus colaboradores se instancian **una sola vez por contenedor**,
pero de forma **perezosa**: no en el ámbito del módulo, sino la primera vez que
se invoca el handler (:func:`_build_service`, memoizado en :data:`_service`). Un
contenedor caliente reutiliza esa instancia y los clientes de boto3 (singletons
de :mod:`sentiment.adapters.aws_clients`) entre invocaciones, eliminando el coste
de reconstrucción en cada barrido (criterios 9.18, 17.11). La construcción es
perezosa porque :class:`~sentiment.adapters.dynamo_repository.DynamoRepository`
resuelve ``TABLE_NAME`` en su constructor: hacerlo a nivel de módulo rompería el
recorrido de importabilidad del paquete, que exige que importar cualquier módulo
no lea configuración del entorno (criterio 17.7). Posponerlo a la primera
invocación mantiene ambas garantías: import seguro y una sola construcción por
contenedor.

Emisor de la métrica ``StuckAnalyses`` (criterio 10.15)
-------------------------------------------------------
El servicio depende de un puerto :class:`StuckMetricEmitter` para publicar la
métrica. :class:`EmfStuckMetricEmitter` es la implementación concreta sobre las
métricas EMF de Powertools (:mod:`sentiment.observability.metrics`): registra
``StuckAnalyses`` **siempre**, incluso con valor 0, y las vuelca a CloudWatch con
``flush_metrics`` al final de la pasada para mantener la serie temporal continua
(la alarma crítica dispara con valores >= 1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.adapters.eventbridge_publisher import EventBridgePublisher
from sentiment.adapters.stepfunctions_adapter import StepFunctionsAdapter
from sentiment.observability.logging import get_logger
from sentiment.observability.metrics import MetricName, MetricUnit, get_metrics
from sentiment.services.reconcile_stuck import (
    ReconcileReport,
    ReconcileStuckService,
)

if TYPE_CHECKING:
    from aws_lambda_powertools import Metrics

__all__ = ["EmfStuckMetricEmitter", "lambda_handler"]

logger = get_logger(service="reconcile_stuck")

#: Namespace de la métrica ``StuckAnalyses``; coincide con el del resto de
#: métricas del Sistema para que la alarma del ``observability_stack`` la
#: encuentre bajo la misma agrupación.
_metrics: Final[Metrics] = get_metrics()


class EmfStuckMetricEmitter:
    """Emisor de la métrica ``StuckAnalyses`` sobre EMF (puerto ``StuckMetricEmitter``).

    Implementa estructuralmente
    :class:`~sentiment.services.reconcile_stuck.StuckMetricEmitter` publicando la
    métrica a través del objeto :class:`~aws_lambda_powertools.metrics.Metrics` de
    Powertools (EMF). El objeto de métricas se inyecta por constructor (inversión
    de dependencias): por defecto, el singleton reutilizable del módulo; las
    pruebas pueden inyectar uno propio y verificar el valor emitido.
    """

    def __init__(self, metrics: Metrics | None = None) -> None:
        """Construye el emisor.

        :param metrics: objeto de métricas de Powertools sobre el que registrar;
            si es ``None``, se usa el singleton del módulo. No se llama a AWS ni se
            lee configuración obligatoria en la construcción.
        """
        self._metrics: Metrics = metrics if metrics is not None else _metrics

    def emit_stuck_analyses(self, count: int) -> None:
        """Registra la métrica ``StuckAnalyses`` con ``count`` (criterio 10.15).

        El valor se registra **siempre**, incluso con ``count == 0``, para que la
        serie temporal de CloudWatch no tenga huecos y la alarma crítica (que
        dispara con valores >= 1) no reporte falsos "sin datos". El vuelco a
        CloudWatch (``flush_metrics``) lo hace el handler al final de la pasada,
        de modo que la métrica se emite una sola vez por invocación.

        :param count: número de Lotes cerrados por abandono en la pasada; ``>= 0``.
        """
        self._metrics.add_metric(
            name=MetricName.STUCK_ANALYSES.value,
            unit=MetricUnit.Count,
            value=float(count),
        )


# Instancia memoizada del servicio: se construye en la primera invocación y se
# reutiliza en las sucesivas del mismo contenedor caliente (una vez por
# contenedor, criterios 9.18, 17.11). No se construye a nivel de módulo para no
# romper la importabilidad del paquete (criterio 17.7).
_service: ReconcileStuckService | None = None


def _build_service() -> ReconcileStuckService:
    """Construye el servicio del sweeper con sus adaptadores reales.

    El sweeper es un rol interno que recorre todos los tenants por diseño (no usa
    session tags): se cablean el repositorio DynamoDB, el inspector de ejecuciones
    de Step Functions, el publicador del Bus_Eventos y el emisor de la métrica
    ``StuckAnalyses`` sobre EMF, más un reloj UTC.

    :return: el :class:`ReconcileStuckService` listo para ejecutar una pasada.
    """
    return ReconcileStuckService(
        repository=DynamoRepository(),
        inspector=StepFunctionsAdapter(),
        events=EventBridgePublisher(),
        metrics=EmfStuckMetricEmitter(),
        clock=lambda: datetime.now(tz=UTC),
    )


def _get_service() -> ReconcileStuckService:
    """Devuelve el servicio memoizado, construyéndolo en la primera invocación.

    Posponer la construcción a la primera invocación (en lugar de hacerla a nivel
    de módulo) preserva la importabilidad del paquete sin configuración y, a la
    vez, garantiza una sola construcción por contenedor.

    :return: la instancia única del servicio para este contenedor.
    """
    global _service
    if _service is None:
        _service = _build_service()
    return _service


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """Adapta el disparo del Scheduler a una pasada del sweeper (criterio 10.10).

    El evento de EventBridge Scheduler no transporta carga útil significativa: la
    pasada no depende de su contenido, por lo que no se inspecciona (ni se loguea,
    para no arrastrar metadatos del Scheduler a CloudWatch). El handler delega en
    el servicio, registra el resumen con solo recuentos y vuelca la métrica
    ``StuckAnalyses`` (que el servicio ya registró, incluso en 0) a CloudWatch.

    :param event: evento del Scheduler. No se usa su contenido.
    :param context: contexto de ejecución de la Lambda. No se usa.
    :return: el resumen de la pasada como ``dict`` de recuentos, útil para la
        inspección del resultado de la ejecución y para las pruebas. **No**
        contiene PII: solo cardinalidades.

    Postcondiciones:
        - Ejecuta exactamente una pasada de :meth:`ReconcileStuckService.reconcile`.
        - Publica la métrica ``StuckAnalyses`` una vez por invocación, incluso con
          valor 0 (criterio 10.15).
        - El log y el valor de retorno contienen solo recuentos (criterio 15.2).
    """
    try:
        report: ReconcileReport = _get_service().reconcile()
    finally:
        # El vuelco se hace en `finally` para que un fallo a mitad de pasada no
        # pierda las métricas ya registradas (p. ej. la de un parcial). El
        # servicio registra `StuckAnalyses` siempre; aquí solo se vuelca a EMF.
        _metrics.flush_metrics()

    logger.info(
        "reconcile_pass_completed",
        extra={
            "candidates_examined": report.candidates_examined,
            "expired": report.expired,
            "events_republished": report.events_republished,
            "skipped": report.skipped,
        },
    )

    return {
        "candidates_examined": report.candidates_examined,
        "expired": report.expired,
        "events_republished": report.events_republished,
        "skipped": report.skipped,
    }
