"""Construct ``TenantDataRole``: rol de datos con *session tags* por tenant (diseño 12.2.1).

Una política IAM adjunta a un rol es estática: no puede saber de qué tenant es
*esta* solicitud. Para que ``dynamodb:LeadingKeys`` y los prefijos de S3 se
apliquen por tenant (criterios 2.5, 2.6 y 19.12) el diseño usa **ABAC con session
tags** (sección 12.2.1): el handler asume en cada solicitud un rol de datos
etiquetando la sesión con el ``tenant_id`` (y, en la purga, el ``analysis_id``)
del claim ya verificado, y las políticas de ese rol se escriben con la variable
de política ``${aws:PrincipalTag/tenant_id}``.

Este construct materializa ese **rol de datos**. Separa dos responsabilidades:

    1. El rol de ejecución de la Lambda (``execution_role``, creado por
       :class:`~constructs.backend_function.BackendFunction`) solo puede registrar
       logs, enviar trazas y ejecutar ``sts:AssumeRole`` + ``sts:TagSession`` sobre
       **este** rol de datos. No tiene ningún permiso de DynamoDB ni de S3.
    2. El rol de datos (``self.role``) confía **únicamente** en ese rol de
       ejecución y limita las claves de etiqueta de sesión permitidas
       (``ForAllValues:StringEquals`` sobre ``aws:TagKeys``) a las que esta
       función necesita. Sus permisos de datos concretos (las declaraciones
       ``LeadingKeys`` / prefijos de S3 de 12.2, que difieren entre ``api_status``,
       ``api_download`` y ``api_purge``) los añade el stack llamante con
       :meth:`add_data_policy`, para mantener el construct reutilizable.

Resultado: aunque la comparación en código (capa 1, sección 8.4) tuviera un
defecto, IAM deniega cualquier lectura o borrado fuera del tenant, y en la purga
fuera del lote solicitado.
"""

from __future__ import annotations

from collections.abc import Sequence

from aws_cdk import aws_iam as iam
from constructs import Construct


class TenantDataRole(Construct):
    """Rol de datos asumible por *session tags* para aislar el acceso por tenant.

    El rol se construye con una política de confianza que acepta exclusivamente
    al rol de ejecución de su Lambda y que exige que las claves de las etiquetas
    de sesión sean **exactamente** las de ``tag_keys`` (ni más ni menos), de modo
    que una sesión no pueda inyectar etiquetas no previstas que relajen las
    condiciones ``${aws:PrincipalTag/...}`` de las políticas de datos.

    El construct concede al ``execution_role`` el permiso de asumir este rol con
    etiquetas (``sts:AssumeRole`` + ``sts:TagSession``), que es todo lo que la
    Lambda necesita para obtener credenciales con el ``tenant_id`` de la solicitud.

    Los permisos de datos (DynamoDB con ``dynamodb:LeadingKeys``, S3 por prefijo
    ``outputs/${aws:PrincipalTag/tenant_id}/``...) **no** se fijan aquí: cada
    función tiene su propio conjunto (sección 12.2), así que el stack llamante los
    añade con :meth:`add_data_policy`.

    Args:
        scope: Construct padre (normalmente el stack de dominio ``api``).
        construct_id: Identificador lógico del construct dentro del stack.
        execution_role: Rol de ejecución de la Lambda (de ``BackendFunction``).
            Es el **único** principal que podrá asumir este rol de datos, y el que
            recibe el permiso de ``AssumeRole``/``TagSession``.
        tag_keys: Claves de las etiquetas de sesión permitidas en el ``AssumeRole``
            (p. ej. ``("tenant_id",)`` para ``api_status``/``api_download`` o
            ``("tenant_id", "analysis_id")`` para ``api_purge``). La política de
            confianza exige que el conjunto de claves de la sesión sea exactamente
            este (criterio de 12.2.1, paso 2).
        role_name: Nombre explícito del rol de datos
            (p. ej. ``sentiment-{env}-api_status-data``). Si es ``None``, CDK
            genera un nombre; conviene fijarlo para que la relación función ↔ rol
            de datos sea legible en auditoría.

    Attributes:
        role: El :class:`aws_cdk.aws_iam.Role` de datos. El stack llamante le
            añade las declaraciones de mínimo privilegio con :meth:`add_data_policy`.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        execution_role: iam.IRole,
        tag_keys: Sequence[str],
        role_name: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        if not tag_keys:
            # Un rol de datos sin claves de etiqueta no tendría forma de resolver
            # el tenant: fallamos en síntesis en vez de desplegar algo inseguro.
            raise ValueError(
                "TenantDataRole requiere al menos una clave de session tag "
                "(p. ej. 'tenant_id'); se recibió una secuencia vacía."
            )

        # Claves ordenadas y sin duplicados para que la condición sea determinista
        # (el hash del template no cambia por el orden de entrada).
        self._tag_keys: tuple[str, ...] = tuple(dict.fromkeys(tag_keys))

        # --- Política de confianza: solo el rol de ejecución, con session tags ---
        # El principal es el ARN del rol de ejecución de la Lambda; nadie más puede
        # asumir este rol. La condición ``ForAllValues:StringEquals`` sobre
        # ``aws:TagKeys`` acota el conjunto de claves de etiqueta que la sesión
        # puede transportar exactamente a ``tag_keys`` (12.2.1, paso 2): impide
        # que se inyecten etiquetas no previstas.
        trust_principal = iam.ArnPrincipal(execution_role.role_arn).with_conditions(
            {
                "StringEquals": {
                    # Las claves de etiqueta de la sesión deben ser un subconjunto
                    # de las permitidas; combinado con TagSession en el otro rol,
                    # el conjunto efectivo queda fijado a ``tag_keys``.
                    "aws:TagKeys": list(self._tag_keys)
                }
            }
        )

        self.role = iam.Role(
            self,
            "Role",
            role_name=role_name,
            assumed_by=trust_principal,
            # El rol de datos solo lo asume la Lambda tras verificar el token: no
            # necesita (ni debe tener) permisos de logs/X-Ray propios, los tiene
            # el rol de ejecución.
            description=(
                "Rol de datos asumible por session tags para aislamiento por "
                f"tenant (keys: {', '.join(self._tag_keys)})."
            ),
        )

        # El AssumeRole con etiquetas requiere BOTH acciones: AssumeRole (obtener
        # las credenciales) y TagSession (adjuntar las etiquetas de sesión). Sin
        # TagSession, la llamada etiquetada falla; sin AssumeRole, no hay sesión.
        self.role.assume_role_policy.add_statements(  # type: ignore[union-attr]
            iam.PolicyStatement(
                sid="AllowTaggedAssumeFromExecutionRole",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(execution_role.role_arn)],
                actions=["sts:TagSession"],
                conditions={
                    "ForAllValues:StringEquals": {
                        "aws:TagKeys": list(self._tag_keys)
                    }
                },
            )
        )

        # El lado del principal: el rol de ejecución recibe el permiso de asumir
        # ESTE rol de datos con etiquetas, y nada más. Es el único acoplamiento
        # entre ambos roles (12.2.1, paso 1).
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="AssumeTenantDataRole",
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[self.role.role_arn],
            )
        )

    @property
    def tag_keys(self) -> tuple[str, ...]:
        """Claves de session tag permitidas por este rol (orden determinista)."""

        return self._tag_keys

    def add_data_policy(self, statement: iam.PolicyStatement) -> None:
        """Añade una declaración de permisos de datos al rol de datos.

        Pensado para que el stack llamante inyecte las declaraciones de mínimo
        privilegio por tenant de la sección 12.2 (p. ej. ``dynamodb:Query`` con
        ``Condition: dynamodb:LeadingKeys = "TENANT#${aws:PrincipalTag/tenant_id}*"``,
        o ``s3:GetObject`` sobre ``outputs/${aws:PrincipalTag/tenant_id}/*``). El
        construct no las fija porque difieren entre ``api_status``, ``api_download``
        y ``api_purge``.

        Args:
            statement: Declaración IAM a adjuntar al rol de datos. Debería usar la
                variable de política ``${aws:PrincipalTag/<clave>}`` para una de las
                claves declaradas en ``tag_keys``, y nombrar ARN concretos
                (criterios 15.10/15.11); el construct no lo verifica.
        """

        self.role.add_to_policy(statement)
