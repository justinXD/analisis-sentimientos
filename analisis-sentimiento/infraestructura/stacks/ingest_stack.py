"""Stack de dominio ``ingest`` (buffer de ingesta). Tarea 12.1.

Dominio: amortiguación de la ingesta y colas de reintento/fallo. Es la pieza que
desacopla la API (``api_submit``) de la orquestación (Step Functions vía
EventBridge Pipes): ``api_submit`` encola exactamente un mensaje por Lote en la
Cola_Solicitudes y responde ``202`` sin esperar al procesamiento (diseño 4.2).

Recursos
--------
- **Cola_Solicitudes** (:attr:`requests_queue`): cola SQS ``analysis-requests``
  en la que cada mensaje representa un Lote listo para orquestar. Visibility
  timeout de ``queue_visibility_timeout_seconds`` (180 s) y *redrive* a la
  **DLQ_Solicitudes** cuando un mensaje alcanza ``queue_max_receive_count`` (3)
  recepciones sin procesarse (criterio 9.8). El visibility timeout y el
  ``maxReceiveCount`` provienen de :class:`EnvConfig`; el criterio 10.12 (validado
  en ``EnvConfig.__post_init__``) garantiza que el umbral del sweeper cubre esta
  ventana de reintentos.
- **DLQ_Solicitudes** (:attr:`requests_dlq`): cola SQS que recibe los mensajes de
  Lote que la Cola_Solicitudes no logró procesar tras 3 recepciones (p. ej. si el
  arranque de la orquestación falla de forma persistente).
- **DLQ_Items** (:attr:`items_dlq`): cola SQS que recibe los mensajes de Ítem
  "veneno" que el procesamiento del Distributed_Map marca como ``FAILED`` (un
  mensaje por Ítem fallido, con ``item_id``/``analysis_id``/``tenant_id``/
  ``correlation_id``/``error_code`` y **sin** el texto de ``respuesta`` ni de
  ``explicacion`` — criterio 9.7). El productor es la Maquina_De_Estados (tarea
  12.3); este stack solo crea la cola de destino.

Las tres colas se cifran en reposo con la CMK del proyecto (criterio 15.6 vía la
CMK del ``StorageStack``; criterio 15.6/15.6 "las DLQs cifran sus datos en reposo
con la CMK"). Las DLQs retienen sus mensajes 14 días para dar margen de
inspección y reproceso (criterio 9.10); la Cola_Solicitudes se configura con la
misma retención de 14 días por consistencia operativa.

Es un stack **sin estado**: las colas son buffers transitorios, no la fuente de
verdad (que vive en DynamoDB, diseño 2.1.3). Por eso **no** lleva
``termination_protection`` ni ``RemovalPolicy.RETAIN``: sus recursos pueden
recrearse sin pérdida de datos de negocio, a diferencia de los stacks con estado
``storage``, ``data`` y ``auth`` (diseño 14.4.2).

Dependencias (diseño 14.4.2): ``storage`` (CMK para cifrar las colas).
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from config.environments import EnvConfig
from stacks.storage_stack import StorageStack

# Retención de mensajes de las colas, en días. El criterio 9.10 la fija en 14
# días para las DLQs; la Cola_Solicitudes usa la misma por consistencia (el
# máximo que admite SQS es 14 días).
_MESSAGE_RETENTION_DAYS = 14

# Nombre lógico de negocio de la Cola_Solicitudes (diseño 2.2 / 4.2). Se compone
# con el prefijo ``{proyecto}-{env}`` para obtener un nombre único por entorno.
_REQUESTS_QUEUE_SUFFIX = "analysis-requests"
_REQUESTS_DLQ_SUFFIX = "analysis-requests-dlq"
_ITEMS_DLQ_SUFFIX = "analysis-items-dlq"


class IngestStack(Stack):
    """Stack del dominio de buffer de ingesta: Cola_Solicitudes y dos DLQs.

    Expone las colas como atributos públicos tipados con la **interfaz** de CDK
    (:class:`aws_cdk.aws_sqs.IQueue`, diseño 14.4.3), de modo que los stacks
    dependientes las referencien sin acoplarse a la implementación concreta:

    - ``compute`` (tarea 12.2) concede a ``aggregate_results`` permiso para
      publicar en la :attr:`items_dlq` los Ítems fallidos.
    - ``workflow`` (tarea 12.4) conecta el EventBridge Pipe a la
      :attr:`requests_queue` (source) y las alarmas de la DLQ se definen en
      ``observability`` (tarea 13.5) a partir de :attr:`requests_dlq`.

    Cada stack consumidor concede sus propios permisos de mínimo privilegio con
    ``grant_*`` sobre estas colas (criterios 15.10 / 15.11).

    Attributes:
        requests_queue: La Cola_Solicitudes ``analysis-requests``
            (:class:`aws_cdk.aws_sqs.IQueue`), con *redrive* a
            :attr:`requests_dlq` a las 3 recepciones.
        requests_dlq: La DLQ_Solicitudes
            (:class:`aws_cdk.aws_sqs.IQueue`).
        items_dlq: La DLQ_Items
            (:class:`aws_cdk.aws_sqs.IQueue`).

    Args:
        scope: Construct padre (la ``App`` de CDK).
        construct_id: Identificador lógico del stack dentro de la app.
        config: Configuración del entorno de despliegue.
        storage: Stack de almacenamiento del que depende (fuente de la CMK).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        storage: StorageStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._config = config
        self._storage = storage
        self.add_dependency(storage)

        prefix = f"{config.project}-{config.env_name}"

        # Las DLQs se crean primero: la Cola_Solicitudes las referencia en su
        # política de redrive, así que deben existir antes.
        requests_dlq = self._create_queue(
            "RequestsDlq", f"{prefix}-{_REQUESTS_DLQ_SUFFIX}"
        )
        self.requests_dlq: sqs.IQueue = requests_dlq

        self.items_dlq: sqs.IQueue = self._create_queue(
            "ItemsDlq", f"{prefix}-{_ITEMS_DLQ_SUFFIX}"
        )

        self.requests_queue: sqs.IQueue = self._create_requests_queue(
            f"{prefix}-{_REQUESTS_QUEUE_SUFFIX}", requests_dlq
        )

    def _create_queue(self, construct_id: str, queue_name: str) -> sqs.Queue:
        """Crea una cola SQS cifrada con la CMK y con retención de 14 días.

        Molde común de las dos DLQs: cifrado en reposo con la CMK del proyecto
        (criterio 15.6), retención de mensajes de 14 días (criterio 9.10) y
        denegación de todo acceso sin TLS (``enforce_ssl``, defensa en profundidad
        coherente con el ``Bucket_Analisis``). No es un recurso con estado: se
        destruye con el stack (``RemovalPolicy.DESTROY``), pues una cola vacía
        recreada no supone pérdida de datos de negocio.

        Args:
            construct_id: Identificador lógico del recurso dentro del stack.
            queue_name: Nombre de la cola, ya prefijado por ``{proyecto}-{env}``.

        Returns:
            La :class:`aws_cdk.aws_sqs.Queue` creada.
        """

        return sqs.Queue(
            self,
            construct_id,
            queue_name=queue_name,
            # SSE-KMS con la CMK del proyecto (criterio 15.6). CUSTOMER_MANAGED
            # exige una clave explícita, a diferencia del cifrado gestionado por SQS.
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self._storage.key,
            # Las DLQs retienen 14 días para permitir inspección y reproceso
            # (criterio 9.10).
            retention_period=Duration.days(_MESSAGE_RETENTION_DAYS),
            # Deniega cualquier operación cuyo contexto `aws:SecureTransport` sea
            # `false` (defensa en profundidad, coherente con 15.7 del bucket).
            enforce_ssl=True,
            # Buffer transitorio: la cola puede recrearse sin pérdida de datos de
            # negocio (la verdad vive en DynamoDB, diseño 2.1.3).
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_requests_queue(
        self, queue_name: str, dead_letter_queue: sqs.IQueue
    ) -> sqs.Queue:
        """Crea la Cola_Solicitudes con visibility 180 s y redrive a la DLQ.

        A diferencia de las DLQs, lleva un ``visibility_timeout`` igual a
        ``queue_visibility_timeout_seconds`` (180 s) y una política de *redrive*
        que traslada el mensaje a la DLQ_Solicitudes al alcanzar
        ``queue_max_receive_count`` (3) recepciones sin eliminarse (criterios 9.8
        y 5.14). Ambos valores vienen de :class:`EnvConfig`; el criterio 10.12
        (validado en ``EnvConfig.__post_init__``) asegura que el umbral del sweeper
        cubre esta ventana de reintentos (3 × 180 s + margen).

        Args:
            queue_name: Nombre de la cola, ya prefijado por ``{proyecto}-{env}``.
            dead_letter_queue: La DLQ_Solicitudes destino del *redrive*.

        Returns:
            La :class:`aws_cdk.aws_sqs.Queue` creada.
        """

        return sqs.Queue(
            self,
            "RequestsQueue",
            queue_name=queue_name,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=self._storage.key,
            retention_period=Duration.days(_MESSAGE_RETENTION_DAYS),
            enforce_ssl=True,
            # Visibility timeout de 180 s: ventana durante la que un mensaje en
            # vuelo (una ejecución de orquestación en arranque) queda invisible
            # para otros consumidores (criterio del diseño; 10.12).
            visibility_timeout=Duration.seconds(
                self._config.queue_visibility_timeout_seconds
            ),
            # Redrive: tras `maxReceiveCount` recepciones sin eliminarse, el
            # mensaje va a la DLQ_Solicitudes (criterio 9.8).
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=self._config.queue_max_receive_count,
                queue=dead_letter_queue,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
