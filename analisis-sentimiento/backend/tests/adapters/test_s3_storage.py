"""Pruebas de humo del adaptador ``S3Storage`` (tarea 6.5).

Verifican sobre un S3 simulado (``moto``) el contrato del puerto
:class:`sentiment.domain.ports.ObjectStorage`:

* ``put_json``/``get_json`` hacen un *round-trip* fiel bajo los tres prefijos del
  layout (``inputs/``, ``outputs/``, ``map-results/``), preservando los
  caracteres no ASCII (``ensure_ascii=False``) de las respuestas en español
  (criterios 4.1, 10.3);
* ``get_json`` traduce el objeto inexistente al error de "no encontrado" del
  dominio (:class:`~sentiment.domain.errors.NotFoundError`), base del HTTP 404 de
  Api_Download (criterio 12.5);
* ``presign_get`` firma un único objeto —no un prefijo ni el bucket— con la
  validez por defecto de 900 segundos (criterio 12.2);
* el nombre del bucket se resuelve de ``BUCKET_NAME`` y su ausencia es un
  ``ConfigError`` accionable (criterio 11.1).

Las pruebas de contrato exhaustivas con ``moto`` son la tarea 6.8; aquí solo se
cubre el camino feliz y los bordes esenciales del adaptador.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto import mock_aws

if TYPE_CHECKING:
    from botocore.client import BaseClient

from sentiment.adapters.s3_storage import (
    DEFAULT_PRESIGN_EXPIRY_SECONDS,
    S3Storage,
)
from sentiment.domain.errors import NotFoundError
from sentiment.observability.config import ConfigError

_BUCKET = "sentiment-test-analysis-123456789012"
_REGION = "us-east-1"


@pytest.fixture
def s3_bucket() -> Iterator[BaseClient]:
    """Crea un bucket S3 simulado y cede un cliente de boto3 apuntando a él."""
    with mock_aws():
        client = boto3.client("s3", region_name=_REGION)
        client.create_bucket(Bucket=_BUCKET)
        yield client


@pytest.fixture
def storage(s3_bucket: BaseClient) -> S3Storage:
    """Instancia el adaptador con el cliente simulado y el bucket de prueba."""
    return S3Storage(client=s3_bucket, bucket_name=_BUCKET)


@pytest.mark.parametrize(
    "key",
    [
        "inputs/tenant-a/2024/01/02/01HZX.json",
        "outputs/tenant-a/2024/01/02/01HZX.json",
        "map-results/01HZX/result.json",
    ],
)
def test_put_json_then_get_json_round_trips_under_each_prefix(
    storage: S3Storage, key: str
) -> None:
    """``put_json`` seguido de ``get_json`` devuelve el payload intacto en cada prefijo."""
    payload = {"analysis_id": "01HZX", "items": [{"index": 0, "respuesta": "mañana ñandú"}]}

    storage.put_json(key, payload)
    result = storage.get_json(key)

    assert result == payload


def test_put_json_preserves_non_ascii_as_utf8(
    storage: S3Storage, s3_bucket: BaseClient
) -> None:
    """El cuerpo escrito es UTF-8 con los caracteres no ASCII literales (no \\uXXXX)."""
    key = "outputs/tenant-a/2024/01/02/01HZX.json"
    storage.put_json(key, {"respuesta": "cañón"})

    raw = s3_bucket.get_object(Bucket=_BUCKET, Key=key)["Body"].read()  # type: ignore[attr-defined]

    assert "cañón".encode() in raw  # bytes UTF-8 literales, sin escapar
    assert b"\\u" not in raw


def test_get_json_missing_object_raises_domain_not_found(storage: S3Storage) -> None:
    """Un objeto inexistente se traduce al error de "no encontrado" del dominio."""
    with pytest.raises(NotFoundError):
        storage.get_json("outputs/tenant-a/2024/01/02/ausente.json")


def _assert_expires_in(url: str, expected_seconds: int) -> None:
    """Verifica que la URL firmada caduca a ``expected_seconds``.

    Independiente de la versión de firma: SigV4 expone ``X-Amz-Expires`` con la
    validez relativa; SigV2 expone ``Expires`` con el instante de caducidad
    absoluto (epoch), que debe aproximarse a ``now + expected_seconds``.
    """
    query = parse_qs(urlparse(url).query)
    if "X-Amz-Expires" in query:
        assert query["X-Amz-Expires"] == [str(expected_seconds)]
    else:
        expires_at = int(query["Expires"][0])
        now = int(time.time())
        # Holgura amplia para absorber el tiempo de firma y la latencia de CI.
        assert expected_seconds - 5 <= expires_at - now <= expected_seconds + 5


def test_presign_get_targets_single_object_with_default_expiry(
    storage: S3Storage,
) -> None:
    """La Presigned_URL firma un único objeto con validez de 900 s por defecto."""
    key = "outputs/tenant-a/2024/01/02/01HZX.json"
    storage.put_json(key, {"ok": True})

    url = storage.presign_get(key)

    parsed = urlparse(url)
    assert parsed.scheme == "https"
    # Apunta exactamente a ese objeto, no a un prefijo ni al bucket.
    assert parsed.path.endswith(f"/{key}")
    assert DEFAULT_PRESIGN_EXPIRY_SECONDS == 900
    _assert_expires_in(url, DEFAULT_PRESIGN_EXPIRY_SECONDS)


def test_presign_get_honours_explicit_expiry(storage: S3Storage) -> None:
    """La validez explícita se refleja en la URL firmada."""
    key = "outputs/tenant-a/2024/01/02/01HZX.json"
    storage.put_json(key, {"ok": True})

    url = storage.presign_get(key, expires_in=120)

    _assert_expires_in(url, 120)


def test_bucket_name_resolved_from_env_when_not_injected(
    s3_bucket: BaseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin bucket inyectado, el nombre se resuelve de ``BUCKET_NAME`` (criterio 11.1)."""
    monkeypatch.setenv("BUCKET_NAME", _BUCKET)
    storage = S3Storage(client=s3_bucket)

    storage.put_json("inputs/tenant-a/2024/01/02/01HZX.json", {"ok": True})

    assert storage.get_json("inputs/tenant-a/2024/01/02/01HZX.json") == {"ok": True}


def test_missing_bucket_name_raises_config_error(
    s3_bucket: BaseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un ``BUCKET_NAME`` ausente es un ``ConfigError`` accionable, no un KeyError."""
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    storage = S3Storage(client=s3_bucket)

    with pytest.raises(ConfigError, match="BUCKET_NAME"):
        storage.put_json("inputs/x.json", {"ok": True})
