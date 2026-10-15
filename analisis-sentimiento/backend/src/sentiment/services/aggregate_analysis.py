"""Caso de uso ``aggregate_analysis``: consolida y cierra un Lote.

Este módulo implementa el servicio que la Lambda ``aggregate_results`` ejecuta
al terminar el Distributed_Map (sección 16.4 del diseño). Es **puro**: toda su
infraestructura entra por puertos inyectados (:class:`~sentiment.domain.ports.AnalysisRepository`,
:class:`~sentiment.domain.ports.ObjectStorage`,
:class:`~sentiment.domain.ports.EventPublisher`) más un ``clock`` para la marca
de tiempo de cierre. No importa ``boto3`` ni ningún adaptador concreto, de modo
que es verificable con dobles en memoria y preserva la frontera hexagonal
(``handlers`` → ``services`` → ``domain``; Requirements 17.1 y 17.2).

Responsabilidad
---------------
Cerrar un Lote de forma **determinista, idempotente y conservadora de ítems**:

#. **Idempotencia por estado terminal (criterio 8.7).** Si el registro META ya
   está en un Estado_Terminal, el servicio devuelve ese META sin recontar, sin
   reescribir el Documento_Consolidado y sin republicar el Evento_Finalizacion.
#. **Recuento desde la verdad (criterios 8.11, 10.4; Property P11).** Los
   contadores finales se obtienen contando los registros ITEM reales con
   :meth:`AnalysisRepository.iter_items`, nunca de los contadores atómicos
   acumulados: así un incremento perdido por un fallo transitorio no corrompe el
   cierre.
#. **Conservación de ítems (criterios 10.3, 10.5; Property P1).** Si la lectura
   no recupera exactamente ``item_count`` registros, el servicio aborta con
   :class:`~sentiment.domain.errors.AggregationError` (``AGGREGATION_FAILED``)
   **sin** fijar contadores ni estado terminal; el cierre queda en manos del
   ``Retry``/``Catch`` → ``MarkFailed`` de la Maquina_De_Estados (criterio 9.11).
#. **Terminación garantizada (criterio 10.2; Property P4).** Todo ITEM que siga
   en ``PENDING`` tras el Map se cierra como ``FAILED`` con
   ``error_code = ORPHANED_AFTER_MAP`` antes de fijar los contadores, de modo que
   ningún ITEM quede no terminal al cerrar el Lote.
#. **Orden del array original (criterios 10.6, 12.7; Property P7).** El array
   ``items`` del Documento_Consolidado se ordena por ``index`` ascendente, orden
   único porque no hay dos ITEM del mismo Lote con el mismo ``index``.
#. **Estado terminal unívoco (criterios 10.1, 10.7, 10.8, 10.9).**
   :func:`decide_final_status` es una función total que mapea los recuentos a
   exactamente uno de ``COMPLETED`` / ``COMPLETED_WITH_ERRORS`` / ``FAILED``.
#. **Puntero y marca de cierre antes del evento (criterio 10.16).** El
   ``output_s3_key`` y el ``completed_at`` quedan registrados en el META **antes**
   de publicar el Evento_Finalizacion.

Orden de escrituras y recuperación ante fallos
-----------------------------------------------
El orden es, deliberadamente: **(1)** escribir el Documento_Consolidado en S3,
**(2)** cerrar el META (``finalize_analysis``), **(3)** publicar el evento,
**(4)** marcar el evento como publicado (``mark_event_published``). Es seguro
ante caídas en cualquier punto:

* Caída tras **(1)** y antes de **(2)**: el META sigue no terminal; una
  reejecución recuenta, reconstruye un documento idéntico (ítems por ``index``,
  mismos contadores) y vuelve a intentar el cierre. El objeto de ``outputs/`` se
  sobrescribe con contenido equivalente (idempotencia de contenido, criterio 8.8).
* Caída tras **(2)** y antes de **(3)**: el META ya es terminal; una reejecución
  entra por la rama de idempotencia (criterio 8.7) y **no** reescribe el
  documento ni recalcula nada. El Lote queda terminal con su ``output_s3_key``
  registrado; el Evento_Finalizacion lo publica Reconcile_Stuck al detectar un
  Lote "terminal sin evento publicado" (criterio 10.19).
* Caída tras **(3)** y antes de **(4)**: el evento se publicó pero el META sigue
  en el índice SWEEP; Reconcile_Stuck lo republica. Los consumidores deduplican
  por ``event_id = {analysis_id}:{status}`` (criterio 10.20), por lo que la
  entrega al-menos-una-vez es segura.

La marca ``completed_at`` del documento la fija el ``clock`` del servicio en la
primera ejecución que efectúa el cierre; a partir de que el META es terminal,
cualquier reejecución corta antes de recalcular, por lo que ese valor queda
congelado (criterio 8.8).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from sentiment.domain.errors import ORPHANED_AFTER_MAP, AggregationError
from sentiment.domain.models import (
    AnalysisItem,
    AnalysisMeta,
    AnalysisStatus,
    ItemStatus,
    SentimientoGeneral,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from sentiment.domain.ports import (
        AnalysisRepository,
        EventPublisher,
        ObjectStorage,
    )

__all__ = [
    "AggregateAnalysisService",
    "Clock",
    "build_output_key",
    "decide_final_status",
]

#: Versión del esquema del Documento_Consolidado (``consolidated_output.schema.json``).
_SCHEMA_VERSION = 1

#: Las cuatro etiquetas de ``sentimiento_general`` que el resumen por indificador
#: cuenta siempre, en el orden del contrato ``consolidated_output.schema.json``.
_SUMMARY_SENTIMENT_LABELS: tuple[SentimientoGeneral, ...] = (
    SentimientoGeneral.POSITIVO,
    SentimientoGeneral.NEGATIVO,
    SentimientoGeneral.NEUTRAL,
    SentimientoGeneral.MIXTO,
)


class Clock(Protocol):
    """Puerto mínimo de reloj: devuelve el instante UTC actual.

    Se inyecta para que la marca ``completed_at`` del Documento_Consolidado sea
    determinista en las pruebas (un reloj fijo produce un documento estable) y
    para no acoplar la lógica de negocio a ``datetime.now`` del sistema.
    """

    def __call__(self) -> datetime:
        """Devuelve el instante actual como ``datetime`` consciente de zona (UTC)."""
        ...


def decide_final_status(
    counts: Counter[ItemStatus], item_count: int
) -> AnalysisStatus:
    """Decide el Estado_Terminal del Lote a partir de los recuentos reales.

    Función **total**: todo recuento produce exactamente uno de los tres estados
    terminales que ``aggregate_results`` puede fijar (criterios 10.7, 10.8, 10.9).
    Presupone que los Ítems huérfanos ya se recontaron como ``FAILED`` (criterio
    10.2), de modo que ``counts`` no contiene Ítems en ``PENDING``.

    Reglas (criterios 10.7-10.9):

    * ``failed_count == item_count`` → ``FAILED`` (no se pudo procesar nada).
    * ``0 < failed_count < item_count`` → ``COMPLETED_WITH_ERRORS``.
    * ``failed_count == 0`` → ``COMPLETED`` (``done_count + skipped_count == item_count``).

    Un Lote sin Ítems (``item_count == 0``) tiene ``failed_count == 0`` y, por la
    tercera regla, se cierra como ``COMPLETED``.

    :param counts: recuento de Ítems por :class:`ItemStatus`, contado desde los
        registros ITEM reales (nunca desde los contadores atómicos acumulados).
    :param item_count: número total de Ítems del Lote (``meta.item_count``).
    :return: el :class:`AnalysisStatus` terminal que corresponde a los recuentos.
    """
    failed = counts[ItemStatus.FAILED]
    if item_count > 0 and failed == item_count:
        return AnalysisStatus.FAILED
    if failed > 0:
        return AnalysisStatus.COMPLETED_WITH_ERRORS
    return AnalysisStatus.COMPLETED


def build_output_key(tenant_id: str, created_at: datetime, analysis_id: str) -> str:
    """Deriva la clave S3 del Documento_Consolidado en el prefijo ``outputs/``.

    Aplica el patrón ``outputs/{tenant_id}/{yyyy}/{mm}/{dd}/{analysis_id}.json``
    del criterio 10.3, con la fecha tomada del ``created_at`` del Lote (no del
    instante de cierre), de modo que la clave sea **determinista**: una
    reejecución del cierre produce exactamente la misma clave y, por tanto,
    sobrescribe el mismo objeto en lugar de crear uno nuevo (idempotencia de
    contenido, criterio 8.8). La partición por fecha coincide con la del input
    y habilita las reglas de ciclo de vida del bucket.

    :param tenant_id: organización propietaria del Lote; primer segmento del
        prefijo y frontera de aislamiento del rol de descarga.
    :param created_at: instante de creación del Lote, en UTC. Determina la
        partición ``{yyyy}/{mm}/{dd}``.
    :param analysis_id: ULID de 26 caracteres del Lote; nombre del objeto.
    :return: la clave S3 completa del Documento_Consolidado.
    """
    return (
        f"outputs/{tenant_id}/"
        f"{created_at:%Y}/{created_at:%m}/{created_at:%d}/"
        f"{analysis_id}.json"
    )


class AggregateAnalysisService:
    """Consolida el Lote, lo cierra en un Estado_Terminal y dispara la notificación.

    Encapsula el algoritmo de ``aggregate_results`` (sección 16.4 del diseño) con
    sus dependencias de infraestructura inyectadas como puertos. Es reutilizable y
    verificable sin AWS: basta inyectar dobles en memoria que satisfagan los
    protocolos.
    """

    def __init__(
        self,
        repository: AnalysisRepository,
        storage: ObjectStorage,
        events: EventPublisher,
        clock: Clock | Callable[[], datetime],
    ) -> None:
        """Inyecta los puertos y el reloj del servicio.

        :param repository: puerto de persistencia del Lote y sus Ítems. Aporta la
            lectura del META, la iteración paginada de Ítems, el cierre forzoso de
            huérfanos, el cierre del META y la marca de evento publicado.
        :param storage: puerto de almacenamiento de objetos JSON; escribe el
            Documento_Consolidado en el prefijo ``outputs/``.
        :param events: puerto de publicación del Evento_Finalizacion.
        :param clock: fuente del instante UTC de cierre (``completed_at`` del
            Documento_Consolidado). Inyectable para pruebas deterministas.
        """
        self._repository = repository
        self._storage = storage
        self._events = events
        self._clock = clock

    def execute(self, analysis_id: str, tenant_id: str) -> AnalysisMeta:
        """Consolida y cierra el Lote ``analysis_id`` del Tenant ``tenant_id``.

        Flujo (ver el docstring del módulo para el detalle de recuperación ante
        fallos):

        #. Lee el META. Si ya es terminal, devuelve ese META sin efectos
           secundarios (idempotencia, criterio 8.7).
        #. Recuenta los Ítems reales con :meth:`AnalysisRepository.iter_items`.
           Si el número leído no es ``item_count``, aborta con
           :class:`AggregationError` sin cerrar el Lote (criterios 10.5, 9.11).
        #. Cierra los Ítems huérfanos (``PENDING``) como ``FAILED`` /
           ``ORPHANED_AFTER_MAP`` y reajusta los recuentos (criterio 10.2).
        #. Ordena los Ítems por ``index``, decide el Estado_Terminal, escribe el
           Documento_Consolidado, cierra el META (fijando ``output_s3_key`` y
           ``completed_at``) y publica el Evento_Finalizacion; por último, marca
           el evento como publicado (criterios 10.1, 10.3, 10.6, 10.16).

        :param analysis_id: ULID del Lote a consolidar.
        :param tenant_id: Tenant propietario; acota la lectura del META
            (aislamiento multi-tenant).
        :return: el registro META ya en Estado_Terminal (releído tras el cierre),
            o el META terminal preexistente si la invocación fue idempotente.
        :raises AggregationError: si la lectura de Ítems no recupera los
            ``item_count`` registros esperados (``AGGREGATION_FAILED``); en ese
            caso no se fijan contadores ni Estado_Terminal.
        """
        meta = self._repository.get_meta(analysis_id, tenant_id)

        # Idempotencia (criterio 8.7): un Lote ya terminal no se reconsolida.
        if meta.is_terminal:
            return meta

        counts, items = self._count_items(analysis_id)

        # Conservación de ítems (criterios 10.5, 9.11): sin los item_count
        # registros, abortamos sin fijar contadores ni Estado_Terminal.
        if len(items) != meta.item_count:
            raise AggregationError(
                "item_records_missing",
                analysis_id=analysis_id,
                expected=meta.item_count,
                actual=len(items),
            )

        self._close_orphans(analysis_id, items, counts)

        # Orden del array original (criterios 10.6, 12.7): único por index.
        items.sort(key=lambda item: item.index)
        status = decide_final_status(counts, meta.item_count)
        output_key = build_output_key(tenant_id, meta.created_at, analysis_id)
        completed_at = self._clock()

        # (1) Documento primero: si cae antes del cierre del META, una
        # reejecución lo reconstruye idéntico (criterio 8.8). (2) Cierre del META
        # con output_s3_key y completed_at ANTES del evento (criterios 10.9, 10.16).
        self._storage.put_json(
            output_key,
            build_consolidated(meta, items, counts, status, completed_at),
        )
        self._repository.finalize_analysis(
            analysis_id,
            status,
            _counts_triple(counts),
            output_key,
        )

        # Releemos el META ya cerrado: aporta al evento el status terminal, los
        # contadores reconciliados, el output_s3_key y el completed_at persistidos.
        final_meta = self._repository.get_meta(analysis_id, tenant_id)

        # (3) Publicar y (4) marcar. El orden "publicar, luego marcar" da entrega
        # al-menos-una-vez; una caída entre ambos deja que Reconcile_Stuck
        # republique (criterio 10.19), y los consumidores deduplican por event_id.
        self._events.publish_analysis_completed(final_meta)
        self._repository.mark_event_published(analysis_id)
        return final_meta

    def _count_items(
        self, analysis_id: str
    ) -> tuple[Counter[ItemStatus], list[AnalysisItem]]:
        """Recuenta los Ítems reales del Lote desde la verdad (criterios 8.11, 10.4).

        Itera el generador paginado de :meth:`AnalysisRepository.iter_items`, que
        no carga el Lote completo en memoria de golpe. El recuento proviene de los
        registros ITEM, nunca de los contadores atómicos acumulados, lo que
        reconcilia cualquier incremento perdido (Property P11).

        Invariante de bucle: ``len(items)`` iguala el número de Ítems leídos hasta
        el momento; la ``Query`` de DynamoDB sobre un ``pk`` fijo no repite sort
        keys, de modo que ``items`` nunca contiene duplicados.

        :param analysis_id: ULID del Lote cuyos Ítems se recuentan.
        :return: una tupla ``(counts, items)`` con el recuento por estado y la
            lista completa de Ítems leídos (sin ordenar todavía).
        """
        counts: Counter[ItemStatus] = Counter()
        items: list[AnalysisItem] = []
        for item in self._repository.iter_items(analysis_id):
            counts[item.status] += 1
            items.append(item)
        return counts, items

    def _close_orphans(
        self,
        analysis_id: str,
        items: list[AnalysisItem],
        counts: Counter[ItemStatus],
    ) -> None:
        """Cierra como ``FAILED``/``ORPHANED_AFTER_MAP`` los Ítems aún en ``PENDING``.

        Garantiza la terminación de todo Ítem (criterio 10.2, Property P4): tras
        este paso ningún Ítem del Lote queda en ``PENDING``. La transición de cada
        Ítem usa :meth:`AnalysisRepository.fail_item`, que es atómica e
        idempotente; su valor de retorno no altera el recuento local porque este
        se reconcilia por completo desde los Ítems reales.

        Reajusta ``counts`` **en memoria** moviendo cada huérfano de ``PENDING`` a
        ``FAILED``, y además refleja la mutación en la proyección ``items`` para
        que el Documento_Consolidado muestre el estado y el ``error_code`` finales.

        :param analysis_id: ULID del Lote.
        :param items: proyección de Ítems leída; se muta in situ para los huérfanos.
        :param counts: recuento por estado; se reajusta restando de ``PENDING`` y
            sumando a ``FAILED`` el número de huérfanos.
        """
        stranded = [item for item in items if item.status == ItemStatus.PENDING]
        if not stranded:
            return
        for item in stranded:
            self._repository.fail_item(analysis_id, item.item_id, ORPHANED_AFTER_MAP)
            item.status = ItemStatus.FAILED
            item.error_code = ORPHANED_AFTER_MAP
        counts[ItemStatus.FAILED] += len(stranded)
        counts[ItemStatus.PENDING] = 0


def _counts_triple(counts: Counter[ItemStatus]) -> tuple[int, int, int]:
    """Proyecta el recuento a la tripleta ``(done, failed, skipped)`` que espera el repo.

    Es el orden que :meth:`AnalysisRepository.finalize_analysis` documenta para su
    parámetro ``counts``: ``DONE``, ``FAILED``, ``SKIPPED``.

    :param counts: recuento por :class:`ItemStatus`.
    :return: ``(done_count, failed_count, skipped_count)``.
    """
    return (
        counts[ItemStatus.DONE],
        counts[ItemStatus.FAILED],
        counts[ItemStatus.SKIPPED],
    )


def build_consolidated(
    meta: AnalysisMeta,
    items: Iterable[AnalysisItem],
    counts: Counter[ItemStatus],
    status: AnalysisStatus,
    completed_at: datetime,
) -> dict[str, object]:
    """Construye el Documento_Consolidado conforme a ``consolidated_output.schema.json``.

    El documento resultante cumple los criterios 12.7, 12.8 y 12.9:

    * ``items`` lleva exactamente un elemento por Ítem, **ordenado por ``index``**
      (el llamante los pasa ya ordenados), cada uno con su resultado clásico y
      profundo, o su ``error_code`` si falló (criterio 12.7).
    * ``resumen_por_indificador`` lleva una entrada por cada ``indificador``
      presente, con el recuento por ``sentimiento_general`` y el recuento de Ítems
      sin sentimiento clásico (``sin_clasico``), de modo que la suma de todos los
      recuentos del resumen iguala el ``item_count`` (criterio 12.8).
    * los metadatos de cabecera (``item_count`` y los tres contadores recontados,
      ``started_at`` = ``created_at`` y ``completed_at``) cumplen la invariante
      ``done + failed + skipped == item_count`` (criterio 12.9).

    :param meta: META del Lote (aporta identidad, Tenant y ``created_at``).
    :param items: Ítems del Lote, ya ordenados por ``index`` por el llamante.
    :param counts: recuento reconciliado por estado (incluidos los huérfanos ya
        contabilizados como ``FAILED``).
    :param status: Estado_Terminal decidido por :func:`decide_final_status`.
    :param completed_at: instante UTC de cierre, para la marca ``completed_at``.
    :return: el Documento_Consolidado como ``dict`` serializable a JSON.
    """
    item_views = [_item_result(item) for item in items]
    return {
        "schema_version": _SCHEMA_VERSION,
        "analysis_id": meta.analysis_id,
        "tenant_id": meta.tenant_id,
        "status": str(status.value),
        "item_count": meta.item_count,
        "done_count": counts[ItemStatus.DONE],
        "failed_count": counts[ItemStatus.FAILED],
        "skipped_count": counts[ItemStatus.SKIPPED],
        "started_at": _iso_utc(meta.created_at),
        "completed_at": _iso_utc(completed_at),
        "resumen_por_indificador": _summary_by_indificador(items),
        "items": item_views,
    }


def _item_result(item: AnalysisItem) -> dict[str, object]:
    """Proyecta un Ítem a su representación en el array ``items`` (``item_result`` schema).

    El estado determina qué campos están presentes (ver ``item_result.schema.json``):

    * un Ítem ``DONE`` lleva el resultado clásico (``language_code``,
      ``sentimientoGeneral``, ``scores``) cuando existe, y el profundo
      (``sentimientos``, ``explicacion``, ``model_id``) cuando existe;
    * un Ítem ``SKIPPED`` lleva ``sentimientos`` vacío y ``explicacion`` vacía;
    * un Ítem ``FAILED`` lleva su ``error_code`` del Catalogo_De_Errores.

    Los campos ausentes se **omiten** (no se emiten como ``null``) para respetar
    ``additionalProperties: false`` del esquema y el carácter opcional de cada uno.

    :param item: Ítem a proyectar; su ``status`` ya es terminal.
    :return: el ``dict`` del Ítem conforme a ``item_result.schema.json``.
    """
    result: dict[str, object] = {
        "item_id": item.item_id,
        "index": item.index,
        "indificador": item.indificador,
        "respuesta": item.respuesta,
        "status": str(item.status.value),
    }

    if item.classic is not None:
        result["language_code"] = item.classic.language_code
        result["language_confidence"] = _decimal_to_float(
            item.classic.language_confidence
        )
        result["sentimientoGeneral"] = str(item.classic.sentimiento_general.value)
        result["scores"] = {
            "positive": _decimal_to_float(item.classic.scores.positive),
            "negative": _decimal_to_float(item.classic.scores.negative),
            "neutral": _decimal_to_float(item.classic.scores.neutral),
            "mixed": _decimal_to_float(item.classic.scores.mixed),
        }

    if item.emotions is not None:
        result["sentimientos"] = list(item.emotions.sentimientos)
        result["explicacion"] = item.emotions.explicacion
        result["model_id"] = item.emotions.model_id
    elif item.status == ItemStatus.SKIPPED:
        # Un Ítem omitido no se analizó: sentimientos vacíos y explicación vacía.
        result["sentimientos"] = []
        result["explicacion"] = ""

    if item.status == ItemStatus.FAILED and item.error_code is not None:
        result["error_code"] = item.error_code

    if item.processed_at is not None:
        result["processed_at"] = _iso_utc(item.processed_at)

    return result


def _summary_by_indificador(
    items: Iterable[AnalysisItem],
) -> dict[str, dict[str, object]]:
    """Agrega el resumen por ``indificador`` del Documento_Consolidado (criterio 12.8).

    Por cada ``indificador`` presente en el Lote produce:

    * ``total``: número de Ítems de ese indificador;
    * ``sentimiento_general``: recuento por cada una de las cuatro etiquetas
      (``Positivo``/``Negativo``/``Neutral``/``Mixto``) de los Ítems con
      sentimiento clásico;
    * ``sentimientos_top``: las emociones del análisis profundo con su frecuencia,
      ordenadas de mayor a menor recuento (desempate alfabético estable), omitidas
      si ninguna aparece.

    Los Ítems sin sentimiento clásico (``SKIPPED``, ``FAILED`` o ``DONE`` sin
    resultado clásico) **no** suman a ninguna etiqueta de ``sentimiento_general``;
    cuentan en ``total``. Así la suma de todos los ``total`` del resumen iguala el
    ``item_count`` (criterio 12.8), aunque la suma de las etiquetas no lo haga.

    :param items: Ítems del Lote.
    :return: un ``dict`` ``indificador -> resumen`` conforme a
        ``consolidated_output.schema.json``.
    """
    totals: Counter[str] = Counter()
    sentiment_counts: dict[str, Counter[SentimientoGeneral]] = {}
    emotion_counts: dict[str, Counter[str]] = {}

    for item in items:
        indificador = item.indificador
        totals[indificador] += 1
        sentiment_counts.setdefault(indificador, Counter())
        emotion_counts.setdefault(indificador, Counter())
        if item.classic is not None:
            sentiment_counts[indificador][item.classic.sentimiento_general] += 1
        if item.emotions is not None:
            emotion_counts[indificador].update(item.emotions.sentimientos)

    summary: dict[str, dict[str, object]] = {}
    for indificador, total in totals.items():
        label_counts = sentiment_counts[indificador]
        summary[indificador] = {
            "total": total,
            "sentimiento_general": {
                str(label.value): label_counts[label]
                for label in _SUMMARY_SENTIMENT_LABELS
            },
            "sentimientos_top": _top_emotions(emotion_counts[indificador]),
        }
    return summary


def _top_emotions(emotion_counts: Counter[str]) -> list[dict[str, object]]:
    """Ordena las emociones por frecuencia descendente (desempate alfabético).

    El orden es determinista para que el Documento_Consolidado sea estable entre
    reejecuciones (criterio 8.8): a igualdad de recuento, se ordena por el nombre
    de la emoción ascendente.

    :param emotion_counts: recuento de cada emoción dentro de un indificador.
    :return: lista de ``{"sentimiento": str, "count": int}`` ordenada; vacía si no
        hay ninguna emoción (``count`` es siempre ``>= 1``, por el esquema).
    """
    ordered = sorted(emotion_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [{"sentimiento": name, "count": count} for name, count in ordered]


def _decimal_to_float(value: Decimal) -> float:
    """Convierte un ``Decimal`` persistido a ``float`` para la serialización JSON.

    Los scores de Comprehend se guardan como ``Decimal`` (DynamoDB no admite
    ``float``), pero el Documento_Consolidado los expone como ``number`` del
    esquema; esta conversión los adapta sin perder el valor en el rango ``[0, 1]``.

    :param value: score en ``Decimal`` dentro de ``[0, 1]``.
    :return: el mismo valor como ``float``.
    """
    return float(value)


def _iso_utc(moment: datetime) -> str:
    """Formatea un ``datetime`` en ISO 8601 UTC con sufijo ``Z``.

    Normaliza el desfase ``+00:00`` a ``Z`` para que las marcas de tiempo del
    documento sean consistentes con el resto del Sistema (p. ej. el formato que
    emite el Evento_Finalizacion). Presupone un ``datetime`` consciente de zona.

    :param moment: instante consciente de zona (UTC) a formatear.
    :return: la marca de tiempo en ISO 8601, p. ej. ``2025-06-14T10:00:24Z``.
    """
    return moment.isoformat().replace("+00:00", "Z")
