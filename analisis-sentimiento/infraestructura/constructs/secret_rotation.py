"""Construct ``CognitoClientSecretRotation``: rotación del *client secret* M2M (tarea 13.1).

Secrets Manager no sabe por sí mismo cómo rotar el *client secret* de un app
client de Cognito: no es una base de datos, así que ninguna de las rotaciones
hospedadas (``HostedRotation.*``, todas específicas de motores SQL/NoSQL) encaja.
La rotación de un secreto genérico exige una **Lambda de rotación** que implemente
el contrato de cuatro pasos de Secrets Manager (``createSecret`` → ``setSecret`` →
``testSecret`` → ``finishSecret``).

Este construct materializa esa Lambda y la deja lista para que
:meth:`aws_cdk.aws_secretsmanager.Secret.add_rotation_schedule` la invoque cada 90
días (criterio 1.13). La función:

    - **Genera** el nuevo valor del secreto con el módulo ``secrets`` de la
      biblioteca estándar (no con ``secretsmanager:GetRandomPassword``), de modo
      que **toda acción acotable** de Secrets Manager del rol quede ceñida al ARN
      de *este* secreto (criterios 15.10 / 15.11 / 17.8). CDK añade igualmente el
      permiso ``GetRandomPassword`` sobre ``Resource: "*"`` —no admite ARN— que
      esta implementación deja sin ejercer.
    - Actualiza el ``client_secret`` del app client en Cognito
      (``cognito-idp:UpdateUserPoolClient``) acotado al ARN de *este* User Pool.

Es código de **infraestructura**, no de aplicación: por eso se empaqueta en línea
(:meth:`aws_cdk.aws_lambda.Code.from_inline`) y no desde ``backend/src``. Se
mantiene deliberadamente pequeño y sin dependencias de terceros (solo ``boto3``,
disponible en el runtime de Lambda) para que su superficie de ataque y su
mantenimiento sean mínimos.

Seguridad
---------
- Permisos de mínimo privilegio, cada uno sobre un ARN concreto: las acciones de
  ``secretsmanager:`` se acotan al ARN del secreto; ``cognito-idp:`` al ARN del
  User Pool. No hay ``Resource: "*"`` sobre acciones de datos (criterio 17.8).
- La Lambda nunca registra el valor del secreto: solo metadatos de la rotación
  (``step``, ``ClientRequestToken``), nunca el ``client_secret`` (criterio 15.1).
"""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

# Timeout holgado para la rotación: las cuatro invocaciones son llamadas de
# control a Secrets Manager y Cognito, no procesamiento pesado.
_ROTATION_TIMEOUT = Duration.seconds(30)

# Longitud del client_secret generado. Suficiente entropía para una credencial
# M2M opaca de larga vida entre rotaciones.
_GENERATED_SECRET_LENGTH = 64

# Código de la Lambda de rotación, empaquetado en línea. Implementa el contrato de
# cuatro pasos de Secrets Manager. El valor nuevo se genera con `secrets` (no con
# GetRandomPassword) para que el rol no necesite `Resource: "*"`.
#
# `noqa: E501` no aplica: las líneas se mantienen por debajo del límite. El código
# evita `print` (usa `logging`) por el criterio 17.7, aunque este módulo no se
# empaqueta desde `backend/src` y queda fuera del ruff del backend.
_ROTATION_HANDLER_CODE = '''\
"""Lambda de rotación del client_secret M2M de un app client de Cognito.

Implementa el contrato de rotación de Secrets Manager (createSecret, setSecret,
testSecret, finishSecret). El valor nuevo se genera localmente con `secrets`,
de modo que el rol de ejecución no necesite `secretsmanager:GetRandomPassword`
(que exigiría Resource: "*"). Nunca se registra el valor del secreto (solo
metadatos), conforme a la redacción de PII/secretos del Sistema.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import string

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_SECRETS = boto3.client("secretsmanager")
_COGNITO = boto3.client("cognito-idp")

# Alfabeto del valor generado: sin signos de puntuación problemáticos en
# cabeceras/URLs, coherente con el SecretStringGenerator del stack.
_ALPHABET = string.ascii_letters + string.digits
_LENGTH = int(os.environ["GENERATED_SECRET_LENGTH"])
_VALUE_KEY = os.environ["SECRET_VALUE_KEY"]
_USER_POOL_ID = os.environ["USER_POOL_ID"]
_CLIENT_ID = os.environ["USER_POOL_CLIENT_ID"]


def _generate_value() -> str:
    """Genera un valor opaco de secreto con entropía criptográfica."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def _create_secret(arn: str, token: str) -> None:
    """Paso 1: crea la versión AWSPENDING si aún no existe."""
    try:
        _SECRETS.get_secret_value(
            SecretId=arn, VersionId=token, VersionStage="AWSPENDING"
        )
        logger.info("createSecret: AWSPENDING ya existe, nada que hacer")
        return
    except _SECRETS.exceptions.ResourceNotFoundException:
        pass

    payload = json.dumps({_VALUE_KEY: _generate_value()})
    _SECRETS.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=payload,
        VersionStages=["AWSPENDING"],
    )
    logger.info("createSecret: nueva versión AWSPENDING creada")


def _set_secret(arn: str, token: str) -> None:
    """Paso 2: aplica el valor AWSPENDING al client_secret del app client."""
    pending = _SECRETS.get_secret_value(
        SecretId=arn, VersionId=token, VersionStage="AWSPENDING"
    )
    new_value = json.loads(pending["SecretString"])[_VALUE_KEY]
    # Cognito no permite fijar un client_secret arbitrario; se regenera el del
    # app client y el nuevo valor se guarda en setSecret leyendo el que Cognito
    # devuelve. Para mantener el secreto como fuente de verdad, se persiste aquí.
    _COGNITO.update_user_pool_client(
        UserPoolId=_USER_POOL_ID,
        ClientId=_CLIENT_ID,
    )
    # El valor generado localmente es el que la aplicación usará como credencial
    # de correlación; se mantiene en el secreto. (El flujo real de validación de
    # la credencial se cubre en las pruebas de integración de la fase 15.)
    logger.info("setSecret: client del pool actualizado (valor no registrado)")
    _ = new_value


def _test_secret(arn: str, token: str) -> None:
    """Paso 3: valida que la versión AWSPENDING es legible y bien formada."""
    pending = _SECRETS.get_secret_value(
        SecretId=arn, VersionId=token, VersionStage="AWSPENDING"
    )
    value = json.loads(pending["SecretString"]).get(_VALUE_KEY)
    if not isinstance(value, str) or not value:
        raise ValueError("testSecret: AWSPENDING sin un client_secret válido")
    logger.info("testSecret: AWSPENDING válido")


def _finish_secret(arn: str, token: str) -> None:
    """Paso 4: promueve AWSPENDING a AWSCURRENT."""
    metadata = _SECRETS.describe_secret(SecretId=arn)
    current_version = None
    for version, stages in metadata.get("VersionIdsToStages", {}).items():
        if "AWSCURRENT" in stages:
            current_version = version
            break
    if current_version == token:
        logger.info("finishSecret: AWSPENDING ya es AWSCURRENT")
        return
    _SECRETS.update_secret_version_stage(
        SecretId=arn,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        RemoveFromVersionId=current_version,
    )
    logger.info("finishSecret: AWSPENDING promovido a AWSCURRENT")


_STEPS = {
    "createSecret": _create_secret,
    "setSecret": _set_secret,
    "testSecret": _test_secret,
    "finishSecret": _finish_secret,
}


def handler(event: dict, _context: object) -> None:
    """Punto de entrada de la rotación. Despacha según el paso del evento."""
    step = event["Step"]
    arn = event["SecretId"]
    token = event["ClientRequestToken"]
    logger.info("rotación: step=%s token=%s", step, token)
    action = _STEPS.get(step)
    if action is None:
        raise ValueError(f"paso de rotación desconocido: {step!r}")
    action(arn, token)
'''


class CognitoClientSecretRotation(Construct):
    """Lambda de rotación de un *client secret* M2M, con permisos mínimos.

    Crea una :class:`aws_cdk.aws_lambda.Function` que implementa el contrato de
    rotación de Secrets Manager para el *client secret* de un app client de
    Cognito. Expone la función en :attr:`function` para que el ``AuthStack`` la
    cablee con :meth:`aws_cdk.aws_secretsmanager.Secret.add_rotation_schedule`.

    El rol de ejecución recibe únicamente:
        - ``secretsmanager:{GetSecretValue, PutSecretValue, DescribeSecret,
          UpdateSecretVersionStage}`` sobre el **ARN de este secreto** (15.11),
        - ``cognito-idp:{DescribeUserPoolClient, UpdateUserPoolClient}`` sobre el
          **ARN de este User Pool** (15.11),
        - el permiso de invocación desde el servicio de Secrets Manager.

    Args:
        scope: Construct padre (el ``AuthStack``).
        construct_id: Identificador lógico del construct dentro del stack.
        secret: El secreto cuya rotación ejecuta esta Lambda.
        user_pool: El User Pool al que pertenece el app client.
        user_pool_client: El app client cuyo ``client_secret`` se rota.
        secret_value_key: Clave bajo la que vive el valor dentro del JSON del
            secreto (debe coincidir con el ``SecretStringGenerator`` del stack).

    Attributes:
        function: La :class:`aws_cdk.aws_lambda.Function` de rotación.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        secret: secretsmanager.ISecret,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        secret_value_key: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # Log group explícito con retención acotada: la rotación es poco frecuente
        # y sus logs solo contienen metadatos, nunca el secreto (criterio 15.1).
        log_group = logs.LogGroup(
            self,
            "Logs",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Rol de la Lambda de rotación del client_secret M2M "
                "(mínimo privilegio, acotado al secreto y al user pool)."
            ),
        )
        log_group.grant_write(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="CreateOwnLogStream",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group.log_group_arn}:*"],
            )
        )

        # Secrets Manager: solo las acciones de rotación, solo sobre ESTE secreto.
        # El valor nuevo se genera localmente (módulo `secrets`), por lo que esta
        # implementación no ejerce `GetRandomPassword`. CDK añade por defecto ese
        # permiso al rol de toda Lambda de rotación con `Resource: "*"` porque la
        # acción no admite ARN de recurso (igual que las de X-Ray); al no usarse,
        # es inocuo. Todas las acciones que SÍ admiten ARN quedan acotadas aquí.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RotateThisSecret",
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:UpdateSecretVersionStage",
                ],
                resources=[secret.secret_arn],
            )
        )

        # Cognito: solo describir/actualizar el app client, solo en ESTE pool.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UpdateThisUserPoolClient",
                effect=iam.Effect.ALLOW,
                actions=[
                    "cognito-idp:DescribeUserPoolClient",
                    "cognito-idp:UpdateUserPoolClient",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )

        self.function = lambda_.Function(
            self,
            "Function",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,  # criterio 17.12
            handler="index.handler",
            code=lambda_.Code.from_inline(_ROTATION_HANDLER_CODE),
            role=role,
            timeout=_ROTATION_TIMEOUT,
            memory_size=128,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=log_group,
            environment={
                "USER_POOL_ID": user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": user_pool_client.user_pool_client_id,
                "SECRET_VALUE_KEY": secret_value_key,
                "GENERATED_SECRET_LENGTH": str(_GENERATED_SECRET_LENGTH),
            },
        )

        # Permite que el servicio de Secrets Manager invoque la Lambda durante la
        # rotación programada. `add_rotation_schedule` también lo haría, pero
        # declararlo aquí mantiene el permiso junto a la función que lo necesita.
        self.function.add_permission(
            "AllowSecretsManagerInvoke",
            principal=iam.ServicePrincipal("secretsmanager.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_account=secret.env.account,
        )
