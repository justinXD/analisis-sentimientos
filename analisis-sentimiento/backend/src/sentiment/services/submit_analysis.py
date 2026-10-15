"""Caso de uso ``submit_analysis``: la única puerta de entrada de datos nuevos.

Este servicio valida, persiste y encola un Lote de respuestas de encuesta para su
análisis asíncrono. Es el caso de uso que la Lambda ``api_submit`` adapta a
HTTP (la sección 16.1 del diseño lo describe como *Low-Level Design*). Es
**puro** respecto de la infraestructura: no importa ``boto3`` ni ningún
adaptador; depende exclusivamente de los puertos de ``domain/ports.py``
(:class:`~sentiment.domain.ports.AnalysisRepository`,
:class:`~sentiment.domain.ports.ObjectStorage`,
:class:`~sentiment.domain.ports.QueuePublisher`,
:class:`~sentiment.domain.ports.EventPublisher`) y de dos *callables* inyectados
(``clock`` e ``id_factory``), lo que lo hace comprobable con dobles en memoria y
semillas deterministas, sin *monkeypatching* (criterio 17.2).

Garantías de diseño que este módulo materializa:

* **Validación total antes de cualquier escritura (criterios 3.1, 3.13).** La
  Fase 1 valida el cuerpo completo contra el JSON_Schema ``submit_request`` más la
  comprobación de longitud en bytes de ``respuesta``, **sin** tocar S3, DynamoDB
  ni SQS. Si algo falla, se lanza una :class:`~sentiment.domain.errors.ValidationError`
  (o subclase) y **ningún** efecto de escritura se ha producido: la operación es
  "todo o nada" desde la óptica del cliente.
* **Precedencia fija del ``error_code`` (criterio 3.16).** Cuando una solicitud
  infringe varias reglas, se devuelve un solo código, elegido por el orden
  ``PAYLOAD_TOO_LARGE > TOO_MANY_ITEMS > UNKNOWN_INDIFICADOR > RESPONSE_TOO_LONG >
  VALIDATION_FAILED``; dentro de un mismo código se reporta el ítem de ``index``
  más bajo. La implementación **recoge** la primera infracción de cada categoría y
  devuelve la de mayor precedencia, en lugar de abortar en la primera que
  encuentra. Un campo ``tenant_id`` en el cuerpo es ``VALIDATION_FAILED``
  (criterio 1.11); el enum cerrado de ``submit_request`` ya rechaza cualquier
  campo no declarado (``additionalProperties: false``).
* **Catch-all a ``VALIDATION_FAILED`` (criterio 3.19).** Cualquier excepción
  inesperada durante la Fase 1 se traduce a ``VALIDATION_FAILED``: una entrada
  malformada, por infrecuente que sea, nunca produce un ``5xx``. Los ``5xx`` quedan
  reservados a los fallos de infraestructura de la Fase 3 (criterios 4.10, 4.11).
* **Normalización NFC + trim (criterio 3.17).** Cada ``respuesta`` que supera la
  validación se normaliza a Unicode NFC y se recortan sus espacios inicial y final
  **antes** de persistirse.
* **Equidad entre tenants (criterio 4.12).** Antes de escribir nada, se rechaza
  con ``QUOTA_EXCEEDED`` (429) al Tenant que ya tiene
  :data:`MAX_INFLIGHT_PER_TENANT` Lotes en estado no terminal.
* **Orden de escritura S3 → DynamoDB → SQS (criterios 4.1, 4.3, 4.8).** S3 es la
  copia inmutable del input y va primero; DynamoDB siembra el META y los N ITEM;
  SQS es **siempre lo último**, porque encolar significa "todo lo anterior está
  listo" y evita que la orquestación lea un Lote incompleto.
* **Cierre del META en ``FAILED`` + evento si la siembra falla (criterios 4.10,
  4.11).** Si la escritura en S3 falla, no queda nada que limpiar y se propaga
  ``INTERNAL_ERROR``. Si falla la siembra en DynamoDB, se marca el META como
  ``FAILED`` (condicional: no-op si no existía o ya era terminal) y, si el META
  llegó a existir, se publica el Evento_Finalizacion **antes** de propagar el
  fallo como ``500 INTERNAL_ERROR``; no se encola nada.

El ``respuesta`` (PII) nunca aparece en ningún log emitido por este servicio.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from sentiment.domain.errors import (
    PayloadTooLargeError,
    QuotaExceededError,
    ResponseTooLongError,
    SentimentError,
    TooManyItemsError,
    UnknownIndificadorError,
    ValidationError,
)
from sentiment.domain.models import AnalysisMeta, AnalysisStatus, SurveyAnswer
from sentiment.observability.logging import get_logger
from sentiment.schemas.loader import (
    SUBMIT_REQUEST_SCHEMA_FILE,
    get_validator,
    submit_request_indificador_enum,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sentiment.domain.ports import (
        AnalysisRepository,
        EventPublisher,
        ObjectStorage,
        QueuePublisher,
    )

__all__ = [
    "MAX_ITEMS",
    "MAX_RESPONSE_BYTES",
    "MAX_PAYLOAD_BYTES",
    "MAX_INFLIGHT_PER_TENANT",
    "INTERNAL_ERROR_CODE",
    "SubmitCommand",
    "SubmitAnalysisService",
]

logger = get_logger()

# --- Límites de validación de ingesta (sección 12.5 del diseño) ------------

#: Número máximo de Ítems por Lote (criterios 3.6, 12.5). El JSON_Schema fija el
#: mismo ``maxItems``; este tope es la revalidación en código con el ``error_code``
#: específico ``TOO_MANY_ITEMS``.
MAX_ITEMS: Final = 1000

#: Longitud máxima de ``respuesta`` **en bytes UTF-8** (criterio 3.10). El
#: ``maxLength`` del esquema cuenta caracteres y es solo un primer guard; el límite
#: real se revalida aquí con ``len(respuesta.encode("utf-8"))``.
MAX_RESPONSE_BYTES: Final = 5000

#: Tamaño máximo del cuerpo de la solicitud en bytes, medido antes de parsear
#: (criterio 3.4): 5 MiB.
MAX_PAYLOAD_BYTES: Final = 5 * 1024 * 1024

#: Máximo de Lotes en estado no terminal (``PENDING``/``RUNNING``) por Tenant
#: (criterio 4.12). Alcanzarlo provoca ``QUOTA_EXCEEDED`` (429).
MAX_INFLIGHT_PER_TENANT: Final = 5

#: ``error_code`` del catálogo que se devuelve al cliente ante un fallo de
#: infraestructura durante la persistencia (criterios 4.10, 4.11). Es el valor por
#: defecto de :class:`~sentiment.domain.errors.SentimentError`.
INTERNAL_ERROR_CODE: Final = "INTERNAL_ERROR"

# Tamaño de página al contar Lotes en vuelo vía el puerto ``list_by_tenant``. El
# conteo se interrumpe en cuanto alcanza el umbral, por lo que basta un valor
# holgado respecto de MAX_INFLIGHT_PER_TENANT para resolver el caso típico en una
# sola página sin cargar el historial completo del Tenant en memoria.
_INFLIGHT_PAGE_SIZE: Final = 25

# Tope de páginas recorridas al contar Lotes en vuelo. Acota el trabajo del conteo
# a lo sumo a este número de ``Query`` aunque el Tenant acumule un historial largo
# de Lotes terminales: una vez vistos MAX_INFLIGHT_PER_TENANT no terminales, el
# conteo ya no necesita seguir, y este tope evita además recorrer el historial
# íntegro en el caso degenerado en que casi todos los Lotes recientes sean
# terminales (defensa contra una consulta no acotada).
_INFLIGHT_MAX_PAGES: Final = 64

# Orden de precedencia del ``error_code`` (criterio 3.16). El índice en esta tupla
# es la prioridad: menor índice gana. Se usa para elegir, entre varias
# infracciones recogidas, la de mayor precedencia.
_ERROR_PRECEDENCE: Final[tuple[str, ...]] = (
    "PAYLOAD_TOO_LARGE",
    "TOO_MANY_ITEMS",
    "UNKNOWN_INDIFICADOR",
    "RESPONSE_TOO_LONG",
    "VALIDATION_FAILED",
)


@dataclass(frozen=True, slots=True)
class SubmitCommand:
    """Entrada del caso de uso ``submit_analysis``, ya autenticada.

    El handler (``api_submit``) construye este comando a partir del evento de API
    Gateway: lee ``tenant_id`` y ``owner_sub`` **exclusivamente** del contexto del
    autorizador (claims ``custom:tenant_id`` y ``sub`` ya verificados, criterio
    1.14) y pasa el cuerpo HTTP crudo sin parsear.

    Attributes:
        tenant_id: organización propietaria del Lote, del claim verificado. No es
            ``None`` ni cadena vacía (lo garantiza el handler; API Gateway responde
            ``403`` si falta, criterio 2.9).
        owner_sub: ``sub`` del usuario solicitante, del claim verificado.
        raw_body: cuerpo HTTP crudo en ``bytes``, tal como llegó, sin parsear. Se
            revalida su tamaño aquí aunque WAF y API Gateway ya lo acoten.
        correlation_id: identificador de correlación UUIDv4 de la solicitud, que el
            handler propaga para la trazabilidad extremo a extremo.
    """

    tenant_id: str
    owner_sub: str
    raw_body: bytes
    correlation_id: str


class _ValidationErrors:
    """Acumulador de infracciones de validación para aplicar la precedencia 3.16.

    En lugar de abortar en la primera infracción, el servicio recorre la entrada
    recogiendo, por cada categoría de ``error_code``, la primera infracción (la del
    ``index`` más bajo). Al terminar, :meth:`raise_highest_precedence` lanza la de
    mayor precedencia según :data:`_ERROR_PRECEDENCE`. Así una solicitud que
    infringe varias reglas devuelve exactamente un ``error_code``, el correcto
    (criterio 3.16), y nunca uno de menor precedencia por un orden de recorrido
    accidental.
    """

    __slots__ = ("_first_by_code",)

    def __init__(self) -> None:
        self._first_by_code: dict[str, ValidationError] = {}

    def add(self, error: ValidationError) -> None:
        """Registra ``error`` como la infracción de su ``error_code`` si es la primera.

        Solo se conserva la primera infracción de cada código, que —por el orden de
        recorrido de los Ítems— es la del ``index`` más bajo (criterio 3.16).
        """
        self._first_by_code.setdefault(error.error_code, error)

    @property
    def has_errors(self) -> bool:
        """``True`` si se ha recogido al menos una infracción."""
        return bool(self._first_by_code)

    def raise_highest_precedence(self) -> None:
        """Lanza la infracción de mayor precedencia recogida, si hay alguna.

        No hace nada si no se recogió ninguna infracción, de modo que el llamante
        puede invocarla incondicionalmente tras la fase de recolección.

        :raises ValidationError: la infracción de mayor precedencia (o subclase).
        """
        for code in _ERROR_PRECEDENCE:
            error = self._first_by_code.get(code)
            if error is not None:
                raise error


class SubmitAnalysisService:
    """Valida, persiste y encola un Lote. Único punto de entrada de datos nuevos.

    Las dependencias se inyectan por constructor (inversión de dependencias): el
    servicio depende de los puertos del dominio y de dos *callables*, nunca de
    adaptadores concretos ni del SDK de AWS. ``clock`` e ``id_factory`` se inyectan
    para que las pruebas sean deterministas sin *monkeypatching*.
    """

    __slots__ = (
        "_repository",
        "_storage",
        "_queue",
        "_events",
        "_clock",
        "_id_factory",
    )

    def __init__(
        self,
        repository: AnalysisRepository,
        storage: ObjectStorage,
        queue: QueuePublisher,
        events: EventPublisher,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
    ) -> None:
        """Inicializa el servicio con sus puertos y *callables* inyectados.

        :param repository: puerto de persistencia del Lote y sus Ítems.
        :param storage: puerto de almacenamiento del payload de entrada en S3.
        :param queue: puerto de encolado del Lote para la orquestación.
        :param events: puerto de publicación del Evento_Finalizacion.
        :param clock: *callable* que devuelve el instante actual como ``datetime``
            con zona horaria; en producción, ``lambda: datetime.now(tz=UTC)``.
        :param id_factory: *callable* que genera un ``analysis_id`` (ULID de 26
            caracteres); en producción, ``sentiment.domain.ids.new_analysis_id``.
        """
        self._repository = repository
        self._storage = storage
        self._queue = queue
        self._events = events
        self._clock = clock
        self._id_factory = id_factory

    def execute(self, command: SubmitCommand) -> AnalysisMeta:
        """Acepta un Lote de respuestas de encuesta para análisis asíncrono.

        Flujo en tres fases: (1) validación total sin escrituras; (2) control de
        equidad entre tenants; (3) escrituras en orden de dependencia estricto
        S3 → DynamoDB → SQS. Ver el docstring del módulo para las garantías de cada
        fase.

        :param command: entrada ya autenticada (ver :class:`SubmitCommand`).
        :return: el :class:`~sentiment.domain.models.AnalysisMeta` del Lote recién
            creado, con ``status == PENDING`` e ``item_count == len(items)``.
        :raises PayloadTooLargeError: el cuerpo excede :data:`MAX_PAYLOAD_BYTES`
            (criterio 3.4).
        :raises TooManyItemsError: ``items`` tiene más de :data:`MAX_ITEMS`
            elementos (criterio 3.6).
        :raises UnknownIndificadorError: algún ``indificador`` no pertenece al enum
            cerrado (criterio 3.8).
        :raises ResponseTooLongError: alguna ``respuesta`` excede
            :data:`MAX_RESPONSE_BYTES` bytes UTF-8 (criterio 3.10).
        :raises ValidationError: cualquier otra condición de entrada malformada,
            incluido un campo ``tenant_id`` en el cuerpo (criterios 1.11, 3.19).
        :raises QuotaExceededError: el Tenant ya tiene :data:`MAX_INFLIGHT_PER_TENANT`
            Lotes no terminales (criterio 4.12).
        :raises SentimentError: ``INTERNAL_ERROR`` ante un fallo de infraestructura
            al escribir en S3 o DynamoDB (criterios 4.10, 4.11).
        """
        # --- Fase 1: validación total. Cero escrituras en esta fase. ---------
        answers = self._validate(command)

        # --- Fase 2: control de equidad entre tenants (criterio 4.12). -------
        self._enforce_inflight_quota(command.tenant_id)

        # --- Fase 3: escrituras, en orden de dependencia estricto. -----------
        return self._persist_and_enqueue(command, answers)

    # -- Fase 1 ------------------------------------------------------------

    def _validate(self, command: SubmitCommand) -> list[SurveyAnswer]:
        """Valida el cuerpo por completo y devuelve las respuestas normalizadas.

        No ejecuta ninguna escritura. Envuelve toda la validación de modo que
        cualquier excepción inesperada se traduzca a ``VALIDATION_FAILED`` en lugar
        de propagarse como un ``5xx`` (criterio 3.19).

        :param command: la entrada del caso de uso.
        :return: la lista de :class:`~sentiment.domain.models.SurveyAnswer`
            normalizadas (NFC + trim), una por elemento de ``items``, en orden.
        :raises ValidationError: o subclase, según la precedencia del criterio 3.16.
        """
        # El tamaño del cuerpo se mide antes de parsear y tiene la máxima
        # precedencia (criterio 3.16): si el payload es demasiado grande, ni
        # siquiera se intenta parsearlo (criterio 3.4).
        if len(command.raw_body) > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(size=len(command.raw_body))

        try:
            payload = self._parse_and_validate_body(command)
        except ValidationError:
            # Infracción ya clasificada con su código específico: se propaga tal cual.
            raise
        except Exception as exc:
            # Catch-all deliberado (criterio 3.19).
            # Cualquier condición malformada no prevista se degrada a
            # VALIDATION_FAILED; nunca a un 5xx (criterio 3.19).
            raise ValidationError("Cuerpo de solicitud no procesable.") from exc

        return self._collect_items(payload["items"])

    def _parse_and_validate_body(self, command: SubmitCommand) -> dict[str, Any]:
        """Parsea el cuerpo JSON y lo valida contra el JSON_Schema ``submit_request``.

        La validación de esquema (``additionalProperties: false``) rechaza por sí
        sola cualquier campo no declarado —incluido un ``tenant_id`` en el cuerpo
        (criterio 1.11)— y comprueba tipos, obligatoriedad y el enum de
        ``indificador``. Un fallo de esquema es ``VALIDATION_FAILED`` salvo que
        corresponda a un código más específico, que se detecta aparte.

        :param command: la entrada del caso de uso.
        :return: el payload parseado como ``dict`` con un ``items`` que es una lista
            de objetos ya validados estructuralmente.
        :raises ValidationError: o subclase, ante un cuerpo no parseable o inválido.
        """
        try:
            parsed: object = json.loads(command.raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Cuerpo vacío o JSON no parseable (criterio 3.2).
            raise ValidationError("El cuerpo no es un documento JSON válido.") from exc

        if not isinstance(parsed, dict):
            # El elemento raíz debe ser un objeto JSON (criterio 3.2).
            raise ValidationError("El elemento raíz del cuerpo no es un objeto JSON.")

        self._apply_schema_precedence(parsed)
        return parsed

    def _apply_schema_precedence(self, payload: dict[str, Any]) -> None:
        """Valida ``payload`` contra el esquema aplicando la precedencia del 3.16.

        El validador recorre **todos** los errores en vez de abortar en el primero,
        y cada error se clasifica en su categoría del catálogo
        (``TOO_MANY_ITEMS``, ``UNKNOWN_INDIFICADOR``, ``RESPONSE_TOO_LONG`` o, por
        defecto, ``VALIDATION_FAILED``). Al final se lanza la de mayor precedencia,
        reportando dentro de cada código el ítem de ``index`` más bajo (criterio
        3.16). ``PAYLOAD_TOO_LARGE`` se decide antes de parsear, fuera de aquí.

        :param payload: el cuerpo parseado como objeto JSON.
        :raises ValidationError: o subclase, si el payload infringe el esquema.
        """
        validator = get_validator(SUBMIT_REQUEST_SCHEMA_FILE)
        collected = _ValidationErrors()

        # ``iter_errors`` produce todos los errores; se ordenan por ruta para que,
        # dentro de un mismo código, gane el ítem de ``index`` más bajo.
        for error in sorted(validator.iter_errors(payload), key=_error_sort_key):
            collected.add(self._classify_schema_error(error))

        collected.raise_highest_precedence()

    @staticmethod
    def _classify_schema_error(error: JsonSchemaValidationError) -> ValidationError:
        """Traduce un error de esquema a la subclase de ``ValidationError`` correcta.

        La clasificación mapea la combinación (ubicación del error, palabra clave
        del esquema infringida) al ``error_code`` del catálogo que exige el criterio
        3.16; cualquier combinación no específica cae en ``VALIDATION_FAILED``. Un
        ``indificador`` ausente, de tipo inválido o fuera del enum es
        ``UNKNOWN_INDIFICADOR`` (criterio 3.8); un ``respuesta`` ausente o de tipo
        inválido es ``VALIDATION_FAILED`` (criterio 3.9); una ``respuesta`` que
        excede ``maxLength`` (caracteres) es ``RESPONSE_TOO_LONG`` como primer guard
        (criterio 3.10, el límite real en bytes se revisa después).

        :param error: el error de validación de ``jsonschema``.
        :return: la instancia de :class:`ValidationError` (o subclase) adecuada, con
            el ``index`` del ítem ofensor en su ``detail`` cuando aplica.
        """
        parts = list(error.absolute_path)
        # ``error.validator`` es la palabra clave del esquema que falló; se compara
        # como cadena (el stub la tipa como un alias, no como ``str``).
        keyword = str(error.validator)
        index = parts[1] if len(parts) >= 2 and isinstance(parts[1], int) else None
        field = parts[2] if len(parts) >= 3 else None

        # Demasiados Ítems: el array ``items`` viola ``maxItems`` (criterio 3.6).
        if keyword == "maxItems" and parts[:1] == ["items"]:
            return TooManyItemsError(maximum=MAX_ITEMS)

        # ``required`` se reporta a nivel de ítem (``["items", i]``); hay que mirar
        # qué propiedad falta para distinguir ``indificador`` (3.8) de ``respuesta``
        # (3.9). ``validator_value`` es la lista de propiedades requeridas.
        if keyword == "required" and _missing_property(error) == "indificador":
            return UnknownIndificadorError(index=index)

        if field == "indificador":
            # Indificador de tipo inválido o fuera del enum (criterio 3.8).
            return UnknownIndificadorError(index=index)

        if field == "respuesta" and keyword == "maxLength":
            # Primer guard por caracteres; el límite real en bytes se revisa después.
            return ResponseTooLongError(index=index)

        return ValidationError("El cuerpo infringe el contrato de ingesta.")

    def _collect_items(self, raw_items: list[dict[str, Any]]) -> list[SurveyAnswer]:
        """Normaliza cada ``respuesta`` y revalida su longitud en bytes.

        Tras pasar el esquema, cada ``respuesta`` se normaliza a Unicode NFC y se
        recortan sus espacios (criterio 3.17) **antes** de medir su longitud en
        bytes UTF-8 (criterio 3.10). Las infracciones de ``RESPONSE_TOO_LONG`` se
        recogen y se lanza la del ``index`` más bajo (criterio 3.16).

        :param raw_items: la lista de objetos ``items`` ya validada por el esquema;
            cada elemento tiene ``indificador`` (del enum) y ``respuesta`` (cadena).
        :return: las :class:`~sentiment.domain.models.SurveyAnswer` normalizadas, en
            el mismo orden que ``raw_items``.
        :raises ResponseTooLongError: si alguna ``respuesta`` excede
            :data:`MAX_RESPONSE_BYTES` bytes (criterio 3.10).
        """
        collected = _ValidationErrors()
        answers: list[SurveyAnswer] = []

        for index, raw in enumerate(raw_items):
            # INVARIANTE: answers[0..index-1] validados y normalizados; 0 escrituras.
            respuesta = unicodedata.normalize("NFC", raw["respuesta"]).strip()
            if len(respuesta.encode("utf-8")) > MAX_RESPONSE_BYTES:
                collected.add(ResponseTooLongError(index=index))
                continue
            answers.append(
                SurveyAnswer(
                    indificador=raw["indificador"],
                    respuesta=respuesta,
                    index=index,
                )
            )

        if collected.has_errors:
            collected.raise_highest_precedence()

        return answers

    # -- Fase 2 ------------------------------------------------------------

    def _enforce_inflight_quota(self, tenant_id: str) -> None:
        """Rechaza con ``QUOTA_EXCEEDED`` si el Tenant supera la cuota de Lotes en vuelo.

        Cuenta los Lotes en estado no terminal del Tenant paginando el puerto
        :meth:`~sentiment.domain.ports.AnalysisRepository.list_by_tenant` y se
        detiene en cuanto alcanza :data:`MAX_INFLIGHT_PER_TENANT`, sin cargar el
        historial completo en memoria. El recorrido está acotado por
        :data:`_INFLIGHT_MAX_PAGES` para que un historial largo de Lotes terminales
        no convierta el conteo en una consulta no acotada.

        :param tenant_id: Tenant cuya cuota se verifica.
        :raises QuotaExceededError: si el Tenant ya tiene
            :data:`MAX_INFLIGHT_PER_TENANT` Lotes no terminales (criterio 4.12).
        """
        inflight = 0
        cursor: str | None = None
        for _ in range(_INFLIGHT_MAX_PAGES):
            metas, cursor = self._repository.list_by_tenant(
                tenant_id, _INFLIGHT_PAGE_SIZE, cursor
            )
            for meta in metas:
                if not meta.is_terminal:
                    inflight += 1
                    if inflight >= MAX_INFLIGHT_PER_TENANT:
                        raise QuotaExceededError(inflight=inflight)
            if cursor is None:
                break

    # -- Fase 3 ------------------------------------------------------------

    def _persist_and_enqueue(
        self, command: SubmitCommand, answers: list[SurveyAnswer]
    ) -> AnalysisMeta:
        """Escribe S3 → DynamoDB → SQS en orden de dependencia estricto.

        SQS es siempre lo último: encolar antes permitiría que la orquestación
        leyera un Lote incompleto. Un fallo de S3 propaga ``INTERNAL_ERROR`` sin
        tocar DynamoDB ni SQS (criterio 4.10); un fallo de la siembra en DynamoDB
        cierra el META como ``FAILED`` y publica el evento de cierre antes de
        propagar ``INTERNAL_ERROR`` (criterio 4.11).

        :param command: la entrada del caso de uso.
        :param answers: las respuestas ya validadas y normalizadas.
        :return: el META del Lote creado.
        """
        analysis_id = self._id_factory()
        now = self._clock()
        input_key = _build_input_key(command.tenant_id, now, analysis_id)

        meta = AnalysisMeta(
            analysis_id=analysis_id,
            tenant_id=command.tenant_id,
            owner_sub=command.owner_sub,
            status=AnalysisStatus.PENDING,
            item_count=len(answers),
            input_s3_key=input_key,
            created_at=now,
            updated_at=now,
        )

        # 3a) S3 primero: copia inmutable del input. Si falla, nada más ocurre
        #     (criterio 4.10). El payload persistido es canónico: solo los campos
        #     del contrato, con las respuestas ya normalizadas (NFC + trim). El
        #     fallo de infraestructura se traduce a INTERNAL_ERROR (-> 500), nunca
        #     se filtra la excepción cruda del adaptador al cliente (criterio 3.18).
        try:
            self._storage.put_json(input_key, _canonicalize(answers))
        except SentimentError:
            raise
        except Exception as exc:
            raise SentimentError(
                "Fallo al escribir el payload de entrada en S3.",
                analysis_id=analysis_id,
            ) from exc

        # 3b) DynamoDB: META + N ITEM en tandas de 25. Si falla, se cierra el META
        #     (si existía) en FAILED y se publica el evento ANTES de propagar el
        #     fallo de infraestructura (-> 500 INTERNAL_ERROR). No se encola nada.
        try:
            self._repository.create_analysis(meta, answers)
        except Exception as exc:
            self._handle_seeding_failure(command.tenant_id, analysis_id)
            if isinstance(exc, SentimentError):
                raise
            raise SentimentError(
                "Fallo al sembrar el Lote en DynamoDB.",
                analysis_id=analysis_id,
            ) from exc

        # 3c) SQS AL FINAL. Enviar el mensaje significa "todo lo anterior está listo".
        self._queue.publish_analysis_queued(meta, command.correlation_id)

        logger.info(
            "analysis_accepted",
            extra={
                "analysis_id": analysis_id,
                "tenant_id": command.tenant_id,
                "item_count": meta.item_count,
                "correlation_id": command.correlation_id,
            },
        )
        return meta

    def _handle_seeding_failure(self, tenant_id: str, analysis_id: str) -> None:
        """Cierra el META en ``FAILED`` y publica el evento de cierre, si el META existe.

        Se invoca cuando la siembra en DynamoDB falla a medias (criterio 4.11). El
        cierre es idempotente y condicional: ``mark_analysis_failed`` no hace nada
        si el META no se llegó a escribir o si ya era terminal. Solo si el META
        existe se publica el Evento_Finalizacion, releyéndolo para reflejar el
        estado ``FAILED`` ya persistido.

        La limpieza nunca enmascara el fallo original: cualquier excepción de la
        propia limpieza se registra y se descarta, de modo que el ``raise`` del
        llamador propague siempre el ``INTERNAL_ERROR`` del fallo de siembra.

        :param tenant_id: Tenant propietario del Lote (acota la relectura del META).
        :param analysis_id: identificador del Lote cuya siembra falló.
        """
        try:
            self._repository.mark_analysis_failed(analysis_id, INTERNAL_ERROR_CODE)
        except Exception:
            # La limpieza no debe ocultar el fallo de siembra original.
            logger.error(
                "seeding_failure_cleanup_failed",
                extra={"analysis_id": analysis_id, "tenant_id": tenant_id},
            )
            return

        # Si el META llegó a existir, se publica el Evento_Finalizacion del cierre.
        # Si no existe, get_meta propaga "no encontrado": no hay Lote del que
        # notificar nada (criterio 4.11: "si ya existía").
        try:
            failed_meta = self._repository.get_meta(analysis_id, tenant_id)
        except Exception:
            # META inexistente ("si ya existía", criterio 4.11) u otro fallo de relectura.
            return

        try:
            self._events.publish_analysis_completed(failed_meta)
        except Exception:
            # Un fallo del bus de eventos no debe cambiar el 500 que se propagará.
            logger.error(
                "seeding_failure_event_publish_failed",
                extra={"analysis_id": analysis_id, "tenant_id": tenant_id},
            )


# --- Helpers de módulo (puros) --------------------------------------------


def _error_sort_key(error: JsonSchemaValidationError) -> tuple[int, ...]:
    """Clave de orden de un error de esquema por su ruta, para preferir el ``index`` menor.

    Convierte la ruta absoluta del error (``absolute_path``) en una tupla de
    enteros con los componentes que son índices de array (p. ej. la posición del
    ítem). Así, al ordenar los errores de un mismo tipo, el del ``index`` más bajo
    aparece primero y es el que se conserva (criterio 3.16). Los componentes de
    ruta que no son enteros se descartan a efectos de orden.

    :param error: un error de validación de ``jsonschema``.
    :return: una tupla de los componentes enteros de la ruta del error.
    """
    return tuple(part for part in error.absolute_path if isinstance(part, int))


def _missing_property(error: JsonSchemaValidationError) -> str | None:
    """Devuelve el nombre de la propiedad ausente de un error ``required``, si lo hay.

    Para un error de palabra clave ``required``, ``jsonschema`` nombra la propiedad
    que falta en el mensaje y, en las versiones recientes, en ``validator_value``
    (la lista de propiedades requeridas). Esta función extrae el primer nombre
    reconocible de forma defensiva: si no puede determinarlo, devuelve ``None`` y la
    clasificación recae en ``VALIDATION_FAILED``.

    :param error: un error de validación de ``jsonschema`` con ``validator ==
        "required"``.
    :return: el nombre de la propiedad ausente (p. ej. ``"indificador"``), o
        ``None`` si no se puede determinar.
    """
    # El mensaje tiene la forma ``'<prop>' is a required property``; se extrae el
    # nombre entre comillas simples, que es estable entre versiones de jsonschema.
    message = error.message
    first = message.find("'")
    if first == -1:
        return None
    second = message.find("'", first + 1)
    if second == -1:
        return None
    return message[first + 1 : second]


def _canonicalize(answers: list[SurveyAnswer]) -> dict[str, Any]:
    """Construye el payload canónico que se persiste en S3 (prefijo ``inputs/``).

    El documento canónico contiene únicamente los campos del contrato de un Ítem,
    con la ``respuesta`` ya normalizada (NFC + trim) y en el mismo orden e índice
    que el array original. Es la copia inmutable del input que el ``ItemReader`` de
    la orquestación lee después, por lo que no incluye metadatos de la solicitud ni
    nada controlado por el cliente fuera de ``indificador`` y ``respuesta``.

    :param answers: las respuestas validadas y normalizadas, en orden.
    :return: un ``dict`` ``{"items": [{"indificador", "respuesta"}, ...]}`` listo
        para serializar a JSON.
    """
    return {
        "items": [
            {"indificador": answer.indificador, "respuesta": answer.respuesta}
            for answer in answers
        ]
    }


def _build_input_key(tenant_id: str, created_at: datetime, analysis_id: str) -> str:
    """Deriva la clave S3 del payload de entrada (criterios 2.8, 4.1).

    Devuelve ``inputs/{tenant_id}/{yyyy}/{mm}/{dd}/{analysis_id}.json`` con el
    año, mes y día tomados de la fecha **UTC** de ``created_at``. El prefijo
    ``inputs/{tenant_id}/`` materializa el aislamiento multi-tenant en S3
    (criterio 2.8); el particionado por fecha facilita el ciclo de vida y la
    enumeración acotada.

    :param tenant_id: Tenant propietario, del claim verificado.
    :param created_at: instante de creación del Lote; se interpreta en UTC.
    :param analysis_id: ULID del Lote.
    :return: la clave S3 completa del objeto de entrada.
    """
    utc_date = created_at.astimezone(UTC)
    return (
        f"inputs/{tenant_id}/"
        f"{utc_date.year:04d}/{utc_date.month:02d}/{utc_date.day:02d}/"
        f"{analysis_id}.json"
    )


def _known_indificadores() -> frozenset[str]:
    """Devuelve el enum cerrado de Indificadores (fuente única del contrato).

    Se expone como ayuda de diagnóstico y para pruebas: el rechazo real de un
    ``indificador`` inválido lo realiza el JSON_Schema ``submit_request`` durante la
    validación, pero este conjunto permite construir Ítems válidos en las pruebas
    sin duplicar la lista literal.

    :return: el conjunto de los 11 valores válidos de ``indificador``.
    """
    return frozenset(submit_request_indificador_enum())
