"""Property 5 — Totalidad de la respuesta de ingesta (tarea 7.2).

**Validates: Requirements 3.1, 3.14, 3.16, 3.18, 3.19, 15.5**

Enunciado (design.md, sección 19, Property 5):

    Toda solicitud posible produce o una aceptación ``202`` con ``analysis_id``,
    o un rechazo con un ``error_code`` del catálogo cerrado. Ninguna entrada
    malformada produce un ``5xx``: los ``5xx`` quedan reservados a fallos de
    infraestructura, que este test no inyecta. Nunca una excepción no manejada ni
    ``None``.

El ejemplo del diseño ejercita el ``api_submit_handler``, que es el adaptador de
transporte HTTP (tarea 9.1, todavía no implementada). Esta prueba verifica la
**misma** propiedad un nivel más abajo, sobre el servicio puro
:class:`~sentiment.services.submit_analysis.SubmitAnalysisService`, que es donde
vive toda la lógica de validación y precedencia (criterios 3.1, 3.16, 3.19). El
mapeo entre el desenlace del servicio y el código HTTP del handler es directo y
está fijado por el catálogo cerrado de errores
(:mod:`sentiment.domain.errors`):

* el servicio devuelve un :class:`~sentiment.domain.models.AnalysisMeta`
  ``PENDING`` con un ``analysis_id`` (que el handler traduce a ``202``), **o**
* lanza un :class:`~sentiment.domain.errors.SentimentError` cuyo ``error_code``
  pertenece al catálogo cerrado y cuyo ``http_status`` es un ``4xx`` de contenido
  (``400``/``413``/``429``), que el handler traduce a ese mismo código.

La totalidad se comprueba por *fuzzing* con tres familias de estrategias:

1. ``st.binary()`` — bytes arbitrarios, incluido JSON malformado y UTF-8 inválido.
2. ``st.text()`` codificado — documentos de texto que casi nunca son el JSON
   esperado.
3. Cuerpos JSON estructurados *válidos y casi válidos* compuestos por una
   estrategia que mezcla a propósito los casos límite del contrato de ingesta:
   Lotes gigantes (``TOO_MANY_ITEMS``), ``respuesta`` desmesurada
   (``RESPONSE_TOO_LONG`` / ``PAYLOAD_TOO_LARGE``), ``indificador`` fuera del enum
   (``UNKNOWN_INDIFICADOR``), un ``tenant_id`` en el cuerpo y campos no declarados
   (``VALIDATION_FAILED``, criterio 1.11), y Lotes perfectamente válidos (``202``).

Las dependencias de infraestructura se sustituyen por dobles en memoria **sanos**
(nunca fallan): así, cualquier aparición de ``INTERNAL_ERROR`` (``500``) sería un
fallo real de la propiedad, no un efecto del entorno de prueba. ``clock`` e
``id_factory`` son deterministas (criterio 17.2): no hay *monkeypatching* ni red.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Final

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sentiment.domain import errors as error_module
from sentiment.domain.errors import SentimentError
from sentiment.domain.ids import is_valid_ulid
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ClassicSentiment,
    EmotionAnalysis,
    SurveyAnswer,
)
from sentiment.schemas.loader import submit_request_indificador_enum
from sentiment.services.submit_analysis import (
    MAX_ITEMS,
    MAX_PAYLOAD_BYTES,
    MAX_RESPONSE_BYTES,
    SubmitAnalysisService,
    SubmitCommand,
)

_TENANT: Final = "acme-corp"
_SUB: Final = "user-123"
_CORRELATION: Final = "11111111-1111-4111-8111-111111111111"
_FIXED_NOW: Final = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
_FIXED_ULID: Final = "01JDQZ8F3K7XWQ2M5N8P4R6T9V"

# El enum real de ``indificador`` (fuente única del contrato). Se usa para
# construir Ítems válidos sin duplicar la lista literal.
_VALID_INDIFICADORES: Final[tuple[str, ...]] = tuple(submit_request_indificador_enum())


# --------------------------------------------------------------------------
# Catálogo cerrado de ``error_code`` derivado del módulo de errores.
# --------------------------------------------------------------------------


def _closed_error_catalog() -> frozenset[str]:
    """Recolecta todos los ``error_code`` de la jerarquía :class:`SentimentError`.

    Recorre las subclases de :class:`~sentiment.domain.errors.SentimentError`
    declaradas en :mod:`sentiment.domain.errors` y además las constantes de
    ``error_code`` que no son excepciones (``ORPHANED_AFTER_MAP``,
    ``EXECUTION_ABANDONED``). El resultado es el **catálogo cerrado** contra el
    que se contrasta todo rechazo: si ``execute`` lanzara un error cuyo código no
    estuviera aquí, la propiedad fallaría (criterio 3.18: nunca un código fuera
    del catálogo).

    :return: el conjunto de todos los ``error_code`` válidos del Sistema.
    """
    codes: set[str] = {SentimentError.error_code}

    def _walk(cls: type[SentimentError]) -> None:
        for subclass in cls.__subclasses__():
            codes.add(subclass.error_code)
            _walk(subclass)

    _walk(SentimentError)

    # Constantes de ``error_code`` que son desenlaces del pipeline, no excepciones.
    for name in ("ORPHANED_AFTER_MAP", "EXECUTION_ABANDONED"):
        value = getattr(error_module, name, None)
        if isinstance(value, str):
            codes.add(value)

    return frozenset(codes)


KNOWN_ERROR_CODES: Final[frozenset[str]] = _closed_error_catalog()

# ``http_status`` admisibles para un rechazo por **contenido** en ingesta
# (criterios 3.14, 3.19): sólo ``4xx``. El ``500`` queda reservado a fallos de
# infraestructura (criterios 4.10, 4.11), que esta prueba no inyecta.
_CONTENT_ERROR_STATUSES: Final[frozenset[int]] = frozenset({400, 413, 429})


# --------------------------------------------------------------------------
# Dobles en memoria SANOS de los puertos del dominio. Nunca fallan: así una
# respuesta ``500`` sólo podría venir de un bug del servicio, no del entorno.
# --------------------------------------------------------------------------


class _HealthyStorage:
    """Doble en memoria del puerto ``ObjectStorage`` que siempre tiene éxito."""

    def __init__(self) -> None:
        self.puts: dict[str, object] = {}

    def put_json(self, key: str, payload: object) -> None:
        self.puts[key] = payload

    def get_json(self, key: str) -> object:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def object_exists(self, key: str) -> bool:  # pragma: no cover
        return key in self.puts

    def presign_get(self, key: str, expires_in: int) -> str:  # pragma: no cover
        raise NotImplementedError


class _HealthyRepository:
    """Doble en memoria del puerto ``AnalysisRepository`` que siempre tiene éxito.

    No hay Lotes preexistentes, de modo que la cuota de 5 Lotes en vuelo
    (criterio 4.12) nunca se dispara: así los únicos rechazos posibles provienen
    de la validación de contenido, que es lo que esta propiedad fuzzea.
    """

    def __init__(self) -> None:
        self.metas: dict[str, AnalysisMeta] = {}

    def create_analysis(self, meta: AnalysisMeta, answers: list[SurveyAnswer]) -> None:
        self.metas[meta.analysis_id] = meta

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:  # pragma: no cover
        return self.metas[analysis_id]

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:
        return [], None

    def mark_analysis_failed(  # pragma: no cover - no se ejercita sin fallo de siembra
        self, analysis_id: str, error_code: str
    ) -> None:
        meta = self.metas.get(analysis_id)
        if meta is not None and not meta.is_terminal:
            meta.status = AnalysisStatus.FAILED

    # Métodos del puerto no ejercitados por este servicio.
    def save_classic_results(self, results: list[ClassicSentiment]) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_item(self, analysis_id: str, item_id: str) -> AnalysisItem:  # pragma: no cover
        raise NotImplementedError

    def complete_item(  # pragma: no cover
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:
        raise NotImplementedError

    def fail_item(  # pragma: no cover
        self, analysis_id: str, item_id: str, error_code: str
    ) -> bool:
        raise NotImplementedError

    def skip_item(self, analysis_id: str, item_id: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:  # pragma: no cover
        raise NotImplementedError

    def finalize_analysis(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
        raise NotImplementedError

    def mark_event_published(self, analysis_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_sweep_candidates(  # pragma: no cover
        self, older_than: datetime
    ) -> Iterator[AnalysisMeta]:
        raise NotImplementedError


class _HealthyQueue:
    """Doble en memoria del puerto ``QueuePublisher`` que siempre tiene éxito."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def publish_analysis_queued(self, meta: AnalysisMeta, correlation_id: str) -> None:
        self.messages.append((meta.analysis_id, correlation_id))


class _HealthyEvents:
    """Doble en memoria del puerto ``EventPublisher`` que siempre tiene éxito."""

    def __init__(self) -> None:
        self.published: list[AnalysisMeta] = []

    def publish_analysis_completed(self, meta: AnalysisMeta) -> None:  # pragma: no cover
        self.published.append(meta)


def _build_service() -> SubmitAnalysisService:
    """Construye el servicio con dobles sanos y dependencias deterministas."""
    return SubmitAnalysisService(
        repository=_HealthyRepository(),
        storage=_HealthyStorage(),
        queue=_HealthyQueue(),
        events=_HealthyEvents(),
        clock=lambda: _FIXED_NOW,
        id_factory=lambda: _FIXED_ULID,
    )


# --------------------------------------------------------------------------
# Estrategias de generación de cuerpos crudos (``bytes``).
# --------------------------------------------------------------------------

# Un Ítem válido del contrato.
_valid_item: st.SearchStrategy[dict[str, object]] = st.fixed_dictionaries(
    {
        "indificador": st.sampled_from(_VALID_INDIFICADORES),
        "respuesta": st.text(max_size=50),
    }
)

# Un Ítem casi válido: puede tener un ``indificador`` fuera del enum, una
# ``respuesta`` que no es cadena o desmesurada, o campos extra no declarados.
# El tipo de valor se anota explícitamente (``object``) porque los dos mapas
# —``mapping`` y ``optional``— tienen estrategias de tipos distintos y mypy no
# puede inferir un argumento de tipo común a ``fixed_dictionaries``.
_near_valid_item: st.SearchStrategy[dict[str, object]] = st.fixed_dictionaries(
    mapping={
        "indificador": st.one_of(
            st.sampled_from(_VALID_INDIFICADORES),
            st.text(max_size=20),  # casi siempre fuera del enum -> UNKNOWN_INDIFICADOR
        ),
        "respuesta": st.one_of(
            st.text(max_size=50),
            st.text(min_size=MAX_RESPONSE_BYTES + 1, max_size=MAX_RESPONSE_BYTES + 20),
            st.integers(),  # tipo inválido -> VALIDATION_FAILED
            st.none(),
        ),
    },
    optional={"campo_extra": st.integers()},  # additionalProperties: false
)


def _structured_bodies() -> st.SearchStrategy[bytes]:
    """Cuerpos JSON estructurados, válidos y casi válidos, con casos límite.

    Mezcla a propósito las categorías que ejercitan la precedencia del
    ``error_code`` (criterio 3.16) y el camino feliz (``202``): Lotes válidos,
    Lotes vacíos, Lotes con Ítems casi válidos, un ``tenant_id`` en el cuerpo
    (criterio 1.11), campos de nivel superior no declarados y, ocasionalmente,
    Lotes de más de :data:`MAX_ITEMS` Ítems (``TOO_MANY_ITEMS``).
    """
    small_items = st.lists(st.one_of(_valid_item, _near_valid_item), max_size=6)
    # Un puñado de Lotes que exceden el tope de Ítems; se mantienen pequeños en
    # contenido para no inflar el ejemplo, pero cruzan el umbral de 1.000.
    oversized_items = st.lists(
        _valid_item, min_size=MAX_ITEMS + 1, max_size=MAX_ITEMS + 3
    )
    empty_items: st.SearchStrategy[list[dict[str, object]]] = st.just([])
    items = st.one_of(small_items, empty_items, oversized_items)

    extra_top_level = st.dictionaries(
        keys=st.sampled_from(["tenant_id", "campo_desconocido", "metadata"]),
        values=st.one_of(st.text(max_size=10), st.integers(), st.none()),
        max_size=2,
    )

    def _assemble(items_value: object, extra: dict[str, object]) -> bytes:
        doc: dict[str, object] = {"items": items_value}
        doc.update(extra)
        return json.dumps(doc, ensure_ascii=False, default=str).encode("utf-8")

    return st.builds(_assemble, items, extra_top_level)


# Cuerpos crudos: bytes arbitrarios, texto arbitrario y JSON estructurado.
_raw_bodies = st.one_of(
    st.binary(min_size=0, max_size=4096),
    st.text(max_size=4096).map(lambda s: s.encode("utf-8", errors="surrogatepass")),
    _structured_bodies(),
)


def _command(body: bytes) -> SubmitCommand:
    return SubmitCommand(
        tenant_id=_TENANT,
        owner_sub=_SUB,
        raw_body=body,
        correlation_id=_CORRELATION,
    )


# --------------------------------------------------------------------------
# La propiedad: totalidad de la respuesta de ingesta (P5).
# --------------------------------------------------------------------------


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(body=_raw_bodies)
def test_submit_is_total(body: bytes) -> None:
    """Fuzzing del cuerpo crudo: ``execute`` siempre decide, sin ``5xx`` ni ``None``.

    Con infraestructura sana, las únicas salidas posibles son:

    * una aceptación: un :class:`AnalysisMeta` ``PENDING`` con un ``analysis_id``
      ULID válido (el ``202`` del handler); o
    * un rechazo: un :class:`SentimentError` cuyo ``error_code`` está en el
      catálogo cerrado y cuyo ``http_status`` es ``400``/``413``/``429`` (nunca un
      ``5xx``, criterio 3.19).

    Nunca ``None`` y nunca una excepción fuera de la jerarquía del dominio
    (criterio 3.18).
    """
    service = _build_service()

    try:
        meta = service.execute(_command(body))
    except SentimentError as rejected:
        # (a) El rechazo usa un ``error_code`` del catálogo cerrado (criterio 3.18).
        assert rejected.error_code in KNOWN_ERROR_CODES, (
            f"error_code fuera del catálogo: {rejected.error_code!r}"
        )
        # (b) Es un rechazo por CONTENIDO: ``4xx``, nunca ``5xx`` (criterio 3.19).
        #     Con dobles sanos, ``INTERNAL_ERROR`` (500) no debe ocurrir jamás.
        assert rejected.http_status in _CONTENT_ERROR_STATUSES, (
            f"una entrada malformada produjo un http_status no 4xx: "
            f"{rejected.http_status} ({rejected.error_code})"
        )
        # (c) El cuerpo expuesto al cliente lleva sólo ``error_code`` +
        #     ``correlation_id``, sin PII ni estado interno (criterio 15.5).
        client_dict = rejected.to_client_dict(_CORRELATION)
        assert client_dict == {
            "error_code": rejected.error_code,
            "correlation_id": _CORRELATION,
        }
        _assert_no_internal_leak(client_dict)
    else:
        # Aceptación: un META ``PENDING`` con ``analysis_id`` ULID válido (3.1, 3.14).
        assert meta is not None, "execute nunca debe devolver None"
        assert isinstance(meta, AnalysisMeta)
        assert meta.status is AnalysisStatus.PENDING
        assert is_valid_ulid(meta.analysis_id)


def _assert_no_internal_leak(client_dict: dict[str, str]) -> None:
    """Verifica que el payload al cliente no filtra detalle interno (criterio 15.5).

    El contrato de ingesta expone exclusivamente ``error_code`` y
    ``correlation_id``; nada de trazas, nombres de servicios de infraestructura ni
    el mensaje de la excepción. Se serializa y se inspecciona el texto para que la
    comprobación sea equivalente a la del ejemplo del diseño sobre el cuerpo HTTP.
    """
    serialized = json.dumps(client_dict).lower()
    for forbidden in ("traceback", "boto", "dynamodb", "s3", "sqs"):
        assert forbidden not in serialized, f"posible fuga de detalle interno: {forbidden!r}"


# --------------------------------------------------------------------------
# Propiedades de apoyo que fijan los invariantes usados por la prueba principal.
# --------------------------------------------------------------------------


def test_known_catalog_contains_the_expected_content_error_codes() -> None:
    """El catálogo derivado contiene los códigos de rechazo por contenido esperados.

    Fija el conjunto de códigos ``4xx`` que ``test_submit_is_total`` puede
    observar como rechazo, de modo que un cambio accidental en la jerarquía de
    errores (p. ej. renombrar un código) rompa esta prueba en vez de debilitar
    silenciosamente la propiedad principal.
    """
    expected_content_codes = {
        "VALIDATION_FAILED",
        "PAYLOAD_TOO_LARGE",
        "TOO_MANY_ITEMS",
        "UNKNOWN_INDIFICADOR",
        "RESPONSE_TOO_LONG",
        "QUOTA_EXCEEDED",
    }
    assert expected_content_codes <= KNOWN_ERROR_CODES


@settings(max_examples=50)
@given(
    items=st.lists(
        st.fixed_dictionaries(
            {
                "indificador": st.sampled_from(_VALID_INDIFICADORES),
                "respuesta": st.text(max_size=100),
            }
        ),
        min_size=1,
        max_size=5,
    )
)
def test_well_formed_batches_are_always_accepted(items: list[dict[str, str]]) -> None:
    """Todo Lote bien formado produce una aceptación (el ``202`` del handler).

    Complementa la totalidad verificando el otro extremo: cuando la entrada es
    estructuralmente válida y respeta los límites, ``execute`` nunca rechaza; el
    resultado es un META ``PENDING`` con ``item_count`` igual al número de Ítems.
    """
    service = _build_service()
    body = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")

    meta = service.execute(_command(body))

    assert meta.status is AnalysisStatus.PENDING
    assert meta.item_count == len(items)
    assert is_valid_ulid(meta.analysis_id)


def test_oversized_raw_body_is_rejected_as_payload_too_large() -> None:
    """Un cuerpo por encima del tope de bytes se rechaza con ``PAYLOAD_TOO_LARGE``.

    Caso límite determinista (no fuzzeado por eficiencia): confirma la máxima
    precedencia del tamaño del cuerpo, que se decide antes de parsear (criterio
    3.4) y mapea a ``413``, uno de los ``http_status`` de contenido admitidos por
    la propiedad.
    """
    service = _build_service()
    body = b"x" * (MAX_PAYLOAD_BYTES + 1)

    with_rejection = None
    try:
        service.execute(_command(body))
    except SentimentError as exc:
        with_rejection = exc

    assert with_rejection is not None
    assert with_rejection.error_code == "PAYLOAD_TOO_LARGE"
    assert with_rejection.http_status == 413
