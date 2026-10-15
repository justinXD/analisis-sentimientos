"""Logging estructurado con redacción de PII (criterios 15.1, 15.2, 15.5).

Este módulo centraliza la creación del logger JSON estructurado del Sistema y
garantiza que ningún campo PII llegue a CloudWatch Logs. La PII de este Sistema
es el texto de ``respuesta``, el de ``explicacion`` y las direcciones de correo
(ver el glosario de los requisitos), más los campos de texto crudo enviados a
Comprehend (``Text``, ``TextList``).

Capas de defensa:

1. **Redacción declarativa en origen.** :func:`get_logger` devuelve un
   :class:`~aws_lambda_powertools.logging.logger.Logger` de AWS Lambda Powertools
   configurado para emitir un valor de reemplazo en cualquier campo cuyo nombre
   pertenezca a :data:`REDACTED_FIELDS`. El reemplazo se aplica de forma recursiva
   sobre diccionarios y listas anidadas antes de serializar el registro.
2. **Helpers de metadatos.** :func:`log_item_processed` emite exclusivamente
   metadatos no sensibles (longitudes, códigos de idioma, etiquetas, recuentos),
   nunca el contenido de los textos, cumpliendo el criterio 15.2.
3. **Red de seguridad en Infraestructura.** La CDK aplica además una CloudWatch
   Logs Data Protection Policy con los identificadores gestionados ``EmailAddress``
   y ``Name``; esa capa vive fuera de este módulo (criterio 15.4).

Prohibido en todo el proyecto: ``print(event)``, ``print(response)`` y registrar
el payload completo (``logger.info(payload)``). La regla de lint ``T201`` del
Pipeline_CI impide reintroducir ``print`` (criterio 17.7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.logging.formatter import LambdaPowertoolsFormatter

if TYPE_CHECKING:
    from aws_lambda_powertools.logging.types import PowertoolsLogRecord

__all__ = [
    "REDACTED_FIELDS",
    "REDACTION_PLACEHOLDER",
    "get_logger",
    "redact",
    "log_item_processed",
]

#: Conjunto cerrado de nombres de campo que nunca deben aparecer con su valor en
#: un log. La comparación es exacta y sensible a mayúsculas y minúsculas, igual
#: que las claves que el Sistema realmente usa (``respuesta``/``explicacion`` en
#: el dominio; ``Text``/``TextList`` en las llamadas a Comprehend; ``email`` como
#: forma genérica de las direcciones de correo).
REDACTED_FIELDS: Final[frozenset[str]] = frozenset(
    {"respuesta", "explicacion", "email", "Text", "TextList"}
)

#: Valor con el que se sustituye un campo PII. No revela la longitud ni ningún
#: fragmento del contenido original.
REDACTION_PLACEHOLDER: Final = "[REDACTED]"

#: Servicio por defecto para el logger de Powertools. Puede sobrescribirse por
#: Lambda vía el argumento ``service`` o la variable de entorno
#: ``POWERTOOLS_SERVICE_NAME`` que inyecta la Infraestructura.
_DEFAULT_SERVICE: Final = "sentiment"


def redact(value: Any) -> Any:
    """Devuelve una copia de ``value`` con los campos PII enmascarados.

    Recorre recursivamente diccionarios y listas/tuplas. Cualquier entrada de un
    diccionario cuya clave pertenezca a :data:`REDACTED_FIELDS` se reemplaza por
    :data:`REDACTION_PLACEHOLDER`, sin inspeccionar su contenido. Los valores
    escalares (y los tipos no contenedores) se devuelven sin cambios.

    La función es pura: no muta la estructura recibida. Esto importa porque el
    mismo objeto suele seguir usándose para la lógica de negocio después de
    loguearlo.

    :param value: estructura arbitraria (dict, list, tuple o escalar) a sanear.
    :return: una estructura equivalente con los campos PII sustituidos.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTION_PLACEHOLDER
                if key in REDACTED_FIELDS
                else redact(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class _RedactingFormatter(LambdaPowertoolsFormatter):
    """Formateador de Powertools que redacta PII antes de serializar.

    Extiende el formateador JSON estándar de Powertools interceptando el
    diccionario del registro justo antes de volcarlo, de modo que la redacción se
    aplica tanto a los campos añadidos con ``extra=`` como a cualquier clave
    estructurada que un llamador haya incluido por error. Es la red de seguridad
    que hace que :data:`REDACTED_FIELDS` se respete aunque alguien olvide usar los
    helpers de este módulo.
    """

    def serialize(self, log: dict[str, Any] | PowertoolsLogRecord) -> str:
        """Serializa el registro de log con los campos PII ya enmascarados.

        :param log: el diccionario del registro construido por Powertools.
        :return: la representación JSON del registro, sin PII.
        """
        sanitized = redact(dict(log))
        return super().serialize(sanitized)


def get_logger(service: str | None = None, **kwargs: Any) -> Logger:
    """Crea un :class:`Logger` de Powertools con redacción de PII incorporada.

    El logger resultante emite JSON estructurado y aplica
    :class:`_RedactingFormatter`, por lo que ningún campo de
    :data:`REDACTED_FIELDS` puede salir con su valor, ni siquiera si se pasa por
    descuido en ``extra=``.

    No lee configuración obligatoria del entorno: puede construirse en el ámbito
    de módulo de cada handler sin romper la importabilidad del paquete.

    :param service: nombre del servicio para el campo ``service`` del log; si es
        ``None`` se usa ``POWERTOOLS_SERVICE_NAME`` o el valor por defecto.
    :param kwargs: argumentos adicionales reenviados a :class:`Logger`.
    :return: un logger estructurado listo para usarse.
    """
    return Logger(
        service=service or _DEFAULT_SERVICE,
        logger_formatter=_RedactingFormatter(),
        **kwargs,
    )


def log_item_processed(
    logger: Logger,
    *,
    analysis_id: str,
    item_id: str,
    indificador: str,
    respuesta_length: int,
    language_code: str | None = None,
    sentimiento_general: str | None = None,
    sentimientos_count: int | None = None,
    latency_ms: float | None = None,
    model_id: str | None = None,
) -> None:
    """Registra el procesamiento de un Ítem usando solo metadatos no sensibles.

    Cumple el criterio 15.2: se registra la **longitud** de la ``respuesta``, no
    su contenido, y nunca se incluye ``explicacion``, el correo ni el texto de los
    sentimientos. ``indificador`` es una categoría cerrada (no PII) y
    ``sentimiento_general`` es una etiqueta de un enum (no texto libre).

    :param logger: el logger estructurado sobre el que emitir el registro.
    :param analysis_id: ULID del Lote.
    :param item_id: identificador del Ítem (``{analysis_id}#{index:05d}``).
    :param indificador: categoría de la pregunta de encuesta (no PII).
    :param respuesta_length: longitud del texto de la respuesta, en caracteres.
    :param language_code: código de idioma detectado, si está disponible.
    :param sentimiento_general: etiqueta de sentimiento clásico, si está disponible.
    :param sentimientos_count: número de emociones detectadas, si está disponible.
    :param latency_ms: latencia de la operación en milisegundos, si procede.
    :param model_id: identificador del modelo usado, si procede.
    """
    metadata: dict[str, Any] = {
        "analysis_id": analysis_id,
        "item_id": item_id,
        "indificador": indificador,
        "respuesta_length": respuesta_length,
    }
    if language_code is not None:
        metadata["language_code"] = language_code
    if sentimiento_general is not None:
        metadata["sentimiento_general"] = sentimiento_general
    if sentimientos_count is not None:
        metadata["sentimientos_count"] = sentimientos_count
    if latency_ms is not None:
        metadata["latency_ms"] = latency_ms
    if model_id is not None:
        metadata["model_id"] = model_id

    logger.info("item_processed", extra=metadata)
