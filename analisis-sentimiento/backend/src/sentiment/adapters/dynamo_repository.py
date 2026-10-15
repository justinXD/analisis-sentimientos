"""Adaptador de persistencia DynamoDB *single-table* (``AnalysisRepository``).

Este módulo implementa los puertos :class:`~sentiment.domain.ports.AnalysisRepository`
y :class:`~sentiment.domain.ports.PurgeRepository` sobre la Dynamo_Table de tabla
única descrita en la sección 6 del diseño. Es la **fuente de verdad** del estado
de un Lote: el registro ``META`` (``sk == "META"``) guarda el estado y los
contadores atómicos, y cada ``ITEM`` (``sk == "ITEM#{index:05d}"``) guarda el
resultado por respuesta.

Modelo de claves (diseño 6.2)
-----------------------------
- ``pk  = TENANT#{tenant_id}#ANALYSIS#{analysis_id}`` — el ``tenant_id`` va en la
  clave de partición, lo que materializa el aislamiento multi-tenant tanto en el
  acceso por aplicación (``get_meta`` acota por tenant) como en una política IAM
  de *leading keys* (criterio 2.1, decisión D2).
- ``sk  = "META"`` para el registro del Lote; ``sk = "ITEM#{index:05d}"`` para los
  Ítems. El relleno a 5 dígitos preserva el orden lexicográfico por ``index``.
- **GSI1** (``gsi1pk = TENANT#{tenant_id}``, ``gsi1sk = CREATED#{created_at}#{analysis_id}``):
  listado de Lotes de un tenant, más recientes primero (patrón de acceso 4).
- **GSI2** *sparse*: el ``META`` lleva ``gsi2pk = SWEEP`` mientras el Lote no sea
  terminal **o** no tenga el Evento_Finalizacion publicado; cada ``ITEM`` lleva
  ``gsi2pk = STUCK#{analysis_id}`` mientras siga en ``PENDING``. El atributo
  ``gsi2pk`` se **elimina** en la transición terminal para mantener el índice
  disperso (patrones 7 y 8).

Decisiones de diseño que este adaptador respeta
-----------------------------------------------
- **Atomicidad contador ↔ transición (criterios 8.3, 8.4, diseño 6.3 patrón 6).**
  :meth:`DynamoRepository.complete_item` y :meth:`DynamoRepository.fail_item`
  usan ``TransactWriteItems`` con dos operaciones en una sola transacción: el
  ``Update`` del ITEM condicionado a ``status == PENDING``, y el ``ADD`` al
  contador del META. O se aplican ambas o ninguna; el contador nunca diverge del
  número real de Ítems en cada estado.
- **Idempotencia (criterios 8.4, 8.5, 8.10).** Si la condición
  ``status == PENDING`` falla porque el ITEM ya era terminal, DynamoDB lanza
  ``TransactionCanceledException`` con razón ``ConditionalCheckFailed``. Ese caso
  se trata como **éxito idempotente**: no se reintenta, no se incrementa el
  contador y se devuelve ``False`` sin propagar error (base de P2 y P11).
  Cualquier otra cancelación (p. ej. ``TransactionConflict``) **sí** se propaga,
  para que la Maquina_De_Estados reintente (criterio 8.10 vs. 8.5).
- **Escritura por lotes de 25 (criterios 4.3, 4.4).**
  :meth:`DynamoRepository.create_analysis` escribe el META y los N ITEM con
  ``BatchWriteItem`` en tandas de 25, reintentando los ``UnprocessedItems`` que
  DynamoDB pueda devolver con un backoff exponencial acotado.
- **Scores como ``Decimal`` (criterio 6.9).** DynamoDB no admite ``float``; los
  scores se persisten como ``Decimal`` redondeados a 6 decimales.
- **TTL de 90 días (criterio 16.1).** El atributo ``ttl`` se fija a
  ``created_at + ttl_days()`` (configurable vía Parameter_Store/entorno).
- **Paginación, no carga total (sección backend).** :meth:`iter_items`,
  :meth:`list_by_tenant` y :meth:`list_sweep_candidates` paginan la consulta y
  nunca cargan el Lote completo en memoria.

Importabilidad (criterio 17.7)
------------------------------
Importar este módulo no lee ``TABLE_NAME`` ni ``TTL_DAYS`` ni llama a AWS: el
nombre de tabla y el TTL se resuelven de forma **perezosa** (en el constructor y
en cada escritura), de modo que ``pkgutil.walk_packages`` puede importar el
módulo sin configuración en el entorno. El cliente de DynamoDB se obtiene del
singleton de :mod:`sentiment.adapters.aws_clients`.

Sin PII en logs (criterio 15.1)
-------------------------------
Este adaptador no registra el contenido de ``respuesta`` ni de ``explicacion``.
Los logs se limitan a identificadores y recuentos; el texto sensible viaja a
DynamoDB (cifrado en reposo con la CMK) pero nunca a CloudWatch Logs.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from botocore.exceptions import ClientError

from sentiment.adapters.aws_clients import dynamodb_client
from sentiment.domain.errors import NotFoundError, SentimentError
from sentiment.domain.ids import make_item_id
from sentiment.domain.models import (
    TERMINAL_ANALYSIS_STATUSES,
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ClassicSentiment,
    EmotionAnalysis,
    ItemStatus,
    SentimentScores,
    SentimientoGeneral,
    SurveyAnswer,
)
from sentiment.observability import config
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from botocore.client import BaseClient

__all__ = ["DynamoRepository"]

logger = get_logger(service="dynamo_repository")


class _DynamoDBClient(Protocol):
    """Interfaz mínima del cliente de DynamoDB que este repositorio consume.

    El meta-paquete ``boto3-stubs`` se instala sin el *extra* de DynamoDB, por lo
    que :class:`botocore.client.BaseClient` no expone las operaciones del servicio
    y ``mypy --strict`` no las resolvería. En lugar de añadir una dependencia nueva
    de *stubs*, se declara aquí el subconjunto exacto de operaciones usadas
    (segregación de interfaces): un ``Protocol`` estructural que el cliente real de
    boto3 satisface en tiempo de ejecución y que tipa correctamente las llamadas.
    Las firmas usan ``Any`` en los parámetros variádicos porque reflejan la API de
    boto3 (diccionarios de bajo nivel), no un contrato propio que debamos estrechar.
    """

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def put_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def query(self, **kwargs: Any) -> dict[str, Any]: ...
    def batch_write_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]: ...

# ---------------------------------------------------------------------------
# Nombres de atributos y constantes del esquema (deben coincidir con el
# data_stack de la tarea 11.2). Centralizados aquí para no repetir literales.
# ---------------------------------------------------------------------------
_PK: Final = "pk"
_SK: Final = "sk"
_GSI1_PK: Final = "gsi1pk"
_GSI1_SK: Final = "gsi1sk"
_GSI2_PK: Final = "gsi2pk"
_GSI2_SK: Final = "gsi2sk"
_GSI1_NAME: Final = "GSI1"
_GSI2_NAME: Final = "GSI2"

_SK_META: Final = "META"
_SK_ITEM_PREFIX: Final = "ITEM#"

_ENTITY_META: Final = "AnalysisMeta"
_ENTITY_ITEM: Final = "AnalysisItem"

#: Marcador del META en el índice disperso del sweeper (patrón de acceso 8).
_SWEEP_MARKER: Final = "SWEEP"

#: Nombre de la variable de entorno que inyecta la Infraestructura con el nombre
#: de la Dynamo_Table (``sentiment-{env}-analysis``). Se lee de forma perezosa.
_ENV_TABLE_NAME: Final = "TABLE_NAME"

#: Tamaño máximo de una tanda de ``BatchWriteItem`` (límite duro de DynamoDB).
_BATCH_SIZE: Final = 25

#: Reintentos del backoff para los ``UnprocessedItems`` de ``BatchWriteItem``.
#: Es un bucle acotado y local a la escritura por lotes; no es el reintento de la
#: Maquina_De_Estados, que opera a nivel de tarea.
_MAX_BATCH_RETRIES: Final = 5
_BATCH_BACKOFF_BASE_SECONDS: Final = 0.05

#: Precisión de persistencia de los scores de Comprehend (criterio 6.9).
_SCORE_QUANT: Final = Decimal("0.000001")

#: Nombre de la razón de cancelación que DynamoDB devuelve cuando una
#: ``ConditionExpression`` de una operación de ``TransactWriteItems`` no se cumple.
_COND_CHECK_FAILED: Final = "ConditionalCheckFailed"

#: Longitud máxima de ``explicacion`` persistida (criterio 7.8).
_EXPLICACION_MAX: Final = 1000


def _utcnow() -> datetime:
    """Instante actual en UTC. Encapsulado para poder sustituirlo en pruebas."""
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    """Serializa un ``datetime`` a ISO 8601 en UTC (criterio 10.16)."""
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    """Deserializa un instante ISO 8601 a ``datetime`` consciente de zona (UTC).

    Es total frente a entradas razonables del propio Sistema: toda marca de tiempo
    que este adaptador escribe pasa por :func:`_iso`. Un valor sin zona se
    interpreta como UTC.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class DynamoRepository:
    """Repositorio *single-table* de Lotes e Ítems sobre DynamoDB.

    Implementa estructuralmente :class:`~sentiment.domain.ports.AnalysisRepository`
    y :class:`~sentiment.domain.ports.PurgeRepository`. El cliente de boto3 y el
    nombre de tabla se inyectan opcionalmente para las pruebas; en runtime se
    resuelven del singleton de clientes y de la variable de entorno ``TABLE_NAME``.

    El acceso principal "dame el META de un Lote" es un ``GetItem`` por ``pk``/``sk``;
    "dame todos los Ítems" es una ``Query`` por ``pk`` paginada; "lista los Lotes de
    un tenant" es una ``Query`` sobre GSI1. Las transiciones de Ítem son
    transaccionales e idempotentes (``TransactWriteItems`` con
    ``ConditionExpression``).
    """

    def __init__(
        self, client: BaseClient | None = None, table_name: str | None = None
    ) -> None:
        """Construye el repositorio resolviendo cliente y nombre de tabla.

        :param client: cliente de DynamoDB a usar; por defecto, el singleton de
            :func:`sentiment.adapters.aws_clients.dynamodb_client`.
        :param table_name: nombre de la Dynamo_Table; por defecto se lee de la
            variable de entorno ``TABLE_NAME``. La resolución es perezosa (ocurre
            aquí, en el constructor, no al importar el módulo), por lo que importar
            el módulo nunca exige que ``TABLE_NAME`` esté definida.
        :raises SentimentError: si no se puede resolver un nombre de tabla.
        """
        raw_client = client if client is not None else dynamodb_client()
        # El cliente de boto3 satisface estructuralmente `_DynamoDBClient`; el
        # `cast` da a mypy el tipo con las operaciones de DynamoDB sin requerir el
        # *extra* de stubs. No realiza conversión alguna en tiempo de ejecución.
        self._client: _DynamoDBClient = cast("_DynamoDBClient", raw_client)
        self._table_name: str = table_name or _resolve_table_name()
        # Memo de tenant por analysis_id. La `pk` requiere el `tenant_id`, pero los
        # puertos pasan solo el `analysis_id`; resolverlo en cada transición sería
        # un patrón N+1 (una query extra por Ítem). Como el repositorio vive lo que
        # vive el contenedor de la Lambda y una invocación opera sobre un único
        # Lote, memoizar el tenant por `analysis_id` elimina esas lecturas
        # repetidas sin introducir estado global compartido entre invocaciones.
        self._tenant_cache: dict[str, str] = {}

    # -- Claves ------------------------------------------------------------

    @staticmethod
    def _pk(tenant_id: str, analysis_id: str) -> str:
        """Construye la clave de partición ``TENANT#{tenant_id}#ANALYSIS#{analysis_id}``."""
        return f"TENANT#{tenant_id}#ANALYSIS#{analysis_id}"

    @staticmethod
    def _item_sk(index: int) -> str:
        """Construye la *sort key* ``ITEM#{index:05d}`` de un Ítem."""
        return f"{_SK_ITEM_PREFIX}{index:05d}"

    @staticmethod
    def _ttl_epoch(created_at: datetime) -> int:
        """Calcula el ``ttl`` como epoch de ``created_at + ttl_days()`` (criterio 16.1).

        El número de días de retención se lee de forma perezosa de
        :func:`sentiment.observability.config.ttl_days` (entorno/Parameter_Store),
        con 90 días por defecto.
        """
        return int((created_at + timedelta(days=config.ttl_days())).timestamp())

    # -- Serialización de valores DynamoDB (formato de bajo nivel) ---------

    @staticmethod
    def _score(value: Decimal) -> str:
        """Normaliza un score a 6 decimales como cadena para el tipo ``N`` (criterio 6.9)."""
        quantized = value.quantize(_SCORE_QUANT, rounding=ROUND_HALF_UP)
        return format(quantized, "f")

    def _meta_item(self, meta: AnalysisMeta) -> dict[str, Any]:
        """Serializa un :class:`AnalysisMeta` al formato de ``put_item`` de DynamoDB.

        Incluye ``gsi2pk = SWEEP`` porque un META recién creado no es terminal:
        entra en el índice del sweeper hasta que se cierre y publique su evento.
        """
        pk = self._pk(meta.tenant_id, meta.analysis_id)
        item: dict[str, Any] = {
            _PK: {"S": pk},
            _SK: {"S": _SK_META},
            "entity_type": {"S": _ENTITY_META},
            "analysis_id": {"S": meta.analysis_id},
            "tenant_id": {"S": meta.tenant_id},
            "owner_sub": {"S": meta.owner_sub},
            "status": {"S": str(meta.status)},
            "item_count": {"N": str(meta.item_count)},
            "done_count": {"N": str(meta.done_count)},
            "failed_count": {"N": str(meta.failed_count)},
            "skipped_count": {"N": str(meta.skipped_count)},
            "input_s3_key": {"S": meta.input_s3_key},
            "created_at": {"S": _iso(meta.created_at)},
            "updated_at": {"S": _iso(meta.updated_at)},
            "schema_version": {"N": str(meta.schema_version)},
            "ttl": {"N": str(self._ttl_epoch(meta.created_at))},
            _GSI1_PK: {"S": f"TENANT#{meta.tenant_id}"},
            _GSI1_SK: {"S": f"CREATED#{_iso(meta.created_at)}#{meta.analysis_id}"},
            _GSI2_PK: {"S": _SWEEP_MARKER},
            _GSI2_SK: {"S": f"UPDATED#{_iso(meta.updated_at)}#{meta.analysis_id}"},
        }
        if meta.output_s3_key is not None:
            item["output_s3_key"] = {"S": meta.output_s3_key}
        if meta.execution_arn is not None:
            item["execution_arn"] = {"S": meta.execution_arn}
        if meta.completed_at is not None:
            item["completed_at"] = {"S": _iso(meta.completed_at)}
        return item

    def _seed_item(
        self, meta: AnalysisMeta, answer: SurveyAnswer
    ) -> dict[str, Any]:
        """Serializa un ITEM recién sembrado en ``PENDING`` para ``create_analysis``.

        Un ITEM ``PENDING`` lleva ``gsi2pk = STUCK#{analysis_id}`` (patrón de
        acceso 7): el índice disperso solo contiene Ítems no terminales.
        """
        item_id = make_item_id(meta.analysis_id, answer.index)
        return {
            _PK: {"S": self._pk(meta.tenant_id, meta.analysis_id)},
            _SK: {"S": self._item_sk(answer.index)},
            "entity_type": {"S": _ENTITY_ITEM},
            "item_id": {"S": item_id},
            "analysis_id": {"S": meta.analysis_id},
            "tenant_id": {"S": meta.tenant_id},
            "index": {"N": str(answer.index)},
            "indificador": {"S": answer.indificador},
            "respuesta": {"S": answer.respuesta},
            "status": {"S": str(ItemStatus.PENDING)},
            "attempt_count": {"N": "0"},
            "ttl": {"N": str(self._ttl_epoch(meta.created_at))},
            _GSI2_PK: {"S": f"STUCK#{meta.analysis_id}"},
            _GSI2_SK: {"S": f"CREATED#{_iso(meta.created_at)}"},
        }

    # -- create_analysis ---------------------------------------------------

    def create_analysis(
        self, meta: AnalysisMeta, answers: list[SurveyAnswer]
    ) -> None:
        """Crea el registro META y los N ITEM del Lote con ``BatchWriteItem``.

        Escribe el META y un ITEM ``PENDING`` por cada :class:`SurveyAnswer`, en
        tandas de 25 (criterios 4.3, 4.4). Si DynamoDB devuelve ``UnprocessedItems``
        (p. ej. por *throttling* momentáneo), reintenta solo esos elementos con un
        backoff exponencial acotado; si tras agotar el backoff siguen quedando sin
        procesar, propaga el error para que el servicio llamante cierre el META
        como ``FAILED`` (criterio 4.11).

        Postcondición: tras retornar sin error, existe un META ``PENDING`` con
        ``item_count == len(answers)`` y exactamente ``len(answers)`` ITEM
        ``PENDING`` con ``index`` recorriendo ``range(len(answers))`` sin huecos.

        :param meta: el META del Lote a sembrar (``status`` debe ser ``PENDING``).
        :param answers: las respuestas ya validadas y normalizadas, en orden.
        :raises SentimentError: si alguna tanda no logra escribirse por completo.
        """
        requests: list[dict[str, Any]] = [
            {"PutRequest": {"Item": self._meta_item(meta)}}
        ]
        requests.extend(
            {"PutRequest": {"Item": self._seed_item(meta, answer)}}
            for answer in answers
        )

        for start in range(0, len(requests), _BATCH_SIZE):
            batch = requests[start : start + _BATCH_SIZE]
            self._write_batch(batch)

        logger.info(
            "analysis_seeded",
            extra={
                "analysis_id": meta.analysis_id,
                "tenant_id": meta.tenant_id,
                "item_count": meta.item_count,
            },
        )

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        """Escribe una tanda de hasta 25 ``PutRequest`` reintentando los no procesados.

        :param batch: lista de ``WriteRequest`` para un único ``BatchWriteItem``.
        :raises SentimentError: si quedan elementos sin procesar tras el backoff.
        """
        pending: dict[str, list[dict[str, Any]]] = {self._table_name: batch}
        for attempt in range(_MAX_BATCH_RETRIES + 1):
            response = self._client.batch_write_item(RequestItems=pending)
            unprocessed = response.get("UnprocessedItems") or {}
            if not unprocessed.get(self._table_name):
                return
            pending = unprocessed
            if attempt < _MAX_BATCH_RETRIES:
                time.sleep(_BATCH_BACKOFF_BASE_SECONDS * (2**attempt))

        remaining = len(pending.get(self._table_name, []))
        raise SentimentError(
            "BatchWriteItem dejó elementos sin procesar tras agotar el backoff.",
            unprocessed_count=remaining,
        )

    # -- Lecturas ----------------------------------------------------------

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        """Lee el META de un Lote acotado por ``tenant_id`` (aislamiento multi-tenant).

        Como el ``tenant_id`` forma parte de la ``pk``, un ``analysis_id`` ajeno
        produce simplemente una lectura vacía: nunca se devuelve el META de otro
        tenant. La ausencia se traduce a :class:`NotFoundError`, que el llamante
        convierte en un 404 idéntico al de "no existe" (criterios 2.2, 2.3).

        :raises NotFoundError: si no existe un META para ese ``(tenant_id, analysis_id)``.
        """
        response = self._client.get_item(
            TableName=self._table_name,
            Key={
                _PK: {"S": self._pk(tenant_id, analysis_id)},
                _SK: {"S": _SK_META},
            },
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if not raw:
            raise NotFoundError(analysis_id=analysis_id)
        return self._deserialize_meta(raw)

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:
        """Lista los Lotes de un tenant por GSI1, más recientes primero, paginado.

        Usa ``ScanIndexForward=False`` para devolver los Lotes en orden descendente
        de ``created_at`` (el ``gsi1sk`` empieza por ``CREATED#{created_at}``). La
        paginación se expone como un cursor opaco: la codificación interna de
        ``LastEvaluatedKey``, que el llamante trata como una cadena sin estructura.

        :param tenant_id: tenant propietario; frontera de aislamiento.
        :param limit: tope de elementos por página (``Limit`` del ``Query``).
        :param cursor: cursor opaco de la página previa, o ``None`` para la primera.
        :return: ``(metas, next_cursor)``; ``next_cursor is None`` si no hay más.
        """
        kwargs: dict[str, Any] = {
            "TableName": self._table_name,
            "IndexName": _GSI1_NAME,
            "KeyConditionExpression": "#gpk = :tenant",
            "ExpressionAttributeNames": {"#gpk": _GSI1_PK},
            "ExpressionAttributeValues": {":tenant": {"S": f"TENANT#{tenant_id}"}},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        start_key = _decode_cursor(cursor)
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key

        response = self._client.query(**kwargs)
        metas = [self._deserialize_meta(raw) for raw in response.get("Items", [])]
        next_cursor = _encode_cursor(response.get("LastEvaluatedKey"))
        return metas, next_cursor

    def get_item(self, analysis_id: str, item_id: str) -> AnalysisItem:
        """Lee un único ITEM de un Lote por su ``item_id`` con lectura fuertemente consistente.

        Implementa :meth:`sentiment.domain.ports.AnalysisRepository.get_item`. Es
        la relectura que ``analyze_item_emotions`` ejecuta **antes** de invocar al
        Modelo_Bedrock (criterio 7.2): aporta el ``indificador`` y la ``respuesta``
        que se envían al modelo y el ``status`` sobre el que se decide el guard de
        idempotencia. La lectura usa ``ConsistentRead=True`` para que un reintento
        observe el estado terminal que escribió un intento anterior y no vuelva a
        invocar al modelo (base del guard de P2).

        El ``item_id`` es ``{analysis_id}#{index:05d}``; su índice es el último
        segmento tras ``#`` y determina la ``sk = ITEM#{index:05d}``. El
        ``tenant_id`` —que la ``pk`` requiere y el ``item_id`` no transporta— se
        resuelve del índice de ítems del Lote (:meth:`_tenant_for_analysis`), igual
        que en las transiciones de Ítem; es una lectura pura que no muta nada.

        :param analysis_id: ULID del Lote al que pertenece el ITEM.
        :param item_id: ``{analysis_id}#{index:05d}`` del ITEM a leer.
        :return: el :class:`AnalysisItem` persistido, con su ``status`` y, al
            menos, ``indificador`` y ``respuesta`` poblados.
        :raises NotFoundError: si no existe un ITEM para ese
            ``(analysis_id, item_id)``. Un fallo de lectura de la dependencia se
            propaga tal cual; en ambos casos el servicio llamante no invoca al
            Modelo_Bedrock ni escribe ningún campo (criterio 7.14).
        """
        response = self._client.get_item(
            TableName=self._table_name,
            Key=self._item_key(analysis_id, item_id),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if not raw:
            raise NotFoundError(analysis_id=analysis_id)
        return self._deserialize_item(raw)

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        """Itera TODOS los ITEM de un Lote paginando internamente (no carga en memoria).

        Pagina la ``Query`` por ``pk`` con ``sk begins_with ITEM#`` siguiendo el
        ``LastEvaluatedKey``, de modo que un Lote de hasta 1.000 Ítems se recorre
        sin cargarlo entero. Para localizar la ``pk`` sin el ``tenant_id`` lee
        primero el ``tenant_id`` del ITEM a través de un atributo propio; como el
        ``analysis_id`` no basta para formar la ``pk``, se resuelve el tenant a
        partir del propio recorrido del índice de ítems del Lote.

        :param analysis_id: ULID del Lote cuyos Ítems se iteran.
        :return: generador de :class:`AnalysisItem` del Lote.
        """
        start_key: dict[str, Any] | None = None
        tenant_id = self._tenant_for_analysis(analysis_id)
        pk = self._pk(tenant_id, analysis_id)
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
                "ExpressionAttributeNames": {"#pk": _PK, "#sk": _SK},
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk},
                    ":prefix": {"S": _SK_ITEM_PREFIX},
                },
                "ConsistentRead": True,
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._client.query(**kwargs)
            for raw in response.get("Items", []):
                yield self._deserialize_item(raw)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return

    # -- Transiciones idempotentes de Ítem (TransactWriteItems) ------------

    def complete_item(
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:
        """Pasa un ITEM a ``DONE`` e incrementa ``done_count`` atómica e idempotentemente.

        Ejecuta un ``TransactWriteItems`` con dos operaciones indivisibles
        (criterio 8.4): (1) ``Update`` del ITEM condicionado a ``status == PENDING``,
        que escribe ``sentimientos``, ``explicacion`` (truncada a 1.000),
        ``model_id`` y ``processed_at``, pasa el ``status`` a ``DONE`` y **elimina**
        ``gsi2pk`` para sacarlo del índice disperso; (2) ``ADD done_count :1`` sobre
        el META, que además refresca ``updated_at`` y ``gsi2sk``.

        :param analysis_id: ULID del Lote al que pertenece el ITEM.
        :param item_id: ``{analysis_id}#{index:05d}`` del ITEM a cerrar.
        :param emotions: resultado del análisis profundo ya normalizado.
        :return: ``True`` si aplicó la transición (estaba ``PENDING``); ``False`` si
            el ITEM ya era terminal (reproceso idempotente, criterio 8.5).
        :raises SentimentError: ante cualquier fallo de la transacción distinto del
            incumplimiento de la condición (criterio 8.10).
        """
        now = _utcnow()
        explicacion = emotions.explicacion[:_EXPLICACION_MAX]
        set_expr = (
            "#st = :done, sentimientos = :sent, explicacion = :exp, "
            "model_id = :model, processed_at = :pat, "
            "input_tokens = :itok, output_tokens = :otok"
        )
        item_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": self._item_key(analysis_id, item_id),
                "UpdateExpression": f"SET {set_expr} REMOVE {_GSI2_PK}, {_GSI2_SK}",
                "ConditionExpression": "#st = :pending",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":done": {"S": str(ItemStatus.DONE)},
                    ":pending": {"S": str(ItemStatus.PENDING)},
                    ":sent": _string_list(emotions.sentimientos),
                    ":exp": {"S": explicacion},
                    ":model": {"S": emotions.model_id},
                    ":pat": {"S": _iso(now)},
                    ":itok": {"N": str(emotions.input_tokens)},
                    ":otok": {"N": str(emotions.output_tokens)},
                },
            }
        }
        return self._transact_item_transition(
            analysis_id, item_id, item_update, counter="done_count", now=now
        )

    def fail_item(self, analysis_id: str, item_id: str, error_code: str) -> bool:
        """Pasa un ITEM a ``FAILED`` con ``error_code`` e incrementa ``failed_count``.

        Mismo contrato transaccional e idempotente que :meth:`complete_item`
        (criterios 8.4, 8.5, 8.10): condiciona la transición a ``status == PENDING``,
        registra ``error_code`` y ``processed_at``, elimina ``gsi2pk`` del ITEM y
        hace ``ADD failed_count :1`` sobre el META en la misma transacción.

        :param error_code: código del Catalogo_De_Errores a registrar en el ITEM.
        :return: ``True`` si aplicó la transición; ``False`` si ya era terminal.
        """
        now = _utcnow()
        item_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": self._item_key(analysis_id, item_id),
                "UpdateExpression": (
                    "SET #st = :failed, error_code = :ec, processed_at = :pat "
                    f"REMOVE {_GSI2_PK}, {_GSI2_SK}"
                ),
                "ConditionExpression": "#st = :pending",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":failed": {"S": str(ItemStatus.FAILED)},
                    ":pending": {"S": str(ItemStatus.PENDING)},
                    ":ec": {"S": error_code},
                    ":pat": {"S": _iso(now)},
                },
            }
        }
        return self._transact_item_transition(
            analysis_id, item_id, item_update, counter="failed_count", now=now
        )

    def skip_item(self, analysis_id: str, item_id: str) -> bool:
        """Pasa un ITEM a ``SKIPPED`` e incrementa ``skipped_count`` atómica e idempotentemente.

        Mismo contrato transaccional e idempotente que :meth:`complete_item` y
        :meth:`fail_item` (criterios 8.4, 8.5, 8.10): condiciona la transición a
        ``status == PENDING``, registra ``processed_at``, elimina ``gsi2pk`` del
        ITEM para sacarlo del índice disperso y hace ``ADD skipped_count :1`` sobre
        el META en la misma transacción. No registra ``error_code``: un Ítem
        omitido por ser un Marcador_Vacio no es un fallo (criterios 6.6, 18.6).

        :param analysis_id: ULID del Lote al que pertenece el ITEM.
        :param item_id: ``{analysis_id}#{index:05d}`` del ITEM a omitir.
        :return: ``True`` si aplicó la transición; ``False`` si ya era terminal.
        """
        now = _utcnow()
        item_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": self._item_key(analysis_id, item_id),
                "UpdateExpression": (
                    "SET #st = :skipped, processed_at = :pat "
                    f"REMOVE {_GSI2_PK}, {_GSI2_SK}"
                ),
                "ConditionExpression": "#st = :pending",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":skipped": {"S": str(ItemStatus.SKIPPED)},
                    ":pending": {"S": str(ItemStatus.PENDING)},
                    ":pat": {"S": _iso(now)},
                },
            }
        }
        return self._transact_item_transition(
            analysis_id, item_id, item_update, counter="skipped_count", now=now
        )

    def _item_key(self, analysis_id: str, item_id: str) -> dict[str, Any]:
        """Construye la clave primaria de un ITEM a partir de su ``item_id``.

        El ``item_id`` es ``{analysis_id}#{index:05d}``; el índice es su último
        segmento. El ``tenant_id`` se resuelve del índice de ítems del Lote, pues
        la ``pk`` lo requiere y el ``item_id`` no lo transporta.
        """
        index_str = item_id.rsplit("#", 1)[-1]
        tenant_id = self._tenant_for_analysis(analysis_id)
        return {
            _PK: {"S": self._pk(tenant_id, analysis_id)},
            _SK: {"S": f"{_SK_ITEM_PREFIX}{index_str}"},
        }

    def _transact_item_transition(
        self,
        analysis_id: str,
        item_id: str,
        item_update: dict[str, Any],
        *,
        counter: str,
        now: datetime,
    ) -> bool:
        """Ejecuta la transacción ITEM→terminal + ``ADD`` al contador del META.

        Trata la ``ConditionalCheckFailed`` del ITEM como éxito idempotente
        (devuelve ``False``, criterio 8.5) y propaga cualquier otra cancelación o
        error (criterio 8.10). El ``Update`` del META refresca ``updated_at`` y
        ``gsi2sk`` para que ``updated_at`` sea una señal fiable para el sweeper.

        :param counter: nombre del contador del META a incrementar
            (``done_count`` o ``failed_count``).
        :return: ``True`` si la transacción se aplicó; ``False`` si el ITEM ya era
            terminal.
        """
        tenant_id = self._tenant_for_analysis(analysis_id)
        pk = self._pk(tenant_id, analysis_id)
        meta_update = {
            "Update": {
                "TableName": self._table_name,
                "Key": {_PK: {"S": pk}, _SK: {"S": _SK_META}},
                "UpdateExpression": (
                    f"ADD {counter} :one SET updated_at = :now, "
                    f"{_GSI2_SK} = :gsi2sk"
                ),
                "ExpressionAttributeValues": {
                    ":one": {"N": "1"},
                    ":now": {"S": _iso(now)},
                    ":gsi2sk": {"S": f"UPDATED#{_iso(now)}#{analysis_id}"},
                },
            }
        }
        try:
            self._client.transact_write_items(
                TransactItems=[item_update, meta_update]
            )
        except ClientError as exc:
            if self._is_idempotent_item_conflict(exc):
                logger.info(
                    "item_transition_idempotent",
                    extra={"analysis_id": analysis_id, "item_id": item_id},
                )
                return False
            raise
        return True

    @staticmethod
    def _is_idempotent_item_conflict(exc: ClientError) -> bool:
        """Determina si la cancelación corresponde a un ITEM ya terminal (idempotente).

        DynamoDB cancela un ``TransactWriteItems`` con
        ``TransactionCanceledException`` y una lista ``CancellationReasons``
        posicional. La transición es idempotente **solo** si la razón de la
        **primera** operación (el ``Update`` del ITEM, condicionada a ``PENDING``)
        es ``ConditionalCheckFailed``. Cualquier otra razón (``TransactionConflict``,
        error de capacidad, etc.) **no** es idempotente y debe propagarse para que
        la Maquina_De_Estados reintente (criterio 8.10 frente a 8.5).

        :param exc: la excepción de boto3 a clasificar.
        :return: ``True`` si es el conflicto idempotente esperado; ``False`` en caso
            contrario.
        """
        error = exc.response.get("Error", {})
        if error.get("Code") != "TransactionCanceledException":
            return False
        reasons = exc.response.get("CancellationReasons")
        if not reasons:
            return False
        item_reason = reasons[0]
        return bool(item_reason.get("Code") == _COND_CHECK_FAILED)

    # -- save_classic_results (Comprehend) ---------------------------------

    def save_classic_results(self, results: list[ClassicSentiment]) -> None:
        """Persiste los resultados del análisis clásico (Comprehend) de varios Ítems.

        Mantiene el ITEM en ``PENDING`` (criterio 6.10: el análisis profundo es
        posterior) y escribe ``language_code``, ``sentimiento_general`` y los cuatro
        scores como ``Decimal`` a 6 decimales (criterios 6.8, 6.9). Cada resultado
        se asocia a su ITEM por el ``item_id`` que ya trae embebido.

        :param results: resultados clásicos a persistir; cada uno con su ``item_id``.
        """
        for result in results:
            analysis_id, index_str = result.item_id.rsplit("#", 1)
            tenant_id = self._tenant_for_analysis(analysis_id)
            self._client.update_item(
                TableName=self._table_name,
                Key={
                    _PK: {"S": self._pk(tenant_id, analysis_id)},
                    _SK: {"S": f"{_SK_ITEM_PREFIX}{index_str}"},
                },
                UpdateExpression=(
                    "SET language_code = :lang, language_confidence = :lconf, "
                    "sentimiento_general = :sg, sentiment_score_positive = :sp, "
                    "sentiment_score_negative = :sn, sentiment_score_neutral = :snt, "
                    "sentiment_score_mixed = :sm"
                ),
                ExpressionAttributeValues={
                    ":lang": {"S": result.language_code},
                    ":lconf": {"N": self._score(result.language_confidence)},
                    ":sg": {"S": str(result.sentimiento_general)},
                    ":sp": {"N": self._score(result.scores.positive)},
                    ":sn": {"N": self._score(result.scores.negative)},
                    ":snt": {"N": self._score(result.scores.neutral)},
                    ":sm": {"N": self._score(result.scores.mixed)},
                },
            )

    # -- Cierre del META ---------------------------------------------------

    def finalize_analysis(
        self,
        analysis_id: str,
        status: AnalysisStatus,
        counts: tuple[int, int, int],
        output_s3_key: str | None,
    ) -> None:
        """Cierra el META en un estado terminal con los recuentos reales y la salida.

        Fija ``done_count``/``failed_count``/``skipped_count`` al recuento contado
        desde los ITEM reales (criterio 10.4; prevalece sobre el acumulado, 8.11),
        el ``status`` terminal, ``completed_at`` y ``output_s3_key`` **antes** de que
        se publique el Evento_Finalizacion (criterio 10.16). No retira el META del
        índice SWEEP: eso lo hace :meth:`mark_event_published` tras publicar el
        evento, de modo que un cierre sin evento siga siendo visible para el sweeper.

        :param status: estado terminal del Lote.
        :param counts: ``(done_count, failed_count, skipped_count)`` reales.
        :param output_s3_key: clave del Documento_Consolidado, o ``None``.
        """
        done, failed, skipped = counts
        now = _utcnow()
        tenant_id = self._tenant_for_analysis(analysis_id)
        set_clauses = [
            "#st = :status",
            "done_count = :done",
            "failed_count = :failed",
            "skipped_count = :skipped",
            "completed_at = :now",
            "updated_at = :now",
            f"{_GSI2_SK} = :gsi2sk",
        ]
        values: dict[str, Any] = {
            ":status": {"S": str(status)},
            ":done": {"N": str(done)},
            ":failed": {"N": str(failed)},
            ":skipped": {"N": str(skipped)},
            ":now": {"S": _iso(now)},
            ":gsi2sk": {"S": f"UPDATED#{_iso(now)}#{analysis_id}"},
        }
        if output_s3_key is not None:
            set_clauses.append("output_s3_key = :out")
            values[":out"] = {"S": output_s3_key}
        self._client.update_item(
            TableName=self._table_name,
            Key={_PK: {"S": self._pk(tenant_id, analysis_id)}, _SK: {"S": _SK_META}},
            UpdateExpression="SET " + ", ".join(set_clauses),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=values,
        )

    def mark_analysis_failed(self, analysis_id: str, error_code: str) -> None:
        """Marca el META como ``FAILED`` con ``error_code``, solo si no es ya terminal.

        La transición es idempotente: la ``ConditionExpression`` exige que el
        ``status`` no esté ya en un estado terminal, de modo que no sobrescribe un
        cierre previo (criterio 4.11). Si el META ya es terminal, la condición falla
        y la operación se absorbe silenciosamente.

        :param error_code: código del Catalogo_De_Errores a registrar.
        """
        now = _utcnow()
        tenant_id = self._tenant_for_analysis(analysis_id)
        terminal_values = {
            f":t{i}": {"S": str(s)} for i, s in enumerate(TERMINAL_ANALYSIS_STATUSES)
        }
        not_terminal = "NOT (#st IN (" + ", ".join(terminal_values) + "))"
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={
                    _PK: {"S": self._pk(tenant_id, analysis_id)},
                    _SK: {"S": _SK_META},
                },
                UpdateExpression=(
                    "SET #st = :failed, error_code = :ec, updated_at = :now, "
                    f"{_GSI2_SK} = :gsi2sk"
                ),
                ConditionExpression=not_terminal,
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":failed": {"S": str(AnalysisStatus.FAILED)},
                    ":ec": {"S": error_code},
                    ":now": {"S": _iso(now)},
                    ":gsi2sk": {"S": f"UPDATED#{_iso(now)}#{analysis_id}"},
                    **terminal_values,
                },
            )
        except ClientError as exc:
            if _is_conditional_check_failed(exc):
                return
            raise

    def mark_event_published(self, analysis_id: str) -> None:
        """Fija ``event_published_at`` y retira el META del índice SWEEP (patrón 8).

        Elimina ``gsi2pk``/``gsi2sk`` del META: una vez publicado el
        Evento_Finalizacion de un Lote terminal, deja de ser candidato del sweeper.
        """
        now = _utcnow()
        tenant_id = self._tenant_for_analysis(analysis_id)
        self._client.update_item(
            TableName=self._table_name,
            Key={_PK: {"S": self._pk(tenant_id, analysis_id)}, _SK: {"S": _SK_META}},
            UpdateExpression=(
                f"SET event_published_at = :now, updated_at = :now "
                f"REMOVE {_GSI2_PK}, {_GSI2_SK}"
            ),
            ExpressionAttributeValues={":now": {"S": _iso(now)}},
        )

    def list_sweep_candidates(self, older_than: datetime) -> Iterator[AnalysisMeta]:
        """Itera los candidatos a barrido vía GSI2 (``SWEEP``), paginando.

        Consulta el índice disperso con ``gsi2pk == SWEEP`` y
        ``gsi2sk < UPDATED#{older_than}``, devolviendo META de Lotes no terminales
        o terminales sin evento publicado y suficientemente antiguos. Como GSI2 es
        ``KEYS_ONLY``, se relee cada META completo por su ``pk`` (así el índice no
        replica la PII de ``respuesta``).

        :param older_than: umbral; solo se devuelven META con ``gsi2sk`` anterior.
        :return: generador de :class:`AnalysisMeta` candidatos.
        """
        boundary = f"UPDATED#{_iso(older_than)}"
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "IndexName": _GSI2_NAME,
                "KeyConditionExpression": "#gpk = :sweep AND #gsk < :boundary",
                "ExpressionAttributeNames": {"#gpk": _GSI2_PK, "#gsk": _GSI2_SK},
                "ExpressionAttributeValues": {
                    ":sweep": {"S": _SWEEP_MARKER},
                    ":boundary": {"S": boundary},
                },
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._client.query(**kwargs)
            for raw_key in response.get("Items", []):
                meta_raw = self._client.get_item(
                    TableName=self._table_name,
                    Key={_PK: raw_key[_PK], _SK: {"S": _SK_META}},
                    ConsistentRead=True,
                ).get("Item")
                if meta_raw:
                    yield self._deserialize_meta(meta_raw)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return

    def _tenant_for_analysis(self, analysis_id: str) -> str:
        """Resuelve (y memoiza) el ``tenant_id`` de un Lote a partir del ``analysis_id``.

        La ``pk`` incluye el ``tenant_id`` por diseño (D2), pero los puertos pasan
        solo el ``analysis_id``. Como no existe un patrón de acceso "busca por
        ``analysis_id`` sin tenant", el tenant se descubre de los índices dispersos
        de ese Lote, que **sí** contienen la ``pk`` completa:

        1. **GSI2 ``SWEEP``** (META no terminal o sin evento publicado): cubre todo
           el ciclo de vida del Lote hasta que se publica su Evento_Finalizacion,
           que es cuando operan las transiciones de Ítem, el guardado clásico y la
           agregación.
        2. **GSI2 ``STUCK#{analysis_id}``** (Ítems ``PENDING``): respaldo cuando el
           META ya salió de SWEEP pero aún quedan Ítems ``PENDING`` que iterar.

        El resultado se memoiza por ``analysis_id`` para evitar una lectura extra
        por cada Ítem (patrón N+1): una invocación procesa un único Lote y el
        repositorio vive lo que vive el contenedor de la Lambda.

        :raises NotFoundError: si el Lote no es localizable por ninguno de los dos
            índices (no existe, o ya es terminal con evento publicado y sin Ítems
            ``PENDING``).
        """
        cached = self._tenant_cache.get(analysis_id)
        if cached is not None:
            return cached

        tenant_id = self._lookup_tenant_in_sweep(analysis_id)
        if tenant_id is None:
            tenant_id = self._lookup_tenant_in_stuck(analysis_id)
        if tenant_id is None:
            raise NotFoundError(analysis_id=analysis_id)

        self._tenant_cache[analysis_id] = tenant_id
        return tenant_id

    def _lookup_tenant_in_sweep(self, analysis_id: str) -> str | None:
        """Busca el ``tenant_id`` del META de un Lote en el índice GSI2 ``SWEEP``.

        :return: el ``tenant_id``, o ``None`` si el META no está en el índice SWEEP.
        """
        response = self._client.query(
            TableName=self._table_name,
            IndexName=_GSI2_NAME,
            KeyConditionExpression="#gpk = :sweep",
            FilterExpression="contains(#gsk, :aid)",
            ExpressionAttributeNames={"#gpk": _GSI2_PK, "#gsk": _GSI2_SK},
            ExpressionAttributeValues={
                ":sweep": {"S": _SWEEP_MARKER},
                ":aid": {"S": analysis_id},
            },
        )
        return _tenant_of_matching_pk(response.get("Items", []), analysis_id)

    def _lookup_tenant_in_stuck(self, analysis_id: str) -> str | None:
        """Busca el ``tenant_id`` de un Lote por un ITEM ``PENDING`` en GSI2 ``STUCK#``.

        :return: el ``tenant_id``, o ``None`` si no hay Ítems ``PENDING`` del Lote.
        """
        response = self._client.query(
            TableName=self._table_name,
            IndexName=_GSI2_NAME,
            KeyConditionExpression="#gpk = :stuck",
            ExpressionAttributeNames={"#gpk": _GSI2_PK},
            ExpressionAttributeValues={":stuck": {"S": f"STUCK#{analysis_id}"}},
            Limit=1,
        )
        return _tenant_of_matching_pk(response.get("Items", []), analysis_id)

    # -- PurgeRepository ---------------------------------------------------

    def delete_items(self, analysis_id: str, tenant_id: str) -> int:
        """Borra todos los ITEM del Lote con ``BatchWriteItem`` (``DeleteRequest``).

        Proyecta solo las claves (``pk``/``sk``) para no leer PII (patrón 9) y borra
        en tandas de 25. El META se conserva (se borra al final de la purga con
        :meth:`delete_meta`), de modo que la operación sea reintentable sin dejar
        Ítems huérfanos.

        :return: número de ITEM eliminados.
        """
        pk = self._pk(tenant_id, analysis_id)
        keys: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
                "ExpressionAttributeNames": {"#pk": _PK, "#sk": _SK},
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk},
                    ":prefix": {"S": _SK_ITEM_PREFIX},
                },
                "ProjectionExpression": "#pk, #sk",
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = self._client.query(**kwargs)
            keys.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break

        for start in range(0, len(keys), _BATCH_SIZE):
            batch = [
                {"DeleteRequest": {"Key": {_PK: k[_PK], _SK: k[_SK]}}}
                for k in keys[start : start + _BATCH_SIZE]
            ]
            self._write_batch(batch)
        return len(keys)

    def delete_meta(self, analysis_id: str, tenant_id: str) -> None:
        """Borra el META del Lote. Último paso de la purga (tras ITEM y objetos S3)."""
        self._client.delete_item(
            TableName=self._table_name,
            Key={_PK: {"S": self._pk(tenant_id, analysis_id)}, _SK: {"S": _SK_META}},
        )

    # -- Deserialización ---------------------------------------------------

    def _deserialize_meta(self, raw: dict[str, Any]) -> AnalysisMeta:
        """Reconstruye un :class:`AnalysisMeta` desde el formato de bajo nivel."""
        return AnalysisMeta(
            analysis_id=raw["analysis_id"]["S"],
            tenant_id=raw["tenant_id"]["S"],
            owner_sub=raw["owner_sub"]["S"],
            status=AnalysisStatus(raw["status"]["S"]),
            item_count=int(raw["item_count"]["N"]),
            done_count=int(raw["done_count"]["N"]),
            failed_count=int(raw["failed_count"]["N"]),
            skipped_count=int(raw["skipped_count"]["N"]),
            input_s3_key=raw.get("input_s3_key", {}).get("S", ""),
            output_s3_key=raw.get("output_s3_key", {}).get("S"),
            execution_arn=raw.get("execution_arn", {}).get("S"),
            created_at=_parse_iso(raw["created_at"]["S"]),
            updated_at=_parse_iso(raw["updated_at"]["S"]),
            completed_at=(
                _parse_iso(raw["completed_at"]["S"])
                if "completed_at" in raw
                else None
            ),
            schema_version=int(raw.get("schema_version", {}).get("N", "1")),
        )

    def _deserialize_item(self, raw: dict[str, Any]) -> AnalysisItem:
        """Reconstruye un :class:`AnalysisItem` desde el formato de bajo nivel."""
        classic = self._deserialize_classic(raw)
        emotions = self._deserialize_emotions(raw)
        return AnalysisItem(
            analysis_id=raw["analysis_id"]["S"],
            item_id=raw["item_id"]["S"],
            index=int(raw["index"]["N"]),
            indificador=raw["indificador"]["S"],
            respuesta=raw.get("respuesta", {}).get("S", ""),
            status=ItemStatus(raw["status"]["S"]),
            classic=classic,
            emotions=emotions,
            error_code=raw.get("error_code", {}).get("S"),
            attempt_count=int(raw.get("attempt_count", {}).get("N", "0")),
            processed_at=(
                _parse_iso(raw["processed_at"]["S"])
                if "processed_at" in raw
                else None
            ),
        )

    @staticmethod
    def _deserialize_classic(raw: dict[str, Any]) -> ClassicSentiment | None:
        """Reconstruye el resultado clásico de un ITEM, o ``None`` si no lo tiene."""
        if "language_code" not in raw or "sentiment_score_positive" not in raw:
            return None
        return ClassicSentiment(
            item_id=raw["item_id"]["S"],
            language_code=raw["language_code"]["S"],
            language_confidence=Decimal(
                raw.get("language_confidence", {}).get("N", "0")
            ),
            sentimiento_general=SentimientoGeneral(
                raw.get("sentimiento_general", {}).get(
                    "S", str(SentimientoGeneral.NO_DETERMINADO)
                )
            ),
            scores=SentimentScores(
                positive=Decimal(raw["sentiment_score_positive"]["N"]),
                negative=Decimal(raw["sentiment_score_negative"]["N"]),
                neutral=Decimal(raw["sentiment_score_neutral"]["N"]),
                mixed=Decimal(raw["sentiment_score_mixed"]["N"]),
            ),
        )

    @staticmethod
    def _deserialize_emotions(raw: dict[str, Any]) -> EmotionAnalysis | None:
        """Reconstruye el resultado profundo de un ITEM, o ``None`` si no lo tiene."""
        if "model_id" not in raw:
            return None
        sentimientos = tuple(
            member["S"] for member in raw.get("sentimientos", {}).get("L", [])
        )
        return EmotionAnalysis(
            item_id=raw["item_id"]["S"],
            sentimientos=sentimientos,
            explicacion=raw.get("explicacion", {}).get("S", ""),
            model_id=raw["model_id"]["S"],
            input_tokens=int(raw.get("input_tokens", {}).get("N", "0")),
            output_tokens=int(raw.get("output_tokens", {}).get("N", "0")),
        )


# ---------------------------------------------------------------------------
# Utilidades de módulo.
# ---------------------------------------------------------------------------


def _resolve_table_name() -> str:
    """Resuelve el nombre de la Dynamo_Table desde ``TABLE_NAME`` (perezoso).

    :raises SentimentError: si ``TABLE_NAME`` no está definida o está en blanco.
    """
    raw = os.environ.get(_ENV_TABLE_NAME)
    if raw is None or not raw.strip():
        raise SentimentError(
            "Falta la configuración requerida 'TABLE_NAME' (nombre de la "
            "Dynamo_Table). Debe inyectarse como variable de entorno."
        )
    return raw.strip()


def _tenant_of_matching_pk(
    items: list[dict[str, Any]], analysis_id: str
) -> str | None:
    """Extrae el ``tenant_id`` del primer item cuya ``pk`` corresponde al Lote.

    La ``pk`` es ``TENANT#{tenant_id}#ANALYSIS#{analysis_id}``; el ``tenant_id`` es
    su segundo segmento. Se exige que la ``pk`` termine en ``ANALYSIS#{analysis_id}``
    para descartar coincidencias espurias del ``FilterExpression`` (``contains``).

    :param items: items de bajo nivel devueltos por una ``Query`` sobre un GSI.
    :param analysis_id: Lote cuyo tenant se busca.
    :return: el ``tenant_id``, o ``None`` si ningún item corresponde al Lote.
    """
    suffix = f"ANALYSIS#{analysis_id}"
    for raw in items:
        key: str = raw[_PK]["S"]
        if key.endswith(suffix):
            return key.split("#")[1]
    return None


def _string_list(values: tuple[str, ...]) -> dict[str, Any]:
    """Serializa una tupla de cadenas a una lista DynamoDB (tipo ``L`` de ``S``).

    Se usa una lista (``L``), no un conjunto (``SS``), porque DynamoDB no admite
    conjuntos de cadenas vacíos y el orden de ``sentimientos`` es significativo
    (se conserva la primera aparición, criterio 7.6).
    """
    return {"L": [{"S": value} for value in values]}


def _is_conditional_check_failed(exc: ClientError) -> bool:
    """Indica si ``exc`` es un ``ConditionalCheckFailedException`` de ``update_item``."""
    return bool(
        exc.response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _encode_cursor(last_key: dict[str, Any] | None) -> str | None:
    """Codifica un ``LastEvaluatedKey`` como cursor opaco base64, o ``None``.

    El cursor es un detalle de implementación: el llamante lo trata como una
    cadena sin estructura. Se serializa a JSON y se codifica en base64 URL-safe
    para que viaje sin problemas en una query string.
    """
    if not last_key:
        return None
    import base64
    import json

    return base64.urlsafe_b64encode(
        json.dumps(last_key, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    """Decodifica un cursor opaco a un ``ExclusiveStartKey``, o ``None``.

    :raises SentimentError: si el cursor está presente pero es malformado.
    """
    if cursor is None:
        return None
    import base64
    import json

    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        key = json.loads(decoded)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SentimentError("Cursor de paginación malformado.") from exc
    if not isinstance(key, dict):
        raise SentimentError("Cursor de paginación malformado.")
    return key
