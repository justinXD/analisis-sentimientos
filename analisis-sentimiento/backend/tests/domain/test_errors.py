"""Pruebas unitarias del Catalogo_De_Errores cerrado (tarea 3.2).

Verifican que la jerarquía de :mod:`sentiment.domain.errors` cumple el glosario
«Catalogo_De_Errores» y la clasificación transitorio/permanente de los criterios
9.16 y 9.17:

* cada subclase expone el ``error_code``, ``http_status`` e ``is_transient`` que
  los requisitos le asignan;
* la familia transitoria (9.16) es reintentable y la permanente (9.17) no;
* los desenlaces del pipeline que no son condiciones de error de una dependencia
  no entran en esa clasificación;
* ``ORPHANED_AFTER_MAP`` y ``EXECUTION_ABANDONED`` son constantes de ``error_code``
  (no excepciones) y ``ALREADY_TERMINAL`` es un valor de resultado, no un error;
* el payload hacia el cliente no filtra detalle interno (criterio 15.5).
"""

from __future__ import annotations

import pytest

from sentiment.domain import errors as e

# --- (error_code, http_status) esperados para la familia de API (HTTP) ---
_HTTP_ERRORS: list[tuple[type[e.SentimentError], str, int]] = [
    (e.ValidationError, "VALIDATION_FAILED", 400),
    (e.PayloadTooLargeError, "PAYLOAD_TOO_LARGE", 413),
    (e.TooManyItemsError, "TOO_MANY_ITEMS", 400),
    (e.UnknownIndificadorError, "UNKNOWN_INDIFICADOR", 400),
    (e.ResponseTooLongError, "RESPONSE_TOO_LONG", 400),
    (e.UnauthenticatedError, "UNAUTHENTICATED", 401),
    (e.ForbiddenError, "FORBIDDEN", 403),
    (e.NotFoundError, "NOT_FOUND", 404),
    (e.NotReadyError, "ANALYSIS_NOT_READY", 409),
    (e.QuotaExceededError, "QUOTA_EXCEEDED", 429),
]

# --- error_code de la familia PERMANENTE de procesamiento (criterio 9.17) ---
_PERMANENT_ERRORS: list[tuple[type[e.PermanentProcessingError], str]] = [
    (e.ComprehendItemFailedError, "COMPREHEND_ITEM_FAILED"),
    (e.LlmInputRejectedError, "LLM_INPUT_REJECTED"),
    (e.LlmSchemaError, "LLM_OUTPUT_INVALID"),
    (e.TaskInputInvalidError, "TASK_INPUT_INVALID"),
]

# --- Desenlaces de procesamiento que NO entran en la clasificación 9.16/9.17 ---
_UNCLASSIFIED_PROCESSING_ERRORS: list[tuple[type[e.SentimentError], str]] = [
    (e.ComprehendResultMissingError, "COMPREHEND_RESULT_MISSING"),
    (e.InputReadFailedError, "INPUT_READ_FAILED"),
    (e.AggregationError, "AGGREGATION_FAILED"),
]


class TestBaseContract:
    """Contrato de la raíz :class:`SentimentError`."""

    def test_default_is_internal_error_500_non_transient(self) -> None:
        assert e.SentimentError.error_code == "INTERNAL_ERROR"
        assert e.SentimentError.http_status == 500
        assert e.SentimentError.is_transient is False

    def test_every_error_is_a_sentiment_error_and_exception(self) -> None:
        for cls, _code, _status in _HTTP_ERRORS:
            assert issubclass(cls, e.SentimentError)
            assert issubclass(cls, Exception)

    def test_message_defaults_to_error_code(self) -> None:
        assert str(e.NotFoundError()) == "NOT_FOUND"
        assert str(e.NotFoundError("contexto interno")) == "contexto interno"


class TestHttpFamily:
    """Familia de API: cada error cruza la frontera HTTP con su estado."""

    @pytest.mark.parametrize(("cls", "code", "status"), _HTTP_ERRORS)
    def test_error_code_and_http_status(
        self, cls: type[e.SentimentError], code: str, status: int
    ) -> None:
        inst = cls()
        assert inst.error_code == code
        assert inst.http_status == status

    @pytest.mark.parametrize(("cls", "_code", "_status"), _HTTP_ERRORS)
    def test_http_family_is_not_transient(
        self, cls: type[e.SentimentError], _code: str, _status: int
    ) -> None:
        assert cls().is_transient is False

    @pytest.mark.parametrize(
        "cls",
        [
            e.PayloadTooLargeError,
            e.TooManyItemsError,
            e.UnknownIndificadorError,
            e.ResponseTooLongError,
        ],
    )
    def test_validation_subclasses_derive_from_validation_error(
        self, cls: type[e.ValidationError]
    ) -> None:
        assert issubclass(cls, e.ValidationError)


class TestTransientFamily:
    """Clase transitoria (criterio 9.16): reintentable, sin estado HTTP."""

    @pytest.mark.parametrize("cls", [e.TransientDependencyError, e.ThrottledError])
    def test_is_transient_and_no_http_status(
        self, cls: type[e.TransientDependencyError]
    ) -> None:
        inst = cls()
        assert inst.is_transient is True
        assert inst.http_status is None

    def test_throttled_is_a_transient_dependency_error(self) -> None:
        assert issubclass(e.ThrottledError, e.TransientDependencyError)


class TestPermanentFamily:
    """Clase permanente (criterio 9.17): no reintentable, con su error_code."""

    @pytest.mark.parametrize(("cls", "code"), _PERMANENT_ERRORS)
    def test_error_code_not_transient_no_http(
        self, cls: type[e.PermanentProcessingError], code: str
    ) -> None:
        inst = cls()
        assert inst.error_code == code
        assert inst.is_transient is False
        assert inst.http_status is None
        assert issubclass(cls, e.PermanentProcessingError)


class TestUnclassifiedProcessingOutcomes:
    """Desenlaces de procesamiento fuera de la clasificación 9.16/9.17."""

    @pytest.mark.parametrize(("cls", "code"), _UNCLASSIFIED_PROCESSING_ERRORS)
    def test_internal_code_no_http_not_transient(
        self, cls: type[e.SentimentError], code: str
    ) -> None:
        inst = cls()
        assert inst.error_code == code
        assert inst.http_status is None
        assert inst.is_transient is False

    @pytest.mark.parametrize(("cls", "_code"), _UNCLASSIFIED_PROCESSING_ERRORS)
    def test_not_in_transient_or_permanent_families(
        self, cls: type[e.SentimentError], _code: str
    ) -> None:
        assert not issubclass(cls, e.TransientDependencyError)
        assert not issubclass(cls, e.PermanentProcessingError)


class TestConstantsAndResultValue:
    """``ORPHANED_AFTER_MAP`` y ``EXECUTION_ABANDONED`` son constantes.

    ``ALREADY_TERMINAL`` es un valor de resultado, no un ``error_code``.
    """

    def test_orphaned_after_map_is_a_string_constant(self) -> None:
        assert e.ORPHANED_AFTER_MAP == "ORPHANED_AFTER_MAP"
        assert isinstance(e.ORPHANED_AFTER_MAP, str)

    def test_execution_abandoned_is_a_string_constant(self) -> None:
        assert e.EXECUTION_ABANDONED == "EXECUTION_ABANDONED"
        assert isinstance(e.EXECUTION_ABANDONED, str)

    def test_already_terminal_is_a_result_value_not_an_error(self) -> None:
        # No es una excepción (no es una clase derivada de SentimentError) ni se
        # expone como un error_code: es un valor de resultado (criterio 8.2).
        assert e.ALREADY_TERMINAL == "ALREADY_TERMINAL"
        assert isinstance(e.ALREADY_TERMINAL, str)
        assert not isinstance(e.ALREADY_TERMINAL, type)


class TestClientPayloadDoesNotLeak:
    """El payload hacia el cliente contiene sólo código + correlation_id (15.5)."""

    def test_to_client_dict_excludes_internal_detail_and_message(self) -> None:
        inst = e.LlmInputRejectedError(
            "detalle interno con rutas y ARNs", item_id="A#00001", internal="x"
        )
        payload = inst.to_client_dict("corr-123")
        assert payload == {
            "error_code": "LLM_INPUT_REJECTED",
            "correlation_id": "corr-123",
        }
        # El detalle interno se conserva en la instancia, no en el payload.
        assert inst.detail == {"item_id": "A#00001", "internal": "x"}
