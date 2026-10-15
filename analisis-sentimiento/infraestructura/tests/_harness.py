"""Utilidades compartidas por las Pruebas_De_Infraestructura (tarea 11.4).

Centraliza el *andamiaje* que necesitan las assertions sobre el template de
CloudFormation sintetizado (diseño 21.4):

- :data:`TEST_ENV_CONFIG`: una :class:`~config.environments.EnvConfig` de prueba
  con datos **sintéticos** (criterio 17.10), que satisface la invariante del
  umbral del sweeper (criterio 10.12) para que su construcción no falle.
- :func:`synth_storage_template` / :func:`synth_data_template`: sintetizan el
  stack de dominio correspondiente y devuelven su
  :class:`aws_cdk.assertions.Template`.
- :func:`synth_harness_template`: sintetiza un stack-arnés mínimo que instancia
  :class:`~constructs.backend_function.BackendFunction` y
  :class:`~constructs.tenant_data_role.TenantDataRole` sobre recursos reales del
  ``StorageStack``, de modo que puedan verificarse sus propiedades (``arm64``,
  concurrencia reservada, retención de logs, políticas IAM) antes de que los
  stacks ``compute``/``api`` (tareas 12.2/13.2) los cableen.

Las plantillas se cachean por módulo: sintetizar es la parte cara de estas
pruebas y el template de un stack dado es inmutable una vez construido, así que
se reutiliza entre tests para que la suite siga siendo rápida.

Se construye cada stack dentro de su propia :class:`aws_cdk.App` y **sin** vincular
cuenta/región (``env=None``), igual que el ``cdk synth`` de CI sin credenciales:
las assertions operan sobre el template, que es válido sin credenciales de AWS.

Además, cada ``App`` se crea con el contexto ``aws:cdk:bundling-stacks = []`` para
que CDK **omita el empaquetado de assets** (la Lambda Layer de dependencias y el
código del backend) durante la síntesis: estas pruebas afirman sobre propiedades
del template, no sobre el contenido de los assets, así que evitar el *bundling*
las hace herméticas (sin necesidad de ``pip`` en el ``PATH`` ni de Docker) y
rápidas. El empaquetado real lo sigue ejerciendo ``cdk synth`` en CI (criterio
17.18), que es donde debe validarse.
"""

from __future__ import annotations

from functools import lru_cache

import aws_cdk as cdk
from aws_cdk import Stack
from aws_cdk import assertions
from aws_cdk import aws_iam as iam

from config.environments import EnvConfig
from constructs import Construct
from constructs.backend_function import BackendFunction
from constructs.tenant_data_role import TenantDataRole
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.ingest_stack import IngestStack
from stacks.notifications_stack import NotificationsStack
from stacks.observability_stack import ObservabilityStack
from stacks.storage_stack import StorageStack
from stacks.workflow_stack import WorkflowStack

# Configuración de prueba con datos exclusivamente sintéticos (criterio 17.10).
# Reproduce los valores nominales del diseño; en particular satisface la
# invariante del sweeper (900 >= 3 × 180 + 360) para que `EnvConfig` se
# construya sin lanzar (criterio 10.12).
TEST_ENV_CONFIG = EnvConfig(
    project="sentiment",
    env_name="test",
    log_retention_days=30,
    cors_origins=("http://localhost:5173",),
    bedrock_model_id="<MODEL_ID_A_CALIBRAR>",
    ses_sender_email="no-reply-test@sentiment.example.com",
    distributed_map_max_concurrency=4,
    llm_fanout_max_concurrency=5,
    analyze_llm_reserved_concurrency=40,
    queue_visibility_timeout_seconds=180,
    queue_max_receive_count=3,
    stuck_threshold_seconds=900,
    data_ttl_days=90,
    waf_mode="COUNT",
)

# Concurrencia reservada de prueba del arnés de BackendFunction. Es un valor
# sintético; lo relevante para los tests es que la propiedad quede fijada en el
# template, no su magnitud concreta.
HARNESS_RESERVED_CONCURRENCY = 20


def _test_app() -> cdk.App:
    """Crea una ``App`` de CDK con el empaquetado de assets desactivado.

    El contexto ``aws:cdk:bundling-stacks = []`` indica a CDK que no empaquete
    ningún asset (la Lambda Layer ni el código del backend) al sintetizar, de modo
    que las pruebas de template no dependan de ``pip`` ni de Docker. Las
    propiedades que estas pruebas verifican (IAM, S3, arquitectura de Lambda,
    retención de logs) no se ven afectadas por el contenido de los assets.

    Returns:
        Una :class:`aws_cdk.App` lista para instanciar un stack de prueba.
    """

    return cdk.App(context={"aws:cdk:bundling-stacks": []})


class _BackendFunctionHarnessStack(Stack):
    """Stack-arnés que ejercita ``BackendFunction`` y ``TenantDataRole``.

    ``BackendFunction`` y ``TenantDataRole`` todavía no están cableados en los
    stacks sintetizables (los instancian ``compute`` en 12.2 y ``api`` en 13.2),
    así que este arnés los instancia sobre la CMK y el Layer reales del
    ``StorageStack`` para poder afirmar sobre el template que la Lambda sale
    ``arm64``, con concurrencia reservada y con un *log group* de 30 días, y que
    el rol de datos no concede acciones de datos sobre ``Resource: "*"``.

    Se le adjunta una política de datos de ejemplo (``dynamodb:Query`` acotada por
    ``dynamodb:LeadingKeys`` sobre un ARN concreto) que imita la forma de las
    políticas por tenant de la sección 12.2, de modo que la verificación de "sin
    comodín en acciones de datos" tenga una declaración de datos real que inspeccionar.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, config: EnvConfig
    ) -> None:
        super().__init__(scope, construct_id)

        storage = StorageStack(self, "Storage", config=config)

        backend = BackendFunction(
            self,
            "SampleFn",
            handler="sentiment.handlers.api_status.handler",
            layer=storage.shared_layer,
            key=storage.key,
            memory_mb=256,
            timeout_seconds=30,
            reserved_concurrency=HARNESS_RESERVED_CONCURRENCY,
            environment={"POWERTOOLS_SERVICE_NAME": "sentiment-test"},
        )

        data_role = TenantDataRole(
            self,
            "SampleDataRole",
            execution_role=backend.role,
            tag_keys=("tenant_id",),
            role_name=f"{config.project}-{config.env_name}-sample-data",
        )

        # Política de datos de ejemplo con la forma de la sección 12.2: acción de
        # datos acotada a un ARN concreto y a las claves del tenant (nunca "*").
        data_role.add_data_policy(
            iam.PolicyStatement(
                sid="SampleTenantQuery",
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:Query"],
                resources=[
                    f"arn:aws:dynamodb:{self.region}:{self.account}"
                    f":table/{config.project}-{config.env_name}-analysis"
                ],
                conditions={
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": [
                            "TENANT#${aws:PrincipalTag/tenant_id}*"
                        ]
                    }
                },
            )
        )


@lru_cache(maxsize=1)
def synth_storage_template() -> assertions.Template:
    """Sintetiza el ``StorageStack`` y devuelve su :class:`Template` (cacheado)."""

    app = _test_app()
    stack = StorageStack(app, "sentiment-test-storage", config=TEST_ENV_CONFIG)
    return assertions.Template.from_stack(stack)


@lru_cache(maxsize=1)
def synth_data_template() -> assertions.Template:
    """Sintetiza el ``DataStack`` y devuelve su :class:`Template` (cacheado)."""

    app = _test_app()
    storage = StorageStack(app, "sentiment-test-storage", config=TEST_ENV_CONFIG)
    data = DataStack(
        app, "sentiment-test-data", config=TEST_ENV_CONFIG, storage=storage
    )
    return assertions.Template.from_stack(data)


@lru_cache(maxsize=1)
def synth_notifications_template() -> assertions.Template:
    """Sintetiza el ``NotificationsStack`` y devuelve su :class:`Template` (cacheado).

    Instancia ``storage`` y ``data`` como dependencias reales (el stack necesita la
    CMK, el Layer y la Dynamo_Table) y sintetiza el ``NotificationsStack`` sobre
    ellos, igual que lo hace ``app.py``.
    """

    app = _test_app()
    storage = StorageStack(app, "sentiment-test-storage", config=TEST_ENV_CONFIG)
    data = DataStack(
        app, "sentiment-test-data", config=TEST_ENV_CONFIG, storage=storage
    )
    notifications = NotificationsStack(
        app,
        "sentiment-test-notifications",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
    )
    return assertions.Template.from_stack(notifications)


@lru_cache(maxsize=1)
def synth_harness_template() -> assertions.Template:
    """Sintetiza el stack-arnés de ``BackendFunction`` + ``TenantDataRole``.

    Returns:
        El :class:`aws_cdk.assertions.Template` del
        :class:`_BackendFunctionHarnessStack`.
    """

    app = _test_app()
    stack = _BackendFunctionHarnessStack(
        app, "sentiment-test-harness", config=TEST_ENV_CONFIG
    )
    return assertions.Template.from_stack(stack)


@lru_cache(maxsize=1)
def synth_workflow_template() -> assertions.Template:
    """Sintetiza el ``WorkflowStack`` y devuelve su :class:`Template` (cacheado).

    Monta la cadena de dependencias real que exige el ``WorkflowStack``
    (``storage`` → ``data`` → ``ingest`` → ``notifications`` → ``compute``), igual
    que ``app.py``, de modo que las assertions operen sobre la Maquina_De_Estados
    tal como se sintetizaría en un despliegue. El empaquetado de assets está
    desactivado por :func:`_test_app`, así que no se necesita ``pip`` ni Docker.

    Returns:
        El :class:`aws_cdk.assertions.Template` del ``WorkflowStack``.
    """

    app = _test_app()
    storage = StorageStack(app, "sentiment-test-storage", config=TEST_ENV_CONFIG)
    data = DataStack(
        app, "sentiment-test-data", config=TEST_ENV_CONFIG, storage=storage
    )
    ingest = IngestStack(
        app, "sentiment-test-ingest", config=TEST_ENV_CONFIG, storage=storage
    )
    notifications = NotificationsStack(
        app,
        "sentiment-test-notifications",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
    )
    compute = ComputeStack(
        app,
        "sentiment-test-compute",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
    )
    workflow = WorkflowStack(
        app,
        "sentiment-test-workflow",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
        compute=compute,
    )
    return assertions.Template.from_stack(workflow)


@lru_cache(maxsize=1)
def synth_observability_template() -> assertions.Template:
    """Sintetiza el ``ObservabilityStack`` y devuelve su :class:`Template` (cacheado).

    Monta la cadena de dependencias real que exige el ``ObservabilityStack``
    (``auth``/``storage``/``data``/``notifications``/``ingest``/``compute``/
    ``workflow``/``api``), igual que ``app.py``, de modo que las assertions operen
    sobre el topic de guardia y las cuatro alarmas críticas tal como se
    sintetizarían en un despliegue. El empaquetado de assets está desactivado por
    :func:`_test_app`, así que no se necesita ``pip`` ni Docker.

    Returns:
        El :class:`aws_cdk.assertions.Template` del ``ObservabilityStack``.
    """

    app = _test_app()
    auth = AuthStack(app, "sentiment-test-auth", config=TEST_ENV_CONFIG)
    storage = StorageStack(app, "sentiment-test-storage", config=TEST_ENV_CONFIG)
    data = DataStack(
        app, "sentiment-test-data", config=TEST_ENV_CONFIG, storage=storage
    )
    notifications = NotificationsStack(
        app,
        "sentiment-test-notifications",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
    )
    ingest = IngestStack(
        app, "sentiment-test-ingest", config=TEST_ENV_CONFIG, storage=storage
    )
    compute = ComputeStack(
        app,
        "sentiment-test-compute",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
    )
    workflow = WorkflowStack(
        app,
        "sentiment-test-workflow",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
        compute=compute,
    )
    api = ApiStack(
        app,
        "sentiment-test-api",
        config=TEST_ENV_CONFIG,
        auth=auth,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
    )
    observability = ObservabilityStack(
        app,
        "sentiment-test-observability",
        config=TEST_ENV_CONFIG,
        workflow=workflow,
        api=api,
        compute=compute,
    )
    return assertions.Template.from_stack(observability)


@lru_cache(maxsize=1)
def synth_api_template() -> assertions.Template:
    """Sintetiza el ``ApiStack`` y devuelve su :class:`Template` (cacheado).

    Monta la cadena de dependencias real que exige el ``ApiStack``
    (``auth``/``storage``/``data``/``ingest``/``notifications``), igual que
    ``app.py``, de modo que las assertions operen sobre la API REST, el autorizador
    JWT nativo, el WAF, el Usage_Plan y las cuatro Lambdas de API tal como se
    sintetizarían en un despliegue. El empaquetado de assets está desactivado por
    :func:`_test_app`, así que no se necesita ``pip`` ni Docker.

    Returns:
        El :class:`aws_cdk.assertions.Template` del ``ApiStack``.
    """

    app = _test_app()
    auth = AuthStack(app, "sentiment-test-auth", config=TEST_ENV_CONFIG)
    storage = StorageStack(app, "sentiment-test-storage", config=TEST_ENV_CONFIG)
    data = DataStack(
        app, "sentiment-test-data", config=TEST_ENV_CONFIG, storage=storage
    )
    ingest = IngestStack(
        app, "sentiment-test-ingest", config=TEST_ENV_CONFIG, storage=storage
    )
    notifications = NotificationsStack(
        app,
        "sentiment-test-notifications",
        config=TEST_ENV_CONFIG,
        storage=storage,
        data=data,
    )
    api = ApiStack(
        app,
        "sentiment-test-api",
        config=TEST_ENV_CONFIG,
        auth=auth,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
    )
    return assertions.Template.from_stack(api)
