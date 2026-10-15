"""Pruebas de contrato del borrado de versiones en S3 (tarea 6.8).

La tarea 6.8 exige verificar sobre AWS simulado (``moto``) que la Purga de un Lote
borra **todas las versiones y marcadores de eliminación** en S3 (criterio 19.4),
usando solo datos sintéticos (criterio 17.10). Estas pruebas ejercen
:meth:`sentiment.adapters.s3_storage.S3Storage.delete_all_versions` (puerto
:class:`~sentiment.domain.ports.PurgeStorage`) sobre un bucket **versionado**,
que es la condición que hace no trivial el borrado: con el versionado activado
(criterio 16.7), un ``DeleteObject`` simple solo añade un marcador de eliminación
y deja recuperables las versiones anteriores. El contrato que se verifica es que,
tras ``delete_all_versions``, no queda **ningún** dato recuperable del Lote.

Complementan las pruebas de humo de :mod:`tests.adapters.test_support_adapters`
(tarea 6.7) distinguiendo explícitamente versiones vivas de marcadores de
eliminación, cubriendo el prefijo ``map-results/`` con varios objetos y
verificando el aislamiento: un objeto de otro Lote bajo otro prefijo no se toca.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

if TYPE_CHECKING:
    from botocore.client import BaseClient

from sentiment.adapters.s3_storage import S3Storage

_REGION = "us-east-1"
_BUCKET = "sentiment-test-analysis-123456789012"
_ANALYSIS_ID = "01JDQZ8F3K7XWQ2M5N8P4R6T9V"
_OTHER_ANALYSIS_ID = "01JDQZ8F3K7XWQ2M5N8P4R6TZZ"
_TENANT = "acme"


@pytest.fixture
def versioned_bucket() -> Iterator[BaseClient]:
    """Crea un bucket S3 **versionado** simulado y cede un cliente apuntando a él."""
    with mock_aws():
        client = boto3.client("s3", region_name=_REGION)
        client.create_bucket(Bucket=_BUCKET)
        client.put_bucket_versioning(
            Bucket=_BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        yield client


@pytest.fixture
def storage(versioned_bucket: BaseClient) -> S3Storage:
    """Instancia el adaptador con el cliente simulado y el bucket versionado."""
    return S3Storage(client=versioned_bucket, bucket_name=_BUCKET)


def _put(client: BaseClient, key: str, body: bytes) -> None:
    """Escribe (una versión más de) un objeto sintético en el bucket."""
    client.put_object(Bucket=_BUCKET, Key=key, Body=body)  # type: ignore[attr-defined]


def _delete(client: BaseClient, key: str) -> None:
    """Pone un marcador de eliminación sobre ``key`` (``DeleteObject`` simple)."""
    client.delete_object(Bucket=_BUCKET, Key=key)  # type: ignore[attr-defined]


def _versions(client: BaseClient, prefix: str = "") -> tuple[int, int]:
    """Devuelve ``(versiones_vivas, marcadores_de_eliminacion)`` bajo ``prefix``."""
    response = client.list_object_versions(  # type: ignore[attr-defined]
        Bucket=_BUCKET, Prefix=prefix
    )
    return (
        len(response.get("Versions", [])),
        len(response.get("DeleteMarkers", [])),
    )


def _input_key(analysis_id: str) -> str:
    return f"inputs/{_TENANT}/2025/06/14/{analysis_id}.json"


def _output_key(analysis_id: str) -> str:
    return f"outputs/{_TENANT}/2025/06/14/{analysis_id}.json"


def _map_prefix(analysis_id: str) -> str:
    return f"map-results/{analysis_id}/"


def test_delete_all_versions_removes_versions_and_delete_markers(
    storage: S3Storage, versioned_bucket: BaseClient
) -> None:
    """No queda ninguna versión viva ni marcador de eliminación del Lote (19.4).

    Se generan varias versiones de ``inputs/`` (dos escrituras), una versión más
    un marcador de eliminación de ``outputs/`` (un escritura + un DeleteObject) y
    dos objetos bajo ``map-results/``. El contrato: tras la Purga, tanto las
    versiones vivas como los marcadores de eliminación quedan en cero.
    """
    input_key = _input_key(_ANALYSIS_ID)
    output_key = _output_key(_ANALYSIS_ID)
    map_prefix = _map_prefix(_ANALYSIS_ID)

    _put(versioned_bucket, input_key, b'{"v":1}')
    _put(versioned_bucket, input_key, b'{"v":2}')  # segunda versión de la misma clave
    _put(versioned_bucket, output_key, b'{"out":1}')
    _delete(versioned_bucket, output_key)  # marcador de eliminación
    _put(versioned_bucket, f"{map_prefix}result-0.json", b"{}")
    _put(versioned_bucket, f"{map_prefix}result-1.json", b"{}")

    live_before, markers_before = _versions(versioned_bucket)
    assert live_before >= 5  # 2 inputs + 1 output + 2 map-results
    assert markers_before >= 1  # el DeleteObject de output

    deleted = storage.delete_all_versions(
        exact_keys=[input_key, output_key], prefix=map_prefix
    )

    assert deleted == live_before + markers_before
    live_after, markers_after = _versions(versioned_bucket)
    assert live_after == 0
    assert markers_after == 0


def test_delete_all_versions_clears_every_version_of_a_single_key(
    storage: S3Storage, versioned_bucket: BaseClient
) -> None:
    """Las N versiones de una misma clave se eliminan todas, no solo la actual (19.4)."""
    input_key = _input_key(_ANALYSIS_ID)
    for version in range(4):
        _put(versioned_bucket, input_key, f'{{"v":{version}}}'.encode())

    live_before, _ = _versions(versioned_bucket, prefix=input_key)
    assert live_before == 4

    deleted = storage.delete_all_versions(exact_keys=[input_key], prefix="")

    assert deleted == 4
    assert _versions(versioned_bucket, prefix=input_key) == (0, 0)


def test_delete_all_versions_does_not_touch_another_batch(
    storage: S3Storage, versioned_bucket: BaseClient
) -> None:
    """La Purga de un Lote no borra objetos de otro Lote (aislamiento, 19.4).

    Un objeto de ``_OTHER_ANALYSIS_ID`` bajo su propio prefijo ``map-results/``
    sobrevive intacto a la Purga del Lote ``_ANALYSIS_ID``.
    """
    target_input = _input_key(_ANALYSIS_ID)
    target_map = f"{_map_prefix(_ANALYSIS_ID)}result-0.json"
    other_map = f"{_map_prefix(_OTHER_ANALYSIS_ID)}result-0.json"

    _put(versioned_bucket, target_input, b"{}")
    _put(versioned_bucket, target_map, b"{}")
    _put(versioned_bucket, other_map, b"{}")

    storage.delete_all_versions(
        exact_keys=[target_input], prefix=_map_prefix(_ANALYSIS_ID)
    )

    assert _versions(versioned_bucket, prefix=_map_prefix(_ANALYSIS_ID)) == (0, 0)
    assert _versions(versioned_bucket, prefix=target_input) == (0, 0)
    # El Lote ajeno permanece intacto.
    assert _versions(versioned_bucket, prefix=_map_prefix(_OTHER_ANALYSIS_ID)) == (1, 0)
