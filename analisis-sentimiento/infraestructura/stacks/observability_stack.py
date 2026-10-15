"""Stack de dominio ``observability`` (operación). Tareas 13.5 y 13.6.

Dominio: observabilidad, alarmas y presupuesto. Es el **último** stack del orden
de despliegue (diseño 14.4.2): no crea datos de negocio, solo LEE nombres y ARN
de los stacks anteriores (``workflow``, ``api``, ``compute``) para construir las
dimensiones de sus métricas y las acciones de sus alarmas.

Alcance de esta tarea (13.5)
----------------------------
Esta tarea implementa **solo** el topic SNS de guardia y las **cuatro alarmas
críticas** del [catálogo de alarmas 13.2](design.md) y del Requirement 14
(criterios 14.7, 14.8, 14.9, 14.14):

1. **Mensajes en la DLQ_Solicitudes** (criterio 14.7): alarma sobre
   ``ApproximateNumberOfMessagesVisible`` de la DLQ de la Cola_Solicitudes del
   stack ``ingest`` (``workflow._ingest.requests_dlq``), que se dispara con ≥ 1
   mensaje visible. Un mensaje en esta DLQ significa que el arranque de la
   orquestación de un Lote falló de forma persistente (3 recepciones): es un
   incidente que exige intervención.
2. **Ejecuciones de Step Functions fallidas** (criterio 14.8): alarma sobre la
   métrica ``ExecutionsFailed`` de la Maquina_De_Estados
   (``workflow.state_machine``), que se dispara con ≥ 1 fallo en 5 minutos.
3. **``AccessDenied`` en cualquier Lambda** (criterio 14.9): un *metric filter*
   sobre los *log groups* de las Lambdas de procesamiento que incrementa una
   métrica custom al detectar ``AccessDenied`` en el log, con una alarma que se
   dispara con ≥ 1 ocurrencia. Un acceso denegado es un **defecto de despliegue**
   (una política IAM mal cableada), no un error de datos, de ahí su severidad
   crítica.
4. **Lotes atascados** (criterio 14.9/14.14, "stuck batches"): alarma sobre la
   métrica custom EMF ``StuckAnalyses`` que publica el sweeper ``reconcile_stuck``
   (namespace ``SentimentPipeline``, criterio 10.15), que se dispara con ≥ 1 Lote
   expirado en una ejecución del barrido.

Todas las alarmas críticas envían su notificación al **topic SNS de guardia**
(:attr:`oncall_topic`, criterio 14.14). La suscripción concreta (correo, canal de
guardia) se hace **fuera de banda** —no se *hardcodea* ningún endpoint ni correo
real en el código (steering de seguridad / criterio 17.20)—.

Alcance de la tarea 13.6 (esta extensión)
-----------------------------------------
Sobre la base de 13.5 (topic de guardia + cuatro alarmas críticas), la tarea 13.6
añade, **sin tocar la firma de ``__init__``** y de forma puramente aditiva:

1. **Panel de CloudWatch** (:attr:`dashboard`, criterio 14.13): un único
   *dashboard* que agrupa las métricas clave de las tres fases del pipeline
   —ingesta, procesamiento y notificación— para diagnosticar el estado del
   Sistema de un vistazo (throughput de Ítems, fallos, throttling y tokens de
   Bedrock, Lotes atascados, latencia de ``api_submit`` y profundidad de la
   Cola_Solicitudes). Las métricas de negocio (``ItemsProcessed``/``ItemsFailed``/
   ``BedrockThrottles``/``BedrockInputTokens``/``BedrockOutputTokens``/
   ``StuckAnalyses``) las **produce** el backend en formato EMF (criterio 14.6);
   aquí solo se referencian por namespace + nombre como constantes locales, igual
   que 13.5 referenció ``StuckAnalyses`` (ver nota sobre el contrato observable).

2. **Alarmas no críticas** del [catálogo 13.2](design.md) (criterios 14.10, 14.11,
   14.12), que se enrutan al **mismo topic de guardia** (:attr:`oncall_topic`):
   el diseño 13.2 define un único topic para la notificación operativa y la tarea
   permite reutilizarlo para las severidades alta/media, evitando dispersar la
   suscripción fuera de banda en varios endpoints.

   - **Severidad ALTA**
     - *Errores de Lambda* (criterio 14.10): la proporción ``Errors / Invocations``
       de cada Lambda del camino crítico (las tres de ``compute`` más
       ``api_submit``) supera el 5 % en 5 minutos.
     - *Throttling de Bedrock*: la métrica custom EMF ``BedrockThrottles`` supera
       50 en 5 minutos (anticipa el agotamiento de cuota del Modelo_Bedrock).
     - *Mensaje de solicitudes sin consumir*: la antigüedad del mensaje más viejo
       de la Cola_Solicitudes supera 300 s (detecta un Pipe deshabilitado o
       atascado antes de que el sweeper expire el Lote, diseño 10.6.1).
     - *Bounce rate de SES*: la métrica de reputación ``Reputation.BounceRate`` de
       la cuenta supera el 5 % (una tasa de rebote alta amenaza la entregabilidad
       de las notificaciones de fin de proceso).
     - *Presupuesto diario* (criterio 14.12): un **AWS Budget** de costo con
       unidad diaria dispara al superar el umbral acordado
       (:data:`_DAILY_BUDGET_USD`) y notifica al topic de guardia. Es la red de
       seguridad financiera frente a un gasto desbocado de Bedrock.
   - **Severidad MEDIA**
     - *Ítems fallidos* (criterio 14.11): la métrica custom EMF ``ItemsFailed``
       supera el umbral del catálogo. El criterio liga el umbral al 5 % del
       ``item_count`` del Lote, un porcentaje por Lote que CloudWatch no puede
       computar de forma genérica (no conoce el ``item_count`` de cada Lote); se
       aproxima con un umbral absoluto por ventana y se documenta la desviación.
     - *Lambda throttled*: la métrica ``Throttles`` de cualquier Lambda del camino
       crítico ≥ 1 en 5 minutos (señala falta de concurrencia).
     - *Latencia p99 de ``api_submit``*: ``Duration`` p99 > 5 s (degradación de la
       experiencia de envío).

3. **Trazas X-Ray** (criterio 14.5): se habilitan **aguas arriba**, no en este
   stack. Cada Lambda sale de :class:`~constructs.backend_function.BackendFunction`
   con ``Tracing.ACTIVE`` y la Maquina_De_Estados con ``tracing_enabled=True``, de
   modo que las trazas distribuidas ya cubren el pipeline de extremo a extremo. El
   diseño no pide un recurso de X-Ray a nivel de stack (p. ej. una regla de
   muestreo), por lo que esta extensión no crea ninguno: documentarlo es la acción
   correcta (ver :meth:`_assert_tracing_enabled_upstream`).

La estructura sigue usando métodos privados ``_build_*`` y expone atributos
públicos tipados (:attr:`oncall_topic`, las cuatro alarmas críticas de 13.5, el
:attr:`dashboard`, las alarmas no críticas y el :attr:`daily_budget`) para que las
Pruebas_De_Infraestructura afirmen sobre ellos.

Cifrado del topic
-----------------
El topic de guardia se cifra en reposo con la **clave gestionada por AWS para SNS**
(``alias/aws/sns``), no con la CMK del proyecto: este stack **no** recibe la CMK
del ``StorageStack`` entre sus *props* (su firma es ``workflow``/``api``/``compute``,
fijada por ``app.py`` y por la tarea 1.6) y alcanzarla a través de un stack
dependiente rompería el encapsulamiento del grafo de dependencias (diseño 14.4.3).
Las alarmas no transportan PII —solo nombres de métrica, umbrales e
identificadores de recurso—, por lo que la clave gestionada de SNS es suficiente
para el cifrado en reposo de las notificaciones (criterio 15.6 aplica a los datos
de negocio: bucket, tabla, colas y log groups, todos ya cifrados con la CMK).

Dependencias (diseño 14.4.2): ``workflow``, ``api`` y ``compute``; solo lee de
ellos nombres y ARN para las dimensiones de las métricas y las fuentes de las
alarmas.
"""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from config.environments import EnvConfig
from stacks.api_stack import ApiStack
from stacks.compute_stack import ComputeStack
from stacks.workflow_stack import WorkflowStack

# --- Namespace de las métricas custom EMF (fuente única: backend ---
# ``sentiment/observability/metrics.py``). Las alarmas que leen una métrica EMF
# del backend DEBEN usar el mismo namespace que el productor, o alarmarían sobre
# una métrica inexistente. Se mantiene como constante local porque la
# Infraestructura no importa el paquete ``backend`` (son dos venvs/paquetes
# distintos); el valor es parte del contrato observable del Sistema.
_METRICS_NAMESPACE = "SentimentPipeline"

# Nombre de la métrica custom EMF de Lotes atascados que publica el sweeper
# ``reconcile_stuck`` (criterio 10.15; backend ``MetricName.STUCK_ANALYSES``).
_METRIC_STUCK_ANALYSES = "StuckAnalyses"

# --- Métricas custom EMF restantes del Sistema (criterio 14.6) ------------
# Nombres byte a byte del enum ``MetricName`` del backend
# (``sentiment/observability/metrics.py``). Como con ``StuckAnalyses``, la
# Infraestructura NO importa el paquete backend (venvs distintos): el nombre es
# parte del contrato observable y se duplica aquí como constante local. Un
# desajuste en estos nombres alarmaría/graficaría sobre una métrica inexistente.
_METRIC_ITEMS_PROCESSED = "ItemsProcessed"
_METRIC_ITEMS_FAILED = "ItemsFailed"
_METRIC_BEDROCK_THROTTLES = "BedrockThrottles"
_METRIC_BEDROCK_INPUT_TOKENS = "BedrockInputTokens"
_METRIC_BEDROCK_OUTPUT_TOKENS = "BedrockOutputTokens"

# --- Métrica custom de ``AccessDenied`` derivada del log (criterio 14.9) ---
# El *metric filter* escribe esta métrica custom cuando detecta ``AccessDenied``
# en el log de una Lambda. Se agrupa bajo el mismo namespace del Sistema.
_METRIC_ACCESS_DENIED = "AccessDeniedErrors"

# Patrón del *metric filter*: cualquier línea de log que contenga el término
# ``AccessDenied`` (cubre ``AccessDeniedException`` de IAM/STS y los mensajes de
# acceso denegado de las dependencias). Es una subcadena, no un JSON path, para
# capturar tanto los logs estructurados como los mensajes de error crudos.
_ACCESS_DENIED_PATTERN = "AccessDenied"

# --- Ventana de evaluación común de las alarmas (diseño 13.2) ---
# Las alarmas críticas de evento (DLQ, ejecuciones fallidas, AccessDenied, lotes
# atascados) evalúan en una ventana de 5 minutos y disparan con un único periodo
# en alarma: un solo evento crítico ya exige intervención.
_ALARM_PERIOD = Duration.minutes(5)
_EVALUATION_PERIODS = 1

# Umbral de las alarmas de evento: cualquier valor ≥ 1 dispara (criterios
# 14.7/14.8/14.9). Se expresa como "> 0" (``GREATER_THAN_THRESHOLD`` con umbral 0)
# para que el estado sea OK con 0 y ALARM con 1 o más.
_THRESHOLD_ZERO = 0

# --- Umbrales de las alarmas NO críticas (catálogo de alarmas, diseño 13.2) ---
# Proporción de error de una Lambda (``Errors / Invocations``) que dispara la
# alarma de severidad alta (criterio 14.10): 5 % en una ventana de 5 minutos. Se
# expresa como fracción porque la ``MathExpression`` divide dos conteos.
_LAMBDA_ERROR_RATE_THRESHOLD = 0.05

# Throttles del Modelo_Bedrock en 5 minutos que disparan la alarma alta (13.2):
# por encima de 50 throttles la cuota del modelo está realmente comprometida; por
# debajo, el backoff de la Maquina_De_Estados los absorbe sin incidente.
_BEDROCK_THROTTLES_THRESHOLD = 50

# Antigüedad (segundos) del mensaje más viejo de la Cola_Solicitudes que dispara
# la alarma alta (13.2 / diseño 10.6.1): 300 s detectan un Pipe deshabilitado o
# atascado ~10 min antes de que el sweeper expire el Lote.
_REQUESTS_AGE_THRESHOLD_SECONDS = 300

# Tasa de rebote de SES que dispara la alarma alta (13.2): 5 %. Por encima de este
# valor la reputación de envío —y por tanto la entregabilidad de las
# notificaciones de fin de proceso— está en riesgo.
_SES_BOUNCE_RATE_THRESHOLD = 0.05

# Ítems fallidos por ventana que disparan la alarma MEDIA (criterio 14.11). El
# criterio liga el umbral al 5 % del ``item_count`` del Lote, un porcentaje por
# Lote que CloudWatch no puede computar genéricamente (desconoce el ``item_count``
# de cada ejecución). Se aproxima con un conteo absoluto por ventana de 5 min;
# la desviación se documenta en :meth:`_build_items_failed_alarm`.
_ITEMS_FAILED_THRESHOLD = 10

# Latencia p99 (segundos) de ``api_submit`` que dispara la alarma media (13.2):
# por encima de 5 s la experiencia de envío se degrada de forma perceptible.
_API_SUBMIT_P99_SECONDS = 5

# Umbral absoluto ≥ 1 para las alarmas de evento no críticas (p. ej. ``Throttles``
# de Lambda, 13.2): el estado es OK con 0 y ALARM con 1 o más.
_THRESHOLD_ONE = 1

# --- Presupuesto diario (AWS Budgets, criterio 14.12) ----------------------
# Importe (USD) del gasto DIARIO a partir del cual se dispara la alarma alta de
# presupuesto. Es un marcador de proyecto deliberadamente conservador: el importe
# real por entorno se ajusta fuera de banda (no se expone el presupuesto real de
# la cuenta en el Repositorio; steering de seguridad). Se mantiene como constante
# local porque ``EnvConfig`` no transporta hoy un umbral de presupuesto y añadir
# un campo obligatorio rompería la carga de los entornos ya desplegados.
_DAILY_BUDGET_USD = 100.0

# Porcentaje del presupuesto a partir del cual AWS Budgets notifica. Se alarma al
# alcanzar el 100 % del gasto REAL diario (``ACTUAL``), que es el evento que el
# criterio 14.12 describe ("el gasto diario excede el umbral presupuestado").
_BUDGET_NOTIFICATION_PERCENT = 100.0

# Principal de servicio de AWS Budgets, autorizado a publicar en el topic de
# guardia para la notificación de presupuesto (política de recurso del topic).
_BUDGETS_SERVICE_PRINCIPAL = "budgets.amazonaws.com"

# Ventana común de las alarmas de proporción/latencia no críticas (5 min, 13.2).
_RATE_ALARM_PERIOD = Duration.minutes(5)


class ObservabilityStack(Stack):
    """Stack del dominio de operación: topic de guardia y alarmas críticas (13.5).

    Expone el topic SNS y las cuatro alarmas críticas como atributos públicos
    tipados para que (a) la tarea 13.6 extienda el stack (panel, métricas y
    alarmas no críticas) reutilizando el mismo topic, y (b) las
    Pruebas_De_Infraestructura afirmen sobre ellos. Todas las alarmas usan el
    topic de guardia como acción de alarma (criterio 14.14).

    Attributes:
        oncall_topic: El topic SNS de guardia que recibe las alarmas críticas
            (:class:`aws_cdk.aws_sns.ITopic`), cifrado con la clave gestionada de
            SNS. La suscripción (correo/canal) se hace fuera de banda.
        dlq_requests_alarm: Alarma crítica de mensajes en la DLQ_Solicitudes
            (criterio 14.7).
        executions_failed_alarm: Alarma crítica de ejecuciones de Step Functions
            fallidas (criterio 14.8).
        access_denied_alarm: Alarma crítica de ``AccessDenied`` en cualquier
            Lambda (criterio 14.9).
        stuck_analyses_alarm: Alarma crítica de Lotes atascados (criterio 14.9).
        dashboard: Panel de CloudWatch que agrupa las métricas clave de ingesta,
            procesamiento y notificación del pipeline (criterio 14.13).
        lambda_error_rate_alarms: Alarmas de severidad alta (una por Lambda del
            camino crítico) sobre la proporción de error ``Errors/Invocations``
            > 5 % en 5 min (criterio 14.10).
        bedrock_throttles_alarm: Alarma alta sobre ``BedrockThrottles`` > 50 en
            5 min (catálogo 13.2).
        requests_age_alarm: Alarma alta sobre la antigüedad del mensaje más viejo
            de la Cola_Solicitudes > 300 s (catálogo 13.2 / diseño 10.6.1).
        ses_bounce_rate_alarm: Alarma alta sobre la tasa de rebote de SES > 5 %
            (catálogo 13.2).
        items_failed_alarm: Alarma media sobre la métrica custom ``ItemsFailed``
            (criterio 14.11; ver la desviación documentada del porcentaje por Lote).
        lambda_throttled_alarms: Alarmas medias (una por Lambda del camino crítico)
            sobre ``Throttles`` ≥ 1 en 5 min (catálogo 13.2).
        api_submit_latency_alarm: Alarma media sobre la latencia p99 de
            ``api_submit`` > 5 s (catálogo 13.2).
        daily_budget: AWS Budget de costo diario que notifica al topic de guardia
            al superar el umbral acordado (criterio 14.12).

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
        workflow: Stack de orquestación observado (Maquina_De_Estados, DLQ).
        api: Stack de exposición observado.
        compute: Stack de procesamiento observado (Lambdas con sus log groups).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        workflow: WorkflowStack,
        api: ApiStack,
        compute: ComputeStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._config = config
        self._workflow = workflow
        self._api = api
        self._compute = compute
        # Es el último del orden de despliegue: depende de todos los anteriores.
        self.add_dependency(workflow)
        self.add_dependency(api)
        self.add_dependency(compute)

        self._prefix = f"{config.project}-{config.env_name}"

        # 1) Topic de guardia (debe existir antes que las alarmas: es su acción).
        self.oncall_topic: sns.ITopic = self._build_oncall_topic()

        # 2) Las cuatro alarmas críticas, todas con acción → topic de guardia.
        self.dlq_requests_alarm = self._build_dlq_requests_alarm()
        self.executions_failed_alarm = self._build_executions_failed_alarm()
        self.access_denied_alarm = self._build_access_denied_alarm()
        self.stuck_analyses_alarm = self._build_stuck_analyses_alarm()

        # --- Tarea 13.6: panel, alarmas no críticas y presupuesto ---
        # Las trazas X-Ray (criterio 14.5) se habilitan aguas arriba (cada Lambda
        # y la state machine); aquí solo se verifica como guarda defensiva.
        self._assert_tracing_enabled_upstream()

        # 3) Alarmas de severidad ALTA → topic de guardia (criterios 14.10/14.12).
        self.lambda_error_rate_alarms = self._build_lambda_error_rate_alarms()
        self.bedrock_throttles_alarm = self._build_bedrock_throttles_alarm()
        self.requests_age_alarm = self._build_requests_age_alarm()
        self.ses_bounce_rate_alarm = self._build_ses_bounce_rate_alarm()

        # 4) Alarmas de severidad MEDIA → topic de guardia (criterio 14.11).
        self.items_failed_alarm = self._build_items_failed_alarm()
        self.lambda_throttled_alarms = self._build_lambda_throttled_alarms()
        self.api_submit_latency_alarm = self._build_api_submit_latency_alarm()

        # 5) Presupuesto diario (AWS Budgets, criterio 14.12). Debe ir tras las
        # alarmas porque autoriza al servicio de Budgets a publicar en el topic.
        self.daily_budget = self._build_daily_budget()

        # 6) Panel de CloudWatch con el estado del pipeline (criterio 14.13).
        self.dashboard = self._build_dashboard()

    # --- Topic SNS de guardia -------------------------------------------------

    def _build_oncall_topic(self) -> sns.Topic:
        """Crea el topic SNS de guardia cifrado, sin suscripciones *hardcodeadas*.

        El topic se cifra en reposo con la clave gestionada por AWS para SNS
        (``alias/aws/sns``): este stack no recibe la CMK del proyecto entre sus
        *props* y alcanzarla a través de un stack dependiente rompería el
        encapsulamiento del grafo (ver docstring del módulo). Las notificaciones
        de alarma no contienen PII, por lo que la clave gestionada basta.

        **No** se declara ninguna suscripción (correo, HTTPS, canal de guardia) en
        el código: el endpoint real se conecta fuera de banda, de modo que ningún
        dato de contacto quede *hardcodeado* en el Repositorio (steering de
        seguridad / criterio 17.20).

        Returns:
            El :class:`aws_cdk.aws_sns.Topic` de guardia.
        """

        return sns.Topic(
            self,
            "OnCallTopic",
            topic_name=f"{self._prefix}-oncall-critical",
            display_name=f"{self._prefix} alarmas críticas",
            # Cifrado en reposo con la clave gestionada de SNS (ver docstring).
            master_key=kms.Alias.from_alias_name(
                self, "SnsManagedKey", "alias/aws/sns"
            ),
        )

    # --- Alarma 1: mensajes en la DLQ_Solicitudes (criterio 14.7) ------------

    def _build_dlq_requests_alarm(self) -> cloudwatch.Alarm:
        """Alarma crítica cuando aparece ≥ 1 mensaje en la DLQ_Solicitudes.

        Lee ``ApproximateNumberOfMessagesVisible`` de la DLQ de la Cola_Solicitudes
        (``ingest.requests_dlq``, referenciada vía el stack ``workflow``), con la
        estadística ``Maximum`` en la ventana de 5 minutos: así un único mensaje
        visible en cualquier instante del periodo dispara la alarma (criterio
        14.7). Un mensaje aquí significa que el arranque de la orquestación de un
        Lote falló de forma persistente tras agotar sus reintentos.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        # La DLQ_Solicitudes vive en el stack ``ingest``, del que depende
        # ``workflow``; se referencia por su atributo público tipado (IQueue).
        requests_dlq = self._workflow._ingest.requests_dlq

        metric = requests_dlq.metric_approximate_number_of_messages_visible(
            period=_ALARM_PERIOD,
            statistic=cloudwatch.Stats.MAXIMUM,
        )

        return self._critical_alarm(
            "DlqRequestsMessages",
            metric=metric,
            alarm_description=(
                "CRÍTICA: hay mensajes en la DLQ_Solicitudes; el arranque de la "
                "orquestación de un Lote falló de forma persistente (criterio 14.7)."
            ),
        )

    # --- Alarma 2: ejecuciones de Step Functions fallidas (criterio 14.8) ----

    def _build_executions_failed_alarm(self) -> cloudwatch.Alarm:
        """Alarma crítica cuando una ejecución de la Maquina_De_Estados falla.

        Lee la métrica ``ExecutionsFailed`` de la Maquina_De_Estados
        (``workflow.state_machine.metric_failed``) con ``Sum`` en 5 minutos:
        cualquier ejecución fallida (≥ 1) dispara la alarma (criterio 14.8). Una
        ejecución en fallo es un Lote que no alcanzó su Estado_Terminal por el
        camino feliz y exige revisión.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        metric = self._workflow.state_machine.metric_failed(
            period=_ALARM_PERIOD,
            statistic=cloudwatch.Stats.SUM,
        )

        return self._critical_alarm(
            "StepFunctionsExecutionsFailed",
            metric=metric,
            alarm_description=(
                "CRÍTICA: una ejecución de la Maquina_De_Estados terminó en fallo "
                "(ExecutionsFailed ≥ 1 en 5 min; criterio 14.8)."
            ),
        )

    # --- Alarma 3: AccessDenied en cualquier Lambda (criterio 14.9) ----------

    def _build_access_denied_alarm(self) -> cloudwatch.Alarm:
        """Alarma crítica cuando se registra ``AccessDenied`` en cualquier Lambda.

        Instala un *metric filter* sobre los *log groups* de las Lambdas de
        procesamiento (``analyze_comprehend``, ``analyze_llm``,
        ``aggregate_results``) que incrementa la métrica custom
        :data:`_METRIC_ACCESS_DENIED` cada vez que una línea de log contiene
        ``AccessDenied``. La alarma dispara con ≥ 1 ocurrencia en 5 minutos. Un
        acceso denegado es un **defecto de despliegue** (una política IAM mal
        cableada), no un error de datos, de ahí su severidad crítica (diseño 13.2).

        El filtro se aplica a los log groups de ``compute`` porque son las Lambdas
        del camino de procesamiento que acceden a Bedrock, Comprehend, DynamoDB,
        S3 y EventBridge con políticas de mínimo privilegio: un ``AccessDenied``
        ahí señala exactamente el tipo de bug de permisos que esta alarma vigila.
        La tarea 13.6 puede extender el filtro a las Lambdas de ``api``/``workflow``
        reutilizando la misma métrica y alarma.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        # Un filtro por log group, todos escribiendo la MISMA métrica custom: la
        # alarma agrega las ocurrencias de todas las funciones en una sola señal.
        for construct_id, function in self._compute_functions().items():
            logs.MetricFilter(
                self,
                f"AccessDeniedFilter{construct_id}",
                log_group=function.log_group,
                metric_namespace=_METRICS_NAMESPACE,
                metric_name=_METRIC_ACCESS_DENIED,
                filter_pattern=logs.FilterPattern.any_term(_ACCESS_DENIED_PATTERN),
                metric_value="1",
                # Sin ocurrencias, la métrica vale 0 (y no "sin datos"), de modo
                # que la alarma tenga un estado OK bien definido en reposo.
                default_value=0,
            )

        metric = cloudwatch.Metric(
            namespace=_METRICS_NAMESPACE,
            metric_name=_METRIC_ACCESS_DENIED,
            period=_ALARM_PERIOD,
            statistic=cloudwatch.Stats.SUM,
        )

        return self._critical_alarm(
            "AccessDeniedInAnyLambda",
            metric=metric,
            alarm_description=(
                "CRÍTICA: se registró AccessDenied en una Lambda; es un defecto de "
                "despliegue de una política IAM (criterio 14.9)."
            ),
        )

    # --- Alarma 4: Lotes atascados (criterio 14.9 / stuck batches) -----------

    def _build_stuck_analyses_alarm(self) -> cloudwatch.Alarm:
        """Alarma crítica cuando el sweeper expira ≥ 1 Lote atascado.

        Lee la métrica custom EMF ``StuckAnalyses`` que publica el sweeper
        ``reconcile_stuck`` en cada barrido (namespace ``SentimentPipeline``,
        criterio 10.15), con ``Sum`` en 5 minutos. El sweeper publica la métrica
        **incluso con valor 0** en cada ejecución, de modo que la alarma tiene un
        estado OK bien definido y solo dispara cuando algún Lote quedó atascado y
        hubo que cerrarlo (≥ 1).

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        metric = cloudwatch.Metric(
            namespace=_METRICS_NAMESPACE,
            metric_name=_METRIC_STUCK_ANALYSES,
            period=_ALARM_PERIOD,
            statistic=cloudwatch.Stats.SUM,
        )

        return self._critical_alarm(
            "StuckAnalyses",
            metric=metric,
            alarm_description=(
                "CRÍTICA: el sweeper expiró uno o más Lotes atascados "
                "(StuckAnalyses ≥ 1; criterios 14.9 / 10.15)."
            ),
        )

    # --- Tarea 13.6: trazas X-Ray (criterio 14.5) ----------------------------

    def _assert_tracing_enabled_upstream(self) -> None:
        """Verifica que las trazas X-Ray estén habilitadas aguas arriba (14.5).

        El criterio 14.5 ("las Lambdas emiten trazas de X-Ray") se cumple en el
        construct :class:`~constructs.backend_function.BackendFunction`
        (``Tracing.ACTIVE``) y en la Maquina_De_Estados (``tracing_enabled=True``),
        no en este stack: el diseño no define ningún recurso de X-Ray a nivel de
        observabilidad (p. ej. una regla de muestreo). Esta guarda no crea nada;
        documenta la decisión y falla pronto si alguna Lambda del camino crítico
        perdiera su configuración de trazas en un refactor futuro, de modo que la
        garantía observable quede anclada por una afirmación verificable y no por
        un comentario que puede quedar obsoleto.

        Raises:
            ValueError: Si alguna Lambda observada no tiene las trazas activas.
        """

        for construct_id, function in self._critical_path_functions().items():
            if function.node.default_child is None:  # pragma: no cover - defensivo
                continue
            tracing = function.node.default_child.tracing_config  # type: ignore[union-attr]
            # ``tracing_config`` es ``None`` solo si la Lambda se creó sin trazas;
            # ``BackendFunction`` siempre lo fija a ``Active``.
            if tracing is None:
                raise ValueError(
                    f"La Lambda {construct_id!r} del camino crítico no tiene trazas "
                    "X-Ray activas; viola el criterio 14.5. Debe construirse con "
                    "BackendFunction (Tracing.ACTIVE)."
                )

    # --- Tarea 13.6: alarmas de severidad ALTA -------------------------------

    def _build_lambda_error_rate_alarms(self) -> list[cloudwatch.Alarm]:
        """Alarma alta por Lambda cuando su tasa de error supera el 5 % (14.10).

        Para cada Lambda del camino crítico (las tres de ``compute`` más
        ``api_submit``) construye una :class:`aws_cdk.aws_cloudwatch.MathExpression`
        ``errors / invocations`` sobre una ventana de 5 minutos y alarma cuando la
        proporción supera :data:`_LAMBDA_ERROR_RATE_THRESHOLD`. Se usa una alarma
        por función (en vez de una agregada) para que la notificación identifique
        exactamente qué Lambda se está degradando, que es lo que el operador
        necesita para actuar.

        El tratamiento de "sin datos" es ``NOT_BREACHING``: una función sin
        invocaciones en la ventana no tiene tasa de error definida y no es un
        incidente (evita además la división por cero, que CloudWatch resuelve
        dejando la expresión sin datos).

        Returns:
            La lista de alarmas creadas, una por Lambda, con acción → topic.
        """

        alarms: list[cloudwatch.Alarm] = []
        for construct_id, function in self._critical_path_functions().items():
            error_rate = cloudwatch.MathExpression(
                expression="(errors / invocations) ",
                using_metrics={
                    "errors": function.metric_errors(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.SUM,
                    ),
                    "invocations": function.metric_invocations(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.SUM,
                    ),
                },
                period=_RATE_ALARM_PERIOD,
                label=f"{construct_id} error rate",
            )
            alarms.append(
                self._noncritical_alarm(
                    f"LambdaErrorRate{construct_id}",
                    metric=error_rate,
                    threshold=_LAMBDA_ERROR_RATE_THRESHOLD,
                    evaluation_periods=1,
                    alarm_description=(
                        f"ALTA: la Lambda {construct_id} supera el 5% de error en "
                        "5 min (Errors/Invocations; criterio 14.10)."
                    ),
                )
            )
        return alarms

    def _build_bedrock_throttles_alarm(self) -> cloudwatch.Alarm:
        """Alarma alta cuando ``BedrockThrottles`` supera 50 en 5 min (13.2).

        Lee la métrica custom EMF ``BedrockThrottles`` que publica el backend
        (namespace ``SentimentPipeline``) con ``Sum`` en 5 minutos. Un pico de
        throttling por encima de :data:`_BEDROCK_THROTTLES_THRESHOLD` indica que la
        cuota del Modelo_Bedrock está realmente comprometida y que el backoff de la
        Maquina_De_Estados ya no basta para absorberlo de forma transparente.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        metric = cloudwatch.Metric(
            namespace=_METRICS_NAMESPACE,
            metric_name=_METRIC_BEDROCK_THROTTLES,
            period=_RATE_ALARM_PERIOD,
            statistic=cloudwatch.Stats.SUM,
        )
        return self._noncritical_alarm(
            "BedrockThrottles",
            metric=metric,
            threshold=_BEDROCK_THROTTLES_THRESHOLD,
            evaluation_periods=1,
            alarm_description=(
                "ALTA: el Modelo_Bedrock supera 50 throttles en 5 min; la cuota "
                "está comprometida (BedrockThrottles; catálogo 13.2)."
            ),
        )

    def _build_requests_age_alarm(self) -> cloudwatch.Alarm:
        """Alarma alta si el mensaje más viejo de la Cola_Solicitudes supera 300 s.

        Lee ``ApproximateAgeOfOldestMessage`` de la Cola_Solicitudes
        (``workflow._ingest.requests_queue``) con ``Maximum`` en 5 minutos. Un
        mensaje que envejece por encima de :data:`_REQUESTS_AGE_THRESHOLD_SECONDS`
        delata un EventBridge Pipe deshabilitado o atascado, y avisa ~10 min antes
        de que el sweeper expire el Lote (diseño 10.6.1), dando margen a reactivar
        el consumo sin perder trabajo.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        requests_queue = self._workflow._ingest.requests_queue
        metric = requests_queue.metric_approximate_age_of_oldest_message(
            period=_RATE_ALARM_PERIOD,
            statistic=cloudwatch.Stats.MAXIMUM,
        )
        return self._noncritical_alarm(
            "RequestsOldestMessageAge",
            metric=metric,
            threshold=_REQUESTS_AGE_THRESHOLD_SECONDS,
            evaluation_periods=1,
            alarm_description=(
                "ALTA: hay mensajes sin consumir en la Cola_Solicitudes > 300 s; "
                "el Pipe puede estar deshabilitado o atascado (catálogo 13.2)."
            ),
        )

    def _build_ses_bounce_rate_alarm(self) -> cloudwatch.Alarm:
        """Alarma alta cuando la tasa de rebote de SES supera el 5 % (13.2).

        Lee la métrica de reputación de la cuenta ``Reputation.BounceRate``
        (namespace ``AWS/SES``, sin dimensiones: es una métrica a nivel de cuenta)
        con ``Average`` en 5 minutos. Una tasa de rebote sostenida por encima del
        5 % degrada la reputación de envío y, con ella, la entregabilidad de las
        notificaciones de fin de proceso; AWS puede llegar a pausar la cuenta de
        envío si la métrica no se corrige.

        Nota de encapsulamiento: este stack no recibe el ``NotificationsStack``
        entre sus *props* (su firma es ``workflow``/``api``/``compute``), pero la
        métrica de reputación es de cuenta y no necesita el ARN del
        *configuration set*, así que se referencia directamente por namespace y
        nombre sin ampliar el contrato del constructor.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        metric = cloudwatch.Metric(
            namespace="AWS/SES",
            metric_name="Reputation.BounceRate",
            period=_RATE_ALARM_PERIOD,
            statistic=cloudwatch.Stats.AVERAGE,
        )
        return self._noncritical_alarm(
            "SesBounceRate",
            metric=metric,
            threshold=_SES_BOUNCE_RATE_THRESHOLD,
            evaluation_periods=1,
            alarm_description=(
                "ALTA: la tasa de rebote de SES supera el 5%; la entregabilidad de "
                "las notificaciones de fin de proceso está en riesgo (catálogo 13.2)."
            ),
        )

    # --- Tarea 13.6: alarmas de severidad MEDIA ------------------------------

    def _build_items_failed_alarm(self) -> cloudwatch.Alarm:
        """Alarma media sobre la métrica custom ``ItemsFailed`` (criterio 14.11).

        Lee la métrica custom EMF ``ItemsFailed`` (namespace ``SentimentPipeline``)
        con ``Sum`` en 5 minutos y alarma al superar :data:`_ITEMS_FAILED_THRESHOLD`.

        Desviación documentada (criterio 14.11): el criterio liga el umbral al
        **5 % del ``item_count`` del Lote**. Ese porcentaje es *por Lote* y depende
        de un dato (``item_count``) que CloudWatch no conoce al evaluar una métrica
        agregada por ventana temporal: no existe una forma genérica de expresar
        "5 % del item_count de cada Lote" como umbral de una alarma de CloudWatch.
        El cómputo exacto del porcentaje por Lote corresponde al backend —el
        agregador conoce el ``item_count`` y los Ítems fallidos de su Lote— y es
        allí donde debe emitirse una señal "lote degradado" si se requiere el
        umbral porcentual exacto. Aquí se aproxima con un umbral absoluto por
        ventana que capta un repunte anómalo de fallos a nivel de Sistema, que es
        lo máximo que esta capa puede garantizar sin el contexto del Lote. La
        desviación es intencionada y se eleva al usuario en el informe de la tarea.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        metric = cloudwatch.Metric(
            namespace=_METRICS_NAMESPACE,
            metric_name=_METRIC_ITEMS_FAILED,
            period=_RATE_ALARM_PERIOD,
            statistic=cloudwatch.Stats.SUM,
        )
        return self._noncritical_alarm(
            "ItemsFailed",
            metric=metric,
            threshold=_ITEMS_FAILED_THRESHOLD,
            evaluation_periods=1,
            alarm_description=(
                "MEDIA: repunte de Ítems fallidos en 5 min (ItemsFailed; criterio "
                "14.11, aproximado por umbral absoluto — ver docstring)."
            ),
        )

    def _build_lambda_throttled_alarms(self) -> list[cloudwatch.Alarm]:
        """Alarma media por Lambda cuando se registra ≥ 1 throttle en 5 min (13.2).

        Para cada Lambda del camino crítico lee su métrica ``Throttles`` con ``Sum``
        en 5 minutos y alarma con ≥ 1. Un throttle significa que la concurrencia
        reservada de la función (p. ej. el techo de 40 de ``analyze_llm``) se agotó
        y una invocación fue rechazada; a diferencia del error de datos, se corrige
        subiendo la concurrencia, de ahí su severidad media y no crítica.

        Returns:
            La lista de alarmas creadas, una por Lambda, con acción → topic.
        """

        alarms: list[cloudwatch.Alarm] = []
        for construct_id, function in self._critical_path_functions().items():
            metric = function.metric_throttles(
                period=_RATE_ALARM_PERIOD,
                statistic=cloudwatch.Stats.SUM,
            )
            alarms.append(
                self._noncritical_alarm(
                    f"LambdaThrottled{construct_id}",
                    metric=metric,
                    threshold=_THRESHOLD_ZERO,
                    evaluation_periods=1,
                    alarm_description=(
                        f"MEDIA: la Lambda {construct_id} fue throttled (≥ 1 en 5 "
                        "min); falta concurrencia reservada (catálogo 13.2)."
                    ),
                )
            )
        return alarms

    def _build_api_submit_latency_alarm(self) -> cloudwatch.Alarm:
        """Alarma media cuando la latencia p99 de ``api_submit`` supera 5 s (13.2).

        Lee la métrica ``Duration`` de ``api_submit`` con el estadístico p99 en una
        ventana de 5 minutos y alarma por encima de
        :data:`_API_SUBMIT_P99_SECONDS`. El p99 (no el promedio) captura la cola de
        latencia que degrada la experiencia de envío de los Tenants aunque la media
        siga siendo buena.

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada, con acción → topic.
        """

        api_submit = self._as_function(self._api.api_submit)
        metric = api_submit.metric_duration(
            period=_RATE_ALARM_PERIOD,
            statistic=cloudwatch.Stats.percentile(99),
        )
        return self._noncritical_alarm(
            "ApiSubmitLatencyP99",
            metric=metric,
            # ``Duration`` está en milisegundos: 5 s = 5000 ms.
            threshold=_API_SUBMIT_P99_SECONDS * 1000,
            evaluation_periods=1,
            alarm_description=(
                "MEDIA: la latencia p99 de api_submit supera 5 s; la experiencia de "
                "envío se degrada (catálogo 13.2)."
            ),
        )

    # --- Tarea 13.6: presupuesto diario (AWS Budgets, criterio 14.12) --------

    def _build_daily_budget(self) -> budgets.CfnBudget:
        """Crea un AWS Budget de costo diario que notifica al topic de guardia.

        Define un :class:`aws_cdk.aws_budgets.CfnBudget` de tipo ``COST`` con unidad
        temporal ``DAILY`` y límite :data:`_DAILY_BUDGET_USD`. Cuando el gasto
        **real** (``ACTUAL``) alcanza el :data:`_BUDGET_NOTIFICATION_PERCENT` del
        límite, Budgets publica en el topic de guardia (criterio 14.12). Para que el
        servicio de Budgets pueda publicar, se añade a la *resource policy* del
        topic un permiso ``SNS:Publish`` acotado al principal del servicio y al ARN
        del propio topic (mínimo privilegio): nada de comodines ni cuentas de
        terceros.

        El importe es un marcador de proyecto conservador (:data:`_DAILY_BUDGET_USD`);
        el presupuesto real por cuenta/entorno se ajusta fuera de banda para no
        exponer datos financieros sensibles en el Repositorio (steering de
        seguridad). El *budget* se nombra por ``{project}-{env}`` para localizarlo
        por entorno en la consola.

        Returns:
            El :class:`aws_cdk.aws_budgets.CfnBudget` creado.
        """

        # Autoriza al servicio de Budgets a publicar SOLO en este topic (mínimo
        # privilegio): sin esta sentencia, la notificación de presupuesto fallaría
        # silenciosamente. Se acota por ``aws:SourceAccount`` a la cuenta del stack.
        self.oncall_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBudgetsPublish",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal(_BUDGETS_SERVICE_PRINCIPAL)],
                actions=["SNS:Publish"],
                resources=[self.oncall_topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account}
                },
            )
        )

        return budgets.CfnBudget(
            self,
            "DailyCostBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{self._prefix}-daily-cost",
                budget_type="COST",
                time_unit="DAILY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=_DAILY_BUDGET_USD,
                    unit="USD",
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=_BUDGET_NOTIFICATION_PERCENT,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="SNS",
                            address=self.oncall_topic.topic_arn,
                        )
                    ],
                )
            ],
        )

    # --- Tarea 13.6: panel de CloudWatch (criterio 14.13) --------------------

    def _build_dashboard(self) -> cloudwatch.Dashboard:
        """Crea el panel de CloudWatch con el estado del pipeline (criterio 14.13).

        Agrupa en un único *dashboard* las métricas clave de las tres fases del
        pipeline, de modo que el operador diagnostique el estado del Sistema de un
        vistazo (criterio 14.13):

        - **Ingesta**: profundidad y antigüedad de la Cola_Solicitudes, y la
          latencia p99 de ``api_submit`` (la puerta de entrada de los Lotes).
        - **Procesamiento**: throughput (``ItemsProcessed``) frente a fallos
          (``ItemsFailed``), throttling (``BedrockThrottles``) y consumo de tokens
          (``BedrockInputTokens``/``BedrockOutputTokens``), más la tasa de error y
          los throttles de las Lambdas del camino.
        - **Notificación / salud**: Lotes atascados (``StuckAnalyses``), ejecuciones
          fallidas de la Maquina_De_Estados y tasa de rebote de SES.

        Las métricas de negocio las produce el backend en EMF (criterio 14.6); el
        panel solo las referencia por namespace + nombre. El panel se nombra por
        ``{project}-{env}`` para localizarlo por entorno.

        Returns:
            El :class:`aws_cdk.aws_cloudwatch.Dashboard` creado.
        """

        dashboard = cloudwatch.Dashboard(
            self,
            "PipelineDashboard",
            dashboard_name=f"{self._prefix}-pipeline",
        )

        functions = self._critical_path_functions()
        requests_queue = self._workflow._ingest.requests_queue

        dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown=(
                    f"# {self._prefix} — pipeline de análisis de sentimiento\n"
                    "Estado de **ingesta**, **procesamiento** y **notificación** "
                    "(criterio 14.13)."
                ),
                width=24,
                height=2,
            )
        )

        # Fila 1 — Ingesta.
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Ingesta · profundidad de la Cola_Solicitudes",
                left=[
                    requests_queue.metric_approximate_number_of_messages_visible(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.MAXIMUM,
                    ),
                ],
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Ingesta · antigüedad del mensaje más viejo (s)",
                left=[
                    requests_queue.metric_approximate_age_of_oldest_message(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.MAXIMUM,
                    ),
                ],
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Ingesta · latencia p99 de api_submit (ms)",
                left=[
                    self._as_function(self._api.api_submit).metric_duration(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.percentile(99),
                    ),
                ],
                width=8,
                height=6,
            ),
        )

        # Fila 2 — Procesamiento (throughput, fallos, Bedrock).
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Procesamiento · Ítems procesados vs. fallidos",
                left=[
                    self._emf_metric(_METRIC_ITEMS_PROCESSED),
                    self._emf_metric(_METRIC_ITEMS_FAILED),
                ],
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Procesamiento · throttles de Bedrock",
                left=[self._emf_metric(_METRIC_BEDROCK_THROTTLES)],
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Procesamiento · tokens de Bedrock (in/out)",
                left=[
                    self._emf_metric(_METRIC_BEDROCK_INPUT_TOKENS),
                    self._emf_metric(_METRIC_BEDROCK_OUTPUT_TOKENS),
                ],
                width=8,
                height=6,
            ),
        )

        # Fila 3 — Salud de las Lambdas del camino crítico.
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Procesamiento · errores de Lambda (suma)",
                left=[
                    function.metric_errors(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.SUM,
                    )
                    for function in functions.values()
                ],
                width=12,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Procesamiento · throttles de Lambda (suma)",
                left=[
                    function.metric_throttles(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.SUM,
                    )
                    for function in functions.values()
                ],
                width=12,
                height=6,
            ),
        )

        # Fila 4 — Notificación y salud terminal del Lote.
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Salud · Lotes atascados (StuckAnalyses)",
                left=[self._emf_metric(_METRIC_STUCK_ANALYSES)],
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Salud · ejecuciones fallidas de la Maquina_De_Estados",
                left=[
                    self._workflow.state_machine.metric_failed(
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.SUM,
                    ),
                ],
                width=8,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Notificación · tasa de rebote de SES",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/SES",
                        metric_name="Reputation.BounceRate",
                        period=_RATE_ALARM_PERIOD,
                        statistic=cloudwatch.Stats.AVERAGE,
                    ),
                ],
                width=8,
                height=6,
            ),
        )

        return dashboard

    # --- Utilidades compartidas ----------------------------------------------

    def _emf_metric(self, metric_name: str) -> cloudwatch.Metric:
        """Construye una métrica custom EMF del Sistema por su nombre.

        Centraliza la creación de las métricas del namespace ``SentimentPipeline``
        (producidas por el backend, criterio 14.6) con la ventana y el estadístico
        comunes del panel, para que el nombre de métrica —el único acoplamiento con
        el backend— aparezca una sola vez por métrica.

        Args:
            metric_name: Nombre EMF exacto de la métrica (constante local
                ``_METRIC_*``, byte a byte con el enum ``MetricName`` del backend).

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Metric` lista para graficar/alarmar.
        """

        return cloudwatch.Metric(
            namespace=_METRICS_NAMESPACE,
            metric_name=metric_name,
            period=_RATE_ALARM_PERIOD,
            statistic=cloudwatch.Stats.SUM,
        )

    def _critical_path_functions(self) -> dict[str, lambda_.Function]:
        """Lambdas del camino crítico observadas por las alarmas de 13.6.

        Reúne las tres Lambdas de procesamiento de ``compute`` y la ``api_submit``
        de ``api`` —las funciones cuya degradación afecta directamente al flujo de
        un Lote (procesamiento) o a su admisión (envío)—. Se usan para las alarmas
        de tasa de error (14.10) y de *throttling* (13.2) y para los *widgets* de
        salud del panel. ``api_status``/``api_download``/``api_purge`` quedan fuera:
        su degradación es menos crítica para el ciclo de vida del Lote y añadiría
        ruido al panel y a la notificación de guardia.

        Returns:
            Un mapa ``{id_lógico: Function}`` de las cuatro Lambdas del camino.
        """

        functions = dict(self._compute_functions())
        functions["ApiSubmit"] = self._as_function(self._api.api_submit)
        return functions

    def _noncritical_alarm(
        self,
        construct_id: str,
        *,
        metric: cloudwatch.IMetric,
        threshold: float,
        evaluation_periods: int,
        alarm_description: str,
    ) -> cloudwatch.Alarm:
        """Molde común de las alarmas NO críticas (alta/media) de la tarea 13.6.

        Comparte la configuración de las alarmas de severidad alta/media: operador
        ``GREATER_THAN_THRESHOLD``, "sin datos" tratado como ``NOT_BREACHING`` (una
        métrica sin muestras no es un incidente) y acción —de alarma y de vuelta a
        OK— hacia el **mismo topic de guardia** que las alarmas críticas, que el
        diseño 13.2 define como único punto de notificación operativa. A diferencia
        de :meth:`_critical_alarm`, el umbral y el número de periodos son
        parametrizables porque las alarmas no críticas usan umbrales heterogéneos
        (proporciones, segundos, conteos).

        Args:
            construct_id: Identificador lógico de la alarma dentro del stack.
            metric: La métrica (o expresión) sobre la que alarmar.
            threshold: Umbral de disparo.
            evaluation_periods: Número de periodos consecutivos en infracción.
            alarm_description: Descripción legible del incidente (sin PII).

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada y cableada al topic.
        """

        alarm = cloudwatch.Alarm(
            self,
            construct_id,
            alarm_name=f"{self._prefix}-{construct_id}",
            alarm_description=alarm_description,
            metric=metric,
            threshold=threshold,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            evaluation_periods=evaluation_periods,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        action = cw_actions.SnsAction(self.oncall_topic)
        alarm.add_alarm_action(action)
        alarm.add_ok_action(action)
        return alarm

    def _compute_functions(self) -> dict[str, lambda_.Function]:
        """Devuelve las Lambdas de ``compute`` indexadas por un id lógico estable.

        Las tres funciones del camino de procesamiento se exponen en ``compute``
        como :class:`aws_cdk.aws_lambda.IFunction`, pero en runtime son instancias
        de :class:`aws_cdk.aws_lambda.Function` (las construye ``BackendFunction``),
        por lo que exponen ``log_group``, que el *metric filter* de ``AccessDenied``
        necesita. El id lógico (``Comprehend``/``Llm``/``Aggregate``) se usa para
        nombrar de forma determinista los constructs del filtro por función.

        Returns:
            Un mapa ``{id_lógico: Function}`` de las tres Lambdas de procesamiento.
        """

        return {
            "Comprehend": self._as_function(self._compute.analyze_comprehend),
            "Llm": self._as_function(self._compute.analyze_llm),
            "Aggregate": self._as_function(self._compute.aggregate_results),
        }

    @staticmethod
    def _as_function(function: lambda_.IFunction) -> lambda_.Function:
        """Estrecha una ``IFunction`` de ``compute`` a la ``Function`` concreta.

        ``compute`` tipa sus Lambdas como ``IFunction`` (desacoplamiento, diseño
        14.4.3), pero son ``Function`` reales con un ``log_group`` explícito. El
        *metric filter* de ``AccessDenied`` necesita ese ``log_group``, así que se
        estrecha el tipo tras validar en runtime que la instancia lo es.

        Args:
            function: La Lambda expuesta por ``compute`` (``IFunction``).

        Returns:
            La misma Lambda vista como :class:`aws_cdk.aws_lambda.Function`.

        Raises:
            TypeError: Si la Lambda no es una ``Function`` concreta (no debería
                ocurrir con las funciones de ``compute``; es una guarda defensiva).
        """

        if not isinstance(function, lambda_.Function):
            raise TypeError(
                "Se esperaba una aws_lambda.Function concreta con log_group "
                f"explícito, no {type(function).__name__}."
            )
        return function

    def _critical_alarm(
        self,
        construct_id: str,
        *,
        metric: cloudwatch.IMetric,
        alarm_description: str,
    ) -> cloudwatch.Alarm:
        """Molde común de las alarmas críticas de evento, con acción → topic.

        Centraliza la configuración compartida de las cuatro alarmas críticas de
        evento (criterios 14.7/14.8/14.9): umbral ``> 0`` (dispara con ≥ 1),
        ``GREATER_THAN_THRESHOLD``, un único periodo de evaluación de 5 minutos y
        el tratamiento de "sin datos" como ``NOT_BREACHING`` (una métrica sin
        muestras no es un incidente). Conecta la acción de alarma —y la de vuelta a
        OK— al topic de guardia (criterio 14.14).

        Args:
            construct_id: Identificador lógico de la alarma dentro del stack.
            metric: La métrica sobre la que alarmar.
            alarm_description: Descripción legible del incidente para la consola y
                la notificación de guardia (sin PII).

        Returns:
            La :class:`aws_cdk.aws_cloudwatch.Alarm` creada y cableada al topic.
        """

        alarm = cloudwatch.Alarm(
            self,
            construct_id,
            alarm_name=f"{self._prefix}-{construct_id}",
            alarm_description=alarm_description,
            metric=metric,
            threshold=_THRESHOLD_ZERO,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            evaluation_periods=_EVALUATION_PERIODS,
            # Una métrica sin muestras no es un incidente: la alarma permanece OK.
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        # Acción de alarma → topic de guardia (criterio 14.14). Se añade también la
        # acción de vuelta a OK para que guardia reciba el cierre del incidente.
        action = cw_actions.SnsAction(self.oncall_topic)
        alarm.add_alarm_action(action)
        alarm.add_ok_action(action)
        return alarm
