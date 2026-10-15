"""Jerarquía de errores del dominio y Catalogo_De_Errores cerrado.

Este módulo define la raíz de excepciones del dominio (:class:`SentimentError`) y
sus familias, de forma que todo ``error_code`` que el Sistema puede devolver a un
cliente o registrar en un registro ITEM o META provenga de una enumeración
**cerrada** (ver el glosario «Catalogo_De_Errores» de ``requirements.md``). El
Sistema no emite ningún ``error_code`` ausente de esta jerarquía.

Invariantes de diseño:

* **Pureza (Requirement 17.2):** el módulo SHALL ser importable sin que el SDK de
  AWS (``boto3``) esté instalado. Sólo usa la biblioteca estándar y no importa
  adaptadores ni servicios de infraestructura; preserva la frontera hexagonal.
* **Catálogo cerrado (glosario Catalogo_De_Errores):** cada subclase fija su
  ``error_code`` a un valor del catálogo. Ninguna subclase introduce un código
  fuera de la enumeración documentada en los requisitos.
* **Dos familias que no se mezclan (glosario, «Separación de roles»):**

  - *Errores de API (HTTP):* cruzan la frontera de la API y se devuelven al
    cliente con un ``http_status``. Nunca se registran como estado interno de un
    Ítem.
  - *Errores de procesamiento (internos):* ``http_status`` es ``None``; nunca se
    devuelven en una respuesta HTTP. Su ``error_code`` se registra en el campo
    ``error_code`` de un registro ITEM o META. (La única exposición permitida es
    el detalle de Ítems fallidos de ``include_failures`` del criterio 11.5, que
    devuelve el ``error_code`` sin PII.)

* **Clasificación transitorio/permanente cerrada (criterios 9.16 y 9.17):** las
  Lambdas de procesamiento no reintentan; propagan el error en su primera
  ocurrencia **indicando su clase** mediante el nombre de la excepción. La
  Maquina_De_Estados compara ese nombre en ``ErrorEquals`` para decidir si
  reintenta (clase :class:`TransientDependencyError`, criterio 9.16) o lo lleva a
  ``FAILED`` sin reintentos (clase :class:`PermanentProcessingError`, criterio
  9.17). El atributo :attr:`SentimentError.is_transient` expone esa clase de forma
  programática.

Nota sobre los desenlaces que no son excepciones: ``ORPHANED_AFTER_MAP`` y
``EXECUTION_ABANDONED`` son valores de ``error_code`` que ``aggregate_results`` y
``reconcile_stuck`` escriben directamente al marcar Ítems como ``FAILED``, no
condiciones de error de una dependencia; se exponen como constantes, no como
excepciones. ``ALREADY_TERMINAL`` es un valor de **resultado** interno (no un
``error_code`` ni un fallo) y se expone como :data:`ALREADY_TERMINAL`.
"""

from __future__ import annotations


class SentimentError(Exception):
    """Raíz de toda excepción del dominio.

    El Catalogo_De_Errores es **cerrado**: ninguna subclase emite un
    ``error_code`` que no figure en el glosario de los requisitos. Hay dos
    familias que no se mezclan: errores de API (HTTP), que se devuelven al cliente
    con :attr:`http_status`, y errores de procesamiento (internos), que se
    registran en el campo ``error_code`` de un registro ITEM o META y nunca se
    devuelven en una respuesta HTTP.

    Attributes:
        error_code: código del catálogo cerrado, seguro para exponer al cliente.
            Las subclases lo redefinen; el valor por defecto ``INTERNAL_ERROR``
            cubre el fallo interno expuesto al cliente (HTTP 500).
        http_status: código HTTP cuando el error cruza la frontera de la API, o
            ``None`` en los errores de procesamiento, que no cruzan esa frontera.
        is_transient: ``True`` si la Maquina_De_Estados debe reintentar la tarea
            afectada (clase transitoria del criterio 9.16); ``False`` en los
            errores permanentes y en los que no entran en esa clasificación.
        detail: contexto interno asociado a la instancia. **Nunca** se serializa
            hacia el cliente; sólo alimenta logs y trazas del lado del servidor.
    """

    error_code: str = "INTERNAL_ERROR"
    http_status: int | None = 500
    is_transient: bool = False

    def __init__(self, message: str = "", **detail: object) -> None:
        """Inicializa la excepción con un mensaje y detalle interno opcional.

        Args:
            message: mensaje para el log/traza del lado del servidor. Si está
                vacío, se usa el :attr:`error_code` de la clase como mensaje.
                Nunca se expone al cliente.
            **detail: pares clave-valor de contexto interno (p. ej. ``item_id``,
                ``index``). Se guardan en :attr:`detail` y **no** se devuelven al
                cliente; pueden contener identificadores internos, nunca PII.
        """
        super().__init__(message or self.error_code)
        self.detail: dict[str, object] = detail

    def to_client_dict(self, correlation_id: str) -> dict[str, str]:
        """Construye el payload seguro que se devuelve al cliente.

        El cuerpo expuesto al cliente contiene únicamente el ``error_code`` del
        catálogo y el ``correlation_id`` de la solicitud, sin :attr:`detail`, sin
        el mensaje de la excepción y sin ningún fragmento del estado interno del
        Sistema (criterio 15.5 y sección 20.2 del diseño). El ``correlation_id``
        es el puente para que soporte localice el detalle completo en los logs.

        Args:
            correlation_id: identificador UUIDv4 de la solicitud HTTP.

        Returns:
            Un diccionario ``{"error_code": ..., "correlation_id": ...}`` apto
            para serializar directamente hacia el cliente.
        """
        return {"error_code": self.error_code, "correlation_id": correlation_id}


# ===========================================================================
# Familia de API (HTTP). Cruzan la frontera de la API y se devuelven al cliente
# con un http_status. Nunca se registran como estado interno de un Ítem.
# ===========================================================================

# --- Errores de validación de entrada (4xx, permanentes desde la óptica del
#     cliente: la solicitud es malformada y debe corregirse antes de reenviar) ---
class ValidationError(SentimentError):
    """Entrada de cliente malformada no cubierta por un código más específico.

    Es el código por defecto de la familia de validación (criterio 3.19): toda
    condición de entrada malformada a la que no aplique un código más específico
    de esta familia se reporta como ``VALIDATION_FAILED``.
    """

    error_code = "VALIDATION_FAILED"
    http_status = 400


class PayloadTooLargeError(ValidationError):
    """El cuerpo de la solicitud excede el límite de tamaño admitido."""

    error_code = "PAYLOAD_TOO_LARGE"
    http_status = 413


class TooManyItemsError(ValidationError):
    """El Lote contiene más Ítems de los permitidos (tope de 1.000)."""

    error_code = "TOO_MANY_ITEMS"
    http_status = 400


class UnknownIndificadorError(ValidationError):
    """Un ``indificador`` del Lote no pertenece al enum cerrado de Indificadores."""

    error_code = "UNKNOWN_INDIFICADOR"


class ResponseTooLongError(ValidationError):
    """Un texto de ``respuesta`` excede el límite de longitud admitido (5.000)."""

    error_code = "RESPONSE_TOO_LONG"


# --- Errores de autenticación y autorización. Normalmente los emite la
#     API_Gateway sin invocar ninguna Lambda; se modelan aquí para tener un único
#     catálogo y para los tests de contrato de los handlers de la capa de API. ---
class UnauthenticatedError(SentimentError):
    """Credencial ausente, malformada, expirada o no verificable (criterio 1.x).

    El cuerpo de la respuesta no revela qué claim, firma o app client motivó el
    rechazo (criterio 1.17): sólo el ``error_code`` genérico y el
    ``correlation_id``.
    """

    error_code = "UNAUTHENTICATED"
    http_status = 401


class ForbiddenError(SentimentError):
    """Credencial verificada pero sin scope suficiente o sin Tenant asociado.

    Cubre tanto el scope insuficiente como el Access_Token verificado al que le
    falta el claim ``custom:tenant_id`` o ``sub`` (criterio 1.15).
    """

    error_code = "FORBIDDEN"
    http_status = 403


# --- Errores de acceso y de control de flujo y recursos ---
class NotFoundError(SentimentError):
    """Recurso inexistente, o acceso a un recurso ajeno al Tenant solicitante.

    Se usa el **mismo** código para el ``analysis_id`` inexistente, el ajeno a
    otro Tenant y el sintácticamente inválido, de modo que la respuesta sea
    idéntica en los tres casos y no filtre la existencia de Lotes de otros
    Tenants (criterios 2.2 y 2.3).
    """

    error_code = "NOT_FOUND"
    http_status = 404


class NotReadyError(SentimentError):
    """El Lote aún no está en un estado que permita la operación solicitada.

    Lo devuelve la consulta de descarga cuando el Lote no es terminal todavía o
    es terminal sin Documento_Consolidado, y la purga de un Lote no terminal
    (criterios 12.4 y 19.x).
    """

    error_code = "ANALYSIS_NOT_READY"
    http_status = 409


class QuotaExceededError(SentimentError):
    """El Tenant ya tiene el máximo de Lotes no terminales en vuelo (5)."""

    error_code = "QUOTA_EXCEEDED"
    http_status = 429


# ===========================================================================
# Familia de PROCESAMIENTO (interna). http_status = None: no cruzan la frontera
# HTTP. Las Lambdas de procesamiento las propagan a la Maquina_De_Estados en la
# primera ocurrencia; el NOMBRE de la clase es lo que compara ErrorEquals para
# decidir el reintento. is_transient expone esa clase de forma programática.
# ===========================================================================

# --- Clase TRANSITORIA (criterio 9.16): throttling o límite de tasa de una
#     dependencia, plazo máximo de invocación agotado, error 5xx de una
#     dependencia, o fallo de conexión/red. Reintentable por la Maquina_De_Estados. ---
class TransientDependencyError(SentimentError):
    """Condición de error transitoria y reintentable (criterio 9.16).

    La Maquina_De_Estados reintenta la tarea afectada con backoff exponencial y
    jitter (criterios 9.1 a 9.3). Si el Ítem agota los reintentos configurados, el
    desenlace es ``RETRIES_EXHAUSTED`` (criterio 9.5), que la Maquina_De_Estados
    registra; no es el ``error_code`` de esta clase.
    """

    http_status = None
    is_transient = True


class ThrottledError(TransientDependencyError):
    """Throttling o límite de tasa excedido por una dependencia (subcaso de 9.16)."""


# --- Clase PERMANENTE (criterio 9.17): primera ocurrencia => FAILED, sin
#     reintentos. Cada subclase fija el error_code que se registra en el Ítem. ---
class PermanentProcessingError(SentimentError):
    """Condición de error permanente, no reintentable (criterio 9.17).

    Ante la primera ocurrencia, la Maquina_De_Estados transiciona el Ítem a
    ``FAILED`` registrando el ``error_code`` que fija la subclase, sin ejecutar
    ningún reintento.
    """

    http_status = None
    is_transient = False


class ComprehendItemFailedError(PermanentProcessingError):
    """Texto que excede el límite de Amazon Comprehend o idioma no soportado."""

    error_code = "COMPREHEND_ITEM_FAILED"


class LlmInputRejectedError(PermanentProcessingError):
    """El Modelo_Bedrock rechaza el texto de ``respuesta`` por límite o no procesable."""

    error_code = "LLM_INPUT_REJECTED"


class LlmSchemaError(PermanentProcessingError):
    """La salida del Modelo_Bedrock no valida contra el JSON_Schema ``llm_output``.

    Es **permanente** (criterio 9.17): no se reintenta aunque el modelo sea no
    determinista, porque un reintento no corrige un contrato de salida inválido.
    """

    error_code = "LLM_OUTPUT_INVALID"


class TaskInputInvalidError(PermanentProcessingError):
    """Cualquier otro fallo de validación de la entrada de una tarea del pipeline.

    Por ejemplo, un batch de Comprehend con más de 25 Ítems (criterio 6.12). Es
    un defecto de la orquestación, no del contenido del cliente, y por eso usa el
    código interno ``TASK_INPUT_INVALID`` y nunca una respuesta HTTP.
    """

    error_code = "TASK_INPUT_INVALID"


# --- Desenlaces del pipeline que NO son condiciones de error de una dependencia
#     y, por tanto, NO entran en la clasificación transitorio/permanente
#     (is_transient se mantiene en False, heredado de SentimentError). ---
class ComprehendResultMissingError(SentimentError):
    """Un Ítem enviado no aparece ni en ``ResultList`` ni en ``ErrorList`` (6.13).

    Nunca se omite un Ítem: el que falta en ambas listas se cierra con este
    ``error_code`` para preservar la conservación de Ítems.
    """

    error_code = "COMPREHEND_RESULT_MISSING"
    http_status = None


class InputReadFailedError(SentimentError):
    """Fallo al leer ``inputs/``, o ``item_count`` distinto del número leído (5.16).

    Impide iniciar el Distributed_Map; el Lote se cierra como ``FAILED`` con este
    ``error_code`` y se publica el Evento_Finalizacion.
    """

    error_code = "INPUT_READ_FAILED"
    http_status = None


class AggregationError(SentimentError):
    """La agregación falla tras agotar sus reintentos (criterio 9.11).

    El registro META del Lote se cierra como ``FAILED`` con este ``error_code``.
    """

    error_code = "AGGREGATION_FAILED"
    http_status = None


# ===========================================================================
# Constantes de error_code que NO son excepciones. Son desenlaces del pipeline
# que aggregate_results y reconcile_stuck escriben directamente en el campo
# error_code de un Ítem (o del META) al marcarlo como FAILED, sin que medie el
# lanzamiento y la captura de una excepción.
# ===========================================================================

#: ``error_code`` con el que ``aggregate_results`` cierra como ``FAILED`` los
#: Ítems que quedaron en ``PENDING`` tras terminar el Distributed_Map
#: (criterio 10.2), de modo que al cerrar el Lote ninguno quede ``PENDING``.
ORPHANED_AFTER_MAP = "ORPHANED_AFTER_MAP"

#: ``error_code`` con el que ``reconcile_stuck`` cierra los Ítems de un Lote que
#: el sweeper marca como ``EXPIRED`` porque su ejecución se abandonó o excedió el
#: plazo máximo (criterios 10.11 a 10.13).
EXECUTION_ABANDONED = "EXECUTION_ABANDONED"

# ===========================================================================
# Valor de RESULTADO (no un error_code, no un fallo). ALREADY_TERMINAL es lo que
# Analyze_Llm devuelve a la Maquina_De_Estados cuando el Ítem recibido ya estaba
# en un Estado_Terminal (criterio 8.2): denota una invocación idempotente que no
# tuvo trabajo que hacer. NO pertenece al Catalogo_De_Errores, no se persiste en
# ningún registro ITEM o META y no se devuelve en ninguna respuesta HTTP.
# ===========================================================================

#: Valor de resultado interno de una invocación idempotente de Analyze_Llm sobre
#: un Ítem ya terminal (criterio 8.2). No es un ``error_code`` ni un fallo.
ALREADY_TERMINAL = "ALREADY_TERMINAL"
