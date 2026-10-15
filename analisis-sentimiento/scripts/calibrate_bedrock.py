#!/usr/bin/env python3
"""Runner de calibración de concurrencia del Modelo_Bedrock (tarea 15.3).

Este script implementa el procedimiento de calibración de la decisión **D4** del
diseño (``design.md``, "D4 — Dimensionado del batch vs límite de 5 min de
Express") y de los criterios **18.2** y **18.4** de los requisitos. Mide la
latencia real (p50/p95/p99) y el uso de tokens de ``analyze_llm`` invocando el
Modelo_Bedrock a varias concurrencias (1/5/10/20/40 por defecto), calcula el
tiempo de batch frente al **techo duro de 300 s de un workflow Express** y
propone los tres diales de concurrencia que el diseño deja abiertos a
calibración:

* ``distributed_map_max_concurrency`` — ``MaxConcurrency`` del Distributed Map
  padre (cuántos batches de 25 ítems se procesan en paralelo por lote).
* ``llm_fanout_max_concurrency`` — ``MaxConcurrency`` del Map interno (fan-out
  por ítem hacia ``analyze_llm``) dentro de cada ejecución hija Express.
* ``analyze_llm_reserved_concurrency`` — concurrencia reservada de la Lambda
  ``analyze_llm`` (tope duro global de invocaciones simultáneas a Bedrock).

Los valores propuestos se escriben en ``infraestructura/config/{env}.json``
**únicamente** cuando se pasa el flag explícito ``--write-config``. Por defecto
el script solo mide, calcula e imprime la propuesta (no toca ningún archivo
versionado ni la red si se usa ``--dry-run``).

Interfaz de línea de comandos
-----------------------------
Ejemplos (desde la raíz del repositorio, con el venv de backend activado)::

    # Ayuda; no toca red ni archivos.
    python scripts/calibrate_bedrock.py -h

    # Ensayo en seco: valida argumentos, construye la muestra sintética y aplica
    # la lógica de propuesta sobre latencias simuladas. CERO llamadas a Bedrock.
    python scripts/calibrate_bedrock.py --env dev --dry-run

    # Calibración real contra Bedrock en la cuenta/región del perfil activo.
    # Solo imprime la propuesta (no escribe config).
    python scripts/calibrate_bedrock.py --env dev --model-id <MODEL_ID> \\
        --region us-east-1 --samples 200

    # Igual que el anterior, pero además persiste la propuesta en el JSON del
    # entorno. Este es el ÚNICO modo que modifica un archivo versionado.
    python scripts/calibrate_bedrock.py --env dev --model-id <MODEL_ID> \\
        --region us-east-1 --samples 200 --write-config

Entorno de ejecución
--------------------
El script se ejecuta con el **intérprete del venv de backend**
(``backend/.venv``): reutiliza sus dependencias (``boto3``) y el paquete
``sentiment`` del backend (adaptador real de Bedrock y ``system_prompt``
versionado), lo que hace que la medición use exactamente el mismo camino de
código y el mismo prompt que la Lambda ``analyze_llm`` en producción. El script
inserta ``backend/src`` en ``sys.path`` en tiempo de ejecución porque el paquete
``sentiment`` no está instalado en el venv (solo es visible vía el ``pythonpath``
de pytest); esto se hace dentro de una función, nunca en el import de nivel de
módulo, para no romper la importabilidad ni la colección de pruebas.

Seguridad (steering de ciberseguridad)
--------------------------------------
* **Sin credenciales ni ``model_id`` hardcodeados.** El ``model_id`` llega por
  ``--model-id`` o por la configuración del entorno; las credenciales de AWS las
  resuelve ``boto3`` del entorno/perfil estándar.
* **Sin PII.** La muestra es 100 % sintética (textos genéricos de clima laboral);
  nunca se registran respuestas reales. Los logs solo contienen metadatos:
  latencias, recuentos de tokens, tasas de error y longitudes.
* **Escritura explícita.** El JSON del entorno solo se sobrescribe con
  ``--write-config``; por defecto el script es de solo lectura sobre el disco.

Fiabilidad (steering de backend)
--------------------------------
Toda la E/S de red va contra el adaptador real ``BedrockAdapter``, que ya aplica
timeouts explícitos (``connect_timeout=5``, ``read_timeout=90``) y **cero**
reintentos del SDK. El propio runner añade, encima de ese adaptador, reintentos
con backoff exponencial y jitter para las condiciones transitorias (throttling,
5xx, timeouts, fallos de red), de modo que la medición de latencia no se
contamine con fallos recuperables puntuales ni amplifique la carga durante un
pico de throttling.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - solo para anotaciones de tipo.
    from sentiment.adapters.bedrock_adapter import BedrockAdapter

# --------------------------------------------------------------------------- #
# Constantes de calibración (derivadas del diseño D4 y de los criterios 18.x).
# --------------------------------------------------------------------------- #

#: Techo duro de duración de un workflow Express de Step Functions, en segundos
#: (diseño 10.7). Un batch no puede tardar más que esto o la ejecución hija se
#: aborta por ``States.Timeout``.
EXPRESS_CEILING_SECONDS: Final[float] = 300.0

#: Margen de seguridad bajo el techo Express (diseño D4, paso 4). El p99 del
#: tiempo de batch debe quedar por debajo de ``EXPRESS_CEILING - MARGEN`` para
#: aceptar la configuración; es decir, p99 <= 240 s.
EXPRESS_SAFETY_MARGIN_SECONDS: Final[float] = 60.0

#: Tamaño de batch que produce ``ItemBatcher`` (criterio 5.4 / diseño). Es el
#: número de ítems que una ejecución hija Express procesa, repartidos entre el
#: fan-out interno.
ITEMS_PER_BATCH: Final[int] = 25

#: Tamaño del Lote de referencia para el cálculo de duración total (criterio
#: 18.2 / diseño D4 paso 6): el máximo admitido de 1.000 ítems.
REFERENCE_BATCH_ITEM_COUNT: Final[int] = 1000

#: Concurrencias por defecto a muestrear (diseño D4 pasos 2-3).
DEFAULT_CONCURRENCIES: Final[tuple[int, ...]] = (1, 5, 10, 20, 40)

#: Número mínimo de ítems sintéticos recomendado por la calibración (D4 paso 1).
DEFAULT_SAMPLE_SIZE: Final[int] = 200

#: Tope duro de concurrencia reservada de ``analyze_llm`` que no se debe superar
#: sin revisar la cuota de la cuenta (criterio 5.7: valor inicial 40).
RESERVED_CONCURRENCY_HARD_CAP: Final[int] = 40

#: Reintentos del runner sobre condiciones transitorias (encima del adaptador,
#: que no reintenta). Backoff exponencial con jitter completo.
_MAX_TRANSIENT_RETRIES: Final[int] = 3
_RETRY_BASE_DELAY_SECONDS: Final[float] = 1.0
_RETRY_MAX_DELAY_SECONDS: Final[float] = 20.0

#: Ruta del directorio de configuración de la Infraestructura, relativa a la raíz
#: del repositorio (dos niveles por encima de ``scripts/``).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_INFRA_CONFIG_DIR: Final[Path] = _REPO_ROOT / "infraestructura" / "config"
_BACKEND_SRC_DIR: Final[Path] = _REPO_ROOT / "backend" / "src"

#: Entornos soportados (debe coincidir con ``infraestructura/config/environments``).
VALID_ENVIRONMENTS: Final[tuple[str, ...]] = ("dev", "stage", "prod")

#: Claves del JSON de entorno que esta calibración propone/reescribe.
CONFIG_KEY_MAP_CONCURRENCY: Final = "distributed_map_max_concurrency"
CONFIG_KEY_FANOUT_CONCURRENCY: Final = "llm_fanout_max_concurrency"
CONFIG_KEY_RESERVED_CONCURRENCY: Final = "analyze_llm_reserved_concurrency"


# --------------------------------------------------------------------------- #
# Estructuras de datos puras (sin dependencia de red).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SampleItem:
    """Un ítem sintético de la muestra de calibración.

    Attributes:
        indificador: categoría de la pregunta; uno de los 11 Indificadores. No
            es PII.
        respuesta: texto sintético de la respuesta. Nunca es una respuesta real
            de una persona; se genera a partir de plantillas genéricas.
    """

    indificador: str
    respuesta: str


@dataclass(frozen=True, slots=True)
class Measurement:
    """Resultado de una única invocación medida de ``analyze_llm``.

    Attributes:
        latency_s: latencia extremo a extremo de la invocación, en segundos.
        input_tokens: tokens de entrada informados por el modelo (0 si ausente).
        output_tokens: tokens de salida informados por el modelo (0 si ausente).
        throttled: ``True`` si la invocación observó throttling antes de
            resolverse (tras agotar los reintentos del runner, o en el intento
            final).
    """

    latency_s: float
    input_tokens: int
    output_tokens: int
    throttled: bool


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """Percentiles y medias de latencia y tokens de una tanda de medidas.

    Attributes:
        samples: número de invocaciones exitosas incluidas.
        p50_s: percentil 50 de la latencia, en segundos.
        p95_s: percentil 95 de la latencia, en segundos.
        p99_s: percentil 99 de la latencia, en segundos.
        mean_s: media de la latencia, en segundos.
        mean_input_tokens: media de tokens de entrada.
        mean_output_tokens: media de tokens de salida.
        throttle_rate: fracción [0, 1] de invocaciones que observaron throttling.
    """

    samples: int
    p50_s: float
    p95_s: float
    p99_s: float
    mean_s: float
    mean_input_tokens: float
    mean_output_tokens: float
    throttle_rate: float


@dataclass(frozen=True, slots=True)
class ConcurrencyResult:
    """Estadísticas medidas a un nivel de concurrencia concreto.

    Attributes:
        concurrency: número de invocaciones simultáneas de este punto de la curva.
        stats: :class:`LatencyStats` agregadas de todas las medidas del punto.
    """

    concurrency: int
    stats: LatencyStats


@dataclass(frozen=True, slots=True)
class ConcurrencyProposal:
    """Propuesta de diales de concurrencia derivada de la calibración.

    Attributes:
        distributed_map_max_concurrency: ``MaxConcurrency`` propuesto del
            Distributed Map padre.
        llm_fanout_max_concurrency: ``MaxConcurrency`` propuesto del fan-out
            interno por ítem.
        analyze_llm_reserved_concurrency: concurrencia reservada propuesta de
            ``analyze_llm``.
        batch_time_p99_s: p99 estimado del tiempo de un batch de 25 ítems con el
            fan-out propuesto, en segundos.
        batch_time_within_ceiling: ``True`` si ``batch_time_p99_s`` respeta el
            techo Express con su margen de seguridad.
        estimated_total_duration_s: duración total estimada de un Lote de 1.000
            ítems con los diales propuestos, en segundos.
        rationale: explicación legible de cómo se derivó la propuesta.
    """

    distributed_map_max_concurrency: int
    llm_fanout_max_concurrency: int
    analyze_llm_reserved_concurrency: int
    batch_time_p99_s: float
    batch_time_within_ceiling: bool
    estimated_total_duration_s: float
    rationale: str


# --------------------------------------------------------------------------- #
# Funciones PURAS de cálculo (testeables sin red).
# --------------------------------------------------------------------------- #


def percentile(values: Sequence[float], pct: float) -> float:
    """Calcula un percentil por interpolación lineal entre vecinos.

    Implementa el mismo método que ``numpy.percentile`` (interpolación lineal,
    tipo 7 de Hyndman & Fan) sin añadir ``numpy`` como dependencia. Es una
    función pura y total: con un único valor devuelve ese valor para cualquier
    percentil, y con la lista vacía lanza ``ValueError`` (no hay percentil de un
    conjunto vacío).

    Args:
        values: muestras numéricas no vacías. No es necesario que estén
            ordenadas; la función ordena una copia.
        pct: percentil a calcular en el rango cerrado [0, 100].

    Returns:
        El valor del percentil ``pct`` interpolado.

    Raises:
        ValueError: si ``values`` está vacía o ``pct`` está fuera de [0, 100].
    """
    if not values:
        raise ValueError("percentile() requiere al menos un valor.")
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"El percentil debe estar en [0, 100]; se recibió {pct}.")

    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])

    rank = (pct / 100.0) * (len(ordered) - 1)
    lower_index = math.floor(rank)
    upper_index = math.ceil(rank)
    if lower_index == upper_index:
        return float(ordered[lower_index])

    weight = rank - lower_index
    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return float(lower + (upper - lower) * weight)


def summarize_latencies(measurements: Sequence[Measurement]) -> LatencyStats:
    """Resume una tanda de medidas en percentiles de latencia y medias de tokens.

    Función pura: no toca red ni estado global. La ``throttle_rate`` se calcula
    sobre el total de medidas (exitosas o con throttling observado), mientras que
    los percentiles y las medias se calculan solo sobre las latencias disponibles.

    Args:
        measurements: medidas no vacías de un punto de concurrencia.

    Returns:
        Las :class:`LatencyStats` del punto.

    Raises:
        ValueError: si ``measurements`` está vacía.
    """
    if not measurements:
        raise ValueError("summarize_latencies() requiere al menos una medida.")

    latencies = [m.latency_s for m in measurements]
    throttled_count = sum(1 for m in measurements if m.throttled)

    return LatencyStats(
        samples=len(measurements),
        p50_s=percentile(latencies, 50.0),
        p95_s=percentile(latencies, 95.0),
        p99_s=percentile(latencies, 99.0),
        mean_s=statistics.fmean(latencies),
        mean_input_tokens=statistics.fmean(m.input_tokens for m in measurements),
        mean_output_tokens=statistics.fmean(m.output_tokens for m in measurements),
        throttle_rate=throttled_count / len(measurements),
    )


def estimate_batch_time(
    per_item_latency_s: float,
    fanout_concurrency: int,
    *,
    items_per_batch: int = ITEMS_PER_BATCH,
) -> float:
    """Estima el tiempo de procesar un batch dado el paralelismo interno.

    Aplica la fórmula del diseño D4 (paso 4)::

        tiempo_batch ~= ceil(items_per_batch / fanout_concurrency) x latencia_por_ítem

    Es decir, los ítems de un batch se procesan en "olas" de ``fanout_concurrency``
    ítems; el número de olas es el techo de la división, y cada ola tarda la
    latencia de un ítem (las invocaciones de una ola corren en paralelo).

    Args:
        per_item_latency_s: latencia de una invocación individual (p. ej. el p99).
        fanout_concurrency: ítems procesados en paralelo dentro del batch (>= 1).
        items_per_batch: ítems por batch; por defecto 25.

    Returns:
        El tiempo estimado del batch, en segundos.

    Raises:
        ValueError: si ``fanout_concurrency`` o ``items_per_batch`` no son >= 1,
            o si la latencia es negativa.
    """
    if fanout_concurrency < 1:
        raise ValueError("fanout_concurrency debe ser >= 1.")
    if items_per_batch < 1:
        raise ValueError("items_per_batch debe ser >= 1.")
    if per_item_latency_s < 0.0:
        raise ValueError("per_item_latency_s no puede ser negativa.")

    waves = math.ceil(items_per_batch / fanout_concurrency)
    return waves * per_item_latency_s


def estimate_total_duration(
    batch_time_s: float,
    map_concurrency: int,
    *,
    total_items: int = REFERENCE_BATCH_ITEM_COUNT,
    items_per_batch: int = ITEMS_PER_BATCH,
) -> float:
    """Estima la duración total de un Lote dado el paralelismo del Map padre.

    Aplica la fórmula del diseño D4 (paso 6)::

        duración_lote ~= ceil(num_batches / map_concurrency) x tiempo_batch

    donde ``num_batches = ceil(total_items / items_per_batch)``.

    Args:
        batch_time_s: tiempo estimado de un batch (p. ej. el calculado con p95).
        map_concurrency: batches procesados en paralelo por el Distributed Map
            padre (>= 1).
        total_items: ítems del Lote de referencia; por defecto 1.000.
        items_per_batch: ítems por batch; por defecto 25.

    Returns:
        La duración total estimada del Lote, en segundos.

    Raises:
        ValueError: si algún parámetro de concurrencia o tamaño no es >= 1, o si
            ``batch_time_s`` es negativo.
    """
    if map_concurrency < 1:
        raise ValueError("map_concurrency debe ser >= 1.")
    if total_items < 1:
        raise ValueError("total_items debe ser >= 1.")
    if items_per_batch < 1:
        raise ValueError("items_per_batch debe ser >= 1.")
    if batch_time_s < 0.0:
        raise ValueError("batch_time_s no puede ser negativo.")

    num_batches = math.ceil(total_items / items_per_batch)
    waves = math.ceil(num_batches / map_concurrency)
    return waves * batch_time_s


def propose_fanout_concurrency(
    p99_latency_s: float,
    *,
    items_per_batch: int = ITEMS_PER_BATCH,
    ceiling_s: float = EXPRESS_CEILING_SECONDS,
    margin_s: float = EXPRESS_SAFETY_MARGIN_SECONDS,
) -> int:
    """Propone el ``MaxConcurrency`` del fan-out interno frente al techo Express.

    Elige el menor paralelismo interno tal que el p99 del tiempo de batch quede
    por debajo de ``ceiling_s - margin_s`` (diseño D4 paso 4: p99 <= 240 s con el
    techo de 300 s y 60 s de margen). Busca de 1 a ``items_per_batch`` (no tiene
    sentido más paralelismo que ítems hay en un batch) y, si ni siquiera el
    máximo cabe bajo el techo, devuelve ``items_per_batch`` como mejor esfuerzo
    (el llamador detecta el incumplimiento vía :func:`estimate_batch_time`).

    Args:
        p99_latency_s: p99 de la latencia por invocación observado a concurrencia
            representativa.
        items_per_batch: ítems por batch; por defecto 25.
        ceiling_s: techo duro del workflow Express; por defecto 300 s.
        margin_s: margen de seguridad bajo el techo; por defecto 60 s.

    Returns:
        El paralelismo interno propuesto, en el rango [1, items_per_batch].

    Raises:
        ValueError: si ``p99_latency_s`` es negativa o ``items_per_batch`` < 1.
    """
    if p99_latency_s < 0.0:
        raise ValueError("p99_latency_s no puede ser negativa.")
    if items_per_batch < 1:
        raise ValueError("items_per_batch debe ser >= 1.")

    budget = ceiling_s - margin_s
    for fanout in range(1, items_per_batch + 1):
        if estimate_batch_time(
            p99_latency_s, fanout, items_per_batch=items_per_batch
        ) <= budget:
            return fanout
    return items_per_batch


def propose_reserved_concurrency(
    mean_latency_s: float,
    total_concurrent_bedrock: int,
    *,
    hard_cap: int = RESERVED_CONCURRENCY_HARD_CAP,
) -> int:
    """Propone la concurrencia reservada de ``analyze_llm``.

    El diseño D4 (paso 5) fija ``reservedConcurrentExecutions`` como el número de
    invocaciones de Bedrock que pueden estar en vuelo simultáneamente. En v1 ese
    número es el producto ``map_concurrency x fanout_concurrency`` por Lote
    (criterio 18.4): el tope reservado debe cubrir al menos esa concurrencia
    por-Lote y nunca superar el tope duro de cuenta (criterio 5.7). La latencia
    media solo entra como factor de sobredimensionado defensivo frente a colas de
    arranque; se mantiene el resultado dentro de ``hard_cap``.

    Args:
        mean_latency_s: latencia media por invocación (diagnóstica; no cambia el
            mínimo requerido, que lo fija la concurrencia por-Lote).
        total_concurrent_bedrock: ``map_concurrency x fanout_concurrency``, la
            concurrencia de Bedrock originada por un Lote (criterio 18.4).
        hard_cap: tope máximo que no se debe superar sin revisar la cuota
            (criterio 5.7); por defecto 40.

    Returns:
        La concurrencia reservada propuesta, en el rango
        [total_concurrent_bedrock, hard_cap].

    Raises:
        ValueError: si ``total_concurrent_bedrock`` < 1, ``hard_cap`` < 1, o la
            concurrencia por-Lote excede el tope duro (configuración imposible
            que el operador debe resolver subiendo la cuota).
    """
    if total_concurrent_bedrock < 1:
        raise ValueError("total_concurrent_bedrock debe ser >= 1.")
    if hard_cap < 1:
        raise ValueError("hard_cap debe ser >= 1.")
    if mean_latency_s < 0.0:
        raise ValueError("mean_latency_s no puede ser negativa.")
    if total_concurrent_bedrock > hard_cap:
        raise ValueError(
            f"La concurrencia por-Lote ({total_concurrent_bedrock}) excede el tope "
            f"duro de cuenta ({hard_cap}). Súbase la cuota de Bedrock o redúzcase "
            f"map_concurrency x fanout_concurrency antes de calibrar."
        )
    return min(hard_cap, total_concurrent_bedrock)


def build_proposal(results: Sequence[ConcurrencyResult]) -> ConcurrencyProposal:
    """Deriva la propuesta de diales a partir de la curva de concurrencia medida.

    Es la función pura central de la decisión D4. Toma la curva de concurrencia
    (una :class:`ConcurrencyResult` por nivel medido) y produce la
    :class:`ConcurrencyProposal`. El p99 que gobierna el tiempo de batch se toma
    del **mayor** nivel de concurrencia medido (el peor caso de contención real),
    y la media de latencia, del mismo punto.

    Procedimiento:
        1. Elegir el fan-out interno con :func:`propose_fanout_concurrency` para
           que el p99 del batch quede bajo el techo Express con margen.
        2. Fijar ``map_concurrency`` de modo que ``map x fanout`` no exceda el
           tope duro de concurrencia reservada (criterio 18.4 / 5.7).
        3. Proponer la concurrencia reservada con
           :func:`propose_reserved_concurrency`.
        4. Calcular el tiempo de batch (p99) y la duración total (p95) para
           reportarlos.

    Args:
        results: curva de concurrencia medida, no vacía.

    Returns:
        La :class:`ConcurrencyProposal` derivada.

    Raises:
        ValueError: si ``results`` está vacía.
    """
    if not results:
        raise ValueError("build_proposal() requiere al menos un ConcurrencyResult.")

    worst = max(results, key=lambda r: r.concurrency)
    p99 = worst.stats.p99_s
    p95 = worst.stats.p95_s
    mean = worst.stats.mean_s

    fanout = propose_fanout_concurrency(p99)

    # map x fanout no debe exceder el tope duro de concurrencia reservada
    # (criterio 18.4: 20 = 4 x 5 inicialmente, acotado por 40 de 5.7).
    map_concurrency = max(1, RESERVED_CONCURRENCY_HARD_CAP // fanout)
    total_concurrent = map_concurrency * fanout
    reserved = propose_reserved_concurrency(mean, total_concurrent)

    batch_time_p99 = estimate_batch_time(p99, fanout)
    budget = EXPRESS_CEILING_SECONDS - EXPRESS_SAFETY_MARGIN_SECONDS
    within_ceiling = batch_time_p99 <= budget

    batch_time_p95 = estimate_batch_time(p95, fanout)
    total_duration = estimate_total_duration(batch_time_p95, map_concurrency)

    ceiling_note = (
        f"p99 del batch = {batch_time_p99:.1f} s "
        f"{'<=' if within_ceiling else '>'} presupuesto {budget:.0f} s "
        f"(techo Express {EXPRESS_CEILING_SECONDS:.0f} s - margen "
        f"{EXPRESS_SAFETY_MARGIN_SECONDS:.0f} s)"
    )
    rationale = (
        f"Basado en latencia p99={p99:.2f}s / p95={p95:.2f}s / media={mean:.2f}s a "
        f"concurrencia {worst.concurrency}. fan-out={fanout} es el menor "
        f"paralelismo interno que cumple el techo Express ({ceiling_note}); "
        f"map={map_concurrency} mantiene mapxfan-out={total_concurrent} <= tope duro "
        f"{RESERVED_CONCURRENCY_HARD_CAP} (criterio 18.4); reservada={reserved} "
        f"cubre la concurrencia por-Lote sin exceder la cuota (criterio 5.7). "
        f"Duración estimada de un Lote de {REFERENCE_BATCH_ITEM_COUNT} ítems "
        f"~= {total_duration:.0f} s (D4 paso 6, con p95)."
    )
    if not within_ceiling:
        rationale += (
            " ADVERTENCIA: ni el fan-out máximo cabe bajo el techo Express; "
            "reduzca MaxItemsPerBatch o considere hijos Standard (diseño 17.3)."
        )

    return ConcurrencyProposal(
        distributed_map_max_concurrency=map_concurrency,
        llm_fanout_max_concurrency=fanout,
        analyze_llm_reserved_concurrency=reserved,
        batch_time_p99_s=batch_time_p99,
        batch_time_within_ceiling=within_ceiling,
        estimated_total_duration_s=total_duration,
        rationale=rationale,
    )


# --------------------------------------------------------------------------- #
# Lectura / escritura de config (puras salvo el toque de disco, aisladas).
# --------------------------------------------------------------------------- #


def config_path(env: str, *, config_dir: Path = _INFRA_CONFIG_DIR) -> Path:
    """Devuelve la ruta del JSON de configuración de un entorno.

    Args:
        env: nombre del entorno; uno de :data:`VALID_ENVIRONMENTS`.
        config_dir: directorio de configuración; por defecto el de la
            Infraestructura. Se parametriza para las pruebas.

    Returns:
        La ruta ``{config_dir}/{env}.json``.

    Raises:
        ValueError: si ``env`` no es un entorno soportado.
    """
    if env not in VALID_ENVIRONMENTS:
        raise ValueError(
            f"Entorno desconocido: {env!r}. Válidos: {', '.join(VALID_ENVIRONMENTS)}."
        )
    return config_dir / f"{env}.json"


def load_config(path: Path) -> dict[str, object]:
    """Lee un JSON de configuración de entorno desde disco.

    Args:
        path: ruta al archivo ``{env}.json``.

    Returns:
        El contenido del JSON como diccionario.

    Raises:
        FileNotFoundError: si el archivo no existe.
        ValueError: si el contenido no es un objeto JSON.
        json.JSONDecodeError: si el archivo no es JSON válido.
    """
    if not path.is_file():
        raise FileNotFoundError(f"No existe el archivo de configuración: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"La configuración de {path} debe ser un objeto JSON.")
    return data


def apply_proposal(
    config: dict[str, object], proposal: ConcurrencyProposal
) -> dict[str, object]:
    """Aplica una propuesta a una copia de la configuración, sin mutar la entrada.

    Función pura: devuelve un diccionario nuevo con las tres claves de
    concurrencia reescritas y el resto intacto. No escribe en disco.

    Args:
        config: configuración de entorno original.
        proposal: diales propuestos por la calibración.

    Returns:
        Una copia de ``config`` con las claves de concurrencia actualizadas.
    """
    updated = dict(config)
    updated[CONFIG_KEY_MAP_CONCURRENCY] = proposal.distributed_map_max_concurrency
    updated[CONFIG_KEY_FANOUT_CONCURRENCY] = proposal.llm_fanout_max_concurrency
    updated[CONFIG_KEY_RESERVED_CONCURRENCY] = proposal.analyze_llm_reserved_concurrency
    return updated


def write_config(path: Path, config: dict[str, object]) -> None:
    """Escribe la configuración en disco con formato estable (2 espacios, LF).

    Reproduce el estilo de los JSON versionados existentes (indentación de 2
    espacios y una línea en blanco final) para minimizar el diff. Esta es la
    única función del módulo que escribe en un archivo versionado, y solo se
    invoca bajo el flag explícito ``--write-config``.

    Args:
        path: ruta de destino.
        config: configuración a serializar.
    """
    serialized = json.dumps(config, indent=2, ensure_ascii=False)
    path.write_text(serialized + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Muestra sintética (sin PII).
# --------------------------------------------------------------------------- #

# Plantillas de respuesta 100 % sintéticas de clima laboral. NO son respuestas
# reales de personas; su único fin es reproducir longitudes y vocabulario
# plausibles para que la latencia medida sea representativa.
_SYNTHETIC_TEMPLATES: Final[tuple[str, ...]] = (
    "En general me siento conforme con el ambiente del equipo.",
    "Creo que la comunicacion con mi jefatura podria mejorar bastante.",
    "Valoro las oportunidades de aprendizaje que ofrece la empresa.",
    "A veces la carga de trabajo se vuelve dificil de sostener.",
    "Me gustaria recibir mas reconocimiento por los proyectos entregados.",
    "El espacio fisico de la oficina es comodo y bien iluminado.",
    "Siento que mis aportes no siempre se toman en cuenta en las decisiones.",
    "La colaboracion entre areas ha mejorado en los ultimos meses.",
    "Me preocupa la falta de claridad sobre el plan de crecimiento interno.",
    "Estoy satisfecho con el equilibrio entre vida personal y trabajo.",
)

# Marcadores vacíos sintéticos para reproducir la proporción de respuestas sin
# contenido analizable (criterio 18.2, muestra representativa). El runner los
# excluye de la medición igual que el pipeline los omitiría (criterio 18.6).
_EMPTY_MARKERS: Final[tuple[str, ...]] = ("", "sin comentarios", "n/a", "-")


def build_sample(
    size: int,
    indificadores: Sequence[str],
    *,
    empty_ratio: float = 0.05,
    seed: int = 1234,
) -> list[SampleItem]:
    """Construye una muestra sintética determinista de ``size`` ítems.

    La muestra reproduce la distribución de la calibración (D4 paso 1): los 11
    Indificadores en rotación, textos genéricos de longitud variable y una
    pequeña proporción de respuestas vacías. Es determinista (``seed`` fijo) para
    que dos ejecuciones de calibración sean comparables. **Nunca** contiene PII.

    Args:
        size: número de ítems a generar (>= 1).
        indificadores: lista de Indificadores de la que extraer la categoría.
        empty_ratio: fracción [0, 1) de ítems con respuesta vacía/marcador.
        seed: semilla del generador para reproducibilidad.

    Returns:
        La lista de :class:`SampleItem` sintéticos.

    Raises:
        ValueError: si ``size`` < 1, ``indificadores`` está vacía o
            ``empty_ratio`` está fuera de [0, 1).
    """
    if size < 1:
        raise ValueError("size debe ser >= 1.")
    if not indificadores:
        raise ValueError("indificadores no puede estar vacía.")
    if not 0.0 <= empty_ratio < 1.0:
        raise ValueError("empty_ratio debe estar en [0, 1).")

    rng = random.Random(seed)
    items: list[SampleItem] = []
    for i in range(size):
        indificador = indificadores[i % len(indificadores)]
        if rng.random() < empty_ratio:
            respuesta = rng.choice(_EMPTY_MARKERS)
        else:
            # Combina 1-3 plantillas para variar la longitud del texto.
            parts = rng.choices(_SYNTHETIC_TEMPLATES, k=rng.randint(1, 3))
            respuesta = " ".join(parts)
        items.append(SampleItem(indificador=indificador, respuesta=respuesta))
    return items


def is_empty_marker(respuesta: str) -> bool:
    """Indica si una respuesta es un Marcador_Vacio (criterio 18.6).

    Normaliza a minúsculas y recorta espacios antes de comparar, igual que el
    pipeline. Los ítems vacíos se excluyen de la medición de latencia.

    Args:
        respuesta: texto de la respuesta.

    Returns:
        ``True`` si la respuesta no tiene contenido analizable.
    """
    normalized = respuesta.strip().lower()
    return normalized in {"", "sin comentarios", "n/a", "ninguno", "-"}


# --------------------------------------------------------------------------- #
# Medición contra Bedrock (ÚNICA sección con E/S de red). Nada aquí se ejecuta
# en import ni en colección de pruebas: solo se invoca desde run_calibration().
# --------------------------------------------------------------------------- #


def _retry_delay(attempt: int, rng: random.Random) -> float:
    """Calcula el retardo de backoff exponencial con jitter completo.

    Args:
        attempt: número de intento ya fallido (0-based).
        rng: generador para el jitter.

    Returns:
        El retardo en segundos, acotado por :data:`_RETRY_MAX_DELAY_SECONDS`.
    """
    ceiling = min(_RETRY_MAX_DELAY_SECONDS, _RETRY_BASE_DELAY_SECONDS * (2.0**attempt))
    # Jitter completo (full jitter): uniforme en [0, ceiling].
    return rng.uniform(0.0, ceiling)


def measure_single(
    adapter: BedrockAdapter,
    item: SampleItem,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Measurement:
    """Mide una invocación de ``analyze`` con reintentos sobre transitorios.

    Encima del adaptador real (que no reintenta), aplica hasta
    :data:`_MAX_TRANSIENT_RETRIES` reintentos con backoff exponencial y jitter
    para throttling, 5xx, timeouts y fallos de red, de modo que la latencia
    medida refleje el camino feliz y no un fallo recuperable puntual. Los errores
    permanentes (entrada rechazada, salida inválida) se propagan sin reintentar.

    Args:
        adapter: adaptador real de Bedrock (puerto ``LlmAnalyzer``).
        item: ítem sintético a analizar.
        sleep: función de espera (inyectable para pruebas).
        rng: generador para el jitter (inyectable para pruebas).

    Returns:
        La :class:`Measurement` de la invocación que finalmente tuvo éxito.

    Raises:
        Exception: cualquier error permanente del adaptador, o el último error
            transitorio tras agotar los reintentos.
    """
    # Import local: nunca a nivel de módulo, para no acoplar el import del script
    # a las excepciones del backend ni romper la colección de pruebas.
    from sentiment.domain.errors import ThrottledError, TransientDependencyError

    local_rng = rng if rng is not None else random.Random()
    observed_throttle = False
    last_error: Exception | None = None

    for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
        started = time.monotonic()
        try:
            result = adapter.analyze(item.indificador, item.respuesta)
        except ThrottledError as exc:
            observed_throttle = True
            last_error = exc
        except TransientDependencyError as exc:
            last_error = exc
        else:
            latency = time.monotonic() - started
            return Measurement(
                latency_s=latency,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                throttled=observed_throttle,
            )
        if attempt < _MAX_TRANSIENT_RETRIES:
            sleep(_retry_delay(attempt, local_rng))

    assert last_error is not None  # garantizado por el bucle: siempre hay error.
    raise last_error


def measure_concurrency_point(
    adapter: BedrockAdapter,
    sample: Sequence[SampleItem],
    concurrency: int,
) -> ConcurrencyResult:
    """Mide la latencia de la muestra a un nivel de concurrencia dado.

    Lanza las invocaciones de los ítems no vacíos de la muestra a través de un
    pool de hilos de tamaño ``concurrency`` (la E/S de Bedrock libera el GIL, por
    lo que los hilos consiguen concurrencia real de red). Los ítems vacíos se
    omiten, igual que el pipeline (criterio 18.6).

    Args:
        adapter: adaptador real de Bedrock.
        sample: muestra sintética.
        concurrency: número de invocaciones simultáneas (>= 1).

    Returns:
        El :class:`ConcurrencyResult` con las estadísticas del punto.

    Raises:
        ValueError: si ``concurrency`` < 1 o no hay ítems analizables.
    """
    if concurrency < 1:
        raise ValueError("concurrency debe ser >= 1.")

    analyzable = [it for it in sample if not is_empty_marker(it.respuesta)]
    if not analyzable:
        raise ValueError("La muestra no contiene ningún ítem analizable.")

    measurements: list[Measurement] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(measure_single, adapter, it) for it in analyzable]
        for future in as_completed(futures):
            measurements.append(future.result())

    return ConcurrencyResult(
        concurrency=concurrency, stats=summarize_latencies(measurements)
    )


def run_calibration(
    adapter: BedrockAdapter,
    sample: Sequence[SampleItem],
    concurrencies: Sequence[int],
    *,
    log: Callable[[str], None],
) -> list[ConcurrencyResult]:
    """Ejecuta la curva de concurrencia completa contra Bedrock.

    Recorre ``concurrencies`` en orden ascendente, mide cada punto y registra solo
    metadatos (nunca PII). Devuelve la curva para que :func:`build_proposal` la
    convierta en la propuesta de diales.

    Args:
        adapter: adaptador real de Bedrock.
        sample: muestra sintética analizable.
        concurrencies: niveles de concurrencia a medir.
        log: función de registro de metadatos (inyectada por el CLI).

    Returns:
        La curva de concurrencia medida.

    Raises:
        ValueError: si ``concurrencies`` está vacía.
    """
    if not concurrencies:
        raise ValueError("concurrencies no puede estar vacía.")

    results: list[ConcurrencyResult] = []
    for concurrency in sorted(set(concurrencies)):
        log(f"Midiendo concurrencia={concurrency} sobre {len(sample)} ítems...")
        result = measure_concurrency_point(adapter, sample, concurrency)
        s = result.stats
        log(
            f"  concurrencia={concurrency}: muestras={s.samples} "
            f"p50={s.p50_s:.2f}s p95={s.p95_s:.2f}s p99={s.p99_s:.2f}s "
            f"tokens_in~{s.mean_input_tokens:.0f} tokens_out~{s.mean_output_tokens:.0f} "
            f"throttle={s.throttle_rate:.1%}"
        )
        results.append(result)
    return results


# --------------------------------------------------------------------------- #
# Dry-run: curva simulada SIN red, para ejercitar la lógica pura de punta a punta.
# --------------------------------------------------------------------------- #


def simulate_results(
    concurrencies: Sequence[int], *, base_latency_s: float = 8.0
) -> list[ConcurrencyResult]:
    """Genera una curva de concurrencia simulada (sin red) para ``--dry-run``.

    Modela de forma deliberadamente simple una latencia que crece con la
    concurrencia (contención), de modo que la lógica pura de propuesta se
    ejercite de punta a punta sin invocar Bedrock. No pretende predecir la
    latencia real: solo producir una curva plausible y determinista.

    Args:
        concurrencies: niveles de concurrencia a simular.
        base_latency_s: latencia base a concurrencia 1, en segundos.

    Returns:
        Una curva de :class:`ConcurrencyResult` simulada.

    Raises:
        ValueError: si ``concurrencies`` está vacía.
    """
    if not concurrencies:
        raise ValueError("concurrencies no puede estar vacía.")

    results: list[ConcurrencyResult] = []
    for concurrency in sorted(set(concurrencies)):
        # La latencia crece suavemente con la concurrencia (contención simulada).
        factor = 1.0 + 0.05 * (concurrency - 1)
        p50 = base_latency_s * factor
        stats = LatencyStats(
            samples=concurrency,
            p50_s=p50,
            p95_s=p50 * 1.4,
            p99_s=p50 * 1.8,
            mean_s=p50 * 1.1,
            mean_input_tokens=350.0,
            mean_output_tokens=180.0,
            throttle_rate=0.0 if concurrency <= 20 else 0.1,
        )
        results.append(ConcurrencyResult(concurrency=concurrency, stats=stats))
    return results


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def _parse_concurrencies(raw: str) -> tuple[int, ...]:
    """Parsea la lista de concurrencias de ``--concurrencies`` (CSV de enteros).

    Args:
        raw: cadena como ``"1,5,10,20,40"``.

    Returns:
        La tupla de concurrencias, ordenada y deduplicada.

    Raises:
        argparse.ArgumentTypeError: si algún elemento no es un entero positivo.
    """
    values: list[int] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"'{token}' no es un entero válido en --concurrencies."
            ) from exc
        if value < 1:
            raise argparse.ArgumentTypeError(
                f"Las concurrencias deben ser >= 1; se recibió {value}."
            )
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("--concurrencies no puede estar vacía.")
    return tuple(sorted(set(values)))


def build_arg_parser() -> argparse.ArgumentParser:
    """Construye el parser de argumentos del CLI.

    Returns:
        El :class:`argparse.ArgumentParser` configurado.
    """
    parser = argparse.ArgumentParser(
        prog="calibrate_bedrock.py",
        description=(
            "Calibra la concurrencia del Modelo_Bedrock (decisión D4, criterios "
            "18.2/18.4): mide latencia y tokens de analyze_llm, calcula el tiempo "
            "de batch frente al techo Express de 300 s y propone los diales de "
            "MaxConcurrency y la concurrencia reservada."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--env",
        choices=VALID_ENVIRONMENTS,
        default="dev",
        help="Entorno de Infraestructura cuyo config/{env}.json se lee/propone.",
    )
    parser.add_argument(
        "--concurrencies",
        type=_parse_concurrencies,
        default=DEFAULT_CONCURRENCIES,
        help="Lista CSV de concurrencias a muestrear (p. ej. '1,5,10,20,40').",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help="Número de ítems sintéticos de la muestra (>= 1).",
    )
    parser.add_argument(
        "--model-id",
        default=None,
        help=(
            "Identificador del Modelo_Bedrock a invocar. Si se omite, se usa el "
            "MODEL_ID del entorno/Parameter_Store. Nunca se hardcodea un modelo."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Región de AWS para las llamadas a Bedrock (p. ej. us-east-1).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "No llama a Bedrock: simula la curva de concurrencia para ejercitar la "
            "lógica de propuesta. No toca la red."
        ),
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        help=(
            "Escribe la propuesta en infraestructura/config/{env}.json. Sin este "
            "flag, el script solo imprime la propuesta (modo de solo lectura)."
        ),
    )
    return parser


def _ensure_backend_on_path() -> None:
    """Inserta ``backend/src`` en ``sys.path`` para importar el paquete sentiment.

    El paquete ``sentiment`` del backend no está instalado en el venv (solo es
    visible vía el ``pythonpath`` de pytest), así que el script lo añade en
    tiempo de ejecución. Se hace aquí, dentro de ``main()`` (no en el import de
    nivel de módulo), para que importar este archivo —p. ej. desde las pruebas—
    nunca dependa del layout del repositorio ni ejecute efectos secundarios.
    """
    src = str(_BACKEND_SRC_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)


def _print_proposal(proposal: ConcurrencyProposal, log: Callable[[str], None]) -> None:
    """Imprime la propuesta de diales en formato legible.

    Args:
        proposal: la propuesta a mostrar.
        log: función de salida.
    """
    log("")
    log("=== Propuesta de calibración (decisión D4) ===")
    log(f"  {CONFIG_KEY_MAP_CONCURRENCY}      = {proposal.distributed_map_max_concurrency}")
    log(f"  {CONFIG_KEY_FANOUT_CONCURRENCY}       = {proposal.llm_fanout_max_concurrency}")
    log(
        f"  {CONFIG_KEY_RESERVED_CONCURRENCY} = "
        f"{proposal.analyze_llm_reserved_concurrency}"
    )
    log(
        f"  tiempo de batch p99 ~= {proposal.batch_time_p99_s:.1f} s "
        f"({'dentro del' if proposal.batch_time_within_ceiling else 'EXCEDE el'} "
        f"techo Express)"
    )
    log(
        f"  duración estimada de un Lote de {REFERENCE_BATCH_ITEM_COUNT} ítems ~= "
        f"{proposal.estimated_total_duration_s:.0f} s"
    )
    log(f"  justificación: {proposal.rationale}")
    log("")


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada del CLI.

    Orquesta el flujo completo: parseo de argumentos, construcción de la muestra
    sintética, medición (real o simulada), derivación de la propuesta y, bajo
    ``--write-config``, escritura del JSON del entorno. Toda la E/S de red vive
    aquí o en las funciones que esta llama; nunca en el import del módulo.

    Args:
        argv: argumentos de línea de comandos; por defecto ``sys.argv[1:]``.

    Returns:
        Código de salida del proceso (0 = éxito, 2 = error de uso/configuración).
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Logger sencillo a stderr. Nunca se usa `print` (prohibido por el steering y
    # por la regla T201 de ruff): se escribe directamente al stream.
    def log(message: str) -> None:
        sys.stderr.write(message + "\n")

    if args.samples < 1:
        parser.error("--samples debe ser >= 1.")

    try:
        _ensure_backend_on_path()
        from sentiment.prompts.indificadores import INDIFICADORES

        sample = build_sample(args.samples, INDIFICADORES)
        log(
            f"Muestra sintética construida: {len(sample)} ítems "
            f"(0 PII; textos genéricos de clima laboral)."
        )

        if args.dry_run:
            log("Modo --dry-run: NO se invoca a Bedrock; se simula la curva.")
            results = simulate_results(args.concurrencies)
        else:
            adapter = _build_adapter(model_id=args.model_id, region=args.region, log=log)
            analyzable = sum(1 for it in sample if not is_empty_marker(it.respuesta))
            log(
                f"Calibrando contra Bedrock: {analyzable} ítems analizables, "
                f"concurrencias={list(args.concurrencies)}."
            )
            results = run_calibration(adapter, sample, args.concurrencies, log=log)

        proposal = build_proposal(results)
        _print_proposal(proposal, log)

        if args.write_config:
            path = config_path(args.env)
            current = load_config(path)
            updated = apply_proposal(current, proposal)
            write_config(path, updated)
            log(f"Configuración escrita en {path} (--write-config).")
        else:
            log(
                "Modo de solo lectura: use --write-config para persistir la "
                f"propuesta en infraestructura/config/{args.env}.json."
            )
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    except Exception as exc:  # frontera del CLI: reporta y sale sin traza cruda.
        log(f"ERROR: la calibración falló: {exc.__class__.__name__}: {exc}")
        return 1

    return 0


def _build_adapter(
    *, model_id: str | None, region: str | None, log: Callable[[str], None]
) -> BedrockAdapter:
    """Construye el adaptador real de Bedrock, fijando model_id/región del entorno.

    Reutiliza ``BedrockAdapter`` del backend —con sus timeouts explícitos y cero
    reintentos del SDK— para que la medición use el mismo camino de código que la
    Lambda ``analyze_llm``. El ``model_id`` y la región se inyectan en el entorno
    del proceso (no se hardcodean) antes de instanciar el adaptador y reconstruir
    los clientes de boto3 con la región elegida.

    Args:
        model_id: modelo a usar; si es ``None``, se usa el del entorno/SSM.
        region: región de AWS; si es ``None``, se usa la del entorno/perfil.
        log: función de registro de metadatos.

    Returns:
        El :class:`BedrockAdapter` listo para invocar.
    """
    import os

    from sentiment.adapters import aws_clients
    from sentiment.adapters.bedrock_adapter import BedrockAdapter

    if region:
        os.environ["AWS_REGION"] = region
        os.environ["AWS_DEFAULT_REGION"] = region
    if model_id:
        os.environ["MODEL_ID"] = model_id

    # Reconstruye los clientes singleton con la región ya fijada (los singletons
    # se crearon al importar el módulo con la región previa del entorno).
    aws_clients.reset_clients()
    log("Adaptador de Bedrock construido (timeouts explícitos, 0 reintentos SDK).")
    return BedrockAdapter()


__all__ = [
    "SampleItem",
    "Measurement",
    "LatencyStats",
    "ConcurrencyResult",
    "ConcurrencyProposal",
    "percentile",
    "summarize_latencies",
    "estimate_batch_time",
    "estimate_total_duration",
    "propose_fanout_concurrency",
    "propose_reserved_concurrency",
    "build_proposal",
    "config_path",
    "load_config",
    "apply_proposal",
    "write_config",
    "build_sample",
    "is_empty_marker",
    "simulate_results",
    "build_arg_parser",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
