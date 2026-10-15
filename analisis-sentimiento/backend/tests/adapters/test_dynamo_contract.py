"""Pruebas de contrato del ``DynamoRepository`` con AWS simulado (tarea 6.8).

Complementan las pruebas de humo de :mod:`tests.adapters.test_dynamo_repository`
(tarea 6.4) con el contrato que la tarea 6.8 exige verificar sobre una DynamoDB
simulada con ``moto`` y **solo datos sintéticos** (criterio 17.10):

* **Idempotencia de ``complete_item`` (criterios 8.4, 8.5).** La transición
  ITEM→``DONE`` + incremento de ``done_count`` ocurre **exactamente una vez** por
  Ítem, aunque la Maquina_De_Estados reintente la tarea muchas veces (entrega "al
  menos una vez"). La ``ConditionalCheckFailedException`` que DynamoDB lanza al
  reprocesar un Ítem ya terminal se trata como **éxito idempotente** (devuelve
  ``False``), no como error: el contador no se mueve y no se propaga excepción.
  Se verifica además que la atomicidad del ``TransactWriteItems`` mantiene el
  contador en línea con el número real de Ítems ``DONE`` a través de varios Ítems.

* **Relectura con ``get_item`` (criterio 7.2, base del guard de idempotencia de
  P2).** El nuevo método de dominio ``get_item`` relee un ITEM por su ``item_id``
  con lectura fuertemente consistente: devuelve lo que ``create_analysis`` sembró
  (``indificador``/``respuesta``/``PENDING``), refleja la escritura terminal de un
  ``complete_item`` previo y traduce un ITEM inexistente a ``NotFoundError``.

Estas pruebas usan el mismo esquema de tabla (claves, GSI1, GSI2) que despliega
el ``data_stack`` de la tarea 11.2, de modo que el repositorio se ejercita contra
la misma forma de tabla que la Infraestructura crea.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

if TYPE_CHECKING:
    from botocore.client import BaseClient

from sentiment.adapters.dynamo_repository import DynamoRepository
from sentiment.domain.errors import NotFoundError
from sentiment.domain.ids import make_item_id, new_analysis_id
from sentiment.domain.models import (
    AnalysisMeta,
    AnalysisStatus,
    EmotionAnalysis,
    ItemStatus,
    SurveyAnswer,
)

_TABLE = "sentiment-test-analysis"
_REGION = "us-east-1"
_TENANT = "tenant-contract"
_INDIFICADOR = "liderazgo"


@pytest.fixture
def dynamo_table() -> Iterator[BaseClient]:
    """Crea la single-table simulada con GSI1 y GSI2 y cede el cliente de boto3."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
                {"AttributeName": "gsi2pk", "AttributeType": "S"},
                {"AttributeName": "gsi2sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2",
                    "KeySchema": [
                        {"AttributeName": "gsi2pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
            ],
        )
        yield client


@pytest.fixture
def repo(dynamo_table: BaseClient) -> DynamoRepository:
    """Instancia el repositorio con el cliente simulado y la tabla de prueba."""
    return DynamoRepository(client=dynamo_table, table_name=_TABLE)


def _make_meta(analysis_id: str, item_count: int) -> AnalysisMeta:
    """Construye un META ``PENDING`` sintético con marcas de tiempo deterministas."""
    created = datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC)
    return AnalysisMeta(
        analysis_id=analysis_id,
        tenant_id=_TENANT,
        owner_sub="user-contract",
        status=AnalysisStatus.PENDING,
        item_count=item_count,
        input_s3_key=f"inputs/{_TENANT}/2024/05/06/{analysis_id}.json",
        created_at=created,
        updated_at=created,
    )


def _answers(count: int) -> list[SurveyAnswer]:
    """Genera ``count`` respuestas sintéticas (sin PII real)."""
    return [
        SurveyAnswer(
            indificador=_INDIFICADOR, respuesta=f"respuesta sintetica {i}", index=i
        )
        for i in range(count)
    ]


def _emotions(item_id: str) -> EmotionAnalysis:
    """Construye un resultado profundo válido y sintético para un ITEM."""
    return EmotionAnalysis(
        item_id=item_id,
        sentimientos=("frustracion", "esperanza"),
        explicacion="justificacion sintetica",
        model_id="model-contract",
        input_tokens=11,
        output_tokens=22,
    )


# ---------------------------------------------------------------------------
# complete_item: idempotencia exacta (criterios 8.4, 8.5)
# ---------------------------------------------------------------------------


def test_complete_item_increments_done_count_exactly_once_under_many_retries(
    repo: DynamoRepository,
) -> None:
    """Reprocesar el mismo Ítem N veces mueve ``done_count`` una sola vez (8.4, 8.5).

    Simula la entrega "al menos una vez" de la orquestación: la primera llamada
    aplica la transición (``True``); las nueve siguientes son no-ops idempotentes
    (``False``) que **no** vuelven a incrementar el contador ni degradan el estado
    ``DONE`` del Ítem.
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    results = [
        repo.complete_item(analysis_id, item_id, _emotions(item_id))
        for _ in range(10)
    ]

    assert results[0] is True
    assert all(r is False for r in results[1:])

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.done_count == 1
    assert meta.failed_count == 0
    assert meta.skipped_count == 0

    item = repo.get_item(analysis_id, item_id)
    assert item.status is ItemStatus.DONE


def test_conditional_check_failed_is_treated_as_idempotent_success(
    repo: DynamoRepository,
) -> None:
    """Un Ítem ya terminal no propaga error: ``complete_item`` devuelve ``False`` (8.5).

    Tras un ``fail_item`` (ITEM ya ``FAILED``), la condición ``status == PENDING``
    de ``complete_item`` falla y DynamoDB cancela la transacción con
    ``ConditionalCheckFailed``. El repositorio lo absorbe como éxito idempotente:
    no lanza, no incrementa ``done_count`` y conserva el estado ``FAILED`` previo.
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    assert repo.fail_item(analysis_id, item_id, "LLM_OUTPUT_INVALID") is True

    # No debe lanzar: la ConditionalCheckFailed se trata como no-op idempotente.
    assert repo.complete_item(analysis_id, item_id, _emotions(item_id)) is False

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.done_count == 0
    assert meta.failed_count == 1
    assert repo.get_item(analysis_id, item_id).status is ItemStatus.FAILED


def test_done_count_matches_real_done_items_across_batch(
    repo: DynamoRepository,
) -> None:
    """La atomicidad del transact mantiene ``done_count`` == nº real de Ítems ``DONE``.

    Completa 3 de 5 Ítems (cada uno reprocesado dos veces para ejercer la
    idempotencia) y verifica que el contador del META refleja exactamente los
    Ítems realmente ``DONE``, no la suma de intentos (base de P11).
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 5), _answers(5))

    for index in (0, 2, 4):
        item_id = make_item_id(analysis_id, index)
        repo.complete_item(analysis_id, item_id, _emotions(item_id))
        repo.complete_item(analysis_id, item_id, _emotions(item_id))  # reintento

    meta = repo.get_meta(analysis_id, _TENANT)
    done_items = [i for i in repo.iter_items(analysis_id) if i.status is ItemStatus.DONE]
    assert meta.done_count == 3
    assert len(done_items) == 3
    assert {i.index for i in done_items} == {0, 2, 4}


# ---------------------------------------------------------------------------
# get_item: relectura fuertemente consistente (criterio 7.2)
# ---------------------------------------------------------------------------


def test_get_item_reads_back_seeded_item(repo: DynamoRepository) -> None:
    """``get_item`` devuelve lo que ``create_analysis`` sembró para ese Ítem (7.2)."""
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 3), _answers(3))
    item_id = make_item_id(analysis_id, 1)

    item = repo.get_item(analysis_id, item_id)

    assert item.item_id == item_id
    assert item.analysis_id == analysis_id
    assert item.index == 1
    assert item.indificador == _INDIFICADOR
    assert item.respuesta == "respuesta sintetica 1"
    assert item.status is ItemStatus.PENDING
    assert item.emotions is None


def test_get_item_reflects_terminal_write_from_complete_item(
    repo: DynamoRepository,
) -> None:
    """Tras ``complete_item``, ``get_item`` observa el estado terminal (base de P2).

    Es la lectura fuertemente consistente que permite al guard de idempotencia de
    ``analyze_item_emotions`` ver el ``DONE`` escrito por un intento anterior y no
    reinvocar al Modelo_Bedrock (criterio 7.2).
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    repo.complete_item(analysis_id, item_id, _emotions(item_id))

    item = repo.get_item(analysis_id, item_id)
    assert item.status is ItemStatus.DONE
    assert item.emotions is not None
    assert item.emotions.sentimientos == ("frustracion", "esperanza")
    assert item.emotions.model_id == "model-contract"


def test_get_item_raises_not_found_for_missing_item(repo: DynamoRepository) -> None:
    """Un ITEM inexistente en un Lote existente se traduce a ``NotFoundError`` (7.14).

    El Lote tiene 1 Ítem (index 0); pedir el index 7 —cuya ``sk`` no existe— no
    devuelve un ITEM vacío ni ``None``: propaga ``NotFoundError``, de modo que el
    servicio llamante no invoque al modelo ni escriba nada.
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    missing_item_id = make_item_id(analysis_id, 7)

    with pytest.raises(NotFoundError):
        repo.get_item(analysis_id, missing_item_id)
