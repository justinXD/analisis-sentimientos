"""Pruebas de integración del pipeline completo contra un stack dev desplegado (tarea 15.2).

Estas pruebas ejercitan el Sistema **end-to-end** contra recursos AWS reales de un
stack desplegado en ``dev`` (API Gateway, DynamoDB, S3, Step Functions y la Lambda
``reconcile_stuck``). Cubren los seis escenarios de la tarea 15.2:

1. **Camino feliz**: un Lote válido alcanza un Estado_Terminal y su resultado se
   descarga por una Presigned_URL (criterios 5.9, 12.11).
2. **Fallo parcial**: un Lote con ítems vacíos (Marcador_Vacio) y textos reales
   produce ítems ``SKIPPED`` conviviendo con ``DONE``, y el Lote termina de forma
   consistente con la conservación de contadores (criterios 5.9, 10.14).
3. **Lote vacío**: ``POST /v1/analyses`` con ``items: []`` se rechaza con 400 en el
   borde, sin crear ningún Lote (criterio 3.7, soporte del contrato de ingesta).
4. **Lote de 1.000 ítems**: el máximo que admite el diseño (criterio 5.3) se acepta
   y termina, respetando ese tope (un ítem más se rechaza).
5. **Expiración por el sweeper**: un Lote atascado lo cierra ``reconcile_stuck`` a
   ``EXPIRED``, publicando el Evento_Finalizacion (criterios 10.14, soporte 10.11).
6. **Purga**: tras la Purga de un Lote terminal, ``GET`` responde 404 y **no queda
   ninguna versión ni marcador de eliminación** en S3 bajo los prefijos del Lote
   (criterios 19.3, 19.4), verificado con ``list_object_versions``.

Gate de ejecución
-----------------
Por diseño, estas pruebas corren solo contra un ``dev`` real. En un entorno sin
stack ni credenciales se **SALTAN** (``skip``) de forma limpia mediante el marker
``integration`` y un ``skipif`` condicionado a las variables de entorno de
:mod:`tests.integration._harness`. Nunca fallan por ausencia de infraestructura y
nunca contaminan la recolección del resto de la suite.

Cómo ejecutarlas contra un dev real
------------------------------------
Ver ``backend/tests/integration/README.md`` y la sección correspondiente de
``docs/ARCHITECTURE.md``. En resumen, con el venv del backend activo::

    export AWS_REGION=<region>
    export INTEGRATION_STACK_ENV=dev
    export INTEGRATION_API_BASE_URL=<url-base-de-la-api>
    export INTEGRATION_ACCESS_TOKEN=<access-token-bearer>
    export INTEGRATION_TENANT_ID=<tenant-id-del-token>
    # opcional, solo para el escenario del sweeper:
    export INTEGRATION_RECONCILE_FUNCTION_NAME=<nombre-lambda-reconcile-stuck>
    python -m pytest -m integration -q

Seguridad (steering)
--------------------
Nada de credenciales, endpoints, ARNs de cuenta ni direcciones reales en el
código: todo se lee de variables de entorno por NOMBRE. Los textos enviados son
sintéticos y no se registran payloads con PII.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from . import _harness
from ._harness import (
    ApiClient,
    StackConfig,
    make_items,
    poll_until_terminal,
    remaining_object_versions,
    skip_if_no_reconcile,
)

# Marker + salto limpio a nivel de módulo. Patrón estándar: todo el módulo es
# `integration` y se salta cuando faltan stack/credenciales (criterio: no romper
# la suite existente ni la recolección en entornos sin despliegue).
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_harness.SHOULD_SKIP, reason=_harness.SKIP_REASON),
]

# Techos de espera del sondeo (segundos). Un Lote pequeño termina en pocos
# minutos; el de 1.000 ítems necesita más holgura por la concurrencia acotada
# (criterios 5.5-5.7). El sweeper corre cada 15 min, pero aquí lo invocamos bajo
# demanda, por lo que la ventana es la de la propia Lambda.
_SMALL_BATCH_TIMEOUT_S = 15 * 60
_LARGE_BATCH_TIMEOUT_S = 60 * 60
_SWEEPER_TIMEOUT_S = 5 * 60

# Tope de ítems que admite el diseño por Lote (criterios 5.3 y 3.6).
_MAX_ITEMS = 1000


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def config() -> StackConfig:
    """Resuelve una sola vez los nombres de recurso del stack dev por convención."""
    return _harness.load_stack_config()


@pytest.fixture(scope="module")
def api(config: StackConfig) -> ApiClient:
    """Cliente HTTP autenticado contra la API Gateway desplegada."""
    return ApiClient(config)


@pytest.fixture(scope="module")
def s3_client(config: StackConfig) -> Any:
    """Cliente boto3 de S3 en la región del despliegue."""
    return boto3.client("s3", region_name=config.region)


@pytest.fixture(scope="module")
def dynamodb_client(config: StackConfig) -> Any:
    """Cliente boto3 de DynamoDB en la región del despliegue."""
    return boto3.client("dynamodb", region_name=config.region)


@pytest.fixture
def purge_guard(
    api: ApiClient,
) -> Iterator[list[str]]:
    """Garantiza la limpieza de los Lotes creados por una prueba, incluso si falla.

    Las pruebas registran aquí los ``analysis_id`` que crean; al terminar, se
    purgan en el *teardown* para no dejar datos sintéticos ni objetos de S3 en el
    entorno ``dev``. Un Lote ya purgado por la propia prueba devuelve 404 y se
    ignora. Requiere el scope ``purge`` en el token.
    """
    created: list[str] = []
    yield created
    for analysis_id in created:
        # La limpieza nunca debe enmascarar el fallo real de una prueba: un Lote ya
        # purgado (404) o un error de red en el teardown se ignoran deliberadamente.
        with contextlib.suppress(Exception):
            api.purge(analysis_id)


# --------------------------------------------------------------------------- #
# Escenario 1: camino feliz                                                    #
# --------------------------------------------------------------------------- #


def test_happy_path_batch_completes_and_downloads(
    api: ApiClient, purge_guard: list[str]
) -> None:
    """Camino feliz: un Lote válido termina y su resultado se descarga.

    Verifica el ciclo completo de ingesta → orquestación → consolidación →
    descarga (criterios 5.9, 12.11): ``POST`` devuelve 202 con ``analysis_id``, el
    Lote alcanza un Estado_Terminal con contadores conservados
    (``done+failed+skipped == item_count``), y ``download`` emite una Presigned_URL
    descargable que contiene exactamente ``item_count`` ítems ordenados por índice.
    """
    item_count = 5
    submit = api.submit(make_items(item_count))
    assert submit.status == 202, f"ingesta rechazada: {submit.status} {submit.body}"
    analysis_id = submit.body["analysis_id"]
    purge_guard.append(analysis_id)
    assert submit.body["status"] == "PENDING"
    assert submit.body["item_count"] == item_count

    final = poll_until_terminal(api, analysis_id, timeout_seconds=_SMALL_BATCH_TIMEOUT_S)
    assert final["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert final["progress"] == 1
    assert (
        final["done_count"] + final["failed_count"] + final["skipped_count"]
        == item_count
    )
    assert final["download_available"] is True

    download = api.download(analysis_id)
    assert download.status == 200, f"descarga inesperada: {download.status}"
    assert download.body["analysis_id"] == analysis_id
    presigned_url = download.body["download_url"]
    assert isinstance(presigned_url, str) and presigned_url.startswith("https://")

    document = _fetch_consolidated(presigned_url)
    assert document["analysis_id"] == analysis_id
    assert document["item_count"] == item_count
    indices = [item["index"] for item in document["items"]]
    # Colección contigua y ordenada por índice de 0 a item_count-1 (criterio 12.7).
    assert indices == list(range(item_count))


# --------------------------------------------------------------------------- #
# Escenario 2: fallo parcial                                                   #
# --------------------------------------------------------------------------- #


def test_partial_failure_mixes_skipped_and_done(
    api: ApiClient, purge_guard: list[str]
) -> None:
    """Fallo parcial: ítems vacíos (SKIPPED) conviven con ítems reales (DONE).

    Un ``respuesta`` vacío es un Marcador_Vacio: Analyze_Comprehend lo transiciona
    a ``SKIPPED`` sin invocar Comprehend ni Bedrock (criterio 6.6), mientras los
    textos reales se procesan. El Lote termina en un Estado_Terminal con los
    contadores conservados (criterios 5.9, 10.14): la suma de ``done``, ``failed``
    y ``skipped`` iguala el ``item_count`` y hay al menos un ítem omitido.
    """
    real = make_items(3)
    empty = [{"indificador": "Reconocimiento", "respuesta": ""} for _ in range(2)]
    items = real + empty
    submit = api.submit(items)
    assert submit.status == 202, f"ingesta rechazada: {submit.status} {submit.body}"
    analysis_id = submit.body["analysis_id"]
    purge_guard.append(analysis_id)

    final = poll_until_terminal(api, analysis_id, timeout_seconds=_SMALL_BATCH_TIMEOUT_S)
    assert final["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    assert final["progress"] == 1
    assert (
        final["done_count"] + final["failed_count"] + final["skipped_count"]
        == len(items)
    )
    # Los dos ítems con Marcador_Vacio deben contarse como SKIPPED (criterio 6.6).
    assert final["skipped_count"] >= len(empty)


# --------------------------------------------------------------------------- #
# Escenario 3: lote vacío                                                      #
# --------------------------------------------------------------------------- #


def test_empty_batch_is_rejected(api: ApiClient) -> None:
    """Lote vacío: ``items: []`` se rechaza con 400 en el borde, sin crear Lote.

    El array vacío infringe ``minItems: 1`` del JSON_Schema; Api_Submit responde
    400 con ``VALIDATION_FAILED`` y un cuerpo compuesto solo por ``error_code`` y
    ``correlation_id``, sin PII (criterios 3.7, 3.14). No se crea ningún
    ``analysis_id`` que haya que purgar.
    """
    submit = api.submit([])
    assert submit.status == 400, f"se esperaba 400, llegó {submit.status}"
    assert submit.body.get("error_code") == "VALIDATION_FAILED"
    assert "analysis_id" not in submit.body


# --------------------------------------------------------------------------- #
# Escenario 4: lote de 1.000 ítems                                             #
# --------------------------------------------------------------------------- #


def test_max_items_batch_is_accepted_and_terminates(
    api: ApiClient, purge_guard: list[str]
) -> None:
    """Lote de 1.000 ítems: el máximo del diseño se acepta y alcanza un terminal.

    Respeta el límite del criterio 5.3 (hasta 1.000 ítems por Lote): un Lote de
    exactamente 1.000 ítems se acepta (202) y termina con contadores conservados,
    mientras que uno de 1.001 se rechaza con ``TOO_MANY_ITEMS`` (criterio 3.6), lo
    que fija el tope por ambos lados.
    """
    # Un ítem por encima del tope: rechazo determinista en el borde (criterio 3.6).
    too_many = api.submit(make_items(_MAX_ITEMS + 1))
    assert too_many.status == 400
    assert too_many.body.get("error_code") == "TOO_MANY_ITEMS"

    submit = api.submit(make_items(_MAX_ITEMS))
    assert submit.status == 202, f"ingesta rechazada: {submit.status} {submit.body}"
    analysis_id = submit.body["analysis_id"]
    purge_guard.append(analysis_id)
    assert submit.body["item_count"] == _MAX_ITEMS

    final = poll_until_terminal(api, analysis_id, timeout_seconds=_LARGE_BATCH_TIMEOUT_S)
    assert final["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}
    assert final["progress"] == 1
    assert (
        final["done_count"] + final["failed_count"] + final["skipped_count"]
        == _MAX_ITEMS
    )


# --------------------------------------------------------------------------- #
# Escenario 5: expiración por el sweeper                                        #
# --------------------------------------------------------------------------- #


def test_sweeper_expires_abandoned_batch(
    api: ApiClient,
    config: StackConfig,
    dynamodb_client: Any,
    purge_guard: list[str],
) -> None:
    """Expiración por el sweeper: un Lote atascado se cierra a ``EXPIRED``.

    Simula el abandono envejeciendo el ``updated_at`` del registro META de un Lote
    recién creado por detrás del ``STUCK_THRESHOLD`` (900 s) e invocando la Lambda
    ``reconcile_stuck`` bajo demanda. El sweeper debe transicionar el Lote a
    ``EXPIRED`` y sus ítems ``PENDING`` a ``FAILED`` (criterios 10.11/10.12), tras
    lo cual ``GET`` lo reporta en un Estado_Terminal (criterio 10.14).

    Requiere ``INTEGRATION_RECONCILE_FUNCTION_NAME``; sin ella, se salta.
    """
    function_name = skip_if_no_reconcile(config)
    lambda_client = boto3.client("lambda", region_name=config.region)

    submit = api.submit(make_items(2))
    assert submit.status == 202
    analysis_id = submit.body["analysis_id"]
    purge_guard.append(analysis_id)

    # Envejece el META por detrás del umbral del sweeper para que lo evalúe como
    # abandonado (criterio 10.12). Se escribe un `updated_at` ISO 8601 muy
    # anterior; el pk/sk siguen la convención del diseño (6.2).
    stale_iso = "2000-01-01T00:00:00+00:00"
    dynamodb_client.update_item(
        TableName=config.table_name,
        Key={
            "pk": {"S": f"TENANT#{config.tenant_id}#ANALYSIS#{analysis_id}"},
            "sk": {"S": "META"},
        },
        UpdateExpression="SET updated_at = :u",
        ExpressionAttributeValues={":u": {"S": stale_iso}},
    )

    # Invoca el sweeper bajo demanda (equivale a su disparo programado de 15 min).
    response = lambda_client.invoke(FunctionName=function_name, InvocationType="RequestResponse")
    assert response["StatusCode"] == 200, "la invocación del sweeper falló"

    # El cierre a EXPIRED es síncrono dentro de la invocación; damos una ventana
    # corta por consistencia eventual de lecturas.
    deadline = time.monotonic() + _SWEEPER_TIMEOUT_S
    status_body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        current = api.status(analysis_id)
        assert current.status == 200
        status_body = current.body
        if status_body["status"] == "EXPIRED":
            break
        time.sleep(5)
    assert status_body.get("status") == "EXPIRED", (
        f"el sweeper no expiró el Lote: status={status_body.get('status')!r}"
    )


# --------------------------------------------------------------------------- #
# Escenario 6: purga con verificación de que no quedan versiones en S3          #
# --------------------------------------------------------------------------- #


def test_purge_removes_all_s3_versions(
    api: ApiClient,
    config: StackConfig,
    s3_client: Any,
) -> None:
    """Purga: tras suprimir un Lote terminal no queda ninguna versión en S3.

    Procesa un Lote hasta un Estado_Terminal, lo purga con ``DELETE`` (criterio
    19.1) y verifica (criterios 19.3, 19.4): (a) ``GET`` de estado y de descarga
    responden 404 ``NOT_FOUND`` (criterio 19.9); (b) con ``list_object_versions``
    **no queda ninguna versión ni marcador de eliminación** bajo los prefijos
    ``inputs/{tenant}/.../{analysis_id}``, ``outputs/{tenant}/.../{analysis_id}`` ni
    ``map-results/{analysis_id}/`` del Lote purgado.

    No se registra en ``purge_guard`` porque la propia prueba ejecuta la Purga.
    """
    submit = api.submit(make_items(4))
    assert submit.status == 202, f"ingesta rechazada: {submit.status} {submit.body}"
    analysis_id = submit.body["analysis_id"]

    final = poll_until_terminal(api, analysis_id, timeout_seconds=_SMALL_BATCH_TIMEOUT_S)
    assert final["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}

    purge = api.purge(analysis_id)
    assert purge.status == 200, f"purga inesperada: {purge.status} {purge.body}"
    assert purge.body["analysis_id"] == analysis_id
    assert "purged_at" in purge.body

    # (a) El Lote deja de ser consultable (criterio 19.9).
    assert api.status(analysis_id).status == 404
    assert api.download(analysis_id).status == 404

    # (b) No queda ninguna versión ni marcador bajo los prefijos del Lote. El
    # sufijo `{analysis_id}` acota la enumeración al Lote purgado sin tocar los de
    # otros Lotes del mismo Tenant (criterios 19.3/19.4).
    tenant = config.tenant_id
    prefixes = [
        f"inputs/{tenant}/",
        f"outputs/{tenant}/",
        f"map-results/{analysis_id}/",
    ]
    for prefix in prefixes:
        remaining = remaining_object_versions(s3_client, config.bucket_name, prefix)
        offending = [v for v in remaining if analysis_id in v["Key"]]
        assert not offending, (
            f"quedaron versiones/marcadores del Lote purgado bajo {prefix!r}: "
            f"{[v['Key'] for v in offending]}"
        )


# --------------------------------------------------------------------------- #
# Utilidades locales                                                           #
# --------------------------------------------------------------------------- #


def _fetch_consolidated(presigned_url: str) -> dict[str, Any]:
    """Descarga y decodifica el Documento_Consolidado desde una Presigned_URL.

    Usa :mod:`urllib` con un timeout explícito (steering backend). La URL es
    temporal (900 s, criterio 12.2) y de un solo objeto cifrado con la CMK.

    Args:
        presigned_url: La Presigned_URL emitida por ``api_download``.

    Returns:
        El Documento_Consolidado decodificado como ``dict``.
    """
    import urllib.request

    with urllib.request.urlopen(
        presigned_url, timeout=_harness.HTTP_TIMEOUT_SECONDS
    ) as resp:
        payload = resp.read()
    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed
