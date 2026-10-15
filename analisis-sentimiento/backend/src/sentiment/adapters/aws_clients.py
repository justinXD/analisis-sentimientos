"""Singletons de clientes de AWS (boto3) a nivel de módulo.

Este módulo es la **fuente única** de los clientes de boto3 que usan los
adaptadores de infraestructura. Centralizarlos aquí tiene dos razones de diseño:

- **Reutilización entre invocaciones calientes (criterio 17.11, diseño 2.1).**
  Los clientes se construyen **una sola vez** en el ámbito del módulo y se
  reutilizan. Al reaprovechar el mismo contenedor de Lambda, la segunda
  invocación y las siguientes reutilizan el cliente ya creado —y, con él, la
  conexión TLS subyacente del *connection pool* de ``botocore``—, lo que reduce
  la latencia y el coste de *cold start*. Instanciar un cliente nuevo por
  invocación forzaría un *handshake* TLS en cada llamada.

- **Cero reintentos del SDK (criterio 9.18, diseño 10.1).** Todo cliente se
  construye con ``retries={"total_max_attempts": 1, "mode": "standard"}``, es
  decir, un único intento y **ningún** reintento interno del SDK.
  ``analyze_comprehend`` y ``analyze_llm`` deben propagar cualquier error de
  dependencia a la Maquina_De_Estados en su **primera** ocurrencia; el único
  lugar donde vive la política de reintentos es Step Functions (``Retry``
  declarativo por tipo de error). Un bucle de reintento dentro de la Lambda
  pagaría tiempo de cómputo facturado durmiendo y escondería cada intento del
  historial de la ejecución.

  **Matiz de semántica de botocore.** El diseño (10.1) escribe literalmente
  ``retries={"max_attempts": 1}``. En botocore, sin embargo, la clave
  ``max_attempts`` del diccionario de ``retries`` cuenta los reintentos que se
  hacen **después** de la petición inicial, de modo que ``max_attempts: 1``
  permitiría **un** reintento (dos intentos en total) —lo contrario de lo que
  exige el criterio 9.18—. La clave que fija el número total de intentos es
  ``total_max_attempts``; por eso aquí se usa ``total_max_attempts: 1`` para
  obtener de verdad cero reintentos. Se respeta la **intención** del diseño
  (cero reintentos del SDK), corrigiendo el valor literal a la clave correcta
  de la API.

**Importabilidad (criterio 17.7).** Importar este módulo nunca lee ``MODEL_ID``
ni ninguna otra configuración obligatoria, y nunca llama a AWS. Construir un
cliente de boto3 no requiere credenciales ni acceso de red: la resolución de
credenciales y la conexión son perezosas, se difieren a la primera llamada de
API. La región se toma de las variables de entorno estándar de AWS que la
Infraestructura inyecta (``AWS_REGION`` / ``AWS_DEFAULT_REGION``), con un valor
por defecto, sin leer configuración del propio Sistema. Esto permite que el
recorrido de importabilidad (``pkgutil.walk_packages``) importe el módulo con
``MODEL_ID`` sin definir.

Nota sobre tipos: con el meta-paquete ``boto3-stubs`` instalado sin los *extras*
por servicio, ``boto3.client(...)`` se tipa como :class:`botocore.client.BaseClient`.
Los accesores exponen ese tipo; cada adaptador concreto (Comprehend, Bedrock,
etc.) encapsula las llamadas específicas del servicio sobre el cliente que recibe.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, cast

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from botocore.client import BaseClient

__all__ = [
    "DEFAULT_REGION",
    "comprehend_client",
    "bedrock_runtime_client",
    "dynamodb_client",
    "s3_client",
    "sqs_client",
    "eventbridge_client",
    "stepfunctions_client",
    "secretsmanager_client",
    "ssm_client",
    "ses_client",
    "reset_clients",
]

#: Región por defecto cuando el entorno no define ninguna. En runtime la
#: Infraestructura siempre inyecta ``AWS_REGION``; este valor solo cubre entornos
#: locales o de pruebas que no lo hayan definido.
DEFAULT_REGION: Final = "us-east-1"


def _region() -> str:
    """Resuelve la región de AWS desde el entorno estándar.

    Lee las variables de entorno canónicas de AWS, no configuración propia del
    Sistema, para no acoplar los clientes a :mod:`sentiment.observability.config`
    ni introducir una lectura obligatoria en el import.

    :return: el código de región (p. ej. ``us-east-1``); nunca una cadena vacía.
    """
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    return region if region and region.strip() else DEFAULT_REGION


# ---------------------------------------------------------------------------
# Configuraciones de botocore.
#
# `total_max_attempts: 1` fija UN único intento, es decir, CERO reintentos del
# SDK (criterio 9.18). Ojo: la clave `max_attempts` NO sirve aquí, porque en
# botocore cuenta los reintentos posteriores al intento inicial (`max_attempts: 1`
# = 1 reintento = 2 intentos); por eso se usa `total_max_attempts`. El
# `mode: standard` fija la semántica de clasificación de errores del SDK,
# irrelevante aquí porque no reintentamos, pero explícito para no depender del
# modo por defecto.
#
# Los timeouts provienen de la sección 10.7 del diseño:
#   - Comprehend: lotes rápidos (~200 ms); timeouts ajustados (connect 3, read 20).
#   - Bedrock: 2-20 s por ítem, con holgura (connect 5, read 90).
# El resto de servicios usan un timeout genérico razonable, también sin reintentos.
# ---------------------------------------------------------------------------

_COMPREHEND_CONFIG: Final = Config(
    retries={"total_max_attempts": 1, "mode": "standard"},
    connect_timeout=3,
    read_timeout=20,
)

_BEDROCK_CONFIG: Final = Config(
    retries={"total_max_attempts": 1, "mode": "standard"},
    connect_timeout=5,
    read_timeout=90,
)

_DEFAULT_CONFIG: Final = Config(
    retries={"total_max_attempts": 1, "mode": "standard"},
    connect_timeout=5,
    read_timeout=30,
)


# ---------------------------------------------------------------------------
# Fábricas por servicio.
#
# Cada fábrica llama a `boto3.client` con el nombre de servicio como literal: los
# stubs de botocore solo exponen sobrecargas por `Literal`, de modo que el nombre
# debe aparecer textualmente para que el tipado estático resuelva. Sin los
# *extras* por servicio de `boto3-stubs`, esas sobrecargas devuelven `Any`; cada
# resultado se estrecha explícitamente a `BaseClient` con `cast`, que es el
# contrato que exponen los accesores. Ninguna de estas llamadas realiza E/S:
# `boto3.client` difiere credenciales y conexión a la primera invocación de API.
# ---------------------------------------------------------------------------


def _new_comprehend() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("comprehend", region_name=_region(), config=_COMPREHEND_CONFIG),
    )


def _new_bedrock_runtime() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client(
            "bedrock-runtime", region_name=_region(), config=_BEDROCK_CONFIG
        ),
    )


def _new_dynamodb() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("dynamodb", region_name=_region(), config=_DEFAULT_CONFIG),
    )


def _new_s3() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("s3", region_name=_region(), config=_DEFAULT_CONFIG),
    )


def _new_sqs() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("sqs", region_name=_region(), config=_DEFAULT_CONFIG),
    )


def _new_eventbridge() -> BaseClient:
    # El nombre de servicio de boto3 para EventBridge es `events`.
    return cast(
        "BaseClient",
        boto3.client("events", region_name=_region(), config=_DEFAULT_CONFIG),
    )


def _new_stepfunctions() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("stepfunctions", region_name=_region(), config=_DEFAULT_CONFIG),
    )


def _new_secretsmanager() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("secretsmanager", region_name=_region(), config=_DEFAULT_CONFIG),
    )


def _new_ssm() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("ssm", region_name=_region(), config=_DEFAULT_CONFIG),
    )


def _new_ses() -> BaseClient:
    return cast(
        "BaseClient",
        boto3.client("ses", region_name=_region(), config=_DEFAULT_CONFIG),
    )


# ---------------------------------------------------------------------------
# Singletons a nivel de módulo (criterio 17.11).
#
# Se construyen una vez al importar el módulo. El contenedor de Lambda reutiliza
# estas instancias —y sus conexiones TLS— entre invocaciones calientes. Los dos
# clientes del camino crítico (Comprehend y Bedrock) llevan sus timeouts
# específicos; el resto, el timeout genérico. Todos, cero reintentos del SDK.
# ---------------------------------------------------------------------------

_comprehend: BaseClient = _new_comprehend()
_bedrock_runtime: BaseClient = _new_bedrock_runtime()
_dynamodb: BaseClient = _new_dynamodb()
_s3: BaseClient = _new_s3()
_sqs: BaseClient = _new_sqs()
_eventbridge: BaseClient = _new_eventbridge()
_stepfunctions: BaseClient = _new_stepfunctions()
_secretsmanager: BaseClient = _new_secretsmanager()
_ssm: BaseClient = _new_ssm()
_ses: BaseClient = _new_ses()


def comprehend_client() -> BaseClient:
    """Cliente de Amazon Comprehend, reutilizado entre invocaciones calientes.

    Configurado con ``connect_timeout=3`` y ``read_timeout=20`` (diseño 10.7) y
    sin reintentos del SDK (criterio 9.18).

    :return: el singleton del cliente de Comprehend.
    """
    return _comprehend


def bedrock_runtime_client() -> BaseClient:
    """Cliente de Amazon Bedrock Runtime, reutilizado entre invocaciones calientes.

    Configurado con ``connect_timeout=5`` y ``read_timeout=90`` (diseño 10.7),
    holgado para la latencia por ítem de 2-20 s del Modelo_Bedrock, y sin
    reintentos del SDK (criterio 9.18): el reintento vive en Step Functions.

    :return: el singleton del cliente de Bedrock Runtime.
    """
    return _bedrock_runtime


def dynamodb_client() -> BaseClient:
    """Cliente de Amazon DynamoDB, reutilizado entre invocaciones calientes.

    :return: el singleton del cliente de DynamoDB.
    """
    return _dynamodb


def s3_client() -> BaseClient:
    """Cliente de Amazon S3, reutilizado entre invocaciones calientes.

    :return: el singleton del cliente de S3.
    """
    return _s3


def sqs_client() -> BaseClient:
    """Cliente de Amazon SQS, reutilizado entre invocaciones calientes.

    :return: el singleton del cliente de SQS.
    """
    return _sqs


def eventbridge_client() -> BaseClient:
    """Cliente de Amazon EventBridge, reutilizado entre invocaciones calientes.

    El nombre de servicio de boto3 es ``events``.

    :return: el singleton del cliente de EventBridge.
    """
    return _eventbridge


def stepfunctions_client() -> BaseClient:
    """Cliente de AWS Step Functions, reutilizado entre invocaciones calientes.

    :return: el singleton del cliente de Step Functions.
    """
    return _stepfunctions


def secretsmanager_client() -> BaseClient:
    """Cliente de AWS Secrets Manager, reutilizado entre invocaciones calientes.

    :return: el singleton del cliente de Secrets Manager.
    """
    return _secretsmanager


def ssm_client() -> BaseClient:
    """Cliente de AWS Systems Manager (Parameter_Store), reutilizado en caliente.

    :return: el singleton del cliente de SSM.
    """
    return _ssm


def ses_client() -> BaseClient:
    """Cliente de Amazon SES, reutilizado entre invocaciones calientes.

    :return: el singleton del cliente de SES.
    """
    return _ses


def reset_clients() -> None:
    """Reconstruye todos los clientes singleton.

    Pensado para las pruebas que necesitan reconstruir los clientes tras cambiar
    la región del entorno o tras activar un simulador de AWS (``moto``). En
    runtime no es necesario: los singletons viven lo que vive el contenedor de la
    Lambda.
    """
    global _comprehend, _bedrock_runtime, _dynamodb, _s3, _sqs
    global _eventbridge, _stepfunctions, _secretsmanager, _ssm, _ses
    _comprehend = _new_comprehend()
    _bedrock_runtime = _new_bedrock_runtime()
    _dynamodb = _new_dynamodb()
    _s3 = _new_s3()
    _sqs = _new_sqs()
    _eventbridge = _new_eventbridge()
    _stepfunctions = _new_stepfunctions()
    _secretsmanager = _new_secretsmanager()
    _ssm = _new_ssm()
    _ses = _new_ses()
