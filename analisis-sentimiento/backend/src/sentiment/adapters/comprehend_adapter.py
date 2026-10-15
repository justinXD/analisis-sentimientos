"""Adaptador de Amazon Comprehend: implementación del puerto ``SentimentDetector``.

Evolución del ``ComprehendClass`` legado (migrado en las tareas 1.2 y 1.4) hacia
la implementación del puerto :class:`~sentiment.domain.ports.SentimentDetector`
(tarea 6.2). El cambio sustituye las dos llamadas *por ítem* del código original
(``detect_dominant_language`` + ``detect_sentiment``) por las dos operaciones
**batch** de Comprehend, lo que reduce 25x el número de llamadas, la latencia
agregada y el riesgo de throttling (diseño 11.4).

Dos operaciones batch
---------------------
* :meth:`ComprehendAdapter.detect_languages` usa ``batch_detect_dominant_language``
  en **una** llamada para todo el batch. Es *total y conservadora de longitud*:
  devuelve exactamente un ``language_code`` por texto de entrada, en el mismo
  orden, con *fallback* a ``"es"`` para cualquier texto cuyo idioma Comprehend no
  determine (criterios 6.1 y 6.14). Esta postcondición de longitud es la base de
  la conservación de ítems de la capa batch (Property 10).
* :meth:`ComprehendAdapter.detect_sentiments` emite **una** llamada
  ``batch_detect_sentiment`` para hasta 25 textos de un mismo idioma (criterio
  6.2) y asocia cada resultado por el campo ``Index`` que devuelve Comprehend,
  **nunca** por la posición dentro de ``ResultList`` (criterio 6.8). Devuelve un
  diccionario de éxitos y otro de errores, ambos indexados por ese ``Index``.

Detalle crítico de las APIs batch de AWS: ``ResultList`` y ``ErrorList`` traen un
campo ``Index`` que apunta a la posición en ``TextList``, y **no** se garantiza
``len(ResultList) == len(TextList)``. Asumir esa igualdad —o reconciliar por
posición en la lista— es el bug clásico de estas integraciones (diseño 11.4). Por
eso toda la asociación de este adaptador es por ``Index``.

Frontera del puerto
-------------------
El puerto expone *solo* ``texts`` (no los ``item_id``): la asociación de cada
resultado con su Ítem es responsabilidad del servicio llamante
(``AnalyzeBatchSentimentService``, tarea 7.3), que conoce la correspondencia
posición → Ítem. Por eso los :class:`~sentiment.domain.models.ClassicSentiment`
que este adaptador construye llevan ``item_id = ""``: es un marcador que el
servicio reemplaza al asociar por ``Index``. El adaptador no inventa identidad de
Ítem que no recibe.

Clasificación de errores (criterios 6.7, 6.15)
----------------------------------------------
* Un Ítem que aparece en el ``ErrorList`` de Comprehend es un fallo **permanente**
  del Ítem (texto demasiado largo, idioma no soportado): se reporta como
  ``COMPREHEND_ITEM_FAILED`` en el diccionario de errores, no como excepción.
* Un fallo de la **llamada entera** (indisponibilidad, throttling, timeout) se
  propaga como :class:`~sentiment.domain.errors.TransientDependencyError` o
  :class:`~sentiment.domain.errors.ThrottledError` (clase transitoria): la
  Maquina_De_Estados decide el reintento, no este adaptador (criterio 9.18). Al
  propagar, ningún Ítem del batch recibe resultado parcial (criterio 6.15).

PII y logging (criterios 15.1, 15.2, 17.7)
------------------------------------------
Ningún ``print`` y ningún texto de ``respuesta`` llega a los logs: ni ``TextList``
ni la respuesta cruda de Comprehend (que reproduce el texto enviado) se registran.
Solo se emiten metadatos no sensibles: tamaños de batch, códigos de idioma y
recuentos de resultados/errores.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from sentiment.adapters.aws_clients import comprehend_client
from sentiment.domain.errors import ThrottledError, TransientDependencyError
from sentiment.domain.models import (
    ClassicSentiment,
    SentimentScores,
    SentimientoGeneral,
)
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from botocore.client import BaseClient

__all__ = [
    "COMPREHEND_ITEM_FAILED",
    "DEFAULT_LANGUAGE_CODE",
    "MAX_BATCH_SIZE",
    "ComprehendAdapter",
]

logger = get_logger(service="analyze_comprehend")

#: Idioma de *fallback* que se asigna a un texto cuando ``batch_detect_dominant_language``
#: no devuelve ningún idioma para él (criterio 6.14). El Sistema analiza encuestas
#: en español, por lo que ``"es"`` es el supuesto más seguro.
DEFAULT_LANGUAGE_CODE = "es"

#: Tope de textos por llamada ``batch_detect_sentiment`` que impone Comprehend y
#: que respeta el ``ItemBatcher`` del Distributed_Map (criterios 6.2, 6.11).
MAX_BATCH_SIZE = 25

#: ``error_code`` del Catalogo_De_Errores con el que se reporta un Ítem presente
#: en el ``ErrorList`` de Comprehend (fallo permanente del Ítem, criterio 6.7).
COMPREHEND_ITEM_FAILED = "COMPREHEND_ITEM_FAILED"

#: Cuantización de los scores de sentimiento a 6 dígitos decimales (criterio 6.9).
_SCORE_QUANTUM = Decimal("0.000001")

#: Códigos de error de Comprehend (campo ``Code`` de ``ClientError``) que denotan
#: una condición **transitoria** reintentable por la Maquina_De_Estados
#: (criterio 9.16). El resto de ``ClientError`` se trata como transitorio genérico
#: salvo el throttling, que se eleva a :class:`ThrottledError`.
_THROTTLING_ERROR_CODES = frozenset(
    {"ThrottlingException", "TooManyRequestsException"}
)


class ComprehendAdapter:
    """Implementación de :class:`~sentiment.domain.ports.SentimentDetector` sobre Comprehend.

    Encapsula las dos operaciones batch de Amazon Comprehend que el análisis
    clásico necesita. No crea su propio cliente de boto3: reutiliza el singleton a
    nivel de módulo de :mod:`sentiment.adapters.aws_clients`, construido una sola
    vez por contenedor con cero reintentos del SDK y los timeouts de Comprehend
    (criterios 9.18, 17.11). El cliente se inyecta opcionalmente para facilitar
    las pruebas de contrato con ``moto`` (tarea 6.8).
    """

    def __init__(self, client: BaseClient | None = None) -> None:
        """Inicializa el adaptador con el cliente de Comprehend.

        :param client: cliente de Comprehend a usar. Si es ``None`` (caso de
            producción), se toma el singleton compartido de
            :func:`sentiment.adapters.aws_clients.comprehend_client`, que
            reutiliza la conexión TLS entre invocaciones calientes. El parámetro
            existe para inyectar un cliente simulado en las pruebas.
        """
        self._client: BaseClient = client if client is not None else comprehend_client()

    # ------------------------------------------------------------------
    # Puerto SentimentDetector
    # ------------------------------------------------------------------

    def detect_languages(self, texts: list[str]) -> list[str]:
        """Detecta el idioma dominante de cada texto en una única llamada batch.

        Implementa :meth:`sentiment.domain.ports.SentimentDetector.detect_languages`.
        Usa ``batch_detect_dominant_language`` una sola vez para todo el batch
        (criterio 6.1) y reconcilia la respuesta por el campo ``Index``, no por la
        posición en ``ResultList``.

        :param texts: textos a analizar, en el orden original del batch.
        :return: lista de ``language_code`` (ISO 639-1), uno por texto de entrada,
            en el mismo orden. Para cada texto se toma el idioma de mayor ``Score``
            (criterio 6.1); todo texto cuyo idioma no se pudo determinar —porque
            está en el ``ErrorList``, porque falta su ``Index`` en la respuesta o
            porque su lista de idiomas viene vacía— recibe
            :data:`DEFAULT_LANGUAGE_CODE` (criterio 6.14).
        :raises TransientDependencyError: si la llamada entera falla por una
            condición transitoria (indisponibilidad, red, 5xx).
        :raises ThrottledError: si Comprehend limita la tasa de la llamada.

        Postcondición: ``len(resultado) == len(texts)``. La salida nunca omite ni
        duplica posiciones ni produce ``None`` (base de la Property 10).
        """
        # El batch vacío no justifica una llamada de red: devuelve la lista vacía,
        # preservando trivialmente la postcondición de longitud.
        if not texts:
            return []

        response = self._call(
            "batch_detect_dominant_language",
            operation="batch_detect_dominant_language",
            batch_size=len(texts),
            TextList=texts,
        )

        # Reconciliación por Index: se parte de la lista de fallback y solo se
        # sobrescriben las posiciones para las que Comprehend devolvió un idioma.
        # Así, un Index ausente de ResultList (p. ej. presente en ErrorList)
        # conserva el fallback "es" (criterio 6.14) sin ramas adicionales.
        languages: list[str] = [DEFAULT_LANGUAGE_CODE] * len(texts)
        for result in response.get("ResultList", []):
            index = result.get("Index")
            if index is None or not 0 <= index < len(texts):
                continue
            detected = self._top_language(result.get("Languages", []))
            if detected is not None:
                languages[index] = detected

        logger.info(
            "comprehend_languages_detected",
            extra={
                "batch_size": len(texts),
                "result_count": len(response.get("ResultList", [])),
                "error_count": len(response.get("ErrorList", [])),
            },
        )
        return languages

    def detect_sentiments(
        self, texts: list[str], language_code: str
    ) -> tuple[dict[int, ClassicSentiment], dict[int, str]]:
        """Analiza el sentimiento de hasta 25 textos del mismo idioma en una llamada batch.

        Implementa :meth:`sentiment.domain.ports.SentimentDetector.detect_sentiments`.
        Emite una única llamada ``batch_detect_sentiment`` (criterio 6.2) y asocia
        cada resultado por el campo ``Index`` que devuelve Comprehend, no por la
        posición en ``ResultList`` (criterio 6.8).

        :param texts: hasta 25 textos, todos en el idioma ``language_code``.
        :param language_code: código ISO 639-1 compartido por todos los textos.
        :return: una tupla ``(exitos, errores)`` donde ``exitos`` mapea el
            ``Index`` de cada texto analizado con éxito a su
            :class:`~sentiment.domain.models.ClassicSentiment`, y ``errores``
            mapea el ``Index`` de cada texto del ``ErrorList`` de Comprehend al
            ``error_code`` :data:`COMPREHEND_ITEM_FAILED` (criterio 6.7). Las
            claves de ambos diccionarios son disjuntas. La unión de las claves
            **puede** no cubrir ``range(len(texts))``: un ``Index`` que Comprehend
            no devuelva en ninguna lista queda fuera, y es el servicio quien lo
            clasifica como ``COMPREHEND_RESULT_MISSING`` (criterio 6.13).
        :raises TransientDependencyError: si la llamada entera falla por una
            condición transitoria; en ese caso ningún Ítem recibe resultado
            parcial (criterio 6.15).
        :raises ThrottledError: si Comprehend limita la tasa de la llamada.

        Nota sobre ``item_id``: el puerto no recibe los ``item_id`` del batch, por
        lo que cada ``ClassicSentiment`` se construye con ``item_id = ""``. El
        servicio llamante conoce la correspondencia ``Index`` → Ítem y asigna el
        ``item_id`` real al asociar los resultados.
        """
        successes: dict[int, ClassicSentiment] = {}
        errors: dict[int, str] = {}

        # Batch vacío: nada que analizar, no se gasta una llamada de red.
        if not texts:
            return successes, errors

        response = self._call(
            "batch_detect_sentiment",
            operation="batch_detect_sentiment",
            batch_size=len(texts),
            language_code=language_code,
            TextList=texts,
            LanguageCode=language_code,
        )

        for result in response.get("ResultList", []):
            index = result.get("Index")
            if index is None:
                continue
            successes[index] = self._build_classic(language_code, result)

        for error in response.get("ErrorList", []):
            index = error.get("Index")
            if index is None:
                continue
            # Un Index no puede ser a la vez éxito y error en la respuesta de
            # Comprehend; el guard mantiene la postcondición de claves disjuntas
            # aun ante una respuesta anómala.
            if index in successes:
                continue
            errors[index] = COMPREHEND_ITEM_FAILED

        logger.info(
            "comprehend_sentiments_detected",
            extra={
                "language_code": language_code,
                "batch_size": len(texts),
                "success_count": len(successes),
                "error_count": len(errors),
            },
        )
        return successes, errors

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    @staticmethod
    def _top_language(languages: list[dict[str, Any]]) -> str | None:
        """Devuelve el ``LanguageCode`` de mayor ``Score`` de una lista de idiomas.

        :param languages: la lista ``Languages`` de una entrada de ``ResultList``
            de ``batch_detect_dominant_language``; cada elemento tiene
            ``LanguageCode`` y ``Score``.
        :return: el código del idioma de mayor confianza (criterio 6.1), o
            ``None`` si la lista está vacía o ningún elemento aporta un
            ``LanguageCode`` no vacío (el llamante aplica entonces el fallback).
        """
        best_code: str | None = None
        best_score = float("-inf")
        for language in languages:
            code = language.get("LanguageCode")
            if not code:
                continue
            score = language.get("Score", 0.0)
            if score > best_score:
                best_score = score
                best_code = code
        return best_code

    @classmethod
    def _build_classic(
        cls, language_code: str, result: dict[str, Any]
    ) -> ClassicSentiment:
        """Construye un :class:`ClassicSentiment` a partir de un resultado de Comprehend.

        Mapea la etiqueta ``Sentiment`` al enum en español vía
        :meth:`SentimientoGeneral.from_comprehend` —total: una etiqueta
        desconocida cae en ``No determinado`` (criterios 6.3, 6.4)— y convierte los
        cuatro scores a ``Decimal`` cuantizado a 6 decimales (criterios 6.9, 15.x:
        DynamoDB no admite ``float``).

        :param language_code: idioma del batch, que se persiste como el
            ``language_code`` del Ítem.
        :param result: una entrada de ``ResultList`` de ``batch_detect_sentiment``,
            con ``Sentiment`` y ``SentimentScore``.
        :return: el :class:`ClassicSentiment` resultante, con ``item_id = ""``
            (lo asigna el servicio llamante al asociar por ``Index``).
        """
        score_map = result.get("SentimentScore", {})
        scores = SentimentScores(
            positive=cls._to_score(score_map.get("Positive")),
            negative=cls._to_score(score_map.get("Negative")),
            neutral=cls._to_score(score_map.get("Neutral")),
            mixed=cls._to_score(score_map.get("Mixed")),
        )
        # La confianza del idioma no viene en la respuesta de sentimiento; el
        # análisis clásico la persiste a partir de la detección de idioma. En este
        # adaptador batch, el servicio reconcilia idioma y sentimiento, por lo que
        # el score de idioma se deja en 0 (se completa en la capa de servicio).
        return ClassicSentiment(
            item_id="",
            language_code=language_code,
            language_confidence=Decimal("0"),
            sentimiento_general=SentimientoGeneral.from_comprehend(
                result.get("Sentiment", "")
            ),
            scores=scores,
        )

    @staticmethod
    def _to_score(raw: float | None) -> Decimal:
        """Convierte un score de Comprehend a ``Decimal`` acotado a ``[0, 1]`` y 6 decimales.

        Cumple el criterio 6.9: cada score se persiste como un decimal entre 0 y 1
        redondeado a 6 dígitos. La conversión pasa por ``str`` para evitar el ruido
        binario de construir un ``Decimal`` desde un ``float``, y se cuantiza con
        redondeo bancario (``ROUND_HALF_EVEN``).

        :param raw: el score ``float`` devuelto por Comprehend, o ``None`` si la
            clave no vino en la respuesta.
        :return: el score como ``Decimal`` en ``[0, 1]`` con 6 decimales; ``0`` si
            ``raw`` es ``None``.
        """
        if raw is None:
            return Decimal("0.000000")
        value = Decimal(str(raw))
        # Acota defensivamente al rango documentado por si la API devolviera un
        # valor marginalmente fuera de [0, 1] por redondeo.
        if value < 0:
            value = Decimal("0")
        elif value > 1:
            value = Decimal("1")
        return value.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)

    def _call(
        self,
        method_name: str,
        *,
        operation: str,
        batch_size: int,
        language_code: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoca una operación de Comprehend traduciendo sus fallos a errores de dominio.

        Centraliza el manejo de errores de las dos operaciones batch para no
        duplicar la traducción ``ClientError``/``BotoCoreError`` → error de
        dominio ni la decisión transitorio/throttling (criterios 9.16, 9.18). Un
        fallo de la llamada entera se eleva a la clase transitoria; nunca se
        registra el ``TextList`` ni la respuesta cruda (PII, criterio 15.1).

        :param method_name: nombre del método del cliente de boto3 a invocar
            (``batch_detect_dominant_language`` o ``batch_detect_sentiment``).
        :param operation: nombre de la operación para el log de metadatos.
        :param batch_size: número de textos del batch, para el log de metadatos.
        :param language_code: idioma del batch, para el log (solo en sentimiento).
        :param kwargs: argumentos de la llamada a boto3 (``TextList`` y, en
            sentimiento, ``LanguageCode``).
        :return: la respuesta cruda de boto3 como diccionario.
        :raises ThrottledError: ante un ``ClientError`` de throttling (transitorio).
        :raises TransientDependencyError: ante cualquier otro fallo de la llamada
            (``ClientError`` no-throttling o ``BotoCoreError``: red, timeout, 5xx).
        """
        try:
            method = getattr(self._client, method_name)
            response: dict[str, Any] = method(**kwargs)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            # Metadatos del error, nunca el texto enviado (criterio 15.1).
            extra: dict[str, Any] = {
                "operation": operation,
                "batch_size": batch_size,
                "aws_error_code": error_code,
            }
            if language_code is not None:
                extra["language_code"] = language_code
            logger.warning("comprehend_call_failed", extra=extra)
            if error_code in _THROTTLING_ERROR_CODES:
                raise ThrottledError(
                    "comprehend throttled", operation=operation
                ) from exc
            raise TransientDependencyError(
                "comprehend call failed", operation=operation
            ) from exc
        except BotoCoreError as exc:
            # Fallos de red/timeout/configuración de botocore: clase transitoria.
            logger.warning(
                "comprehend_call_failed",
                extra={
                    "operation": operation,
                    "batch_size": batch_size,
                    "error_type": exc.__class__.__name__,
                },
            )
            raise TransientDependencyError(
                "comprehend call failed", operation=operation
            ) from exc
        return response
