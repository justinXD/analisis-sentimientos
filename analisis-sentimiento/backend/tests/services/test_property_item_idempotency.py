"""Property 2 — Idempotencia del procesamiento de un Ítem (tarea 7.6).

**Validates: Requirements 8.2, 8.3, 8.4, 8.5, 8.9**

Enunciado (design.md, sección 19, Property 2):

    Procesar el mismo ``item_id`` N veces produce el mismo estado final que
    procesarlo una vez, y los contadores se incrementan exactamente una vez.

Esta prueba ejercita el caso de uso
:class:`~sentiment.services.analyze_item_emotions.AnalyzeItemEmotionsService`
—el que ejecuta la Lambda ``Analyze_Llm`` por Ítem— bajo la semántica de entrega
**al menos una vez** y los reintentos declarativos de la Maquina_De_Estados: la
misma ejecución se repite entre 2 y 7 veces sobre el mismo Ítem (criterio 8.9) y
el Sistema debe comportarse como si se hubiera ejecutado una sola vez.

Modelo de los dobles (fakes, no mocks; diseño 21.2)
--------------------------------------------------
La corrección de P2 descansa en el contrato idempotente de
:meth:`AnalysisRepository.complete_item` (diseño: ``TransactWriteItems``
condicionado a ``status == PENDING`` con ``ADD done_count`` en la misma
transacción). El fake :class:`StatefulRepositoryFake` reproduce ese contrato en
memoria, sin DynamoDB:

* mantiene el ``status`` del Ítem y el ``done_count`` del META como estado real;
* ``complete_item`` aplica la transición **solo** si el Ítem está en ``PENDING``:
  en ese caso lo pasa a ``DONE``, incrementa ``done_count`` en exactamente 1 y
  devuelve ``True`` (criterios 8.3, 8.4); en cualquier otra llamada la condición
  falla y devuelve ``False`` sin tocar el contador (criterio 8.5);
* ``get_item`` devuelve el estado actual con consistencia fuerte, de modo que un
  reintento observe el ``DONE`` que escribió el intento anterior y el guard de
  idempotencia del servicio (criterio 8.2) corte antes de invocar a Bedrock.

El fake :class:`CountingAnalyzerFake` cuenta las invocaciones a ``analyze`` para
aseverar que el Modelo_Bedrock se invoca **a lo sumo una vez** por la transición
``PENDING`` → terminal (y **cero** veces si el Ítem ya era terminal al empezar).

Propiedades aseveradas
----------------------
Para cualquier número de repeticiones ``N`` en ``[2, 7]`` (criterio 8.9) y
cualquier orden de llegada:

1. El ``done_count`` se incrementa **exactamente una vez** en total, con
   independencia de ``N`` (criterios 8.3, 8.9).
2. El Ítem queda en **un único** Estado_Terminal (``DONE``) y no vuelve a cambiar
   tras el primer cierre (criterio 8.9).
3. El Modelo_Bedrock se invoca **a lo sumo una vez** por la transición
   ``PENDING`` → terminal; los reintentos sobre un Ítem ya terminal son no-ops
   que **no** lo invocan ni mueven contadores (criterios 8.2, 8.5).
4. Toda ejecución salvo (como mucho) la primera devuelve ``ALREADY_TERMINAL``, y
   el estado final (``status`` del Ítem + ``done_count``) es idéntico al que deja
   una sola ejecución (criterios 8.5, 8.9).
"""

from __future__ import annotations

from typing import cast

from hypothesis import given
from hypothesis import strategies as st

from sentiment.domain.ids import make_item_id
from sentiment.domain.models import (
    TERMINAL_ITEM_STATUSES,
    AnalysisItem,
    EmotionAnalysis,
    ItemStatus,
)
from sentiment.domain.ports import AnalysisRepository, LlmAnalyzer
from sentiment.services.analyze_item_emotions import (
    AnalyzeItemEmotionsService,
    ItemOutcome,
)

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_MODEL_ID = "test.model.v1"


# ---------------------------------------------------------------------------
# Dobles en memoria con estado (fakes).
# ---------------------------------------------------------------------------
class StatefulRepositoryFake:
    """Doble en memoria del puerto ``AnalysisRepository`` con estado idempotente.

    Reproduce el contrato normativo de :meth:`AnalysisRepository.complete_item`
    (``TransactWriteItems`` condicionado a ``status == PENDING`` con ``ADD`` al
    ``done_count`` del META en la misma transacción) sin depender de DynamoDB:

    * El ``status`` del Ítem y el ``done_count`` son el estado real que evoluciona
      entre llamadas; ``get_item`` lo expone con consistencia fuerte (un reintento
      ve el ``DONE`` que escribió el intento previo).
    * ``complete_item`` solo aplica la transición desde ``PENDING``: pasa el Ítem a
      ``DONE``, incrementa ``done_count`` en 1 y devuelve ``True`` exactamente una
      vez; cualquier llamada posterior encuentra el Ítem ya terminal, no toca el
      contador y devuelve ``False`` (idempotencia, criterios 8.3, 8.4, 8.5).
    """

    def __init__(self, *, initial_status: ItemStatus, item_id: str, index: int) -> None:
        self._item_id = item_id
        self._index = index
        self._status = initial_status
        self.done_count = 0
        self.get_item_calls = 0
        self.complete_item_calls = 0
        #: Número de veces que ``complete_item`` devolvió ``True`` (transiciones
        #: PENDING -> DONE realmente aplicadas). Debe ser 0 o 1.
        self.transitions_applied = 0

    @property
    def status(self) -> ItemStatus:
        """Estado actual del Ítem (lo que vería una lectura consistente)."""
        return self._status

    def get_item(self, analysis_id: str, item_id: str) -> AnalysisItem:
        self.get_item_calls += 1
        return AnalysisItem(
            analysis_id=analysis_id,
            item_id=item_id,
            index=self._index,
            indificador="Liderazgo",
            respuesta="El equipo me apoya en los momentos dificiles.",
            status=self._status,
        )

    def complete_item(
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:
        self.complete_item_calls += 1
        # La transición y el incremento son una sola operación condicionada a
        # PENDING (criterio 8.4). Si ya es terminal, la condición falla: no se
        # modifica el Ítem ni el contador y se devuelve False (criterio 8.5).
        if self._status is not ItemStatus.PENDING:
            return False
        self._status = ItemStatus.DONE
        self.done_count += 1
        self.transitions_applied += 1
        return True


class CountingAnalyzerFake:
    """Doble en memoria del puerto ``LlmAnalyzer`` que cuenta sus invocaciones.

    Devuelve un ``EmotionAnalysis`` fijo (con ``item_id`` vacío, como el adaptador
    real; diseño 16.3) y registra cuántas veces se le invocó, para aseverar que el
    Modelo_Bedrock se llama a lo sumo una vez por la transición PENDING -> terminal.
    """

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, indificador: str, respuesta: str) -> EmotionAnalysis:
        self.calls += 1
        return EmotionAnalysis(
            item_id="",
            sentimientos=("gratitud",),
            explicacion="El texto expresa agradecimiento.",
            model_id=_MODEL_ID,
            input_tokens=120,
            output_tokens=48,
        )


def _service(
    repo: StatefulRepositoryFake, analyzer: CountingAnalyzerFake
) -> AnalyzeItemEmotionsService:
    """Construye el servicio con los dobles estrechados a sus puertos."""
    return AnalyzeItemEmotionsService(
        cast("AnalysisRepository", repo), cast("LlmAnalyzer", analyzer)
    )


# ---------------------------------------------------------------------------
# Estrategias.
# ---------------------------------------------------------------------------
# Índice del Ítem dentro del rango válido del Lote (criterio 8.6: 0..999).
_indices = st.integers(min_value=0, max_value=999)

# Número de ejecuciones repetidas del mismo Ítem. El criterio 8.9 acota el caso a
# procesar un mismo item_id "entre 2 y 7 veces"; se cubre ese rango exacto.
_repeticiones = st.integers(min_value=2, max_value=7)


# ---------------------------------------------------------------------------
# (a) Ítem que empieza en PENDING: N ejecuciones == una sola ejecución.
# ---------------------------------------------------------------------------
@given(index=_indices, repeticiones=_repeticiones)
def test_repeated_execution_increments_counter_exactly_once(
    index: int, repeticiones: int
) -> None:
    """N ejecuciones de un Ítem PENDING cierran en DONE e incrementan el contador 1 vez.

    Es el enunciado central de P2: ``complete_item`` aplica la transición
    ``PENDING`` → ``DONE`` una única vez (incrementando ``done_count`` en 1) y toda
    repetición posterior es un no-op idempotente que devuelve ``ALREADY_TERMINAL``
    sin mover el contador (criterios 8.3, 8.4, 8.5, 8.9).
    """
    item_id = make_item_id(_ANALYSIS_ID, index)
    repo = StatefulRepositoryFake(
        initial_status=ItemStatus.PENDING, item_id=item_id, index=index
    )
    analyzer = CountingAnalyzerFake()
    service = _service(repo, analyzer)

    resultados = [service.execute(_ANALYSIS_ID, item_id) for _ in range(repeticiones)]

    # La primera ejecución (o la primera que gane la carrera de escritura) cierra
    # el Ítem; su desenlace es DONE. El resto son ALREADY_TERMINAL.
    assert resultados[0] is ItemOutcome.DONE
    assert all(r is ItemOutcome.ALREADY_TERMINAL for r in resultados[1:])

    # Contador incrementado EXACTAMENTE una vez, con independencia de N (8.3, 8.9).
    assert repo.done_count == 1
    assert repo.transitions_applied == 1

    # Estado final: un único Estado_Terminal (DONE), estable tras el primer cierre.
    assert repo.status is ItemStatus.DONE

    # El Modelo_Bedrock se invocó a lo sumo una vez por la transición: exactamente
    # una, en la ejecución que encontró el Ítem en PENDING (criterios 8.2, 8.5).
    assert analyzer.calls == 1


# ---------------------------------------------------------------------------
# (b) Ítem ya terminal al empezar: toda ejecución es no-op, sin Bedrock.
# ---------------------------------------------------------------------------
@given(
    index=_indices,
    repeticiones=_repeticiones,
    status=st.sampled_from(sorted(TERMINAL_ITEM_STATUSES)),
)
def test_already_terminal_item_never_invokes_model_or_counter(
    index: int, repeticiones: int, status: ItemStatus
) -> None:
    """Un Ítem ya terminal nunca invoca a Bedrock ni mueve contadores (criterios 8.2, 8.5).

    Reintentar un Ítem que ya alcanzó un Estado_Terminal (``DONE`` / ``FAILED`` /
    ``SKIPPED``) devuelve siempre ``ALREADY_TERMINAL`` sin invocar al Modelo_Bedrock
    y sin ejecutar ninguna escritura ni incremento, con independencia del número de
    reintentos. Es lo que hace que la entrega al-menos-una-vez sea segura y gratuita.
    """
    item_id = make_item_id(_ANALYSIS_ID, index)
    repo = StatefulRepositoryFake(
        initial_status=status, item_id=item_id, index=index
    )
    analyzer = CountingAnalyzerFake()
    service = _service(repo, analyzer)

    resultados = [service.execute(_ANALYSIS_ID, item_id) for _ in range(repeticiones)]

    assert all(r is ItemOutcome.ALREADY_TERMINAL for r in resultados)
    assert analyzer.calls == 0  # el guard corta antes de Bedrock (criterio 8.2)
    assert repo.complete_item_calls == 0  # ninguna escritura (criterio 8.5)
    assert repo.done_count == 0  # ningún contador movido
    assert repo.status is status  # el Estado_Terminal no cambia


# ---------------------------------------------------------------------------
# (c) Carrera de escritura: el primer complete_item gana, los demás son no-ops.
# ---------------------------------------------------------------------------
@given(index=_indices, repeticiones=_repeticiones)
def test_only_one_execution_wins_the_write_race(
    index: int, repeticiones: int
) -> None:
    """Entre N ejecuciones del mismo Ítem PENDING, solo una aplica la transición (8.3, 8.5).

    Modela que el estado terminal lo escribe exactamente un intento: ``complete_item``
    devuelve ``True`` una sola vez (la transición aplicada) y ``False`` el resto. El
    estado final es indistinguible de ejecutar una sola vez (criterio 8.9).
    """
    item_id = make_item_id(_ANALYSIS_ID, index)
    repo = StatefulRepositoryFake(
        initial_status=ItemStatus.PENDING, item_id=item_id, index=index
    )
    analyzer = CountingAnalyzerFake()
    service = _service(repo, analyzer)

    outcomes = [service.execute(_ANALYSIS_ID, item_id) for _ in range(repeticiones)]

    # Exactamente una transición aplicada => exactamente un DONE observado.
    assert repo.transitions_applied == 1
    assert sum(1 for o in outcomes if o is ItemOutcome.DONE) == 1
    assert repo.done_count == 1
