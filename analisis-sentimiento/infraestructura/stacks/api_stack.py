"""Stack de dominio ``api`` (exposición HTTP). Tarea 13.2.

Dominio: la frontera pública del Sistema. Expone la API REST que los Sistemas_M2M
consumen y encapsula las decisiones de autenticación/autorización en el borde, el
rate limiting (WAF), la cuota por Tenant (Usage_Plan), el aislamiento CORS por
entorno y las cuatro Lambdas de la capa de API con sus roles de datos de mínimo
privilegio.

Recursos
--------
- **API Gateway REST** (:attr:`rest_api`) con integración *proxy* Lambda y cinco
  rutas sobre ``/v1/analyses`` (diseño 18.3):

  - ``POST   /v1/analyses``                      → ``api_submit``   (scope ``analyses.write``)
  - ``GET    /v1/analyses``                      → ``api_status``   (scope ``analyses.read``)
  - ``GET    /v1/analyses/{analysis_id}``        → ``api_status``   (scope ``analyses.read``)
  - ``GET    /v1/analyses/{analysis_id}/download``→ ``api_download`` (scope ``analyses.read``)
  - ``DELETE /v1/analyses/{analysis_id}``        → ``api_purge``    (scope ``analyses.purge``)

- **Autorizador JWT nativo de Cognito** (:attr:`authorizer`), un
  :class:`aws_cdk.aws_apigateway.CognitoUserPoolsAuthorizer` cableado al User Pool
  del ``auth_stack``. API Gateway valida la **firma** contra el JWKS del pool, el
  **issuer** (``iss``), la **audiencia** / ``client_id`` (los app clients del pool,
  criterio 1.5) y la **expiración** (``exp``) **antes** de invocar ninguna Lambda
  (criterios 1.1-1.5). El **scope por ruta** (criterios 1.6, 1.7, 19.2) se declara
  en cada método con ``authorization_scopes``, usando los *scopes* que el
  ``auth_stack`` ya expone (``scope_write`` / ``scope_read`` / ``scope_purge``). Un
  token sin el scope de la ruta recibe 403 **genérico** sin invocar la Lambda
  (criterio 1.17); un token ausente, caducado o con firma inválida recibe 401
  genérico (criterios 1.1-1.4, 1.17).

- **WAF Web ACL** (:attr:`web_acl`) de ámbito ``REGIONAL``, asociada al *stage* de
  la API (:attr:`web_acl_association`), con, en orden (diseño 12.7): tres grupos de
  reglas gestionadas de AWS (``CommonRuleSet``, ``KnownBadInputs``,
  ``AmazonIpReputationList``) y una regla de **rate limiting** por IP de 2.000
  peticiones en 5 minutos (criterios 15.12 / 18.x). El modo (``COUNT`` para
  observar o ``BLOCK`` para bloquear) lo fija ``config.waf_mode`` por entorno: en
  ``dev``/``stage`` se observa, en ``prod`` se bloquea.

- **Usage_Plan** (:attr:`usage_plan`) con *throttling* de 10 rps en ráfaga y una
  cuota de 1.000 envíos de Lote por día (criterio 15.13), más una **API_Key** de
  ejemplo asociada. La API_Key identifica al consumidor ante el Usage_Plan para
  medir y limitar; **no** autentica ni deriva el ``tenant_id`` (criterio 1.12): esa
  identidad la aporta exclusivamente el JWT.

- **Las cuatro Lambdas de la capa de API**, construidas con
  :class:`~constructs.backend_function.BackendFunction` (``arm64``, rol propio,
  *log group* cifrado con la CMK y retención de 30 días, X-Ray), cada una con su
  **concurrencia reservada** (criterios 18.10-18.12 y el objetivo de ``api_purge``):

  - ``api_submit``   → 20   (criterio 18.10)
  - ``api_status``   → 30   (criterio 18.11)
  - ``api_download`` → 10   (criterio 18.12)
  - ``api_purge``    → 2    (operación destructiva y poco frecuente)

Aislamiento multi-tenant por IAM (diseño 12.2 / 12.2.1)
-------------------------------------------------------
``api_status``, ``api_download`` y ``api_purge`` no acceden a los datos con su rol
de ejecución: asumen en cada solicitud un **rol de datos** (:class:`TenantDataRole`)
etiquetando la sesión con el ``tenant_id`` del claim verificado (y el
``analysis_id`` de la ruta, en la purga). Las políticas de ese rol de datos se
escriben con ``${aws:PrincipalTag/tenant_id}`` y ``dynamodb:LeadingKeys`` /
prefijos de S3, de modo que IAM deniega estructuralmente cualquier lectura o
borrado fuera del Tenant, aunque la comparación en código fallara (criterios 2.5,
2.6, 19.12). ``api_submit`` conserva en v1 un rol sin *session tags*: escribe en
``inputs/{tenant_id}/`` con el prefijo derivado del claim y encola en la
Cola_Solicitudes (diseño 12.2, nota final).

Ninguna política concede acciones de datos sobre ``Resource: "*"`` (criterios 15.11
/ 17.8): todos los ARN son concretos y los prefijos de S3 / las claves de DynamoDB
se acotan por *session tag*.

Seguridad
---------
- CORS restringido a la lista blanca de ``config.cors_origins`` por entorno; nunca
  ``*`` sobre datos sensibles (criterio 15.9 / steering de seguridad).
- 401/403 genéricos: el autorizador nativo no revela qué claim, scope o firma
  motivó el rechazo (criterio 1.17).
- TLS 1.2 como mínimo: cuando se configura un dominio personalizado, su
  *security policy* es :data:`_MIN_TLS_SECURITY_POLICY` (criterio 15.15). En v1 no
  se despliega dominio; el valor se centraliza aquí para que añadirlo no reabra la
  decisión.

Dependencias (diseño 14.4.2): ``auth`` (User Pool + scopes), ``storage`` (CMK,
bucket, Layer), ``data`` (tabla), ``ingest`` (Cola_Solicitudes) y ``notifications``
(Bus_Eventos).
"""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct
from constructs.backend_function import BackendFunction
from constructs.tenant_data_role import TenantDataRole

from config.environments import EnvConfig
from stacks.auth_stack import AuthStack
from stacks.data_stack import DataStack
from stacks.ingest_stack import IngestStack
from stacks.naming import event_bus_name
from stacks.notifications_stack import NotificationsStack
from stacks.storage_stack import StorageStack

# --- Rutas de los handlers (diseño 14.4.4) ---
# Cadenas de módulo que apuntan a los handlers empaquetados en ``backend/src``.
# Coinciden con los módulos reales de ``sentiment/handlers/`` (tarea 9.1).
_HANDLER_SUBMIT = "sentiment.handlers.api_submit.handler"
_HANDLER_STATUS = "sentiment.handlers.api_status.handler"
_HANDLER_DOWNLOAD = "sentiment.handlers.api_download.handler"
_HANDLER_PURGE = "sentiment.handlers.api_purge.handler"

# --- Concurrencia reservada por Lambda (criterios 18.10/18.11/18.12) ---
# Techo duro de invocaciones simultáneas por función. ``api_purge`` no tiene un
# criterio explícito; se reserva baja porque es una operación destructiva y poco
# frecuente, y así no compite por la concurrencia no reservada de la cuenta.
_RESERVED_SUBMIT = 20  # criterio 18.10
_RESERVED_STATUS = 30  # criterio 18.11
_RESERVED_DOWNLOAD = 10  # criterio 18.12
_RESERVED_PURGE = 2  # operación destructiva, baja frecuencia

# --- Memoria y timeout por Lambda de API ---
# Son adaptadores *thin* de I/O (DynamoDB/S3/SQS): ligeros en CPU. 256 MiB basta.
# El timeout queda holgadamente por debajo del límite de integración de 29 s de la
# API_Gateway (criterio 18.1), de modo que un cuelgue nunca supere ese tope.
_MEMORY_MB = 256
_TIMEOUT_S = 29

# --- WAF: regla de rate limiting por IP (criterios 15.12 / 18.x; diseño 12.7) ---
# AWS WAF evalúa el límite basado en IP sobre una ventana deslizante de 5 minutos
# (300 s), que es el periodo fijo del tipo de regla ``RateBasedStatement``.
_WAF_RATE_LIMIT = 2000  # peticiones por IP en 5 minutos (criterio 15.12)

# Grupos de reglas gestionadas de AWS, en el orden del diseño 12.7. Son la primera
# línea de defensa (OWASP común, entradas maliciosas conocidas, IPs con mala
# reputación) antes de la regla de rate limiting propia.
_WAF_MANAGED_RULE_GROUPS: tuple[str, ...] = (
    "AWSManagedRulesCommonRuleSet",
    "AWSManagedRulesKnownBadInputsRuleSet",
    "AWSManagedRulesAmazonIpReputationList",
)

# --- Usage_Plan: throttling y cuota por Tenant (criterio 15.13) ---
_USAGE_PLAN_RATE_RPS = 10  # régimen sostenido, peticiones/segundo
_USAGE_PLAN_BURST = 10  # ráfaga máxima (criterio 15.13: 10 rps burst)
_USAGE_PLAN_QUOTA_PER_DAY = 1000  # envíos de Lote por día (criterio 15.13)

# --- CORS (criterio 15.9; diseño 12.6) ---
# Cabeceras y métodos realmente usados por la API. ``allow_credentials=False``
# porque la autenticación es ``Authorization: Bearer``, no cookies.
_CORS_ALLOW_HEADERS: tuple[str, ...] = ("Content-Type", "Authorization", "X-Amz-Date")
_CORS_ALLOW_METHODS: tuple[str, ...] = ("GET", "POST", "DELETE", "OPTIONS")
_CORS_MAX_AGE = Duration.seconds(600)

# --- TLS mínimo del dominio personalizado (criterio 15.15; diseño 12.3) ---
# Centralizado aquí: cuando se añada un dominio personalizado, su `security_policy`
# debe ser este valor. En v1 no se despliega dominio (no hay certificado ACM en la
# configuración), así que el valor queda documentado para no reabrir la decisión.
_MIN_TLS_SECURITY_POLICY = apigateway.SecurityPolicy.TLS_1_2

# --- Variables de entorno de las Lambdas de API ---
# ``CONFIG_ENV`` habilita el fallback a Parameter_Store del backend (no es secreto).
# ``TABLE_NAME`` lo resuelve ``DynamoRepository``; ``BUCKET_NAME``, ``S3Storage``;
# ``EVENT_BUS_NAME``, el publicador de ``api_submit`` (cierre del Lote en FAILED,
# criterio 10.17). ``DATA_ROLE_ARN`` es el ARN del rol de datos que el handler
# asume por solicitud (ABAC con session tags, diseño 12.2.1); no es un secreto.
_ENV_CONFIG = "CONFIG_ENV"
_ENV_TABLE_NAME = "TABLE_NAME"
_ENV_BUCKET_NAME = "BUCKET_NAME"
_ENV_EVENT_BUS_NAME = "EVENT_BUS_NAME"
_ENV_REQUESTS_QUEUE_URL = "REQUESTS_QUEUE_URL"
_ENV_DATA_ROLE_ARN = "DATA_ROLE_ARN"

# --- Prefijos de S3 y variables de política ABAC (diseño 12.2 / 12.2.1) ---
# La variable de política ``${aws:PrincipalTag/<clave>}`` se resuelve en runtime al
# valor de la session tag que el handler adjunta al asumir el rol de datos.
_TAG_TENANT = "tenant_id"
_TAG_ANALYSIS = "analysis_id"
_TENANT_TAG_VAR = f"${{aws:PrincipalTag/{_TAG_TENANT}}}"
_ANALYSIS_TAG_VAR = f"${{aws:PrincipalTag/{_TAG_ANALYSIS}}}"

# Claves de DynamoDB (pk) por Tenant: ``TENANT#{tenant_id}#...`` (criterio 2.1).
_DDB_TENANT_PREFIX = f"TENANT#{_TENANT_TAG_VAR}"
_DDB_TENANT_ANALYSIS_PREFIX = f"TENANT#{_TENANT_TAG_VAR}#ANALYSIS#{_ANALYSIS_TAG_VAR}"

# Prefijos de objeto de S3 por Tenant / por Lote (diseño 6.5 / 12.2).
_S3_INPUTS_TENANT = f"inputs/{_TENANT_TAG_VAR}/*"
_S3_OUTPUTS_TENANT = f"outputs/{_TENANT_TAG_VAR}/*"
_S3_MAP_RESULTS_ANALYSIS = f"map-results/{_ANALYSIS_TAG_VAR}/*"


class ApiStack(Stack):
    """Stack del dominio de exposición: API REST, autorizador JWT, WAF, Usage_Plan y Lambdas.

    Expone sus recursos como atributos públicos tipados (diseño 14.4.3) para que el
    ``observability_stack`` (tarea 13.5) alarme sobre ellos (p. ej. 4xx/5xx del
    *stage*, errores de las Lambdas) sin acoplarse a la implementación.

    Attributes:
        rest_api: La API REST pública (:class:`aws_cdk.aws_apigateway.RestApi`).
        authorizer: El autorizador JWT nativo de Cognito
            (:class:`aws_cdk.aws_apigateway.CognitoUserPoolsAuthorizer`).
        web_acl: La Web ACL de WAF asociada al *stage*
            (:class:`aws_cdk.aws_wafv2.CfnWebACL`).
        web_acl_association: La asociación WAF ↔ *stage*.
        usage_plan: El Usage_Plan con throttling y cuota por Tenant.
        api_submit: Lambda de ``POST /v1/analyses`` (reserva 20).
        api_status: Lambda de ``GET /v1/analyses[/{analysis_id}]`` (reserva 30).
        api_download: Lambda de ``GET /v1/analyses/{analysis_id}/download`` (reserva 10).
        api_purge: Lambda de ``DELETE /v1/analyses/{analysis_id}`` (reserva 2).

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
        auth: Stack de identidad del que depende (User Pool, scopes, app clients).
        storage: Stack de almacenamiento del que depende (CMK, Layer, bucket).
        data: Stack de datos del que depende (tabla).
        ingest: Stack de ingesta del que depende (Cola_Solicitudes).
        notifications: Stack de notificación del que depende (Bus_Eventos).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        auth: AuthStack,
        storage: StorageStack,
        data: DataStack,
        ingest: IngestStack,
        notifications: NotificationsStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._config = config
        self._auth = auth
        self._storage = storage
        self._data = data
        self._ingest = ingest
        self._notifications = notifications
        self.add_dependency(auth)
        self.add_dependency(storage)
        self.add_dependency(data)
        self.add_dependency(ingest)
        self.add_dependency(notifications)

        self._prefix = f"{config.project}-{config.env_name}"

        # 1) Las cuatro Lambdas de API, con sus roles de datos de mínimo privilegio.
        self.api_submit: lambda_.IFunction = self._build_submit()
        self.api_status: lambda_.IFunction = self._build_status()
        self.api_download: lambda_.IFunction = self._build_download()
        self.api_purge: lambda_.IFunction = self._build_purge()

        # 2) La API REST con el autorizador JWT nativo y el scope por ruta.
        self.rest_api, self.authorizer = self._build_rest_api()

        # 3) El Usage_Plan (throttling + cuota por Tenant) y su API_Key.
        self.usage_plan = self._build_usage_plan()

        # 4) El WAF (rate limiting + reglas gestionadas) asociado al stage.
        self.web_acl, self.web_acl_association = self._build_waf()

    # --- Entorno común de las Lambdas de API --------------------------------

    def _common_environment(self) -> dict[str, str]:
        """Variables de entorno comunes a las cuatro Lambdas de API.

        Returns:
            El nombre de la tabla, el del bucket y el entorno de configuración.
            Cada Lambda añade después lo que necesite (cola, bus, rol de datos).
        """

        return {
            _ENV_CONFIG: self._config.env_name,
            _ENV_TABLE_NAME: self._data.table.table_name,
            _ENV_BUCKET_NAME: self._storage.bucket.bucket_name,
        }

    def _backend(
        self,
        construct_id: str,
        *,
        handler: str,
        reserved_concurrency: int,
        environment: dict[str, str],
    ) -> BackendFunction:
        """Construye una :class:`BackendFunction` con los defaults de la capa de API.

        Centraliza la memoria, el timeout (bajo el límite de integración de 29 s de
        la API_Gateway, criterio 18.1), el Layer y la CMK compartidos, de modo que
        cada ``_build_*`` solo decida su handler, su reserva de concurrencia y su
        entorno.

        Args:
            construct_id: Identificador lógico del construct en el stack.
            handler: Ruta de módulo del handler empaquetado.
            reserved_concurrency: Concurrencia reservada de la función.
            environment: Variables de entorno de la función (se cifran con la CMK).

        Returns:
            El :class:`BackendFunction` creado (función + rol de ejecución).
        """

        return BackendFunction(
            self,
            construct_id,
            handler=handler,
            layer=self._storage.shared_layer,
            key=self._storage.key,
            memory_mb=_MEMORY_MB,
            timeout_seconds=_TIMEOUT_S,
            reserved_concurrency=reserved_concurrency,
            environment=environment,
        )

    # --- api_submit ----------------------------------------------------------

    def _build_submit(self) -> lambda_.IFunction:
        """Construye ``api_submit`` (reserva 20) con sus permisos de mínimo privilegio.

        Permisos (diseño 12.2): escritura del payload en ``inputs/{tenant_id}/`` del
        bucket, siembra del Lote en DynamoDB (``PutItem``/``UpdateItem``/
        ``ConditionCheckItem``) sobre la tabla y consulta del GSI1 (límite de Lotes
        en vuelo), envío a la Cola_Solicitudes y publicación del Evento_Finalizacion
        en el Bus_Eventos (cierre en FAILED si la siembra falla, criterio 10.17).

        A diferencia de las otras tres, ``api_submit`` conserva en v1 un rol **sin**
        *session tags*: escribe en ``inputs/`` con el prefijo derivado del claim en
        código (diseño 12.2, nota final). El prefijo ``inputs/*`` del rol es un ARN
        acotado (no ``Resource: "*"``), no un comodín de datos.

        Returns:
            La función ``api_submit`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        environment = self._common_environment()
        environment[_ENV_REQUESTS_QUEUE_URL] = self._ingest.requests_queue.queue_url
        environment[_ENV_EVENT_BUS_NAME] = event_bus_name(self._config)

        backend = self._backend(
            "ApiSubmit",
            handler=_HANDLER_SUBMIT,
            reserved_concurrency=_RESERVED_SUBMIT,
            environment=environment,
        )

        # Escritura del payload acotada al prefijo inputs/ (nunca al bucket
        # completo). ``grant_put`` añade el KMS de la CMK para cifrar el objeto.
        self._storage.bucket.grant_put(backend.function, "inputs/*")
        # Siembra del Lote y cierre en FAILED: PutItem/UpdateItem (BatchWriteItem y
        # TransactWriteItems se expresan en IAM con las acciones de sus operaciones)
        # sobre la tabla, y Query sobre el GSI1 para el límite de Lotes en vuelo.
        self._data.table.grant(
            backend.function,
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:ConditionCheckItem",
            "dynamodb:GetItem",
        )
        backend.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="QueryInFlightLimitIndex",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:Query"],
                resources=[
                    f"{self._data.table.table_arn}/index/{self._data.gsi1_name}"
                ],
            )
        )
        # Encola exactamente un mensaje por Lote (diseño 4.2). ``grant_send_messages``
        # concede ``sqs:SendMessage`` sobre la cola concreta y el KMS de la CMK.
        self._ingest.requests_queue.grant_send_messages(backend.function)
        # Publica el Evento_Finalizacion si la siembra falla (criterio 10.17), sobre
        # el ARN del bus concreto derivado de la convención compartida (nunca "*").
        self._grant_event_bus(backend.role)

        return backend.function

    def _grant_event_bus(self, role: iam.IRole) -> None:
        """Concede ``events:PutEvents`` sobre el Bus_Eventos concreto (criterio 10.17).

        El ARN del bus se deriva de :func:`stacks.naming.event_bus_name`, la misma
        fuente de verdad que usa el ``notifications_stack`` para crearlo, de modo que
        la política apunte siempre al bus real (nunca ``Resource: "*"``).

        Args:
            role: Rol de ejecución dedicado de ``api_submit``.
        """

        event_bus_arn = self.format_arn(
            service="events",
            resource="event-bus",
            resource_name=event_bus_name(self._config),
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PublishBatchFailedEvent",
                effect=iam.Effect.ALLOW,
                actions=["events:PutEvents"],
                resources=[event_bus_arn],
            )
        )

    # --- api_status ----------------------------------------------------------

    def _build_status(self) -> lambda_.IFunction:
        """Construye ``api_status`` (reserva 30) con su rol de datos por tenant.

        Solo lectura: ``GetItem`` / ``Query`` sobre la tabla y el GSI1, acotados por
        ``dynamodb:LeadingKeys = "TENANT#${aws:PrincipalTag/tenant_id}*"`` en el rol
        de datos que el handler asume con la session tag ``tenant_id`` (diseño 12.2 /
        12.2.1). Sin escritura, sin acceso a S3.

        Returns:
            La función ``api_status`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        backend = self._backend(
            "ApiStatus",
            handler=_HANDLER_STATUS,
            reserved_concurrency=_RESERVED_STATUS,
            environment=self._common_environment(),
        )
        data_role = self._data_role(backend, "ApiStatus", tag_keys=(_TAG_TENANT,))
        self._add_data_role_arn_env(backend, data_role)

        # Lectura por tenant sobre la tabla y el GSI1 (listado y estado). El prefijo
        # LeadingKeys usa StringLike porque la pk sigue con "#ANALYSIS#..." tras el
        # tenant; el GSI1 particiona por "TENANT#{tenant_id}" exacto.
        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="ReadTenantAnalyses",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem", "dynamodb:Query"],
                resources=[
                    self._data.table.table_arn,
                    f"{self._data.table.table_arn}/index/{self._data.gsi1_name}",
                ],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": [f"{_DDB_TENANT_PREFIX}*"]
                    }
                },
            )
        )
        self._grant_table_cmk_decrypt(data_role)

        return backend.function

    # --- api_download --------------------------------------------------------

    def _build_download(self) -> lambda_.IFunction:
        """Construye ``api_download`` (reserva 10) con su rol de datos por tenant.

        Permisos (diseño 12.2): ``GetItem`` sobre la tabla acotado por
        ``dynamodb:LeadingKeys`` del tenant (para resolver el META y decidir 404/409
        antes de firmar), y ``s3:GetObject`` **solo** sobre
        ``outputs/${aws:PrincipalTag/tenant_id}/*`` —el alcance de toda Presigned_URL
        que firme este rol queda acotado a ese prefijo (diseño 8.5, criterios 2.6 /
        2.11)—. Sin escritura, sin ``PutObject``.

        Returns:
            La función ``api_download`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        backend = self._backend(
            "ApiDownload",
            handler=_HANDLER_DOWNLOAD,
            reserved_concurrency=_RESERVED_DOWNLOAD,
            environment=self._common_environment(),
        )
        data_role = self._data_role(backend, "ApiDownload", tag_keys=(_TAG_TENANT,))
        self._add_data_role_arn_env(backend, data_role)

        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="ReadTenantMeta",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:GetItem"],
                resources=[self._data.table.table_arn],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": [f"{_DDB_TENANT_PREFIX}#*"]
                    }
                },
            )
        )
        # Lectura del Documento_Consolidado acotada al prefijo del tenant: el alcance
        # de la Presigned_URL no puede exceder el del principal que la firma (8.5).
        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="GetTenantOutputObject",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[self._bucket_object_arn(_S3_OUTPUTS_TENANT)],
            )
        )
        self._grant_table_cmk_decrypt(data_role)

        return backend.function

    # --- api_purge -----------------------------------------------------------

    def _build_purge(self) -> lambda_.IFunction:
        """Construye ``api_purge`` (reserva 2) con su rol de datos por tenant y lote.

        Única Lambda con permisos de **borrado** (diseño 12.2). Asume un rol de datos
        con **dos** session tags (``tenant_id`` y ``analysis_id``):

        - DynamoDB: ``GetItem`` / ``Query`` / ``DeleteItem`` sobre la tabla, acotado
          por ``dynamodb:LeadingKeys`` al **lote exacto**
          ``TENANT#{tenant_id}#ANALYSIS#{analysis_id}`` (``StringEquals``), sin
          índices (criterio 19.12).
        - S3: ``ListBucketVersions`` sobre el bucket con ``s3:prefix`` acotado a los
          tres prefijos del lote, y ``DeleteObject`` / ``DeleteObjectVersion`` sobre
          ``inputs/{tenant_id}/*``, ``outputs/{tenant_id}/*`` y
          ``map-results/{analysis_id}/*`` (criterio 19.12). Sin ``Put*`` de ningún
          tipo.

        El prefijo ``map-results/`` se acota por ``analysis_id`` (no lleva
        ``tenant_id``): IAM limita a un solo lote, y la pertenencia al tenant la
        prueba el código al leer el META antes de tocar S3 (diseño 12.2).

        Returns:
            La función ``api_purge`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        backend = self._backend(
            "ApiPurge",
            handler=_HANDLER_PURGE,
            reserved_concurrency=_RESERVED_PURGE,
            environment=self._common_environment(),
        )
        data_role = self._data_role(
            backend, "ApiPurge", tag_keys=(_TAG_TENANT, _TAG_ANALYSIS)
        )
        self._add_data_role_arn_env(backend, data_role)

        # Borrado acotado al LOTE exacto (tenant + analysis), sin índices (19.12).
        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="PurgeAnalysisRecords",
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:DeleteItem",
                ],
                resources=[self._data.table.table_arn],
                conditions={
                    "ForAllValues:StringEquals": {
                        "dynamodb:LeadingKeys": [_DDB_TENANT_ANALYSIS_PREFIX]
                    }
                },
            )
        )
        # Enumeración de versiones acotada por prefijo a los objetos del lote (19.12).
        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="ListBatchObjectVersions",
                effect=iam.Effect.ALLOW,
                actions=["s3:ListBucketVersions", "s3:ListBucket"],
                resources=[self._storage.bucket.bucket_arn],
                conditions={
                    "StringLike": {
                        "s3:prefix": [
                            _S3_INPUTS_TENANT,
                            _S3_OUTPUTS_TENANT,
                            _S3_MAP_RESULTS_ANALYSIS,
                        ]
                    }
                },
            )
        )
        # Borrado de objetos y de todas sus versiones, acotado a los prefijos del
        # lote; nunca ``Put*`` (criterio 19.12 / diseño 12.2).
        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="DeleteBatchObjects",
                effect=iam.Effect.ALLOW,
                actions=["s3:DeleteObject", "s3:DeleteObjectVersion"],
                resources=[
                    self._bucket_object_arn(_S3_INPUTS_TENANT),
                    self._bucket_object_arn(_S3_OUTPUTS_TENANT),
                    self._bucket_object_arn(_S3_MAP_RESULTS_ANALYSIS),
                ],
            )
        )
        self._grant_table_cmk_decrypt(data_role)

        return backend.function

    # --- Helpers de rol de datos / permisos ---------------------------------

    def _data_role(
        self,
        backend: BackendFunction,
        name: str,
        *,
        tag_keys: tuple[str, ...],
    ) -> TenantDataRole:
        """Crea el :class:`TenantDataRole` de una Lambda de API (diseño 12.2.1).

        El rol de ejecución de la Lambda solo podrá asumir **este** rol de datos con
        las ``tag_keys`` indicadas; el rol de datos confía exclusivamente en ese rol
        de ejecución. Las políticas de datos las añade cada ``_build_*`` con
        :meth:`TenantDataRole.add_data_policy`.

        Args:
            backend: El ``BackendFunction`` cuya ejecución asume el rol de datos.
            name: Nombre lógico base (p. ej. ``"ApiStatus"``).
            tag_keys: Claves de session tag permitidas (``tenant_id``, y
                ``analysis_id`` en la purga).

        Returns:
            El :class:`TenantDataRole` creado.
        """

        return TenantDataRole(
            self,
            f"{name}DataRole",
            execution_role=backend.role,
            tag_keys=tag_keys,
            role_name=f"{self._prefix}-{name.lower()}-data",
        )

    def _add_data_role_arn_env(
        self, backend: BackendFunction, data_role: TenantDataRole
    ) -> None:
        """Inyecta el ARN del rol de datos como variable de entorno de la Lambda.

        El handler lo lee para ``AssumeRole`` con session tags en cada solicitud
        (diseño 12.2.1). No es un secreto; es un identificador de recurso.

        Args:
            backend: El ``BackendFunction`` de la Lambda.
            data_role: El rol de datos que la Lambda asume.
        """

        backend.function.add_environment(_ENV_DATA_ROLE_ARN, data_role.role.role_arn)

    def _grant_table_cmk_decrypt(self, data_role: TenantDataRole) -> None:
        """Concede al rol de datos el descifrado de la CMK para leer datos cifrados.

        Las políticas ``LeadingKeys`` se escriben a mano (no vía ``table.grant``, que
        no admite condiciones), por lo que el permiso de KMS sobre la CMK —necesario
        para leer/escribir los datos cifrados— se añade aquí explícitamente, acotado
        a la clave concreta (nunca un comodín de clave; criterio 15.6).

        Args:
            data_role: El rol de datos de la Lambda.
        """

        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="UseProjectCmk",
                effect=iam.Effect.ALLOW,
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                ],
                resources=[self._storage.key.key_arn],
            )
        )

    def _bucket_object_arn(self, key_pattern: str) -> str:
        """Construye el ARN de objeto del bucket para un patrón de clave.

        Args:
            key_pattern: Patrón de clave de objeto (p. ej.
                ``"outputs/${aws:PrincipalTag/tenant_id}/*"``).

        Returns:
            El ARN ``arn:aws:s3:::{bucket}/{key_pattern}``.
        """

        return f"{self._storage.bucket.bucket_arn}/{key_pattern}"

    # --- API REST + autorizador JWT nativo ----------------------------------

    def _build_rest_api(
        self,
    ) -> tuple[apigateway.RestApi, apigateway.CognitoUserPoolsAuthorizer]:
        """Crea la API REST, el autorizador JWT nativo y las cinco rutas con scope.

        El autorizador :class:`CognitoUserPoolsAuthorizer` delega en API Gateway la
        validación de **firma** (contra el JWKS del pool), ``iss``, ``aud`` /
        ``client_id`` y ``exp`` (criterios 1.1-1.5), sin código de autorizador
        propio. El **scope por ruta** (criterios 1.6/1.7/19.2) se declara en cada
        método con ``authorization_scopes``. CORS se restringe a la lista blanca del
        entorno (criterio 15.9). Un fallo de autorización produce 401/403 genéricos
        (criterio 1.17).

        Returns:
            La tupla ``(rest_api, authorizer)``.
        """

        rest_api = apigateway.RestApi(
            self,
            "RestApi",
            rest_api_name=f"{self._prefix}-api",
            description=(
                "API REST del pipeline de análisis de sentimiento: envío, listado, "
                "estado, descarga y purga de Lotes (autorizador JWT de Cognito)."
            ),
            deploy_options=apigateway.StageOptions(
                stage_name=self._config.env_name,
                # Trazas y métricas por método para la observabilidad (tarea 13.5).
                tracing_enabled=True,
                metrics_enabled=True,
                throttling_rate_limit=_USAGE_PLAN_RATE_RPS,
                throttling_burst_limit=_USAGE_PLAN_BURST,
            ),
            # CORS restringido a la lista blanca del entorno (criterio 15.9); nunca
            # ``*`` sobre datos sensibles (steering de seguridad / diseño 12.6).
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=list(self._config.cors_origins),
                allow_headers=list(_CORS_ALLOW_HEADERS),
                allow_methods=list(_CORS_ALLOW_METHODS),
                allow_credentials=False,
                max_age=_CORS_MAX_AGE,
            ),
            # El request validator valida presencia de cuerpo/parámetros en el borde
            # como primera línea; la validación semántica vive en la Lambda (12.5).
            cloud_watch_role=True,
        )

        authorizer = apigateway.CognitoUserPoolsAuthorizer(
            self,
            "JwtAuthorizer",
            cognito_user_pools=[self._auth.user_pool],
            authorizer_name=f"{self._prefix}-jwt",
            # El token viaja en el header estándar Authorization: Bearer <jwt>.
            identity_source=apigateway.IdentitySource.header("Authorization"),
        )

        self._wire_routes(rest_api, authorizer)
        return rest_api, authorizer

    def _wire_routes(
        self,
        rest_api: apigateway.RestApi,
        authorizer: apigateway.CognitoUserPoolsAuthorizer,
    ) -> None:
        """Cablea las cinco rutas de ``/v1/analyses`` con su integración y scope.

        Cada método usa ``AuthorizationType.COGNITO`` con el autorizador JWT y
        declara el scope exacto que exige su ruta (criterios 1.6/1.7/19.2), tomado de
        los *scopes* que el ``auth_stack`` ya expone, para no redeclararlos.

        Args:
            rest_api: La API REST sobre la que se montan las rutas.
            authorizer: El autorizador JWT nativo compartido por todas las rutas.
        """

        # /v1
        v1 = rest_api.root.add_resource("v1")
        analyses = v1.add_resource("analyses")
        by_id = analyses.add_resource("{analysis_id}")
        download = by_id.add_resource("download")

        write_scope = self._scope_name(self._auth.scope_write)
        read_scope = self._scope_name(self._auth.scope_read)
        purge_scope = self._scope_name(self._auth.scope_purge)

        # POST /v1/analyses → api_submit (analyses.write).
        self._add_method(
            analyses, "POST", self.api_submit, authorizer, write_scope
        )
        # GET /v1/analyses → api_status (analyses.read).
        self._add_method(
            analyses, "GET", self.api_status, authorizer, read_scope
        )
        # GET /v1/analyses/{analysis_id} → api_status (analyses.read).
        self._add_method(by_id, "GET", self.api_status, authorizer, read_scope)
        # DELETE /v1/analyses/{analysis_id} → api_purge (analyses.purge).
        self._add_method(by_id, "DELETE", self.api_purge, authorizer, purge_scope)
        # GET /v1/analyses/{analysis_id}/download → api_download (analyses.read).
        self._add_method(
            download, "GET", self.api_download, authorizer, read_scope
        )

    @staticmethod
    def _scope_name(scope: cognito.OAuthScope) -> str:
        """Devuelve el nombre del scope OAuth para ``authorization_scopes``.

        Args:
            scope: El :class:`aws_cdk.aws_cognito.OAuthScope` expuesto por el
                ``auth_stack`` (p. ej. ``sentiment/analyses.write``).

        Returns:
            El nombre del scope tal como viaja en el claim ``scope`` del token.
        """

        return scope.scope_name

    def _add_method(
        self,
        resource: apigateway.IResource,
        http_method: str,
        function: lambda_.IFunction,
        authorizer: apigateway.CognitoUserPoolsAuthorizer,
        scope: str,
    ) -> None:
        """Añade un método a una ruta con integración *proxy* y scope requerido.

        Args:
            resource: El recurso (ruta) de la API al que se añade el método.
            http_method: Verbo HTTP (``GET`` / ``POST`` / ``DELETE``).
            function: La Lambda integrada en integración *proxy*.
            authorizer: El autorizador JWT nativo de Cognito.
            scope: Nombre del scope OAuth que la ruta exige (criterios 1.6/1.7/19.2).
        """

        resource.add_method(
            http_method,
            apigateway.LambdaIntegration(function, proxy=True),
            authorization_type=apigateway.AuthorizationType.COGNITO,
            authorizer=authorizer,
            authorization_scopes=[scope],
            # La API_Key identifica al consumidor ante el Usage_Plan (cuota), no
            # autentica (criterio 1.12): la autenticación la hace el JWT authorizer.
            api_key_required=True,
        )

    # --- Usage_Plan (throttling + cuota por Tenant) -------------------------

    def _build_usage_plan(self) -> apigateway.UsagePlan:
        """Crea el Usage_Plan con throttling de 10 rps y cuota de 1.000/día (15.13).

        El Usage_Plan limita el caudal por consumidor (API_Key) para controlar el
        abuso y el coste de Bedrock. Las API_Keys **no** autentican (criterio 1.12):
        solo asocian al solicitante con esta cuota. Se crea una API_Key de ejemplo
        asociada al plan y al *stage* desplegado.

        Returns:
            El :class:`aws_cdk.aws_apigateway.UsagePlan` creado.
        """

        plan = self.rest_api.add_usage_plan(
            "TenantUsagePlan",
            name=f"{self._prefix}-tenant-plan",
            description=(
                "Cuota y throttling por Tenant (10 rps burst, 1.000 Lotes/día; "
                "criterio 15.13). La API_Key mide y limita, no autentica (1.12)."
            ),
            throttle=apigateway.ThrottleSettings(
                rate_limit=_USAGE_PLAN_RATE_RPS,
                burst_limit=_USAGE_PLAN_BURST,
            ),
            quota=apigateway.QuotaSettings(
                limit=_USAGE_PLAN_QUOTA_PER_DAY,
                period=apigateway.Period.DAY,
            ),
        )

        # El plan debe cubrir el stage desplegado para que la cuota/throttling
        # apliquen a las llamadas reales.
        plan.add_api_stage(stage=self.rest_api.deployment_stage)

        # API_Key de ejemplo (una por consumidor en producción). Identifica al
        # cliente ante el plan; no transporta identidad verificable (criterio 1.12).
        api_key = self.rest_api.add_api_key(
            "TenantApiKey", api_key_name=f"{self._prefix}-tenant-key"
        )
        plan.add_api_key(api_key)

        return plan

    # --- WAF (rate limiting + reglas gestionadas) ---------------------------

    def _build_waf(
        self,
    ) -> tuple[wafv2.CfnWebACL, wafv2.CfnWebACLAssociation]:
        """Crea la Web ACL de WAF y la asocia al *stage* de la API (diseño 12.7).

        Reglas, en orden: los tres grupos gestionados de AWS
        (:data:`_WAF_MANAGED_RULE_GROUPS`) y una regla de **rate limiting** por IP de
        2.000 peticiones / 5 min (criterio 15.12). La acción por defecto es
        ``Allow`` (el WAF solo bloquea lo que una regla marca); el **modo** de las
        reglas lo fija ``config.waf_mode``: en ``COUNT`` solo se cuentan los aciertos
        (observación), en ``BLOCK`` se bloquean (diseño 12.7).

        Returns:
            La tupla ``(web_acl, association)``.
        """

        web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            name=f"{self._prefix}-web-acl",
            scope="REGIONAL",  # API Gateway REST es un recurso regional
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow=wafv2.CfnWebACL.AllowActionProperty()
            ),
            visibility_config=self._waf_visibility(f"{self._prefix}-web-acl"),
            rules=self._waf_rules(),
        )

        association = wafv2.CfnWebACLAssociation(
            self,
            "WebAclAssociation",
            web_acl_arn=web_acl.attr_arn,
            # ARN del stage desplegado de la API REST (recurso que el WAF protege).
            resource_arn=self._stage_arn(),
        )
        # El stage debe existir antes de asociarle el WAF.
        association.node.add_dependency(self.rest_api.deployment_stage)

        return web_acl, association

    def _waf_rules(self) -> list[wafv2.CfnWebACL.RuleProperty]:
        """Construye las reglas del WAF en el orden del diseño 12.7.

        Returns:
            Los tres grupos de reglas gestionadas seguidos de la regla de rate
            limiting por IP. El ``override_action`` / ``action`` de cada regla
            respeta ``config.waf_mode`` (COUNT u BLOCK).
        """

        rules: list[wafv2.CfnWebACL.RuleProperty] = []

        for priority, group_name in enumerate(_WAF_MANAGED_RULE_GROUPS):
            rules.append(
                wafv2.CfnWebACL.RuleProperty(
                    name=group_name,
                    priority=priority,
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=(
                            wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                vendor_name="AWS",
                                name=group_name,
                            )
                        )
                    ),
                    # override_action aplica a grupos de reglas gestionadas: en COUNT
                    # se cuentan sus aciertos sin bloquear; en BLOCK se respeta la
                    # acción interna del grupo (``none``).
                    override_action=self._waf_group_override(),
                    visibility_config=self._waf_visibility(group_name),
                )
            )

        # Regla propia de rate limiting por IP (criterio 15.12), tras los grupos.
        rules.append(
            wafv2.CfnWebACL.RuleProperty(
                name="RateLimitPerIp",
                priority=len(_WAF_MANAGED_RULE_GROUPS),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=(
                        wafv2.CfnWebACL.RateBasedStatementProperty(
                            # Ventana de evaluación fija de 5 min del tipo de regla.
                            limit=_WAF_RATE_LIMIT,
                            aggregate_key_type="IP",
                        )
                    )
                ),
                # action aplica a reglas propias: COUNT cuenta, BLOCK bloquea.
                action=self._waf_rule_action(),
                visibility_config=self._waf_visibility("RateLimitPerIp"),
            )
        )

        return rules

    def _waf_group_override(self) -> wafv2.CfnWebACL.OverrideActionProperty:
        """Devuelve el ``override_action`` de un grupo gestionado según el modo WAF.

        Returns:
            ``count`` cuando ``config.waf_mode == "COUNT"`` (observación), o ``none``
            (respetar la acción interna del grupo) cuando es ``BLOCK``.
        """

        if self._config.waf_mode == "COUNT":
            return wafv2.CfnWebACL.OverrideActionProperty(
                count=wafv2.CfnWebACL.CountActionProperty()
            )
        # ``none`` deja actuar la acción interna del grupo gestionado; en el
        # esquema de WAFv2 es un objeto vacío (no tiene un *property* dedicado como
        # ``CountActionProperty``), por lo que se expresa como ``{}``.
        return wafv2.CfnWebACL.OverrideActionProperty(none={})

    def _waf_rule_action(self) -> wafv2.CfnWebACL.RuleActionProperty:
        """Devuelve la acción de la regla de rate limiting según el modo WAF.

        Returns:
            ``count`` para observar (modo ``COUNT``) o ``block`` para bloquear las
            IPs que exceden el límite (modo ``BLOCK``; criterio 15.12).
        """

        if self._config.waf_mode == "COUNT":
            return wafv2.CfnWebACL.RuleActionProperty(
                count=wafv2.CfnWebACL.CountActionProperty()
            )
        return wafv2.CfnWebACL.RuleActionProperty(
            block=wafv2.CfnWebACL.BlockActionProperty()
        )

    @staticmethod
    def _waf_visibility(
        metric_name: str,
    ) -> wafv2.CfnWebACL.VisibilityConfigProperty:
        """Configuración de métricas/muestreo de CloudWatch para una regla o la ACL.

        Args:
            metric_name: Nombre de la métrica de CloudWatch (sin PII).

        Returns:
            La :class:`VisibilityConfigProperty` con métricas y muestreo activos.
        """

        return wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=metric_name,
            sampled_requests_enabled=True,
        )

    def _stage_arn(self) -> str:
        """Construye el ARN del *stage* de la API REST para la asociación con el WAF.

        Returns:
            El ARN ``arn:aws:apigateway:{region}::/restapis/{id}/stages/{stage}``.
        """

        return (
            f"arn:aws:apigateway:{self.region}::/restapis/"
            f"{self.rest_api.rest_api_id}/stages/"
            f"{self.rest_api.deployment_stage.stage_name}"
        )
