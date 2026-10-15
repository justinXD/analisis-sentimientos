"""Prueba de carga de concurrencia multi-tenant contra un stack dev (tarea 15.4).

Lanza **5 Lotes de 5 Tenants distintos de forma simultánea** (concurrencia real
con ``concurrent.futures``) contra la API desplegada en ``dev`` y verifica dos
cosas:

1. **Conservación de ítems y aislamiento entre Tenants** (criterios 5.8/18.3/18.5):
   cada Lote alcanza un Estado_Terminal con ``done + failed + skipped ==
   item_count`` y ningún Lote comparte ``analysis_id`` con otro; los contadores de
   un Tenant no se mezclan con los de otro.
2. **Latencias de ``api_submit`` y ``api_status``** (criterios 18.1/18.9): se mide
   la latencia por operación bajo carga y se calculan p50/p95/p99 frente a los
   objetivos iniciales del diseño (sección 22.1):

   * ``POST /v1/analyses`` (<= 100 ítems): p95 < 1 s, techo duro 29 s.
   * ``GET /v1/analyses/{id}``: p95 < 300 ms.

   Si la p95 medida supera el objetivo (o alguna observación supera el techo
   duro), la prueba **falla** con un mensaje claro — es una prueba, no un simple
   benchmark informativo. Como el diseño marca estas cifras como *objetivos
   iniciales revisables tras las pruebas de carga*, se admite un modo
   **report-only** activable con ``INTEGRATION_LOAD_REPORT_ONLY=1`` que registra
   el resumen sin fallar, pensado para la primera calibración.

Gate de ejecución
-----------------
Igual que las de integración (tarea 15.2), estas pruebas corren solo contra un
``dev`` real. Sin stack/credenciales o sin los 5 Tenants configurados se
**SALTAN** (``skip``) de forma limpia mediante los markers ``load``/``integration``
y un ``skipif`` condicionado a las variables de entorno. Nunca fallan por ausencia
de infraestructura ni contaminan la recolección del resto de la suite.

Cómo ejecutarla contra un dev real
----------------------------------
Ver ``backend/tests/load/README.md``. En resumen, con el venv del backend activo::

    export AWS_REGION=<region>
    export INTEGRATION_STACK_ENV=dev
    export INTEGRATION_API_BASE_URL=<url-base-de-la-api>
    # 5 Tenants distintos, cada uno con su token (scopes write/read/purge):
    export INTEGRATION_TENANT_TOKENS="t1:<TOKEN_1>,t2:<TOKEN_2>,...,t5:<TOKEN_5>"
    # opcional, primera calibración sin fallar por objetivos:
    export INTEGRATION_LOAD_REPORT_ONLY=1
    python -m pytest -m load -q

Seguridad (steering)
--------------------
Nada de credenciales, tokens, endpoints ni direcciones reales en el código: todo
se lee de variables de entorno por NOMBRE. Los textos enviados son sintéticos y no
se registran payloads ni tokens con PII. Cada Lote creado se purga al final
(incluso si la prueba falla) para no dejar datos sintéticos en ``dev``.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import pytest

from integration._harness import make_items, poll_until_terminal

from . import _multitenant
from ._metrics import (
    BatchOutcome,
    LatencyObjective,
    batch_outcome_from_status,
    evaluate_objective,
    summarize_latencies,
    verify_conservation,
)
from ._multitenant import REQUIRED_TENANT_COUNT, MultiTenantConfig, TenantContext

# Markers + salto limpio a nivel de módulo. Se reutiliza el marker `integration`
# (estas pruebas también tocan el stack desplegado) y se añade `load` para poder
# seleccionarlas/aislarlas con `-m load`.
pytestmark = [
    pytest.mark.load,
    pytest.mark.integration,
    pytest.mark.skipif(_multitenant.SHOULD_SKIP, reason=_multitenant.SKIP_REASON),
]

# Items por Lote. 5 Lotes de este tamaño son carga suficiente para medir latencias
# de `api_submit`/`api_status` sin incurrir en el coste de un Lote de 1.000 ítems;
# además encaja en el objetivo de "<= 100 ítems" de `api_submit` (criterio 18.1).
_ITEMS_PER_BATCH = 20

# Sondeos de estado por Lote mientras se espera a su Estado_Terminal. Cada sondeo
# es una observación de latencia de `api_status` bajo carga concurrente.
_STATUS_SAMPLES_PER_BATCH = 8
_STATUS_SAMPLE_INTERVAL_S = 2.0

# Techo de espera a que cada Lote termine (segundos). Lotes pequeños terminan en
# pocos minutos aun con 5 Tenants compartiendo la concurrencia de Bedrock.
_BATCH_TERMINAL_TIMEOUT_S = 20 * 60

# Objetivos iniciales de latencia del diseño (sección 22.1, criterios 18.1/18.9).
_SUBMIT_OBJECTIVE = LatencyObjective(
    name="api_submit (<=100 items)",
    p95_target_seconds=1.0,
    hard_ceiling_seconds=29.0,
)
_STATUS_OBJECTIVE = LatencyObjective(
    name="api_status",
    p95_target_seconds=0.300,
)


def _report_only() -> bool:
    """``True`` si el modo report-only está activo (no falla por objetivos)."""
    return os.getenv("INTEGRATION_LOAD_REPORT_ONLY", "").strip() in {"1", "true", "yes"}


# --------------------------------------------------------------------------- #
# Resultado por Lote                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _BatchRun:
    """Medición de un Lote: su desenlace y las latencias observadas.

    Attributes:
        outcome: Contadores terminales del Lote (conservación de ítems).
        submit_latency: Latencia de ``POST /v1/analyses`` en segundos.
        status_latencies: Latencias de los ``GET`` de estado en segundos.
    """

    outcome: BatchOutcome
    submit_latency: float
    status_latencies: tuple[float, ...]


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def config() -> MultiTenantConfig:
    """Resuelve una sola vez la configuración multi-tenant del stack dev."""
    cfg = _multitenant.load_multitenant_config()
    # Defensa en profundidad: el gate ya garantiza >= 5 Tenants, pero no se asume.
    assert len(cfg.tenants) >= REQUIRED_TENANT_COUNT, (
        f"se requieren al menos {REQUIRED_TENANT_COUNT} Tenants distintos, "
        f"hay {len(cfg.tenants)}"
    )
    return cfg


@pytest.fixture
def purge_guard(config: MultiTenantConfig) -> Iterator[list[tuple[TenantContext, str]]]:
    """Garantiza la limpieza de los Lotes creados, incluso si la prueba falla.

    Registra pares ``(tenant, analysis_id)`` y los purga en el *teardown* con el
    cliente del Tenant dueño del Lote, para no dejar datos sintéticos ni objetos
    de S3 en ``dev``. Requiere el scope ``purge`` en cada token.
    """
    created: list[tuple[TenantContext, str]] = []
    yield created
    for tenant, analysis_id in created:
        # La limpieza nunca enmascara el fallo real de la prueba: un Lote ya
        # purgado (404) o un error de red en el teardown se ignoran.
        with contextlib.suppress(Exception):
            tenant.client.purge(analysis_id)


# --------------------------------------------------------------------------- #
# Trabajo por Tenant (ejecutado en paralelo)                                   #
# --------------------------------------------------------------------------- #


def _run_tenant_batch(tenant: TenantContext) -> tuple[str, _BatchRun]:
    """Envía y sondea un Lote de un Tenant, midiendo las latencias.

    Mide la latencia de ``api_submit`` (una observación) y de ``api_status``
    (varias observaciones durante el sondeo hasta el Estado_Terminal). Devuelve el
    ``analysis_id`` creado para que el llamador lo registre en la limpieza antes de
    cualquier aserción.

    Args:
        tenant: El Tenant con su cliente de API autenticado.

    Returns:
        Par ``(analysis_id, _BatchRun)`` con el desenlace y las latencias.

    Raises:
        AssertionError: Si la ingesta no devuelve 202 o si el Lote no termina.
    """
    items = make_items(_ITEMS_PER_BATCH)

    submit_started = time.monotonic()
    submit = tenant.client.submit(items)
    submit_latency = time.monotonic() - submit_started
    assert submit.status == 202, (
        f"ingesta rechazada para tenant {tenant.tenant_id}: "
        f"{submit.status} {submit.body.get('error_code')}"
    )
    analysis_id = str(submit.body["analysis_id"])

    # Observaciones de latencia de `api_status` bajo carga. Se toma una tanda
    # acotada de muestras cronometradas y luego se espera al terminal sin medir,
    # para no sesgar la serie con sondeos muy espaciados.
    status_latencies: list[float] = []
    for _ in range(_STATUS_SAMPLES_PER_BATCH):
        started = time.monotonic()
        response = tenant.client.status(analysis_id)
        status_latencies.append(time.monotonic() - started)
        assert response.status == 200, (
            f"estado inesperado para {analysis_id}: {response.status}"
        )
        if str(response.body["status"]) in _multitenant_terminal_states():
            break
        time.sleep(_STATUS_SAMPLE_INTERVAL_S)

    final = poll_until_terminal(
        tenant.client, analysis_id, timeout_seconds=_BATCH_TERMINAL_TIMEOUT_S
    )
    outcome = batch_outcome_from_status(analysis_id, tenant.tenant_id, final)
    return analysis_id, _BatchRun(
        outcome=outcome,
        submit_latency=submit_latency,
        status_latencies=tuple(status_latencies),
    )


def _multitenant_terminal_states() -> frozenset[str]:
    """Estados terminales del Lote, reutilizados del arnés de integración."""
    from integration._harness import TERMINAL_STATES

    return TERMINAL_STATES


# --------------------------------------------------------------------------- #
# Prueba de carga                                                              #
# --------------------------------------------------------------------------- #


def test_five_tenants_concurrent_conserve_items_and_meet_latency(
    config: MultiTenantConfig,
    purge_guard: list[tuple[TenantContext, str]],
) -> None:
    """5 Tenants simultáneos: sin pérdida de ítems y latencias dentro de objetivo.

    Lanza los 5 Lotes a la vez con un pool de hilos (concurrencia real, sin locks
    globales: cada hilo opera sobre su propio Tenant/cliente) y, al reunir los
    resultados, verifica la conservación de ítems y el aislamiento entre Tenants
    (criterios 5.8/18.3/18.5) y las latencias de ``api_submit``/``api_status``
    (criterios 18.1/18.9) frente a sus objetivos iniciales.
    """
    tenants = config.tenants[:REQUIRED_TENANT_COUNT]

    runs: list[_BatchRun] = []
    # Concurrencia real: un hilo por Tenant, todos arrancan a la vez. Se registran
    # los analysis_id en `purge_guard` conforme van creándose, antes de evaluar
    # cualquier aserción, para que la limpieza alcance incluso a los Lotes de las
    # tareas que fallen una aserción posterior.
    with ThreadPoolExecutor(max_workers=len(tenants)) as pool:
        futures = {
            pool.submit(_run_tenant_batch, tenant): tenant for tenant in tenants
        }
        for future in as_completed(futures):
            tenant = futures[future]
            analysis_id, run = future.result()
            purge_guard.append((tenant, analysis_id))
            runs.append(run)

    assert len(runs) == REQUIRED_TENANT_COUNT

    # --- Conservación de ítems y aislamiento entre Tenants (5.8/18.3/18.5) ---
    conservation = verify_conservation([run.outcome for run in runs])
    assert conservation.all_conserved, conservation.describe()
    # Aislamiento adicional: cada Lote pertenece a un Tenant distinto.
    tenant_ids = {run.outcome.tenant_id for run in runs}
    assert len(tenant_ids) == REQUIRED_TENANT_COUNT, (
        f"se esperaban {REQUIRED_TENANT_COUNT} Tenants distintos, "
        f"hubo {sorted(tenant_ids)}"
    )

    # --- Latencias de api_submit / api_status (18.1/18.9) ---
    submit_summary = summarize_latencies([run.submit_latency for run in runs])
    status_samples = [
        latency for run in runs for latency in run.status_latencies
    ]
    assert status_samples, "no se recogió ninguna muestra de latencia de api_status"
    status_summary = summarize_latencies(status_samples)

    submit_result = evaluate_objective(submit_summary, _SUBMIT_OBJECTIVE)
    status_result = evaluate_objective(status_summary, _STATUS_OBJECTIVE)

    report = "\n".join(
        [
            "Resumen de latencias (carga multi-tenant):",
            submit_result.describe(),
            status_result.describe(),
        ]
    )

    if _report_only():
        # Primera calibración: se informa sin fallar (el diseño marca las cifras
        # como objetivos iniciales revisables tras las pruebas de carga).
        pytest.skip(f"modo report-only (INTEGRATION_LOAD_REPORT_ONLY):\n{report}")

    assert submit_result.passed and status_result.passed, (
        "latencias fuera de los objetivos iniciales (criterios 18.1/18.9):\n"
        + report
    )
