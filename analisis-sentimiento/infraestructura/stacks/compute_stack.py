"""Stack de dominio ``compute`` (procesamiento). Tarea 12.2.

Dominio: las tres Lambdas del **camino principal de procesamiento** que la
Maquina_De_Estados (tarea 12.3) orquesta por lote e ítem (diseño 7, 9 y 12.2):

- ``analyze_comprehend``: detección batch de idioma y sentimiento clásico con
  Amazon Comprehend (hasta 25 docs por invocación) y escritura del resultado en
  la Dynamo_Table. Timeout 60 s (criterio 9.15).
- ``analyze_llm``: una única invocación al Modelo_Bedrock (API ``Converse`` vía
  ``bedrock:InvokeModel``) por ítem, con **concurrencia reservada 40** —el tope
  duro global de presión sobre Bedrock (criterios 5.7 y 18.4)— y timeout 120 s
  (criterio 9.13).
- ``aggregate_results``: consolida el lote, escribe el Documento_Consolidado en
  ``outputs/`` de S3 y publica el Evento_Finalizacion en el Bus_Eventos. Timeout
  300 s (criterio 9.14).

Cada Lambda se construye con :class:`~constructs.backend_function.BackendFunction`
(``arm64``, rol de ejecución propio, *log group* cifrado con la CMK y retención de
30 días, X-Ray, empaquetado desde ``backend/src/``), y recibe **solo** los
permisos de datos que su handler ejecuta, concedidos sobre **ARN concretos** por
el método privado correspondiente de este stack (mínimo privilegio, criterios
15.10 y 15.11). No hay ningún ``Resource: "*"`` sobre acciones de datos
(DynamoDB, S3, Bedrock-modelo): las dos únicas excepciones documentadas son X-Ray
(que inyecta el propio ``BackendFunction``) y las acciones ``Detect*`` de
Comprehend, cuya API no admite ARN de recurso (ver :meth:`_grant_comprehend`).

IAM por función (diseño 12.2):

- ``analyze_comprehend``:
  ``comprehend:BatchDetectDominantLanguage`` + ``comprehend:BatchDetectSentiment``
  (+ ``DetectDominantLanguage``) sobre ``"*"`` **acotado por región**
  (``aws:RequestedRegion``), y escritura DynamoDB (``UpdateItem`` /
  ``TransactWriteItems`` / ``Query`` / ``GetItem``) sobre el ARN de la tabla y sus
  índices. **Sin** Bedrock, **sin** S3.
- ``analyze_llm``:
  ``bedrock:InvokeModel`` sobre el ARN del **modelo concreto** (``foundation-model``,
  nunca comodín de modelo), ``ssm:GetParameter`` sobre el ARN del parámetro
  ``MODEL_ID``, y lectura/escritura DynamoDB (``GetItem`` / ``UpdateItem`` /
  ``TransactWriteItems``) sobre el ARN de la tabla. **Sin** Comprehend, **sin** S3.
- ``aggregate_results``:
  DynamoDB (``Query`` / ``GetItem`` / ``UpdateItem`` / ``TransactWriteItems``)
  sobre la tabla y sus índices, ``s3:PutObject`` sobre ``outputs/*`` y
  ``s3:GetObject`` sobre ``map-results/*`` del bucket, y ``events:PutEvents`` sobre
  el Bus_Eventos del dominio de notificación.

El cifrado en reposo lo cubre la CMK del ``StorageStack``: las concesiones
``grant_*`` de DynamoDB/S3 ya añaden el permiso de KMS necesario sobre esa clave,
de modo que la Lambda puede cifrar/descifrar los datos que toca sin un comodín de
clave.

Dependencias (diseño 14.4.2): ``storage`` (CMK + bucket + Layer), ``data`` (tabla +
parámetro ``MODEL_ID``), ``ingest`` (DLQ de ítems) y ``notifications`` (Bus_Eventos).

Expone las tres funciones como atributos públicos tipados para que el
``workflow_stack`` (tarea 12.3) las cablee en la Maquina_De_Estados.
"""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct
from constructs.backend_function import BackendFunction

from config.environments import EnvConfig
from stacks.data_stack import DataStack
from stacks.ingest_stack import IngestStack
from stacks.naming import event_bus_name as _event_bus_name
from stacks.notifications_stack import NotificationsStack
from stacks.storage_stack import StorageStack

# --- Rutas de los handlers (diseño 14.4.4) ---
# Cadenas de módulo que apuntan al handler empaquetado en ``backend/src``. Deben
# coincidir con los módulos reales de ``sentiment/handlers/`` (tarea 9.2).
_HANDLER_COMPREHEND = "sentiment.handlers.analyze_comprehend.handler"
_HANDLER_LLM = "sentiment.handlers.analyze_llm.handler"
_HANDLER_AGGREGATE = "sentiment.handlers.aggregate_results.handler"

# --- Timeouts por función (criterios 9.13/9.14/9.15; diseño 10.7) ---
# El timeout de la Lambda es el techo duro que la Maquina_De_Estados clasifica
# como condición transitoria reintentable (criterio 9.16).
_TIMEOUT_COMPREHEND_S = 60  # criterio 9.15
_TIMEOUT_LLM_S = 120  # criterio 9.13
_TIMEOUT_AGGREGATE_S = 300  # criterio 9.14

# --- Memoria por función (MiB) ---
# Las Lambdas de procesamiento son ligeras en CPU y su latencia la domina la
# dependencia externa (Comprehend/Bedrock) o DynamoDB, no el cómputo local; 256
# MiB es un punto de partida razonable. ``aggregate_results`` sube a 512 MiB
# porque mantiene en memoria el Documento_Consolidado de hasta 1.000 ítems antes
# de serializarlo a S3 (diseño 10.3), evitando presión de GC en el techo de 300 s.
_MEMORY_COMPREHEND_MB = 256
_MEMORY_LLM_MB = 256
_MEMORY_AGGREGATE_MB = 512

# --- Variables de entorno comunes ---
# ``CONFIG_ENV`` permite al backend resolver parámetros de Parameter_Store
# (``/sentiment/<env>/...``) como fallback cuando una variable de entorno no está
# presente (ver ``observability/config.py``). No es un secreto.
_ENV_CONFIG = "CONFIG_ENV"
_ENV_TABLE_NAME = "TABLE_NAME"
_ENV_BUCKET_NAME = "BUCKET_NAME"
_ENV_EVENT_BUS_NAME = "EVENT_BUS_NAME"
_ENV_MODEL_ID = "MODEL_ID"

# --- Prefijos de S3 que toca ``aggregate_results`` (diseño 6.5) ---
_S3_OUTPUTS_PREFIX = "outputs/*"
_S3_MAP_RESULTS_PREFIX = "map-results/*"

# --- Convención del nombre del Bus_Eventos del dominio de notificación ---
# ``aggregate_results`` necesita el ARN (para la política ``events:PutEvents``) y
# el nombre (para la variable ``EVENT_BUS_NAME`` que lee ``eventbridge_publisher``)
# del Bus_Eventos. Ambos se derivan de la convención ``{project}-{env}-events``,
# cuya única fuente de verdad vive en :mod:`stacks.naming` (importada arriba como
# ``_event_bus_name``) para que el ``notifications_stack`` (tarea 13.3), que CREA
# el bus con ese nombre, y este stack, que lo REFERENCIA por nombre, no puedan
# desincronizarse. El alias ``_event_bus_name`` conserva la compatibilidad con el
# código y las pruebas que ya lo importaban desde este módulo.


class ComputeStack(Stack):
    """Stack del dominio de procesamiento: las tres Lambdas del camino principal.

    Expone las funciones como atributos públicos tipados con la interfaz
    :class:`aws_cdk.aws_lambda.IFunction` (diseño 14.4.3), de modo que el
    ``workflow_stack`` (tarea 12.3) las referencie en los estados ``Task`` de la
    Maquina_De_Estados sin acoplarse a la implementación de este stack.

    Attributes:
        analyze_comprehend: Lambda de detección batch con Comprehend
            (:class:`aws_cdk.aws_lambda.IFunction`).
        analyze_llm: Lambda de análisis de emociones con Bedrock, con
            concurrencia reservada 40 (:class:`aws_cdk.aws_lambda.IFunction`).
        aggregate_results: Lambda de consolidación y cierre del lote
            (:class:`aws_cdk.aws_lambda.IFunction`).

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
        storage: Stack de almacenamiento del que depende (CMK, bucket, Layer).
        data: Stack de datos del que depende (tabla, parámetro ``MODEL_ID``).
        ingest: Stack de ingesta del que depende (DLQ de ítems).
        notifications: Stack de notificación del que depende (Bus_Eventos).
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
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._config = config
        self._storage = storage
        self._data = data
        self._ingest = ingest
        self._notifications = notifications
        self.add_dependency(storage)
        self.add_dependency(data)
        self.add_dependency(ingest)
        self.add_dependency(notifications)

        self.analyze_comprehend: lambda_.IFunction = self._build_comprehend()
        self.analyze_llm: lambda_.IFunction = self._build_llm()
        self.aggregate_results: lambda_.IFunction = self._build_aggregate()

    # --- Nombres de recurso compartidos entre funciones ---

    def _common_environment(self) -> dict[str, str]:
        """Variables de entorno comunes a las tres Lambdas de procesamiento.

        Returns:
            El nombre de la tabla (para los adaptadores de DynamoDB) y el entorno
            de configuración (para el fallback a Parameter_Store).
        """

        return {
            _ENV_TABLE_NAME: self._data.table.table_name,
            _ENV_CONFIG: self._config.env_name,
        }

    # --- analyze_comprehend --------------------------------------------------

    def _build_comprehend(self) -> lambda_.IFunction:
        """Construye ``analyze_comprehend`` con sus permisos de mínimo privilegio.

        Permisos: Comprehend batch (sobre ``"*"`` acotado por región, por ser una
        API sin recursos direccionables) y escritura DynamoDB sobre la tabla y sus
        índices. Sin Bedrock, sin S3 (diseño 12.2).

        Returns:
            La función ``analyze_comprehend`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        backend = BackendFunction(
            self,
            "AnalyzeComprehend",
            handler=_HANDLER_COMPREHEND,
            layer=self._storage.shared_layer,
            key=self._storage.key,
            memory_mb=_MEMORY_COMPREHEND_MB,
            timeout_seconds=_TIMEOUT_COMPREHEND_S,
            reserved_concurrency=None,
            environment=self._common_environment(),
        )

        self._grant_comprehend(backend.role)
        # Escritura condicionada del ITEM + contador del META (TransactWriteItems,
        # UpdateItem) y relectura por `pk` (Query/GetItem); sobre la tabla y sus
        # índices, nunca sobre un comodín de tabla (criterio 15.11).
        self._grant_table_write(backend.function)

        return backend.function

    def _grant_comprehend(self, role: iam.IRole) -> None:
        """Concede las acciones batch de Comprehend, acotadas por región.

        Las acciones ``Detect*`` / ``BatchDetect*`` de Comprehend **no admiten ARN
        de recurso** (la API no tiene recursos direccionables), por lo que su
        ``Resource`` debe ser ``"*"``. Es la única excepción de datos documentada
        del diseño (nota de 12.2): se compensa (1) acotando con la condición
        ``aws:RequestedRegion`` a la región de despliegue y (2) aislando estas
        acciones en un rol que, por lo demás, solo escribe en la tabla —ni Bedrock
        ni S3—.

        Args:
            role: Rol de ejecución dedicado de ``analyze_comprehend``.
        """

        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ComprehendBatchDetect",
                effect=iam.Effect.ALLOW,
                actions=[
                    "comprehend:BatchDetectDominantLanguage",
                    "comprehend:BatchDetectSentiment",
                    "comprehend:DetectDominantLanguage",
                ],
                # Inevitable: la API de Comprehend Detect* no es direccionable por
                # ARN. Acotado por región para limitar el alcance (nota 12.2).
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:RequestedRegion": self.region}
                },
            )
        )

    # --- analyze_llm ---------------------------------------------------------

    def _build_llm(self) -> lambda_.IFunction:
        """Construye ``analyze_llm`` con concurrencia reservada 40 y timeout 120 s.

        Permisos: ``bedrock:InvokeModel`` sobre el ARN del **modelo concreto**
        (nunca comodín de modelo), ``ssm:GetParameter`` sobre el parámetro
        ``MODEL_ID`` y lectura/escritura DynamoDB sobre la tabla. Sin Comprehend,
        sin S3 (diseño 12.2).

        Returns:
            La función ``analyze_llm`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        environment = self._common_environment()
        # ``analyze_llm`` resuelve el MODEL_ID; se inyecta además como variable de
        # entorno (fuente primaria) y queda disponible el fallback a SSM vía
        # CONFIG_ENV. El valor es un marcador no secreto por entorno (criterio
        # 17.20); el valor real se fija fuera de banda tras la calibración (15.3).
        environment[_ENV_MODEL_ID] = self._config.bedrock_model_id

        backend = BackendFunction(
            self,
            "AnalyzeLlm",
            handler=_HANDLER_LLM,
            layer=self._storage.shared_layer,
            key=self._storage.key,
            memory_mb=_MEMORY_LLM_MB,
            timeout_seconds=_TIMEOUT_LLM_S,  # criterio 9.13
            # Tope duro y global de invocaciones simultáneas a Bedrock, sumando
            # todos los tenants y lotes (criterios 5.7 / 18.4). Reservar también
            # limita: es el único dial anti-throttle de Bedrock.
            reserved_concurrency=self._config.analyze_llm_reserved_concurrency,
            environment=environment,
        )

        self._grant_bedrock_model(backend.role)
        self._grant_model_id_parameter(backend.role)
        # El servicio relee el ITEM (GetItem) y cierra su transición con
        # TransactWriteItems/UpdateItem; no consulta índices.
        self._data.table.grant(
            backend.function,
            "dynamodb:GetItem",
            "dynamodb:UpdateItem",
            "dynamodb:PutItem",
            "dynamodb:ConditionCheckItem",
        )

        return backend.function

    def _grant_bedrock_model(self, role: iam.IRole) -> None:
        """Concede ``bedrock:InvokeModel`` sobre el ARN del modelo concreto.

        Construye el ARN ``arn:aws:bedrock:{region}::foundation-model/{model_id}``
        a partir de ``config.bedrock_model_id`` (diseño 12.2: "no wildcard de
        modelo"). ``bedrock:InvokeModel`` también autoriza la API ``Converse`` que
        usa el adaptador (no existe una acción ``bedrock:Converse`` separada).

        El ``model_id`` es un marcador por entorno hasta la calibración (15.3);
        cuando se fije el modelo real, la política debe reflejar su ARN exacto. Por
        eso el ARN se deriva del mismo valor de configuración que publica el
        parámetro SSM: una sola fuente para el modelo autorizado y el modelo
        invocado.

        Args:
            role: Rol de ejecución dedicado de ``analyze_llm``.
        """

        # Los foundation-models de Bedrock viven en una partición sin cuenta en el
        # ARN (``...:bedrock:{region}::foundation-model/{id}``). ``format_arn`` con
        # ``account=""`` produce ese doble ``::`` requerido.
        model_arn = self.format_arn(
            service="bedrock",
            account="",
            resource="foundation-model",
            resource_name=self._config.bedrock_model_id,
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="BedrockInvokeConcreteModel",
                effect=iam.Effect.ALLOW,
                # InvokeModel autoriza también Converse/ConverseStream.
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[model_arn],  # modelo concreto; nunca comodín (12.2)
            )
        )

    def _grant_model_id_parameter(self, role: iam.IRole) -> None:
        """Concede ``ssm:GetParameter`` sobre el ARN del parámetro ``MODEL_ID``.

        Acota la lectura al parámetro ``/sentiment/{env}/MODEL_ID`` concreto que
        publica el ``DataStack`` (diseño 12.2), no a un comodín de parámetros.

        Args:
            role: Rol de ejecución dedicado de ``analyze_llm``.
        """

        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ReadModelIdParameter",
                effect=iam.Effect.ALLOW,
                actions=["ssm:GetParameter"],
                resources=[self._data.model_id_parameter.parameter_arn],
            )
        )

    # --- aggregate_results ---------------------------------------------------

    def _build_aggregate(self) -> lambda_.IFunction:
        """Construye ``aggregate_results`` con timeout 300 s y sus permisos.

        Permisos: lectura/escritura DynamoDB sobre la tabla y sus índices,
        ``s3:PutObject`` sobre ``outputs/*``, ``s3:GetObject`` sobre
        ``map-results/*`` y ``events:PutEvents`` sobre el Bus_Eventos (diseño 12.2).

        Returns:
            La función ``aggregate_results`` (:class:`aws_cdk.aws_lambda.IFunction`).
        """

        environment = self._common_environment()
        environment[_ENV_BUCKET_NAME] = self._storage.bucket.bucket_name
        environment[_ENV_EVENT_BUS_NAME] = _event_bus_name(self._config)

        backend = BackendFunction(
            self,
            "AggregateResults",
            handler=_HANDLER_AGGREGATE,
            layer=self._storage.shared_layer,
            key=self._storage.key,
            memory_mb=_MEMORY_AGGREGATE_MB,
            timeout_seconds=_TIMEOUT_AGGREGATE_S,  # criterio 9.14
            reserved_concurrency=None,
            environment=environment,
        )

        # Recuento desde los ITEMs reales (Query), relectura del META (GetItem) y
        # cierre del lote (UpdateItem/TransactWriteItems) sobre la tabla y sus
        # índices; sin comodín de tabla (criterio 15.11).
        self._grant_table_write(backend.function)
        self._grant_s3_outputs(backend)
        self._grant_event_bus(backend.role)

        return backend.function

    def _grant_s3_outputs(self, backend: BackendFunction) -> None:
        """Concede escritura del Documento_Consolidado y lectura de map-results.

        ``s3:PutObject`` se acota al prefijo ``outputs/*`` (donde vive el
        Documento_Consolidado) y ``s3:GetObject`` al prefijo ``map-results/*`` (los
        resultados parciales que escribe el Distributed Map). Las concesiones
        ``grant_*`` de S3 incluyen el permiso de KMS sobre la CMK del bucket, por lo
        que la Lambda puede cifrar el objeto de salida y descifrar los parciales
        sin un comodín de clave (criterio 15.6).

        Args:
            backend: El ``BackendFunction`` de ``aggregate_results``.
        """

        # Escritura acotada al prefijo de salidas: nunca al bucket completo.
        self._storage.bucket.grant_put(backend.function, _S3_OUTPUTS_PREFIX)
        # Lectura acotada a los parciales del Distributed Map.
        self._storage.bucket.grant_read(backend.function, _S3_MAP_RESULTS_PREFIX)

    def _grant_event_bus(self, role: iam.IRole) -> None:
        """Concede ``events:PutEvents`` sobre el Bus_Eventos del dominio notificación.

        El NotificationsStack (tarea 13.3) todavía no expone el bus como atributo,
        así que el ARN se construye por convención de nombre
        (:func:`_event_bus_name`) con ``format_arn``. Cuando 13.3 cree el bus con
        ese mismo nombre, esta política autoriza la publicación del
        Evento_Finalizacion sobre el recurso real (diseño 12.2 / 13.1).

        Args:
            role: Rol de ejecución dedicado de ``aggregate_results``.
        """

        event_bus_arn = self.format_arn(
            service="events",
            resource="event-bus",
            resource_name=_event_bus_name(self._config),
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PublishAnalysisCompleted",
                effect=iam.Effect.ALLOW,
                actions=["events:PutEvents"],
                resources=[event_bus_arn],  # bus concreto; nunca "*" (15.11)
            )
        )

    # --- Permisos de datos compartidos --------------------------------------

    def _grant_table_write(self, grantee: lambda_.IFunction) -> None:
        """Concede el acceso de datos a la Dynamo_Table y a sus índices.

        Otorga el conjunto de acciones que ejecutan los servicios de
        ``analyze_comprehend`` y ``aggregate_results``: ``TransactWriteItems`` (que
        en IAM se expresa como las acciones de sus operaciones subyacentes:
        ``PutItem``/``UpdateItem``/``ConditionCheckItem``), más ``UpdateItem``,
        ``Query`` y ``GetItem``. Se concede sobre el ARN de la tabla **y** los de
        sus índices GSI (``grant`` sobre el recurso de la tabla no cubre los
        índices por sí solo), nunca sobre un comodín de tabla (criterio 15.11).

        La concesión ``grant`` de DynamoDB añade además el permiso de KMS sobre la
        CMK que cifra la tabla, de modo que la Lambda puede leer y escribir los
        datos cifrados sin un comodín de clave (criterio 15.6).

        Args:
            grantee: La Lambda que recibe el permiso.
        """

        actions = (
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:UpdateItem",
            "dynamodb:PutItem",
            "dynamodb:ConditionCheckItem",
        )
        # Permiso sobre la tabla (incluye KMS sobre la CMK de cifrado).
        self._data.table.grant(grantee, *actions)
        # ...y sobre sus índices: una Query puede dirigirse a GSI1/GSI2 y el ARN
        # de la tabla no cubre `.../index/*` por sí mismo.
        grantee.grant_principal.add_to_principal_policy(
            iam.PolicyStatement(
                sid="TableIndexAccess",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:Query", "dynamodb:GetItem"],
                resources=[f"{self._data.table.table_arn}/index/*"],
            )
        )
