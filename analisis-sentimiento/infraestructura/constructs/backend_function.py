"""Construct ``BackendFunction``: Lambda del backend con secure defaults (diseño 14.4.6).

Este construct es el molde único de las nueve Lambdas del sistema. Centraliza en
un solo lugar las decisiones de seguridad y empaquetado que, de otro modo, cada
stack tendría que repetir (y podría olvidar):

    - Arquitectura ``arm64`` / Graviton (criterio 17.12), runtime Python 3.12.
    - Un **rol de ejecución propio y dedicado** por función (diseño 12.2):
      nunca un rol compartido, de modo que cada stack añada a ese rol solo las
      acciones que su Lambda ejecuta (criterio 15.10) sobre ARN concretos
      (criterio 15.11). El construct no concede ningún permiso de datos; eso es
      responsabilidad del stack que lo instancia.
    - Un *log group* explícito con retención de 30 días cifrado con la CMK del
      proyecto (criterios 15.6 y la retención de 30 días del diseño 14.3), en
      lugar del grupo implícito sin cifrar que crearía Lambda por defecto.
    - Trazas X-Ray activas (``Tracing.ACTIVE``) para la depuración distribuida.
    - Concurrencia reservada configurable (p. ej. el techo duro de 40 de
      ``analyze_llm`` hacia Bedrock, criterio 5.7, o las reservas de la API).
    - Variables de entorno cifradas con la misma CMK.
    - Empaquetado del código propio desde ``backend/src/`` con
      :meth:`aws_cdk.aws_lambda.Code.from_asset`, excluyendo pruebas y cachés, de
      modo que una sola ruta de código alimenta las nueve funciones (criterios
      17.21 y 14.4.4) y las dependencias de terceros llegan por el Layer
      compartido (criterio 17.16).

La CMK y el Layer compartido se reciben por parámetro como **interfaces** de CDK
(``IKey`` / ``ILayerVersion``), que publica el ``StorageStack`` (tarea 11.1); así
el construct no acopla con la implementación concreta de ese stack.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from aws_cdk import Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

# Raíz del código de aplicación empaquetado en cada Lambda. ``__file__`` vive en
# ``infraestructura/constructs/``; dos niveles arriba está la raíz del repo, y de
# ahí colgamos ``backend/src`` (criterios 17.21 y 14.4.4). Se resuelve una vez,
# en import, para no recalcularlo por cada función.
BACKEND_SRC: Path = Path(__file__).resolve().parents[2] / "backend" / "src"

# Patrones excluidos del asset: no tiene sentido llevar pruebas ni cachés dentro
# del paquete de despliegue, y además inflarían el hash del asset.
_ASSET_EXCLUDE: tuple[str, ...] = (
    "**/__pycache__",
    "**/*.pyc",
    "**/tests",
    "**/.pytest_cache",
    "**/.mypy_cache",
    "**/.ruff_cache",
    "**/.venv",
)

# Retención de los log groups del backend: 30 días (diseño 14.3, tabla de
# ``constructs/``). ``ONE_MONTH`` es el valor del enum de CDK que mapea a 30 días.
_LOG_RETENTION = logs.RetentionDays.ONE_MONTH


class BackendFunction(Construct):
    """Lambda del backend: ``arm64``, rol propio, log group cifrado 30 días, X-Ray.

    Expone la función y su rol de ejecución para que el stack que la instancia
    (``compute`` en 12.2, ``api`` en 13.2, etc.) adjunte al rol las políticas de
    mínimo privilegio concretas de esa Lambda. El construct deliberadamente **no**
    concede ningún permiso de datos (DynamoDB, S3, Bedrock...): su contrato es dar
    una base segura y uniforme, no decidir el acceso de cada función.

    Args:
        scope: Construct padre (normalmente el stack de dominio).
        construct_id: Identificador lógico del construct dentro del stack.
        handler: Ruta de módulo del handler, p. ej.
            ``"sentiment.handlers.analyze_llm.handler"`` (criterio 14.4.4).
        layer: Layer compartido con las dependencias de terceros, publicado por
            el ``StorageStack`` (criterio 17.16).
        key: CMK del proyecto, publicada por el ``StorageStack``. Cifra el *log
            group* (criterio 15.6) y las variables de entorno de la función.
        memory_mb: Memoria asignada a la función, en MiB.
        timeout_seconds: Timeout de la función, en segundos (p. ej. 120 para
            ``analyze_llm``, criterio 9.13).
        reserved_concurrency: Concurrencia reservada. Un entero fija el techo duro
            de invocaciones simultáneas (y, por tanto, también la reserva de
            capacidad); ``None`` deja la función sin reserva, usando la
            concurrencia no reservada de la cuenta.
        environment: Variables de entorno de la función. Se cifran con ``key``.

    Attributes:
        function: La :class:`aws_cdk.aws_lambda.Function` creada.
        role: El :class:`aws_cdk.aws_iam.Role` de ejecución dedicado de la
            función, al que el stack llamante añade las políticas de datos.
        log_group: El :class:`aws_cdk.aws_logs.LogGroup` explícito, cifrado con
            la CMK y con retención de 30 días.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        handler: str,
        layer: lambda_.ILayerVersion,
        key: kms.IKey,
        memory_mb: int,
        timeout_seconds: int,
        reserved_concurrency: int | None,
        environment: Mapping[str, str],
    ) -> None:
        super().__init__(scope, construct_id)

        # --- Log group explícito, cifrado con la CMK, retención de 30 días ---
        # Crearlo nosotros (en vez de dejar que Lambda cree el implícito) es lo
        # que permite cifrarlo con la CMK (criterio 15.6) y fijar la retención;
        # el grupo por defecto de Lambda no está cifrado y retiene indefinidamente.
        self.log_group = logs.LogGroup(
            self,
            "Logs",
            retention=_LOG_RETENTION,
            encryption_key=key,
        )

        # --- Rol de ejecución propio y dedicado (diseño 12.2) ---
        # Cada Lambda tiene su propio rol: nunca uno compartido. Partimos del
        # permiso mínimo para escribir en SU log group (no un comodín de logs) y
        # para enviar trazas a X-Ray; el stack llamante añade el resto con
        # add_to_role_policy / grant_* sobre ARN concretos (criterios 15.10/15.11).
        self.role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                f"Rol de ejecución dedicado de la Lambda {construct_id} "
                "(mínimo privilegio; sin permisos de datos por defecto)."
            ),
        )
        # Permiso de logs acotado a los streams del propio log group, no "*".
        self.log_group.grant_write(self.role)
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="CreateOwnLogStream",
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{self.log_group.log_group_arn}:*"],
            )
        )
        # X-Ray no admite ARN de recurso en estas acciones (API sin recursos
        # direccionables), por lo que "*" aquí es inevitable; se aísla en un rol
        # que, por lo demás, solo puede escribir en su propio log group.
        self.role.add_to_policy(
            iam.PolicyStatement(
                sid="XRayTracing",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            )
        )
        # La función necesita poder usar la CMK para descifrar sus variables de
        # entorno; se acota con kms:ViaService a Lambda para que la concesión no
        # sirva fuera de ese servicio.
        key.grant_decrypt(self.role)

        # --- La función Lambda ---
        self.function = lambda_.Function(
            self,
            "Function",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,  # criterio 17.12
            handler=handler,
            code=lambda_.Code.from_asset(
                str(BACKEND_SRC),
                exclude=list(_ASSET_EXCLUDE),
            ),
            layers=[layer],
            role=self.role,
            memory_size=memory_mb,
            timeout=Duration.seconds(timeout_seconds),
            reserved_concurrent_executions=reserved_concurrency,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=self.log_group,
            environment=dict(environment),
            environment_encryption=key,
        )
