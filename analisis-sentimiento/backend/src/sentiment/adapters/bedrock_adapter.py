"""Adaptador del Modelo_Bedrock: implementación del puerto ``LlmAnalyzer``.

Evoluciona el ``LLMModel`` legado (que vivía aquí tras la tarea 1.4) hacia el
adaptador de infraestructura que implementa el puerto
:class:`~sentiment.domain.ports.LlmAnalyzer` (tarea 6.3). El cambio estructural
clave respecto del código legado es doble:

* **Una sola invocación, sin reintentos internos (criterios 7.1, 7.3, 9.18).**
  Desaparece el bucle ``for attempt in range(max_retries)`` con ``time.sleep``
  del ``invoke_llm`` legado. El adaptador emite **exactamente una** llamada al
  modelo y propaga cualquier error de dependencia en su primera ocurrencia. La
  política de reintentos vive en la Maquina_De_Estados (``Retry`` declarativo por
  tipo de error), no aquí; un bucle en la Lambda pagaría cómputo facturado
  durmiendo y escondería cada intento del historial de la ejecución.

* **Salida total: nunca ``None`` (criterio 7.12).** El ``invoke_llm`` legado
  devolvía ``None`` al agotar los reintentos y obligaba al llamador a adivinar.
  Este adaptador devuelve **siempre** un :class:`~sentiment.domain.models.EmotionAnalysis`
  válido (vía :func:`~sentiment.domain.llm_parsing.parse_llm_output`, que es
  total) o lanza una excepción tipada del Catalogo_De_Errores.

Decisiones de diseño:

* **API ``Converse`` (criterio 7.1, diseño 14.2).** Se usa la API unificada
  ``converse`` del cliente de Bedrock Runtime en lugar de construir el cuerpo de
  ``invoke_model`` a mano. Se conserva el truco de *prefill* del ``assistant``
  (``{"role": "assistant", "content": [{"text": "```json"}]}``) que ya usaba el
  código legado para sesgar la salida hacia JSON; forzar el esquema vía
  ``toolConfig`` se evalúa en v1.1 (Out of Scope 7).

* **``model_id`` perezoso y validado (criterios 7.9, 7.15).** Se resuelve desde
  :func:`sentiment.observability.config.model_id`, que lee de la variable de
  entorno o de Parameter_Store y lanza :class:`ConfigError` si falta o está en
  blanco. Nunca se lee a nivel de módulo.

* **Cliente singleton reutilizado (criterios 9.18, 17.11).** El cliente de boto3
  proviene de :func:`sentiment.adapters.aws_clients.bedrock_runtime_client`,
  construido una vez por contenedor con ``total_max_attempts=1`` (cero reintentos
  del SDK) y los timeouts de Bedrock.

* **Tokens como métrica, sin PII (criterio 7.11).** Los tokens de entrada/salida
  se leen del campo ``usage`` de la respuesta de ``Converse`` y se publican con
  :func:`sentiment.observability.metrics.record_bedrock_tokens` (0 cuando el
  modelo no los informa). Ni ``respuesta`` ni ``explicacion`` ni ningún fragmento
  del texto llegan a los logs ni a las métricas: solo metadatos (longitudes,
  recuentos, el tipo de error).

* **Mapeo de errores al Catalogo_De_Errores (criterios 9.16, 9.17).** El
  throttling de Bedrock se traduce a :class:`ThrottledError` (transitorio); los
  5xx, timeouts y fallos de red a :class:`TransientDependencyError`
  (transitorio); el rechazo de la entrada (``ValidationException``) a
  :class:`LlmInputRejectedError` (permanente, ``LLM_INPUT_REJECTED``). El nombre
  de la clase es lo que la Maquina_De_Estados compara en ``ErrorEquals`` para
  decidir el reintento.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from botocore.exceptions import BotoCoreError, ClientError

from sentiment.adapters.aws_clients import bedrock_runtime_client
from sentiment.domain.errors import (
    LlmInputRejectedError,
    ThrottledError,
    TransientDependencyError,
)
from sentiment.domain.llm_parsing import parse_llm_output
from sentiment.observability.config import model_id as get_model_id
from sentiment.observability.logging import get_logger
from sentiment.observability.metrics import (
    MetricName,
    MetricUnit,
    get_metrics,
    record_bedrock_tokens,
)
from sentiment.prompts.system_prompt import build_system_prompt

if TYPE_CHECKING:
    from aws_lambda_powertools import Metrics

    from sentiment.domain.models import EmotionAnalysis

__all__ = ["BedrockAdapter", "ConverseClient"]

logger = get_logger(service="analyze_llm")

# Parámetros de inferencia del Modelo_Bedrock. Se conservan los mismos valores
# que usaba el código legado (``maxTokens``, ``topP``, ``temperature``) para no
# alterar el comportamiento del modelo durante la migración de arquitectura.
# ``topK`` no es un parámetro de ``inferenceConfig`` de la API ``Converse``: los
# modelos que lo soportan lo reciben por ``additionalModelRequestFields``.
_INFERENCE_CONFIG: Final[dict[str, Any]] = {
    "maxTokens": 10000,
    "topP": 0.9,
    "temperature": 0.2,
}
_ADDITIONAL_MODEL_FIELDS: Final[dict[str, Any]] = {"inferenceConfig": {"topK": 20}}

# Prefijo de *prefill* del ``assistant`` que sesga la salida hacia un bloque JSON,
# idéntico al truco del código legado. El parseo total posterior tolera de todas
# formas prosa, fences y envoltura en array (criterio 7.4), por lo que esto es
# una optimización, no un requisito de corrección.
_ASSISTANT_PREFILL: Final = "```json"

# Códigos de error de ``ClientError`` que Bedrock devuelve como throttling o
# límite de tasa (clase transitoria, criterio 9.16). El nombre completo que
# botocore expone en ``Error.Code`` es, p. ej., ``ThrottlingException``.
_THROTTLING_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"ThrottlingException", "TooManyRequestsException"}
)

# Códigos de ``ClientError`` que indican una condición transitoria distinta del
# throttling: 5xx de la dependencia o agotamiento del plazo de invocación del
# modelo (criterio 9.16).
_TRANSIENT_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)

# Códigos de ``ClientError`` que indican que el modelo rechaza la entrada por su
# tamaño o por no ser procesable (clase permanente, ``LLM_INPUT_REJECTED``,
# criterio 9.17).
_INPUT_REJECTED_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"ValidationException"}
)


class ConverseClient(Protocol):
    """Vista tipada de la única operación de Bedrock Runtime que usa el adaptador.

    El meta-paquete ``boto3-stubs`` sin los *extras* por servicio tipa
    ``boto3.client("bedrock-runtime")`` como :class:`botocore.client.BaseClient`,
    que no expone ``converse``. En lugar de desactivar la verificación de tipos
    con ``# type: ignore`` o de usar ``getattr`` sin tipo, se declara aquí la
    firma mínima que el adaptador necesita y se estrecha el cliente a este
    protocolo. Así ``mypy --strict`` verifica la llamada y el adaptador documenta
    exactamente qué método del servicio consume (mismo patrón de encapsulación de
    llamadas por servicio que describe ``aws_clients.py``).

    Es un :class:`typing.Protocol` (tipado estructural): el cliente real de boto3
    lo satisface por tener el método ``converse``, y los dobles de prueba lo
    satisfacen sin heredar de nada, lo que permite inyectar un ``converse`` falso
    sin simular todo ``BaseClient``.
    """

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        """Envía mensajes al Modelo_Bedrock por la API ``Converse``."""
        ...


class BedrockAdapter:
    """Implementación del puerto :class:`~sentiment.domain.ports.LlmAnalyzer`.

    Encapsula una invocación al Modelo_Bedrock por la API ``Converse`` para
    clasificar las emociones de una respuesta de encuesta. No contiene lógica de
    reintento ni de persistencia: el servicio ``analyze_item_emotions`` orquesta
    la idempotencia y la escritura, y la Maquina_De_Estados orquesta el reintento.
    """

    def __init__(self, client: ConverseClient | None = None) -> None:
        """Construye el adaptador reutilizando el cliente singleton de Bedrock.

        El ``model_id`` y el ``system_prompt`` **no** se resuelven aquí: el
        ``model_id`` se lee de forma perezosa y validada en cada invocación
        (criterios 7.9, 7.15), y el ``system_prompt`` se construye una vez por
        instancia (es estable para todos los Ítems de un Lote, criterio 7.12).

        :param client: cliente de Bedrock Runtime a usar; por defecto, el
            singleton reutilizado entre invocaciones calientes
            (:func:`~sentiment.adapters.aws_clients.bedrock_runtime_client`),
            estrechado al protocolo :class:`ConverseClient`. Se inyecta en las
            pruebas para sustituirlo por un doble que implemente ``converse``.
        """
        self._client: ConverseClient = client or cast(
            "ConverseClient", bedrock_runtime_client()
        )
        self._metrics: Metrics = get_metrics()
        self._system_prompt: str = build_system_prompt()

    def analyze(self, indificador: str, respuesta: str) -> EmotionAnalysis:
        """Analiza una respuesta con una ÚNICA invocación del modelo (criterio 7.1).

        Resuelve el ``model_id`` (criterio 7.9), emite una sola llamada a la API
        ``Converse`` (criterio 7.3), publica los tokens de entrada/salida como
        métrica (criterio 7.11) y transforma el texto crudo en un
        :class:`EmotionAnalysis` total vía
        :func:`~sentiment.domain.llm_parsing.parse_llm_output` (criterio 7.12).

        El puerto no recibe ``item_id``: el :class:`EmotionAnalysis` devuelto lo
        lleva vacío y es el servicio llamante quien reconstruye el resultado con
        el ``item_id`` correcto (diseño 16.3). Los metadatos de normalización y de
        tokens sí quedan poblados aquí.

        :param indificador: categoría de la respuesta; miembro de la lista única
            de Indificadores (no es PII).
        :param respuesta: texto de la respuesta, ya validado y normalizado. Es
            dato sensible (PII): nunca se registra en logs ni en métricas.
        :return: un :class:`EmotionAnalysis` válido y normalizado; **nunca**
            ``None`` (criterio 7.12).
        :raises sentiment.observability.config.ConfigError: si ``MODEL_ID`` falta
            o está en blanco (criterio 7.15); se propaga sin invocar al modelo.
        :raises ThrottledError: si Bedrock limita la petición (transitorio, 9.16).
        :raises TransientDependencyError: ante 5xx, timeout del modelo o fallo de
            red de Bedrock (transitorio, 9.16).
        :raises LlmInputRejectedError: si Bedrock rechaza la entrada por tamaño o
            por no ser procesable (permanente, ``LLM_INPUT_REJECTED``, 9.17).
        :raises LlmSchemaError: si la salida del modelo no cumple el JSON_Schema
            ``llm_output`` (permanente, ``LLM_OUTPUT_INVALID``, 9.17); la lanza
            :func:`parse_llm_output`.
        """
        # Criterio 7.9/7.15: resolución perezosa y validada del model_id. Si falta
        # o está en blanco, ConfigError sube SIN invocar al modelo.
        model_id = get_model_id()

        started = time.monotonic()
        response = self._invoke(model_id, respuesta)
        latency_ms = (time.monotonic() - started) * 1000.0

        raw_text = self._extract_text(response)
        input_tokens, output_tokens = self._extract_usage(response)

        # Criterio 7.11: tokens como métrica, 0 si el modelo no los informa; sin
        # PII. Se publican SIEMPRE que hubo invocación, aun si el parseo falla
        # después, porque la invocación consumió tokens de todas formas.
        record_bedrock_tokens(
            self._metrics,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        # Log de solo metadatos: nunca el texto (PII). La longitud de la salida y
        # la latencia son diagnósticas y seguras.
        logger.info(
            "llm_invocation_succeeded",
            extra={
                "model_id": model_id,
                "indificador": indificador,
                "output_length": len(raw_text),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": round(latency_ms, 2),
            },
        )

        # Criterio 7.12/7.13: parseo total. Devuelve un EmotionAnalysis válido o
        # lanza LlmSchemaError (permanente). El item_id lo fija el servicio
        # llamante; aquí se deja vacío por no formar parte del contrato del puerto.
        return parse_llm_output(
            raw_text,
            item_id="",
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _invoke(self, model_id: str, respuesta: str) -> dict[str, Any]:
        """Emite la ÚNICA llamada a la API ``Converse`` y mapea errores (9.18).

        No reintenta: el cliente de boto3 ya está configurado con cero reintentos
        del SDK y aquí no hay ningún bucle. Traduce las excepciones de boto3 a las
        clases del Catalogo_De_Errores según su clase transitoria/permanente.

        :param model_id: identificador del Modelo_Bedrock ya resuelto y validado.
        :param respuesta: texto a analizar (PII); nunca se registra.
        :return: el diccionario de respuesta de la API ``Converse``.
        :raises ThrottledError: throttling de Bedrock (transitorio).
        :raises TransientDependencyError: 5xx, timeout o fallo de red (transitorio).
        :raises LlmInputRejectedError: entrada rechazada por el modelo (permanente).
        """
        messages = [
            {"role": "user", "content": [{"text": respuesta}]},
            {"role": "assistant", "content": [{"text": _ASSISTANT_PREFILL}]},
        ]
        try:
            response: dict[str, Any] = self._client.converse(
                modelId=model_id,
                messages=messages,
                system=[{"text": self._system_prompt}],
                inferenceConfig=_INFERENCE_CONFIG,
                additionalModelRequestFields=_ADDITIONAL_MODEL_FIELDS,
            )
        except ClientError as exc:
            raise self._map_client_error(exc, model_id) from exc
        except BotoCoreError as exc:
            # Fallos de conexión/red (connect/read timeout, conexión rehusada o
            # reiniciada): clase transitoria (criterio 9.16). Solo se registra el
            # tipo del error, nunca el texto enviado.
            logger.warning(
                "llm_invocation_transient_failure",
                extra={"model_id": model_id, "error_type": exc.__class__.__name__},
            )
            raise TransientDependencyError(
                "bedrock_connection_failure", error_type=exc.__class__.__name__
            ) from exc
        return response

    def _map_client_error(self, exc: ClientError, model_id: str) -> Exception:
        """Traduce un ``ClientError`` de Bedrock a una excepción del catálogo.

        :param exc: la excepción de boto3 capturada.
        :param model_id: el modelo invocado, solo para el log de metadatos.
        :return: la excepción tipada del dominio a lanzar (no la lanza; devuelve
            la instancia para que el llamador use ``raise ... from exc``).
        """
        error_code = exc.response.get("Error", {}).get("Code", "")
        logger.warning(
            "llm_invocation_failed",
            extra={
                "model_id": model_id,
                "error_type": exc.__class__.__name__,
                "aws_error_code": error_code,
            },
        )
        if error_code in _THROTTLING_ERROR_CODES:
            # Métrica de throttles del Modelo_Bedrock (criterio 14.6).
            self._metrics.add_metric(
                name=MetricName.BEDROCK_THROTTLES.value,
                unit=MetricUnit.Count,
                value=1.0,
            )
            return ThrottledError("bedrock_throttled", aws_error_code=error_code)
        if error_code in _INPUT_REJECTED_ERROR_CODES:
            return LlmInputRejectedError(
                "bedrock_input_rejected", aws_error_code=error_code
            )
        # 5xx, timeouts del modelo y cualquier otro ClientError no clasificado se
        # tratan como transitorios: es más seguro reintentar (la Maquina_De_Estados
        # acota el número de reintentos) que fallar de forma permanente ante un
        # error de servicio recuperable.
        return TransientDependencyError(
            "bedrock_service_error", aws_error_code=error_code
        )

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        """Extrae el texto de la respuesta de ``Converse`` de forma defensiva.

        La estructura esperada es
        ``response["output"]["message"]["content"][*]["text"]``. Se concatenan
        todos los bloques de tipo texto por si el modelo fragmenta la salida. Si
        la estructura no trae ningún bloque de texto, se devuelve la cadena vacía:
        el parseo total posterior la convertirá en :class:`LlmSchemaError`
        (criterio 7.13), de modo que nunca se propaga un ``KeyError`` crudo.

        :param response: el diccionario de respuesta de la API ``Converse``.
        :return: el texto crudo concatenado del modelo, o ``""`` si no hay texto.
        """
        try:
            content = response["output"]["message"]["content"]
        except (KeyError, TypeError):
            return ""
        if not isinstance(content, list):
            return ""
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "".join(parts)

    @staticmethod
    def _extract_usage(response: dict[str, Any]) -> tuple[int, int]:
        """Lee los tokens de entrada/salida del campo ``usage`` de la respuesta.

        La API ``Converse`` devuelve ``usage.inputTokens`` y ``usage.outputTokens``.
        Cualquier recuento ausente o no entero se trata como 0 (criterio 7.11), de
        modo que esta función es total y nunca lanza.

        :param response: el diccionario de respuesta de la API ``Converse``.
        :return: la tupla ``(input_tokens, output_tokens)``.
        """
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return 0, 0
        return (
            _as_count(usage.get("inputTokens")),
            _as_count(usage.get("outputTokens")),
        )


def _as_count(value: object) -> int:
    """Normaliza un recuento de tokens a un entero no negativo.

    :param value: valor crudo del campo de tokens (``int`` esperado, pero se
        tolera cualquier tipo).
    :return: el valor como ``int`` no negativo, o 0 si no es un entero válido.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    return 0
