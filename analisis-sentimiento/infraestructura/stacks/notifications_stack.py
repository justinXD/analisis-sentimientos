"""Stack de dominio ``notifications`` (notificación). Tarea 13.3.

Dominio: notificación de fin de proceso. Es el *backbone* de integración push
sobre el que se apoyan los canales opcionales (webhook, correo), sin que ninguno
sea la fuente de verdad —esa vive en la Dynamo_Table (criterio 13.9)—. El
``aggregate_results`` del ``compute_stack`` publica un Evento_Finalizacion
(``detail-type = analysis.completed``, criterio 13.1) en el Bus_Eventos; las
reglas de este stack lo enrutan a los consumidores de entrega.

Recursos (tarea 13.3)
---------------------
- **Bus_Eventos** (:attr:`event_bus`): bus EventBridge personalizado con el nombre
  EXACTO ``{project}-{env}-events`` (criterio 13.1). Ese literal es un contrato
  compartido: el ``compute_stack`` ya concede ``events:PutEvents`` y publica la
  variable ``EVENT_BUS_NAME`` de ``aggregate_results`` derivándolo de la misma
  convención (:func:`stacks.naming.event_bus_name`, sufijo ``-events``).
  Si el nombre no coincide, la política IAM y la variable de entorno del publicador
  apuntarían a un bus inexistente; por eso aquí se **reutiliza** esa única fuente
  de verdad en lugar de duplicar el literal.
- **Regla ``analysis.completed``** (:attr:`webhook_rule`): regla sobre el
  Bus_Eventos que hace *match* por ``detail-type = "analysis.completed"`` (criterio
  13.1) y enruta el evento a la Lambda ``notify_webhook``.
- **Lambda ``notify_webhook``** (:attr:`notify_webhook`): entrega el
  Evento_Finalizacion por HTTPS POST a la URL de webhook del Tenant, firmado con
  HMAC-SHA256 (criterios 13.5–13.7). Construida con
  :class:`~constructs.backend_function.BackendFunction` (mismo molde que las
  Lambdas del ``compute_stack``: ``arm64``, rol propio, *log group* cifrado,
  X-Ray). Se configura con un destino asíncrono ``OnFailure`` hacia la
  DLQ_Notificaciones: cuando el handler agota los 3 intentos y propaga
  ``WebhookDeliveryError``, el *redrive* nativo traslada el evento original
  (con ``analysis_id`` y ``correlation_id``) a la DLQ (criterios 9.9, 9.10).
- **Secreto HMAC** (:attr:`webhook_secret`): secreto en Secrets_Manager cuyo valor
  se **genera** (nunca se *hardcodea*; criterio 15.5 / steering de seguridad) y se
  cifra en reposo con la CMK del proyecto. ``notify_webhook`` recibe **solo**
  lectura (``GetSecretValue``) sobre el ARN concreto de este secreto, nunca sobre
  un comodín (mínimo privilegio, criterios 15.10 / 15.11). El valor del secreto no
  se expone en *outputs* ni en logs.
- **DLQ_Notificaciones** (:attr:`notifications_dlq`): cola SQS cifrada con la CMK,
  retención de 14 días (criterio 9.10), destino del *redrive* asíncrono de
  ``notify_webhook`` para los webhooks no entregados (criterio 9.9).

Seguridad y privacidad
----------------------
- El secreto HMAC se genera en Secrets_Manager y se cifra con la CMK; su valor
  nunca entra en el código, en los *outputs* ni en los logs.
- ``notify_webhook`` recibe mínimo privilegio: lectura del secreto concreto,
  publicación en la DLQ concreta y lectura de la Dynamo_Table (resolución del
  perfil del Tenant: ``pk = TENANT#{tenant_id}``, ``sk = PROFILE``; diseño 8.6).
  Ningún permiso se concede sobre ``Resource: "*"`` de datos.
- El cifrado en reposo de la DLQ y del secreto lo cubre la CMK del
  ``StorageStack``; las concesiones ``grant_*`` añaden el permiso de KMS necesario
  sin un comodín de clave.

SES: identidad verificada + configuration set (tarea 13.4)
----------------------------------------------------------
- **Identidad SES verificada** (:attr:`ses_identity`): la dirección remitente
  (``From``) que SES debe verificar para poder enviar las notificaciones de fin de
  proceso (criterio 13.4). La dirección es un valor **no secreto por entorno**
  (``config.ses_sender_email``), un marcador que el operador sustituye por el
  buzón real verificado fuera de banda; nunca se *hardcodea* una dirección real en
  el código (steering de seguridad). La identidad se asocia al configuration set
  de este stack, de modo que todo correo enviado desde ella herede el seguimiento
  de rebotes/quejas por defecto.
- **Configuration set de SES** (:attr:`ses_configuration_set`): configuration set
  **dedicado** que captura los eventos de rebote (bounce) y queja (complaint) y los
  publica en un destino de eventos (criterio 13.10). Si estos eventos no se
  gestionan, SES puede suspender la cuenta (diseño sección 9.2). El backend
  (``notify_email``, tarea 9.5) pasa el nombre de este configuration set en cada
  ``SendEmail`` vía la variable ``SES_CONFIGURATION_SET``.
- **Topic SNS de rebotes/quejas** (:attr:`ses_bounce_topic`): destino de eventos
  del configuration set. Un topic SNS da registros durables y consumibles de cada
  rebote/queja (un suscriptor de operaciones puede reaccionar o alarmar), a
  diferencia de un destino de solo métricas. Se cifra en reposo con la CMK del
  proyecto (criterio 15.6) y exige TLS en su política de acceso.

Esta tarea añade **solo** SES (identidad + configuration set + topic de eventos).
El cableado de la Lambda ``notify_email`` sobre el Bus_Eventos no forma parte de la
composición de este stack según el diseño (sección 14.4.2 lista para
``notifications`` únicamente ``notify_webhook``); queda fuera del alcance de 13.4.

Dependencias (diseño 14.4.2): ``storage`` (CMK para cifrar la DLQ y el secreto, y
Layer + CMK para la Lambda) y ``data`` (Dynamo_Table que ``notify_webhook`` lee
para resolver el destino del Tenant).
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ses as ses
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct
from constructs.backend_function import BackendFunction

from config.environments import EnvConfig
from stacks.data_stack import DataStack
from stacks.naming import event_bus_name
from stacks.storage_stack import StorageStack

# --- Ruta del handler (diseño 14.4.4) ---
# Cadena de módulo que apunta al handler empaquetado en ``backend/src``. Debe
# coincidir con el módulo real ``sentiment/handlers/notify_webhook.py`` (tarea 9.4).
_HANDLER_NOTIFY_WEBHOOK = "sentiment.handlers.notify_webhook.handler"

# --- detail-type que enruta la regla del Bus_Eventos (criterio 13.1) ---
# El publicador (``eventbridge_publisher.py``) emite el Evento_Finalizacion con
# este ``detail-type`` exacto; el handler lo revalida como defensa en profundidad.
_DETAIL_TYPE_ANALYSIS_COMPLETED = "analysis.completed"

# --- Parámetros de la Lambda notify_webhook ---
# La entrega es ligera (POST HTTPS con reintentos en proceso); 256 MiB basta. El
# timeout cubre holgadamente los 3 intentos con backoff del handler (base 1 s,
# factor 2,0, timeout de 5 s por intento): peor caso ~ 3×5 s de petición + ~3 s de
# backoff. 60 s deja margen sin arriesgar colgar un contenedor indefinidamente.
_MEMORY_NOTIFY_WEBHOOK_MB = 256
_TIMEOUT_NOTIFY_WEBHOOK_S = 60

# --- Retención de la DLQ_Notificaciones (criterio 9.10) ---
# 14 días: el máximo de SQS, coherente con las demás DLQs del sistema (9.10).
_DLQ_RETENTION_DAYS = 14

# --- Variables de entorno de notify_webhook ---
# ``CONFIG_ENV`` habilita el fallback a Parameter_Store del backend; no es secreto.
# ``TABLE_NAME`` lo usa el resolutor del destino (perfil del Tenant en la
# Dynamo_Table). ``WEBHOOK_HMAC_SECRET_ARN`` es el identificador (ARN) del secreto
# HMAC —no su valor—, que el proveedor de secretos lee de Secrets_Manager.
# ``NOTIFICATIONS_DLQ_URL`` queda disponible para trazabilidad/depuración; el
# traslado real a la DLQ lo hace el destino asíncrono ``OnFailure``, no el handler.
_ENV_CONFIG = "CONFIG_ENV"
_ENV_TABLE_NAME = "TABLE_NAME"
_ENV_WEBHOOK_SECRET_ARN = "WEBHOOK_HMAC_SECRET_ARN"
_ENV_NOTIFICATIONS_DLQ_URL = "NOTIFICATIONS_DLQ_URL"

# --- Nombres de recurso (prefijados por {project}-{env}) ---
_DLQ_SUFFIX = "notifications-dlq"
_SECRET_SUFFIX = "webhook-hmac"
# Sufijo del nombre del configuration set de SES (criterio 13.10). El backend
# (``notify_email``) lo recibe por la variable ``SES_CONFIGURATION_SET`` cuando se
# cablee la Lambda de correo (fuera del alcance de esta tarea); se deriva de la
# misma convención de nombre para que ambos lados coincidan byte a byte.
_SES_CONFIG_SET_SUFFIX = "email-events"
# Sufijo del topic SNS que recibe los eventos de rebote/queja de SES (criterio
# 13.10). Es el destino de eventos del configuration set.
_SES_BOUNCE_TOPIC_SUFFIX = "ses-bounces-complaints"

# Longitud del material aleatorio del secreto HMAC generado por Secrets_Manager.
# 64 bytes hex (``exclude_punctuation`` + ``password_length``) da >256 bits de
# entropía, de sobra para una clave HMAC-SHA256.
_HMAC_SECRET_LENGTH = 64

# --- Eventos de SES capturados por el configuration set (criterio 13.10) ---
# El criterio 13.10 exige registrar EXACTAMENTE los eventos de rebote (bounce) y
# queja (complaint): si no se gestionan, SES puede suspender la cuenta (diseño
# sección 9.2). No se capturan los demás tipos de evento (envío, entrega, apertura…)
# para no generar ruido ni volumen de datos innecesario.
_SES_TRACKED_EVENTS = (
    ses.EmailSendingEvent.BOUNCE,
    ses.EmailSendingEvent.COMPLAINT,
)


class NotificationsStack(Stack):
    """Stack del dominio de notificación: Bus_Eventos, regla, webhook, secreto y DLQ.

    Expone sus recursos como atributos públicos tipados con **interfaces** de CDK
    (diseño 14.4.3), de modo que los stacks dependientes (``compute`` ya cablea el
    Bus_Eventos por convención de nombre; ``observability`` alarmará sobre la DLQ)
    los referencien sin acoplarse a la implementación concreta.

    Es un stack **sin estado**: el Bus_Eventos, la regla, la Lambda y la DLQ son
    recursos recreables (la verdad del estado vive en la Dynamo_Table, criterio
    13.9). El **secreto HMAC** sí es material sensible de larga vida: se retiene
    ante un ``cdk destroy`` accidental (``RemovalPolicy.RETAIN``) para no invalidar
    las firmas acordadas con los receptores de webhook.

    Attributes:
        event_bus: El Bus_Eventos personalizado ``{project}-{env}-events``
            (:class:`aws_cdk.aws_events.IEventBus`).
        notify_webhook: La Lambda de entrega de webhook
            (:class:`aws_cdk.aws_lambda.IFunction`).
        webhook_rule: La regla ``analysis.completed`` → ``notify_webhook``
            (:class:`aws_cdk.aws_events.Rule`).
        webhook_secret: El secreto HMAC en Secrets_Manager
            (:class:`aws_cdk.aws_secretsmanager.ISecret`).
        notifications_dlq: La DLQ_Notificaciones
            (:class:`aws_cdk.aws_sqs.IQueue`).
        ses_bounce_topic: El topic SNS que recibe los eventos de rebote y queja
            de SES, cifrado con la CMK (:class:`aws_cdk.aws_sns.ITopic`).
        ses_configuration_set: El configuration set de SES dedicado al
            seguimiento de rebotes/quejas (criterio 13.10)
            (:class:`aws_cdk.aws_ses.IConfigurationSet`).
        ses_identity: La identidad SES verificada (remitente de las
            notificaciones de correo, criterio 13.4)
            (:class:`aws_cdk.aws_ses.EmailIdentity`).

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
        storage: Stack de almacenamiento del que depende (CMK + Layer).
        data: Stack de datos del que depende (Dynamo_Table).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        storage: StorageStack,
        data: DataStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._config = config
        self._storage = storage
        self._data = data
        self.add_dependency(storage)
        self.add_dependency(data)

        # Orden de construcción: el bus y los recursos de entrega (DLQ, secreto)
        # deben existir antes de la Lambda que los referencia, y la Lambda antes de
        # la regla que la enruta.
        self.event_bus: events.IEventBus = self._build_event_bus()
        self.notifications_dlq: sqs.IQueue = self._build_notifications_dlq()
        self.webhook_secret: secretsmanager.ISecret = self._build_webhook_secret()
        self.notify_webhook: lambda_.IFunction = self._build_notify_webhook()
        self.webhook_rule: events.Rule = self._build_webhook_rule()

        # --- SES (tarea 13.4): identidad verificada + configuration set para
        # rebotes/quejas (criterios 13.4, 13.10). Orden de construcción: el topic
        # de eventos y el configuration set que lo referencia deben existir antes
        # de la identidad que se cablea a ese configuration set.
        self.ses_bounce_topic: sns.ITopic = self._build_ses_bounce_topic()
        self.ses_configuration_set: ses.IConfigurationSet = (
            self._build_ses_configuration_set()
        )
        self.ses_identity: ses.EmailIdentity = self._build_ses_identity()

    # --- Bus_Eventos ---------------------------------------------------------

    def _build_event_bus(self) -> events.EventBus:
        """Crea el Bus_Eventos personalizado con el nombre EXACTO del contrato.

        El nombre ``{project}-{env}-events`` se **reutiliza** de la única fuente de
        verdad (:func:`stacks.naming.event_bus_name`) para garantizar que
        coincide, byte a byte, con el que ``aggregate_results`` usa en su política
        ``events:PutEvents`` y en su variable de entorno ``EVENT_BUS_NAME``
        (criterio 13.1). Importar la convención en lugar de redeclarar el literal
        hace imposible que ambos lados se desincronicen.

        Returns:
            El :class:`aws_cdk.aws_events.EventBus` creado.
        """

        return events.EventBus(
            self,
            "EventBus",
            # Contrato con compute_stack: el publicador deriva el ARN y el nombre de
            # ESTA misma convención (sufijo ``-events``). No duplicar el literal.
            event_bus_name=event_bus_name(self._config),
        )

    # --- DLQ_Notificaciones --------------------------------------------------

    def _build_notifications_dlq(self) -> sqs.Queue:
        """Crea la DLQ_Notificaciones cifrada con la CMK y con retención de 14 días.

        Recibe, vía el destino asíncrono ``OnFailure`` de ``notify_webhook``, los
        eventos de webhooks no entregados tras los 3 intentos (criterio 9.9). El
        evento conserva ``analysis_id`` y ``correlation_id`` porque el *redrive*
        asíncrono traslada la carga original intacta. Se cifra en reposo con la CMK
        del proyecto (criterio 15.6) y deniega todo acceso sin TLS (defensa en
        profundidad, coherente con las demás colas del sistema). Retención de 14
        días (criterio 9.10).

        Returns:
            La :class:`aws_cdk.aws_sqs.Queue` creada.
        """

        prefix = f"{self._config.project}-{self._config.env_name}"
        return sqs.Queue(
            self,
            "NotificationsDlq",
            queue_name=f"{prefix}-{_DLQ_SUFFIX}",
            # SSE-KMS con la CMK del proyecto (criterio 15.6). CUSTOMER_MANAGED
            # exige una clave explícita, a diferencia del cifrado gestionado por SQS.
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self._storage.key,
            # Retención máxima de SQS; coherente con las demás DLQs (criterio 9.10).
            retention_period=Duration.days(_DLQ_RETENTION_DAYS),
            # Deniega cualquier operación con `aws:SecureTransport=false`.
            enforce_ssl=True,
            # Buffer transitorio: la verdad del estado vive en la Dynamo_Table.
            removal_policy=RemovalPolicy.DESTROY,
        )

    # --- Secreto HMAC --------------------------------------------------------

    def _build_webhook_secret(self) -> secretsmanager.Secret:
        """Crea el secreto HMAC en Secrets_Manager con valor **generado** y CMK.

        El valor del secreto NUNCA se *hardcodea* (criterio 15.5 / steering de
        seguridad): Secrets_Manager lo **genera** con
        :class:`~aws_cdk.aws_secretsmanager.SecretStringGenerator` (material
        aleatorio de alta entropía) y lo cifra en reposo con la CMK del proyecto.
        Es material sensible de larga vida —las firmas HMAC acordadas con los
        receptores dependen de él—, por lo que se **retiene** ante un ``destroy``
        accidental (``RemovalPolicy.RETAIN``). El valor no se expone en *outputs*
        ni en logs; solo su ARN se publica a la Lambda como variable de entorno.

        En v1 es un único secreto a nivel de proyecto; cuando el modelo evolucione
        a un secreto por Tenant, el resolutor de ``notify_webhook`` devolverá el
        ``secret_id`` del Tenant y este stack concederá la lectura sobre ese
        conjunto acotado. La forma del permiso (ARN concreto, nunca comodín) no
        cambia.

        Returns:
            El :class:`aws_cdk.aws_secretsmanager.Secret` creado.
        """

        prefix = f"{self._config.project}-{self._config.env_name}"
        return secretsmanager.Secret(
            self,
            "WebhookHmacSecret",
            secret_name=f"{prefix}-{_SECRET_SUFFIX}",
            description=(
                "Secreto HMAC-SHA256 compartido para firmar los webhooks de fin de "
                "proceso (X-Signature; criterio 13.6). Valor generado por "
                "Secrets_Manager, nunca hardcodeado."
            ),
            # Cifrado en reposo con la CMK del proyecto (criterio 15.6).
            encryption_key=self._storage.key,
            # El VALOR se genera; no se incluye ningún material en el código.
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=_HMAC_SECRET_LENGTH,
                # Clave HMAC: material aleatorio opaco. Se excluye la puntuación
                # para evitar problemas de codificación al transportarlo como
                # SecretString; la entropía sigue siendo >256 bits.
                exclude_punctuation=True,
            ),
            # Material sensible de larga vida: retener ante un destroy accidental
            # para no invalidar las firmas acordadas con los receptores.
            removal_policy=RemovalPolicy.RETAIN,
        )

    # --- Lambda notify_webhook ----------------------------------------------

    def _build_notify_webhook(self) -> lambda_.IFunction:
        """Construye ``notify_webhook`` con mínimo privilegio y redrive a la DLQ.

        Molde :class:`~constructs.backend_function.BackendFunction` (``arm64``, rol
        propio, *log group* cifrado 30 días, X-Ray), con el destino asíncrono
        ``OnFailure`` apuntando a la DLQ_Notificaciones: una invocación asíncrona
        que agota los reintentos (el handler propaga ``WebhookDeliveryError``) se
        *redrije* a la DLQ con el evento original intacto (criterios 9.9, 9.10).

        Permisos de mínimo privilegio (criterios 15.10 / 15.11):

        - ``secretsmanager:GetSecretValue`` **solo** sobre el ARN del secreto HMAC
          concreto (:meth:`_grant_secret_read`), nunca un comodín.
        - Lectura de la Dynamo_Table (``GetItem`` / ``Query``) para resolver el
          perfil del Tenant (URL de webhook + ``secret_id``; diseño 8.6).
        - El ``OnFailure`` concede implícitamente ``sqs:SendMessage`` sobre la DLQ
          concreta (y el KMS asociado), gestionado por el propio destino.

        Returns:
            La función ``notify_webhook`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        environment = {
            _ENV_CONFIG: self._config.env_name,
            _ENV_TABLE_NAME: self._data.table.table_name,
            # Identificador (ARN) del secreto, NO su valor: el proveedor de secretos
            # del handler lo lee de Secrets_Manager en runtime.
            _ENV_WEBHOOK_SECRET_ARN: self.webhook_secret.secret_arn,
            # Disponible para trazabilidad; el traslado real lo hace el OnFailure.
            _ENV_NOTIFICATIONS_DLQ_URL: self.notifications_dlq.queue_url,
        }

        backend = BackendFunction(
            self,
            "NotifyWebhook",
            handler=_HANDLER_NOTIFY_WEBHOOK,
            layer=self._storage.shared_layer,
            key=self._storage.key,
            memory_mb=_MEMORY_NOTIFY_WEBHOOK_MB,
            timeout_seconds=_TIMEOUT_NOTIFY_WEBHOOK_S,
            # Sin concurrencia reservada: la entrega de webhooks es esporádica y no
            # presiona ninguna dependencia con cuota dura (a diferencia de Bedrock).
            reserved_concurrency=None,
            environment=environment,
        )

        # Redrive nativo del origen asíncrono: los webhooks no entregados tras los
        # reintentos van a la DLQ_Notificaciones con el evento original (criterios
        # 9.9, 9.10). ``BackendFunction`` no expone destinos en su firma (es un
        # molde transversal), así que el destino se configura aquí sobre la función
        # ya creada. ``SqsDestination`` concede ``sqs:SendMessage`` sobre la cola
        # concreta por sí mismo (y el KMS de la CMK), sin comodines.
        backend.function.configure_async_invoke(
            on_failure=destinations.SqsDestination(self.notifications_dlq),
        )

        self._grant_secret_read(backend.role)
        # El resolutor del destino lee el perfil del Tenant (pk = TENANT#{tenant_id},
        # sk = PROFILE; diseño 8.6): GetItem puntual y, como defensa, Query por pk.
        # Sobre la tabla concreta; la concesión añade el KMS de la CMK.
        self._data.table.grant(
            backend.function,
            "dynamodb:GetItem",
            "dynamodb:Query",
        )

        return backend.function

    def _grant_secret_read(self, role: iam.IRole) -> None:
        """Concede lectura del secreto HMAC acotada a su ARN concreto (15.10/15.11).

        ``notify_webhook`` solo necesita **leer** el secreto para firmar
        (criterio 13.6): se conceden ``secretsmanager:GetSecretValue`` y
        ``DescribeSecret`` sobre el ARN del secreto concreto —nunca un comodín de
        secretos ni acciones de mutación (``PutSecretValue``, ``RotateSecret``…)—.

        El permiso se añade a la **política de principal** del rol (no al recurso),
        y el ``kms:Decrypt`` sobre la CMK se concede con ``key.grant_decrypt``, que
        —como la *key policy* del ``StorageStack`` delega en IAM de la cuenta—
        también aterriza en la política de principal. Hacerlo así (en lugar del
        ``secret.grant_read`` por defecto) evita que el permiso se escriba en la
        *key policy* de la CMK referenciando este rol, lo que crearía una
        dependencia ``storage -> notifications`` y, con ella, un ciclo entre stacks
        (``notifications`` ya depende de ``storage``). El valor del secreto nunca
        se registra ni se expone (steering de seguridad).

        Args:
            role: Rol de ejecución dedicado de ``notify_webhook``.
        """

        # Solo lectura sobre el ARN concreto del secreto; nunca comodín ni mutación.
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ReadWebhookHmacSecret",
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[self.webhook_secret.secret_arn],
            )
        )
        # Descifra el secreto con la CMK. La concesión aterriza en el principal
        # (la key policy delega en IAM), evitando el ciclo entre stacks.
        self._storage.key.grant_decrypt(role)

    # --- Regla analysis.completed -------------------------------------------

    def _build_webhook_rule(self) -> events.Rule:
        """Crea la regla ``analysis.completed`` → ``notify_webhook`` en el bus.

        Hace *match* por ``detail-type = "analysis.completed"`` sobre el
        Bus_Eventos (criterio 13.1) y enruta cada Evento_Finalizacion a la Lambda
        ``notify_webhook`` como objetivo. Es una regla opcional de entrega: el
        Evento_Finalizacion se publica siempre (lo hace ``aggregate_results``),
        pero su enrutado al webhook es un canal adicional sobre el endpoint de
        estado consultable (diseño sección 9).

        El objetivo usa el destino asíncrono ``OnFailure`` ya configurado en la
        Lambda para el *redrive* a la DLQ_Notificaciones (criterios 9.9, 9.10): no
        se añade una DLQ en la propia regla, pues el fallo que interesa capturar
        —webhook no entregado— ocurre dentro del handler, no en la entrega de
        EventBridge a la Lambda.

        Returns:
            La :class:`aws_cdk.aws_events.Rule` creada.
        """

        prefix = f"{self._config.project}-{self._config.env_name}"
        rule = events.Rule(
            self,
            "AnalysisCompletedWebhookRule",
            rule_name=f"{prefix}-analysis-completed-webhook",
            event_bus=self.event_bus,
            description=(
                "Enruta los Evento_Finalizacion (analysis.completed) a la Lambda "
                "notify_webhook (criterio 13.1)."
            ),
            # Match EXACTO por detail-type, como lo publica aggregate_results.
            event_pattern=events.EventPattern(
                detail_type=[_DETAIL_TYPE_ANALYSIS_COMPLETED],
            ),
        )
        rule.add_target(targets.LambdaFunction(self.notify_webhook))
        return rule

    # --- SES: topic de rebotes/quejas ---------------------------------------

    def _build_ses_bounce_topic(self) -> sns.Topic:
        """Crea el topic SNS que recibe los eventos de rebote y queja de SES.

        Es el **destino de eventos** del configuration set (criterio 13.10): SES
        publica aquí cada evento de rebote (``bounce``) y queja (``complaint``). Un
        topic SNS —frente a un destino de solo métricas de CloudWatch— da registros
        durables y consumibles: un proceso de operaciones puede suscribirse para
        reaccionar (p. ej. añadir la dirección a una lista de supresión) o alarmar,
        sin que este stack imponga ya un consumidor concreto.

        El topic se cifra en reposo con la CMK del proyecto (criterio 15.6) y su
        política deniega el acceso sin TLS (``aws:SecureTransport = false``), en
        línea con la DLQ y las demás colas del sistema. El permiso de publicación
        de SES sobre el topic lo concede el propio destino de eventos del
        configuration set (:meth:`_build_ses_configuration_set`), acotado a este
        topic concreto, sin comodines.

        Returns:
            El :class:`aws_cdk.aws_sns.Topic` creado.
        """

        prefix = f"{self._config.project}-{self._config.env_name}"
        return sns.Topic(
            self,
            "SesBounceComplaintTopic",
            topic_name=f"{prefix}-{_SES_BOUNCE_TOPIC_SUFFIX}",
            # SSE con la CMK del proyecto (criterio 15.6).
            master_key=self._storage.key,
            # Deniega toda operación sin TLS (aws:SecureTransport=false); el propio
            # CDK añade la política de recurso de denegación (defensa en profundidad).
            enforce_ssl=True,
        )

    # --- SES: configuration set (criterio 13.10) ----------------------------

    def _build_ses_configuration_set(self) -> ses.ConfigurationSet:
        """Crea el configuration set dedicado al seguimiento de rebotes/quejas.

        El configuration set es el mecanismo de SES para capturar eventos de envío
        y enrutarlos a un destino (criterio 13.10). Aquí se crea con un **destino de
        eventos** que publica EXACTAMENTE los eventos de rebote y queja
        (:data:`_SES_TRACKED_EVENTS`) en el topic SNS de este stack; gestionarlos es
        lo que evita que SES suspenda la cuenta por reputación (diseño sección 9.2).

        Se nombra con la convención ``{project}-{env}-email-events`` para que el
        backend (``notify_email``) lo referencie por nombre en cada ``SendEmail``
        (variable ``SES_CONFIGURATION_SET``). Al pasar el ``sns_topic`` como destino,
        CDK añade la política de recurso que autoriza a SES a publicar en ese topic
        concreto, de mínimo privilegio.

        Returns:
            El :class:`aws_cdk.aws_ses.ConfigurationSet` creado.
        """

        prefix = f"{self._config.project}-{self._config.env_name}"
        configuration_set = ses.ConfigurationSet(
            self,
            "EmailConfigurationSet",
            configuration_set_name=f"{prefix}-{_SES_CONFIG_SET_SUFFIX}",
            # Exige TLS en la entrega del correo saliente (defensa en profundidad).
            tls_policy=ses.ConfigurationSetTlsPolicy.REQUIRE,
            # Publica las métricas de reputación (BounceRate/ComplaintRate) que la
            # alarma de observabilidad vigila (diseño sección de observabilidad).
            reputation_metrics=True,
        )
        configuration_set.add_event_destination(
            "BounceComplaintDestination",
            destination=ses.EventDestination.sns_topic(self.ses_bounce_topic),
            # SOLO rebote y queja (criterio 13.10): no se capturan los demás tipos.
            events=list(_SES_TRACKED_EVENTS),
            enabled=True,
        )
        return configuration_set

    # --- SES: identidad verificada (criterio 13.4) --------------------------

    def _build_ses_identity(self) -> ses.EmailIdentity:
        """Crea la identidad SES verificada del remitente de las notificaciones.

        SES exige verificar la identidad (dirección o dominio) desde la que se
        envía antes de poder emitir correo (criterio 13.4). La dirección remitente
        proviene de ``config.ses_sender_email`` —un valor **no secreto por entorno**
        (marcador que el operador sustituye por el buzón real verificado fuera de
        banda); nunca se *hardcodea* una dirección real en el código (steering de
        seguridad)—.

        La identidad se asocia al configuration set de rebotes/quejas de este stack,
        de modo que todo correo enviado desde ella herede ese seguimiento por
        defecto aunque un llamador omitiera el ``ConfigurationSetName`` (defensa en
        profundidad sobre el criterio 13.10). En ``dev`` el valor es un marcador
        de dominio ``example.com``, suficiente para que ``cdk synth`` sintetice la
        identidad sin verificar un buzón real.

        Returns:
            La :class:`aws_cdk.aws_ses.EmailIdentity` creada.
        """

        return ses.EmailIdentity(
            self,
            "SenderIdentity",
            identity=ses.Identity.email(self._config.ses_sender_email),
            # Hereda el seguimiento de rebotes/quejas por defecto (criterio 13.10).
            configuration_set=self.ses_configuration_set,
        )
