"""Handler ``Notify_Webhook``: lectura, firma y entrega del webhook.

Este módulo es el adaptador de transporte *thin* (diseño sección 2.1) que una
regla del Bus_Eventos invoca cuando se publica un Evento_Finalizacion
(``detail-type = analysis.completed``, criterio 13.1). El flujo completo es:

#. Parsear el ``detail`` del evento de EventBridge a una estructura tipada
   (:class:`AnalysisCompletedEvent`), validando que trae los campos mínimos.
#. Resolver la información de destino de la entrega (URL de webhook del Tenant y
   el identificador de su secreto HMAC) a través de un puerto inyectable
   (:class:`WebhookTargetResolver`).
#. Preparar la carga de la entrega HTTP (:class:`WebhookDelivery`): método, URL,
   cuerpo JSON (el propio Evento_Finalizacion, sin PII) y las cabeceras base.
#. **Firmar** la entrega (tarea 9.4): calcular ``X-Timestamp`` (epoch en
   segundos, criterio 13.7) y ``X-Signature`` (HMAC-SHA256 de ``timestamp.body``
   con el secreto del Tenant leído de Secrets_Manager, criterio 13.6).
#. **Entregar** por HTTPS con un ``POST`` con *timeout* explícito, hasta 3
   intentos con *backoff* exponencial y *jitter* completo (criterio 9.9).

Traslado a la DLQ_Notificaciones (criterios 9.9, 9.10)
------------------------------------------------------
Cuando los 3 intentos se agotan, el handler **lanza** :class:`WebhookDeliveryError`
en lugar de tragarse el fallo. La Lambda está configurada (diseño sección 10,
tabla de DLQs: ``dlq-notifications`` como destino asíncrono ``OnFailure``) para
que una invocación asíncrona fallida se redrija a la DLQ_Notificaciones **con el
evento original intacto**, que conserva el ``analysis_id`` y el ``correlation_id``
(criterio 9.9). Delegar el traslado en el *redrive* nativo del origen asíncrono
—en lugar de publicar nosotros a la cola— evita duplicar lógica de entrega y
mantiene el handler como adaptador *thin*. Un evento malformado
(:class:`WebhookEventError`, permanente) se propaga por el mismo camino.

Firma y seguridad del secreto (criterios 13.6, 13.7)
----------------------------------------------------
* La firma se calcula sobre ``f"{timestamp}.{body}"`` con el ``body`` serializado
  byte a byte en :func:`prepare_delivery` (orden de claves estable), de modo que
  es reproducible por el receptor. El valor del header es ``sha256=<hexdigest>``.
* Incluir el ``timestamp`` dentro de la cadena firmada ata la firma a un instante
  y permite al receptor rechazar entregas replay fuera de ventana.
* El secreto del Tenant se obtiene de Secrets_Manager por ``secret_id`` y se
  **cachea por contenedor** (criterios 9.18, 17.11): una entrada por ``secret_id``
  en memoria del proceso, reutilizada entre invocaciones calientes.
* **Nunca** se registra el secreto ni el valor de la firma; los logs se limitan a
  identificadores, recuentos y metadatos del intento.

Garantías de seguridad y privacidad
------------------------------------
* **Sin PII (criterios 13.3, 15.1, 15.2).** El cuerpo de la entrega es el
  ``detail`` del evento tal cual, que por contrato excluye ``respuesta``,
  ``explicacion`` y todo Presigned_URL. Los logs se limitan a identificadores y
  recuentos; nunca se registra la URL de webhook completa (es dato del Tenant) ni
  ningún secreto.
* **Solo HTTPS (criterios 13.5, 13.12).** La URL de webhook se valida en la
  ingesta contra ``^https://`` (criterio 3.11); como defensa en profundidad, la
  preparación rechaza cualquier URL que no sea HTTPS antes de preparar la entrega,
  y el emisor HTTP vuelve a rechazar cualquier esquema que no sea HTTPS antes de
  abrir la conexión.
* **Construcción por contenedor (criterios 9.18, 17.11).** El resolutor de
  destino, el proveedor de secretos y el emisor HTTP se construyen una vez a nivel
  de módulo y se reutilizan entre invocaciones en caliente.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets as _secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol, cast, runtime_checkable

from sentiment.adapters.aws_clients import secretsmanager_client
from sentiment.domain.errors import SentimentError
from sentiment.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AnalysisCompletedEvent",
    "WebhookTarget",
    "WebhookTargetResolver",
    "WebhookDelivery",
    "WebhookEventError",
    "WebhookDeliveryError",
    "WebhookSecretProvider",
    "SecretsManagerSecretProvider",
    "HttpWebhookSender",
    "sign_delivery",
    "prepare_delivery",
    "lambda_handler",
]

logger = get_logger(service="notify_webhook")

#: ``detail-type`` que esta Lambda espera recibir (criterio 13.1). La regla del
#: Bus_Eventos ya filtra por él; se revalida como defensa en profundidad.
_EXPECTED_DETAIL_TYPE: Final = "analysis.completed"

#: Esquema de URL exigido para toda entrega de webhook (criterios 13.5, 3.11).
_HTTPS_SCHEME: Final = "https://"

#: Método HTTP de la entrega del webhook (criterio 13.5).
_HTTP_METHOD: Final = "POST"

#: Prefijo del valor del header ``X-Signature`` (criterio 13.6). El receptor
#: puede negociar algoritmos por este prefijo; aquí siempre es HMAC-SHA256.
_SIGNATURE_PREFIX: Final = "sha256="

#: Nombres de los headers de firma (criterios 13.6, 13.7).
_SIGNATURE_HEADER: Final = "X-Signature"
_TIMESTAMP_HEADER: Final = "X-Timestamp"

#: Política de entrega (criterio 9.9): hasta 3 intentos con backoff exponencial
#: (base * factor^n) y jitter completo, más un timeout explícito por intento.
_MAX_ATTEMPTS: Final = 3
_BACKOFF_BASE_SECONDS: Final = 1.0
_BACKOFF_FACTOR: Final = 2.0
_REQUEST_TIMEOUT_SECONDS: Final = 5.0

#: Códigos HTTP 2xx que el emisor considera entrega satisfactoria.
_HTTP_OK_FLOOR: Final = 200
_HTTP_OK_CEIL: Final = 300


class WebhookEventError(SentimentError):
    """El evento recibido no es un Evento_Finalizacion procesable.

    Error interno de control de flujo (no cruza la frontera HTTP: ``http_status``
    es ``None``). Lo lanza :func:`parse_event` cuando el ``detail`` del evento no
    tiene la forma esperada (falta ``detail-type``, ``detail`` no es un objeto, o
    faltan ``analysis_id``/``tenant_id``). No es transitorio: reintentar con el
    mismo evento malformado no lo arregla, por lo que el llamador no debe
    reintentar (la tarea 9.4 lo trasladará a la DLQ_Notificaciones).
    """

    error_code = "WEBHOOK_EVENT_INVALID"
    http_status = None
    is_transient = False


class WebhookDeliveryError(SentimentError):
    """La entrega del webhook falló tras agotar los 3 intentos (criterio 9.9).

    Error interno de control de flujo (``http_status`` es ``None``: no cruza la
    frontera HTTP). Lo lanza :meth:`HttpWebhookSender.send` cuando los
    :data:`_MAX_ATTEMPTS` intentos con *backoff* se han agotado sin una respuesta
    2xx. Se marca como **transitorio** (:attr:`is_transient` ``True``) porque el
    receptor del webhook podría recuperarse: el reintento legítimo lo hace el
    *redrive* asíncrono de la Lambda hacia la DLQ_Notificaciones (criterios 9.9,
    9.10), no un bucle dentro del proceso. Propagar esta excepción es,
    precisamente, lo que dispara ese *redrive* conservando el evento original (y
    con él, ``analysis_id`` y ``correlation_id``).

    El detalle de la instancia nunca incluye el secreto, la firma ni la URL
    completa; solo identificadores y metadatos del intento.
    """

    error_code = "WEBHOOK_DELIVERY_FAILED"
    http_status = None
    is_transient = True


@dataclass(frozen=True, slots=True)
class AnalysisCompletedEvent:
    """Vista tipada del ``detail`` de un evento ``analysis.completed`` (sección 9.3).

    Contiene exclusivamente punteros e identificadores: por contrato (criterio
    13.3) el ``detail`` nunca incluye PII ni Presigned_URL. Se conserva el ``detail``
    crudo (:attr:`raw_detail`) porque es ese objeto, serializado byte a byte, el
    que constituye el cuerpo de la entrega y sobre el que la tarea 9.4 calculará
    la firma HMAC: reconstruirlo desde los campos tipados podría cambiar el orden
    de claves y, por tanto, la firma.

    Attributes:
        analysis_id: ULID del Lote (identificador, no PII).
        tenant_id: Tenant propietario; determina el destino y el secreto HMAC.
        status: estado terminal del Lote.
        event_id: identificador determinista de desduplicación
            (``{analysis_id}:{status}``, criterio 10.20).
        correlation_id: ``correlation_id`` de la solicitud, si el ``detail`` lo
            trae; cadena vacía si no. Se registra en los logs del intento y, en
            caso de agotar los reintentos, el *redrive* a la DLQ conserva el
            evento original que lo contiene (criterio 9.9).
        raw_detail: el objeto ``detail`` original, que será el cuerpo de la entrega.
    """

    analysis_id: str
    tenant_id: str
    status: str
    event_id: str
    correlation_id: str
    raw_detail: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    """Destino de entrega de un webhook para un Tenant.

    Resuelto por un :class:`WebhookTargetResolver`. En esta tarea solo se usa la
    URL para preparar la entrega; :attr:`secret_id` es el identificador del
    secreto HMAC en Secrets_Manager que la tarea 9.4 usará para firmar
    (criterio 13.6). Nunca se transporta aquí el valor del secreto, solo su
    identificador.

    Attributes:
        url: URL HTTPS a la que se entrega el Evento_Finalizacion (criterio 13.5).
        secret_id: identificador (ARN o nombre) del secreto HMAC del Tenant en
            Secrets_Manager; ``None`` si el Tenant no tiene secreto configurado.
    """

    url: str
    secret_id: str | None = None


@runtime_checkable
class WebhookTargetResolver(Protocol):
    """Puerto de resolución del destino de webhook de un Tenant.

    Se expresa como un ``Protocol`` local (no en ``domain/ports.py``) porque es
    específico de este caso de uso. La implementación real (tarea 9.4) resolverá
    el destino desde el perfil del Tenant/Lote (``pk = TENANT#{tenant_id}``,
    ``sk = PROFILE``; diseño 8.6) y el identificador del secreto HMAC en
    Secrets_Manager; las pruebas inyectan un doble en memoria.
    """

    def resolve(self, event: AnalysisCompletedEvent) -> WebhookTarget | None:
        """Devuelve el destino de webhook del Lote, o ``None`` si no hay webhook.

        :param event: el Evento_Finalizacion ya parseado.
        :return: el :class:`WebhookTarget` del Tenant, o ``None`` si el Lote no
            declaró ``metadata.webhook_url`` (en cuyo caso no hay nada que
            entregar y el handler termina sin acción).
        """
        ...


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """Carga preparada de una entrega de webhook, lista para firmar y enviar.

    Es el artefacto que produce esta tarea (9.3). La tarea 9.4 la completa
    añadiendo las cabeceras de firma con :meth:`with_signature_headers` y la envía
    con reintentos. El :attr:`body` es el cuerpo JSON ya serializado (bytes
    estables), para que la firma HMAC de 9.4 se calcule sobre exactamente lo que
    se envía.

    Attributes:
        method: método HTTP; siempre ``POST`` (criterio 13.5).
        url: URL HTTPS de destino.
        body: cuerpo de la petición, el ``detail`` del evento serializado a JSON.
        headers: cabeceras base (``Content-Type``), sin la firma HMAC todavía.
    """

    method: str
    url: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def with_signature_headers(self, signature_headers: Mapping[str, str]) -> WebhookDelivery:
        """Devuelve una copia con las cabeceras de firma añadidas.

        Único punto de inyección de la firma: :func:`sign_delivery` calcula
        ``X-Signature`` (HMAC-SHA256 del timestamp y :attr:`body`, criterio 13.6) y
        ``X-Timestamp`` (criterio 13.7) y las pasa aquí, sin recomputar el cuerpo.
        No muta la instancia original (``frozen``): devuelve una nueva.

        :param signature_headers: cabeceras de firma a fusionar (p. ej.
            ``X-Signature`` y ``X-Timestamp``).
        :return: una nueva :class:`WebhookDelivery` con las cabeceras combinadas.
        """
        merged = {**self.headers, **dict(signature_headers)}
        return WebhookDelivery(
            method=self.method,
            url=self.url,
            body=self.body,
            headers=merged,
        )


# --------------------------------------------------------------------------- #
# Proveedor del secreto HMAC (Secrets_Manager, cacheado por contenedor)       #
# --------------------------------------------------------------------------- #


class _GetSecretValueClient(Protocol):
    """Superficie mínima del cliente de Secrets_Manager que este módulo consume.

    Se tipa contra este ``Protocol`` en lugar de ``botocore.client.BaseClient``,
    que no expone ``get_secret_value`` sin los *extras* por servicio de
    ``boto3-stubs``. El singleton del adaptador se estrecha a esta superficie con
    un ``cast``, igual que el resto de adaptadores del proyecto.
    """

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]:  # noqa: N803
        """Devuelve el valor de un secreto de Secrets_Manager por su identificador."""
        ...


@runtime_checkable
class WebhookSecretProvider(Protocol):
    """Puerto de obtención del secreto HMAC de un Tenant (criterio 13.6).

    Abstrae el origen del secreto para que las pruebas inyecten un doble en
    memoria sin tocar Secrets_Manager. La implementación real
    (:class:`SecretsManagerSecretProvider`) lee el secreto por su identificador y
    lo cachea por contenedor.
    """

    def get_secret(self, secret_id: str) -> bytes:
        """Devuelve el material del secreto HMAC del Tenant en bytes.

        :param secret_id: identificador (ARN o nombre) del secreto en Secrets_Manager.
        :return: el secreto en bytes, listo para la clave HMAC.
        :raises WebhookDeliveryError: si el secreto no puede obtenerse.
        """
        ...


class SecretsManagerSecretProvider:
    """Lee el secreto HMAC del Tenant de Secrets_Manager y lo cachea por contenedor.

    La caché es un diccionario ``secret_id -> bytes`` en memoria del proceso: una
    sola llamada a ``GetSecretValue`` por ``secret_id`` por contenedor, reutilizada
    entre invocaciones calientes (criterios 9.18, 17.11). El cliente de
    Secrets_Manager se obtiene del adaptador de clientes a nivel de módulo.

    El valor del secreto **nunca** se registra; solo se cachea en memoria y se usa
    como clave HMAC.
    """

    def __init__(self) -> None:
        """Inicializa la caché vacía de secretos por ``secret_id``."""
        self._cache: dict[str, bytes] = {}

    def get_secret(self, secret_id: str) -> bytes:
        """Devuelve el secreto del Tenant, leyéndolo de Secrets_Manager una vez.

        :param secret_id: identificador del secreto HMAC del Tenant.
        :return: el secreto en bytes (``SecretString`` en UTF-8 o ``SecretBinary``).
        :raises WebhookDeliveryError: si Secrets_Manager falla o el secreto está
            vacío o ausente. Se trata como transitorio para que el *redrive* a la
            DLQ_Notificaciones lo recupere, sin exponer el motivo exacto en el log.
        """
        cached = self._cache.get(secret_id)
        if cached is not None:
            return cached

        # El singleton se tipa como ``BaseClient`` (stubs sin extras por servicio),
        # que no expone ``get_secret_value`` estáticamente; se estrecha al Protocol
        # que este módulo realmente usa.
        client = cast("_GetSecretValueClient", secretsmanager_client())
        try:
            response = client.get_secret_value(SecretId=secret_id)
        except Exception as exc:
            # Se normaliza cualquier fallo del SDK a un error del dominio.
            # No se registra el `secret_id` completo ni el detalle del error del
            # SDK (podría filtrar el ARN del secreto): solo que la lectura falló.
            raise WebhookDeliveryError(
                "No se pudo obtener el secreto del webhook de Secrets_Manager."
            ) from exc

        secret_string = response.get("SecretString")
        if isinstance(secret_string, str) and secret_string:
            secret = secret_string.encode("utf-8")
        else:
            secret_binary = response.get("SecretBinary")
            if isinstance(secret_binary, bytes | bytearray) and secret_binary:
                secret = bytes(secret_binary)
            else:
                raise WebhookDeliveryError(
                    "El secreto del webhook está vacío o tiene un formato inesperado."
                )

        self._cache[secret_id] = secret
        return secret


def sign_delivery(
    delivery: WebhookDelivery,
    secret: bytes,
    *,
    timestamp: int | None = None,
) -> WebhookDelivery:
    """Firma una entrega con HMAC-SHA256 y añade los headers de firma.

    Calcula ``X-Timestamp`` (epoch en segundos, criterio 13.7) y ``X-Signature``
    (criterio 13.6) como ``sha256=<hexdigest>``, donde el *hexdigest* es el
    HMAC-SHA256 de la cadena ``f"{timestamp}.{body}"`` usando ``secret`` como
    clave. Incluir el timestamp en la cadena firmada ata la firma a un instante y
    permite al receptor rechazar entregas replay.

    :param delivery: la entrega preparada (:attr:`WebhookDelivery.body` es la base
        de la firma, byte a byte).
    :param secret: el secreto HMAC del Tenant, en bytes.
    :param timestamp: epoch en segundos a usar; por defecto el instante actual.
        Parametrizable para pruebas deterministas.
    :return: una nueva :class:`WebhookDelivery` con ``X-Timestamp`` y ``X-Signature``.

    Postcondiciones:
        - El cuerpo no cambia: la firma se calcula sobre el :attr:`body` existente.
        - ``X-Signature`` tiene el prefijo ``sha256=`` y 64 dígitos hexadecimales.
    """
    ts = int(time.time()) if timestamp is None else timestamp
    signing_input = f"{ts}.".encode() + delivery.body
    digest = hmac.new(secret, signing_input, hashlib.sha256).hexdigest()
    return delivery.with_signature_headers(
        {
            _TIMESTAMP_HEADER: str(ts),
            _SIGNATURE_HEADER: f"{_SIGNATURE_PREFIX}{digest}",
        }
    )


# --------------------------------------------------------------------------- #
# Emisor HTTP con timeout, 3 intentos con backoff + jitter (criterio 9.9)     #
# --------------------------------------------------------------------------- #


class HttpWebhookSender:
    """Emisor HTTPS con *timeout*, 3 intentos y *backoff* exponencial con *jitter*.

    Implementa el envío de la entrega (criterio 13.5) con la política de
    resiliencia del criterio 9.9: hasta :data:`_MAX_ATTEMPTS` intentos, cada uno
    con un *timeout* explícito (:data:`_REQUEST_TIMEOUT_SECONDS`), y una espera
    entre intentos de ``base x factor^(n-1)`` segundos con *jitter* completo
    (``uniform(0, delay)``). Si todos los intentos fallan, lanza
    :class:`WebhookDeliveryError`, que propaga el handler para que el *redrive*
    asíncrono traslade el evento a la DLQ_Notificaciones (criterios 9.9, 9.10).

    Usa ``urllib.request`` de la biblioteca estándar para mantener la Lambda
    ligera (sin dependencias HTTP pesadas). Nunca registra la URL completa, el
    cuerpo, el secreto ni la firma: solo el número de intento y metadatos.

    El reintento es seguro: la entrega del webhook es idempotente desde la óptica
    del receptor (mismo ``event_id`` y misma firma; el consumidor desduplica por
    ``event_id``, criterio 10.20).
    """

    def __init__(
        self,
        *,
        max_attempts: int = _MAX_ATTEMPTS,
        base_delay: float = _BACKOFF_BASE_SECONDS,
        factor: float = _BACKOFF_FACTOR,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        sleep: Any = time.sleep,
    ) -> None:
        """Configura la política de reintentos y los puntos de inyección de pruebas.

        :param max_attempts: número total de intentos (criterio 9.9: 3).
        :param base_delay: intervalo base del *backoff* en segundos.
        :param factor: factor de crecimiento exponencial del *backoff*.
        :param timeout: *timeout* por intento, en segundos.
        :param sleep: función de espera inyectable (las pruebas pasan un *no-op*
            para no dormir de verdad).
        """
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._factor = factor
        self._timeout = timeout
        self._sleep = sleep

    def _attempt(self, delivery: WebhookDelivery) -> None:
        """Realiza un único ``POST`` HTTPS; lanza en error de transporte o HTTP no-2xx.

        :param delivery: la entrega firmada a enviar.
        :raises WebhookDeliveryError: si el esquema no es HTTPS o la respuesta no
            es 2xx (defensa en profundidad; no transporta detalle sensible).
        :raises urllib.error.URLError: ante un fallo de transporte, que el bucle
            de :meth:`send` captura y reintenta.
        """
        if not delivery.url.startswith(_HTTPS_SCHEME):
            # Defensa en profundidad: no abrir conexión a un esquema no HTTPS.
            raise WebhookDeliveryError("La URL de webhook no usa HTTPS.")

        # `urllib.request.Request` con un `data` no vacío y método explícito emite
        # un POST. La URL está validada como HTTPS justo arriba, por lo que no
        # abre esquemas locales (file://) ni inseguros (http://).
        request = urllib.request.Request(
            url=delivery.url,
            data=delivery.body,
            headers=delivery.headers,
            method=delivery.method,
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            status = int(getattr(response, "status", 0))
            if not (_HTTP_OK_FLOOR <= status < _HTTP_OK_CEIL):
                raise WebhookDeliveryError(
                    "El receptor del webhook respondió con un estado no 2xx.",
                    http_response_status=status,
                )

    def send(self, delivery: WebhookDelivery) -> None:
        """Entrega la carga con hasta 3 intentos y *backoff* exponencial con *jitter*.

        :param delivery: la entrega **ya firmada** a enviar.
        :raises WebhookDeliveryError: si los :data:`_MAX_ATTEMPTS` intentos se
            agotan sin una respuesta 2xx. El handler la propaga para activar el
            *redrive* a la DLQ_Notificaciones (criterios 9.9, 9.10).

        Postcondiciones:
            - Devuelve ``None`` en la primera respuesta 2xx.
            - No duerme después del último intento fallido.
        """
        last_status: int | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._attempt(delivery)
            except urllib.error.HTTPError as exc:
                # Respuesta HTTP con código de error: se trata como intento fallido.
                last_status = int(exc.code)
            except (urllib.error.URLError, TimeoutError, OSError):
                # Fallo de transporte o timeout: intento fallido, sin detalle.
                last_status = None
            except WebhookDeliveryError as exc:
                raw = exc.detail.get("http_response_status")
                last_status = raw if isinstance(raw, int) else None
            else:
                logger.info("webhook_delivered", extra={"attempt": attempt})
                return

            logger.warning(
                "webhook_delivery_attempt_failed",
                extra={
                    "attempt": attempt,
                    "max_attempts": self._max_attempts,
                    # Solo el código de estado (si lo hubo); nunca URL, cuerpo,
                    # secreto ni firma.
                    "response_status": last_status,
                },
            )
            if attempt < self._max_attempts:
                self._sleep(self._backoff_delay(attempt))

        raise WebhookDeliveryError(
            "La entrega del webhook agotó los reintentos.",
            attempts=self._max_attempts,
            last_response_status=last_status,
        )

    def _backoff_delay(self, attempt: int) -> float:
        """Calcula la espera con *backoff* exponencial y *jitter* completo.

        El *backoff* exponencial crece como ``base x factor^(attempt-1)`` y el
        *jitter* completo elige un valor uniforme en ``[0, delay]``. Esto reparte
        los reintentos en el tiempo y evita "tormentas de reintentos" sincronizadas
        contra un receptor que se está recuperando.

        :param attempt: número de intento ya realizado (1-based).
        :return: segundos a esperar antes del siguiente intento.
        """
        ceiling = self._base_delay * (self._factor ** (attempt - 1))
        # `secrets.SystemRandom.uniform` da jitter sin un PRNG predecible; el valor
        # no es un secreto, pero evita el aviso de uso de `random` no criptográfico.
        return _secrets.SystemRandom().uniform(0.0, ceiling)


# Resolutor de destino, proveedor de secretos y emisor construidos UNA vez por
# contenedor (criterios 9.18, 17.11). El resolutor por defecto es `None` y lo
# cablea la Infraestructura (perfil del Tenant en la Dynamo_Table) o las pruebas;
# el proveedor de secretos lee de Secrets_Manager con caché por contenedor, y el
# emisor HTTP aplica timeout, 3 intentos con backoff y jitter.
_RESOLVER: WebhookTargetResolver | None = None
_SECRET_PROVIDER: WebhookSecretProvider = SecretsManagerSecretProvider()
_SENDER: HttpWebhookSender = HttpWebhookSender()


def parse_event(event: Mapping[str, Any]) -> AnalysisCompletedEvent:
    """Parsea un evento de EventBridge a :class:`AnalysisCompletedEvent`.

    Valida la forma mínima del evento entregado por el Bus_Eventos: el
    ``detail-type`` debe ser ``analysis.completed`` y el ``detail`` debe ser un
    objeto con al menos ``analysis_id`` y ``tenant_id``. No valida PII (el
    contrato del evento ya la excluye); solo comprueba estructura.

    :param event: el evento de EventBridge recibido por la Lambda.
    :return: la vista tipada del ``detail``.
    :raises WebhookEventError: si el ``detail-type`` no es el esperado, si
        ``detail`` no es un objeto, o si faltan ``analysis_id``/``tenant_id``.
    """
    detail_type = event.get("detail-type")
    if detail_type != _EXPECTED_DETAIL_TYPE:
        raise WebhookEventError(
            "El evento no es un 'analysis.completed'.",
            detail_type=str(detail_type),
        )

    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise WebhookEventError("El 'detail' del evento no es un objeto JSON.")

    analysis_id = detail.get("analysis_id")
    tenant_id = detail.get("tenant_id")
    if not isinstance(analysis_id, str) or not analysis_id:
        raise WebhookEventError("El 'detail' no contiene un 'analysis_id' válido.")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise WebhookEventError(
            "El 'detail' no contiene un 'tenant_id' válido.",
            analysis_id=analysis_id,
        )

    status = detail.get("status")
    event_id = detail.get("event_id")
    correlation_id = detail.get("correlation_id")
    return AnalysisCompletedEvent(
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        status=status if isinstance(status, str) else "",
        event_id=event_id if isinstance(event_id, str) else f"{analysis_id}:",
        correlation_id=correlation_id if isinstance(correlation_id, str) else "",
        raw_detail=detail,
    )


def prepare_delivery(event: AnalysisCompletedEvent, target: WebhookTarget) -> WebhookDelivery:
    """Prepara la carga HTTP de la entrega del Evento_Finalizacion (criterio 13.5).

    Serializa el ``detail`` del evento como cuerpo JSON (sin PII, por contrato del
    evento) y arma las cabeceras base. La firma HMAC y la cabecera de timestamp se
    añaden en la tarea 9.4 vía :meth:`WebhookDelivery.with_signature_headers`.

    :param event: el Evento_Finalizacion parseado; su ``raw_detail`` es el cuerpo.
    :param target: el destino resuelto; su ``url`` debe ser HTTPS.
    :return: la :class:`WebhookDelivery` lista para firmar y enviar.
    :raises WebhookEventError: si la URL de destino no usa HTTPS (criterio 13.5,
        defensa en profundidad sobre la validación de ingesta del criterio 3.11).

    Postcondiciones:
        - El :attr:`WebhookDelivery.body` es el ``detail`` serializado byte a byte
          (orden de claves estable), de modo que la firma de 9.4 se calcula sobre
          exactamente lo que se enviará.
        - Las cabeceras base no incluyen aún ``X-Signature`` ni ``X-Timestamp``.
    """
    if not target.url.startswith(_HTTPS_SCHEME):
        raise WebhookEventError(
            "La URL de webhook no usa HTTPS.",
            analysis_id=event.analysis_id,
        )

    # El cuerpo es el `detail` serializado de forma determinista: `sort_keys`
    # fija el orden para que la firma HMAC (criterio 13.6) sea reproducible, y
    # `ensure_ascii=False` conserva los acentos de las etiquetas sin escaparlos.
    body = json.dumps(
        dict(event.raw_detail),
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")

    return WebhookDelivery(
        method=_HTTP_METHOD,
        url=target.url,
        body=body,
        headers={"Content-Type": "application/json"},
    )


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, str]:
    """Lee, firma y entrega el Evento_Finalizacion por webhook (criterios 13.5-13.7, 9.9).

    Adaptador *thin*: parsea el evento, resuelve el destino del Tenant, prepara la
    carga, la firma con HMAC-SHA256 usando el secreto del Tenant de Secrets_Manager
    (criterios 13.6, 13.7) y la entrega por HTTPS con hasta 3 intentos y *backoff*
    (criterio 9.9). Si la entrega agota los reintentos, propaga
    :class:`WebhookDeliveryError`; la Lambda está configurada para redrigir la
    invocación asíncrona fallida a la DLQ_Notificaciones conservando el evento
    original (criterios 9.9, 9.10).

    :param event: evento de EventBridge con ``detail-type = analysis.completed``.
    :param context: contexto de ejecución de la Lambda. No se usa.
    :return: un ``dict`` de metadatos del resultado (``status`` y ``analysis_id``),
        sin PII, útil para la inspección del resultado y las pruebas.
    :raises WebhookEventError: si el evento no es un Evento_Finalizacion válido
        (permanente). Se propaga para redrigir a la DLQ_Notificaciones.
    :raises WebhookDeliveryError: si la entrega agota los 3 intentos o el secreto
        no puede obtenerse. Se propaga para redrigir a la DLQ_Notificaciones.

    Postcondiciones:
        - Si no hay resolutor cableado, no actúa y devuelve ``status = "unconfigured"``.
        - Si el Lote no declaró ``metadata.webhook_url`` (resolutor devuelve
          ``None``), no entrega y devuelve ``status = "skipped"``.
        - Si el destino carece de ``secret_id``, no se puede firmar: se registra y
          se devuelve ``status = "unsigned"`` sin entregar (defensa en profundidad;
          la Infraestructura garantiza el secreto por Tenant con webhook).
        - En caso de entrega 2xx, devuelve ``status = "delivered"``.
        - El valor de retorno y los logs contienen solo identificadores y estado,
          nunca PII, la URL de webhook completa, el secreto ni la firma.
    """
    completed = parse_event(event)

    resolver = _RESOLVER
    if resolver is None:
        # Sin resolutor cableado por la Infraestructura (perfil del Tenant), no hay
        # destino que resolver. Se registra y se termina sin acción, sin tratarlo
        # como error del evento.
        logger.info(
            "webhook_resolver_not_configured",
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
            "webhook_not_declared",
            extra={
                "analysis_id": completed.analysis_id,
                "tenant_id": completed.tenant_id,
            },
        )
        return {"status": "skipped", "analysis_id": completed.analysis_id}

    delivery = prepare_delivery(completed, target)

    if not target.secret_id:
        # Un Tenant con webhook siempre debe tener secreto HMAC configurado
        # (criterio 13.6). Si falta, no se firma ni se envía sin firma: se registra
        # y se omite, para no entregar una carga no firmada ni fallar el pipeline.
        logger.warning(
            "webhook_secret_not_configured",
            extra={
                "analysis_id": completed.analysis_id,
                "tenant_id": completed.tenant_id,
            },
        )
        return {"status": "unsigned", "analysis_id": completed.analysis_id}

    secret = _SECRET_PROVIDER.get_secret(target.secret_id)
    signed = sign_delivery(delivery, secret)

    _SENDER.send(signed)

    logger.info(
        "webhook_notification_complete",
        extra={
            "analysis_id": completed.analysis_id,
            "tenant_id": completed.tenant_id,
            "status": completed.status,
            "event_id": completed.event_id,
            "correlation_id": completed.correlation_id,
        },
    )
    return {"status": "delivered", "analysis_id": completed.analysis_id}
