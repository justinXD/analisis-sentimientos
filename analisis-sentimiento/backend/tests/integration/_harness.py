"""Arnés de las pruebas de integración contra un stack dev desplegado.

Centraliza (a) el *gate* de salto (``skip``) cuando no hay stack ni credenciales,
(b) la resolución de los nombres de recurso del stack ``dev`` por la convención
``{project}-{env}-...`` del diseño (sección 6.5 y ``infraestructura/app.py``), y
(c) los clientes boto3 y las operaciones compartidas (envío por la API real,
sondeo del estado, descarga, purga y verificación de que no quedan versiones en
S3).

Decisión de resolución de recursos
-----------------------------------
El diseño nombra los recursos con estado por convención ``{project}-{env}-...``:

* Dynamo_Table      → ``{project}-{env}-analysis``        (``data_stack``)
* Bucket_Analisis   → ``{project}-{env}-analysis-{account_id}``  (``storage_stack``)
* Maquina_De_Estados→ ``{project}-{env}-analysis``        (``workflow_stack``)
* Cola_Solicitudes  → ``{project}-{env}-requests``        (``ingest_stack``)

La Lambda ``reconcile_stuck`` (sweeper) **no** fija ``function_name`` explícito,
así que CloudFormation le asigna un nombre generado no derivable por convención;
por eso su nombre se toma de ``INTEGRATION_RECONCILE_FUNCTION_NAME`` cuando el
escenario del sweeper quiere invocarla bajo demanda. El resto se derivan del par
``{project}-{env}``, con ``project`` por defecto ``sentiment`` (configurable con
``INTEGRATION_PROJECT``) y ``env`` tomado de ``INTEGRATION_STACK_ENV``.

Seguridad (steering)
--------------------
Este módulo **no** contiene credenciales, endpoints, ARNs de cuenta ni direcciones
reales: todo valor sensible se lee en runtime de variables de entorno nombradas y
boto3 resuelve las credenciales de la cadena estándar del SDK. Los textos de
``respuesta`` enviados son sintéticos; nunca se registran payloads con PII.

Variables de entorno (NOMBRES; nunca valores reales en el repo)
---------------------------------------------------------------
* ``INTEGRATION_STACK_ENV``            (obligatoria): entorno desplegado, p. ej. ``dev``.
* ``INTEGRATION_API_BASE_URL``         (obligatoria): URL base de la API desplegada,
  p. ej. ``https://<api-id>.execute-api.<region>.amazonaws.com/<stage>``.
* ``INTEGRATION_ACCESS_TOKEN``         (obligatoria): Access_Token Bearer con los
  scopes ``write``/``read`` (y ``purge`` para el escenario de purga). Su claim
  ``custom:tenant_id`` fija el Tenant de la prueba.
* ``INTEGRATION_TENANT_ID``            (obligatoria): ``tenant_id`` del token, usado
  para construir los prefijos de S3 al verificar la purga.
* ``INTEGRATION_PROJECT``              (opcional, por defecto ``sentiment``).
* ``INTEGRATION_ACCOUNT_ID``           (opcional): id de cuenta para el nombre del
  bucket; si falta se resuelve con STS (``GetCallerIdentity``).
* ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` (obligatoria para boto3).
* ``INTEGRATION_RECONCILE_FUNCTION_NAME`` (opcional): nombre de la Lambda del
  sweeper; sin ella, el escenario del sweeper se salta.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3
import pytest

# --------------------------------------------------------------------------- #
# Gate de salto (skip) a nivel de módulo                                       #
# --------------------------------------------------------------------------- #

#: Variables de entorno sin las que ninguna prueba de integración puede correr.
#: Su ausencia es la condición de salto limpio en entornos sin stack desplegado.
_REQUIRED_ENV_VARS: tuple[str, ...] = (
    "INTEGRATION_STACK_ENV",
    "INTEGRATION_API_BASE_URL",
    "INTEGRATION_ACCESS_TOKEN",
    "INTEGRATION_TENANT_ID",
)


def _missing_required_env() -> list[str]:
    """Devuelve los nombres de las variables obligatorias ausentes o vacías."""
    return [name for name in _REQUIRED_ENV_VARS if not os.getenv(name)]


#: Razón del salto cuando falta configuración. Se evalúa una sola vez al importar.
SKIP_REASON: str = (
    "pruebas de integración: requieren un stack dev desplegado y credenciales AWS; "
    f"faltan variables de entorno: {', '.join(_missing_required_env()) or '—'} "
    "(ver backend/tests/integration/README.md)"
)

#: Condición booleana del salto: cierto cuando NO hay stack/credenciales.
SHOULD_SKIP: bool = bool(_missing_required_env())


# --------------------------------------------------------------------------- #
# Configuración resuelta del stack desplegado                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StackConfig:
    """Nombres de recurso del stack dev, resueltos por convención.

    Todos los nombres se derivan del par ``{project}-{env}`` salvo el del bucket,
    que incluye el id de cuenta, y el de la Lambda del sweeper, que no es derivable
    por convención (ver el docstring del módulo).

    Attributes:
        project: Prefijo del proyecto (``sentiment`` por defecto).
        env_name: Entorno desplegado (``dev``).
        api_base_url: URL base de la API Gateway desplegada (sin barra final).
        access_token: Access_Token Bearer para autenticar las solicitudes.
        tenant_id: ``tenant_id`` del token, para construir prefijos de S3.
        region: Región AWS del despliegue.
        account_id: Id de cuenta, parte del nombre del bucket.
        table_name: Nombre de la Dynamo_Table.
        bucket_name: Nombre del Bucket_Analisis.
        reconcile_function_name: Nombre de la Lambda del sweeper, o ``None``.
    """

    project: str
    env_name: str
    api_base_url: str
    access_token: str
    tenant_id: str
    region: str
    account_id: str
    table_name: str
    bucket_name: str
    reconcile_function_name: str | None

    @property
    def prefix(self) -> str:
        """Prefijo ``{project}-{env}`` común a los nombres de recurso."""
        return f"{self.project}-{self.env_name}"


def _resolve_account_id(region: str) -> str:
    """Resuelve el id de cuenta desde ``INTEGRATION_ACCOUNT_ID`` o STS.

    Args:
        region: Región AWS para construir el cliente de STS.

    Returns:
        El id de cuenta de 12 dígitos.
    """
    explicit = os.getenv("INTEGRATION_ACCOUNT_ID")
    if explicit:
        return explicit
    sts = boto3.client("sts", region_name=region)
    return str(sts.get_caller_identity()["Account"])


def load_stack_config() -> StackConfig:
    """Construye el :class:`StackConfig` del stack dev desde el entorno.

    Deriva los nombres de recurso por la convención ``{project}-{env}-...`` del
    diseño. No se invoca cuando :data:`SHOULD_SKIP` es cierto, de modo que la mera
    importación/recolección del módulo nunca toca AWS ni STS.

    Returns:
        La configuración resuelta del stack.

    Raises:
        RuntimeError: Si no hay región configurada para boto3.
    """
    env_name = os.environ["INTEGRATION_STACK_ENV"]
    project = os.getenv("INTEGRATION_PROJECT", "sentiment")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or ""
    if not region:
        raise RuntimeError(
            "AWS_REGION o AWS_DEFAULT_REGION debe estar definida para las pruebas "
            "de integración (boto3 necesita una región)."
        )
    account_id = _resolve_account_id(region)
    prefix = f"{project}-{env_name}"
    return StackConfig(
        project=project,
        env_name=env_name,
        api_base_url=os.environ["INTEGRATION_API_BASE_URL"].rstrip("/"),
        access_token=os.environ["INTEGRATION_ACCESS_TOKEN"],
        tenant_id=os.environ["INTEGRATION_TENANT_ID"],
        region=region,
        account_id=account_id,
        table_name=f"{prefix}-analysis",
        bucket_name=f"{prefix}-analysis-{account_id}",
        reconcile_function_name=os.getenv("INTEGRATION_RECONCILE_FUNCTION_NAME"),
    )


# --------------------------------------------------------------------------- #
# Cliente HTTP de la API real                                                  #
# --------------------------------------------------------------------------- #

#: Timeout (segundos) de toda llamada HTTP a la API. Toda llamada de red lleva un
#: timeout explícito: nunca se deja colgar indefinidamente (steering backend).
HTTP_TIMEOUT_SECONDS: float = 30.0


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """Respuesta HTTP mínima de la API: código de estado y cuerpo decodificado."""

    status: int
    body: dict[str, Any]


class ApiClient:
    """Cliente HTTP fino sobre la API Gateway desplegada, autenticado por Bearer.

    Usa :mod:`urllib` (de la biblioteca estándar) para no añadir dependencias de
    runtime a las pruebas. Cada método mapea un endpoint del contrato de la API
    (Requirements 4, 11, 12, 19).
    """

    def __init__(self, config: StackConfig) -> None:
        """Inicializa el cliente con la URL base y el token del :class:`StackConfig`."""
        self._base = config.api_base_url
        self._token = config.access_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        """Ejecuta una solicitud HTTP y decodifica el cuerpo JSON de la respuesta.

        Args:
            method: Verbo HTTP (``GET``/``POST``/``DELETE``).
            path: Ruta relativa a la raíz de la API, con barra inicial.
            body: Cuerpo JSON opcional (solo ``POST``).

        Returns:
            La :class:`ApiResponse` con el código de estado y el cuerpo decodificado
            (``{}`` si la respuesta no trae cuerpo JSON).
        """
        url = f"{self._base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url=url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=HTTP_TIMEOUT_SECONDS
            ) as response:
                return ApiResponse(
                    status=int(response.status),
                    body=_decode_json(response.read()),
                )
        except urllib.error.HTTPError as exc:
            # Los códigos de error (4xx/5xx) son respuestas esperadas del contrato,
            # no fallos del transporte: se decodifican igual que un 2xx.
            return ApiResponse(status=int(exc.code), body=_decode_json(exc.read()))

    def submit(self, items: Iterable[Mapping[str, str]]) -> ApiResponse:
        """``POST /v1/analyses``: envía un Lote de ítems sintéticos."""
        return self._request("POST", "/v1/analyses", body={"items": list(items)})

    def status(self, analysis_id: str) -> ApiResponse:
        """``GET /v1/analyses/{analysis_id}``: estado y contadores de un Lote."""
        return self._request("GET", f"/v1/analyses/{analysis_id}")

    def download(self, analysis_id: str) -> ApiResponse:
        """``GET /v1/analyses/{analysis_id}/download``: Presigned_URL del resultado."""
        return self._request("GET", f"/v1/analyses/{analysis_id}/download")

    def purge(self, analysis_id: str) -> ApiResponse:
        """``DELETE /v1/analyses/{analysis_id}``: Purga del Lote (scope purge)."""
        return self._request("DELETE", f"/v1/analyses/{analysis_id}")


def _decode_json(raw: bytes) -> dict[str, Any]:
    """Decodifica un cuerpo HTTP a ``dict``; devuelve ``{}`` si no es JSON objeto."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# Operaciones compartidas por los escenarios                                   #
# --------------------------------------------------------------------------- #

#: Estados terminales del Lote (diseño 6.2 / Requirement 10).
TERMINAL_STATES: frozenset[str] = frozenset(
    {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "EXPIRED"}
)


def make_items(count: int) -> list[dict[str, str]]:
    """Construye ``count`` ítems sintéticos válidos para ``POST /v1/analyses``.

    Usa un ``indificador`` del enum cerrado y textos de ``respuesta`` sintéticos
    (sin PII real, steering de seguridad). El patrón alterna textos no vacíos para
    que el pipeline produzca resultados de sentimiento reales.

    Args:
        count: Número de ítems a generar (``1 <= count <= 1000`` por criterio 5.3).

    Returns:
        La lista de ítems ``{"indificador", "respuesta"}``.
    """
    indificador = "Satisfacción en el Puesto"
    return [
        {
            "indificador": indificador,
            "respuesta": f"Respuesta sintetica de prueba numero {i}.",
        }
        for i in range(count)
    ]


def poll_until_terminal(
    api: ApiClient,
    analysis_id: str,
    *,
    timeout_seconds: float,
    interval_seconds: float = 10.0,
) -> dict[str, Any]:
    """Sondea ``GET /{analysis_id}`` hasta que el Lote alcanza un Estado_Terminal.

    Verifica de paso la monotonía del progreso (criterio 11.3): ni ``progress`` ni
    los contadores decrecen entre consultas consecutivas.

    Args:
        api: Cliente de la API.
        analysis_id: Identificador del Lote.
        timeout_seconds: Tiempo máximo de espera antes de fallar la prueba.
        interval_seconds: Intervalo entre sondeos.

    Returns:
        El cuerpo del último ``GET`` (ya en Estado_Terminal).

    Raises:
        AssertionError: Si se excede ``timeout_seconds`` o si el progreso retrocede.
    """
    deadline = time.monotonic() + timeout_seconds
    last_progress = -1.0
    last_done = last_failed = last_skipped = -1
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = api.status(analysis_id)
        assert response.status == 200, f"estado inesperado: {response.status}"
        body = response.body
        progress = float(body["progress"])
        done = int(body["done_count"])
        failed = int(body["failed_count"])
        skipped = int(body["skipped_count"])
        # Monotonía del progreso (criterio 11.3): nunca retrocede.
        assert progress >= last_progress, "el progreso retrocedió entre consultas"
        assert done >= last_done and failed >= last_failed and skipped >= last_skipped
        last_progress, last_done, last_failed, last_skipped = (
            progress,
            done,
            failed,
            skipped,
        )
        if str(body["status"]) in TERMINAL_STATES:
            return body
        time.sleep(interval_seconds)
    raise AssertionError(
        f"el Lote {analysis_id} no alcanzó un Estado_Terminal en {timeout_seconds}s "
        f"(último status={body.get('status')!r})"
    )


def remaining_object_versions(
    s3_client: Any, bucket: str, prefix: str
) -> list[dict[str, str]]:
    """Enumera TODAS las versiones y marcadores de eliminación bajo un prefijo.

    Materializa la verificación de los criterios 19.3/19.4: tras una Purga no debe
    quedar **ninguna** versión ni marcador de eliminación de los objetos del Lote.
    Usa ``list_object_versions`` paginado para no cargar en memoria colecciones no
    acotadas (steering backend).

    Args:
        s3_client: Cliente boto3 de S3.
        bucket: Nombre del Bucket_Analisis.
        prefix: Prefijo a enumerar (p. ej. ``inputs/{tenant}/`` o
            ``map-results/{analysis_id}/``).

    Returns:
        Lista de ``{"Key", "VersionId"}`` de cada versión y marcador remanente
        (vacía cuando la Purga eliminó todo, que es el resultado esperado).
    """
    paginator = s3_client.get_paginator("list_object_versions")
    remaining: list[dict[str, str]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Versions", []):
            remaining.append({"Key": entry["Key"], "VersionId": entry["VersionId"]})
        for marker in page.get("DeleteMarkers", []):
            remaining.append({"Key": marker["Key"], "VersionId": marker["VersionId"]})
    return remaining


def skip_if_no_reconcile(config: StackConfig) -> str:
    """Devuelve el nombre de la Lambda del sweeper o salta el escenario si falta.

    El nombre de ``reconcile_stuck`` no es derivable por convención (lo genera
    CloudFormation), así que el escenario del sweeper requiere
    ``INTEGRATION_RECONCILE_FUNCTION_NAME``. Sin ella, el escenario se salta de
    forma limpia en vez de fallar.

    Args:
        config: La configuración del stack.

    Returns:
        El nombre de la función del sweeper.
    """
    if not config.reconcile_function_name:
        pytest.skip(
            "escenario del sweeper: define INTEGRATION_RECONCILE_FUNCTION_NAME con "
            "el nombre de la Lambda reconcile_stuck del stack dev"
        )
    return config.reconcile_function_name
