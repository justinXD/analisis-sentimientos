"""Pruebas unitarias de ``analyze_item_emotions`` (caso de uso de ``analyze_llm``).

Verifican el comportamiento observable del servicio
:class:`~sentiment.services.analyze_item_emotions.AnalyzeItemEmotionsService`
descrito en la sección 16.3 del diseño, usando **dobles en memoria** de los dos
puertos que consume (fakes, no mocks; sección 21.2 del diseño). Cada fake lleva
la cuenta de las invocaciones que recibe para poder aseverar sobre ellas:

* relectura del ITEM antes de cualquier invocación al modelo (criterio 7.2);
* guard de idempotencia: un Ítem terminal devuelve ``ALREADY_TERMINAL`` sin
  invocar a Bedrock ni escribir (criterios 8.2, 8.3);
* una sola invocación en el camino de éxito (criterios 7.1, 7.3);
* propagación sin captura de las excepciones del análisis (transitorias y
  permanentes) y de la configuración, manteniendo el Ítem sin escrituras
  (criterios 7.8, 7.10, 7.14, 7.15);
* normalización de ``sentimientos`` y coherencia de ``explicacion`` (7.6, 7.7);
* idempotencia de la carrera de escritura: ``complete_item`` devuelve ``False``
  y el servicio responde ``ALREADY_TERMINAL`` (criterios 8.3, 8.5).
"""

from __future__ import annotations

from typing import cast

import pytest

from sentiment.domain.errors import (
    LlmInputRejectedError,
    LlmSchemaError,
    NotFoundError,
    ThrottledError,
    TransientDependencyError,
)
from sentiment.domain.ids import make_item_id
from sentiment.domain.models import (
    AnalysisItem,
    EmotionAnalysis,
    ItemStatus,
)
from sentiment.domain.ports import AnalysisRepository, LlmAnalyzer
from sentiment.observability.config import ConfigError
from sentiment.services.analyze_item_emotions import (
    AnalyzeItemEmotionsService,
    ItemOutcome,
)

_ANALYSIS_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_INDEX = 42
_ITEM_ID = make_item_id(_ANALYSIS_ID, _INDEX)
_MODEL_ID = "test.model.v1"


def _pending_item(
    *,
    indificador: str = "Liderazgo",
    respuesta: str = "El equipo me apoya en los momentos dificiles.",
    status: ItemStatus = ItemStatus.PENDING,
) -> AnalysisItem:
    """Construye un ``AnalysisItem`` mínimo para alimentar el fake del repositorio."""
    return AnalysisItem(
        analysis_id=_ANALYSIS_ID,
        item_id=_ITEM_ID,
        index=_INDEX,
        indificador=indificador,
        respuesta=respuesta,
        status=status,
    )


class FakeRepository:
    """Doble en memoria del puerto ``AnalysisRepository`` usado por el servicio.

    Solo implementa los dos métodos que ``analyze_item_emotions`` consume
    (``get_item`` y ``complete_item``); registra sus llamadas para las aserciones.
    Un ``item`` ``None`` simula un ITEM inexistente (``get_item`` lanza
    ``NotFoundError``, criterio 7.14).
    """

    def __init__(
        self, item: AnalysisItem | None, *, complete_result: bool = True
    ) -> None:
        self._item = item
        self._complete_result = complete_result
        self.get_item_calls: list[tuple[str, str]] = []
        self.complete_item_calls: list[tuple[str, str, EmotionAnalysis]] = []

    def get_item(self, analysis_id: str, item_id: str) -> AnalysisItem:
        self.get_item_calls.append((analysis_id, item_id))
        if self._item is None:
            raise NotFoundError(analysis_id=analysis_id, item_id=item_id)
        return self._item

    def complete_item(
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:
        self.complete_item_calls.append((analysis_id, item_id, emotions))
        return self._complete_result


class FakeAnalyzer:
    """Doble en memoria del puerto ``LlmAnalyzer``.

    Devuelve un ``EmotionAnalysis`` fijo (con ``item_id`` vacío, como hace el
    adaptador real, diseño 16.3) o, si se le pasa ``error``, lo lanza para simular
    un fallo de Bedrock o de configuración. Registra cada invocación recibida para
    poder aseverar que hubo exactamente una, o ninguna.
    """

    def __init__(
        self,
        result: EmotionAnalysis | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def analyze(self, indificador: str, respuesta: str) -> EmotionAnalysis:
        self.calls.append((indificador, respuesta))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _emotions(
    *,
    sentimientos: tuple[str, ...] = ("gratitud",),
    explicacion: str = "El texto expresa agradecimiento.",
    item_id: str = "",
    input_tokens: int = 120,
    output_tokens: int = 48,
) -> EmotionAnalysis:
    """Resultado crudo del análisis tal como lo entrega el puerto ``LlmAnalyzer``."""
    return EmotionAnalysis(
        item_id=item_id,
        sentimientos=sentimientos,
        explicacion=explicacion,
        model_id=_MODEL_ID,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _service(repo: FakeRepository, analyzer: FakeAnalyzer) -> AnalyzeItemEmotionsService:
    """Construye el servicio con los dobles, estrechados a sus puertos.

    Los fakes implementan solo los métodos de los puertos que el caso de uso
    consume (``get_item``/``complete_item`` y ``analyze``). El ``cast`` es la
    costura de prueba honesta: declara que estos dobles ocupan el lugar de los
    puertos :class:`AnalysisRepository` y :class:`LlmAnalyzer`, sin obligar al
    fake a reproducir el resto del contrato que este servicio no toca.
    """
    return AnalyzeItemEmotionsService(
        cast("AnalysisRepository", repo), cast("LlmAnalyzer", analyzer)
    )


# ---------------------------------------------------------------------------
# Camino feliz: DONE con una sola invocación y una sola escritura.
# ---------------------------------------------------------------------------
def test_execute_done_single_invocation_and_write() -> None:
    """Un Ítem PENDING se analiza una vez y se cierra en DONE (criterios 7.1, 7.3, 7.8)."""
    repo = FakeRepository(_pending_item())
    analyzer = FakeAnalyzer(_emotions())
    service = _service(repo, analyzer)

    outcome = service.execute(_ANALYSIS_ID, _ITEM_ID)

    assert outcome is ItemOutcome.DONE
    assert len(analyzer.calls) == 1
    assert len(repo.complete_item_calls) == 1


def test_execute_reads_item_before_invoking_model() -> None:
    """La relectura del ITEM ocurre antes de invocar al modelo (criterio 7.2)."""
    repo = FakeRepository(_pending_item(indificador="Comunicacion", respuesta="texto"))
    analyzer = FakeAnalyzer(_emotions())
    service = _service(repo, analyzer)

    service.execute(_ANALYSIS_ID, _ITEM_ID)

    # El ITEM se leyó por su item_id y el modelo recibió el indificador y la
    # respuesta leídos de ese ITEM, no otros.
    assert repo.get_item_calls == [(_ANALYSIS_ID, _ITEM_ID)]
    assert analyzer.calls == [("Comunicacion", "texto")]


def test_execute_sets_item_id_on_persisted_result() -> None:
    """El resultado persistido lleva el item_id correcto, no el vacío del puerto (16.3)."""
    repo = FakeRepository(_pending_item())
    analyzer = FakeAnalyzer(_emotions(item_id=""))
    service = _service(repo, analyzer)

    service.execute(_ANALYSIS_ID, _ITEM_ID)

    _, _, persisted = repo.complete_item_calls[0]
    assert persisted.item_id == _ITEM_ID


# ---------------------------------------------------------------------------
# Guard de idempotencia: Ítem ya terminal => ALREADY_TERMINAL sin Bedrock.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    [ItemStatus.DONE, ItemStatus.FAILED, ItemStatus.SKIPPED],
)
def test_execute_already_terminal_skips_bedrock_and_write(status: ItemStatus) -> None:
    """Un Ítem terminal no invoca a Bedrock ni escribe nada (criterios 8.2, 8.3)."""
    repo = FakeRepository(_pending_item(status=status))
    analyzer = FakeAnalyzer(_emotions())
    service = _service(repo, analyzer)

    outcome = service.execute(_ANALYSIS_ID, _ITEM_ID)

    assert outcome is ItemOutcome.ALREADY_TERMINAL
    assert analyzer.calls == []
    assert repo.complete_item_calls == []


def test_already_terminal_value_matches_domain_result() -> None:
    """El valor de ALREADY_TERMINAL coincide con la constante de resultado del dominio."""
    from sentiment.domain.errors import ALREADY_TERMINAL

    assert str(ItemOutcome.ALREADY_TERMINAL) == ALREADY_TERMINAL


# ---------------------------------------------------------------------------
# ITEM inexistente / lectura fallida => propaga sin invocar al modelo (7.14).
# ---------------------------------------------------------------------------
def test_execute_missing_item_propagates_without_invoking_model() -> None:
    """Si el ITEM no existe, NotFoundError sube y Bedrock no se invoca (criterio 7.14)."""
    repo = FakeRepository(None)
    analyzer = FakeAnalyzer(_emotions())
    service = _service(repo, analyzer)

    with pytest.raises(NotFoundError):
        service.execute(_ANALYSIS_ID, _ITEM_ID)

    assert analyzer.calls == []
    assert repo.complete_item_calls == []


# ---------------------------------------------------------------------------
# Excepciones del análisis: suben SIN captura y SIN escritura (7.8, 7.10).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [
        ThrottledError("throttled"),
        TransientDependencyError("bedrock_5xx"),
        LlmInputRejectedError("input_rejected"),
        LlmSchemaError("output_invalid"),
    ],
)
def test_execute_analyzer_errors_propagate_uncaught(error: Exception) -> None:
    """Las excepciones del análisis suben sin captura y sin escribir (criterios 7.8, 7.10)."""
    repo = FakeRepository(_pending_item())
    analyzer = FakeAnalyzer(error=error)
    service = _service(repo, analyzer)

    with pytest.raises(type(error)):
        service.execute(_ANALYSIS_ID, _ITEM_ID)

    # Hubo exactamente un intento de invocación y ninguna escritura parcial.
    assert len(analyzer.calls) == 1
    assert repo.complete_item_calls == []


def test_execute_config_error_propagates_without_write() -> None:
    """Un MODEL_ID ausente (ConfigError) sube sin escribir en el ITEM (criterio 7.15)."""
    repo = FakeRepository(_pending_item())
    analyzer = FakeAnalyzer(error=ConfigError("MODEL_ID ausente"))
    service = _service(repo, analyzer)

    with pytest.raises(ConfigError):
        service.execute(_ANALYSIS_ID, _ITEM_ID)

    assert repo.complete_item_calls == []


# ---------------------------------------------------------------------------
# Normalización de sentimientos y coherencia de explicacion (7.6, 7.7).
# ---------------------------------------------------------------------------
def test_execute_normalizes_sentimientos() -> None:
    """Los sentimientos se normalizan (trim, minúsculas, dedup) antes de persistir (7.6)."""
    repo = FakeRepository(_pending_item())
    analyzer = FakeAnalyzer(
        _emotions(sentimientos=("  Gratitud ", "GRATITUD", "alivio"))
    )
    service = _service(repo, analyzer)

    service.execute(_ANALYSIS_ID, _ITEM_ID)

    _, _, persisted = repo.complete_item_calls[0]
    assert persisted.sentimientos == ("gratitud", "alivio")


def test_execute_empty_sentimientos_forces_empty_explicacion() -> None:
    """Sin sentimientos tras normalizar, la explicacion se vacía (criterio 7.7)."""
    repo = FakeRepository(_pending_item())
    analyzer = FakeAnalyzer(
        _emotions(sentimientos=(), explicacion="texto que debe descartarse")
    )
    service = _service(repo, analyzer)

    service.execute(_ANALYSIS_ID, _ITEM_ID)

    _, _, persisted = repo.complete_item_calls[0]
    assert persisted.sentimientos == ()
    assert persisted.explicacion == ""


def test_execute_preserves_token_counts_and_model_id() -> None:
    """Los metadatos de la invocación (model_id, tokens) se conservan tal cual."""
    repo = FakeRepository(_pending_item())
    analyzer = FakeAnalyzer(_emotions(input_tokens=321, output_tokens=99))
    service = _service(repo, analyzer)

    service.execute(_ANALYSIS_ID, _ITEM_ID)

    _, _, persisted = repo.complete_item_calls[0]
    assert persisted.model_id == _MODEL_ID
    assert persisted.input_tokens == 321
    assert persisted.output_tokens == 99


# ---------------------------------------------------------------------------
# Carrera de escritura: complete_item devuelve False => ALREADY_TERMINAL (8.5).
# ---------------------------------------------------------------------------
def test_execute_losing_write_race_returns_already_terminal() -> None:
    """Si complete_item devuelve False (otro intento ganó), el resultado es terminal (8.5)."""
    repo = FakeRepository(_pending_item(), complete_result=False)
    analyzer = FakeAnalyzer(_emotions())
    service = _service(repo, analyzer)

    outcome = service.execute(_ANALYSIS_ID, _ITEM_ID)

    assert outcome is ItemOutcome.ALREADY_TERMINAL
    # La invocación sí ocurrió (no se sabía el resultado de la carrera de antemano),
    # pero el desenlace observable es idempotente, no un error.
    assert len(analyzer.calls) == 1
