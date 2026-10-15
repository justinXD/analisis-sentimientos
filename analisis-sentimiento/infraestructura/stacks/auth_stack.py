"""Stack de dominio ``auth`` (identidad y autenticación M2M). Tarea 13.1.

Dominio: identidad autoritativa del Sistema y autorización por *scope*. Es el
emisor de los Access_Tokens que la ``api`` (tarea 13.2) verifica en su autorizador
JWT nativo.

Recursos
--------
- **Cognito User Pool** (:attr:`user_pool`). Almacén de identidad y emisor de los
  Access_Tokens. Declara el atributo personalizado inmutable ``custom:tenant_id``,
  del que el Sistema deriva el Tenant de cada solicitud (criterios 1.9 / 2.1). No
  se habilita el auto-registro: los Sistemas_M2M no son usuarios finales, sino app
  clients con el flujo ``client_credentials``.

- **Resource Server** (:attr:`resource_server`) con identificador ``sentiment`` y
  los tres *scopes* de autorización del Sistema (criterios 1.6, 1.7, 19.1):
  ``analyses.write`` (envío de Lotes), ``analyses.read`` (listado/estado/descarga)
  y ``analyses.purge`` (Purga). El nombre de *scope* completo que el autorizador
  comprueba es ``sentiment/analyses.{write,read,purge}``, expuesto como
  :class:`aws_cdk.aws_cognito.OAuthScope` en :attr:`scope_write` / :attr:`scope_read`
  / :attr:`scope_purge` para que la ``api`` lo cablee por ruta sin redeclararlo.

- **User Pool Domain** (:attr:`user_pool_domain`). El prefijo del dominio hospedado
  de Cognito expone el *endpoint* del token (``/oauth2/token``) que los
  Sistemas_M2M usan para obtener su Access_Token mediante ``client_credentials``;
  sin un dominio, ese flujo no tiene *endpoint* al que llamar.

- **App clients** ``client_credentials`` con *client secret* generado por Cognito:

  - :attr:`m2m_client`: cliente de servicio general, con los *scopes*
    ``analyses.write`` y ``analyses.read``.
  - :attr:`purge_client`: cliente **dedicado a la Purga**, con **únicamente** el
    *scope* ``analyses.purge`` (mínimo privilegio, criterio 15.11): la operación
    destructiva de la Purga queda aislada en una credencial que no puede enviar ni
    leer Lotes.

  Ambos emiten Access_Tokens con una vigencia de 3.600 segundos como máximo
  (criterio 1.8) y con revocación de token habilitada.

- **Secretos M2M en Secrets Manager** (:attr:`m2m_secret`, :attr:`purge_secret`),
  uno por app client. El valor del secreto se **genera** en el momento del
  despliegue (:class:`~aws_cdk.aws_secretsmanager.SecretStringGenerator`): el
  template no embebe ningún valor secreto (criterio 17.20) ni el *client secret*
  real del app client de Cognito. Cada secreto tiene **rotación automática
  habilitada con un intervalo de 90 días** (criterio 1.13), ejecutada por una
  Lambda de rotación dedicada y acotada a ese único secreto y a este User Pool
  (:class:`constructs.secret_rotation.CognitoClientSecretRotation`).

Dependencias (diseño 14.4.2): **ninguna**. Es un stack base con estado y se
despliega con ``termination_protection=True`` para que un ``cdk destroy``
accidental no borre el almacén de identidad ni invalide todos los Access_Tokens
en circulación. El User Pool y los secretos se retienen (``RemovalPolicy.RETAIN``)
por el mismo motivo.

Seguridad
---------
- El *client secret* **no se expone** en ningún ``CfnOutput`` ni log: los secretos
  en Secrets Manager son el único canal para obtenerlo, con rotación periódica.
- Los app clients no habilitan ningún flujo OAuth interactivo (``authorization_code``
  / ``implicit``): solo ``client_credentials``, que es el único flujo M2M (D6).
- El *scope* de la Purga vive en un app client separado para que el compromiso de
  la credencial de lectura/escritura no habilite el borrado de datos.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from config.environments import EnvConfig
from constructs.secret_rotation import CognitoClientSecretRotation

# --- Resource Server: identificador y scopes (criterios 1.6, 1.7, 19.1) ---
# El identificador del Resource Server prefija cada scope en el token: el claim
# `scope` del Access_Token lleva, p. ej., `sentiment/analyses.write`. El
# autorizador de la `api` (tarea 13.2) comprueba exactamente estos nombres.
_RESOURCE_SERVER_IDENTIFIER = "sentiment"

_SCOPE_WRITE_NAME = "analyses.write"
_SCOPE_READ_NAME = "analyses.read"
_SCOPE_PURGE_NAME = "analyses.purge"

# Atributo personalizado del que el Sistema deriva el Tenant (criterios 1.9 / 2.1).
# Es inmutable: el `tenant_id` de un principal no puede cambiar tras su creación,
# para que no se reasigne la pertenencia de datos de un Tenant a otro.
_TENANT_ID_ATTRIBUTE = "tenant_id"
_TENANT_ID_MIN_LEN = 1
_TENANT_ID_MAX_LEN = 128

# Vigencia máxima del Access_Token de un Sistema_M2M (criterio 1.8).
_ACCESS_TOKEN_VALIDITY = Duration.hours(1)

# Intervalo máximo de rotación del client_secret en Secrets Manager (criterio 1.13).
_SECRET_ROTATION_DAYS = 90

# Clave bajo la que el SecretStringGenerator deposita el valor generado en el JSON
# del secreto. El valor se genera en el despliegue; nunca se escribe en el código
# ni en el template (criterio 17.20).
_SECRET_VALUE_KEY = "client_secret"


class AuthStack(Stack):
    """Stack del dominio de identidad: User Pool, scopes, app clients y secretos.

    Expone sus recursos como atributos públicos tipados con **interfaces** de CDK
    (diseño 14.4.3), de modo que el ``api_stack`` (tarea 13.2) los consuma por
    *props* sin acoplarse a la implementación ni redeclarar los *scopes*.

    Attributes:
        user_pool: El Cognito User Pool emisor de los Access_Tokens
            (:class:`aws_cdk.aws_cognito.IUserPool`).
        user_pool_domain: El dominio hospedado que expone el *endpoint* del token
            (:class:`aws_cdk.aws_cognito.UserPoolDomain`).
        resource_server: El Resource Server ``sentiment``
            (:class:`aws_cdk.aws_cognito.IUserPoolResourceServer`).
        scope_write: *Scope* OAuth ``sentiment/analyses.write``
            (:class:`aws_cdk.aws_cognito.OAuthScope`).
        scope_read: *Scope* OAuth ``sentiment/analyses.read``.
        scope_purge: *Scope* OAuth ``sentiment/analyses.purge``.
        m2m_client: App client general con los *scopes* de escritura y lectura
            (:class:`aws_cdk.aws_cognito.IUserPoolClient`).
        purge_client: App client dedicado a la Purga, con **solo** el *scope*
            ``analyses.purge``.
        m2m_secret: Secreto rotado del ``m2m_client`` en Secrets Manager
            (:class:`aws_cdk.aws_secretsmanager.ISecret`).
        purge_secret: Secreto rotado del ``purge_client`` en Secrets Manager.

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        **kwargs: object,
    ) -> None:
        # Protección de terminación: es un stack base con estado (diseño 14.4.2).
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)
        self._config = config
        prefix = f"{config.project}-{config.env_name}"

        # 1) El almacén de identidad.
        self.user_pool: cognito.IUserPool = self._create_user_pool(prefix)

        # 2) El dominio hospedado (endpoint del token para client_credentials).
        self.user_pool_domain = self._create_domain(prefix)

        # 3) El Resource Server y sus tres scopes.
        (
            self.resource_server,
            self.scope_write,
            self.scope_read,
            self.scope_purge,
        ) = self._create_resource_server()

        # 4) Los app clients client_credentials (general y de purga).
        self.m2m_client: cognito.IUserPoolClient = self._create_app_client(
            construct_id="M2mClient",
            client_name=f"{prefix}-m2m",
            scopes=[self.scope_write, self.scope_read],
        )
        # Mínimo privilegio: el cliente de purga NO lleva write ni read (15.11).
        self.purge_client: cognito.IUserPoolClient = self._create_app_client(
            construct_id="PurgeClient",
            client_name=f"{prefix}-purge",
            scopes=[self.scope_purge],
        )

        # 5) Los secretos M2M generados y rotados a 90 días (criterios 1.13 / 17.20).
        self.m2m_secret: secretsmanager.ISecret = self._create_rotated_secret(
            construct_id="M2mSecret",
            secret_name=f"{prefix}-m2m-client-secret",
            client=self.m2m_client,
            description=(
                "client_secret generado del app client M2M general "
                "(scopes analyses.write/analyses.read). Rotación de 90 días."
            ),
        )
        self.purge_secret: secretsmanager.ISecret = self._create_rotated_secret(
            construct_id="PurgeSecret",
            secret_name=f"{prefix}-purge-client-secret",
            client=self.purge_client,
            description=(
                "client_secret generado del app client dedicado a la Purga "
                "(scope analyses.purge). Rotación de 90 días."
            ),
        )

    def _create_user_pool(self, prefix: str) -> cognito.UserPool:
        """Crea el User Pool con el atributo personalizado ``custom:tenant_id``.

        El atributo ``tenant_id`` es **inmutable**: la pertenencia de un principal a
        un Tenant no puede reasignarse tras crearse, lo que impide filtrar datos de
        un Tenant a otro por una mutación del claim (criterios 1.9 / 2.1). No se
        habilita el auto-registro ni la recuperación por SMS: los consumidores son
        Sistemas_M2M, no usuarios finales.

        Args:
            prefix: Prefijo ``{proyecto}-{env}`` para el nombre del User Pool.

        Returns:
            El :class:`aws_cdk.aws_cognito.UserPool` creado.
        """

        return cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{prefix}-users",
            # Sistemas_M2M, no usuarios finales: sin auto-registro.
            self_sign_up_enabled=False,
            custom_attributes={
                _TENANT_ID_ATTRIBUTE: cognito.StringAttribute(
                    min_len=_TENANT_ID_MIN_LEN,
                    max_len=_TENANT_ID_MAX_LEN,
                    # Inmutable: el tenant de un principal no se reasigna (2.1).
                    mutable=False,
                )
            },
            # Postura de seguridad conservadora para el almacén de identidad.
            # No se habilita la protección avanzada frente a amenazas: es una
            # característica orientada a usuarios finales (riesgo de credenciales,
            # inicios de sesión sospechosos) que exige el plan de características
            # "Plus"; los consumidores aquí son Sistemas_M2M con client_credentials,
            # donde no aporta y solo añadiría coste.
            account_recovery=cognito.AccountRecovery.NONE,
            # Stack base con estado: retener el almacén de identidad ante un
            # destroy accidental (diseño 14.4.2). Perder el pool invalidaría todos
            # los Access_Tokens y los app clients.
            removal_policy=RemovalPolicy.RETAIN,
        )

    def _create_domain(self, prefix: str) -> cognito.UserPoolDomain:
        """Crea el dominio hospedado que expone el *endpoint* del token.

        El flujo ``client_credentials`` obtiene el Access_Token en
        ``https://{prefix}-auth.auth.{region}.amazoncognito.com/oauth2/token``. Sin
        un dominio, Cognito no publica ese *endpoint* y los Sistemas_M2M no tienen a
        dónde llamar.

        Args:
            prefix: Prefijo ``{proyecto}-{env}`` para el prefijo del dominio.

        Returns:
            El :class:`aws_cdk.aws_cognito.UserPoolDomain` creado.
        """

        return self.user_pool.add_domain(
            "UserPoolDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{prefix}-auth"
            ),
        )

    def _create_resource_server(
        self,
    ) -> tuple[
        cognito.IUserPoolResourceServer,
        cognito.OAuthScope,
        cognito.OAuthScope,
        cognito.OAuthScope,
    ]:
        """Crea el Resource Server ``sentiment`` y sus tres *scopes* OAuth.

        Los *scopes* se declaran en el Resource Server y luego se envuelven en
        :class:`aws_cdk.aws_cognito.OAuthScope` para poder (1) concederlos a los app
        clients y (2) exportarlos a la ``api``, que los comprueba por ruta. El nombre
        completo que viaja en el claim ``scope`` del token es
        ``sentiment/analyses.{write,read,purge}``.

        Returns:
            Una tupla ``(resource_server, scope_write, scope_read, scope_purge)``.
        """

        scope_write = cognito.ResourceServerScope(
            scope_name=_SCOPE_WRITE_NAME,
            scope_description="Enviar Lotes de análisis (POST /v1/analyses).",
        )
        scope_read = cognito.ResourceServerScope(
            scope_name=_SCOPE_READ_NAME,
            scope_description="Listar, consultar y descargar Lotes propios.",
        )
        scope_purge = cognito.ResourceServerScope(
            scope_name=_SCOPE_PURGE_NAME,
            scope_description="Purgar (eliminar) un Lote propio terminal.",
        )

        resource_server = self.user_pool.add_resource_server(
            "ResourceServer",
            identifier=_RESOURCE_SERVER_IDENTIFIER,
            scopes=[scope_write, scope_read, scope_purge],
        )

        return (
            resource_server,
            cognito.OAuthScope.resource_server(resource_server, scope_write),
            cognito.OAuthScope.resource_server(resource_server, scope_read),
            cognito.OAuthScope.resource_server(resource_server, scope_purge),
        )

    def _create_app_client(
        self,
        *,
        construct_id: str,
        client_name: str,
        scopes: list[cognito.OAuthScope],
    ) -> cognito.UserPoolClient:
        """Crea un app client ``client_credentials`` con *client secret*.

        El cliente habilita **únicamente** el flujo ``client_credentials`` (el único
        flujo M2M, decisión D6): no se activa ningún flujo interactivo
        (``authorization_code`` / ``implicit``). El Access_Token caduca a los 3.600
        segundos como máximo (criterio 1.8) y la revocación de token queda activa.

        Args:
            construct_id: Identificador lógico del app client en el stack.
            client_name: Nombre legible del app client.
            scopes: *Scopes* OAuth concedidos a este cliente (mínimo privilegio).

        Returns:
            El :class:`aws_cdk.aws_cognito.UserPoolClient` creado.
        """

        return self.user_pool.add_client(
            construct_id,
            user_pool_client_name=client_name,
            # client_credentials exige un client secret (confidencial, M2M).
            generate_secret=True,
            # Vigencia máxima del Access_Token: 3.600 s (criterio 1.8).
            access_token_validity=_ACCESS_TOKEN_VALIDITY,
            enable_token_revocation=True,
            auth_flows=cognito.AuthFlow(),  # sin flujos de usuario/contraseña
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    client_credentials=True,
                    # Flujos interactivos deshabilitados explícitamente.
                    authorization_code_grant=False,
                    implicit_code_grant=False,
                ),
                scopes=scopes,
            ),
            # No listar providers externos: la credencial es la del propio pool.
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            prevent_user_existence_errors=True,
        )

    def _create_rotated_secret(
        self,
        *,
        construct_id: str,
        secret_name: str,
        client: cognito.IUserPoolClient,
        description: str,
    ) -> secretsmanager.Secret:
        """Crea un secreto generado en Secrets Manager con rotación de 90 días.

        El valor del secreto se **genera** en el despliegue mediante
        :class:`~aws_cdk.aws_secretsmanager.SecretStringGenerator`: ni el código ni
        el template contienen un valor secreto (criterio 17.20). La rotación
        automática se habilita con un intervalo de 90 días (criterio 1.13),
        ejecutada por una Lambda dedicada y acotada a este único secreto y a este
        User Pool.

        Args:
            construct_id: Identificador lógico del secreto en el stack.
            secret_name: Nombre del secreto en Secrets Manager.
            client: App client cuyo ``client_secret`` representa el secreto; la
                Lambda de rotación lo actualiza en Cognito.
            description: Descripción legible del secreto (sin valores sensibles).

        Returns:
            El :class:`aws_cdk.aws_secretsmanager.Secret` creado, con su rotación.
        """

        secret = secretsmanager.Secret(
            self,
            construct_id,
            secret_name=secret_name,
            description=description,
            # El valor NUNCA se escribe en el código ni en el template: se genera
            # en el despliegue (criterio 17.20 / steering de seguridad).
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key=_SECRET_VALUE_KEY,
                password_length=64,
                # Un client_secret es opaco; se excluyen caracteres problemáticos
                # en cabeceras/URLs para que sea seguro transportarlo.
                exclude_punctuation=True,
                exclude_characters='"@/\\',
                require_each_included_type=True,
            ),
            # Stack base con estado: retener el secreto ante un destroy accidental.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Lambda de rotación dedicada, con permisos de mínimo privilegio acotados a
        # ESTE secreto y a ESTE User Pool (criterios 15.10 / 15.11).
        rotation = CognitoClientSecretRotation(
            self,
            f"{construct_id}Rotation",
            secret=secret,
            user_pool=self.user_pool,
            user_pool_client=client,
            secret_value_key=_SECRET_VALUE_KEY,
        )

        # Rotación automática con intervalo máximo de 90 días (criterio 1.13).
        secret.add_rotation_schedule(
            "RotationSchedule",
            rotation_lambda=rotation.function,
            automatically_after=Duration.days(_SECRET_ROTATION_DAYS),
        )

        return secret
