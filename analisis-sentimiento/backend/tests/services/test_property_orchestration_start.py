"""Property 3 — Idempotencia del arranque de la orquestación (tarea 12.5).

**Validates: Requirements 5.1, 5.14, 5.15, 8.1**

Enunciado (design.md, sección 19, Property 3):

    Entregar el mismo mensaje de SQS N veces produce exactamente una ejecución de
    Step Functions.

y, en su forma observable a nivel de dominio (criterio 8.1):

    Cuando la Cola_Solicitudes entrega el mensaje de un mismo Lote más de una vez
    (hasta 3 recepciones), el Sistema mantiene exactamente **una** ejecución de la
    Maquina_De_Estados para ese ``analysis_id`` y deja los contadores, el
    ``status`` y los resultados del META e ITEM con los **mismos** valores que
    produce una entrega única.

Qué nivel se prueba aquí (y por qué)
------------------------------------
El mecanismo de P3 es doble (design.md, tabla 19.1 — "Sin lotes duplicados:
``StartExecution`` con ``name = analysis_id``"):

1. **Nivel infraestructura (criterios 5.1, 5.14).** El EventBridge_Pipe arranca la
   ejecución con ``name = analysis_id``; una 2ª entrega del mismo mensaje produce
   ``ExecutionAlreadyExists`` y el Pipe la descarta sin segunda ejecución, sin DLQ
   y sin tocar el META. Esto se verifica como Prueba_De_Infraestructura sobre el
   template de CDK (tarea 12.4 / 13.6), no como propiedad de backend: no hay
   lógica de dominio que ejercitar en ese plano.

2. **Nivel dominio (criterios 5.15, 8.1).** Aun si una segunda ejecución llegara a
   arrancar (p. ej. por una entrega duplicada que ganara una carrera, o por un
   reintento del propio Pipe), el **arranque efectivo** del Lote es idempotente: la
   transición ``MarkRunning`` de la Maquina_De_Estados pasa el META a ``RUNNING``
   **sólo si** está en ``PENDING`` (``UpdateItem`` con ``ConditionExpression`` sobre
   ``status``). Si el META ya está en ``RUNNING`` o en un Estado_Terminal, la
   condición falla y la ejecución termina sin reprocesar ningún Ítem y sin tocar el
   META ni los ITEM (criterio 5.15). Esa es la invariante **testeable como
   propiedad de backend**, y es la que cubre este archivo.

El objeto bajo prueba es, por tanto, la transición de arranque guardada por
``status == PENDING``. En el backend esa transición vive hoy **sólo** en la
Maquina_De_Estados (estado ``MarkRunning`` del ``workflow_stack``, tarea 12.3): no
existe un servicio ``mark_running`` ni un método del puerto que la encarne. Para
probar su **contrato** sin acoplarse a CDK ni a DynamoDB, se reproduce exactamente
—igual que la P2 reproduce el contrato de ``complete_item`` sobre un fake— con:

* :class:`MetaRepositoryFake`: doble en memoria que modela el ``UpdateItem``
  condicionado a ``PENDING`` con consistencia fuerte (una relectura observa el
  ``RUNNING`` que escribió el arranque anterior); y
* :func:`start_orchestration`: función pura que mirror-ea la semántica del estado
  ``MarkRunning`` (leer META → si ``PENDING`` transicionar a ``RUNNING`` y registrar
  ``execution_arn``; si no, terminar sin efectos — criterio 5.15).

El modelado permite aseverar el **estado** final (lo que P3 afirma), no meras
interacciones, conforme a la sección 21.2 del diseño (fakes, no mocks).

Propiedades aseveradas
----------------------
Para cualquier número de entregas ``N`` del mismo mensaje y cualquier
``status`` inicial del META:

* (a) META en ``PENDING``: exactamente **un** arranque es efectivo (uno solo pasa
  ``PENDING`` → ``RUNNING`` y registra el ``execution_arn``); todas las entregas
  restantes son no-ops. El estado final es idéntico al de una entrega única: META
  en ``RUNNING`` con el ``execution_arn`` del arranque ganador (criterios 5.1,
  8.1).
* (b) META ya en un Estado_Terminal: **ningún** arranque es efectivo, con
  independencia de ``N``; el META no cambia de ``status`` ni se le registra
  ``execution_arn`` (criterio 5.15).
* (c) META ya en ``RUNNING`` (una ejecución previa ya arrancó): toda entrega
  posterior es no-op; no hay segundo arranque efectivo y el ``execution_arn``
  original se conserva (criterios 5.14, 8.1).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Final

from hypothesis import given
from hypothesis import strategies as st

from sentiment.domain.models import (
    TERMINAL_ANALYSIS_STATUSES,
    AnalysisMeta,
    AnalysisStatus,
)

_ANALYSIS_ID: Final = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_TENANT_ID: Final = "acme-corp"
_OWNER_SUB: Final = "sub-123"
_CREATED_AT: Final = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)

#: ARN base de la ejecución de Step Functions cuyo nombre es el ``analysis_id``
#: (criterio 5.1). Cada entrega propone su propio ARN para evidenciar cuál ganó.
_EXEC_ARN_PREFIX: Final = (
    "arn:aws:states:us-east-1:123456789012:execution:sentiment-pipeline:"
)


def _execution_arn(delivery_index: int) -> str:
    """ARN propuesto por la entrega ``delivery_index`` para su intento de arranque.

    El nombre de la ejecución es siempre el ``analysis_id`` (criterio 5.1); el
    sufijo distingue qué entrega concreta propuso el ARN, para poder aseverar que
    el que queda registrado es el del **único** arranque efectivo.
    """
    return f"{_EXEC_ARN_PREFIX}{_ANALYSIS_ID}:{delivery_index}"


# ---------------------------------------------------------------------------
# Doble en memoria con estado (fake, no mock) del contrato ``MarkRunning``.
# ---------------------------------------------------------------------------
class MetaRepositoryFake:
    """Doble en memoria del arranque guardado del META (estado ``MarkRunning``).

    Reproduce el ``UpdateItem`` condicionado a ``status == PENDING`` con el que la
    Maquina_De_Estados materializa la transición de arranque (criterio 5.2),
    **sin** depender de DynamoDB ni de CDK:

    * El ``status`` del META y su ``execution_arn`` son el estado real que
      evoluciona entre entregas; :meth:`get_meta` lo expone con consistencia
      fuerte (una entrega repetida ve el ``RUNNING`` que escribió el arranque
      anterior).
    * :meth:`mark_running` aplica la transición **sólo** desde ``PENDING``: pasa el
      META a ``RUNNING``, registra el ``execution_arn`` recibido y devuelve
      ``True`` exactamente una vez; cualquier entrega posterior encuentra el META
      en un estado distinto de ``PENDING``, no lo modifica y devuelve ``False``
      (idempotencia del arranque: criterios 5.14, 5.15, 8.1).
    """

    def __init__(self, *, initial_status: AnalysisStatus) -> None:
        self._meta = AnalysisMeta(
            analysis_id=_ANALYSIS_ID,
            tenant_id=_TENANT_ID,
            owner_sub=_OWNER_SUB,
            status=initial_status,
            item_count=5,
            created_at=_CREATED_AT,
            updated_at=_CREATED_AT,
        )
        self.get_meta_calls = 0
        self.mark_running_calls = 0
        #: Número de arranques realmente aplicados (``PENDING`` → ``RUNNING``).
        #: Debe ser 0 o 1 con independencia del número de entregas.
        self.transitions_applied = 0

    def get_meta(self) -> AnalysisMeta:
        """Relee el META con consistencia fuerte (una copia inmutable del estado)."""
        self.get_meta_calls += 1
        # Se devuelve una copia para que el llamante no pueda mutar el estado
        # interno salvo a través de la transición condicionada.
        return replace(self._meta)

    def mark_running(self, execution_arn: str) -> bool:
        """Transiciona ``PENDING`` → ``RUNNING`` de forma condicionada e idempotente.

        Mirror del ``UpdateItem`` con ``ConditionExpression`` del estado
        ``MarkRunning``: la escritura sólo se aplica si el META sigue en
        ``PENDING``. Si ya está en ``RUNNING`` o en un Estado_Terminal, la
        condición falla, no se modifica nada y se devuelve ``False``.

        :param execution_arn: ARN de la ejecución que intenta el arranque; su
            nombre es el ``analysis_id`` (criterio 5.1).
        :return: ``True`` si aplicó la transición (arranque efectivo); ``False``
            si el META ya no estaba en ``PENDING`` (arranque descartado).
        """
        self.mark_running_calls += 1
        if self._meta.status is not AnalysisStatus.PENDING:
            # Condición fallida: ya arrancó (RUNNING) o ya terminó (terminal).
            # No se toca el META ni se registra un segundo execution_arn.
            return False
        self._meta.status = AnalysisStatus.RUNNING
        self._meta.execution_arn = execution_arn
        self.transitions_applied += 1
        return True

    @property
    def status(self) -> AnalysisStatus:
        """Estado actual del META (lo que vería una lectura consistente)."""
        return self._meta.status

    @property
    def execution_arn(self) -> str | None:
        """ARN de la ejecución registrada, o ``None`` si no hubo arranque efectivo."""
        return self._meta.execution_arn


def start_orchestration(repo: MetaRepositoryFake, execution_arn: str) -> bool:
    """Mirror puro de la semántica del estado ``MarkRunning`` de la Maquina_De_Estados.

    Reproduce lo que hace una ejecución al comenzar (criterios 5.2, 5.15): relee el
    META y, **sólo si** está en ``PENDING``, lo transiciona a ``RUNNING`` registrando
    el ``execution_arn``. Si el META ya está en ``RUNNING`` o en un Estado_Terminal,
    la ejecución termina sin procesar Ítems y sin modificar el META ni los ITEM; el
    arranque no es efectivo.

    :param repo: doble del META sobre el que se intenta el arranque.
    :param execution_arn: ARN propuesto por esta entrega.
    :return: ``True`` si esta entrega produjo el arranque efectivo; ``False`` si
        fue descartada por no estar el META en ``PENDING``.
    """
    meta = repo.get_meta()
    if meta.status is not AnalysisStatus.PENDING:
        # Criterio 5.15 (terminal) / 5.14 (ya RUNNING): terminar sin efectos.
        return False
    return repo.mark_running(execution_arn)


# ---------------------------------------------------------------------------
# Estrategias.
# ---------------------------------------------------------------------------
# Número de entregas del mismo mensaje. El criterio 8.1 acota el caso real a "más
# de una vez, hasta un máximo de 3 recepciones"; se cubre ese rango y un poco más
# (hasta 5, como en el enunciado de design.md) para no atar la propiedad al tope
# concreto de reintentos de la Cola_Solicitudes.
_entregas = st.integers(min_value=1, max_value=5)

# Estados terminales del Lote, para el caso (b): una ejecución que arranca sobre un
# META ya terminal debe terminar sin efectos (criterio 5.15).
_estados_terminales = st.sampled_from(sorted(TERMINAL_ANALYSIS_STATUSES))


# ---------------------------------------------------------------------------
# (a) META en PENDING: N entregas == un solo arranque efectivo.
# ---------------------------------------------------------------------------
@given(entregas=_entregas)
def test_duplicate_delivery_starts_orchestration_exactly_once(entregas: int) -> None:
    """N entregas de un mensaje PENDING producen exactamente un arranque efectivo.

    Es el enunciado central de P3: aunque el mismo mensaje de la Cola_Solicitudes
    se entregue ``N`` veces (entrega al-menos-una-vez), la transición de arranque
    ``PENDING`` → ``RUNNING`` se aplica una **única** vez y el resto de entregas son
    no-ops. El estado final —META en ``RUNNING`` con el ``execution_arn`` del
    arranque ganador— es idéntico al de una entrega única (criterios 5.1, 8.1).
    """
    repo = MetaRepositoryFake(initial_status=AnalysisStatus.PENDING)

    resultados = [start_orchestration(repo, _execution_arn(i)) for i in range(entregas)]

    # Exactamente un arranque efectivo, con independencia de N (criterio 8.1).
    assert sum(resultados) == 1
    assert resultados[0] is True
    assert all(r is False for r in resultados[1:])
    assert repo.transitions_applied == 1

    # Estado final idéntico al de una entrega única: RUNNING con el ARN del
    # arranque ganador (la primera entrega), nombre = analysis_id (criterio 5.1).
    assert repo.status is AnalysisStatus.RUNNING
    assert repo.execution_arn == _execution_arn(0)
    assert repo.execution_arn is not None
    assert repo.execution_arn.endswith(_ANALYSIS_ID + ":0")


# ---------------------------------------------------------------------------
# (b) META ya terminal: ningún arranque efectivo, META intacto (criterio 5.15).
# ---------------------------------------------------------------------------
@given(entregas=_entregas, status=_estados_terminales)
def test_execution_on_terminal_meta_never_starts(
    entregas: int, status: AnalysisStatus
) -> None:
    """Una ejecución que arranca sobre un META terminal termina sin efectos (criterio 5.15).

    Si una ejecución comienza y el META ya está en un Estado_Terminal
    (``COMPLETED`` / ``COMPLETED_WITH_ERRORS`` / ``FAILED`` / ``EXPIRED``), la
    condición de ``MarkRunning`` falla y la ejecución termina sin procesar ningún
    Ítem y sin modificar el META: no cambia de ``status`` ni se le registra un
    ``execution_arn``, con independencia del número de entregas.
    """
    repo = MetaRepositoryFake(initial_status=status)

    resultados = [start_orchestration(repo, _execution_arn(i)) for i in range(entregas)]

    assert all(r is False for r in resultados)  # ningún arranque efectivo
    assert repo.transitions_applied == 0
    assert repo.mark_running_calls == 0  # el guard corta antes del UpdateItem
    assert repo.status is status  # el Estado_Terminal no cambia
    assert repo.execution_arn is None  # no se registra ninguna ejecución


# ---------------------------------------------------------------------------
# (c) META ya en RUNNING: no hay segundo arranque; el ARN original se conserva.
# ---------------------------------------------------------------------------
@given(entregas=_entregas)
def test_already_running_meta_is_not_restarted(entregas: int) -> None:
    """Un META ya en RUNNING no vuelve a arrancar: la 2ª entrega es no-op (criterios 5.14, 8.1).

    Modela la carrera del criterio 5.14: una ejecución ya arrancó (META en
    ``RUNNING`` con su ``execution_arn``) y llega una segunda entrega del mismo
    mensaje. El arranque guardado la descarta sin segundo efecto y conserva el ARN
    de la ejecución original; los contadores y el ``status`` quedan como tras una
    entrega única.
    """
    repo = MetaRepositoryFake(initial_status=AnalysisStatus.PENDING)

    # Primer arranque efectivo (la ejecución "ganadora", ARN con sufijo 0).
    assert start_orchestration(repo, _execution_arn(0)) is True
    arn_original = repo.execution_arn

    # Entregas duplicadas posteriores: todas descartadas (criterio 5.14).
    reentregas = [
        start_orchestration(repo, _execution_arn(i)) for i in range(1, entregas + 1)
    ]

    assert all(r is False for r in reentregas)
    assert repo.transitions_applied == 1  # sigue siendo un único arranque
    assert repo.status is AnalysisStatus.RUNNING
    assert repo.execution_arn == arn_original  # el ARN original no se sobrescribe
