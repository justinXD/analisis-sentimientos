"""Métricas EMF personalizadas del Sistema (criterio 14 y tokens del 7.11).

Expone una fábrica del objeto :class:`~aws_lambda_powertools.metrics.Metrics` de
AWS Lambda Powertools, que emite métricas en el formato EMF (Embedded Metric
Format) de CloudWatch sin necesidad de llamadas síncronas a la API de métricas:
Powertools las escribe como JSON estructurado en el log y CloudWatch las extrae.

Las métricas de negocio del Sistema (los nombres son estables porque las alarmas
del ``observability_stack`` dependen de ellos):

- ``ItemsProcessed`` / ``ItemsFailed`` / ``ItemsSkipped``: desenlace de los Ítems.
- ``BedrockThrottles``: throttling del Modelo_Bedrock.
- ``BedrockInputTokens`` / ``BedrockOutputTokens``: tokens por invocación
  (criterio 7.11); se registra 0 cuando el modelo no informa un recuento.
- ``StuckAnalyses``: Lotes atascados detectados por el sweeper (se publica incluso
  cuando vale 0, criterio del Reconcile_Stuck).

Ninguna métrica incluye PII (criterio 7.11): solo recuentos y etiquetas. Como el
resto de observabilidad, este módulo no lee configuración obligatoria del entorno
al importarse, por lo que es seguro importarlo en el ámbito de módulo de un
handler y en el recorrido de importabilidad del paquete.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final

from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit

__all__ = [
    "MetricName",
    "MetricUnit",
    "get_metrics",
    "record_bedrock_tokens",
]

#: Namespace de CloudWatch bajo el que se agrupan todas las métricas del Sistema.
_DEFAULT_NAMESPACE: Final = "SentimentPipeline"


class MetricName(str, Enum):
    """Nombres estables de las métricas de negocio.

    Se centralizan en un enum para que productores (handlers/servicios) y
    consumidores (alarmas del ``observability_stack``) compartan una única fuente
    de verdad y un nombre mal escrito se detecte en la verificación de tipos.
    """

    ITEMS_PROCESSED = "ItemsProcessed"
    ITEMS_FAILED = "ItemsFailed"
    ITEMS_SKIPPED = "ItemsSkipped"
    BEDROCK_THROTTLES = "BedrockThrottles"
    BEDROCK_INPUT_TOKENS = "BedrockInputTokens"
    BEDROCK_OUTPUT_TOKENS = "BedrockOutputTokens"
    STUCK_ANALYSES = "StuckAnalyses"


def get_metrics(namespace: str | None = None, **kwargs: Any) -> Metrics:
    """Crea el objeto :class:`Metrics` de Powertools para emitir EMF.

    :param namespace: namespace de CloudWatch; si es ``None`` se usa
        ``POWERTOOLS_METRICS_NAMESPACE`` (inyectado por la Infraestructura) o el
        valor por defecto del Sistema.
    :param kwargs: argumentos adicionales reenviados a :class:`Metrics`.
    :return: el objeto de métricas listo para registrar valores.
    """
    return Metrics(namespace=namespace or _DEFAULT_NAMESPACE, **kwargs)


def record_bedrock_tokens(
    metrics: Metrics,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    """Registra los tokens de entrada y salida de una invocación a Bedrock.

    Cumple el criterio 7.11: siempre publica ambas métricas y usa 0 para el
    recuento que el Modelo_Bedrock no informe (``None``). No incluye PII: solo
    cardinalidades.

    :param metrics: el objeto de métricas de Powertools sobre el que registrar.
    :param input_tokens: tokens de entrada informados por el modelo, o ``None``.
    :param output_tokens: tokens de salida informados por el modelo, o ``None``.
    """
    metrics.add_metric(
        name=MetricName.BEDROCK_INPUT_TOKENS.value,
        unit=MetricUnit.Count,
        value=float(input_tokens or 0),
    )
    metrics.add_metric(
        name=MetricName.BEDROCK_OUTPUT_TOKENS.value,
        unit=MetricUnit.Count,
        value=float(output_tokens or 0),
    )
