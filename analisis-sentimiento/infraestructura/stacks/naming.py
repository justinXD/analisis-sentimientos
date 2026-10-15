"""Convenciones de nombre de recursos compartidas entre stacks (sin dependencias).

Este módulo es la **única fuente de verdad** de los nombres de recurso que dos o
más stacks deben compartir byte a byte. Vive aislado y sin importar ningún stack,
de modo que cualquier stack pueda reutilizar la convención sin crear ciclos de
importación entre dominios.

Caso de uso principal (tarea 13.3): el nombre del **Bus_Eventos**. El
``compute_stack`` concede ``events:PutEvents`` y publica la variable de entorno
``EVENT_BUS_NAME`` de ``aggregate_results``, mientras que el ``notifications_stack``
**crea** el bus. Ambos derivan el nombre de :func:`event_bus_name`, por lo que es
imposible que se desincronicen: si el nombre cambiara aquí, cambiaría en los dos
sitios a la vez (criterio 13.1).
"""

from __future__ import annotations

from config.environments import EnvConfig

# Sufijo del nombre del Bus_Eventos personalizado del dominio de notificación.
# La convención completa es ``{project}-{env}-events`` (criterio 13.1); este
# sufijo es la pieza invariable del contrato entre compute y notifications.
_EVENT_BUS_SUFFIX = "events"


def event_bus_name(config: EnvConfig) -> str:
    """Nombre del Bus_Eventos personalizado del dominio de notificación.

    Fuente única de la convención ``{project}-{env}-events`` que comparten el
    ``compute_stack`` (que concede ``events:PutEvents`` y publica ``EVENT_BUS_NAME``)
    y el ``notifications_stack`` (que crea el bus con este mismo nombre, tarea 13.3).

    Args:
        config: Configuración del entorno de despliegue.

    Returns:
        El nombre del Bus_Eventos, p. ej. ``sentiment-dev-events``.
    """

    return f"{config.project}-{config.env_name}-{_EVENT_BUS_SUFFIX}"
