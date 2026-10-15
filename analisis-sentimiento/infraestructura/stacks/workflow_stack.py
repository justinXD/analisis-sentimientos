"""Stack de dominio ``workflow`` (orquestación). Tarea 12.3.

Dominio: orquestación de un Lote con una única ejecución de Step Functions por
``analysis_id`` y fan-out interno por Ítem (diseño 4, 17).

La tarea 12.3 implementó la Maquina_De_Estados: la máquina padre de tipo Standard
(``MarkRunning`` → ``ProcessItems`` → ``Aggregate``, con el estado terminal
``MarkFailed``) y, dentro de ella, el estado ``ProcessItems`` como un Distributed
Map que ejecuta un workflow hijo Express por Batch de 25 Ítems.

La tarea 12.4 añade, sobre esa misma máquina, los dos recursos que cierran el
lazo de arranque y de terminación garantizada (sin cambiar la firma ``__init__``):

- **EventBridge Pipe** (:attr:`WorkflowStack.request_pipe`): consume la
  Cola_Solicitudes (:attr:`IngestStack.requests_queue`) e inicia la máquina con
  ``StartExecution`` usando ``name = analysis_id``. Esa elección es la
  **idempotencia de nivel 1** (criterios 5.1/5.14/8.1): la segunda entrega del
  mismo mensaje por SQS (at-least-once) falla con ``ExecutionAlreadyExists``, que
  Pipes trata como éxito y descarta el mensaje **sin DLQ** y sin tocar el META.
- **Reconcile_Stuck** (:attr:`WorkflowStack.reconcile_stuck`): la Lambda *sweeper*
  (tarea 9.3) con su regla de **EventBridge Scheduler cada 15 min**
  (:attr:`WorkflowStack.reconcile_schedule`, criterio 10.10). Vive aquí —y no en
  ``compute``— porque necesita el **nombre de la state machine** para inspeccionar
  sus ejecuciones con ``DescribeExecution`` (su nombre es el ``analysis_id``), lo
  que evita un ciclo de dependencias ``compute`` ↔ ``workflow``.

Topología de la máquina (diseño 17.1 / 17.2)
--------------------------------------------
Padre Standard (``Distributed Map`` solo existe en Standard; además el Lote puede
durar horas, por encima del techo de 5 min de Express):

    MarkRunning   Task (SDK dynamodb:updateItem). META.status = RUNNING +
                  execution_arn. ConditionExpression: #st = PENDING.
                  Catch ConditionalCheckFailed → Succeed (el Lote ya era
                  terminal, p. ej. expirado por el sweeper: criterio 5.15).
                  Catch States.ALL → MarkFailed.
    ProcessItems  Map modo DISTRIBUTED, hijos EXPRESS. ItemReader de S3,
                  ItemBatcher de 25, MaxConcurrency 4, ToleratedFailurePercentage
                  100, ResultWriter a ``map-results/``. Catch de fallo de lectura
                  de ``inputs/`` → MarkFailed(INPUT_READ_FAILED) (criterio 5.16);
                  cualquier otro States.ALL → Aggregate (el Lote SIEMPRE se agrega
                  para que el cliente reciba un Estado_Terminal honesto).
    Aggregate     Task (lambda:invoke) → aggregate_results. Retry transitorio 3×.
                  Catch States.ALL → MarkFailed(AGGREGATION_FAILED).
    MarkFailed    Task (dynamodb:updateItem, status=FAILED + error_code) →
                  Task (events:putEvents, Evento_Finalizacion) → Fail. Publica el
                  cierre en FAILED antes de terminar (criterios 10.17/10.18).

Hijo Express (dentro del Distributed Map):

    ClassicSentiment  Task → analyze_comprehend (batch 25). Retry transitorio 5×.
                      Catch States.ALL → RecordBatchFailure (fallo de llamada
                      completa, criterio 6.15).
    LlmFanOut         Map INLINE, MaxConcurrency 5: fan-out por Ítem del batch.
    DeepAnalysis      Task → analyze_llm (1 ítem). Retry transitorio 6×. Las
                      condiciones permanentes NO se reintentan (criterio 9.4):
                      van directas al Catch en su primera ocurrencia.
    RecordItemFailure UpdateItem ITEM → FAILED (dentro de LlmFanOut).

Clasificación transitorio/permanente
-------------------------------------
Las Lambdas de procesamiento NO reintentan: propagan el error en su primera
ocurrencia indicando su clase mediante el **nombre de la excepción** (diseño
``backend/.../domain/errors.py``). La Maquina_De_Estados compara ese nombre en
``ErrorEquals``. Solo las condiciones transitorias del criterio 9.16
(:data:`_TRANSIENT_ERRORS`) llevan ``Retry``; las permanentes del criterio 9.17
no tienen ``Retry`` y caen directas al ``Catch`` (criterio 9.4).

Dependencias (diseño 14.4.2): ``storage`` (CMK + bucket), ``data`` (tabla),
``ingest`` (cola, usada en 12.4), ``notifications`` (Bus_Eventos) y ``compute``
(las tres Lambdas de los ``Task``). ``reconcile_stuck`` vive aquí (no en
``compute``) porque necesita el nombre de la state machine, lo que evita un ciclo
de dependencias.
"""

from __future__ import annotations

import json

from aws_cdk import ArnFormat, Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_pipes as pipes
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct
from constructs.backend_function import BackendFunction

from config.environments import EnvConfig
from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.ingest_stack import IngestStack
from stacks.naming import event_bus_name as _event_bus_name
from stacks.notifications_stack import NotificationsStack
from stacks.storage_stack import StorageStack

# --- Nombre de la Maquina_De_Estados (padre Standard) ---
# El nombre es parte del contrato con el resto del Sistema: EventBridge Pipes
# (tarea 12.4) usa ``name = analysis_id`` sobre ESTA máquina para que la segunda
# entrega de un mismo mensaje falle con ``ExecutionAlreadyExists`` (idempotencia
# de nivel 1, criterios 5.1/5.14/8.1), y Reconcile_Stuck (12.4) llama a
# ``DescribeExecution`` sobre ``execution:{nombre}:*`` (criterios 10.11/10.12).
_STATE_MACHINE_SUFFIX = "analysis"

# --- Claves del registro META (diseño 6.2; backend dynamo_repository) ---
# ``pk = TENANT#{tenant_id}#ANALYSIS#{analysis_id}`` y ``sk = "META"``. Las
# transiciones de estado del padre (MarkRunning, MarkFailed) escriben el META. La
# ``pk`` se reconstruye en-estado desde ``tenant_id`` y ``analysis_id`` (que el
# Pipe de la tarea 12.4 inyecta en el input) con ``JsonPath.format``, de modo que
# la máquina no dependa de que el productor del mensaje precalcule la ``pk``.
_SK_META = "META"
_PK_TEMPLATE = "TENANT#{}#ANALYSIS#{}"

# --- Estados del Lote (diseño; glosario de requirements.md) ---
_STATUS_PENDING = "PENDING"
_STATUS_RUNNING = "RUNNING"
_STATUS_FAILED = "FAILED"

# --- error_code que registra MarkFailed según el camino (Catalogo_De_Errores) ---
_ERR_INPUT_READ_FAILED = "INPUT_READ_FAILED"  # criterio 5.16
_ERR_AGGREGATION_FAILED = "AGGREGATION_FAILED"  # criterio 9.11

# --- Prefijo del ResultWriter del Distributed Map (criterio 5.13; diseño 6.5) ---
# El Distributed Map escribe el mapa de resultados bajo ``map-results/`` del
# Bucket_Analisis. CDK le añade la ruta ``map-results/{execution-id}/...``.
_MAP_RESULTS_PREFIX = "map-results"

# --- Dimensionado del batch (criterio 5.4; = límite de batch_detect_sentiment) ---
_MAX_ITEMS_PER_BATCH = 25

# --- Techo de Ítems de un Lote (criterio 5.3) ---
_MAX_ITEMS_PER_BATCH_READ = 1000

# --- Timeouts de los Task (criterios 9.13/9.14/9.15) ---
# Son el techo duro que la máquina clasifica como ``States.Timeout`` (transitorio,
# criterio 9.16). Coinciden con el timeout de la propia Lambda (compute_stack).
_TIMEOUT_COMPREHEND = Duration.seconds(60)  # criterio 9.15
_TIMEOUT_LLM = Duration.seconds(120)  # criterio 9.13
_TIMEOUT_AGGREGATE = Duration.seconds(300)  # criterio 9.14

# --- Timeout global de una ejecución del padre (criterio 5.12) ---
_EXECUTION_TIMEOUT = Duration.seconds(10800)  # 3 h

# --- Condiciones transitorias reintentables (criterio 9.16) ---
# Nombres de error que la Maquina_De_Estados reintenta. Combinan (a) las clases de
# excepción del backend que denotan transitoriedad —``TransientDependencyError`` y
# su subclase ``ThrottledError`` (diseño domain/errors.py)— con (b) los errores de
# plataforma que Step Functions genera por timeout de la tarea (``States.Timeout``)
# y por fallos de servicio/cliente de la invocación Lambda (familia ``Lambda.*``).
# NINGUNA condición permanente del criterio 9.17 aparece aquí: así caen directas al
# Catch en su primera ocurrencia (criterio 9.4).
_TRANSIENT_ERRORS: tuple[str, ...] = (
    "TransientDependencyError",
    "ThrottledError",
    "States.Timeout",
    "Lambda.TooManyRequestsException",
    "Lambda.ServiceException",
    "Lambda.SdkClientException",
)

# --- Parámetros comunes del backoff exponencial con jitter completo (criterio 9.1) ---
_BACKOFF_RATE = 2.0
_JITTER_FULL = sfn.JitterType.FULL

# --- Reintentos por tarea (criterios 9.2/9.3/9.11) ---
_LLM_MAX_ATTEMPTS = 6  # criterio 9.2
_LLM_INTERVAL = Duration.seconds(2)
_LLM_MAX_DELAY = Duration.seconds(60)

_COMPREHEND_MAX_ATTEMPTS = 5  # criterio 9.3
_COMPREHEND_INTERVAL = Duration.seconds(2)
_COMPREHEND_MAX_DELAY = Duration.seconds(30)

_AGGREGATE_MAX_ATTEMPTS = 3  # criterio 9.11
_AGGREGATE_INTERVAL = Duration.seconds(5)
_AGGREGATE_MAX_DELAY = Duration.seconds(30)

# --- Reintentos de las escrituras SDK de DynamoDB/EventBridge del padre ---
# Las integraciones directas con el SDK (MarkRunning, MarkFailed) solo necesitan
# cubrir el throttling del servicio; 3 intentos con 2 s de base bastan (diseño
# 17.2). No es una condición transitoria del criterio 9.16 (que aplica a las
# dependencias de análisis), sino resiliencia de la propia orquestación.
_SDK_MAX_ATTEMPTS = 3
_SDK_INTERVAL = Duration.seconds(2)

# --- Detail-type del Evento_Finalizacion (criterio 10.20; diseño 13.1) ---
_EVENT_SOURCE = "sentiment.pipeline"
_EVENT_DETAIL_TYPE = "analysis.completed"

# --- EventBridge Pipe: Cola_Solicitudes → StartExecution (tarea 12.4) ---
# El Pipe es el fan-out "sin código" del diseño (secciones 2.3 / 10.4): consume
# la Cola_Solicitudes (source) e inicia la Maquina_De_Estados (target) por cada
# mensaje. No lleva DLQ propia (criterio 5.14): el descarte de duplicados por
# ``ExecutionAlreadyExists`` es un ÉXITO para Pipes (borra el mensaje), no un
# fallo que debiera ir a una DLQ.
_PIPE_SUFFIX = "requests-to-sfn"

# Nº de mensajes que el Pipe agrupa por invocación del target. 1: cada mensaje de
# la Cola_Solicitudes es exactamente un Lote (diseño 4.2) y debe producir una
# única ``StartExecution`` con ``name = analysis_id`` (criterio 5.1). Agrupar
# rompería la correspondencia 1 mensaje → 1 ejecución.
_PIPE_BATCH_SIZE = 1

# --- Reconcile_Stuck (sweeper) y su regla de EventBridge Scheduler (tarea 12.4) ---
_RECONCILE_SUFFIX = "reconcile-stuck"
_SCHEDULE_SUFFIX = "reconcile-stuck-15m"

# Ruta de módulo del handler del sweeper, empaquetado en ``backend/src`` (tarea
# 9.3). Debe coincidir con el módulo real ``sentiment/handlers/reconcile_stuck``.
_HANDLER_RECONCILE = "sentiment.handlers.reconcile_stuck.lambda_handler"

# El sweeper es ligero (una Query al GSI2 sparse + relecturas puntuales) pero
# puede recorrer varias decenas de candidatos y publicar eventos; 256 MiB y 120 s
# le dan holgura sin ser el techo de un barrido completo (diseño 10.6, tabla de
# Lambdas sección 20: 256 MB).
_RECONCILE_MEMORY_MB = 256
_RECONCILE_TIMEOUT_S = 120

# Cadencia del barrido: cada 15 minutos (criterio 10.10). Todo Lote atascado o
# terminal-sin-evento se evalúa como máximo 15 min después de quedar en esa
# situación. ``rate(15 minutes)`` es la expresión de EventBridge Scheduler.
_SCHEDULE_RATE = "rate(15 minutes)"

# Ventana de flexibilidad del Scheduler. OFF: el barrido se dispara en el
# instante exacto de cada ventana de 15 min, sin que el servicio lo adelante o
# retrase dentro de una ventana de holgura; así el intervalo máximo entre
# barridos consecutivos no supera los 15 min que exige el criterio 10.10.
_SCHEDULE_FLEXIBLE_WINDOW = scheduler.CfnSchedule.FlexibleTimeWindowProperty(
    mode="OFF"
)


class WorkflowStack(Stack):
    """Stack del dominio de orquestación: la Maquina_De_Estados (tarea 12.3).

    Expone la máquina de estados como atributo público tipado para que la tarea
    12.4 (EventBridge Pipes + ``reconcile_stuck``) y el ``ObservabilityStack``
    (que alarma sobre sus ejecuciones fallidas, criterio 14.8) la referencien sin
    acoplarse a la construcción interna.

    Attributes:
        state_machine: La Maquina_De_Estados padre de tipo Standard
            (:class:`aws_cdk.aws_stepfunctions.StateMachine`).
        request_pipe: El EventBridge Pipe que consume la Cola_Solicitudes e inicia
            la Maquina_De_Estados con ``name = analysis_id``
            (:class:`aws_cdk.aws_pipes.CfnPipe`, tarea 12.4).
        reconcile_stuck: La Lambda ``Reconcile_Stuck`` (sweeper) que cierra los
            Lotes abandonados (:class:`aws_cdk.aws_lambda.IFunction`, tarea 12.4).
        reconcile_schedule: La regla de EventBridge Scheduler que dispara el
            sweeper cada 15 minutos (:class:`aws_cdk.aws_scheduler.CfnSchedule`,
            tarea 12.4).

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
        storage: Stack de almacenamiento del que depende (CMK, bucket, Layer).
        data: Stack de datos del que depende (tabla).
        ingest: Stack de ingesta del que depende (cola de solicitudes, tarea 12.4).
        notifications: Stack de notificación del que depende (Bus_Eventos).
        compute: Stack de procesamiento del que depende (Lambdas de pasos).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        storage: StorageStack,
        data: DataStack,
        ingest: IngestStack,
        notifications: NotificationsStack,
        compute: ComputeStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._config = config
        self._storage = storage
        self._data = data
        self._ingest = ingest
        self._notifications = notifications
        self._compute = compute
        self.add_dependency(storage)
        self.add_dependency(data)
        self.add_dependency(ingest)
        self.add_dependency(notifications)
        self.add_dependency(compute)

        self._prefix = f"{config.project}-{config.env_name}"

        # La Maquina_De_Estados padre (tarea 12.3).
        self.state_machine: sfn.StateMachine = self._build_state_machine()

        # --- Tarea 12.4: Pipe (idempotencia nivel 1) y sweeper ---
        # El Pipe conecta la Cola_Solicitudes con ``StartExecution`` usando
        # ``name = analysis_id`` (criterios 5.1/5.14/8.1), y el sweeper garantiza
        # la terminación de los Lotes abandonados cada 15 min (criterio 10.10).
        self.request_pipe: pipes.CfnPipe = self._build_request_pipe()
        self.reconcile_stuck: lambda_.IFunction = self._build_reconcile_stuck()
        self.reconcile_schedule: scheduler.CfnSchedule = (
            self._build_reconcile_schedule()
        )

    # --- Nombres derivados ----------------------------------------------------

    @staticmethod
    def _meta_pk() -> str:
        """Construye en-estado la ``pk`` del META desde ``tenant_id``/``analysis_id``.

        Reconstruye ``TENANT#{tenant_id}#ANALYSIS#{analysis_id}`` con
        ``JsonPath.format`` para no exigir que el productor del mensaje (Pipe de la
        tarea 12.4) precalcule la ``pk``. El orden de los argumentos respeta el
        esquema de claves del repositorio (diseño 6.2).

        Returns:
            La expresión JSONPath que evalúa a la ``pk`` del registro META.
        """

        return sfn.JsonPath.format(
            _PK_TEMPLATE,
            sfn.JsonPath.string_at("$.tenant_id"),
            sfn.JsonPath.string_at("$.analysis_id"),
        )

    # --- Construcción de la máquina de estados -------------------------------

    def _build_state_machine(self) -> sfn.StateMachine:
        """Ensambla el workflow padre Standard y lo materializa como ``StateMachine``.

        Encadena ``MarkRunning`` → ``ProcessItems`` → ``Aggregate`` → ``Succeed``,
        con ``MarkFailed`` como estado terminal de los caminos de fallo
        (criterios 10.17/10.18). El tipo **Standard** es obligatorio: el
        Distributed Map solo existe en Standard y el Lote puede durar horas, por
        encima del techo de 5 min de Express (diseño 17.3).

        Returns:
            La :class:`aws_cdk.aws_stepfunctions.StateMachine` padre.
        """

        # El estado terminal de fallo se construye primero porque varios estados
        # lo referencian en sus `Catch` (es un sumidero compartido del grafo).
        mark_failed = self._build_mark_failed()
        aggregate = self._build_aggregate(mark_failed)
        process_items = self._build_process_items(aggregate, mark_failed)
        mark_running = self._build_mark_running(process_items, mark_failed)

        definition = sfn.Chain.start(mark_running)

        # Log group explícito cifrado con la CMK (criterio 15.6), igual que el de
        # las Lambdas (``BackendFunction``): el grupo implícito no estaría cifrado.
        log_group = logs.LogGroup(
            self,
            "StateMachineLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=self._storage.key,
        )

        return sfn.StateMachine(
            self,
            "SentimentAnalysis",
            state_machine_name=f"{self._prefix}-{_STATE_MACHINE_SUFFIX}",
            # Standard: Distributed Map + ejecuciones de horas (diseño 17.3).
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            # Techo duro de una ejecución; el Lote que lo supere lo cierra el
            # sweeper (criterio 5.12).
            timeout=_EXECUTION_TIMEOUT,
            tracing_enabled=True,  # trazabilidad distribuida del Lote
            logs=sfn.LogOptions(
                destination=log_group,
                level=sfn.LogLevel.ALL,
                # No se incluyen datos de ejecución en los logs: el estado propaga
                # identificadores, nunca el texto de `respuesta`/`explicacion`
                # (criterios 5.11/15.3), pero mantenerlo en False es defensa en
                # profundidad contra fugas de PII en los logs del servicio.
                include_execution_data=False,
            ),
        )

    def _build_mark_running(
        self, on_success: sfn.IChainable, on_failure: sfn.State
    ) -> sfn.TaskStateBase:
        """Construye ``MarkRunning``: META.status PENDING → RUNNING (criterio 5.2).

        Integración directa con el SDK de DynamoDB (sin Lambda): escribe
        ``status = RUNNING`` y el ``execution_arn`` de esta ejecución, condicionado
        a que el META esté en ``PENDING``. Si la condición falla, el Lote ya estaba
        en un Estado_Terminal (p. ej. expirado por el sweeper) y la ejecución
        termina en ``Succeed`` sin procesar nada (criterio 5.15). Cualquier otro
        fallo va a ``MarkFailed``.

        Args:
            on_success: Estado al que continuar (``ProcessItems``).
            on_failure: Estado terminal de fallo (``MarkFailed``).

        Returns:
            El estado ``MarkRunning`` ya encadenado con sus ``Retry``/``Catch``.
        """

        mark_running = tasks.DynamoUpdateItem(
            self,
            "MarkRunning",
            table=self._data.table,
            key={
                "pk": tasks.DynamoAttributeValue.from_string(self._meta_pk()),
                "sk": tasks.DynamoAttributeValue.from_string(_SK_META),
            },
            # Idempotencia de nivel 1 / criterio 5.15: solo transiciona si el META
            # sigue en PENDING. Si otro actor (sweeper) ya lo cerró, la condición
            # falla y el Catch de ConditionalCheckFailed lleva a Succeed.
            condition_expression="#st = :pending",
            update_expression=(
                "SET #st = :running, #ea = :exec, #ua = :now"
            ),
            expression_attribute_names={
                "#st": "status",
                "#ea": "execution_arn",
                "#ua": "updated_at",
            },
            expression_attribute_values={
                ":pending": tasks.DynamoAttributeValue.from_string(_STATUS_PENDING),
                ":running": tasks.DynamoAttributeValue.from_string(_STATUS_RUNNING),
                ":exec": tasks.DynamoAttributeValue.from_string(
                    sfn.JsonPath.string_at("$$.Execution.Id")
                ),
                ":now": tasks.DynamoAttributeValue.from_string(
                    sfn.JsonPath.string_at("$$.State.EnteredTime")
                ),
            },
            # No contaminar el estado del Lote con el resultado del UpdateItem.
            result_path=sfn.JsonPath.DISCARD,
        )

        self._add_sdk_retry(mark_running)

        # El Lote ya era terminal (criterio 5.15): fin satisfactorio sin procesar.
        mark_running.add_catch(
            sfn.Succeed(self, "AlreadyTerminal"),
            errors=["DynamoDB.ConditionalCheckFailedException"],
            result_path=sfn.JsonPath.DISCARD,
        )
        # Cualquier otro fallo del arranque → cierre en FAILED.
        mark_running.add_catch(
            on_failure,
            errors=[sfn.Errors.ALL],
            result_path="$.error",
        )

        mark_running.next(on_success)
        return mark_running

    def _build_process_items(
        self, on_done: sfn.State, on_input_failure: sfn.State
    ) -> sfn.DistributedMap:
        """Construye ``ProcessItems``: el Distributed Map que itera los Ítems.

        Lee el array de Ítems de ``inputs/`` con ``ItemReader`` (criterio 5.3),
        los agrupa en Batches de 25 con ``ItemBatcher`` (criterio 5.4), limita a 4
        las ejecuciones hijas en vuelo (``MaxConcurrency``, criterio 5.5), tolera
        el 100% de fallos para que un Ítem envenenado nunca mate el Lote
        (``ToleratedFailurePercentage``, criterio 5.10) y persiste el mapa de
        resultados en ``map-results/`` (``ResultWriter``, criterio 5.13). Cada hijo
        es un workflow Express (barato y rápido).

        El ``Catch`` distingue dos caminos (diseño 17.2):

        - Fallo de lectura de ``inputs/`` (``States.ItemReaderFailed``): no se
          inicia ninguna ejecución hija; el Lote cierra en ``MarkFailed`` con
          ``INPUT_READ_FAILED`` (criterio 5.16).
        - Cualquier otro ``States.ALL``: el Lote **igualmente** se agrega, porque
          el cliente debe recibir un Estado_Terminal honesto con el detalle de lo
          ocurrido (criterio 5.9); va a ``Aggregate``, no a ``MarkFailed``.

        Args:
            on_done: Estado de agregación (``Aggregate``), siguiente natural y
                destino del ``Catch`` genérico.
            on_input_failure: Estado terminal de fallo (``MarkFailed``) para el
                fallo de lectura del input.

        Returns:
            El estado ``ProcessItems`` ya configurado y encadenado.
        """

        process_items = sfn.DistributedMap(
            self,
            "ProcessItems",
            # Hijos Express: cortos y baratos; su at-least-once es seguro porque la
            # idempotencia por Ítem está implementada (diseño 17.3). El tipo de
            # ejecución del hijo se fija aquí (CDK ignora `executionType` dentro de
            # `item_processor` para un DistributedMap, ver aviso de síntesis).
            map_execution_type=sfn.StateMachineType.EXPRESS,
            # Lee el array JSON directamente de S3, sin cargar nada en memoria de
            # una Lambda (criterio 5.3). La clave es dinámica por ejecución.
            item_reader=sfn.S3JsonItemReader(
                bucket=self._storage.bucket,
                key=sfn.JsonPath.string_at("$.input_s3_key"),
                max_items=_MAX_ITEMS_PER_BATCH_READ,
            ),
            # Inyecta el contexto del Lote en cada Ítem (sin texto de respuesta:
            # el propio Ítem ya lo trae del input; se propaga el índice como base
            # determinista del item_id, criterios 5.11/8.6).
            item_selector={
                "analysis_id": sfn.JsonPath.string_at("$.analysis_id"),
                "tenant_id": sfn.JsonPath.string_at("$.tenant_id"),
                "correlation_id": sfn.JsonPath.string_at("$.correlation_id"),
                "item": sfn.JsonPath.string_at("$$.Map.Item.Value"),
                "index": sfn.JsonPath.string_at("$$.Map.Item.Index"),
            },
            item_batcher=sfn.ItemBatcher(
                max_items_per_batch=_MAX_ITEMS_PER_BATCH,  # = límite de Comprehend
                batch_input={
                    "analysis_id": sfn.JsonPath.string_at("$.analysis_id"),
                    "tenant_id": sfn.JsonPath.string_at("$.tenant_id"),
                },
            ),
            # Dial anti-throttle de Bedrock: nº de hijos en vuelo (criterio 5.5),
            # calibrable por entorno (D4 / criterio 18.2).
            max_concurrency=self._config.distributed_map_max_concurrency,
            # Un Ítem envenenado NUNCA detiene el Lote: la señal de éxito son los
            # contadores en DynamoDB, no el resultado del Map (criterio 5.10).
            tolerated_failure_percentage=100,
            # Persiste el mapa de resultados en map-results/ (criterio 5.13); es el
            # artefacto que aggregate_results puede usar para reconciliar. Se usa
            # el `ResultWriter` clásico (y no `ResultWriterV2`) deliberadamente:
            # V2 solo renderiza con el feature flag app-wide
            # `@aws-cdk/aws-stepfunctions:useDistributedMapResultWriterV2`, que
            # alteraría la síntesis de los otros ocho stacks; el clásico produce el
            # ASL que exige el diseño 17.2 (`s3:putObject` + `Prefix: map-results`)
            # sin ese efecto colateral. La deprecación es un aviso, no un error.
            result_writer=sfn.ResultWriter(
                bucket=self._storage.bucket,
                prefix=_MAP_RESULTS_PREFIX,
            ),
            # No propagar el resultado completo del Map al estado (podría ser
            # enorme): solo el puntero. Evita inflar el estado (criterio 5.11).
            result_selector={
                "processed": sfn.JsonPath.string_at("$.ResultWriterDetails"),
            },
            result_path="$.map_result",
        )

        process_items.item_processor(
            self._build_child_workflow(),
            mode=sfn.ProcessorMode.DISTRIBUTED,
            # Requerido por la validación de CDK aunque `map_execution_type` ya lo
            # fije en el constructor; la plantilla deduplica a un único EXPRESS.
            execution_type=sfn.ProcessorType.EXPRESS,
        )

        # Fallo al leer el input: no se inició ningún hijo → FAILED (criterio 5.16).
        process_items.add_catch(
            on_input_failure,
            errors=["States.ItemReaderFailed"],
            result_path="$.error",
        )
        # Cualquier otro fallo del Map: el Lote SE AGREGA igualmente (criterio 5.9).
        process_items.add_catch(
            on_done,
            errors=[sfn.Errors.ALL],
            result_path="$.error",
        )

        process_items.next(on_done)
        return process_items

    def _build_child_workflow(self) -> sfn.IChainable:
        """Construye el workflow hijo Express que procesa un Batch de Ítems.

        ``ClassicSentiment`` (analyze_comprehend, batch de 25) → ``LlmFanOut`` (Map
        inline que hace fan-out por Ítem hacia ``DeepAnalysis``/analyze_llm). Los
        ``Retry`` solo cubren condiciones transitorias (criterios 9.2/9.3); las
        permanentes caen directas al ``Catch`` (criterio 9.4). El fallo de un Ítem
        o de un batch no detiene el resto del Lote (criterios 9.6/6.15).

        Returns:
            La cadena de estados del workflow hijo.
        """

        # --- DeepAnalysis: 1 ítem por invocación (analyze_llm) ---
        deep_analysis = tasks.LambdaInvoke(
            self,
            "DeepAnalysis",
            lambda_function=self._compute.analyze_llm,
            payload=sfn.TaskInput.from_object(
                {
                    "analysis_id": sfn.JsonPath.string_at("$.analysis_id"),
                    "tenant_id": sfn.JsonPath.string_at("$.tenant_id"),
                    "correlation_id": sfn.JsonPath.string_at("$.correlation_id"),
                    "index": sfn.JsonPath.string_at("$.index"),
                    "item": sfn.JsonPath.string_at("$.item"),
                }
            ),
            payload_response_only=True,
            # Desactiva el Retry que LambdaInvoke inyecta por defecto: la política
            # de reintentos debe ser EXCLUSIVAMENTE la declarativa de abajo, solo
            # para condiciones transitorias (criterios 9.2/9.4). El retry por
            # defecto reintentaría errores de servicio con otros parámetros.
            retry_on_service_exceptions=False,
            task_timeout=sfn.Timeout.duration(_TIMEOUT_LLM),  # criterio 9.13
        )
        # Reintenta SOLO lo transitorio (criterio 9.2): 6 intentos, base 2 s,
        # backoff 2.0, jitter completo, techo 60 s. Lo permanente NO está aquí, así
        # que su primera ocurrencia cae al Catch (criterio 9.4).
        deep_analysis.add_retry(
            errors=list(_TRANSIENT_ERRORS),
            max_attempts=_LLM_MAX_ATTEMPTS,
            interval=_LLM_INTERVAL,
            backoff_rate=_BACKOFF_RATE,
            max_delay=_LLM_MAX_DELAY,
            jitter_strategy=_JITTER_FULL,
        )
        deep_analysis.add_catch(
            self._build_record_item_failure(),
            errors=[sfn.Errors.ALL],
            result_path="$.error",
        )

        llm_fan_out = sfn.Map(
            self,
            "LlmFanOut",
            # Ítems de un batch en vuelo a la vez dentro de cada hijo (criterio
            # 5.6). 4 (padre) × 5 = 20 invocaciones concurrentes a Bedrock por Lote
            # (criterio 18.4); calibrable (D4).
            max_concurrency=self._config.llm_fanout_max_concurrency,
            items_path="$.items",
        )
        llm_fan_out.item_processor(deep_analysis)

        # --- ClassicSentiment: batch de 25 (analyze_comprehend) ---
        classic_sentiment = tasks.LambdaInvoke(
            self,
            "ClassicSentiment",
            lambda_function=self._compute.analyze_comprehend,
            payload=sfn.TaskInput.from_object(
                {
                    "analysis_id": sfn.JsonPath.string_at("$.BatchInput.analysis_id"),
                    "tenant_id": sfn.JsonPath.string_at("$.BatchInput.tenant_id"),
                    "items": sfn.JsonPath.string_at("$.Items"),
                }
            ),
            payload_response_only=True,
            # Solo la política declarativa de abajo gobierna los reintentos
            # (criterios 9.3/9.4); se desactiva el Retry por defecto de LambdaInvoke.
            retry_on_service_exceptions=False,
            task_timeout=sfn.Timeout.duration(_TIMEOUT_COMPREHEND),  # criterio 9.15
        )
        # Reintenta SOLO lo transitorio (criterio 9.3): 5 intentos, base 2 s,
        # backoff 2.0, jitter completo, techo 30 s. Los fallos por Ítem
        # (COMPREHEND_ITEM_FAILED/RESULT_MISSING) NO son errores de la tarea: los
        # persiste la propia Lambda y el batch continúa.
        classic_sentiment.add_retry(
            errors=list(_TRANSIENT_ERRORS),
            max_attempts=_COMPREHEND_MAX_ATTEMPTS,
            interval=_COMPREHEND_INTERVAL,
            backoff_rate=_BACKOFF_RATE,
            max_delay=_COMPREHEND_MAX_DELAY,
            jitter_strategy=_JITTER_FULL,
        )
        # Solo cuando falla la llamada completa a Comprehend (criterio 6.15).
        classic_sentiment.add_catch(
            self._build_record_batch_failure(),
            errors=[sfn.Errors.ALL],
            result_path="$.error",
        )

        classic_sentiment.next(llm_fan_out)
        return classic_sentiment

    def _build_record_item_failure(self) -> sfn.State:
        """Construye ``RecordItemFailure``: marca un Ítem como ``FAILED``.

        Terminal del ``Catch`` de ``DeepAnalysis`` dentro de ``LlmFanOut`` (diseño
        17.2). La escritura del ``error_code`` por clase de error y el envío a la
        DLQ_Items (criterios 9.4/9.5/9.7) los implementará con detalle una tarea
        posterior; aquí se deja un ``Pass`` nombrado para que el fallo de un Ítem
        **nunca** detenga el procesamiento del resto del Lote (criterio 9.6):
        ``ToleratedFailurePercentage: 100`` tolera el fallo y el Lote se agrega
        igualmente, momento en el que ``aggregate_results`` recuenta la realidad
        desde DynamoDB (fuente de verdad) y cierra el Ítem huérfano como
        ``ORPHANED_AFTER_MAP`` si fuera necesario (criterio 10.2).

        Returns:
            El estado terminal ``RecordItemFailure``.
        """

        # El fallo del Ítem ya está capturado por el Catch; absorberlo aquí evita
        # que propague y mate la ejecución hija, de modo que los demás Ítems del
        # batch siguen (criterio 9.6). El recuento terminal lo fija el agregador.
        return sfn.Pass(self, "RecordItemFailure")

    def _build_record_batch_failure(self) -> sfn.State:
        """Construye ``RecordBatchFailure``: absorbe el fallo de un batch completo.

        Terminal del ``Catch`` de ``ClassicSentiment`` (criterio 6.15). Igual que
        ``RecordItemFailure``, absorbe el fallo para no detener el resto del Lote
        (criterio 9.6); el cierre real de los Ítems del batch lo reconcilia
        ``aggregate_results`` desde DynamoDB (criterio 10.2). El marcado explícito
        de los 25 Ítems como ``FAILED`` lo detallará una tarea posterior.

        Returns:
            El estado terminal ``RecordBatchFailure``.
        """

        return sfn.Pass(self, "RecordBatchFailure")

    def _build_aggregate(self, on_failure: sfn.State) -> sfn.TaskStateBase:
        """Construye ``Aggregate``: consolidación y cierre del Lote (criterio 5.9).

        Invoca ``aggregate_results`` exactamente una vez, con el ``analysis_id`` y
        el ``tenant_id`` del Lote, cuando el Distributed Map termina (con
        independencia de cuántos Batches fallaran). ``Retry`` cubre solo lo
        transitorio: 3 intentos, base 5 s, backoff 2.0, jitter completo, techo 30 s
        (criterio 9.11). Si agota los reintentos, el ``Catch`` lleva a
        ``MarkFailed`` con ``AGGREGATION_FAILED``.

        Args:
            on_failure: Estado terminal de fallo (``MarkFailed``).

        Returns:
            El estado ``Aggregate`` ya configurado.
        """

        aggregate = tasks.LambdaInvoke(
            self,
            "Aggregate",
            lambda_function=self._compute.aggregate_results,
            payload=sfn.TaskInput.from_object(
                {
                    "analysis_id": sfn.JsonPath.string_at("$.analysis_id"),
                    "tenant_id": sfn.JsonPath.string_at("$.tenant_id"),
                    "map_result": sfn.JsonPath.string_at("$.map_result"),
                }
            ),
            payload_response_only=True,
            # Solo la política declarativa de abajo gobierna los reintentos
            # (criterio 9.11); se desactiva el Retry por defecto de LambdaInvoke.
            retry_on_service_exceptions=False,
            task_timeout=sfn.Timeout.duration(_TIMEOUT_AGGREGATE),  # criterio 9.14
        )
        aggregate.add_retry(
            errors=list(_TRANSIENT_ERRORS),
            max_attempts=_AGGREGATE_MAX_ATTEMPTS,
            interval=_AGGREGATE_INTERVAL,
            backoff_rate=_BACKOFF_RATE,
            max_delay=_AGGREGATE_MAX_DELAY,
            jitter_strategy=_JITTER_FULL,
        )
        aggregate.add_catch(
            on_failure,
            errors=[sfn.Errors.ALL],
            result_path="$.error",
        )
        aggregate.next(sfn.Succeed(self, "BatchCompleted"))
        return aggregate

    def _build_mark_failed(self) -> sfn.State:
        """Construye ``MarkFailed``: cierra el Lote en ``FAILED`` y notifica.

        Estado terminal de los caminos de fallo del padre (``MarkRunning`` y
        ``Aggregate``, y la lectura de input de ``ProcessItems``). Escribe
        ``status = FAILED`` en el META (condicionado a que no sea ya terminal) y
        **publica el Evento_Finalizacion** antes de terminar la ejecución, para
        que todo Lote que la máquina cierra en ``FAILED`` sin pasar por el
        agregador llegue de todos modos al cliente (criterios 10.17/10.18). El
        ``event_id`` determinista ``{analysis_id}:FAILED`` permite al consumidor
        descartar duplicados (criterio 10.20).

        El ``error_code`` concreto (``INPUT_READ_FAILED`` o ``AGGREGATION_FAILED``)
        lo determina el camino que llegó aquí; se toma de ``$.error`` cuando está
        disponible y, en su ausencia, por defecto ``AGGREGATION_FAILED`` (ambos
        caminos de ``Aggregate``/arranque convergen en el cierre genérico). El
        detalle exacto por camino se refina en la tarea de agregación.

        Returns:
            La cadena ``MarkFailed`` → ``PublishFailure`` → ``Fail``.
        """

        update_meta = tasks.DynamoUpdateItem(
            self,
            "MarkFailed",
            table=self._data.table,
            key={
                "pk": tasks.DynamoAttributeValue.from_string(self._meta_pk()),
                "sk": tasks.DynamoAttributeValue.from_string(_SK_META),
            },
            # No sobrescribir un Lote que ya cerró otro actor (p. ej. el sweeper):
            # solo transiciona desde un estado no terminal (diseño 17.2).
            condition_expression="#st IN (:pending, :running)",
            update_expression="SET #st = :failed, #ec = :err, #ua = :now",
            expression_attribute_names={
                "#st": "status",
                "#ec": "error_code",
                "#ua": "updated_at",
            },
            expression_attribute_values={
                ":pending": tasks.DynamoAttributeValue.from_string(_STATUS_PENDING),
                ":running": tasks.DynamoAttributeValue.from_string(_STATUS_RUNNING),
                ":failed": tasks.DynamoAttributeValue.from_string(_STATUS_FAILED),
                ":err": tasks.DynamoAttributeValue.from_string(_ERR_AGGREGATION_FAILED),
                ":now": tasks.DynamoAttributeValue.from_string(
                    sfn.JsonPath.string_at("$$.State.EnteredTime")
                ),
            },
            result_path=sfn.JsonPath.DISCARD,
        )
        self._add_sdk_retry(update_meta)

        publish = tasks.EventBridgePutEvents(
            self,
            "PublishFailure",
            entries=[
                tasks.EventBridgePutEventsEntry(
                    # Referencia directa al Bus_Eventos que publica el
                    # NotificationsStack (tarea 13.3): CDK concede
                    # ``events:PutEvents`` sobre el ARN concreto automáticamente,
                    # sin acoplar por nombre (criterios 10.17/10.18/15.11).
                    event_bus=self._notifications.event_bus,
                    source=_EVENT_SOURCE,
                    detail_type=_EVENT_DETAIL_TYPE,
                    # Entrega al menos una vez; event_id determinista para que el
                    # consumidor descarte duplicados (criterio 10.20).
                    detail=sfn.TaskInput.from_object(
                        {
                            "event_id": sfn.JsonPath.format(
                                "{}:{}",
                                sfn.JsonPath.string_at("$.analysis_id"),
                                _STATUS_FAILED,
                            ),
                            "analysis_id": sfn.JsonPath.string_at("$.analysis_id"),
                            "tenant_id": sfn.JsonPath.string_at("$.tenant_id"),
                            "status": _STATUS_FAILED,
                            "correlation_id": sfn.JsonPath.string_at(
                                "$.correlation_id"
                            ),
                        }
                    ),
                )
            ],
            result_path=sfn.JsonPath.DISCARD,
        )
        self._add_sdk_retry(publish)

        # Termina la ejecución en fallo tras notificar (criterio 10.18): la alarma
        # de ejecución fallida (criterio 14.8) se dispara sobre este Fail.
        fail = sfn.Fail(
            self,
            "AnalysisFailed",
            error=_STATUS_FAILED,
            cause="El Lote cerró en FAILED (ver error_code en el META).",
        )

        update_meta.next(publish)
        publish.next(fail)
        return update_meta

    # --- Helpers --------------------------------------------------------------

    def _add_sdk_retry(self, task: sfn.TaskStateBase) -> None:
        """Añade a una integración SDK del padre un ``Retry`` de throttling.

        Las escrituras directas con el SDK (``MarkRunning``/``MarkFailed`` sobre
        DynamoDB, ``PublishFailure`` sobre EventBridge) solo necesitan resiliencia
        ante throttling del servicio: 3 intentos, base 2 s, backoff 2.0, jitter
        completo (diseño 17.2). No es la política de las dependencias de análisis
        (criterios 9.2/9.3), sino robustez de la propia orquestación.

        Args:
            task: El estado ``Task`` de integración SDK al que añadir el ``Retry``.
        """

        task.add_retry(
            errors=[sfn.Errors.ALL],
            max_attempts=_SDK_MAX_ATTEMPTS,
            interval=_SDK_INTERVAL,
            backoff_rate=_BACKOFF_RATE,
            jitter_strategy=_JITTER_FULL,
        )

    # --- Tarea 12.4: EventBridge Pipe (Cola_Solicitudes → StartExecution) -----

    def _build_request_pipe(self) -> pipes.CfnPipe:
        """Construye el Pipe SQS → ``StartExecution`` con idempotencia de nivel 1.

        El Pipe es el fan-out **sin código** del diseño (secciones 2.3 / 10.4): su
        *source* es la Cola_Solicitudes (:attr:`IngestStack.requests_queue`) y su
        *target* es la Maquina_De_Estados. Por cada mensaje inicia **exactamente
        una** ejecución (criterio 5.1) cuyo **nombre de ejecución es el
        ``analysis_id``** del mensaje. Esa elección es la idempotencia de nivel 1
        (criterios 5.14/8.1): si SQS reentrega el mismo mensaje (es at-least-once),
        la segunda ``StartExecution`` falla con ``ExecutionAlreadyExists``, que
        Pipes trata como entrega exitosa y **borra** el mensaje de la cola **sin
        trasladarlo a la DLQ** y sin tocar el META (criterio 5.14). Por eso el Pipe
        **no** define una DLQ propia.

        El *input transformer* del target extrae del cuerpo del mensaje (JSON
        producido por ``sqs_publisher.publish_analysis_queued``) solo los punteros
        e identificadores que la máquina propaga —``analysis_id``, ``tenant_id``,
        ``correlation_id`` y ``input_s3_key``—, nunca el texto de ``respuesta``
        (criterio 5.11). El cuerpo llega como cadena en ``$.body``; Pipes lo
        resuelve con la sintaxis de ruta JSON ``<$.body.campo>``.

        El rol del Pipe recibe **mínimo privilegio** sobre ARN concretos
        (criterios 15.10/15.11): leer/borrar de la cola concreta y arrancar la
        state machine concreta; sin ``Resource: "*"``.

        Returns:
            El :class:`aws_cdk.aws_pipes.CfnPipe` ya configurado.
        """

        role = self._build_pipe_role()

        return pipes.CfnPipe(
            self,
            "RequestsToSfnPipe",
            name=f"{self._prefix}-{_PIPE_SUFFIX}",
            role_arn=role.role_arn,
            source=self._ingest.requests_queue.queue_arn,
            source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
                sqs_queue_parameters=(
                    pipes.CfnPipe.PipeSourceSqsQueueParametersProperty(
                        # 1 mensaje = 1 Lote = 1 StartExecution (criterio 5.1):
                        # agrupar rompería la correspondencia y la idempotencia.
                        batch_size=_PIPE_BATCH_SIZE,
                    )
                ),
            ),
            target=self.state_machine.state_machine_arn,
            target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
                step_function_state_machine_parameters=(
                    pipes.CfnPipe.PipeTargetStateMachineParametersProperty(
                        # FIRE_AND_FORGET: Standard no admite arranque síncrono
                        # desde Pipes; el Lote dura hasta horas (diseño 10.4).
                        invocation_type="FIRE_AND_FORGET",
                    )
                ),
                # El nombre de ejecución ES el analysis_id (idempotencia nivel 1,
                # criterios 5.1/5.14/8.1). Se fija vía la clave reservada ``Name``
                # del InputTemplate del target de Step Functions, que CloudFormation
                # mapea al parámetro ``Name`` de ``StartExecution``.
                input_template=self._pipe_input_template(),
            ),
        )

    @staticmethod
    def _pipe_input_template() -> str:
        """Construye el ``InputTemplate`` del target del Pipe (JSON con rutas).

        El cuerpo del mensaje de la Cola_Solicitudes llega como cadena en
        ``$.body``; Pipes sustituye cada marcador ``<$.body.campo>`` por el valor
        correspondiente antes de invocar ``StartExecution``. Se emiten:

        - ``Name``: clave reservada que fija el **nombre de ejecución** =
          ``analysis_id`` (idempotencia de nivel 1, criterios 5.1/5.14/8.1).
        - ``Input``: el estado inicial que la Maquina_De_Estados propaga
          —``analysis_id``, ``tenant_id``, ``correlation_id``, ``input_s3_key``—,
          sin el texto de ``respuesta`` ni de ``explicacion`` (criterio 5.11).

        La plantilla se serializa con :func:`json.dumps` para garantizar un JSON
        bien formado y comillas consistentes; los marcadores ``<...>`` quedan como
        literales de cadena que Pipes resuelve en tiempo de ejecución.

        Returns:
            El ``InputTemplate`` como cadena JSON.
        """

        return json.dumps(
            {
                # Nombre de ejecución = analysis_id → ExecutionAlreadyExists en la
                # 2ª entrega (idempotencia nivel 1, criterios 5.1/5.14/8.1).
                "Name": "<$.body.analysis_id>",
                "Input": {
                    "analysis_id": "<$.body.analysis_id>",
                    "tenant_id": "<$.body.tenant_id>",
                    "correlation_id": "<$.body.correlation_id>",
                    # Puntero al payload en S3 que leerá el ItemReader (criterio
                    # 5.3); nunca el texto de las respuestas (criterio 5.11).
                    "input_s3_key": "<$.body.input_s3_key>",
                },
            },
            separators=(",", ":"),
        )

    def _build_pipe_role(self) -> iam.Role:
        """Crea el rol de ejecución del Pipe con permisos de mínimo privilegio.

        El rol lo asume el servicio ``pipes.amazonaws.com`` y concede **solo**
        (criterios 15.10/15.11):

        - Sobre la **Cola_Solicitudes concreta**: ``sqs:ReceiveMessage``,
          ``sqs:DeleteMessage`` y ``sqs:GetQueueAttributes`` —lo que Pipes necesita
          para sondear y borrar el mensaje tras un arranque exitoso (incluido el
          caso ``ExecutionAlreadyExists``)—. Como la cola está cifrada con la CMK,
          se añade ``kms:Decrypt`` acotado a esa clave para poder leer el cuerpo.
        - Sobre la **Maquina_De_Estados concreta**: ``states:StartExecution``.

        Ningún permiso usa ``Resource: "*"``.

        Returns:
            El :class:`aws_cdk.aws_iam.Role` del Pipe.
        """

        role = iam.Role(
            self,
            "RequestPipeRole",
            assumed_by=iam.ServicePrincipal("pipes.amazonaws.com"),
            description=(
                "Rol del EventBridge Pipe Cola_Solicitudes → StartExecution "
                "(mínimo privilegio; ARN concretos)."
            ),
        )

        # Consumo del source: Pipes sondea y borra el mensaje tras el arranque
        # (criterio 5.14). ``grant_consume_messages`` concede Receive/Delete/
        # GetQueueAttributes y, al estar la cola cifrada con la CMK, el
        # ``kms:Decrypt`` necesario sobre esa clave concreta.
        self._ingest.requests_queue.grant_consume_messages(role)

        # Arranque del target: solo StartExecution sobre la máquina concreta.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="StartSentimentExecution",
                effect=iam.Effect.ALLOW,
                actions=["states:StartExecution"],
                resources=[self.state_machine.state_machine_arn],
            )
        )

        return role

    # --- Tarea 12.4: Reconcile_Stuck (sweeper) y su Scheduler -----------------

    def _build_reconcile_stuck(self) -> lambda_.IFunction:
        """Construye la Lambda ``Reconcile_Stuck`` con sus permisos de mínimo privilegio.

        El sweeper vive en **este** stack (no en ``compute``) porque necesita el
        **nombre de la Maquina_De_Estados** para construir el ARN determinista de
        las ejecuciones que inspecciona con ``DescribeExecution`` —el nombre de la
        ejecución es el ``analysis_id`` (diseño 10.6)—; ubicarlo aquí evita un
        ciclo de dependencias ``compute`` ↔ ``workflow``. Se construye con el mismo
        molde que las Lambdas de ``compute`` (:class:`BackendFunction`: ``arm64``,
        rol propio, log group cifrado 30 días, X-Ray, Layer y CMK de ``storage``).

        Permisos de mínimo privilegio (criterios 15.10/15.11), todos sobre ARN
        concretos:

        - ``states:DescribeExecution`` / ``states:ListExecutions`` acotado a
          ``execution:{nombre_sm}:*`` de la máquina concreta (diseño 10.6): el
          sweeper consulta el estado de la ejecución de cada candidato por su
          ``analysis_id``.
        - Lectura/escritura DynamoDB sobre la tabla y sus índices (incluye el GSI2
          sparse que lista los candidatos ``SWEEP``) para cerrar los Lotes
          atascados como ``EXPIRED`` (criterios 10.11/10.14/8.11).
        - ``events:PutEvents`` sobre el Bus_Eventos concreto para publicar el
          Evento_Finalizacion de los Lotes que cierra o que estaban terminales sin
          evento (criterios 10.14/10.19).

        Returns:
            La función ``reconcile_stuck`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        backend = BackendFunction(
            self,
            "ReconcileStuck",
            handler=_HANDLER_RECONCILE,
            layer=self._storage.shared_layer,
            key=self._storage.key,
            memory_mb=_RECONCILE_MEMORY_MB,
            timeout_seconds=_RECONCILE_TIMEOUT_S,
            reserved_concurrency=None,
            environment={
                "TABLE_NAME": self._data.table.table_name,
                "CONFIG_ENV": self._config.env_name,
                "EVENT_BUS_NAME": _event_bus_name(self._config),
                # Nombre de la state machine: el sweeper deriva de él el ARN de las
                # ejecuciones a inspeccionar (su nombre es el analysis_id).
                "STATE_MACHINE_NAME": self.state_machine.state_machine_name,
            },
        )

        self._grant_describe_executions(backend.role)
        self._grant_reconcile_table(backend.function)
        self._grant_reconcile_event_bus(backend.role)

        return backend.function

    def _grant_describe_executions(self, role: iam.IRole) -> None:
        """Concede la inspección de ejecuciones de la máquina concreta.

        ``states:DescribeExecution`` / ``states:ListExecutions`` acotado al ARN de
        ejecución ``...:execution:{nombre_sm}:*`` de **esta** máquina (diseño 10.6):
        el ``*`` cubre cualquier ``analysis_id`` como nombre de ejecución, pero no
        otras máquinas de estado. Nunca ``Resource: "*"`` (criterio 15.11).

        Args:
            role: Rol de ejecución dedicado de ``reconcile_stuck``.
        """

        # ARN de ejecución: ``arn:aws:states:{region}:{account}:execution:{sm}:*``.
        execution_arn = self.format_arn(
            service="states",
            resource="execution",
            resource_name=f"{self.state_machine.state_machine_name}:*",
            # El ARN de ejecución usa ``:`` como separador entre el tipo de
            # recurso (``execution``) y el nombre, igual que ``state-machine``.
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="DescribeSentimentExecutions",
                effect=iam.Effect.ALLOW,
                actions=[
                    "states:DescribeExecution",
                    "states:ListExecutions",
                ],
                resources=[execution_arn],
            )
        )

    def _grant_reconcile_table(self, grantee: lambda_.IFunction) -> None:
        """Concede al sweeper la lectura/escritura de DynamoDB para cerrar Lotes.

        El sweeper lista candidatos por el GSI2 sparse (``Query``), relee cada
        registro por ``pk`` (``GetItem``/``Query``) y cierra los Lotes atascados
        como ``EXPIRED`` reconciliando contadores (``UpdateItem`` /
        ``TransactWriteItems`` → ``PutItem``/``ConditionCheckItem``), sobre la
        tabla **y sus índices**, nunca sobre un comodín de tabla (criterio 15.11).
        La concesión ``grant`` añade el ``kms:Decrypt``/``Encrypt`` sobre la CMK
        que cifra la tabla (criterio 15.6).

        Args:
            grantee: La Lambda ``reconcile_stuck`` que recibe el permiso.
        """

        actions = (
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:UpdateItem",
            "dynamodb:PutItem",
            "dynamodb:ConditionCheckItem",
        )
        self._data.table.grant(grantee, *actions)
        # El GSI2 (candidatos del sweeper) y el GSI1 no están cubiertos por el ARN
        # de la tabla: se concede Query/GetItem sobre ``.../index/*``.
        grantee.grant_principal.add_to_principal_policy(
            iam.PolicyStatement(
                sid="SweeperIndexAccess",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:Query", "dynamodb:GetItem"],
                resources=[f"{self._data.table.table_arn}/index/*"],
            )
        )

    def _grant_reconcile_event_bus(self, role: iam.IRole) -> None:
        """Concede ``events:PutEvents`` sobre el Bus_Eventos concreto.

        El sweeper publica el Evento_Finalizacion de los Lotes que cierra como
        ``EXPIRED`` (criterio 10.14) y de los que ya eran terminales sin evento
        (criterio 10.19). Se usa la referencia directa al bus que publica el
        NotificationsStack, de modo que CDK conceda el permiso sobre su ARN
        concreto, nunca ``"*"`` (criterio 15.11).

        Args:
            role: Rol de ejecución dedicado de ``reconcile_stuck``.
        """

        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="SweeperPublishFinalization",
                effect=iam.Effect.ALLOW,
                actions=["events:PutEvents"],
                resources=[self._notifications.event_bus.event_bus_arn],
            )
        )

    def _build_reconcile_schedule(self) -> scheduler.CfnSchedule:
        """Crea la regla de EventBridge Scheduler que dispara el sweeper cada 15 min.

        ``rate(15 minutes)`` con ventana de flexibilidad ``OFF`` garantiza que todo
        Lote atascado o terminal-sin-evento se evalúe como máximo 15 minutos
        después de quedar en esa situación (criterio 10.10). El Scheduler invoca la
        Lambda ``reconcile_stuck`` con un rol propio que concede **solo**
        ``lambda:InvokeFunction`` sobre esa función concreta (mínimo privilegio,
        criterios 15.10/15.11). El evento del Scheduler no transporta carga útil
        significativa: el handler no lo inspecciona (diseño 9.3).

        Returns:
            El :class:`aws_cdk.aws_scheduler.CfnSchedule` creado.
        """

        schedule_role = iam.Role(
            self,
            "ReconcileScheduleRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description=(
                "Rol del EventBridge Scheduler que invoca Reconcile_Stuck "
                "(mínimo privilegio: solo InvokeFunction sobre esa Lambda)."
            ),
        )
        # Mínimo privilegio: invocar únicamente la Lambda del sweeper (ARN concreto).
        self.reconcile_stuck.grant_invoke(schedule_role)

        return scheduler.CfnSchedule(
            self,
            "ReconcileStuckSchedule",
            name=f"{self._prefix}-{_SCHEDULE_SUFFIX}",
            # Cada 15 minutos exactos (criterio 10.10).
            schedule_expression=_SCHEDULE_RATE,
            flexible_time_window=_SCHEDULE_FLEXIBLE_WINDOW,
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.reconcile_stuck.function_arn,
                role_arn=schedule_role.role_arn,
            ),
        )
