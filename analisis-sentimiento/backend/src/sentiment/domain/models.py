"""Modelo de dominio puro del pipeline de análisis de sentimiento.

Este módulo es el núcleo de negocio de la capa ``domain/``. Define los enums de
estado, los conjuntos de estados terminales y las dataclasses que representan un
Lote (``AnalysisMeta``), sus Ítems (``AnalysisItem``) y los resultados de los dos
análisis (clásico con Comprehend y profundo con Bedrock).

Invariantes de diseño:

* **Pureza (Requirement 17.2):** el módulo SHALL ser importable sin que el SDK de
  AWS (``boto3``) esté instalado. No importa adaptadores ni ningún servicio de
  infraestructura; sólo usa la biblioteca estándar. Esto permite probar el dominio
  de forma aislada y mantiene la frontera hexagonal (``handlers`` → ``services`` →
  ``domain``) sin dependencias invertidas.
* **Grafía ``indificador``:** la grafía del campo ``indificador`` se conserva
  deliberadamente por compatibilidad con el prompt y los datos existentes
  (ver glosario de ``requirements.md``). No es un error tipográfico.
* **``Decimal`` en vez de ``float``:** los scores de Comprehend se modelan con
  ``Decimal`` porque DynamoDB no acepta ``float`` y se persisten tal cual.

Las dataclasses de resultados (``SurveyAnswer``, ``SentimentScores``,
``ClassicSentiment``, ``EmotionAnalysis``) son inmutables (``frozen=True``) por
ser valores. Las proyecciones mutables del estado del Lote (``AnalysisItem``,
``AnalysisMeta``) no son ``frozen`` porque sus contadores y estado evolucionan a
lo largo del procesamiento.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class AnalysisStatus(StrEnum):
    """Estado del Lote completo.

    ``PENDING`` y ``RUNNING`` son estados no terminales; el resto son terminales
    (ver :data:`TERMINAL_ANALYSIS_STATUSES`). El Lote avanza en un solo sentido
    desde la siembra hasta un estado terminal.
    """

    PENDING = "PENDING"  # sembrado, orquestacion no iniciada
    RUNNING = "RUNNING"  # Distributed Map en curso
    COMPLETED = "COMPLETED"  # todos los items DONE
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"  # al menos 1 item FAILED
    FAILED = "FAILED"  # fallo estructural del lote
    EXPIRED = "EXPIRED"  # abandonado, cerrado por el sweeper


# Estado_Terminal (Lote): un Lote en cualquiera de estos estados no vuelve a
# cambiar. Es la condición de parada de la orquestación y la que habilita la
# notificación de fin de proceso y la descarga del Documento_Consolidado.
TERMINAL_ANALYSIS_STATUSES: frozenset[AnalysisStatus] = frozenset(
    {
        AnalysisStatus.COMPLETED,
        AnalysisStatus.COMPLETED_WITH_ERRORS,
        AnalysisStatus.FAILED,
        AnalysisStatus.EXPIRED,
    }
)


class ItemStatus(StrEnum):
    """Estado de un Ítem individual.

    Sólo ``PENDING`` es no terminal. ``SKIPPED`` se usa cuando la respuesta está
    vacía o es un marcador sin contenido analizable (p.ej. "Sin comentarios"):
    el Ítem no se envía a los modelos pero cuenta como procesado.
    """

    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # respuesta vacia o "Sin comentarios": no se analiza


# Estado_Terminal (Ítem): un Ítem en cualquiera de estos estados ya fue procesado
# y reintentarlo es idempotente (no se vuelve a invocar a los modelos).
TERMINAL_ITEM_STATUSES: frozenset[ItemStatus] = frozenset(
    {
        ItemStatus.DONE,
        ItemStatus.FAILED,
        ItemStatus.SKIPPED,
    }
)


class SentimientoGeneral(StrEnum):
    """Etiqueta de sentimiento general en español.

    Es el equivalente tipado del mapeo que el código legado hacía con un ``dict``
    con fallback. Traduce las cuatro etiquetas de Amazon Comprehend al español y
    reserva ``NO_DETERMINADO`` para cualquier etiqueta desconocida o ausente.
    """

    POSITIVO = "Positivo"
    NEGATIVO = "Negativo"
    NEUTRAL = "Neutral"
    MIXTO = "Mixto"
    NO_DETERMINADO = "No determinado"

    @classmethod
    def from_comprehend(cls, raw: str) -> SentimientoGeneral:
        """Traduce la etiqueta de Comprehend a su equivalente en español.

        Mapea ``POSITIVE``/``NEGATIVE``/``NEUTRAL``/``MIXED`` a sus etiquetas en
        español. Cualquier otro valor (etiqueta desconocida, cadena vacía) se
        traduce a :attr:`NO_DETERMINADO`. Esta función es total y nunca lanza:
        garantiza que un valor inesperado de Comprehend no rompa el pipeline
        (Requirement 6.3).

        Args:
            raw: etiqueta ``Sentiment`` devuelta por la API de Comprehend.

        Returns:
            El miembro de :class:`SentimientoGeneral` correspondiente, o
            :attr:`NO_DETERMINADO` si la etiqueta no es reconocida.
        """
        return {
            "POSITIVE": cls.POSITIVO,
            "NEGATIVE": cls.NEGATIVO,
            "NEUTRAL": cls.NEUTRAL,
            "MIXED": cls.MIXTO,
        }.get(raw, cls.NO_DETERMINADO)


@dataclass(frozen=True, slots=True)
class SurveyAnswer:
    """Una respuesta de encuesta tal como llega del cliente, ya validada.

    Es un valor inmutable: representa la entrada canonicalizada de un Ítem antes
    de que se le asigne un ``item_id``.

    Attributes:
        indificador: categoría de la pregunta. Garantizado miembro del enum
            cerrado de Indificadores por la validación previa de la API.
        respuesta: texto de la respuesta, entre 1 y 5000 bytes UTF-8, ya
            normalizado a Unicode NFC y con los espacios inicial y final
            recortados.
        index: posición 0-based del Ítem en el array ``items`` del payload
            original.
    """

    indificador: str  # garantizado miembro de INDIFICADORES
    respuesta: str  # 1..5000 bytes UTF-8, ya normalizado (NFC, trim)
    index: int  # posicion 0-based en el array original


@dataclass(frozen=True, slots=True)
class SentimentScores:
    """Los cuatro scores de confianza de Amazon Comprehend.

    Se usan ``Decimal`` y no ``float`` porque DynamoDB no acepta ``float`` y estos
    valores se persisten directamente en la Dynamo_Table.

    Attributes:
        positive: confianza de la clase positiva, en el rango [0, 1].
        negative: confianza de la clase negativa, en el rango [0, 1].
        neutral: confianza de la clase neutral, en el rango [0, 1].
        mixed: confianza de la clase mixta, en el rango [0, 1].
    """

    positive: Decimal
    negative: Decimal
    neutral: Decimal
    mixed: Decimal


@dataclass(frozen=True, slots=True)
class ClassicSentiment:
    """Resultado del análisis clásico (Comprehend) de un Ítem.

    Attributes:
        item_id: identificador del Ítem al que pertenece este resultado.
        language_code: idioma detectado en ISO 639-1 (p.ej. ``"es"``).
        language_confidence: confianza de la detección de idioma, en [0, 1].
        sentimiento_general: etiqueta de sentimiento general en español.
        scores: los cuatro scores de confianza de sentimiento.
    """

    item_id: str
    language_code: str  # ISO 639-1, p.ej. "es"
    language_confidence: Decimal
    sentimiento_general: SentimientoGeneral
    scores: SentimentScores


@dataclass(frozen=True, slots=True)
class EmotionAnalysis:
    """Resultado del análisis profundo (Bedrock) de un Ítem.

    Attributes:
        item_id: identificador del Ítem al que pertenece este resultado.
        sentimientos: tupla deduplicada de emociones concretas; puede estar
            vacía si el texto no admite clasificación fina.
        explicacion: justificación en lenguaje natural; cadena vacía (``""``)
            cuando ``sentimientos`` está vacía.
        model_id: identificador del Modelo_Bedrock que produjo el resultado.
        input_tokens: tokens de entrada consumidos por la invocación.
        output_tokens: tokens de salida generados por la invocación.
    """

    item_id: str
    sentimientos: tuple[str, ...]  # deduplicado, puede estar vacio
    explicacion: str  # "" si sentimientos esta vacio
    model_id: str
    input_tokens: int
    output_tokens: int


@dataclass(slots=True)
class AnalysisItem:
    """Proyección completa de un Ítem, tal como se persiste y se exporta.

    Mutable: su ``status`` y resultados evolucionan a lo largo del procesamiento.
    Agrega la entrada del Ítem con los resultados de ambos análisis y el estado
    de error, si lo hubo.

    Attributes:
        analysis_id: ULID de 26 caracteres del Lote al que pertenece.
        item_id: ``{analysis_id}#{index:05d}``, derivado de forma determinista.
        index: posición 0-based del Ítem en el array original.
        indificador: categoría de la pregunta (grafía conservada).
        respuesta: texto normalizado de la respuesta.
        status: estado del Ítem (ver :class:`ItemStatus`).
        classic: resultado del análisis clásico, o ``None`` si no se produjo.
        emotions: resultado del análisis profundo, o ``None`` si no se produjo.
        error_code: ``error_code`` del Catalogo_De_Errores si el Ítem falló, o
            ``None`` en caso contrario.
        attempt_count: número de intentos de procesamiento acumulados.
        processed_at: instante UTC en que el Ítem alcanzó un estado terminal, o
            ``None`` si aún no lo ha alcanzado.
    """

    analysis_id: str
    item_id: str
    index: int
    indificador: str
    respuesta: str
    status: ItemStatus
    classic: ClassicSentiment | None = None
    emotions: EmotionAnalysis | None = None
    error_code: str | None = None
    attempt_count: int = 0
    processed_at: datetime | None = None


@dataclass(slots=True)
class AnalysisMeta:
    """Registro META del Lote: la fuente de verdad de su estado.

    Mutable: ``status`` y los contadores atómicos (``done_count``,
    ``failed_count``, ``skipped_count``) evolucionan durante la orquestación.

    Attributes:
        analysis_id: ULID de 26 caracteres que identifica el Lote.
        tenant_id: organización propietaria; frontera de aislamiento de datos.
        owner_sub: ``sub`` del usuario que creó el Lote.
        status: estado del Lote (ver :class:`AnalysisStatus`).
        item_count: número total de Ítems del Lote.
        done_count: contador atómico de Ítems en estado ``DONE``.
        failed_count: contador atómico de Ítems en estado ``FAILED``.
        skipped_count: contador atómico de Ítems en estado ``SKIPPED``.
        input_s3_key: clave S3 del payload canonicalizado de entrada.
        output_s3_key: clave S3 del Documento_Consolidado, o ``None`` si aún no
            se ha generado.
        execution_arn: ARN de la ejecución de Step Functions, o ``None``.
        created_at: instante UTC de creación del Lote.
        updated_at: instante UTC de la última actualización del registro.
        completed_at: instante UTC en que el Lote alcanzó un estado terminal, o
            ``None`` si aún no lo ha alcanzado.
        schema_version: versión del esquema del registro, para evolución futura.
    """

    analysis_id: str
    tenant_id: str
    owner_sub: str
    status: AnalysisStatus
    item_count: int
    done_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    input_s3_key: str = ""
    output_s3_key: str | None = None
    execution_arn: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    schema_version: int = 1

    @property
    def is_terminal(self) -> bool:
        """Indica si el Lote está en un estado terminal.

        Returns:
            ``True`` si ``status`` pertenece a
            :data:`TERMINAL_ANALYSIS_STATUSES`; ``False`` en caso contrario.
        """
        return self.status in TERMINAL_ANALYSIS_STATUSES

    @property
    def progress_ratio(self) -> float:
        """Fracción de Ítems procesados, acotada al rango ``0.0 .. 1.0``.

        Se calcula como ``(done + failed + skipped) / item_count``. Un Lote sin
        Ítems (``item_count == 0``) se considera completo y devuelve ``1.0``,
        evitando además la división por cero. El resultado se acota con ``min``
        a ``1.0`` para que, aun con contadores transitoriamente inconsistentes,
        la invariante "nunca mayor que 1.0" se mantenga (Requirement 11.2).

        Returns:
            Un ``float`` en el intervalo cerrado ``[0.0, 1.0]``.
        """
        if self.item_count == 0:
            return 1.0
        processed = self.done_count + self.failed_count + self.skipped_count
        return min(processed / self.item_count, 1.0)
