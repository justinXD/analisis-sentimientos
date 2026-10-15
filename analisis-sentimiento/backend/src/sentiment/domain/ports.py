"""Puertos del dominio (interfaces hexagonales).

Este módulo define los *puertos* del núcleo de la aplicación: las interfaces
que los servicios (`services/`) consumen y que los adaptadores (`adapters/`)
implementan. Siguiendo la arquitectura hexagonal (sección 2.1 del diseño), el
dominio depende de estas abstracciones, nunca de implementaciones concretas ni
del SDK de AWS; la inversión de dependencias se materializa inyectando una
implementación de cada puerto en el constructor de cada servicio.

Los puertos se expresan con ``typing.Protocol`` (tipado estructural): un
adaptador satisface un puerto por tener los métodos con la firma esperada, sin
necesidad de heredar explícitamente. Esto permite escribir dobles de prueba
("fakes") en memoria que cumplen el contrato sin acoplarse a boto3, y mantiene
el dominio importable sin el SDK de AWS instalado (Requirements 17.1 y 17.2).

Postcondiciones y contrato
--------------------------
Cada método documenta sus postcondiciones en el docstring. El contrato es
normativo: las implementaciones (``ComprehendAdapter``, ``BedrockAdapter``,
``DynamoRepository``, ``S3Storage``, etc.) y los servicios que dependen de los
puertos deben respetarlo. Las propiedades de correctitud del diseño (P1-P12) se
apoyan en estas postcondiciones (p. ej. la conservación de ítems de P1/P10 se
sostiene sobre la postcondición de longitud de :meth:`SentimentDetector.detect_languages`).

Notas de tipado
---------------
``from __future__ import annotations`` convierte todas las anotaciones en
cadenas (PEP 563), por lo que los tipos del modelo de dominio se referencian de
forma perezosa. Las importaciones de los modelos viven bajo
``TYPE_CHECKING``: las evalúa el verificador estático (mypy ``--strict``), pero
nunca en tiempo de ejecución. Así este módulo tipa correctamente y se importa
sin error aunque ``domain/models.py`` y ``domain/errors.py`` aún no existan
(se desarrollan en la misma oleada), y sin arrastrar dependencia alguna de AWS.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from sentiment.domain.models import (
        AnalysisItem,
        AnalysisMeta,
        AnalysisStatus,
        ClassicSentiment,
        EmotionAnalysis,
        SurveyAnswer,
    )

__all__ = [
    "AnalysisRepository",
    "AuditLog",
    "EventPublisher",
    "ExecutionInspector",
    "LlmAnalyzer",
    "ObjectStorage",
    "PurgeRepository",
    "PurgeStorage",
    "QueuePublisher",
    "SentimentDetector",
]


@runtime_checkable
class SentimentDetector(Protocol):
    """Puerto del análisis clásico (idioma + sentimiento). Lo implementa ``ComprehendAdapter``.

    Representa el acceso al análisis por lotes de Amazon Comprehend. El puerto
    expone dos operaciones batch porque Comprehend requiere agrupar textos del
    mismo idioma y asocia resultados por ``Index`` dentro de cada llamada.
    """

    def detect_languages(self, texts: list[str]) -> list[str]:
        """Detecta el idioma dominante de cada texto, en lote.

        Parámetros:
            texts: lista de textos a analizar, en el orden original del lote.

        Devuelve:
            La lista de ``language_code`` (ISO 639-1, p. ej. ``"es"``) del
            idioma dominante de cada texto, en el **mismo orden** que la entrada.

        Postcondiciones:
            - ``len(resultado) == len(texts)`` (conservación de longitud): la
              salida nunca omite ni duplica posiciones. Esta postcondición es la
              base de la conservación de ítems de la capa batch (Property 10).
            - Todo texto cuyo idioma no se pudo determinar recibe el idioma de
              *fallback* configurado (``es``), **no** se omite ni produce ``None``.
            - La correspondencia es posicional: ``resultado[i]`` corresponde a
              ``texts[i]``.

        Errores:
            Puede propagar ``TransientDependencyError`` (clase transitoria) ante
            un fallo de disponibilidad de Comprehend; el reintento lo gestiona la
            capa de orquestación, no este puerto.
        """
        ...

    def detect_sentiments(
        self, texts: list[str], language_code: str
    ) -> tuple[dict[int, ClassicSentiment], dict[int, str]]:
        """Analiza el sentimiento de hasta 25 textos del MISMO idioma, en una llamada batch.

        Parámetros:
            texts: hasta 25 textos, todos en el idioma ``language_code``.
            language_code: código ISO 639-1 compartido por todos los textos.

        Devuelve:
            Una tupla ``(exitos_por_indice, errores_por_indice)`` donde:
            - ``exitos_por_indice`` mapea el ``Index`` devuelto por Comprehend a
              su :class:`~sentiment.domain.models.ClassicSentiment`.
            - ``errores_por_indice`` mapea el ``Index`` a un ``error_code`` del
              Catalogo_De_Errores para los textos que Comprehend reportó en su
              ``ErrorList``.

        Postcondiciones:
            - Las claves de ambos diccionarios son los ``Index`` devueltos por la
              API, **no** la posición en ``texts``. La asociación es por ``Index``,
              lo que preserva la correspondencia aunque la API reordene.
            - Las claves de ``exitos_por_indice`` y ``errores_por_indice`` son
              disjuntas: un índice aparece como éxito o como error, nunca en ambos.
            - La unión de las claves de ambos diccionarios **puede no cubrir**
              ``range(len(texts))``: si la API omite un resultado, ese índice no
              aparecerá en ninguno. Es responsabilidad del servicio llamante
              clasificar cada índice ausente como ``COMPREHEND_RESULT_MISSING``
              (criterio 6.13), de modo que ningún ítem se pierda silenciosamente.

        Errores:
            Puede propagar ``TransientDependencyError`` ante indisponibilidad de
            Comprehend. Un texto que excede el límite de Comprehend o cuyo idioma
            no soporta se reporta como entrada de ``errores_por_indice``
            (``COMPREHEND_ITEM_FAILED``), no como excepción.
        """
        ...


@runtime_checkable
class LlmAnalyzer(Protocol):
    """Puerto del análisis profundo de emociones. Lo implementa ``BedrockAdapter``.

    Encapsula una invocación al Modelo_Bedrock para clasificar emociones y
    producir una explicación a partir de una respuesta de encuesta.
    """

    def analyze(self, indificador: str, respuesta: str) -> EmotionAnalysis:
        """Analiza una sola respuesta con una ÚNICA invocación del modelo, sin reintentos internos.

        Parámetros:
            indificador: el indificador (categoría) de la respuesta; miembro de
                la lista única de Indificadores.
            respuesta: el texto de la respuesta, ya validado y normalizado
                (NFC, trim). Es dato sensible: no debe aparecer en logs.

        Devuelve:
            Un :class:`~sentiment.domain.models.EmotionAnalysis` **siempre**
            válido. A diferencia del ``invoke_llm`` legado (que devolvía ``None``
            y obligaba al llamador a adivinar), este puerto es total en su salida.

        Postcondiciones:
            - **Nunca** devuelve ``None``: todo camino de éxito produce un
              ``EmotionAnalysis`` cuyo ``sentimientos`` está normalizado
              (trim, minúsculas, sin inválidos, deduplicado, tope de 10) y cuya
              ``explicacion`` es ``""`` si ``sentimientos`` está vacío.
            - Realiza como máximo **una** llamada al modelo; la política de
              reintentos vive en la máquina de estados, no aquí.
            - No emite PII (``respuesta`` ni ``explicacion``) a los logs; sí puede
              publicar como métrica el número de tokens de entrada/salida.

        Errores:
            - ``ThrottledError`` / ``TransientDependencyError`` (clase
              **transitoria**): el modelo está limitado o indisponible; la
              orquestación reintentará con backoff.
            - ``LlmInputRejectedError`` (clase **permanente**,
              ``LLM_INPUT_REJECTED``): el modelo rechaza la entrada por tamaño o
              por no ser procesable; no se reintenta.
            - ``LlmSchemaError`` (clase **permanente**, ``LLM_OUTPUT_INVALID``):
              la salida del modelo no valida contra el JSON_Schema ``llm_output``.
        """
        ...


@runtime_checkable
class AnalysisRepository(Protocol):
    """Puerto de persistencia del lote y sus ítems. Lo implementa ``DynamoRepository``.

    Modela el acceso a la Dynamo_Table de tabla única (single-table). La verdad
    del estado del lote vive aquí; los contadores (``done_count``,
    ``failed_count``, ``skipped_count``) del registro META se actualizan de forma
    atómica. Las transiciones de ítem son idempotentes para tolerar la entrega
    "al menos una vez" de la orquestación.
    """

    def create_analysis(self, meta: AnalysisMeta, answers: list[SurveyAnswer]) -> None:
        """Crea el registro META y los N registros ITEM del lote.

        Postcondiciones:
            - Tras retornar, existe exactamente un META con ``status == PENDING``
              e ``item_count == len(answers)``, y exactamente ``len(answers)``
              ITEM en ``PENDING`` con ``index`` recorriendo ``range(len(answers))``
              sin huecos ni repeticiones (base de la conservación de ítems, P1).
            - Las escrituras por lotes se realizan en tandas de 25
              (``BatchWriteItem``). Si una tanda falla, el servicio llamante
              decide el cierre del META como ``FAILED`` (criterio 4.11); este
              puerto no deja estados intermedios silenciosos.
        """
        ...

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        """Lee el registro META de un lote, acotado por ``tenant_id`` (aislamiento multi-tenant).

        Postcondiciones:
            - Devuelve el META solo si pertenece al ``tenant_id`` dado.
            - Si no existe o pertenece a otro tenant, propaga el error de "no
              encontrado" del dominio; el llamante lo traduce a un ``404``
              **idéntico** en ambos casos, sin filtrar la existencia ajena
              (aislamiento entre tenants, Property 6).
        """
        ...

    def list_by_tenant(
        self, tenant_id: str, limit: int, cursor: str | None
    ) -> tuple[list[AnalysisMeta], str | None]:
        """Lista los lotes de un tenant con paginación por cursor opaco.

        Parámetros:
            limit: tope de elementos por página (acota el tamaño de respuesta).
            cursor: cursor opaco de la página anterior, o ``None`` para la primera.

        Postcondiciones:
            - Devuelve ``(metas, next_cursor)``; ``next_cursor is None`` cuando no
              hay más páginas.
            - Solo incluye lotes del ``tenant_id`` dado (aislamiento multi-tenant).
            - La consulta está acotada por ``limit``: nunca carga el conjunto
              completo en memoria.
        """
        ...

    def get_item(self, analysis_id: str, item_id: str) -> AnalysisItem:
        """Lee un único registro ITEM de un Lote por su ``item_id``.

        La relectura del ITEM es el primer paso del análisis profundo
        (``analyze_item_emotions``): aporta el ``indificador`` y la ``respuesta``
        que se envían al Modelo_Bedrock y el ``status`` sobre el que se decide el
        guard de idempotencia, y **debe** ocurrir antes de cualquier invocación al
        modelo (criterio 7.2). Se espera una lectura fuertemente consistente
        (``GetItem`` con ``ConsistentRead``), para que un reintento observe el
        estado terminal que escribió un intento anterior y no vuelva a invocar al
        modelo.

        Parámetros:
            analysis_id: ULID del Lote al que pertenece el ITEM.
            item_id: ``{analysis_id}#{index:05d}`` del ITEM a leer.

        Devuelve:
            El :class:`~sentiment.domain.models.AnalysisItem` persistido, con su
            ``status`` y, al menos, ``indificador`` y ``respuesta`` poblados.

        Postcondiciones:
            - Devuelve el ITEM únicamente si existe; es una lectura pura que no
              modifica ningún registro.
            - La lectura es fuertemente consistente: tras una escritura terminal
              de ese ITEM, una lectura posterior la refleja (base del guard de
              idempotencia de P2).

        Errores:
            - Propaga ``NotFoundError`` si no existe un ITEM para ese
              ``(analysis_id, item_id)``. Un fallo de lectura de la dependencia se
              propaga tal cual (``TransientDependencyError`` u otra): en ambos
              casos el servicio llamante **no** invoca al Modelo_Bedrock ni escribe
              ningún campo (criterio 7.14).
        """
        ...

    def save_classic_results(self, results: list[ClassicSentiment]) -> None:
        """Persiste los resultados del análisis clásico (Comprehend) de varios ítems.

        Postcondiciones:
            - Cada :class:`~sentiment.domain.models.ClassicSentiment` se asocia a
              su ITEM por ``item_id``.
            - Los scores se persisten como ``Decimal`` (DynamoDB no admite
              ``float``).
        """
        ...

    def complete_item(
        self, analysis_id: str, item_id: str, emotions: EmotionAnalysis
    ) -> bool:
        """Pasa un ITEM a ``DONE`` e incrementa ``done_count`` de forma atómica e idempotente.

        Implementación esperada: ``TransactWriteItems`` que condiciona la
        transición del ITEM a estar en ``PENDING`` y hace ``ADD`` al contador del
        META en la misma transacción.

        Devuelve:
            ``True`` si aplicó la transición (el ITEM estaba en ``PENDING``);
            ``False`` si el ITEM ya era terminal (reproceso idempotente).

        Postcondiciones:
            - **Idempotente**: una segunda llamada con el mismo ``item_id`` no
              vuelve a incrementar ``done_count`` y devuelve ``False``. La
              ``ConditionalCheckFailedException`` se trata como éxito idempotente,
              no como error (base de P2 y de la reconciliación de contadores P11).
            - Si devuelve ``True``, ``done_count`` del META refleja exactamente un
              incremento por este ítem.
        """
        ...

    def fail_item(self, analysis_id: str, item_id: str, error_code: str) -> bool:
        """Pasa un ITEM a ``FAILED`` con un ``error_code`` e incrementa ``failed_count`` (atómico).

        Mismo contrato idempotente que :meth:`complete_item`:

        Devuelve:
            ``True`` si aplicó la transición desde ``PENDING``; ``False`` si el
            ITEM ya era terminal.

        Postcondiciones:
            - **Idempotente**: no incrementa ``failed_count`` dos veces para el
              mismo ítem.
            - ``error_code`` pertenece al Catalogo_De_Errores.
        """
        ...

    def skip_item(self, analysis_id: str, item_id: str) -> bool:
        """Pasa un ITEM a ``SKIPPED`` e incrementa ``skipped_count`` de forma atómica e idempotente.

        Un Ítem se omite (``SKIPPED``) cuando su ``respuesta`` es un
        Marcador_Vacio: no se envía ni a Comprehend ni al Modelo_Bedrock, pero
        cuenta como procesado (criterios 6.6, 18.6). Comparte el mismo contrato
        transaccional e idempotente que :meth:`complete_item` y :meth:`fail_item`:
        condiciona la transición a ``status == PENDING`` y hace ``ADD`` al contador
        ``skipped_count`` del META en la misma transacción. A diferencia de
        ``fail_item``, no registra ningún ``error_code`` (un Ítem omitido no es un
        fallo).

        Devuelve:
            ``True`` si aplicó la transición desde ``PENDING``; ``False`` si el
            ITEM ya era terminal (reproceso idempotente).

        Postcondiciones:
            - **Idempotente**: no incrementa ``skipped_count`` dos veces para el
              mismo ítem.
            - El ITEM queda sin ``error_code`` y sin resultados de análisis.
        """
        ...

    def iter_items(self, analysis_id: str) -> Iterator[AnalysisItem]:
        """Itera TODOS los ITEM de un lote paginando internamente.

        Postcondiciones:
            - Es un generador: pagina la consulta y **no** carga el lote completo
              en memoria (seguro para lotes de hasta 1.000 ítems).
            - Produce exactamente un :class:`~sentiment.domain.models.AnalysisItem`
              por cada ITEM persistido del lote.
        """
        ...

    def finalize_analysis(
        self,
        analysis_id: str,
        status: AnalysisStatus,
        counts: tuple[int, int, int],
        output_s3_key: str | None,
    ) -> None:
        """Cierra el META en un estado terminal con los recuentos finales y el puntero de salida.

        Parámetros:
            status: estado terminal (``COMPLETED`` / ``COMPLETED_WITH_ERRORS`` /
                ``FAILED`` / ``EXPIRED``).
            counts: tripleta ``(done_count, failed_count, skipped_count)`` contada
                desde los ITEM reales (no desde el acumulado), para reconciliar.
            output_s3_key: clave del Documento_Consolidado en S3, o ``None`` si no
                se produjo salida.

        Postcondiciones:
            - El META queda en ``status`` con ``done_count + failed_count +
              skipped_count == item_count`` (conservación de ítems, P1; conteo
              exacto, P11).
            - ``output_s3_key`` y ``completed_at`` quedan registrados **antes** de
              que se publique el Evento_Finalizacion.
        """
        ...

    def mark_analysis_failed(self, analysis_id: str, error_code: str) -> None:
        """Marca el META como ``FAILED`` con un ``error_code``, condicionado a no ser ya terminal.

        Postcondiciones:
            - Transición a ``FAILED`` solo si el META no estaba ya en estado
              terminal (idempotente: no sobrescribe un cierre previo).
            - ``error_code`` pertenece al Catalogo_De_Errores (criterio 4.11).
        """
        ...

    def mark_event_published(self, analysis_id: str) -> None:
        """Registra ``event_published_at`` y retira el META del índice de barrido (SWEEP).

        Postcondiciones:
            - Fija ``event_published_at``, lo que saca al META del índice sparse
              GSI2 (``SWEEP``) y evita que el sweeper lo reconsidere.
        """
        ...

    def list_sweep_candidates(self, older_than: datetime) -> Iterator[AnalysisMeta]:
        """Itera los candidatos a barrido: lotes no terminales, o terminales sin evento publicado.

        Implementación esperada: ``Query`` sobre GSI2 con ``gsi2pk == SWEEP`` y
        ``gsi2sk < older_than``.

        Postcondiciones:
            - Es un generador: no carga todos los candidatos en memoria.
            - Solo produce META cuyo ``gsi2sk`` es anterior a ``older_than`` y que
              siguen en el índice SWEEP (no terminales, o terminales a los que aún
              no se les publicó el Evento_Finalizacion).
        """
        ...


@runtime_checkable
class ObjectStorage(Protocol):
    """Puerto de almacenamiento de objetos JSON. Lo implementa ``S3Storage``.

    Modela la lectura/escritura de documentos JSON en el Bucket_Analisis
    (prefijos ``inputs/``, ``outputs/``, ``map-results/``) y la emisión de
    Presigned_URL de descarga.
    """

    def put_json(self, key: str, payload: object) -> None:
        """Serializa ``payload`` a JSON y lo escribe en el objeto ``key``.

        Postcondiciones:
            - Tras retornar, existe un objeto en ``key`` con el contenido JSON de
              ``payload`` (cifrado en reposo según la configuración del bucket).
            - La escritura es la unidad atómica: o el objeto queda escrito, o se
              propaga el error (no deja objetos parciales).
        """
        ...

    def get_json(self, key: str) -> object:
        """Lee el objeto ``key`` y lo deserializa desde JSON.

        Postcondiciones:
            - Devuelve el objeto Python deserializado del JSON almacenado.
            - Si el objeto no existe, propaga el error de "no encontrado" del
              dominio (el llamante decide la traducción, p. ej. ``404``).
        """
        ...

    def object_exists(self, key: str) -> bool:
        """Comprueba si existe un objeto en ``key`` sin transferir su contenido.

        Operación de *solo metadatos* (``HEAD``): no descarga el cuerpo del
        objeto, por lo que no toca PII (``respuesta``/``explicacion``) ni paga la
        transferencia del Documento_Consolidado. La usa el camino de descarga
        para distinguir el caso del criterio 12.5 —el ``META`` referencia un
        ``output_s3_key`` pero el objeto ya no existe en el Bucket_Analisis (p.
        ej. lo expiró el ciclo de vida)— del caso de éxito, **antes** de emitir
        la Presigned_URL.

        Parámetros:
            key: clave exacta del objeto a comprobar.

        Devuelve:
            ``True`` si el objeto existe en el bucket; ``False`` si no existe.

        Postcondiciones:
            - No modifica ningún objeto ni emite ninguna Presigned_URL.
            - ``False`` significa inequívocamente "no existe ese objeto"; el
              llamante lo traduce a un ``404`` idéntico al de un recurso
              inexistente (criterio 12.5), sin filtrar más información.

        Errores:
            Puede propagar un error de dependencia de S3 distinto de "no
            encontrado" (fallo de red o de permisos); el llamante lo trata como
            fallo interno (HTTP 500), nunca como "no existe".
        """
        ...

    def presign_get(self, key: str, expires_in: int) -> str:
        """Genera una Presigned_URL de solo lectura (GET) acotada a un único objeto.

        Parámetros:
            key: clave exacta del objeto a descargar.
            expires_in: validez de la URL en segundos (el camino de descarga usa
                900 s).

        Devuelve:
            Una URL HTTPS firmada que concede acceso de solo lectura a **ese**
            objeto durante ``expires_in`` segundos.

        Postcondiciones:
            - La URL concede acceso exclusivamente a ``key`` (un objeto), no a un
              prefijo ni al bucket.
            - La URL caduca pasados ``expires_in`` segundos.
        """
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Puerto de publicación del Evento_Finalizacion. Lo implementa ``EventBridgePublisher``."""

    def publish_analysis_completed(self, meta: AnalysisMeta) -> None:
        """Publica el Evento_Finalizacion de un lote en el Bus_Eventos.

        Postcondiciones:
            - Publica un evento con ``detail-type == "analysis.completed"`` y
              ``event_id == "{analysis_id}:{status}"`` (desduplicación por estado
              terminal).
            - El detalle **no** contiene PII ni Presigned_URL; solo
              identificadores, estado y recuentos.
            - Se publica únicamente cuando el META ya está en estado terminal y su
              salida (``output_s3_key``) ya está registrada.
        """
        ...


@runtime_checkable
class ExecutionInspector(Protocol):
    """Puerto de inspección de la ejecución de Step Functions (``StepFunctionsAdapter``).

    Lo usa el sweeper para distinguir lotes realmente abandonados de lotes que
    aún están siendo procesados por una ejecución en curso.
    """

    def describe_status(self, analysis_id: str) -> str | None:
        """Devuelve el estado de la ejecución cuyo nombre es ``analysis_id``.

        Devuelve:
            El estado de la ejecución (p. ej. ``"RUNNING"``, ``"SUCCEEDED"``,
            ``"FAILED"``, ``"TIMED_OUT"``, ``"ABORTED"``), o ``None`` si la
            ejecución no existe (``ExecutionDoesNotExist``).

        Postcondiciones:
            - ``None`` significa inequívocamente "no existe ejecución con ese
              nombre": el sweeper lo interpreta como una de las dos condiciones de
              abandono (criterio 10.11/10.12), nunca como un error.
        """
        ...


@runtime_checkable
class PurgeRepository(Protocol):
    """Puerto de borrado en DynamoDB para la purga de un lote. Lo implementa ``DynamoRepository``.

    Opera con credenciales acotadas al tenant y al lote (sección 12.2.1). El
    borrado es reintentable: dejar la operación a medias no corrompe el estado,
    y una segunda ejecución la completa.
    """

    def get_meta(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        """Lee el META del lote a purgar, acotado por ``tenant_id`` (verificación de ownership).

        Postcondiciones:
            - Devuelve el META solo si pertenece al ``tenant_id`` (misma garantía
              de aislamiento que :meth:`AnalysisRepository.get_meta`).
        """
        ...

    def delete_items(self, analysis_id: str, tenant_id: str) -> int:
        """Borra todos los ITEM del lote.

        Implementación esperada: ``Query`` de las claves seguida de
        ``BatchWriteItem`` con ``DeleteRequest``.

        Devuelve:
            El número de registros ITEM efectivamente eliminados.

        Postcondiciones:
            - Tras retornar, no queda ningún ITEM del lote; el META aún existe
              (se borra al final de la purga, para que la operación sea
              reintentable sin dejar ITEM huérfanos).
            - Es reintentable: volver a invocarla sobre un lote ya sin ITEM
              devuelve ``0`` sin error.
        """
        ...

    def delete_meta(self, analysis_id: str, tenant_id: str) -> None:
        """Borra el registro META del lote. Es el último paso de la purga.

        Postcondiciones:
            - Tras retornar, el lote deja de existir por completo en DynamoDB.
            - Se invoca solo después de haber borrado los ITEM y los objetos de S3.
        """
        ...


@runtime_checkable
class PurgeStorage(Protocol):
    """Puerto de borrado en S3 para la purga de un lote. Lo implementa ``S3Storage``.

    Borra tanto objetos concretos (claves exactas) como todo lo que cuelga de un
    prefijo, incluyendo **todas** las versiones y los marcadores de eliminación
    (el bucket tiene versionado activado: un ``DeleteObject`` simple solo pondría
    un delete marker y dejaría las versiones recuperables).
    """

    def delete_all_versions(self, exact_keys: list[str], prefix: str) -> int:
        """Borra todas las versiones de un conjunto de claves exactas y de todo lo bajo un prefijo.

        Implementación esperada: ``ListObjectVersions`` seguido de
        ``DeleteObjects`` especificando el ``VersionId`` de cada versión y
        marcador.

        Parámetros:
            exact_keys: claves exactas a eliminar por completo (todas sus
                versiones).
            prefix: prefijo bajo el cual se eliminan todos los objetos y sus
                versiones.

        Devuelve:
            El número total de versiones (incluidos marcadores de eliminación)
            eliminadas.

        Postcondiciones:
            - Tras retornar, no queda **ninguna** versión ni marcador de
              eliminación de las ``exact_keys`` ni bajo ``prefix`` (criterio 19.4;
              la purga no deja datos recuperables en S3).
            - Es reintentable: una segunda llamada sin versiones que borrar
              devuelve ``0``.
        """
        ...


@runtime_checkable
class AuditLog(Protocol):
    """Puerto de registro auditable de la purga. Lo implementa ``AuditLog`` (adaptador)."""

    def record_purge_completed(self, **fields: object) -> None:
        """Registra un evento auditable de purga completada, sin PII.

        Parámetros:
            **fields: campos del evento (identificadores como ``analysis_id`` y
                ``tenant_id``, recuentos de registros/versiones eliminados y marca
                de tiempo). **No** debe incluir PII (``respuesta``, ``explicacion``,
                correos, texto de encuesta).

        Postcondiciones:
            - Deja constancia auditable de la purga **solo** tras su éxito.
            - El registro contiene exclusivamente metadatos (identificadores,
              recuentos, marca de tiempo), nunca PII (criterio 19.11).
        """
        ...


@runtime_checkable
class QueuePublisher(Protocol):
    """Puerto de encolado del lote para la orquestación. Lo implementa ``SqsPublisher``.

    Publica en la Cola_Solicitudes el mensaje que, vía EventBridge Pipes, dispara
    la ejecución de Step Functions. Es siempre la **última** escritura de la
    ingesta: encolar significa "todo lo anterior (S3 + DynamoDB) está listo".
    """

    def publish_analysis_queued(self, meta: AnalysisMeta, correlation_id: str) -> None:
        """Encola un mensaje ``AnalysisQueued`` que referencia un lote ya persistido.

        Parámetros:
            meta: el META del lote recién creado; aporta ``analysis_id``,
                ``tenant_id``, el bucket/clave del input (``input_s3_key``) y el
                ``item_count``.
            correlation_id: identificador de correlación para la trazabilidad
                extremo a extremo del lote.

        Postcondiciones:
            - Tras retornar, existe exactamente un mensaje en la Cola_Solicitudes
              que referencia ``meta.analysis_id``, con ``tenant_id``, bucket/clave
              del input, ``item_count`` y ``correlation_id``.
            - El mensaje **no** contiene PII (no viaja el texto de las respuestas,
              solo punteros y metadatos).
            - Debe invocarse únicamente después de que S3 y DynamoDB estén
              escritos, para que la orquestación nunca lea un lote incompleto.
        """
        ...
