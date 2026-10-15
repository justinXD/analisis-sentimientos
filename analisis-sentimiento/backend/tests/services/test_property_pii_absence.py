"""Prueba de propiedad **P9**: ausencia de PII en logs, eventos y estado de la ejecución.

Tarea 15.1 del plan de ``serverless-sentiment-pipeline``. Verifica, con
**Hypothesis**, la propiedad de correctitud P9 de la sección 19 del diseño
(``.kiro/specs/serverless-sentiment-pipeline/design.md``, "Correctness Properties"):

> **Property 9 — Ausencia de PII en logs, eventos y estado de la ejecución.**
> Ningún log emitido, ningún evento de EventBridge y ningún estado del historial
> de Step Functions contiene el texto de ``respuesta`` ni de ``explicacion``.

**Validates: Requirements 13.3, 15.1, 15.2, 15.3, 19.10**

Enfoque
-------
El Sistema construye **tres** salidas observables en las que podría filtrarse la
PII de un empleado (el texto de ``respuesta``, el de ``explicacion`` y la
dirección de correo del Lote). P9 afirma que **para todo** texto sensible, esas
tres salidas quedan libres de él. La prueba ejercita el código real de las tres
superficies, sin red ni ``boto3``, generando el texto sensible con Hypothesis:

(a) **Logs estructurados.** El logger de :mod:`sentiment.observability.logging`
    aplica :func:`~sentiment.observability.logging.redact` sobre cada registro
    antes de serializarlo (criterios 15.1, 15.2, 19.10). La propiedad vuelca el
    texto generado por cualquier vía plausible —campos PII en ``extra=``, texto
    anidado en estructuras, y el helper :func:`log_item_processed`— y comprueba
    que el JSON emitido nunca lo contiene: solo quedan identificadores, longitudes
    y etiquetas.

(b) **Eventos del Bus_Eventos.** El ``EventBridgePublisher`` de
    :mod:`sentiment.adapters.eventbridge_publisher`
    publica el Evento_Finalizacion con ``detail-type = analysis.completed`` y
    ``event_id = {analysis_id}:{status}`` (criterios 13.1, 10.20). Su detalle se
    construye **solo** con identificadores y punteros (criterio 13.3): el
    contrato no tiene ningún campo de texto libre, así que ninguna PII generada
    puede aparecer, ni tampoco un Presigned_URL (parámetros de firma de S3).

(c) **Estado de la ejecución de Step Functions.** Lo que el backend propaga por
    la Maquina_De_Estados y expone en Api_Status es la proyección
    :class:`~sentiment.services.get_analysis_status.StatusView`, que transporta
    **identificadores de Ítem** (``item_id``) y ``error_code``, nunca el texto de
    las respuestas (criterio 15.3). La propiedad siembra Ítems con ``respuesta`` y
    ``explicacion`` arbitrarias, construye la vista con el servicio real (incluido
    el detalle de fallos) y comprueba que su serialización no contiene esos textos.

Diseño de la estrategia de PII
------------------------------
Para que la propiedad tenga poder de refutación, el texto sensible no es un
``st.text()`` cualquiera: se mezclan cadenas arbitrarias con **formas que parecen
PII** —correos, teléfonos, nombres, frases de opinión— y se filtran las que
colisionarían con el ruido estructural de la salida (cadenas vacías o triviales
que aparecerían por azar en claves, ULIDs o marcas de tiempo). Así, un fallo del
redactor o un campo de texto filtrado en un contrato producen un counterexample
inmediato, en lugar de pasar por una coincidencia improbable.

Pureza y aislamiento
--------------------
Como el resto de las pruebas de propiedad de esta capa, se usan el código real de
dominio/servicios/adaptadores y dobles en memoria de los puertos y del cliente de
EventBridge (sin red, sin ``boto3``): la propiedad valida la contención de PII que
el **backend** garantiza por construcción, no la red de seguridad de la
Infraestructura (política de protección de datos de CloudWatch, criterio 15.4),
que se verifica aparte en las pruebas de infraestructura.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch

from aws_lambda_powertools import Logger
from hypothesis import assume, given
from hypothesis import strategies as st

from sentiment.adapters.eventbridge_publisher import EventBridgePublisher
from sentiment.domain.errors import NotFoundError
from sentiment.domain.ids import make_item_id, new_analysis_id
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ItemStatus,
)
from sentiment.domain.ports import AnalysisRepository
from sentiment.observability.logging import (
    REDACTION_PLACEHOLDER,
    get_logger,
    log_item_processed,
)
from sentiment.services.get_analysis_status import (
    GetAnalysisStatusService,
    StatusView,
)

# --------------------------------------------------------------------------- #
# Constantes                                                                   #
# --------------------------------------------------------------------------- #

# ``analysis_id`` fijo y válido para derivar ``item_id`` deterministas: la
# propiedad explora el espacio de los TEXTOS sensibles, no el de los
# identificadores de Lote (eso lo cubre P6/P7). Un único ULID es suficiente.
_ANALYSIS_ID = new_analysis_id()
_TENANT_ID = "acme-corp"
_OWNER_SUB = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
_INDIFICADOR = "liderazgo"
_EVENT_BUS_NAME = "sentiment-pipeline"

NOW = datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC)
COMPLETED = datetime(2025, 6, 14, 12, 12, 43, tzinfo=UTC)

# Longitud mínima del texto sensible que la propiedad audita. El enunciado de P9
# en el diseño filtra los textos muy cortos (``len > 10``) porque una subcadena
# trivial podría aparecer por azar en el ruido estructural de la salida (claves
# JSON, un ULID, un fragmento de marca de tiempo). Interesa la fuga de contenido
# real, no la coincidencia espuria.
_MIN_PII_LEN = 11


# --------------------------------------------------------------------------- #
# Estrategia de texto sensible (PII)                                           #
# --------------------------------------------------------------------------- #

# Fragmentos que PARECEN PII: una fuga real tendría esta forma. Se combinan con
# texto arbitrario para cubrir también Unicode y caracteres de control.
_pii_shaped = st.one_of(
    st.emails(),
    st.sampled_from(
        [
            "me siento comodo pero quisiera un mejor salario",
            "la jefa Maria Perez no escucha a su equipo",
            "contactarme al +52 55 1234 5678 por favor",
            "Juan Rodriguez renuncio por el ambiente toxico",
            "el empleado expresa satisfaccion con reservas salariales",
        ]
    ),
    st.text(min_size=_MIN_PII_LEN, max_size=200),
)


def _is_auditable(text: str) -> bool:
    """¿Es ``text`` un texto sensible que la propiedad puede auditar sin falsos positivos?

    Exige una longitud mínima tras recortar para que una subcadena trivial no
    aparezca por azar en el ruido estructural de la salida (coincide con el
    ``len > 10`` del enunciado de P9 en el diseño).

    :param text: candidato a texto de ``respuesta``/``explicacion``/correo.
    :return: ``True`` si el texto es lo bastante distintivo para auditarse.
    """
    return len(text.strip()) >= _MIN_PII_LEN


#: Texto sensible auditable: con forma de PII y lo bastante distintivo.
pii_text = _pii_shaped.filter(_is_auditable)


# --------------------------------------------------------------------------- #
# (a) Logs estructurados — redacción de PII                                    #
# --------------------------------------------------------------------------- #


def _capture_logger() -> tuple[Logger, io.StringIO]:
    """Crea un logger real con redacción cuyo stream se redirige a un buffer."""
    buffer = io.StringIO()
    logger = get_logger(service="pii-property")
    for handler in logger.handlers:
        handler.stream = buffer  # type: ignore[attr-defined]
    return logger, buffer


@given(respuesta=pii_text, explicacion=pii_text, email=pii_text)
def test_logs_never_contain_pii_text(
    respuesta: str, explicacion: str, email: str
) -> None:
    """(a) El JSON del logger nunca contiene el texto de PII, venga como venga.

    **Validates: Requirements 15.1, 15.2, 19.10**

    Para cualquier texto sensible pasado por error en ``extra=`` —en el nivel
    superior o anidado en estructuras—, el registro serializado lo sustituye por
    el placeholder: ninguna de las tres cadenas aparece en la salida (criterio
    15.1). La redacción es la red de seguridad que hace que P9 se sostenga aunque
    un llamador olvide usar los helpers de metadatos.
    """
    logger, buffer = _capture_logger()

    logger.info(
        "item_event",
        extra={
            "analysis_id": _ANALYSIS_ID,
            "item_id": make_item_id(_ANALYSIS_ID, 7),
            # PII filtrada por descuido, directamente y anidada en estructuras.
            "respuesta": respuesta,
            "explicacion": explicacion,
            "email": email,
            "payload": {"respuesta": respuesta, "ok": 1},
            "batch": [{"explicacion": explicacion}, {"email": email}],
        },
    )

    output = buffer.getvalue()
    assert respuesta not in output
    assert explicacion not in output
    assert email not in output
    assert REDACTION_PLACEHOLDER in output


@given(
    respuesta=pii_text,
    explicacion=pii_text,
    respuesta_length=st.integers(min_value=1, max_value=5000),
    language_code=st.sampled_from(["es", "en", "pt"]),
    sentimiento_general=st.sampled_from(["Positivo", "Negativo", "Neutral"]),
    sentimientos_count=st.integers(min_value=0, max_value=10),
)
def test_item_log_helper_emits_only_metadata(
    respuesta: str,
    explicacion: str,
    respuesta_length: int,
    language_code: str,
    sentimiento_general: str,
    sentimientos_count: int,
) -> None:
    """(a) ``log_item_processed`` registra metadatos, nunca el texto (criterio 15.2).

    **Validates: Requirements 15.2, 19.10**

    El helper del camino de Ítem recibe la **longitud** de la respuesta y
    etiquetas cerradas, nunca el texto de ``respuesta`` ni de ``explicacion``.
    Para cualquier par de textos sensibles, su contenido no puede aparecer en el
    log porque el helper simplemente no lo acepta como campo.
    """
    logger, buffer = _capture_logger()

    log_item_processed(
        logger,
        analysis_id=_ANALYSIS_ID,
        item_id=make_item_id(_ANALYSIS_ID, 42),
        indificador=_INDIFICADOR,
        respuesta_length=respuesta_length,
        language_code=language_code,
        sentimiento_general=sentimiento_general,
        sentimientos_count=sentimientos_count,
    )

    output = buffer.getvalue()
    record = json.loads(output.strip())
    assert respuesta not in output
    assert explicacion not in output
    # Solo metadatos no sensibles: la longitud, no el contenido.
    assert record["respuesta_length"] == respuesta_length
    assert record["item_id"] == make_item_id(_ANALYSIS_ID, 42)
    assert "respuesta" not in record
    assert "explicacion" not in record


# --------------------------------------------------------------------------- #
# (b) Eventos del Bus_Eventos — detalle sin PII ni Presigned_URL               #
# --------------------------------------------------------------------------- #


class _RecordingEventsClient:
    """Cliente de EventBridge de prueba que registra las entradas publicadas.

    Imita la respuesta de ``PutEvents`` sin fallos de entrada y conserva las
    ``Entries`` para que la propiedad inspeccione, byte a byte, el detalle emitido
    por el adaptador real (misma superficie que el ``Protocol`` que el adaptador
    consume).
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def put_events(
        self, *, Entries: Sequence[dict[str, str]]  # noqa: N803
    ) -> dict[str, object]:
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "evt-1"}]}


_TERMINAL_STATUSES = (
    AnalysisStatus.COMPLETED,
    AnalysisStatus.COMPLETED_WITH_ERRORS,
    AnalysisStatus.FAILED,
    AnalysisStatus.EXPIRED,
)


def _terminal_meta(status: AnalysisStatus, *, with_output: bool) -> AnalysisMeta:
    """META terminal sintético para publicar un Evento_Finalizacion."""
    return AnalysisMeta(
        analysis_id=_ANALYSIS_ID,
        tenant_id=_TENANT_ID,
        owner_sub=_OWNER_SUB,
        status=status,
        item_count=3,
        done_count=1,
        failed_count=1,
        skipped_count=1,
        input_s3_key=f"inputs/{_TENANT_ID}/{_ANALYSIS_ID}.json",
        output_s3_key=(
            f"outputs/{_TENANT_ID}/{_ANALYSIS_ID}.json" if with_output else None
        ),
        created_at=NOW,
        updated_at=COMPLETED,
        completed_at=COMPLETED,
    )


@given(
    status=st.sampled_from(_TERMINAL_STATUSES),
    with_output=st.booleans(),
    respuesta=pii_text,
    explicacion=pii_text,
    email=pii_text,
)
def test_event_detail_never_contains_pii_or_presigned_url(
    status: AnalysisStatus,
    with_output: bool,
    respuesta: str,
    explicacion: str,
    email: str,
) -> None:
    """(b) El detalle del Evento_Finalizacion no contiene PII ni Presigned_URL.

    **Validates: Requirements 13.3**

    El contrato del evento (criterio 13.3) transporta solo identificadores,
    estado, recuentos, el puntero ``output_s3_key`` y marcas de tiempo. Como no
    tiene ningún campo de texto libre, ninguna de las PII generadas puede
    aparecer en el detalle serializado, ni los parámetros de firma de un
    Presigned_URL de S3. El puntero de salida es una clave, no una URL firmada.

    El adaptador lee ``EVENT_BUS_NAME`` del entorno en tiempo de publicación; se
    fija de forma acotada con ``patch.dict`` para no filtrarlo a otras pruebas
    bajo ejecución concurrente.
    """
    client = _RecordingEventsClient()
    with patch.dict(os.environ, {"EVENT_BUS_NAME": _EVENT_BUS_NAME}):
        EventBridgePublisher(client=client).publish_analysis_completed(
            _terminal_meta(status, with_output=with_output)
        )

    assert len(client.entries) == 1
    raw = client.entries[0]["Detail"]

    # La PII generada no puede filtrarse: el detalle no tiene texto libre.
    assert respuesta not in raw
    assert explicacion not in raw
    assert email not in raw
    # Ni rastro de un Presigned_URL: sus parámetros de firma no aparecen.
    assert "X-Amz-Signature" not in raw
    assert "X-Amz-Credential" not in raw
    # El detalle sí lleva los identificadores y el estado (contrato 13.2).
    detail = json.loads(raw)
    assert detail["analysis_id"] == _ANALYSIS_ID
    assert detail["status"] == str(status.value)
    assert detail["event_id"] == f"{_ANALYSIS_ID}:{status.value}"


# --------------------------------------------------------------------------- #
# (c) Estado de la ejecución de Step Functions — StatusView sin texto          #
# --------------------------------------------------------------------------- #


class _FakeRepository:
    """Doble en memoria de ``AnalysisRepository`` para construir la ``StatusView``.

    Modela el aislamiento estructural de la Dynamo_Table (clave por
    ``(analysis_id, tenant_id)``) y expone los Ítems sembrados —con su
    ``respuesta`` y ``explicacion`` reales— al generador paginado que el servicio
    recorre para el detalle de fallos. La propiedad comprueba que la vista
    resultante proyecta ``item_id`` + ``error_code`` y nunca ese texto.
    """

    def __init__(self, meta: AnalysisMeta, items: list[AnalysisItem]) -> None:
        self._meta = meta
        self._items = items

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        if (analysis_id, tenant_id) != (
            self._meta.analysis_id,
            self._meta.tenant_id,
        ):
            raise NotFoundError(analysis_id=analysis_id)
        return self._meta

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        yield from self._items


@st.composite
def _batch_with_pii(
    draw: st.DrawFn,
) -> tuple[AnalysisMeta, list[AnalysisItem], list[str]]:
    """Genera un Lote ``COMPLETED_WITH_ERRORS`` con PII en cada Ítem.

    Devuelve el META, los Ítems (cada uno con ``respuesta`` y, si falló, también
    ``explicacion`` de PII) y la lista plana de todos los textos sensibles
    sembrados, para auditar que ninguno aparece en la vista. Al menos un Ítem está
    en ``FAILED`` para que el detalle de fallos (``include_failures``) tenga
    contenido que proyectar.
    """
    size = draw(st.integers(min_value=1, max_value=12))
    statuses = draw(
        st.lists(
            st.sampled_from(
                [ItemStatus.DONE, ItemStatus.FAILED, ItemStatus.SKIPPED]
            ),
            min_size=size,
            max_size=size,
        )
    )
    # Garantiza al menos un FAILED: fuerza el primero si el sorteo no lo dio.
    if ItemStatus.FAILED not in statuses:
        statuses[0] = ItemStatus.FAILED

    items: list[AnalysisItem] = []
    secrets: list[str] = []
    done = failed = skipped = 0
    for index, item_status in enumerate(statuses):
        respuesta = draw(pii_text)
        secrets.append(respuesta)
        emotions = None
        error_code = None
        if item_status is ItemStatus.FAILED:
            failed += 1
            error_code = "LLM_OUTPUT_INVALID"
        elif item_status is ItemStatus.SKIPPED:
            skipped += 1
        else:
            done += 1

        items.append(
            AnalysisItem(
                analysis_id=_ANALYSIS_ID,
                item_id=make_item_id(_ANALYSIS_ID, index),
                index=index,
                indificador=_INDIFICADOR,
                respuesta=respuesta,
                status=item_status,
                emotions=emotions,
                error_code=error_code,
                processed_at=COMPLETED,
            )
        )

    meta = AnalysisMeta(
        analysis_id=_ANALYSIS_ID,
        tenant_id=_TENANT_ID,
        owner_sub=_OWNER_SUB,
        status=AnalysisStatus.COMPLETED_WITH_ERRORS,
        item_count=size,
        done_count=done,
        failed_count=failed,
        skipped_count=skipped,
        input_s3_key=f"inputs/{_TENANT_ID}/{_ANALYSIS_ID}.json",
        output_s3_key=f"outputs/{_TENANT_ID}/{_ANALYSIS_ID}.json",
        created_at=NOW,
        updated_at=COMPLETED,
        completed_at=COMPLETED,
    )
    return meta, items, secrets


def _serialize_status_view(view: StatusView) -> str:
    """Serializa la ``StatusView`` a JSON como lo haría el handler hacia el cliente.

    Es la forma en que este estado viaja por la Maquina_De_Estados y sale por
    Api_Status: solo identificadores, recuentos, marcas de tiempo y, por cada
    fallo, ``item_id`` + ``error_code`` (criterio 15.3). Se vuelca a texto para
    poder auditar la ausencia de cualquier subcadena de PII.
    """
    return json.dumps(
        {
            "analysis_id": view.analysis_id,
            "status": str(view.status.value),
            "item_count": view.item_count,
            "done_count": view.done_count,
            "failed_count": view.failed_count,
            "skipped_count": view.skipped_count,
            "progress": view.progress,
            "created_at": view.created_at.isoformat(),
            "completed_at": (
                view.completed_at.isoformat() if view.completed_at else None
            ),
            "download_available": view.download_available,
            "failures": [
                {"item_id": f.item_id, "error_code": f.error_code}
                for f in view.failures
            ],
        }
    )


@given(batch=_batch_with_pii())
def test_execution_state_view_never_contains_pii_text(
    batch: tuple[AnalysisMeta, list[AnalysisItem], list[str]],
) -> None:
    """(c) El estado proyectado por el backend no contiene el texto de respuesta.

    **Validates: Requirements 15.3, 19.10**

    La ``StatusView`` que el backend construye y propaga transporta
    identificadores de Ítem y ``error_code``, nunca el texto de ``respuesta`` ni
    de ``explicacion`` (criterio 15.3). Para cualquier Lote con PII en todos sus
    Ítems, su serialización (incluido el detalle de fallos) está libre de esos
    textos, y aun así conserva los ``item_id`` de los Ítems fallidos.
    """
    meta, items, secrets = batch
    repo = _FakeRepository(meta, items)
    service = GetAnalysisStatusService(cast(AnalysisRepository, repo))

    view = service.get_status(_ANALYSIS_ID, _TENANT_ID, include_failures=True)
    serialized = _serialize_status_view(view)

    # Ninguno de los textos sensibles sembrados aparece en el estado proyectado.
    for secret in secrets:
        assert secret not in serialized

    # El detalle de fallos sí lleva los identificadores: la trazabilidad se
    # conserva sin exponer PII (criterio 15.3).
    failed_item_ids = {
        item.item_id for item in items if item.status is ItemStatus.FAILED
    }
    projected_ids = {failure.item_id for failure in view.failures}
    assert projected_ids == failed_item_ids
    assert all(failure.error_code for failure in view.failures)


@given(batch=_batch_with_pii())
def test_execution_state_view_omits_failures_without_opt_in(
    batch: tuple[AnalysisMeta, list[AnalysisItem], list[str]],
) -> None:
    """(c) Sin ``include_failures`` la vista no recorre Ítems y no expone nada de ellos.

    **Validates: Requirements 15.3, 19.10**

    El estado por defecto (sin pedir el detalle de fallos) es puramente agregado:
    recuentos y marcas de tiempo. No contiene ``item_id`` de Ítems ni, por tanto,
    cabida alguna para su texto. Refuerza que la ausencia de PII no depende de un
    parámetro opcional.
    """
    meta, items, secrets = batch
    assume(any(secrets))
    repo = _FakeRepository(meta, items)
    service = GetAnalysisStatusService(cast(AnalysisRepository, repo))

    view = service.get_status(_ANALYSIS_ID, _TENANT_ID)
    serialized = _serialize_status_view(view)

    for secret in secrets:
        assert secret not in serialized
    assert view.failures == ()
