"""Resolución multi-tenant para las pruebas de carga (tarea 15.4).

Las pruebas de carga lanzan 5 Lotes de **Tenants distintos** de forma simultánea
contra la API desplegada en ``dev``. Para ello necesitan, por cada Tenant, un
``access_token`` propio (con los scopes ``write``/``read`` y, para la limpieza,
``purge``) cuyo claim ``custom:tenant_id`` fije ese Tenant.

Este módulo reutiliza el arnés de integración (``integration._harness``):
el *gate* de salto por variables de entorno, el :class:`StackConfig`, el
:class:`ApiClient` y los helpers ``make_items``/``poll_until_terminal``. Añade de
forma aditiva la lectura de **varios** tokens/tenants desde el entorno y la
construcción de un :class:`ApiClient` por Tenant, sin tocar el arnés existente ni
las pruebas de integración de la tarea 15.2.

Seguridad (steering)
--------------------
No contiene credenciales, tokens ni endpoints reales: todo valor sensible se lee
en runtime de variables de entorno por NOMBRE. Los tokens nunca se registran. Los
textos de ``respuesta`` enviados son sintéticos y no contienen PII.

Variables de entorno (NOMBRES; nunca valores reales en el repo)
---------------------------------------------------------------
Además de las obligatorias del arnés de integración
(``INTEGRATION_STACK_ENV``, ``INTEGRATION_API_BASE_URL``), la prueba de carga
necesita identificar varios Tenants. Se admiten dos formas, por orden de
precedencia:

1. ``INTEGRATION_TENANT_TOKENS`` (recomendada): lista separada por comas de pares
   ``tenant_id:access_token`` (p. ej. ``t1:<TOKEN_1>,t2:<TOKEN_2>,...``). Cada
   par define un Tenant y su token. Es la forma natural de probar el aislamiento
   real entre Tenants distintos.
2. ``INTEGRATION_TENANT_IDS`` + ``INTEGRATION_ACCESS_TOKEN``: lista de
   ``tenant_id`` separada por comas que comparten un único token cuyo claim de
   Tenant es parametrizable por el servidor de autorización. Útil cuando el emisor
   acepta un ``tenant_id`` por cabecera/claim configurable en ``dev``.

Si no se define ninguna de las dos (ni el modo mono-tenant heredado), la prueba de
carga se **SALTA** limpiamente, igual que las de integración sin stack.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from integration._harness import (
    SHOULD_SKIP as INTEGRATION_SHOULD_SKIP,
)
from integration._harness import (
    ApiClient,
    StackConfig,
    load_stack_config,
)

# --------------------------------------------------------------------------- #
# Parámetros de la carga (criterios 5.8 / 18.3 / 18.5)                         #
# --------------------------------------------------------------------------- #

#: Número de Lotes/Tenants simultáneos exigido por los criterios 5.8/18.3.
REQUIRED_TENANT_COUNT: int = 5

#: Nombre de la variable con los pares ``tenant_id:token`` (forma recomendada).
_ENV_TENANT_TOKENS = "INTEGRATION_TENANT_TOKENS"

#: Nombre de la variable con la lista de ``tenant_id`` (forma de token compartido).
_ENV_TENANT_IDS = "INTEGRATION_TENANT_IDS"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Un Tenant de la prueba de carga con su cliente de API autenticado.

    Attributes:
        tenant_id: ``custom:tenant_id`` del Tenant.
        client: :class:`ApiClient` autenticado con el token de ese Tenant.
    """

    tenant_id: str
    client: ApiClient


def _parse_tenant_tokens(raw: str) -> list[tuple[str, str]]:
    """Parsea ``INTEGRATION_TENANT_TOKENS`` a pares ``(tenant_id, token)``.

    El formato es ``tenant_id:token`` separado por comas. El ``token`` puede
    contener ``:`` (los JWT no, pero se es permisivo): solo se parte en el primer
    ``:``. Las entradas vacías se ignoran.

    Args:
        raw: Valor crudo de la variable de entorno.

    Returns:
        La lista de pares ``(tenant_id, token)`` en el orden declarado.

    Raises:
        ValueError: Si alguna entrada no tiene la forma ``tenant_id:token`` o si un
            ``tenant_id`` aparece repetido (los Tenants deben ser distintos).
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        tenant_id, sep, token = entry.partition(":")
        tenant_id = tenant_id.strip()
        token = token.strip()
        if not sep or not tenant_id or not token:
            raise ValueError(
                f"{_ENV_TENANT_TOKENS}: cada entrada debe ser 'tenant_id:token'"
            )
        if tenant_id in seen:
            raise ValueError(
                f"{_ENV_TENANT_TOKENS}: tenant_id repetido {tenant_id!r}; los "
                "Tenants de la prueba de carga deben ser distintos"
            )
        seen.add(tenant_id)
        pairs.append((tenant_id, token))
    return pairs


def _tenant_config(base: StackConfig, tenant_id: str, token: str) -> StackConfig:
    """Deriva un :class:`StackConfig` por Tenant cambiando token y ``tenant_id``.

    Reutiliza toda la resolución por convención del arnés (región, bucket, tabla)
    y solo sustituye las credenciales/identidad del Tenant.

    Args:
        base: Configuración resuelta del stack (común a todos los Tenants).
        tenant_id: ``custom:tenant_id`` del Tenant.
        token: Access_Token Bearer del Tenant.

    Returns:
        Una copia de ``base`` con ``access_token`` y ``tenant_id`` del Tenant.
    """
    return replace(base, access_token=token, tenant_id=tenant_id)


def _resolve_tenant_specs(base: StackConfig) -> list[tuple[str, str]]:
    """Resuelve los pares ``(tenant_id, token)`` desde el entorno.

    Aplica la precedencia documentada en el módulo: primero
    ``INTEGRATION_TENANT_TOKENS``; si no, ``INTEGRATION_TENANT_IDS`` con el token
    compartido de ``base``.

    Args:
        base: Configuración base (aporta el token compartido del modo heredado).

    Returns:
        La lista de pares ``(tenant_id, token)``; vacía si no hay configuración
        multi-tenant (lo que provoca el salto de la prueba).
    """
    raw_tokens = os.getenv(_ENV_TENANT_TOKENS, "").strip()
    if raw_tokens:
        return _parse_tenant_tokens(raw_tokens)

    raw_ids = os.getenv(_ENV_TENANT_IDS, "").strip()
    if raw_ids:
        shared_token = base.access_token
        ids = [tid.strip() for tid in raw_ids.split(",") if tid.strip()]
        if len(set(ids)) != len(ids):
            raise ValueError(
                f"{_ENV_TENANT_IDS}: los tenant_id deben ser distintos entre sí"
            )
        return [(tid, shared_token) for tid in ids]

    return []


@dataclass(frozen=True, slots=True)
class MultiTenantConfig:
    """Configuración resuelta de la prueba de carga multi-tenant.

    Attributes:
        base: El :class:`StackConfig` común (región, bucket, tabla, API base).
        tenants: Un :class:`TenantContext` por Tenant distinto, en orden declarado.
    """

    base: StackConfig
    tenants: tuple[TenantContext, ...]


def load_multitenant_config() -> MultiTenantConfig:
    """Construye la configuración multi-tenant de la prueba de carga.

    No se invoca cuando el *gate* de salto está activo, de modo que la mera
    importación/recolección del módulo nunca toca AWS.

    Returns:
        La :class:`MultiTenantConfig` con un cliente por Tenant.

    Raises:
        ValueError: Si la configuración de Tenants es inválida (ver
            :func:`_parse_tenant_tokens`).
    """
    base = load_stack_config()
    specs = _resolve_tenant_specs(base)
    tenants = tuple(
        TenantContext(
            tenant_id=tenant_id,
            client=ApiClient(_tenant_config(base, tenant_id, token)),
        )
        for tenant_id, token in specs
    )
    return MultiTenantConfig(base=base, tenants=tenants)


def _missing_multitenant_reason() -> str:
    """Devuelve la razón de salto si no hay configuración multi-tenant suficiente.

    Returns:
        Cadena vacía si hay configuración suficiente (>= REQUIRED_TENANT_COUNT
        Tenants distintos); si no, una razón legible para el ``skip``.
    """
    if INTEGRATION_SHOULD_SKIP:
        return (
            "pruebas de carga: requieren un stack dev desplegado y credenciales AWS "
            "(faltan las variables de entorno del arnés de integración; ver "
            "backend/tests/load/README.md)"
        )
    raw_tokens = os.getenv(_ENV_TENANT_TOKENS, "").strip()
    raw_ids = os.getenv(_ENV_TENANT_IDS, "").strip()
    if not raw_tokens and not raw_ids:
        return (
            "pruebas de carga: definen varios Tenants simultáneos; exporta "
            f"{_ENV_TENANT_TOKENS} ('tenant_id:token,...') o {_ENV_TENANT_IDS} "
            "('tenant_id,...'); ver backend/tests/load/README.md"
        )
    # Hay alguna variable: validar el recuento sin tocar red.
    count = (
        len(_parse_tenant_tokens(raw_tokens))
        if raw_tokens
        else len([t for t in raw_ids.split(",") if t.strip()])
    )
    if count < REQUIRED_TENANT_COUNT:
        return (
            f"pruebas de carga: requieren al menos {REQUIRED_TENANT_COUNT} Tenants "
            f"distintos (criterios 5.8/18.3); configurados: {count}"
        )
    return ""


#: Razón del salto cuando falta configuración multi-tenant (evaluada al importar).
SKIP_REASON: str = _missing_multitenant_reason()

#: Condición booleana del salto: cierto cuando NO hay stack o faltan Tenants.
SHOULD_SKIP: bool = bool(SKIP_REASON)
