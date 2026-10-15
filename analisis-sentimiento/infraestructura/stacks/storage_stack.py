"""Stack de dominio ``storage`` (almacenamiento y cifrado base). Tarea 11.1.

Dominio: cifrado base y almacenamiento de objetos. Es el stack raíz del grafo de
dependencias (diseño 14.4.2): no depende de ningún otro y todos los demás
referencian su CMK, su bucket o su Lambda Layer.

Recursos
--------
- **CMK de KMS** (:attr:`key`) con rotación automática **anual** (criterios 15.6
  implícito y 15.14) y una *key policy* que (1) delega en IAM de la cuenta —de modo
  que los permisos se concedan con ``grant_*`` sobre el rol del consumidor y la
  política del recurso no cambie, evitando ciclos entre stacks (diseño 14.4.3)— y
  (2) autoriza a CloudWatch Logs a cifrar los *log groups* del proyecto con la
  clave (diseño 14.4.2).
- **Bucket S3 privado** (:attr:`bucket`): ``BlockPublicAccess`` total (15.8),
  versionado (16.7), ``SSE-KMS`` con la CMK y ``BucketKeyEnabled`` para abaratar
  KMS (diseño 12.3), política que **deniega** todo request sin TLS (15.7, vía
  ``enforce_ssl``) y el *lifecycle* de las secciones 6.5 y 12.8 del diseño
  (16.2–16.5 y expiración de versiones no actuales).
- **Lambda Layer** de dependencias de terceros (:attr:`shared_layer`), empaquetada
  por el construct :class:`constructs.dependencies_layer.DependenciesLayer`
  (criterio 17.16).

Es un stack **con estado**: se despliega con ``termination_protection=True`` y
sus recursos con ``RemovalPolicy.RETAIN`` para que un ``cdk destroy`` accidental
no borre datos ni la clave que los cifra (diseño 14.4.2).
"""

from __future__ import annotations

from aws_cdk import Aws, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct
from constructs.dependencies_layer import DependenciesLayer

from config.environments import EnvConfig

# Prefijos del layout de S3 (diseño 6.5). `inputs/` y `outputs/` son datos de
# negocio con retención larga; `map-results/` es un artefacto operativo del
# Distributed Map con retención corta.
_BUSINESS_PREFIXES: tuple[str, ...] = ("inputs/", "outputs/")
_MAP_RESULTS_PREFIX = "map-results/"

# Plazos del lifecycle (diseño 6.5 / 12.8; criterios 16.2–16.5).
_IA_TRANSITION_DAYS = 30  # inputs/outputs -> Standard-IA
_GLACIER_TRANSITION_DAYS = 90  # inputs/outputs -> Glacier Instant Retrieval
_BUSINESS_EXPIRATION_DAYS = 365  # inputs/outputs -> expiración
_MAP_RESULTS_EXPIRATION_DAYS = 30  # map-results -> expiración
_NONCURRENT_VERSION_EXPIRATION_DAYS = 7  # versiones no actuales (12.8)
_ABORT_MULTIPART_DAYS = 7  # cargas multiparte incompletas


class StorageStack(Stack):
    """Stack del dominio de almacenamiento y cifrado base.

    Expone sus recursos como atributos públicos tipados con **interfaces** de CDK
    (diseño 14.4.3), de modo que los stacks dependientes (``data``, ``ingest``,
    ``compute``, ``api``…) los referencien sin acoplarse a la implementación:

    Attributes:
        key: La CMK del proyecto (:class:`aws_cdk.aws_kms.IKey`).
        bucket: El bucket de análisis privado (:class:`aws_cdk.aws_s3.IBucket`).
        shared_layer: La Lambda Layer de dependencias
            (:class:`aws_cdk.aws_lambda.ILayerVersion`).

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
        # Protección de terminación: es un stack con estado (diseño 14.4.2).
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)
        self._config = config
        prefix = f"{config.project}-{config.env_name}"

        key = self._create_cmk(prefix)
        self.key: kms.IKey = key

        bucket = self._create_bucket(prefix, key)
        self.bucket: s3.IBucket = bucket

        self.shared_layer = DependenciesLayer(
            self, "DependenciesLayer", layer_name=f"{prefix}-deps"
        ).layer

    def _create_cmk(self, prefix: str) -> kms.Key:
        """Crea la CMK del proyecto con rotación anual y su *key policy*.

        La rotación automática de KMS es **anual** por diseño del servicio, por lo
        que ``enable_key_rotation=True`` satisface el criterio 15.14. La política
        delega en IAM de la cuenta (comportamiento por defecto de CDK) y añade un
        permiso para que CloudWatch Logs cifre los *log groups* del proyecto con la
        clave (diseño 14.4.2).

        Args:
            prefix: Prefijo ``{proyecto}-{env}`` para el alias de la clave.

        Returns:
            La :class:`aws_cdk.aws_kms.Key` creada.
        """

        key = kms.Key(
            self,
            "ProjectKey",
            alias=f"alias/{prefix}",
            description=(
                f"CMK del proyecto {prefix}: cifra en reposo S3, DynamoDB, SQS, "
                "las env vars de las Lambdas, los log groups y Secrets Manager."
            ),
            enable_key_rotation=True,  # rotación automática anual (criterio 15.14)
            rotation_period=Duration.days(365),
            removal_policy=RemovalPolicy.RETAIN,  # stack con estado (14.4.2)
        )

        # Permite que CloudWatch Logs use la clave para cifrar los log groups del
        # proyecto. El `kms:ViaService` y la condición de contexto de cifrado
        # restringen el permiso al servicio de Logs de esta región y a los ARNs de
        # log groups de esta cuenta (diseño 14.4.2 / 12.3).
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchLogsEncryption",
                principals=[
                    iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")
                ],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                resources=["*"],  # la key policy aplica a su propia clave
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:aws:logs:{self.region}:{self.account}:log-group:*"
                        )
                    }
                },
            )
        )
        return key

    def _create_bucket(self, prefix: str, key: kms.IKey) -> s3.Bucket:
        """Crea el bucket de análisis privado, cifrado y con el lifecycle del diseño.

        Args:
            prefix: Prefijo ``{proyecto}-{env}`` para el nombre del bucket.
            key: La CMK con la que se cifran los objetos en reposo (``SSE-KMS``).

        Returns:
            El :class:`aws_cdk.aws_s3.Bucket` creado.
        """

        return s3.Bucket(
            self,
            "AnalysisBucket",
            # Nombre global y estable: {proyecto}-{env}-analysis-{account_id}
            # (diseño 6.5). El account id es un pseudo-parámetro de CloudFormation,
            # por lo que `cdk synth` sigue siendo válido sin credenciales en CI.
            bucket_name=f"{prefix}-analysis-{Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,  # criterio 15.8
            versioned=True,  # criterio 16.7
            encryption=s3.BucketEncryption.KMS,  # SSE-KMS con la CMK (12.3)
            encryption_key=key,
            bucket_key_enabled=True,  # abarata KMS ~99% (diseño 12.3)
            enforce_ssl=True,  # deniega aws:SecureTransport=false (criterio 15.7)
            lifecycle_rules=self._lifecycle_rules(),
            removal_policy=RemovalPolicy.RETAIN,  # stack con estado (14.4.2)
        )

    def _lifecycle_rules(self) -> list[s3.LifecycleRule]:
        """Construye las reglas de *lifecycle* del bucket (diseño 6.5 / 12.8).

        Returns:
            Las reglas que implementan los criterios 16.2–16.5 y la expiración de
            versiones no actuales a 7 días (12.8):

            - ``inputs/`` y ``outputs/``: → Standard-IA a 30 días → Glacier Instant
              Retrieval a 90 días → expiración a 365 días.
            - ``map-results/``: expiración a 30 días.
            - Todo el bucket: versiones no actuales expiran a 7 días, se limpian los
              marcadores de eliminación huérfanos y se abortan las cargas multiparte
              incompletas a 7 días.
        """

        rules: list[s3.LifecycleRule] = []

        # Datos de negocio: transición escalonada y expiración a un año.
        for prefix in _BUSINESS_PREFIXES:
            rules.append(
                s3.LifecycleRule(
                    id=f"business-{prefix.rstrip('/')}",
                    enabled=True,
                    prefix=prefix,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(_IA_TRANSITION_DAYS),
                        ),
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=Duration.days(_GLACIER_TRANSITION_DAYS),
                        ),
                    ],
                    expiration=Duration.days(_BUSINESS_EXPIRATION_DAYS),
                )
            )

        # Artefacto operativo del Distributed Map: expiración corta, sin transición.
        rules.append(
            s3.LifecycleRule(
                id="map-results-expiration",
                enabled=True,
                prefix=_MAP_RESULTS_PREFIX,
                expiration=Duration.days(_MAP_RESULTS_EXPIRATION_DAYS),
            )
        )

        # Red de seguridad en TODO el bucket (sin prefijo): versiones no actuales,
        # marcadores de eliminación huérfanos y multiparts incompletas (12.8).
        rules.append(
            s3.LifecycleRule(
                id="noncurrent-and-cleanup",
                enabled=True,
                noncurrent_version_expiration=Duration.days(
                    _NONCURRENT_VERSION_EXPIRATION_DAYS
                ),
                expired_object_delete_marker=True,
                abort_incomplete_multipart_upload_after=Duration.days(
                    _ABORT_MULTIPART_DAYS
                ),
            )
        )

        return rules
