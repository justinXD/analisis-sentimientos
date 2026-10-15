"""Utilidades compartidas de los adaptadores *thin* de la capa de API.

Las cuatro Lambdas de la capa de API (``api_submit``, ``api_status``,
``api_download`` y ``api_purge``) son adaptadores de transporte finos: traducen
un evento de API Gateway (REST, integración *proxy*) en una llamada al caso de
uso correspondiente y la vista resultante —o el error del dominio— en una
respuesta HTTP. Todo lo que esas cuatro comparten vive aquí, en un único lugar,
para que cada handler quede reducido a su lógica de traducción específica y para
que las garantías transversales (identidad desde el autorizador, no fuga de PII,
``correlation_id`` siempre presente) se implementen una sola vez.

Garantías que este módulo centraliza
-------------------------------------
* **Identidad solo del autorizador (criterios 1.14, 2.9, 2.10).**
  :func:`extract_identity` lee ``tenant_id`` (claim ``custom:tenant_id``) y
  ``principal_sub`` (claim ``sub``) **exclusivamente** de
  ``event.requestContext.authorizer`` —nunca del cuerpo, de las cabeceras ni de
  los parámetros de consulta—. Si cualquiera de los dos falta o está en blanco,
  lanza :class:`~sentiment.domain.errors.ForbiddenError` (HTTP 403), que la API
  Gateway normalmente ya habría atajado (criterio 1.15), pero que el handler
  revalida como defensa en profundidad antes de tocar DynamoDB o S3 (criterio
  2.9). El soporte cubre tanto el *shape* del autorizador JWT nativo
  (``authorizer.jwt.claims``) como el *shape* plano (``authorizer.claims``) que
  usan algunos integradores y las pruebas de contrato.

* **``correlation_id`` siempre presente (criterios 3.15, 15.4 del diseño).**
  :func:`resolve_correlation_id` toma el ``requestId`` que API Gateway asigna a
  cada llamada HTTP y, si no estuviera disponible, genera un UUIDv4. Así toda
  respuesta —de éxito o de error— puede correlacionarse con los logs del lado del
  servidor.

* **Respuestas sin PII ni *stack traces* (criterios 3.14, 15.5).**
  :func:`error_response` serializa un :class:`~sentiment.domain.errors.SentimentError`
  usando exclusivamente :meth:`SentimentError.to_client_dict`, que expone solo el
  ``error_code`` del catálogo cerrado y el ``correlation_id``. El ``detail``
  interno, el mensaje de la excepción y cualquier traza quedan en el log, nunca en
  el cuerpo. :func:`internal_error_response` hace lo propio para los fallos de
  infraestructura, con un ``INTERNAL_ERROR`` genérico (HTTP 500).

El módulo es **puro respecto de la infraestructura**: no importa ``boto3`` ni
ningún adaptador, de modo que importar un handler no exige que el SDK de AWS ni
las variables de entorno estén presentes (criterio 17.7); la construcción de los
servicios concretos la hace cada handler en su propio ámbito de módulo.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sentiment.domain.errors import ForbiddenError, SentimentError

__all__ = [
    "INTERNAL_ERROR_CODE",
    "Identity",
    "error_response",
    "extract_identity",
    "internal_error_response",
    "json_response",
    "resolve_correlation_id",
]

#: ``error_code`` del catálogo cerrado que se devuelve ante un fallo de
#: infraestructura no previsto (HTTP 500). Coincide con el valor por defecto de
#: :class:`~sentiment.domain.errors.SentimentError`.
INTERNAL_ERROR_CODE: Final = "INTERNAL_ERROR"

#: Clave del contexto del autorizador de API Gateway dentro del evento. La
#: identidad verificada vive bajo ``event["requestContext"]["authorizer"]``.
_AUTHORIZER_KEY: Final = "authorizer"

#: Nombre del claim de Cognito que porta el Tenant del solicitante. Es un claim
#: ``custom:`` del Access_Token, verificado por API Gateway (criterio 1.14).
_CLAIM_TENANT_ID: Final = "custom:tenant_id"

#: Nombre del claim estándar de OIDC que porta el identificador del principal.
_CLAIM_SUB: Final = "sub"

#: Cabecera de respuesta común a todos los handlers. El contrato de la API es
#: siempre JSON (criterio 3.14 y la sección 15/16 del diseño).
_CONTENT_TYPE_JSON: Final = "application/json"


class Identity:
    """Identidad verificada del solicitante, derivada solo del autorizador.

    Es un valor inmutable con los dos únicos atributos de identidad que los casos
    de uso necesitan, ambos tomados de claims verificados por API Gateway: nunca
    del cuerpo, las cabeceras ni los parámetros de consulta (criterio 1.14).

    Attributes:
        tenant_id: valor del claim ``custom:tenant_id``; frontera de aislamiento
            multi-tenant. Garantizado no vacío por :func:`extract_identity`.
        principal_sub: valor del claim ``sub``; identificador del principal.
            Garantizado no vacío por :func:`extract_identity`.
    """

    __slots__ = ("tenant_id", "principal_sub")

    def __init__(self, tenant_id: str, principal_sub: str) -> None:
        """Construye la identidad con el Tenant y el ``sub`` ya validados.

        :param tenant_id: claim ``custom:tenant_id`` no vacío.
        :param principal_sub: claim ``sub`` no vacío.
        """
        self.tenant_id = tenant_id
        self.principal_sub = principal_sub


def _claims_from_event(event: dict[str, Any]) -> dict[str, Any]:
    """Extrae el mapa de claims del contexto del autorizador del evento.

    Admite los dos *shapes* en que API Gateway expone los claims verificados:

    * **Autorizador JWT nativo (HTTP API / REST con JWT authorizer):** los claims
      cuelgan de ``requestContext.authorizer.jwt.claims``.
    * **Autorizador plano (algunos integradores REST y las pruebas de contrato):**
      los claims cuelgan directamente de ``requestContext.authorizer.claims``.

    La función nunca lanza: ante un evento sin la estructura esperada devuelve un
    mapa vacío, y es :func:`extract_identity` quien decide el rechazo con 403. Así
    el handler no distingue "autorizador ausente" de "claim ausente": ambos son el
    mismo 403 genérico, sin filtrar qué faltó (criterio 1.17).

    :param event: evento de API Gateway (integración *proxy*).
    :return: el mapa de claims, o un diccionario vacío si no se puede localizar.
    """
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        return {}
    authorizer = request_context.get(_AUTHORIZER_KEY)
    if not isinstance(authorizer, dict):
        return {}

    # Shape del JWT authorizer nativo: authorizer.jwt.claims.
    jwt_section = authorizer.get("jwt")
    if isinstance(jwt_section, dict):
        claims = jwt_section.get("claims")
        if isinstance(claims, dict):
            return claims

    # Shape plano: authorizer.claims.
    claims = authorizer.get("claims")
    if isinstance(claims, dict):
        return claims

    return {}


def _non_blank_claim(claims: dict[str, Any], name: str) -> str | None:
    """Devuelve el valor recortado de un claim si es una cadena no vacía.

    Un claim ausente, de tipo no textual, vacío o compuesto solo de espacios se
    trata como ausente (criterios 1.15, 2.9): devuelve ``None``.

    :param claims: mapa de claims verificados.
    :param name: nombre del claim a leer.
    :return: el valor recortado del claim, o ``None`` si no es utilizable.
    """
    raw = claims.get(name)
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    return trimmed or None


def extract_identity(event: dict[str, Any]) -> Identity:
    """Deriva la identidad del solicitante del contexto del autorizador.

    Lee ``custom:tenant_id`` y ``sub`` **exclusivamente** de
    ``event.requestContext.authorizer`` (criterio 1.14). API Gateway ya rechaza
    con 403 un token sin esos claims (criterio 1.15), pero el handler lo
    revalida aquí como defensa en profundidad: si falta cualquiera de los dos,
    lanza :class:`ForbiddenError` **antes** de cualquier lectura o escritura en
    DynamoDB o S3 (criterio 2.9).

    :param event: evento de API Gateway (integración *proxy*).
    :return: la :class:`Identity` con ``tenant_id`` y ``principal_sub`` no vacíos.
    :raises ForbiddenError: si el claim ``custom:tenant_id`` o el claim ``sub``
        están ausentes o en blanco (HTTP 403, ``FORBIDDEN``).
    """
    claims = _claims_from_event(event)
    tenant_id = _non_blank_claim(claims, _CLAIM_TENANT_ID)
    principal_sub = _non_blank_claim(claims, _CLAIM_SUB)

    if tenant_id is None or principal_sub is None:
        # No se revela cuál de los dos faltó: el cuerpo es un 403 genérico
        # (criterio 1.17). El `detail` queda en el log del servidor, nunca en la
        # respuesta.
        raise ForbiddenError(
            "El contexto del autorizador no aporta una identidad completa.",
            missing_tenant_id=tenant_id is None,
            missing_sub=principal_sub is None,
        )

    return Identity(tenant_id=tenant_id, principal_sub=principal_sub)


def resolve_correlation_id(event: dict[str, Any]) -> str:
    """Resuelve el ``correlation_id`` de la solicitud, generándolo si no existe.

    Usa el ``requestId`` que API Gateway asigna a cada llamada HTTP
    (``requestContext.requestId``). Si no estuviera presente —p. ej. una
    invocación directa o un evento malformado—, genera un UUIDv4 para que toda
    respuesta sea correlacionable con los logs del servidor (criterios 3.15,
    15.4 del diseño).

    :param event: evento de API Gateway (integración *proxy*).
    :return: un identificador de correlación no vacío (el ``requestId`` o un
        UUIDv4 recién generado).
    """
    request_context = event.get("requestContext")
    if isinstance(request_context, dict):
        request_id = request_context.get("requestId")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
    return str(uuid.uuid4())


def json_response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Construye una respuesta de integración *proxy* de API Gateway con cuerpo JSON.

    Serializa ``body`` con ``ensure_ascii=False`` para preservar los caracteres no
    ASCII (acentos, ñ) de los textos en español sin inflarlos a secuencias
    ``\\uXXXX``. No fija cabeceras de CORS: la lista blanca de orígenes se
    configura en API Gateway, no en el código (sección 20.2 del diseño; se evita
    el antipatrón ``Access-Control-Allow-Origin: '*'`` del código legado).

    :param status_code: código HTTP de la respuesta.
    :param body: cuerpo a serializar como JSON. Debe contener solo datos seguros
        para el cliente (sin PII ni detalle interno).
    :return: el diccionario de respuesta ``{statusCode, headers, body}`` que
        espera la integración *proxy* de API Gateway.
    """
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": _CONTENT_TYPE_JSON},
        "body": json.dumps(body, ensure_ascii=False),
    }


def error_response(exc: SentimentError, correlation_id: str) -> dict[str, Any]:
    """Traduce un :class:`SentimentError` a una respuesta HTTP sin PII.

    El cuerpo contiene **exclusivamente** el ``error_code`` del catálogo cerrado y
    el ``correlation_id`` (vía :meth:`SentimentError.to_client_dict`), nunca el
    ``detail`` interno, el mensaje de la excepción ni ningún *stack trace*
    (criterios 3.14, 15.5). El código HTTP sale del propio error; si fuera
    ``None`` (un error de procesamiento interno que nunca debería cruzar la
    frontera HTTP), se usa 500 como salvaguarda.

    :param exc: el error del dominio a traducir.
    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta de integración *proxy* con el cuerpo seguro.
    """
    status_code = exc.http_status if exc.http_status is not None else 500
    return json_response(status_code, exc.to_client_dict(correlation_id))


def internal_error_response(correlation_id: str) -> dict[str, Any]:
    """Construye la respuesta genérica de fallo de infraestructura (HTTP 500).

    Se usa para cualquier excepción inesperada (no un :class:`SentimentError`):
    un fallo de dependencia que el servicio no envolvió. El cuerpo revela solo el
    ``error_code`` genérico ``INTERNAL_ERROR`` y el ``correlation_id``; el detalle
    y la traza quedan en el log del servidor (criterio 15.5).

    :param correlation_id: identificador de correlación de la solicitud.
    :return: la respuesta de integración *proxy* con el cuerpo genérico de error.
    """
    return json_response(
        500,
        {"error_code": INTERNAL_ERROR_CODE, "correlation_id": correlation_id},
    )
