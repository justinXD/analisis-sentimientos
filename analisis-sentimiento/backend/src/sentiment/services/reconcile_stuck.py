"""Caso de uso ``reconcile_stuck``: el barrido que garantiza el estado terminal.

Este módulo implementa la lógica de negocio del sweeper (``Reconcile_Stuck``), la
Lambda que una regla de EventBridge Scheduler invoca cada 15 minutos (criterio
10.10) y que convierte la "terminación garantizada" (Property P4) de una
aspiración en una propiedad verificable. Se describe en la sección 10.6 del
diseño y satisface el Requirement 10 (criterios 10.10 a 10.15 y 10.19).

Rol en la arquitectura (hexagonal, diseño 2.1):

* El servicio es **puro respecto de la infraestructura**: no importa ``boto3`` ni
  ningún adaptador concreto. Depende de cuatro colaboradores inyectados en el
  constructor —:class:`~sentiment.domain.ports.AnalysisRepository` (barrido,
  cierre del Lote y de sus Ítems, marca de evento publicado),
  :class:`~sentiment.domain.ports.ExecutionInspector` (estado de la ejecución de
  Step Functions), :class:`~sentiment.domain.ports.EventPublisher` (publicación
  del Evento_Finalizacion) y un :class:`StuckMetricEmitter` (publicación de la
  métrica ``StuckAnalyses``)— más un ``clock`` inyectado, de modo que es
  verificable con dobles en memoria.
* El handler ``reconcile_stuck`` (tarea 9.3) es un adaptador *thin* que adapta el
  evento del Scheduler a una llamada a :meth:`ReconcileStuckService.reconcile`.

Doble condición de abandono (criterios 10.11, 10.12, 10.13)
-----------------------------------------------------------
Un Lote no terminal se considera **abandonado** solo si se cumplen **ambas**
condiciones; en caso contrario, el sweeper no toca ni el META ni los Ítems
(criterio 10.13):

(a) **No avanza:** su ``updated_at`` es anterior en ``STUCK_THRESHOLD`` segundos
    (900 s) o más al instante de evaluación. El barrido ya acota los candidatos a
    los que cumplen esta condición vía :meth:`AnalysisRepository.list_sweep_candidates`
    con ``older_than = now - STUCK_THRESHOLD`` (criterio 10.10).
(b) **Su orquestación ya no corre:** :meth:`ExecutionInspector.describe_status`
    sobre el ``analysis_id`` devuelve un estado **distinto de ``RUNNING``**
    (``SUCCEEDED``/``FAILED``/``TIMED_OUT``/``ABORTED``), **o** ``None``
    (``ExecutionDoesNotExist``, criterio 10.12).

La condición (b) es la que impide expirar un Lote legítimo cuya ejecución sigue
``RUNNING`` (p. ej. con Bedrock degradado durante horas): mientras la ejecución
corra, el Lote no se expira; el techo de 3 h de la Maquina_De_Estados garantiza
que esa ejecución termine y, a partir de ahí, el sweeper lo cierre en la pasada
siguiente.

Dos desenlaces por Lote candidato
----------------------------------
#. **No terminal y abandonado (criterios 10.11, 10.12, 10.14, 16.9 vía 19.1):**
   se cierra el META como ``EXPIRED`` y sus Ítems ``PENDING`` como ``FAILED`` con
   ``error_code = EXECUTION_ABANDONED``; se publica el Evento_Finalizacion antes
   de terminar la ejecución y se marca el evento como publicado (lo retira del
   índice SWEEP). El cierre del META es condicional a no ser ya terminal: si
   ``aggregate_results`` ganó la carrera entre la lectura del barrido y la
   escritura, el sweeper no sobrescribe el estado terminal.
#. **Terminal sin evento publicado (criterio 10.19):** un Lote que
   ``aggregate_results`` cerró pero cuyo Evento_Finalizacion no llegó a
   publicarse (p. ej. murió antes de ``PutEvents``) aparece también en el índice
   SWEEP. El sweeper **solo** publica su evento, **sin** modificar ``status``,
   contadores, ``output_s3_key`` ni ``completed_at``, y lo marca como publicado.

El orden es *publicar y luego marcar*: da entrega al-menos-una-vez; una caída
entre ambos pasos deja el Lote en el índice y una pasada posterior republica. Los
consumidores deduplican por ``event_id = {analysis_id}:{status}`` (criterio 10.20).

Métrica ``StuckAnalyses`` (criterio 10.15)
------------------------------------------
Al terminar, el sweeper publica **siempre** la métrica ``StuckAnalyses`` con el
número de Lotes cerrados por abandono en esa ejecución, **incluido el valor 0**.
Publicarla incluso en 0 mantiene la serie temporal continua para la alarma crítica
(que dispara con valores ≥ 1) y evita falsos "sin datos".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, auto
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from sentiment.domain.errors import EXECUTION_ABANDONED
from sentiment.domain.models import AnalysisStatus, ItemStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sentiment.domain.models import AnalysisMeta
    from sentiment.domain.ports import (
        AnalysisRepository,
        EventPublisher,
        ExecutionInspector,
    )

__all__ = [
    "STUCK_THRESHOLD_SECONDS",
    "RUNNING_EXECUTION_STATUS",
    "ReconcileReport",
    "StuckMetricEmitter",
    "ReconcileStuckService",
]


class _Outcome(Enum):
    """Desenlace de reconciliar un candidato, para la contabilidad de la pasada.

    No se serializa ni se expone fuera del servicio: solo clasifica el resultado
    de :meth:`ReconcileStuckService._reconcile_one` para los recuentos del
    :class:`ReconcileReport`.
    """

    EXPIRED = auto()
    REPUBLISHED = auto()
    SKIPPED = auto()

#: Umbral de inactividad, en segundos, a partir del cual un Lote no terminal es
#: candidato a abandono (criterios 10.11, 10.12). Son 900 s (15 min): cubre la
#: ventana máxima de reintentos de la Cola_Solicitudes (3 por 180 s = 540 s) más un
#: margen de 360 s para la latencia de Pipes y ``StartExecution`` (diseño 10.6.1).
#: La invariante ``stuck_threshold >= ventana_cola + margen`` se verifica en la
#: síntesis de la Infraestructura; aquí es el valor de referencia del dominio.
STUCK_THRESHOLD_SECONDS: Final = 900

#: Único estado de una ejecución de Step Functions que **impide** considerar
#: abandonado un Lote: mientras la ejecución corra, el Lote no se expira (condición
#: (b) de la doble condición, criterio 10.13). Cualquier otro estado terminal
#: —o ``None`` (``ExecutionDoesNotExist``)— satisface la condición de abandono.
RUNNING_EXECUTION_STATUS: Final = "RUNNING"


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Resumen de una pasada del sweeper, útil para el log del handler y las pruebas.

    Attributes:
        candidates_examined: número de candidatos que devolvió el barrido.
        expired: número de Lotes cerrados por abandono (``EXPIRED``) en esta
            pasada. Es el valor publicado como métrica ``StuckAnalyses`` (criterio
            10.15).
        events_republished: número de Lotes terminales a los que se les publicó el
            Evento_Finalizacion pendiente (criterio 10.19), sin cerrarlos de nuevo.
        skipped: número de candidatos que no cumplían la doble condición de
            abandono y se dejaron intactos (criterio 10.13).
    """

    candidates_examined: int
    expired: int
    events_republished: int
    skipped: int


@runtime_checkable
class StuckMetricEmitter(Protocol):
    """Puerto mínimo de emisión de la métrica ``StuckAnalyses`` del sweeper.

    Se expresa como un ``Protocol`` local (no en ``domain/ports.py``) porque es
    específico de este caso de uso: el adaptador real lo implementa sobre las
    métricas EMF de Powertools (``observability/metrics.py``), pero el servicio
    permanece puro y verificable con un doble en memoria que solo registra el
    valor emitido.
    """

    def emit_stuck_analyses(self, count: int) -> None:
        """Publica la métrica ``StuckAnalyses`` con ``count`` (criterio 10.15).

        Debe invocarse **siempre** al terminar una pasada, incluso con ``count``
        igual a 0, para mantener la serie temporal continua.

        :param count: número de Lotes cerrados por abandono en la pasada; ``>= 0``.
        """
        ...


class ReconcileStuckService:
    """Cierra Lotes abandonados y publica eventos pendientes en una pasada del sweeper.

    Encapsula el algoritmo de ``reconcile_stuck`` (sección 10.6 del diseño) con sus
    colaboradores inyectados como puertos. Es reutilizable y verificable sin AWS:
    basta inyectar dobles en memoria que satisfagan los protocolos.
    """

    def __init__(
        self,
        repository: AnalysisRepository,
        inspector: ExecutionInspector,
        events: EventPublisher,
        metrics: StuckMetricEmitter,
        clock: Callable[[], datetime],
    ) -> None:
        """Inyecta los puertos y el reloj del servicio.

        :param repository: puerto de persistencia; aporta el barrido de candidatos,
            el cierre del META (condicional a no terminal), el cierre de Ítems
            huérfanos y la marca de evento publicado.
        :param inspector: puerto de inspección de la ejecución de Step Functions;
            resuelve la condición (b) de la doble condición de abandono.
        :param events: puerto de publicación del Evento_Finalizacion.
        :param metrics: emisor de la métrica ``StuckAnalyses`` (siempre, incluso 0).
        :param clock: fuente del instante UTC de evaluación; fija el ``older_than``
            del barrido. Inyectable para pruebas deterministas.
        """
        self._repository = repository
        self._inspector = inspector
        self._events = events
        self._metrics = metrics
        self._clock = clock

    def reconcile(self) -> ReconcileReport:
        """Ejecuta una pasada del sweeper y devuelve su resumen.

        Toma el instante de evaluación del ``clock`` inyectado, recorre los
        candidatos del barrido (``updated_at`` anterior en ``STUCK_THRESHOLD`` o
        más, o terminales sin evento publicado), aplica a cada uno el desenlace que
        corresponde y, al terminar, publica **siempre** la métrica ``StuckAnalyses``
        con el número de Lotes expirados (criterio 10.15), incluido 0.

        El recuento publicado es el de Lotes **cerrados por abandono** en esta
        pasada; los Lotes a los que solo se les republicó el evento pendiente
        (criterio 10.19) no cuentan como atascados.

        :return: un :class:`ReconcileReport` con los recuentos de la pasada.
        """
        now = self._clock()
        older_than = now - timedelta(seconds=STUCK_THRESHOLD_SECONDS)

        candidates = 0
        expired = 0
        republished = 0
        skipped = 0

        # El barrido pagina internamente (generador): no carga todos los
        # candidatos en memoria (criterio 10.10, patrón de acceso 8).
        for meta in self._repository.list_sweep_candidates(older_than):
            candidates += 1
            outcome = self._reconcile_one(meta)
            if outcome is _Outcome.EXPIRED:
                expired += 1
            elif outcome is _Outcome.REPUBLISHED:
                republished += 1
            else:
                skipped += 1

        # La métrica se publica SIEMPRE, incluso con expired == 0 (criterio 10.15).
        self._metrics.emit_stuck_analyses(expired)

        return ReconcileReport(
            candidates_examined=candidates,
            expired=expired,
            events_republished=republished,
            skipped=skipped,
        )

    def _reconcile_one(self, meta: AnalysisMeta) -> _Outcome:
        """Decide y aplica el desenlace de un único candidato del barrido.

        * **Terminal:** aparece en el índice solo si su evento no consta publicado;
          se republica el Evento_Finalizacion sin modificar su estado (criterio
          10.19) y se marca como publicado.
        * **No terminal:** se evalúa la doble condición de abandono. Si se cumple,
          se cierra como ``EXPIRED`` (criterios 10.11, 10.12); si no, se deja
          intacto (criterio 10.13).

        :param meta: registro META del candidato, tal como lo devolvió el barrido.
        :return: el :class:`_Outcome` aplicado, para la contabilidad de la pasada.
        """
        # Un Lote terminal en el índice SWEEP es un "terminal sin evento publicado"
        # (criterio 10.19): solo se publica su evento, sin tocar su estado.
        if meta.is_terminal:
            self._publish_and_mark(meta)
            return _Outcome.REPUBLISHED

        # No terminal: evaluar la condición (b). La condición (a) ya la garantiza
        # el barrido (older_than = now - STUCK_THRESHOLD, criterio 10.10).
        if not self._execution_abandoned(meta.analysis_id):
            # La ejecución sigue RUNNING: no es abandono, se deja intacto (10.13).
            return _Outcome.SKIPPED

        self._expire(meta)
        return _Outcome.EXPIRED

    def _execution_abandoned(self, analysis_id: str) -> bool:
        """Resuelve la condición (b): la ejecución ya no corre (criterios 10.11, 10.12).

        :param analysis_id: ULID del Lote; es también el nombre de la ejecución de
            Step Functions (arranque idempotente, diseño 10.6).
        :return: ``True`` si la ejecución no existe (``None``) o está en un estado
            terminal (distinto de ``RUNNING``); ``False`` si sigue ``RUNNING``.
        """
        status = self._inspector.describe_status(analysis_id)
        # None (ExecutionDoesNotExist, criterio 10.12) o cualquier estado terminal
        # satisface el abandono; solo RUNNING lo impide (criterio 10.13).
        return status != RUNNING_EXECUTION_STATUS

    def _expire(self, meta: AnalysisMeta) -> None:
        """Cierra un Lote abandonado como ``EXPIRED`` y publica su Evento_Finalizacion.

        Transiciona cada Ítem ``PENDING`` a ``FAILED`` con
        ``error_code = EXECUTION_ABANDONED`` (criterio 10.11) y el META a
        ``EXPIRED`` de forma condicional a no ser ya terminal: si
        ``aggregate_results`` ganó la carrera, :meth:`AnalysisRepository.finalize_analysis`
        no sobrescribe el estado terminal preexistente. Tras releer el META cerrado,
        publica el Evento_Finalizacion antes de terminar (criterio 10.14) y lo
        marca como publicado.

        :param meta: registro META del Lote abandonado.
        """
        analysis_id = meta.analysis_id

        # Ítems huérfanos: cada PENDING -> FAILED/EXECUTION_ABANDONED (criterio
        # 10.11). fail_item es atómico e idempotente; un Ítem ya terminal no se
        # altera (devuelve False), lo que mantiene el barrido reintentable.
        done = failed = skipped = 0
        for item in self._repository.iter_items(analysis_id):
            if item.status == ItemStatus.PENDING:
                self._repository.fail_item(analysis_id, item.item_id, EXECUTION_ABANDONED)
                failed += 1
            elif item.status == ItemStatus.DONE:
                done += 1
            elif item.status == ItemStatus.FAILED:
                failed += 1
            elif item.status == ItemStatus.SKIPPED:
                skipped += 1

        # Cierre del META como EXPIRED, condicional a no ser ya terminal. Los
        # recuentos provienen de los Ítems reales reconciliados, no del acumulado.
        self._repository.finalize_analysis(
            analysis_id,
            AnalysisStatus.EXPIRED,
            (done, failed, skipped),
            meta.output_s3_key,
        )

        # Releemos el META ya cerrado para que el evento lleve el status terminal
        # y los contadores reconciliados persistidos.
        final_meta = self._repository.get_meta(analysis_id, meta.tenant_id)
        self._publish_and_mark(final_meta)

    def _publish_and_mark(self, meta: AnalysisMeta) -> None:
        """Publica el Evento_Finalizacion de un Lote y lo retira del índice SWEEP.

        Orden *publicar y luego marcar* (entrega al-menos-una-vez, criterios 10.14,
        10.19, 10.20): una caída entre ambos pasos deja el Lote en el índice y una
        pasada posterior republica; los consumidores deduplican por ``event_id``.

        :param meta: registro META terminal cuyo evento se publica.
        """
        self._events.publish_analysis_completed(meta)
        self._repository.mark_event_published(meta.analysis_id)
