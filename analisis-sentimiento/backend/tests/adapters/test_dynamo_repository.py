"""Pruebas de humo del adaptador ``DynamoRepository`` (tarea 6.4).

Verifican sobre una DynamoDB simulada (``moto``) el contrato del puerto
:class:`sentiment.domain.ports.AnalysisRepository` y las garantías clave del
modelo *single-table* de la sección 6 del diseño:

* ``create_analysis`` siembra el META y los N ITEM en ``PENDING`` con las claves
  ``pk = TENANT#{tenant_id}#ANALYSIS#{analysis_id}`` y ``sk`` META/ITEM, incluso
  cuando el número de escrituras excede una tanda de 25 (criterios 4.3, 4.4);
* ``get_meta`` acota por ``tenant_id``: un Lote de otro tenant no es visible y se
  traduce a :class:`~sentiment.domain.errors.NotFoundError` (criterios 2.1, 2.2);
* ``complete_item`` es atómico e **idempotente**: procesar el mismo ``item_id``
  varias veces deja el ITEM en ``DONE`` e incrementa ``done_count`` exactamente
  una vez, y las llamadas repetidas devuelven ``False`` (criterios 8.3, 8.4, 8.5,
  8.10 — base de P2 y P11);
* ``fail_item`` tiene el mismo contrato idempotente sobre ``failed_count``;
* ``complete_item`` tras ``fail_item`` (ya terminal) no vuelve a tocar contadores;
* ``finalize_analysis`` fija el estado terminal y los recuentos reales (10.4);
* ``list_sweep_candidates`` devuelve el META por GSI2 mientras no sea terminal con
  evento publicado, y ``mark_event_published`` lo saca del índice (patrón 8);
* ``delete_items``/``delete_meta`` purgan el Lote de forma reintentable (patrón 9).

Las pruebas de contrato exhaustivas con ``moto`` son la tarea 6.8; aquí se cubre
el camino feliz y los bordes esenciales, con énfasis en la idempotencia.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
_TENANT = "tenant-a"
_OTHER_TENANT = "tenant-b"
_INDIFICADOR = "liderazgo"


@pytest.fixture
def dynamo_table() -> Iterator[BaseClient]:
    """Crea la single-table simulada con GSI1 y GSI2 y cede el cliente de boto3.

    El esquema (claves, índices y tipos) replica el ``data_stack`` de la tarea
    11.2, de modo que el repositorio se prueba contra la misma forma de tabla que
    la Infraestructura despliega.
    """
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


def _make_meta(
    analysis_id: str, item_count: int, *, tenant_id: str = _TENANT
) -> AnalysisMeta:
    """Construye un META ``PENDING`` con marcas de tiempo deterministas."""
    created = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    return AnalysisMeta(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        owner_sub="user-1",
        status=AnalysisStatus.PENDING,
        item_count=item_count,
        input_s3_key=f"inputs/{tenant_id}/2024/01/02/{analysis_id}.json",
        created_at=created,
        updated_at=created,
    )


def _answers(count: int) -> list[SurveyAnswer]:
    """Genera ``count`` respuestas sintéticas (sin PII real)."""
    return [
        SurveyAnswer(indificador=_INDIFICADOR, respuesta=f"respuesta {i}", index=i)
        for i in range(count)
    ]


def _emotions(item_id: str) -> EmotionAnalysis:
    """Construye un resultado profundo válido para un ITEM."""
    return EmotionAnalysis(
        item_id=item_id,
        sentimientos=("frustración", "esperanza"),
        explicacion="justificación breve",
        model_id="model-x",
        input_tokens=12,
        output_tokens=34,
    )


def test_create_analysis_seeds_meta_and_items_across_batches(
    repo: DynamoRepository,
) -> None:
    """Siembra META + N ITEM en ``PENDING``, incluso con más de 25 escrituras."""
    analysis_id = new_analysis_id()
    meta = _make_meta(analysis_id, item_count=30)  # 1 META + 30 ITEM = 2 tandas

    repo.create_analysis(meta, _answers(30))

    stored = repo.get_meta(analysis_id, _TENANT)
    assert stored.status is AnalysisStatus.PENDING
    assert stored.item_count == 30

    items = list(repo.iter_items(analysis_id))
    assert len(items) == 30
    assert {item.index for item in items} == set(range(30))
    assert all(item.status is ItemStatus.PENDING for item in items)


def test_get_meta_is_tenant_scoped(repo: DynamoRepository) -> None:
    """Un Lote no es visible para otro tenant: la lectura ajena es ``NotFoundError``."""
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))

    with pytest.raises(NotFoundError):
        repo.get_meta(analysis_id, _OTHER_TENANT)


def test_complete_item_is_idempotent_counter_increments_once(
    repo: DynamoRepository,
) -> None:
    """Procesar el mismo ``item_id`` 3 veces incrementa ``done_count`` una sola vez.

    Es la garantía central de P2/P11 (criterios 8.3, 8.4, 8.5): la primera llamada
    aplica la transición y devuelve ``True``; las siguientes son no-ops idempotentes
    que devuelven ``False`` sin volver a tocar el contador.
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    first = repo.complete_item(analysis_id, item_id, _emotions(item_id))
    second = repo.complete_item(analysis_id, item_id, _emotions(item_id))
    third = repo.complete_item(analysis_id, item_id, _emotions(item_id))

    assert first is True
    assert second is False
    assert third is False

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.done_count == 1
    assert meta.failed_count == 0

    items = list(repo.iter_items(analysis_id))
    assert len(items) == 1
    assert items[0].status is ItemStatus.DONE
    assert items[0].emotions is not None
    assert items[0].emotions.sentimientos == ("frustración", "esperanza")


def test_fail_item_is_idempotent_counter_increments_once(
    repo: DynamoRepository,
) -> None:
    """``fail_item`` repetido deja el ITEM en ``FAILED`` e incrementa una sola vez."""
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    first = repo.fail_item(analysis_id, item_id, "LLM_OUTPUT_INVALID")
    second = repo.fail_item(analysis_id, item_id, "LLM_OUTPUT_INVALID")

    assert first is True
    assert second is False

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.failed_count == 1
    assert meta.done_count == 0


def test_skip_item_is_idempotent_counter_increments_once(
    repo: DynamoRepository,
) -> None:
    """``skip_item`` repetido deja el ITEM en ``SKIPPED`` e incrementa una sola vez.

    Mismo contrato idempotente que ``complete_item``/``fail_item`` sobre
    ``skipped_count`` (criterios 6.6, 8.3, 8.5): la primera llamada aplica la
    transición y devuelve ``True``; las siguientes son no-ops que devuelven
    ``False`` sin volver a mover el contador. El ITEM queda sin ``error_code``.
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    first = repo.skip_item(analysis_id, item_id)
    second = repo.skip_item(analysis_id, item_id)

    assert first is True
    assert second is False

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.skipped_count == 1
    assert meta.done_count == 0
    assert meta.failed_count == 0

    item = next(iter(repo.iter_items(analysis_id)))
    assert item.status is ItemStatus.SKIPPED
    assert item.error_code is None


def test_complete_after_fail_does_not_double_count(repo: DynamoRepository) -> None:
    """Un ITEM ya ``FAILED`` no se transiciona a ``DONE`` ni mueve ``done_count``.

    La condición ``status == PENDING`` de ``complete_item`` falla sobre un ITEM
    terminal; el resultado es un éxito idempotente (``False``) que no incrementa
    ``done_count`` ni degrada la contabilización previa (criterio 8.5).
    """
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    assert repo.fail_item(analysis_id, item_id, "COMPREHEND_ITEM_FAILED") is True
    assert repo.complete_item(analysis_id, item_id, _emotions(item_id)) is False

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.failed_count == 1
    assert meta.done_count == 0
    assert next(iter(repo.iter_items(analysis_id))).status is ItemStatus.FAILED


def test_finalize_analysis_sets_terminal_status_and_counts(
    repo: DynamoRepository,
) -> None:
    """``finalize_analysis`` fija el estado terminal, los recuentos y la salida."""
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 2), _answers(2))

    output_key = f"outputs/{_TENANT}/2024/01/02/{analysis_id}.json"
    repo.finalize_analysis(
        analysis_id,
        AnalysisStatus.COMPLETED,
        counts=(2, 0, 0),
        output_s3_key=output_key,
    )

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.status is AnalysisStatus.COMPLETED
    assert (meta.done_count, meta.failed_count, meta.skipped_count) == (2, 0, 0)
    assert meta.output_s3_key == output_key
    assert meta.completed_at is not None


def test_list_sweep_candidates_then_mark_event_published(
    repo: DynamoRepository,
) -> None:
    """El META es candidato del sweeper hasta que se publica su Evento_Finalizacion."""
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))

    future = datetime.now(tz=UTC) + timedelta(days=1)
    candidates = [m.analysis_id for m in repo.list_sweep_candidates(future)]
    assert analysis_id in candidates

    repo.mark_event_published(analysis_id)

    remaining = [m.analysis_id for m in repo.list_sweep_candidates(future)]
    assert analysis_id not in remaining


def test_mark_analysis_failed_is_idempotent_over_terminal(
    repo: DynamoRepository,
) -> None:
    """``mark_analysis_failed`` no sobrescribe un cierre terminal previo (criterio 4.11)."""
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))

    repo.finalize_analysis(
        analysis_id, AnalysisStatus.COMPLETED, counts=(1, 0, 0), output_s3_key="k"
    )
    # El META ya es terminal (COMPLETED): marcar FAILED debe ser una no-op.
    repo.mark_analysis_failed(analysis_id, "AGGREGATION_FAILED")

    meta = repo.get_meta(analysis_id, _TENANT)
    assert meta.status is AnalysisStatus.COMPLETED


def test_purge_deletes_items_then_meta(repo: DynamoRepository) -> None:
    """La purga borra los ITEM (reintentable) y, al final, el META (patrón 9)."""
    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 3), _answers(3))

    deleted = repo.delete_items(analysis_id, _TENANT)
    assert deleted == 3
    # Reintentable: una segunda pasada no encuentra ITEM que borrar.
    assert repo.delete_items(analysis_id, _TENANT) == 0

    repo.delete_meta(analysis_id, _TENANT)
    with pytest.raises(NotFoundError):
        repo.get_meta(analysis_id, _TENANT)


def test_scores_persisted_as_decimal(repo: DynamoRepository, dynamo_table: BaseClient) -> None:
    """Los scores clásicos se persisten como ``Decimal`` a 6 decimales (criterio 6.9)."""
    from sentiment.domain.models import (
        ClassicSentiment,
        SentimentScores,
        SentimientoGeneral,
    )

    analysis_id = new_analysis_id()
    repo.create_analysis(_make_meta(analysis_id, 1), _answers(1))
    item_id = make_item_id(analysis_id, 0)

    repo.save_classic_results(
        [
            ClassicSentiment(
                item_id=item_id,
                language_code="es",
                language_confidence=Decimal("0.9999991"),
                sentimiento_general=SentimientoGeneral.POSITIVO,
                scores=SentimentScores(
                    positive=Decimal("0.1234567"),
                    negative=Decimal("0.2"),
                    neutral=Decimal("0.3"),
                    mixed=Decimal("0.4"),
                ),
            )
        ]
    )

    raw = dynamo_table.get_item(  # type: ignore[attr-defined]
        TableName=_TABLE,
        Key={"pk": {"S": f"TENANT#{_TENANT}#ANALYSIS#{analysis_id}"}, "sk": {"S": "ITEM#00000"}},
    )["Item"]
    # Redondeado a 6 decimales (ROUND_HALF_UP): 0.1234567 -> 0.123457.
    assert raw["sentiment_score_positive"]["N"] == "0.123457"
    assert raw["language_confidence"]["N"] == "0.999999"
    # Sigue PENDING tras el análisis clásico (criterio 6.10).
    assert raw["status"]["S"] == "PENDING"
