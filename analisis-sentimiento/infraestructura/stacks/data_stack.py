"""Stack de dominio ``data`` (datos). Tarea 11.2.

Dominio: persistencia del estado de los Lotes y de sus Ítems, y los parámetros de
configuración de runtime que la Infraestructura publica para que las Lambdas los
lean sin redeploy.

Recursos
--------
- **Tabla DynamoDB single-table** (:attr:`table`). Modela las dos entidades del
  dominio —``META`` del Lote e ``ITEM`` de cada respuesta— con una relación 1-N
  jerárquica, de modo que "dame el Lote y todos sus Ítems" es una sola ``Query``
  por ``pk`` (diseño 6.1). Configuración (diseño 6.4):

  - ``PAY_PER_REQUEST`` (On-Demand): la carga es a ráfagas e impredecible
    (criterios 16.1 / 18.8).
  - ``SSE-KMS`` con la CMK del proyecto (``customer-managed``): la columna
    ``respuesta`` contiene PII (criterio 15.6 vía la CMK del ``StorageStack``).
  - **PITR** habilitado (ventana de 35 días) (criterio 16.6).
  - **Stream** ``NEW_AND_OLD_IMAGES`` habilitado, sin consumidor en v1 (diseño 6.4).
  - **TTL** sobre el atributo ``ttl`` (90 días por defecto, criterio 16.1).
  - **GSI1** (``gsi1pk``/``gsi1sk``): listado de los Lotes de un tenant, más
    recientes primero (patrón de acceso 4, diseño 6.3).
  - **GSI2** *sparse* (``gsi2pk``/``gsi2sk``): candidatos del ``sweeper`` (``SWEEP``)
    e Ítems atascados (``STUCK#``). Es *sparse* por construcción: el atributo
    ``gsi2pk`` solo existe mientras la entidad no es terminal, por lo que el índice
    contiene a lo sumo unas decenas de entradas (patrones de acceso 7 y 8,
    diseño 6.3).

- **Parámetros SSM Parameter Store** (:attr:`model_id_parameter`,
  :attr:`ttl_days_parameter`): ``MODEL_ID`` y el periodo de retención en días,
  bajo el prefijo ``/sentiment/{env}/`` (criterio 18.8). Los nombres coinciden
  **exactamente** con los sufijos que espera
  :mod:`backend.src.sentiment.observability.config` (``MODEL_ID``, ``TTL_DAYS``),
  que resuelve ``/sentiment/<env>/<sufijo>`` en runtime.

  El valor de ``MODEL_ID`` es un **marcador** por entorno (``bedrock_model_id`` de
  :class:`EnvConfig`, p. ej. ``<MODEL_ID_A_CALIBRAR>`` en ``dev``): no es un
  secreto y se fija al valor real fuera de banda (tras la calibración de la tarea
  15.3) sin redeploy. No se publica ningún secreto en el código ni en los
  archivos de configuración (criterios 15.5 / 17.20).

Dependencias (diseño 14.4.2): ``storage`` (usa la CMK para el cifrado en reposo).

Es un stack **con estado**: se despliega con ``termination_protection=True`` y la
tabla con ``RemovalPolicy.RETAIN``, para que un ``cdk destroy`` accidental no
borre el estado de los Lotes ni haga irrecuperable el PITR (diseño 14.4.2).
"""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config.environments import EnvConfig
from stacks.storage_stack import StorageStack

# --- Nombres de atributos de la single-table (diseño 6.2 / 6.3) ---
# Clave primaria compuesta: `pk` identifica el Lote dentro de su tenant y `sk`
# distingue el META de cada ITEM. Son los mismos nombres que usa el repositorio
# del backend (adaptador de la tarea 6.4), de modo que la tabla y el código que la
# consulta hablan el mismo esquema.
_PARTITION_KEY = "pk"
_SORT_KEY = "sk"

# GSI1 — listado de Lotes por tenant (patrón de acceso 4).
_GSI1_NAME = "GSI1"
_GSI1_PARTITION_KEY = "gsi1pk"  # TENANT#{tenant_id}
_GSI1_SORT_KEY = "gsi1sk"  # CREATED#{created_at}#{analysis_id}

# GSI2 — sparse: candidatos del sweeper (SWEEP) e ítems atascados (STUCK#)
# (patrones de acceso 7 y 8). Es sparse porque `gsi2pk` solo existe mientras la
# entidad no es terminal.
_GSI2_NAME = "GSI2"
_GSI2_PARTITION_KEY = "gsi2pk"  # SWEEP (META) | STUCK#{analysis_id} (ITEM)
_GSI2_SORT_KEY = "gsi2sk"  # UPDATED#{updated_at}#{analysis_id} | CREATED#{created_at}

# Atributo de expiración por TTL (diseño 6.4; criterio 16.1).
_TTL_ATTRIBUTE = "ttl"

# --- Parameter Store (criterio 18.8) ---
# Prefijo del namespace de parámetros. El código del backend
# (observability/config.py) resuelve `/sentiment/<env>/<sufijo>`; estos sufijos
# deben coincidir con los que ese módulo espera (`MODEL_ID`, `TTL_DAYS`).
_SSM_PREFIX = "/sentiment"
_SSM_MODEL_ID_SUFFIX = "MODEL_ID"
_SSM_TTL_DAYS_SUFFIX = "TTL_DAYS"


class DataStack(Stack):
    """Stack del dominio de datos: la single-table y los parámetros SSM.

    Expone sus recursos como atributos públicos tipados con **interfaces** de CDK
    (diseño 14.4.3), de modo que los stacks dependientes (``compute``, ``api``,
    ``notifications``, ``workflow``) los referencien sin acoplarse a la
    implementación. Los stacks consumidores conceden sus propios permisos de
    mínimo privilegio sobre estos recursos con ``grant_*`` (criterios 15.10/15.11).

    Attributes:
        table: La tabla single-table de análisis
            (:class:`aws_cdk.aws_dynamodb.ITable`).
        model_id_parameter: El parámetro SSM ``/sentiment/{env}/MODEL_ID``
            (:class:`aws_cdk.aws_ssm.IStringParameter`).
        ttl_days_parameter: El parámetro SSM ``/sentiment/{env}/TTL_DAYS``
            (:class:`aws_cdk.aws_ssm.IStringParameter`).
        gsi1_name: Nombre del GSI de listado por tenant (``"GSI1"``).
        gsi2_name: Nombre del GSI *sparse* del sweeper (``"GSI2"``).

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
        storage: Stack de almacenamiento del que depende (fuente de la CMK).
    """

    # Expuestos como atributos de clase para que las pruebas de infraestructura y
    # los stacks consumidores referencien los nombres de índice sin duplicar
    # literales.
    gsi1_name: str = _GSI1_NAME
    gsi2_name: str = _GSI2_NAME

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        storage: StorageStack,
        **kwargs: object,
    ) -> None:
        # Protección de terminación: es un stack con estado (diseño 14.4.2).
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)
        self._config = config
        self._storage = storage
        prefix = f"{config.project}-{config.env_name}"

        self.table: dynamodb.ITable = self._create_table(prefix, storage)

        self.model_id_parameter: ssm.IStringParameter = self._create_model_id_param()
        self.ttl_days_parameter: ssm.IStringParameter = self._create_ttl_days_param()

    def _create_table(
        self, prefix: str, storage: StorageStack
    ) -> dynamodb.Table:
        """Crea la single-table con sus dos GSI, PITR, TTL y stream.

        Args:
            prefix: Prefijo ``{proyecto}-{env}`` para el nombre de la tabla.
            storage: Stack de almacenamiento, fuente de la CMK de cifrado.

        Returns:
            La :class:`aws_cdk.aws_dynamodb.Table` creada.
        """

        table = dynamodb.Table(
            self,
            "AnalysisTable",
            table_name=f"{prefix}-analysis",
            partition_key=dynamodb.Attribute(
                name=_PARTITION_KEY, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name=_SORT_KEY, type=dynamodb.AttributeType.STRING
            ),
            # On-Demand: carga a ráfagas e impredecible (criterios 16.1 / 18.8).
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # SSE-KMS con la CMK del proyecto (customer-managed): la `respuesta`
            # es PII (criterio 15.6). CUSTOMER_MANAGED exige una clave explícita.
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=storage.key,
            # PITR habilitado: ventana de 35 días (criterio 16.6).
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                )
            ),
            # Stream NEW_AND_OLD_IMAGES: habilitado sin consumidor en v1, queda
            # disponible para auditoría o proyecciones futuras (diseño 6.4).
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            # TTL sobre `ttl` (epoch): mecanismo de cumplimiento de retención
            # (criterio 16.1). Los días efectivos los fija el backend desde el
            # parámetro SSM TTL_DAYS.
            time_to_live_attribute=_TTL_ATTRIBUTE,
            # Stack con estado: retener la tabla ante un destroy accidental
            # (diseño 14.4.2).
            removal_policy=RemovalPolicy.RETAIN,
        )

        # GSI1 — listado de Lotes por tenant, más recientes primero (patrón 4).
        # Proyección ALL: el listado devuelve metadatos del Lote (estado, progreso,
        # timestamps) sin una segunda lectura del META.
        table.add_global_secondary_index(
            index_name=_GSI1_NAME,
            partition_key=dynamodb.Attribute(
                name=_GSI1_PARTITION_KEY, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name=_GSI1_SORT_KEY, type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI2 — sparse: sweeper (SWEEP) e ítems atascados (STUCK#) (patrones 7/8).
        # Proyección KEYS_ONLY: el sweeper y la detección de atascados solo
        # necesitan las claves para después releer el registro completo por `pk`,
        # y así el índice no replica la PII de `respuesta`.
        table.add_global_secondary_index(
            index_name=_GSI2_NAME,
            partition_key=dynamodb.Attribute(
                name=_GSI2_PARTITION_KEY, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name=_GSI2_SORT_KEY, type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        return table

    def _create_model_id_param(self) -> ssm.StringParameter:
        """Crea el parámetro SSM ``/sentiment/{env}/MODEL_ID``.

        El valor es un **marcador** no secreto (``config.bedrock_model_id``) que se
        fija al identificador real del modelo fuera de banda, sin redeploy. El
        nombre coincide con el sufijo ``MODEL_ID`` que el backend resuelve en
        runtime (criterios 7.9 / 18.8).

        Returns:
            El :class:`aws_cdk.aws_ssm.StringParameter` creado.
        """

        return ssm.StringParameter(
            self,
            "ModelIdParameter",
            parameter_name=self._param_name(_SSM_MODEL_ID_SUFFIX),
            # Marcador de entorno; el valor real se fija fuera de banda tras la
            # calibración (tarea 15.3). Nunca es un secreto (criterio 17.20).
            string_value=self._config.bedrock_model_id,
            description=(
                "Identificador del Modelo_Bedrock a invocar. Marcador por entorno; "
                "se fija al valor real fuera de banda sin redeploy (criterio 7.9)."
            ),
            tier=ssm.ParameterTier.STANDARD,
        )

    def _create_ttl_days_param(self) -> ssm.StringParameter:
        """Crea el parámetro SSM ``/sentiment/{env}/TTL_DAYS`` (retención).

        Publica el periodo de retención en días (``config.data_ttl_days``) para que
        el backend lo lea desde Parameter Store (criterios 16.8 / 18.8). Parameter
        Store solo almacena cadenas; el backend lo interpreta como entero.

        Returns:
            El :class:`aws_cdk.aws_ssm.StringParameter` creado.
        """

        return ssm.StringParameter(
            self,
            "TtlDaysParameter",
            parameter_name=self._param_name(_SSM_TTL_DAYS_SUFFIX),
            string_value=str(self._config.data_ttl_days),
            description=(
                "Periodo de retención en días del atributo `ttl` de la "
                "Dynamo_Table (criterios 16.1 / 16.8)."
            ),
            tier=ssm.ParameterTier.STANDARD,
        )

    def _param_name(self, suffix: str) -> str:
        """Construye el nombre de un parámetro bajo ``/sentiment/{env}/``.

        Args:
            suffix: Sufijo del parámetro (p. ej. ``MODEL_ID``), que debe coincidir
                con el que el backend resuelve en runtime.

        Returns:
            El nombre completo del parámetro, p. ej. ``/sentiment/dev/MODEL_ID``.
        """

        return f"{_SSM_PREFIX}/{self._config.env_name}/{suffix}"
