"""Handler ``Notify_Email``: notificación por correo (SES) de fin de proceso.

Este módulo es el adaptador de transporte *thin* (diseño sección 2.1) que una
regla del Bus_Eventos invoca cuando se publica un Evento_Finalizacion
(``detail-type = analysis.completed``, criterio 13.1), en paralelo al handler
:mod:`~sentiment.handlers.notify_webhook`. Su responsabilidad es, cuando el Lote
declaró ``metadata.notify_email``, enviar un correo con un **enlace al endpoint
de descarga** del resultado consolidado (criterio 13.4).

Flujo
-----
#. Parsear el ``detail`` del evento de EventBridge a una estructura tipada
   (:class:`EmailCompletedEvent`), validando los campos mínimos (``analysis_id``,
   ``tenant_id``).
#. Resolver el destinatario (``notify_email``) a través de un puerto inyectable
   (:class:`EmailTargetResolver`). El correo es **PII** (criterio 15.1) y, por
   contrato, **nunca** viaja en el ``detail`` del evento (criterio 13.3): se
   resuelve del perfil/meta del Lote, igual que el resolutor de destino del
   webhook resuelve la URL.
#. Componer el correo con un **enlace** a ``GET /v1/analyses/{analysis_id}/download``
   (criterio 13.4), nunca una Presigned_URL embebida: el endpoint de descarga
   re-autentica al cliente (ownership + estado terminal, servicio
   ``issue_download_url``); el correo solo apunta allí.
#. Enviar con SES ``SendEmail`` usando un **configuration set** dedicado para el
   seguimiento de rebotes y quejas (criterio 13.10): se pasa ``ConfigurationSetName``.

Mejor esfuerzo: SES nunca bloquea el pipeline (criterio 13.8)
-------------------------------------------------------------
A diferencia del webhook —que **propaga** el fallo para que el *redrive*
asíncrono lo lleve a la DLQ_Notificaciones—, la notificación por correo es de
**mejor esfuerzo**. El registro META en la Dynamo_Table es la única fuente de
verdad del estado del Lote (criterio 13.9) y ya está en su estado terminal antes
de que esta Lambda se invoque; un fallo de SES **no debe** alterar ese estado ni
hacer fallar al consumidor del evento. Por eso el handler **captura** cualquier
error de SES, lo registra (sin PII) y retorna con gracia sin relanzar: un correo
fallido jamás falla el Lote ni el consumidor del evento (criterio 13.8).

Un evento malformado (:class:`EmailEventError`) sí se propaga: no es un fallo de
entrega de SES, sino un contrato de evento roto que no se arregla reintentando y
que debe hacerse visible.

Privacidad: sin PII en logs (criterios 15.1, 15.2)
--------------------------------------------------
El ``notify_email`` es PII. Esta Lambda **nunca** registra la dirección completa
ni el cuerpo del correo junto al destinatario: para trazabilidad se registra una
versión **enmascarada** del correo (ver :func:`mask_email`: solo el primer
carácter del usuario y el dominio, p. ej. ``a***@ejemplo.com``). Además, el
logger del proyecto redacta por completo cualquier campo llamado ``email``
(:data:`~sentiment.observability.logging.REDACTED_FIELDS`) como red de seguridad,
por lo que la dirección enmascarada se emite bajo una clave que no es ``email``.

Construcción por contenedor (criterios 9.18, 17.11)
---------------------------------------------------
El resolutor de destinatario y el emisor de SES se construyen una vez a nivel de
módulo (patrón de *singleton* perezoso, como los demás handlers) y se reutilizan
entre invocaciones calientes. Las variables de entorno (``SES_CONFIGURATION_SET``,
``SES_FROM_ADDRESS``, ``DOWNLOAD_URL_BASE``) se leen de forma **perezosa** en el
momento del envío, no a nivel de módulo, para no romper la importabilidad del
paquete (criterio 17.7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, cast, runtime_checkable

from sentiment.adapters.aws_clients import ses_client
from sentiment.domain.errors import SentimentError
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "EmailCompletedEvent",
    "EmailTarget",
    "EmailTargetResolver",
    "EmailMessage",
    "EmailEventError",
    "EmailSender",
    "SesEmailSender",
    "mask_email",
    "build_download_link",
    "compose_email",
    "parse_event",
    "lambda_handler",
]

logger = get_logger(service="notify_email")

#: ``detail-type`` que esta Lambda espera recibir (criterio 13.1). La regla del
#: Bus_Eventos ya filtra por él; se revalida como defensa en profundidad.
_EXPECTED_DETAIL_TYPE: Final = "analysis.completed"

#: Variables de entorno que inyecta la Infraestructura (tarea 13.4). Se leen de
#: forma perezosa en el envío, nunca a nivel de módulo (criterio 17.7).
_ENV_CONFIGURATION_SET: Final = "SES_CONFIGURATION_SET"
_ENV_FROM_ADDRESS: Final = "SES_FROM_ADDRESS"
_ENV_DOWNLOAD_URL_BASE: Final = "DOWNLOAD_URL_BASE"

#: Asunto y plantillas del correo. El cuerpo contiene exclusivamente el
#: ``analysis_id`` (identificador, no PII) y el enlace al endpoint de descarga;
#: nunca el resultado, la Presigned_URL ni ninguna ``respuesta``/``explicacion``.
_SUBJECT_TEMPLATE: Final = "Tu análisis de sentimiento {analysis_id} ha finalizado"
_BODY_TEMPLATE: Final = (
    "El análisis {analysis_id} ha finalizado con estado {status}.\n\n"
    "Puedes descargar el resultado consolidado desde:\n"
    "{download_link}\n\n"
    "El enlace requiere autenticación: inicia sesión para descargar el resultado."
)

#: Juego de caracteres del correo.
_CHARSET: Final = "UTF-8"


class EmailEventError(SentimentError):
    """El evento recibido no es un Evento_Finalizacion procesable.

    Error interno de control de flujo (``http_status`` es ``None``: no cruza la
    frontera HTTP). Lo lanza :func:`parse_event` cuando el ``detail`` del evento
    no tiene la forma esperada (falta ``detail-type``, ``detail`` no es un objeto,
    o faltan ``analysis_id``/``tenant_id``). No es transitorio: reintentar con el
    mismo evento malformado no lo arregla. A diferencia de un fallo de SES
    (mejor esfuerzo, criterio 13.8), este error **sí** se propaga, porque denota
    un contrato de evento roto que debe hacerse visible, no una entrega fallida.
    """

    error_code = "EMAIL_EVENT_INVALID"
    http_status = None
    is_transient = False


@dataclass(frozen=True, slots=True)
class EmailCompletedEvent:
    """Vista tipada del ``detail`` de un evento ``analysis.completed`` (sección 9.3).

    Contiene exclusivamente identificadores y punteros: por contrato (criterio
    13.3) el ``detail`` **nunca** incluye el ``notify_email`` (es PII) ni ninguna
    Presigned_URL. El destinatario se resuelve aparte, vía
    :class:`EmailTargetResolver`.

    Attributes:
        analysis_id: ULID del Lote (identificador, no PII); forma el enlace de
            descarga.
        tenant_id: Tenant propietario; determina de qué perfil/meta se resuelve
            el ``notify_email``.
        status: estado terminal del Lote; se incluye en el cuerpo del correo.
        correlation_id: ``correlation_id`` de la solicitud, si el ``detail`` lo
            trae; cadena vacía si no. Se registra en los logs del intento.
    """

    analysis_id: str
    tenant_id: str
    status: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class EmailTarget:
    """Destinatario de la notificación por correo de un Lote.

    Resuelto por un :class:`EmailTargetResolver` desde el perfil/meta del Lote,
    porque el correo es PII y no viaja en el evento (criterios 13.3, 15.1).

    Attributes:
        email: dirección de correo del destinatario (``metadata.notify_email``).
            Es PII: nunca se registra en claro (ver :func:`mask_email`).
    """

    email: str


@runtime_checkable
class EmailTargetResolver(Protocol):
    """Puerto de resolución del destinatario de correo de un Lote.

    Se expresa como un ``Protocol`` local (no en ``domain/ports.py``) porque es
    específico de este caso de uso, igual que :class:`WebhookTargetResolver`. La
    implementación real resolverá el ``notify_email`` desde el META del Lote
    (``pk = TENANT#{tenant_id}#ANALYSIS#{analysis_id}``, ``sk = META``); las
    pruebas inyectan un doble en memoria.
    """

    def resolve(self, event: EmailCompletedEvent) -> EmailTarget | None:
        """Devuelve el destinatario del Lote, o ``None`` si no hay correo.

        :param event: el Evento_Finalizacion ya parseado.
        :return: el :class:`EmailTarget` del Lote, o ``None`` si el Lote no
            declaró ``metadata.notify_email`` (en cuyo caso no hay nada que
            enviar y el handler termina sin acción).
        """
        ...


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Correo compuesto, listo para enviar por SES.

    El :attr:`body` y el :attr:`subject` contienen solo el ``analysis_id``, el
    estado y el enlace al endpoint de descarga; nunca el resultado ni ninguna
    Presigned_URL (criterios 13.3, 13.4).

    Attributes:
        sender: dirección ``From`` verificada en SES (``SES_FROM_ADDRESS``).
        recipient: dirección del destinatario (PII; no se registra en claro).
        subject: asunto del correo.
        body: cuerpo de texto del correo, con el enlace de descarga.
        configuration_set: configuration set de SES para rebotes/quejas
            (criterio 13.10).
    """

    sender: str
    recipient: str
    subject: str
    body: str
    configuration_set: str


@runtime_checkable
class EmailSender(Protocol):
    """Puerto de envío de correo (criterio 13.4).

    Abstrae SES para que las pruebas inyecten un doble en memoria sin tocar AWS.
    La implementación real (:class:`SesEmailSender`) usa ``SendEmail`` con el
    configuration set de rebotes/quejas.
    """

    def send(self, message: EmailMessage) -> None:
        """Envía el correo compuesto.

        :param message: el correo a enviar.
        :raises Exception: cualquier fallo del proveedor de correo. El handler lo
            captura y no lo propaga (mejor esfuerzo, criterio 13.8).
        """
        ...


class _SendEmailClient(Protocol):
    """Superficie mínima del cliente de SES que este módulo consume.

    Se tipa contra este ``Protocol`` en lugar de ``botocore.client.BaseClient``,
    que no expone ``send_email`` sin los *extras* por servicio de ``boto3-stubs``.
    El singleton del adaptador se estrecha a esta superficie con un ``cast``,
    igual que el resto de adaptadores del proyecto.
    """

    def send_email(
        self,
        *,
        Source: str,  # noqa: N803
        Destination: Mapping[str, Any],  # noqa: N803
        Message: Mapping[str, Any],  # noqa: N803
        ConfigurationSetName: str,  # noqa: N803
    ) -> Mapping[str, Any]:
        """Envía un correo con SES ``SendEmail`` (firma de la API de AWS)."""
        ...


class SesEmailSender:
    """Emisor de correo basado en Amazon SES ``SendEmail`` (criterios 13.4, 13.10).

    Usa el cliente de SES del adaptador de clientes a nivel de módulo (cero
    reintentos del SDK, criterio 9.18) y pasa el ``ConfigurationSetName`` para que
    SES registre los eventos de rebote y queja en el configuration set dedicado
    (criterio 13.10).

    No implementa reintentos ni *backoff*: la notificación es de mejor esfuerzo
    (criterio 13.8); si SES falla, el handler lo captura y termina sin relanzar.
    """

    def send(self, message: EmailMessage) -> None:
        """Envía el correo con SES ``SendEmail``.

        :param message: el correo compuesto a enviar.
        :raises Exception: propaga cualquier error del SDK de SES; el handler lo
            captura (mejor esfuerzo, criterio 13.8). Nunca incluye el cuerpo ni la
            dirección en claro en el mensaje de error que registre el llamador.
        """
        # El singleton se tipa como ``BaseClient`` (stubs sin extras por servicio),
        # que no expone ``send_email`` estáticamente; se estrecha al Protocol que
        # este módulo realmente usa.
        client = cast("_SendEmailClient", ses_client())
        client.send_email(
            Source=message.sender,
            Destination={"ToAddresses": [message.recipient]},
            Message={
                "Subject": {"Data": message.subject, "Charset": _CHARSET},
                "Body": {"Text": {"Data": message.body, "Charset": _CHARSET}},
            },
            ConfigurationSetName=message.configuration_set,
        )


def mask_email(email: str) -> str:
    """Enmascara una dirección de correo para registrarla sin exponer PII.

    Conserva solo el primer carácter de la parte local y el dominio completo, de
    modo que el log sea útil para correlacionar sin revelar la dirección
    (criterios 15.1, 15.2). Ejemplos:

    * ``ana.perez@ejemplo.com`` -> ``a***@ejemplo.com``
    * ``x@ejemplo.com`` -> ``*@ejemplo.com``
    * una cadena sin ``@`` -> ``***`` (no se asume estructura de correo).

    :param email: la dirección a enmascarar.
    :return: la versión enmascarada, segura para loguear.
    """
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain:
        # No es una dirección con la forma esperada: no se arriesga a filtrar nada.
        return "***"
    return f"{local[0]}***@{domain}"


def build_download_link(base_url: str, analysis_id: str) -> str:
    """Construye el enlace al endpoint de descarga (criterio 13.4).

    El enlace apunta a ``GET /v1/analyses/{analysis_id}/download``; **no** es una
    Presigned_URL. El endpoint re-autentica al cliente antes de emitir la descarga
    (servicio ``issue_download_url``: ownership + estado terminal), por lo que el
    correo puede contener el enlace sin exponer el resultado.

    :param base_url: base del endpoint de descarga (``DOWNLOAD_URL_BASE``), p. ej.
        ``https://api.ejemplo.com/v1/analyses``. Se tolera una ``/`` final.
    :param analysis_id: ULID del Lote.
    :return: la URL completa del endpoint de descarga del Lote.
    """
    return f"{base_url.rstrip('/')}/{analysis_id}/download"


def _require_env(name: str) -> str:
    """Lee una variable de entorno requerida, de forma perezosa.

    :param name: nombre de la variable de entorno.
    :return: el valor recortado, nunca vacío.
    :raises EmailEventError: si la variable falta o está en blanco. Se trata como
        un error de configuración del envío; el handler lo captura como parte del
        mejor esfuerzo (criterio 13.8), de modo que una Lambda mal configurada no
        tumba el consumidor del evento.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raise EmailEventError(f"Falta la configuración requerida '{name}'.")
    return raw.strip()


def compose_email(event: EmailCompletedEvent, target: EmailTarget) -> EmailMessage:
    """Compone el correo de notificación con el enlace de descarga (criterio 13.4).

    Lee de forma perezosa ``SES_FROM_ADDRESS``, ``DOWNLOAD_URL_BASE`` y
    ``SES_CONFIGURATION_SET`` del entorno (criterio 17.7: nada a nivel de módulo).
    El cuerpo contiene el enlace al endpoint de descarga, no una Presigned_URL.

    :param event: el Evento_Finalizacion parseado (aporta ``analysis_id`` y estado).
    :param target: el destinatario resuelto (PII; no se registra en claro).
    :return: el :class:`EmailMessage` listo para enviar.
    :raises EmailEventError: si falta alguna variable de entorno requerida.
    """
    sender = _require_env(_ENV_FROM_ADDRESS)
    download_base = _require_env(_ENV_DOWNLOAD_URL_BASE)
    configuration_set = _require_env(_ENV_CONFIGURATION_SET)

    download_link = build_download_link(download_base, event.analysis_id)
    subject = _SUBJECT_TEMPLATE.format(analysis_id=event.analysis_id)
    body = _BODY_TEMPLATE.format(
        analysis_id=event.analysis_id,
        status=event.status or "FINALIZADO",
        download_link=download_link,
    )
    return EmailMessage(
        sender=sender,
        recipient=target.email,
        subject=subject,
        body=body,
        configuration_set=configuration_set,
    )


def parse_event(event: Mapping[str, Any]) -> EmailCompletedEvent:
    """Parsea un evento de EventBridge a :class:`EmailCompletedEvent`.

    Valida la forma mínima del evento entregado por el Bus_Eventos: el
    ``detail-type`` debe ser ``analysis.completed`` y el ``detail`` debe ser un
    objeto con al menos ``analysis_id`` y ``tenant_id``. No valida PII (el
    contrato del evento ya la excluye); solo comprueba estructura.

    :param event: el evento de EventBridge recibido por la Lambda.
    :return: la vista tipada del ``detail``.
    :raises EmailEventError: si el ``detail-type`` no es el esperado, si
        ``detail`` no es un objeto, o si faltan ``analysis_id``/``tenant_id``.
    """
    detail_type = event.get("detail-type")
    if detail_type != _EXPECTED_DETAIL_TYPE:
        raise EmailEventError(
            "El evento no es un 'analysis.completed'.",
            detail_type=str(detail_type),
        )

    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise EmailEventError("El 'detail' del evento no es un objeto JSON.")

    analysis_id = detail.get("analysis_id")
    tenant_id = detail.get("tenant_id")
    if not isinstance(analysis_id, str) or not analysis_id:
        raise EmailEventError("El 'detail' no contiene un 'analysis_id' válido.")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise EmailEventError(
            "El 'detail' no contiene un 'tenant_id' válido.",
            analysis_id=analysis_id,
        )

    status = detail.get("status")
    correlation_id = detail.get("correlation_id")
    return EmailCompletedEvent(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        status=status if isinstance(status, str) else "",
        correlation_id=correlation_id if isinstance(correlation_id, str) else "",
    )


# Resolutor de destinatario y emisor de SES construidos UNA vez por contenedor
# (criterios 9.18, 17.11). El resolutor por defecto es `None` y lo cablea la
# Infraestructura (META del Lote en la Dynamo_Table) o las pruebas; el emisor por
# defecto usa SES con el configuration set de rebotes/quejas.
_RESOLVER: EmailTargetResolver | None = None
_SENDER: EmailSender = SesEmailSender()


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, str]:
    """Notifica por correo el fin de proceso con enlace de descarga (13.4, 13.8, 13.10).

    Adaptador *thin*: parsea el evento, resuelve el destinatario (PII, del META del
    Lote), compone el correo con el enlace a ``GET /v1/analyses/{analysis_id}/download``
    (criterio 13.4) y lo envía por SES con el configuration set de rebotes/quejas
    (criterio 13.10).

    **Mejor esfuerzo (criterio 13.8):** cualquier fallo de SES (o de configuración
    del envío) se captura, se registra sin PII y se devuelve con gracia sin
    relanzar, de modo que un correo fallido nunca falla el Lote ni el consumidor
    del evento. El registro META de la Dynamo_Table conserva su estado terminal
    (criterio 13.9). Contrasta con el webhook, que **sí** propaga a la DLQ.

    :param event: evento de EventBridge con ``detail-type = analysis.completed``.
    :param context: contexto de ejecución de la Lambda. No se usa.
    :return: un ``dict`` de metadatos del resultado (``status`` y ``analysis_id``),
        sin PII.
    :raises EmailEventError: si el evento no es un Evento_Finalizacion válido
        (contrato de evento roto). Un fallo de SES **no** se propaga.

    Postcondiciones:
        - Si no hay resolutor cableado, no actúa y devuelve ``status = "unconfigured"``.
        - Si el Lote no declaró ``metadata.notify_email`` (resolutor devuelve
          ``None``), no envía y devuelve ``status = "skipped"``.
        - Si SES o la configuración del envío fallan, devuelve ``status = "failed"``
          sin relanzar (mejor esfuerzo, criterio 13.8).
        - En caso de envío correcto, devuelve ``status = "sent"``.
        - El valor de retorno y los logs contienen solo identificadores y el correo
          enmascarado, nunca el correo en claro ni el cuerpo del mensaje.
    """
    completed = parse_event(event)

    resolver = _RESOLVER
    if resolver is None:
        # Sin resolutor cableado por la Infraestructura (META del Lote), no hay
        # destinatario que resolver. Se registra y se termina sin acción.
        logger.info(
            "email_resolver_not_configured",
            extra={
                "analysis_id": completed.analysis_id,
                "tenant_id": completed.tenant_id,
                "status": completed.status,
            },
        )
        return {"status": "unconfigured", "analysis_id": completed.analysis_id}

    target = resolver.resolve(completed)
    if target is None:
        logger.info(
            "email_not_declared",
            extra={
                "analysis_id": completed.analysis_id,
                "tenant_id": completed.tenant_id,
            },
        )
        return {"status": "skipped", "analysis_id": completed.analysis_id}

    # A partir de aquí, cualquier fallo de composición/envío es de mejor esfuerzo
    # (criterio 13.8): se captura, se registra sin PII y se termina con gracia.
    try:
        message = compose_email(completed, target)
        _SENDER.send(message)
    except Exception:
        # No se relanza: un correo fallido nunca falla el Lote ni el consumidor
        # del evento (criterio 13.8). No se registra la excepción con su traza
        # (podría acarrear la dirección); solo metadatos y el correo enmascarado.
        logger.warning(
            "email_notification_failed",
            extra={
                "analysis_id": completed.analysis_id,
                "tenant_id": completed.tenant_id,
                "status": completed.status,
                "correlation_id": completed.correlation_id,
                # Correo enmascarado bajo una clave que NO es ``email`` para que
                # el redactor del logger no lo sustituya por completo, pero sin
                # exponer la dirección en claro.
                "recipient_masked": mask_email(target.email),
            },
        )
        return {"status": "failed", "analysis_id": completed.analysis_id}

    logger.info(
        "email_notification_sent",
        extra={
            "analysis_id": completed.analysis_id,
            "tenant_id": completed.tenant_id,
            "status": completed.status,
            "correlation_id": completed.correlation_id,
            "recipient_masked": mask_email(target.email),
        },
    )
    return {"status": "sent", "analysis_id": completed.analysis_id}
