"""Adaptador de inspección de ejecuciones de Step Functions (``ExecutionInspector``).

Este módulo materializa el puerto
:class:`~sentiment.domain.ports.ExecutionInspector` (contrato normativo en
``domain/ports.py``) sobre AWS Step Functions. Lo consume únicamente el sweeper
(``reconcile_stuck``) para distinguir un Lote realmente **abandonado** de un
Lote que todavía está siendo procesado por una ejecución en curso (criterios
10.11 y 10.12; diseño sección 10.6).

Derivación determinista del ARN de la ejecución (diseño 10.6, condición (b))
----------------------------------------------------------------------------
El nombre de la ejecución de la Maquina_De_Estados **es** el ``analysis_id``
(criterio 5.14), de modo que su ARN es determinista y no hace falta que el META
guarde un ``execution_arn``. El ARN de la ejecución se deriva del ARN de la
Maquina_De_Estados, que la Infraestructura inyecta como la variable de entorno
``STATE_MACHINE_ARN``:

* State-machine ARN:
  ``arn:aws:states:{region}:{account}:stateMachine:{name}``
* Execution ARN:
  ``arn:aws:states:{region}:{account}:execution:{name}:{analysis_id}``

La transformación sustituye el segmento ``stateMachine:`` por ``execution:`` y
añade ``:{analysis_id}``. Derivarlo así —en lugar de reconstruir el ARN a partir
de ``region``/``account`` leídos por separado— evita duplicar la lógica de
composición del ARN y hereda el nombre de la Maquina_De_Estados tal cual lo
publica la Infraestructura, sin acoplarse a un literal.

Decisiones de diseño
--------------------
- **``None`` significa "no existe", nunca un error (criterio 10.12).** Cuando
  ``DescribeExecution`` responde ``ExecutionDoesNotExist``, el método devuelve
  ``None``; el sweeper lo interpreta como una de las dos condiciones de abandono.
  Cualquier otro error de dependencia (indisponibilidad, throttling) se propaga:
  no es seguro tratar un fallo transitorio como "la ejecución no existe", porque
  llevaría al sweeper a expirar un Lote que sí está en curso.
- **Resolución perezosa de la configuración (criterio 17.7).** ``STATE_MACHINE_ARN``
  se lee en el momento de la inspección, no al importar ni al construir el
  adaptador, para preservar la importabilidad del paquete sin la variable
  definida. Su ausencia es un :class:`~sentiment.observability.config.ConfigError`
  accionable, nunca un ``KeyError`` opaco.
- **Cliente reutilizado, cero reintentos del SDK (criterios 17.11, 9.18).** El
  cliente de Step Functions procede del singleton de
  :mod:`sentiment.adapters.aws_clients`.
- **Sin PII.** La inspección opera solo con identificadores (``analysis_id``,
  ARNs) y el estado de la ejecución; nunca toca el contenido del Lote.
"""

from __future__ import annotations

import os
from typing import Final, Protocol, cast

from botocore.exceptions import ClientError

from sentiment.adapters.aws_clients import stepfunctions_client
from sentiment.observability.config import ConfigError
from sentiment.observability.logging import get_logger

__all__ = ["StepFunctionsAdapter"]


class _DescribeExecutionClient(Protocol):
    """Superficie mínima del cliente de Step Functions que este adaptador consume.

    Se tipa contra este Protocol en lugar de ``botocore.client.BaseClient``, que
    no expone ``describe_execution`` sin los *extras* por servicio de
    ``boto3-stubs``. Facilita además inyectar un doble de prueba que implemente
    solo este método.
    """

    def describe_execution(self, *, executionArn: str) -> dict[str, object]:  # noqa: N803
        """Describe una ejecución por su ARN. Firma parcial de ``boto3`` Step Functions."""
        ...


logger = get_logger(service="reconcile_stuck")

#: Variable de entorno con el ARN de la Maquina_De_Estados padre, inyectada por
#: la Infraestructura (``workflow_stack``, tarea 12.3) con la forma
#: ``arn:aws:states:{region}:{account}:stateMachine:{name}``.
_ENV_STATE_MACHINE_ARN: Final = "STATE_MACHINE_ARN"

#: Segmento del ARN de la Maquina_De_Estados que se sustituye para obtener el ARN
#: de una ejecución concreta.
_STATE_MACHINE_SEGMENT: Final = ":stateMachine:"

#: Segmento equivalente en el ARN de una ejecución.
_EXECUTION_SEGMENT: Final = ":execution:"

#: Código de error de Step Functions que denota inequívocamente "no existe una
#: ejecución con ese ARN". Es el único código que el adaptador traduce a ``None``
#: (criterio 10.12); el resto de errores se propagan.
_EXECUTION_DOES_NOT_EXIST: Final = "ExecutionDoesNotExist"


def _require_state_machine_arn() -> str:
    """Devuelve el ARN de la Maquina_De_Estados desde el entorno, o lanza ConfigError.

    Lectura perezosa (en la inspección, no en import) para preservar la
    importabilidad del paquete sin la variable definida (criterio 17.7).

    :return: el ARN de la Maquina_De_Estados, recortado.
    :raises ConfigError: si ``STATE_MACHINE_ARN`` falta, está vacío o es solo
        espacios.
    """
    raw = os.environ.get(_ENV_STATE_MACHINE_ARN)
    if raw is None or not raw.strip():
        raise ConfigError(
            f"Falta la configuración requerida '{_ENV_STATE_MACHINE_ARN}'. Debe "
            f"inyectarse como variable de entorno en la Lambda del sweeper con el "
            f"ARN de la Maquina_De_Estados "
            f"('arn:aws:states:{{region}}:{{account}}:stateMachine:{{name}}')."
        )
    return raw.strip()


def _execution_arn(state_machine_arn: str, analysis_id: str) -> str:
    """Deriva el ARN de la ejecución cuyo nombre es ``analysis_id``.

    Transforma el ARN de la Maquina_De_Estados sustituyendo el segmento
    ``:stateMachine:`` por ``:execution:`` y añadiendo ``:{analysis_id}``
    (diseño 10.6, condición (b)). El nombre de la ejecución es el ``analysis_id``
    (criterio 5.14), por lo que el ARN resultante es determinista.

    :param state_machine_arn: ARN de la Maquina_De_Estados padre.
    :param analysis_id: ULID del Lote, que es también el nombre de la ejecución.
    :return: el ARN de la ejecución correspondiente.
    :raises ConfigError: si ``state_machine_arn`` no tiene el segmento
        ``:stateMachine:`` esperado y, por tanto, no es un ARN de Maquina_De_Estados.
    """
    if _STATE_MACHINE_SEGMENT not in state_machine_arn:
        raise ConfigError(
            f"El valor de '{_ENV_STATE_MACHINE_ARN}' no es un ARN de "
            f"Maquina_De_Estados válido: falta el segmento "
            f"'{_STATE_MACHINE_SEGMENT.strip(':')}'."
        )
    base = state_machine_arn.replace(_STATE_MACHINE_SEGMENT, _EXECUTION_SEGMENT, 1)
    return f"{base}:{analysis_id}"


class StepFunctionsAdapter:
    """Inspecciona ejecuciones de la Maquina_De_Estados (puerto ``ExecutionInspector``).

    Implementa estructuralmente :class:`~sentiment.domain.ports.ExecutionInspector`.
    El cliente de Step Functions se inyecta por constructor (inversión de
    dependencias): por defecto, el singleton reutilizable del módulo de clientes;
    las pruebas pueden inyectar un cliente apuntando a un simulador (``moto``).
    """

    def __init__(self, client: _DescribeExecutionClient | None = None) -> None:
        """Construye el adaptador.

        :param client: cliente de Step Functions a usar; si es ``None``, se toma
            el singleton de
            :func:`sentiment.adapters.aws_clients.stepfunctions_client`. No se lee
            configuración ni se llama a AWS en la construcción.
        """
        if client is None:
            # El singleton se tipa como ``BaseClient`` (stubs sin extras por
            # servicio), que no expone ``describe_execution`` estáticamente; se
            # estrecha al Protocol que este adaptador realmente usa.
            client = cast("_DescribeExecutionClient", stepfunctions_client())
        self._client: _DescribeExecutionClient = client

    def describe_status(self, analysis_id: str) -> str | None:
        """Devuelve el estado de la ejecución cuyo nombre es ``analysis_id``, o ``None``.

        Deriva el ARN determinista de la ejecución a partir de
        ``STATE_MACHINE_ARN`` y consulta ``DescribeExecution``.

        :param analysis_id: ULID del Lote; es también el nombre de la ejecución
            (criterio 5.14).
        :return: el estado de la ejecución (``"RUNNING"``, ``"SUCCEEDED"``,
            ``"FAILED"``, ``"TIMED_OUT"`` o ``"ABORTED"``), o ``None`` si la
            ejecución no existe (``ExecutionDoesNotExist``, criterio 10.12).
        :raises ConfigError: si ``STATE_MACHINE_ARN`` no está configurada o no es
            un ARN de Maquina_De_Estados válido.
        :raises ClientError: ante cualquier error de Step Functions distinto de
            ``ExecutionDoesNotExist`` (p. ej. throttling o indisponibilidad); se
            propaga en lugar de confundirse con "no existe", para que el sweeper
            no expire un Lote cuya ejecución sí está en curso.

        Postcondiciones:
            - ``None`` significa inequívocamente "no existe ejecución con ese
              nombre"; nunca enmascara un error transitorio de la dependencia.
        """
        execution_arn = _execution_arn(_require_state_machine_arn(), analysis_id)
        try:
            response = self._client.describe_execution(executionArn=execution_arn)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == _EXECUTION_DOES_NOT_EXIST:
                logger.info(
                    "execution_not_found",
                    extra={"analysis_id": analysis_id},
                )
                return None
            logger.warning(
                "describe_execution_failed",
                extra={
                    "analysis_id": analysis_id,
                    "error_type": exc.__class__.__name__,
                },
            )
            raise

        raw_status = response.get("status")
        status = raw_status if isinstance(raw_status, str) else None
        logger.info(
            "execution_status_inspected",
            extra={"analysis_id": analysis_id, "execution_status": status},
        )
        return status
