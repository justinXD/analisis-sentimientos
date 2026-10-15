"""Pruebas unitarias de ``SubmitAnalysisService`` (tarea 7.1).

Ejercitan el caso de uso de ingesta con dobles en memoria que implementan los
puertos del dominio (``AnalysisRepository``, ``ObjectStorage``,
``QueuePublisher``, ``EventPublisher``) y con ``clock`` e ``id_factory``
deterministas inyectados, sin ``boto3`` ni ``moto``. Cubren:

* validación total antes de cualquier escritura y rechazo "todo o nada"
  (criterios 3.1, 3.13);
* precedencia fija del ``error_code`` y reporte del ``index`` más bajo
  (criterio 3.16), incluido el ``tenant_id`` en el cuerpo como
  ``VALIDATION_FAILED`` (criterio 1.11) y el catch-all (criterio 3.19);
* normalización NFC + trim antes de persistir (criterio 3.17);
* revalidación de longitud en bytes UTF-8 de ``respuesta`` (criterio 3.10);
* cuota de 5 Lotes en vuelo por Tenant (criterio 4.12);
* orden de escritura S3 → DynamoDB → SQS y clave de entrada por fecha UTC
  (criterios 4.1, 4.2, 4.3, 4.8, 4.9);
* fallo de S3 → ``INTERNAL_ERROR`` sin tocar DynamoDB ni SQS (criterio 4.10);
* fallo de siembra → META en ``FAILED`` + Evento_Finalizacion antes del 500
  (criterio 4.11);
* el ``analysis_id`` es un ULID independiente del cliente (criterios 4.6, 4.7).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from sentiment.domain.errors import (
    NotFoundError,
    PayloadTooLargeError,
    QuotaExceededError,
    ResponseTooLongError,
    SentimentError,
    TooManyItemsError,
    UnknownIndificadorError,
    ValidationError,
)
from sentiment.domain.ids import is_valid_ulid
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ClassicSentiment,
    EmotionAnalysis,
    SurveyAnswer,
)
from sentiment.services.submit_analysis import (
    MAX_INFLIGHT_PER_TENANT,
    MAX_ITEMS,
    MAX_RESPONSE_BYTES,
    SubmitAnalysisService,
    SubmitCommand,
    _build_input_key,
    _canonicalize,
    _known_indificadores,
)

_TENANT = "acme-corp"
_SUB = "user-123"
_CORRELATION = "11111111-1111-4111-8111-111111111111"
_FIXED_NOW = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
_FIXED_ULID = "01JDQZ8F3K7XWQ2M5N8P4R6T9V"
_VALID_INDIFICADOR = "Reconocimiento"


# --------------------------------------------------------------------------
# Dobles de prueba en memoria que satisfacen los puertos del dominio.
# --------------------------------------------------------------------------


class _Call:
    """Marca de una escritura, con el orden global en que ocurrió."""

    def __init__(self, order: int, kind: str) -> None:
        self.order = order
        self.kind = kind


class _Recorder:
    """Reloj lógico compartido para ordenar las escrituras entre dobles."""

    def __init__(self) -> None:
        self._tick = 0
        self.calls: list[_Call] = []

    def record(self, kind: str) -> int:
        self._tick += 1
        call = _Call(self._tick, kind)
        self.calls.append(call)
        return self._tick

    def kinds(self) -> list[str]:
        return [c.kind for c in self.calls]


class FakeStorage:
    """Doble en memoria del puerto ``ObjectStorage``."""

    def __init__(self, recorder: _Recorder, *, fail: bool = False) -> None:
        self._recorder = recorder
        self._fail = fail
        self.puts: dict[str, object] = {}

    def put_json(self, key: str, payload: object) -> None:
        if self._fail:
            raise RuntimeError("S3 no disponible")
        self._recorder.record("s3")
        self.puts[key] = payload

    def get_json(self, key: str) -> object:  # pragma: no cover - no usado aquí
        raise NotImplementedError

    def object_exists(self, key: str) -> bool:  # pragma: no cover
        return key in self.puts

    def presign_get(self, key: str, expires_in: int) -> str:  # pragma: no cover
        raise NotImplementedError


class FakeRepository:
    """Doble en memoria del puerto ``AnalysisRepository``.

    Guarda el META sembrado y permite simular un fallo de siembra. Modela
    ``list_by_tenant`` como una única página con los META preexistentes para
    ejercitar el conteo de Lotes en vuelo.
    """

    def __init__(
        self,
        recorder: _Recorder,
        *,
        seed_fails: bool = False,
        meta_written_on_fail: bool = True,
        existing: list[AnalysisMeta] | None = None,
    ) -> None:
        self._recorder = recorder
        self._seed_fails = seed_fails
        self._meta_written_on_fail = meta_written_on_fail
        self._existing = existing or []
        self.metas: dict[str, AnalysisMeta] = {}
        self.failed_calls: list[tuple[str, str]] = []

    def create_analysis(self, meta: AnalysisMeta, answers: list[SurveyAnswer]) -> None:
        self._recorder.record("ddb")
        if self._seed_fails:
            if self._meta_written_on_fail:
                # El META va en la primera tanda: puede quedar escrito aunque la
                # siembra falle a medias.
                self.metas[meta.analysis_id] = meta
            raise RuntimeError("BatchWriteItem agotó los reintentos")
        self.metas[meta.analysis_id] = meta

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        meta = self.metas.get(analysis_id)
        if meta is None or meta.tenant_id != tenant_id:
            raise NotFoundError(analysis_id=analysis_id)
        return meta

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:
        page = [m for m in self._existing if m.tenant_id == tenant_id]
        return page, None

    def mark_analysis_failed(self, analysis_id: str, error_code: str) -> None:
        self._recorder.record("ddb_fail")
        self.failed_calls.append((analysis_id, error_code))
        meta = self.metas.get(analysis_id)
        if meta is not None and not meta.is_terminal:
            meta.status = AnalysisStatus.FAILED
            meta.completed_at = _FIXED_NOW

    # Métodos del puerto no ejercitados por este servicio.
    def save_classic_results(self, results: list[ClassicSentiment]) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_item(self, analysis_id: str, item_id: str) -> AnalysisItem:  # pragma: no cover
        raise NotImplementedError

    def complete_item(
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:  # pragma: no cover
        raise NotImplementedError

    def fail_item(
        self, analysis_id: str, item_id: str, error_code: str
    ) -> bool:  # pragma: no cover
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


class FakeQueue:
    """Doble en memoria del puerto ``QueuePublisher``."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self.messages: list[tuple[str, str]] = []

    def publish_analysis_queued(self, meta: AnalysisMeta, correlation_id: str) -> None:
        self._recorder.record("sqs")
        self.messages.append((meta.analysis_id, correlation_id))


class FakeEvents:
    """Doble en memoria del puerto ``EventPublisher``."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self.published: list[AnalysisMeta] = []

    def publish_analysis_completed(self, meta: AnalysisMeta) -> None:
        self._recorder.record("event")
        self.published.append(meta)


# --------------------------------------------------------------------------
# Fábricas de ayuda.
# --------------------------------------------------------------------------


def _body(items: list[dict[str, object]], **extra: object) -> bytes:
    doc: dict[str, object] = {"items": items}
    doc.update(extra)
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def _item(
    respuesta: str = "me siento bien", indificador: str = _VALID_INDIFICADOR
) -> dict[str, object]:
    return {"indificador": indificador, "respuesta": respuesta}


def _command(items: list[dict[str, object]] | None = None, **extra: object) -> SubmitCommand:
    body = _body(items if items is not None else [_item()], **extra)
    return SubmitCommand(
        tenant_id=_TENANT, owner_sub=_SUB, raw_body=body, correlation_id=_CORRELATION
    )


def _build_service(
    recorder: _Recorder,
    *,
    storage: FakeStorage | None = None,
    repository: FakeRepository | None = None,
) -> tuple[SubmitAnalysisService, FakeRepository, FakeStorage, FakeQueue, FakeEvents]:
    repo = repository or FakeRepository(recorder)
    store = storage or FakeStorage(recorder)
    queue = FakeQueue(recorder)
    events = FakeEvents(recorder)
    service = SubmitAnalysisService(
        repository=repo,
        storage=store,
        queue=queue,
        events=events,
        clock=lambda: _FIXED_NOW,
        id_factory=lambda: _FIXED_ULID,
    )
    return service, repo, store, queue, events


# --------------------------------------------------------------------------
# Camino feliz.
# --------------------------------------------------------------------------


def test_accepts_valid_batch_and_returns_pending_meta() -> None:
    recorder = _Recorder()
    service, repo, store, queue, events = _build_service(recorder)

    meta = service.execute(_command([_item("hola"), _item("mundo")]))

    assert meta.status is AnalysisStatus.PENDING
    assert meta.item_count == 2
    assert meta.analysis_id == _FIXED_ULID
    assert meta.created_at == _FIXED_NOW
    assert meta.tenant_id == _TENANT
    assert meta.owner_sub == _SUB
    # Un mensaje a SQS con el analysis_id y el correlation_id (criterios 4.8, 4.9).
    assert queue.messages == [(_FIXED_ULID, _CORRELATION)]
    assert not events.published


def test_analysis_id_is_a_valid_ulid() -> None:
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    meta = service.execute(_command())
    assert is_valid_ulid(meta.analysis_id)


def test_write_order_is_s3_then_dynamo_then_sqs() -> None:
    """Criterios 4.1, 4.3, 4.8: SQS es siempre lo último."""
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    service.execute(_command())
    assert recorder.kinds() == ["s3", "ddb", "sqs"]


def test_input_key_uses_utc_date_and_tenant_prefix() -> None:
    """Criterios 2.8, 4.1: inputs/{tenant}/{yyyy}/{mm}/{dd}/{analysis_id}.json."""
    recorder = _Recorder()
    service, _repo, store, *_ = _build_service(recorder)
    service.execute(_command())
    expected = f"inputs/{_TENANT}/2025/06/14/{_FIXED_ULID}.json"
    assert expected in store.puts


# --------------------------------------------------------------------------
# Validación total: ninguna escritura ante un rechazo (criterios 3.1, 3.13).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", ValidationError),
        (b"not json", ValidationError),
        (b"[]", ValidationError),  # raíz no es objeto
        (_body([]), ValidationError),  # items vacío
        (_body([{"respuesta": "x"}]), UnknownIndificadorError),  # falta indificador
        (_body([_item(indificador="desconocido")]), UnknownIndificadorError),
        (_body([{"indificador": _VALID_INDIFICADOR}]), ValidationError),  # falta respuesta
        (_body([_item()], tenant_id="otro"), ValidationError),  # tenant_id en cuerpo (1.11)
        (_body([_item()], extra_field=1), ValidationError),  # campo no declarado
    ],
)
def test_invalid_body_raises_without_any_write(
    body: bytes, expected: type[SentimentError]
) -> None:
    recorder = _Recorder()
    service, repo, store, queue, events = _build_service(recorder)
    command = SubmitCommand(
        tenant_id=_TENANT, owner_sub=_SUB, raw_body=body, correlation_id=_CORRELATION
    )
    with pytest.raises(expected):
        service.execute(command)
    assert recorder.kinds() == []  # cero escrituras
    assert not repo.metas
    assert not store.puts
    assert not queue.messages


def test_body_tenant_id_is_validation_failed_exact_code() -> None:
    """Criterio 1.11: un tenant_id en el cuerpo es VALIDATION_FAILED exacto."""
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    with pytest.raises(ValidationError) as exc:
        service.execute(_command([_item()], tenant_id="otro"))
    assert exc.value.error_code == "VALIDATION_FAILED"


def test_payload_too_large_rejected_before_parsing() -> None:
    """Criterio 3.4: el tamaño se mide antes de parsear."""
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    big = b"x" * (5 * 1024 * 1024 + 1)  # ni siquiera es JSON válido
    with pytest.raises(PayloadTooLargeError):
        service.execute(
            SubmitCommand(
                tenant_id=_TENANT, owner_sub=_SUB, raw_body=big, correlation_id=_CORRELATION
            )
        )


def test_too_many_items_rejected() -> None:
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    items = [_item("x") for _ in range(MAX_ITEMS + 1)]
    with pytest.raises(TooManyItemsError):
        service.execute(_command(items))


def test_response_too_long_in_bytes() -> None:
    """Criterio 3.10: el límite es en bytes UTF-8, no en caracteres."""
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    # 2501 caracteres de 2 bytes cada uno = 5002 bytes > 5000, pero <= 5000 chars.
    long_text = "ñ" * 2501
    assert len(long_text) <= MAX_RESPONSE_BYTES  # pasa el primer guard por caracteres
    assert len(long_text.encode("utf-8")) > MAX_RESPONSE_BYTES
    with pytest.raises(ResponseTooLongError):
        service.execute(_command([_item(long_text)]))


# --------------------------------------------------------------------------
# Precedencia del error_code (criterio 3.16).
# --------------------------------------------------------------------------


def test_precedence_too_many_items_over_response_too_long() -> None:
    """TOO_MANY_ITEMS gana a RESPONSE_TOO_LONG aunque haya respuestas largas."""
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    long_text = "a" * (MAX_RESPONSE_BYTES + 1)
    items = [_item(long_text) for _ in range(MAX_ITEMS + 1)]
    with pytest.raises(SentimentError) as exc:
        service.execute(_command(items))
    assert exc.value.error_code == "TOO_MANY_ITEMS"


def test_precedence_unknown_indificador_over_response_too_long() -> None:
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    long_text = "a" * (MAX_RESPONSE_BYTES + 1)
    items = [_item(long_text, indificador="desconocido")]
    with pytest.raises(SentimentError) as exc:
        service.execute(_command(items))
    assert exc.value.error_code == "UNKNOWN_INDIFICADOR"


def test_response_too_long_reports_lowest_index() -> None:
    """Criterio 3.16: dentro de un código, gana el index más bajo."""
    recorder = _Recorder()
    service, *_ = _build_service(recorder)
    long_text = "a" * (MAX_RESPONSE_BYTES + 1)
    items = [_item("ok"), _item(long_text), _item(long_text)]
    with pytest.raises(ResponseTooLongError) as exc:
        service.execute(_command(items))
    assert exc.value.detail["index"] == 1


# --------------------------------------------------------------------------
# Normalización NFC + trim (criterio 3.17).
# --------------------------------------------------------------------------


def test_respuesta_is_normalized_nfc_and_trimmed() -> None:
    recorder = _Recorder()
    service, _repo, store, *_ = _build_service(recorder)
    # "é" como e + combining acute (NFD); tras NFC debe quedar un solo code point.
    nfd = "  cafe\u0301  "
    service.execute(_command([_item(nfd)]))
    key = f"inputs/{_TENANT}/2025/06/14/{_FIXED_ULID}.json"
    persisted = store.puts[key]
    assert isinstance(persisted, dict)
    items = persisted["items"]
    assert isinstance(items, list)
    assert items[0]["respuesta"] == "café"  # NFC + trim


# --------------------------------------------------------------------------
# Cuota de Lotes en vuelo (criterio 4.12).
# --------------------------------------------------------------------------


def _inflight_meta(idx: int, status: AnalysisStatus) -> AnalysisMeta:
    return AnalysisMeta(
        analysis_id=f"01JDQZ8F3K7XWQ2M5N8P4R6T{idx:02d}",
        tenant_id=_TENANT,
        owner_sub=_SUB,
        status=status,
        item_count=1,
    )


def test_quota_exceeded_blocks_without_writes() -> None:
    recorder = _Recorder()
    existing = [
        _inflight_meta(i, AnalysisStatus.PENDING) for i in range(MAX_INFLIGHT_PER_TENANT)
    ]
    repo = FakeRepository(recorder, existing=existing)
    service, repo, store, queue, events = _build_service(recorder, repository=repo)
    with pytest.raises(QuotaExceededError):
        service.execute(_command())
    assert recorder.kinds() == []
    assert not store.puts


def test_terminal_batches_do_not_count_toward_quota() -> None:
    recorder = _Recorder()
    existing = [
        _inflight_meta(i, AnalysisStatus.COMPLETED)
        for i in range(MAX_INFLIGHT_PER_TENANT + 3)
    ]
    repo = FakeRepository(recorder, existing=existing)
    service, repo, *_ = _build_service(recorder, repository=repo)
    # Todos terminales: no cuentan, la ingesta procede.
    meta = service.execute(_command())
    assert meta.status is AnalysisStatus.PENDING


# --------------------------------------------------------------------------
# Fallos de infraestructura (criterios 4.10, 4.11).
# --------------------------------------------------------------------------


def test_s3_failure_surfaces_internal_error_without_ddb_or_sqs() -> None:
    recorder = _Recorder()
    store = FakeStorage(recorder, fail=True)
    service, repo, store, queue, events = _build_service(recorder, storage=store)
    with pytest.raises(SentimentError) as exc:
        service.execute(_command())
    assert exc.value.error_code == "INTERNAL_ERROR"
    assert recorder.kinds() == []  # el fallo de put_json no registra
    assert not repo.metas
    assert not queue.messages
    assert not events.published


def test_seeding_failure_closes_meta_and_publishes_event_then_500() -> None:
    """Criterio 4.11: META en FAILED + Evento_Finalizacion, sin encolar, y 500."""
    recorder = _Recorder()
    repo = FakeRepository(recorder, seed_fails=True, meta_written_on_fail=True)
    service, repo, store, queue, events = _build_service(recorder, repository=repo)
    with pytest.raises(SentimentError) as exc:
        service.execute(_command())
    assert exc.value.error_code == "INTERNAL_ERROR"
    # El META quedó en FAILED con un error_code del catálogo.
    assert repo.failed_calls == [(_FIXED_ULID, "INTERNAL_ERROR")]
    assert repo.metas[_FIXED_ULID].status is AnalysisStatus.FAILED
    # Se publicó el evento de cierre y NO se encoló nada.
    assert len(events.published) == 1
    assert events.published[0].status is AnalysisStatus.FAILED
    assert not queue.messages
    # Orden: s3, ddb (falla), ddb_fail, event.
    assert recorder.kinds() == ["s3", "ddb", "ddb_fail", "event"]


def test_seeding_failure_without_meta_skips_event() -> None:
    """Si el META nunca se escribió, no hay Lote del que notificar (criterio 4.11)."""
    recorder = _Recorder()
    repo = FakeRepository(recorder, seed_fails=True, meta_written_on_fail=False)
    service, repo, store, queue, events = _build_service(recorder, repository=repo)
    with pytest.raises(SentimentError):
        service.execute(_command())
    assert not events.published
    assert not queue.messages


# --------------------------------------------------------------------------
# Helpers puros del módulo.
# --------------------------------------------------------------------------


def test_build_input_key_partitions_by_utc_date() -> None:
    key = _build_input_key("t1", datetime(2024, 1, 5, 23, 59, tzinfo=UTC), _FIXED_ULID)
    assert key == f"inputs/t1/2024/01/05/{_FIXED_ULID}.json"


def test_canonicalize_keeps_only_contract_fields_in_order() -> None:
    answers = [
        SurveyAnswer(indificador=_VALID_INDIFICADOR, respuesta="a", index=0),
        SurveyAnswer(indificador=_VALID_INDIFICADOR, respuesta="b", index=1),
    ]
    assert _canonicalize(answers) == {
        "items": [
            {"indificador": _VALID_INDIFICADOR, "respuesta": "a"},
            {"indificador": _VALID_INDIFICADOR, "respuesta": "b"},
        ]
    }


def test_known_indificadores_has_eleven_values() -> None:
    assert len(_known_indificadores()) == 11
    assert _VALID_INDIFICADOR in _known_indificadores()
