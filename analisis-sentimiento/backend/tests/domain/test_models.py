"""Pruebas unitarias del modelo de dominio puro (tarea 3.6).

Verifican el comportamiento observable de :mod:`sentiment.domain.models` que el
plan de pruebas asigna a las pruebas unitarias para los criterios 6.3 y 11.2:

* :meth:`SentimientoGeneral.from_comprehend` traduce las cuatro etiquetas conocidas
  de Amazon Comprehend a su equivalente en español (criterio 6.3) y es total:
  toda etiqueta desconocida, vacía o malformada cae en ``NO_DETERMINADO`` sin
  lanzar (criterio 6.4, la cara negativa del mapeo que 6.3 presupone);
* :attr:`AnalysisMeta.progress_ratio` queda acotado al intervalo cerrado
  ``[0.0, 1.0]``: un Lote sin Ítems devuelve ``1.0`` sin dividir por cero, los
  recuentos parciales se calculan correctamente y los contadores que exceden el
  ``item_count`` se recortan a ``1.0`` (criterio 11.2);
* la pertenencia a los conjuntos ``TERMINAL_*`` y la propiedad derivada
  :attr:`AnalysisMeta.is_terminal` distinguen correctamente los estados terminales
  de los no terminales, tanto para el Lote como para el Ítem.

La prueba de propiedad del determinismo del ``item_id`` (P7, tarea 3.5) y las
pruebas del Catalogo_De_Errores (``test_errors.py``, tarea 3.2) viven en sus
propios módulos; aquí no se duplican.
"""

from __future__ import annotations

import pytest

from sentiment.domain.models import (
    TERMINAL_ANALYSIS_STATUSES,
    TERMINAL_ITEM_STATUSES,
    AnalysisMeta,
    AnalysisStatus,
    ItemStatus,
    SentimientoGeneral,
)


def _meta(
    *,
    item_count: int,
    done: int = 0,
    failed: int = 0,
    skipped: int = 0,
    status: AnalysisStatus = AnalysisStatus.RUNNING,
) -> AnalysisMeta:
    """Construye un ``AnalysisMeta`` mínimo para ejercitar sus propiedades.

    Sólo fija los campos relevantes para ``progress_ratio`` e ``is_terminal``;
    el resto toma valores sintéticos deterministas. Mantiene las pruebas legibles
    y evita repetir el constructor completo en cada caso.
    """
    return AnalysisMeta(
        analysis_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tenant_id="tenant-test",
        owner_sub="sub-test",
        status=status,
        item_count=item_count,
        done_count=done,
        failed_count=failed,
        skipped_count=skipped,
    )


class TestFromComprehend:
    """Mapeo total de la etiqueta de Comprehend a ``SentimientoGeneral`` (6.3)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("POSITIVE", SentimientoGeneral.POSITIVO),
            ("NEGATIVE", SentimientoGeneral.NEGATIVO),
            ("NEUTRAL", SentimientoGeneral.NEUTRAL),
            ("MIXED", SentimientoGeneral.MIXTO),
        ],
    )
    def test_known_labels_map_to_spanish(
        self, raw: str, expected: SentimientoGeneral
    ) -> None:
        """Las cuatro etiquetas de Comprehend se traducen al español (criterio 6.3)."""
        assert SentimientoGeneral.from_comprehend(raw) is expected

    @pytest.mark.parametrize(
        "expected_value",
        [
            ("POSITIVE", "Positivo"),
            ("NEGATIVE", "Negativo"),
            ("NEUTRAL", "Neutral"),
            ("MIXED", "Mixto"),
        ],
    )
    def test_known_labels_have_exact_spanish_string(
        self, expected_value: tuple[str, str]
    ) -> None:
        """El valor del enum es la grafía española exacta que exige el criterio 6.3."""
        raw, spanish = expected_value
        assert SentimientoGeneral.from_comprehend(raw).value == spanish

    @pytest.mark.parametrize(
        "raw",
        [
            "",  # cadena vacia
            "positive",  # minusculas: Comprehend emite mayusculas
            "Positive",  # capitalizacion distinta
            " POSITIVE",  # espacio inicial
            "POSITIVE ",  # espacio final
            "UNKNOWN",  # etiqueta inexistente
            "garbage",  # basura arbitraria
            "NEUTRALISH",  # prefijo de una etiqueta valida
            "POS",  # fragmento
            "123",  # numero como texto
            "No determinado",  # ya traducido: no es etiqueta de Comprehend
        ],
    )
    def test_unknown_labels_map_to_no_determinado(self, raw: str) -> None:
        """Toda etiqueta no reconocida cae en ``NO_DETERMINADO`` (criterio 6.4)."""
        assert SentimientoGeneral.from_comprehend(raw) is SentimientoGeneral.NO_DETERMINADO

    @pytest.mark.parametrize(
        "raw",
        ["", "UNKNOWN", "garbage", "positive", "POSITIVE ", "\n", "\t ", "💥"],
    )
    def test_is_total_never_raises(self, raw: str) -> None:
        """La función es total: ningún valor de entrada provoca una excepción."""
        result = SentimientoGeneral.from_comprehend(raw)
        assert isinstance(result, SentimientoGeneral)

    def test_mapping_is_case_sensitive_by_design(self) -> None:
        """El mapeo exige la grafía exacta de Comprehend (mayúsculas).

        Comprehend emite las etiquetas en mayúsculas; cualquier otra grafía se
        trata como desconocida en lugar de normalizarse, para no enmascarar una
        respuesta inesperada de la dependencia.
        """
        assert (
            SentimientoGeneral.from_comprehend("positive")
            is SentimientoGeneral.NO_DETERMINADO
        )
        assert SentimientoGeneral.from_comprehend("POSITIVE") is SentimientoGeneral.POSITIVO


class TestProgressRatio:
    """``progress_ratio`` acotado al intervalo ``[0.0, 1.0]`` (criterio 11.2)."""

    def test_empty_batch_is_complete_without_divide_by_zero(self) -> None:
        """``item_count == 0`` devuelve ``1.0`` sin dividir por cero (criterio 11.2)."""
        assert _meta(item_count=0).progress_ratio == 1.0

    def test_fresh_batch_is_zero(self) -> None:
        """Un Lote recién sembrado, sin Ítems procesados, tiene progreso ``0.0``."""
        assert _meta(item_count=10).progress_ratio == 0.0

    @pytest.mark.parametrize(
        ("done", "failed", "skipped", "item_count", "expected"),
        [
            (1, 0, 0, 4, 0.25),
            (0, 1, 0, 4, 0.25),
            (0, 0, 1, 4, 0.25),
            (1, 1, 1, 4, 0.75),
            (2, 1, 1, 4, 1.0),
            (3, 0, 0, 10, 0.3),
            (1, 0, 0, 8, 0.125),
        ],
    )
    def test_partial_counts_compute_the_fraction(
        self, done: int, failed: int, skipped: int, item_count: int, expected: float
    ) -> None:
        """El progreso es ``(done + failed + skipped) / item_count`` (criterio 11.2)."""
        meta = _meta(item_count=item_count, done=done, failed=failed, skipped=skipped)
        assert meta.progress_ratio == pytest.approx(expected)

    def test_all_items_processed_is_one(self) -> None:
        """Cuando todos los Ítems están procesados el progreso es exactamente ``1.0``."""
        meta = _meta(item_count=6, done=4, failed=1, skipped=1)
        assert meta.progress_ratio == 1.0

    @pytest.mark.parametrize(
        ("done", "failed", "skipped", "item_count"),
        [
            (10, 0, 0, 5),  # done solo ya excede
            (5, 5, 5, 5),  # suma triplica el total
            (6, 0, 0, 5),  # exceso por un unico contador
            (3, 3, 3, 1),  # contadores desbordados sobre un Lote de 1
        ],
    )
    def test_counts_exceeding_item_count_clamp_to_one(
        self, done: int, failed: int, skipped: int, item_count: int
    ) -> None:
        """Contadores transitoriamente inconsistentes se recortan a ``1.0`` (11.2)."""
        meta = _meta(item_count=item_count, done=done, failed=failed, skipped=skipped)
        assert meta.progress_ratio == 1.0

    @pytest.mark.parametrize(
        ("done", "failed", "skipped", "item_count"),
        [
            (0, 0, 0, 1),
            (1, 0, 0, 3),
            (2, 1, 0, 7),
            (3, 3, 3, 9),
            (100, 0, 0, 50),  # exceso: debe seguir dentro del rango
            (0, 0, 0, 1000),
        ],
    )
    def test_result_always_within_closed_unit_interval(
        self, done: int, failed: int, skipped: int, item_count: int
    ) -> None:
        """La invariante del criterio 11.2: el resultado nunca sale de ``[0.0, 1.0]``."""
        ratio = _meta(
            item_count=item_count, done=done, failed=failed, skipped=skipped
        ).progress_ratio
        assert 0.0 <= ratio <= 1.0


class TestTerminalStatuses:
    """Pertenencia a los conjuntos ``TERMINAL_*`` y ``is_terminal`` del Lote."""

    @pytest.mark.parametrize(
        "status",
        [
            AnalysisStatus.COMPLETED,
            AnalysisStatus.COMPLETED_WITH_ERRORS,
            AnalysisStatus.FAILED,
            AnalysisStatus.EXPIRED,
        ],
    )
    def test_terminal_analysis_statuses_are_terminal(
        self, status: AnalysisStatus
    ) -> None:
        """Los estados terminales del Lote están en el conjunto y marcan ``is_terminal``."""
        assert status in TERMINAL_ANALYSIS_STATUSES
        assert _meta(item_count=1, status=status).is_terminal is True

    @pytest.mark.parametrize(
        "status",
        [AnalysisStatus.PENDING, AnalysisStatus.RUNNING],
    )
    def test_non_terminal_analysis_statuses_are_not_terminal(
        self, status: AnalysisStatus
    ) -> None:
        """``PENDING`` y ``RUNNING`` no son terminales ni marcan ``is_terminal``."""
        assert status not in TERMINAL_ANALYSIS_STATUSES
        assert _meta(item_count=1, status=status).is_terminal is False

    def test_terminal_analysis_set_is_exactly_the_four_terminal_states(self) -> None:
        """El conjunto terminal del Lote es exactamente los cuatro estados finales.

        Protege contra que un nuevo estado del enum entre o salga del conjunto
        por descuido: la partición terminal/no terminal debe cubrir todo el enum.
        """
        non_terminal = {AnalysisStatus.PENDING, AnalysisStatus.RUNNING}
        assert set(AnalysisStatus) - non_terminal == TERMINAL_ANALYSIS_STATUSES

    @pytest.mark.parametrize(
        "status",
        [ItemStatus.DONE, ItemStatus.FAILED, ItemStatus.SKIPPED],
    )
    def test_terminal_item_statuses_are_terminal(self, status: ItemStatus) -> None:
        """``DONE``, ``FAILED`` y ``SKIPPED`` son estados terminales del Ítem."""
        assert status in TERMINAL_ITEM_STATUSES

    def test_pending_item_status_is_not_terminal(self) -> None:
        """``PENDING`` es el único estado no terminal del Ítem."""
        assert ItemStatus.PENDING not in TERMINAL_ITEM_STATUSES

    def test_terminal_item_set_is_exactly_the_three_terminal_states(self) -> None:
        """El conjunto terminal del Ítem es todo el enum salvo ``PENDING``."""
        assert set(ItemStatus) - {ItemStatus.PENDING} == TERMINAL_ITEM_STATUSES
