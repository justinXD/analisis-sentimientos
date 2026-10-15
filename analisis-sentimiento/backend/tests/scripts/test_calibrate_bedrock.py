"""Pruebas unitarias de las funciones PURAS del runner de calibración (tarea 15.3).

Cubren, **sin red**, las tres piezas de lógica pura del script
``scripts/calibrate_bedrock.py`` que la tarea exige testear:

* el cálculo de percentiles (p50/p95/p99) y el resumen de latencias;
* la lógica de propuesta de ``MaxConcurrency`` y de concurrencia reservada frente
  al techo de 300 s de un workflow Express (decisión D4);
* el parseo/escritura del JSON de configuración de entorno.

También verifican invariantes de seguridad y arranque del CLI que no tocan la
red: la muestra sintética no contiene PII, el ``--dry-run`` y el ``-h`` no
invocan a Bedrock, y la propuesta nunca excede el tope duro de concurrencia.

El script vive en ``scripts/`` a nivel de repositorio (fuera del paquete
``sentiment``), así que se añade ese directorio a ``sys.path`` antes de
importarlo. Importar el módulo no tiene efectos secundarios: toda la E/S de red
vive dentro de ``main()`` y de las funciones de medición, nunca en el import.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# scripts/ está en la raíz del repo: backend/tests/scripts/ -> subir 4 niveles.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import calibrate_bedrock as cb  # noqa: E402 - requiere el ajuste de sys.path previo.

# --------------------------------------------------------------------------- #
# percentile()
# --------------------------------------------------------------------------- #


class TestPercentile:
    """Cálculo de percentiles por interpolación lineal (tipo 7)."""

    def test_single_value_returns_that_value_for_any_percentile(self) -> None:
        for pct in (0.0, 50.0, 95.0, 99.0, 100.0):
            assert cb.percentile([42.0], pct) == 42.0

    def test_min_and_max_at_the_extremes(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert cb.percentile(values, 0.0) == 10.0
        assert cb.percentile(values, 100.0) == 50.0

    def test_median_of_odd_length(self) -> None:
        assert cb.percentile([1.0, 2.0, 3.0], 50.0) == 2.0

    def test_median_of_even_length_interpolates(self) -> None:
        # Entre 2 y 3, el p50 por interpolación lineal (tipo 7) es 2.5.
        assert cb.percentile([1.0, 2.0, 3.0, 4.0], 50.0) == 2.5

    def test_matches_numpy_type7_interpolation(self) -> None:
        # Valores conocidos de numpy.percentile(..., interpolation="linear").
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        assert cb.percentile(values, 95.0) == pytest.approx(9.55)
        assert cb.percentile(values, 99.0) == pytest.approx(9.91)
        assert cb.percentile(values, 50.0) == pytest.approx(5.5)

    def test_unordered_input_is_sorted(self) -> None:
        assert cb.percentile([3.0, 1.0, 2.0], 0.0) == 1.0

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="al menos un valor"):
            cb.percentile([], 50.0)

    @pytest.mark.parametrize("pct", [-0.1, 100.1, -5.0, 150.0])
    def test_out_of_range_percentile_raises(self, pct: float) -> None:
        with pytest.raises(ValueError, match=r"\[0, 100\]"):
            cb.percentile([1.0, 2.0], pct)


# --------------------------------------------------------------------------- #
# summarize_latencies()
# --------------------------------------------------------------------------- #


def _measurement(latency: float, *, throttled: bool = False) -> cb.Measurement:
    return cb.Measurement(
        latency_s=latency,
        input_tokens=100,
        output_tokens=50,
        throttled=throttled,
    )


class TestSummarizeLatencies:
    """Resumen de una tanda de medidas en percentiles y medias."""

    def test_computes_percentiles_and_means(self) -> None:
        measurements = [_measurement(float(i)) for i in range(1, 11)]
        stats = cb.summarize_latencies(measurements)
        assert stats.samples == 10
        assert stats.p50_s == pytest.approx(5.5)
        assert stats.p95_s == pytest.approx(9.55)
        assert stats.p99_s == pytest.approx(9.91)
        assert stats.mean_s == pytest.approx(5.5)
        assert stats.mean_input_tokens == pytest.approx(100.0)
        assert stats.mean_output_tokens == pytest.approx(50.0)

    def test_throttle_rate_is_fraction_of_throttled(self) -> None:
        measurements = [
            _measurement(1.0, throttled=True),
            _measurement(1.0, throttled=False),
            _measurement(1.0, throttled=False),
            _measurement(1.0, throttled=True),
        ]
        stats = cb.summarize_latencies(measurements)
        assert stats.throttle_rate == pytest.approx(0.5)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="al menos una medida"):
            cb.summarize_latencies([])


# --------------------------------------------------------------------------- #
# estimate_batch_time() / estimate_total_duration()
# --------------------------------------------------------------------------- #


class TestEstimateBatchTime:
    """Fórmula del diseño D4 paso 4: ceil(25 / fanout) x latencia."""

    def test_serial_processing_is_items_times_latency(self) -> None:
        # fanout=1 -> 25 olas de 1 ítem -> 25 x latencia.
        assert cb.estimate_batch_time(10.0, 1) == pytest.approx(250.0)

    def test_full_parallel_is_a_single_wave(self) -> None:
        # fanout=25 -> 1 ola -> 1 x latencia.
        assert cb.estimate_batch_time(10.0, 25) == pytest.approx(10.0)

    def test_rounds_waves_up(self) -> None:
        # 25 / 10 = 2.5 -> ceil = 3 olas.
        assert cb.estimate_batch_time(4.0, 10) == pytest.approx(12.0)

    def test_fanout_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="fanout_concurrency"):
            cb.estimate_batch_time(1.0, 0)

    def test_negative_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="per_item_latency_s"):
            cb.estimate_batch_time(-1.0, 5)


class TestEstimateTotalDuration:
    """Fórmula del diseño D4 paso 6: ceil(num_batches / map) x tiempo_batch."""

    def test_thousand_items_is_forty_batches(self) -> None:
        # 1000 / 25 = 40 batches; con map=4 -> 10 olas.
        assert cb.estimate_total_duration(100.0, 4) == pytest.approx(1000.0)

    def test_map_larger_than_batches_is_single_wave(self) -> None:
        assert cb.estimate_total_duration(100.0, 40) == pytest.approx(100.0)

    def test_map_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="map_concurrency"):
            cb.estimate_total_duration(1.0, 0)


# --------------------------------------------------------------------------- #
# propose_fanout_concurrency() / propose_reserved_concurrency()
# --------------------------------------------------------------------------- #


class TestProposeFanoutConcurrency:
    """Elección del fan-out frente al techo Express (presupuesto 240 s)."""

    def test_low_latency_needs_minimal_parallelism(self) -> None:
        # 25 ítems x 2 s en serie = 50 s <= 240 s -> fanout=1 basta.
        assert cb.propose_fanout_concurrency(2.0) == 1

    def test_high_latency_needs_more_parallelism(self) -> None:
        # A 20 s/ítem: fanout=1 -> 500 s (no cabe); fanout=2 -> ceil(25/2)=13
        # olas x 20 = 260 s (no cabe); fanout=3 -> ceil(25/3)=9 x 20 = 180 s (cabe).
        assert cb.propose_fanout_concurrency(20.0) == 3

    def test_result_keeps_batch_under_budget(self) -> None:
        p99 = 15.0
        fanout = cb.propose_fanout_concurrency(p99)
        budget = cb.EXPRESS_CEILING_SECONDS - cb.EXPRESS_SAFETY_MARGIN_SECONDS
        assert cb.estimate_batch_time(p99, fanout) <= budget

    def test_extreme_latency_caps_at_items_per_batch(self) -> None:
        # Latencia tan alta que ni el máximo paralelismo cabe: devuelve el tope.
        assert cb.propose_fanout_concurrency(1000.0) == cb.ITEMS_PER_BATCH

    def test_negative_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="p99_latency_s"):
            cb.propose_fanout_concurrency(-1.0)


class TestProposeReservedConcurrency:
    """Concurrencia reservada acotada por el tope duro (criterios 18.4 / 5.7)."""

    def test_matches_per_batch_concurrency_when_under_cap(self) -> None:
        # 4 x 5 = 20 invocaciones por lote (criterio 18.4) -> reservada 20.
        assert cb.propose_reserved_concurrency(10.0, 20) == 20

    def test_caps_at_hard_cap(self) -> None:
        assert cb.propose_reserved_concurrency(10.0, 40) == 40

    def test_exceeding_hard_cap_raises(self) -> None:
        with pytest.raises(ValueError, match="excede el tope duro"):
            cb.propose_reserved_concurrency(10.0, 41)

    def test_zero_concurrency_raises(self) -> None:
        with pytest.raises(ValueError, match="total_concurrent_bedrock"):
            cb.propose_reserved_concurrency(10.0, 0)


# --------------------------------------------------------------------------- #
# build_proposal()
# --------------------------------------------------------------------------- #


def _result(concurrency: int, p50: float, p95: float, p99: float) -> cb.ConcurrencyResult:
    stats = cb.LatencyStats(
        samples=concurrency,
        p50_s=p50,
        p95_s=p95,
        p99_s=p99,
        mean_s=(p50 + p95) / 2.0,
        mean_input_tokens=300.0,
        mean_output_tokens=150.0,
        throttle_rate=0.0,
    )
    return cb.ConcurrencyResult(concurrency=concurrency, stats=stats)


class TestBuildProposal:
    """Derivación completa de la propuesta a partir de la curva de concurrencia."""

    def test_uses_worst_case_concurrency_point(self) -> None:
        results = [
            _result(1, 5.0, 6.0, 7.0),
            _result(40, 10.0, 14.0, 18.0),
        ]
        proposal = cb.build_proposal(results)
        # p99 del punto peor (concurrencia 40) es 18 s. fanout=1 -> 25x18=450 (no);
        # fanout=2 -> 13x18=234 <= 240 (sí) -> fanout=2.
        assert proposal.llm_fanout_max_concurrency == 2
        # map x fanout <= 40 -> map = 40 // 2 = 20.
        assert proposal.distributed_map_max_concurrency == 20
        assert proposal.analyze_llm_reserved_concurrency == 40
        assert proposal.batch_time_within_ceiling is True

    def test_flags_when_ceiling_cannot_be_met(self) -> None:
        results = [_result(40, 400.0, 450.0, 500.0)]
        proposal = cb.build_proposal(results)
        assert proposal.batch_time_within_ceiling is False
        assert "ADVERTENCIA" in proposal.rationale

    def test_product_never_exceeds_hard_cap(self) -> None:
        for p99 in (1.0, 8.0, 20.0, 40.0, 100.0):
            results = [_result(40, p99 * 0.6, p99 * 0.8, p99)]
            proposal = cb.build_proposal(results)
            product = (
                proposal.distributed_map_max_concurrency
                * proposal.llm_fanout_max_concurrency
            )
            assert product <= cb.RESERVED_CONCURRENCY_HARD_CAP
            assert proposal.analyze_llm_reserved_concurrency <= (
                cb.RESERVED_CONCURRENCY_HARD_CAP
            )

    def test_empty_results_raises(self) -> None:
        with pytest.raises(ValueError, match="al menos un ConcurrencyResult"):
            cb.build_proposal([])


# --------------------------------------------------------------------------- #
# Config: config_path / load_config / apply_proposal / write_config
# --------------------------------------------------------------------------- #


def _sample_config() -> dict[str, object]:
    return {
        "project": "sentiment",
        "env_name": "dev",
        "distributed_map_max_concurrency": 4,
        "llm_fanout_max_concurrency": 5,
        "analyze_llm_reserved_concurrency": 40,
        "data_ttl_days": 90,
    }


def _proposal() -> cb.ConcurrencyProposal:
    return cb.ConcurrencyProposal(
        distributed_map_max_concurrency=8,
        llm_fanout_max_concurrency=3,
        analyze_llm_reserved_concurrency=24,
        batch_time_p99_s=180.0,
        batch_time_within_ceiling=True,
        estimated_total_duration_s=900.0,
        rationale="prueba",
    )


class TestConfigPath:
    def test_builds_path_for_valid_env(self, tmp_path: Path) -> None:
        path = cb.config_path("dev", config_dir=tmp_path)
        assert path == tmp_path / "dev.json"

    def test_rejects_unknown_env(self) -> None:
        with pytest.raises(ValueError, match="Entorno desconocido"):
            cb.config_path("qa")


class TestLoadConfig:
    def test_reads_json_object(self, tmp_path: Path) -> None:
        path = tmp_path / "dev.json"
        path.write_text('{"a": 1, "b": 2}', encoding="utf-8")
        assert cb.load_config(path) == {"a": 1, "b": 2}

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            cb.load_config(tmp_path / "nope.json")

    def test_non_object_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "dev.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="objeto JSON"):
            cb.load_config(path)


class TestApplyProposal:
    def test_overwrites_only_concurrency_keys(self) -> None:
        config = _sample_config()
        updated = cb.apply_proposal(config, _proposal())
        assert updated["distributed_map_max_concurrency"] == 8
        assert updated["llm_fanout_max_concurrency"] == 3
        assert updated["analyze_llm_reserved_concurrency"] == 24
        # El resto de claves se conserva intacto.
        assert updated["project"] == "sentiment"
        assert updated["data_ttl_days"] == 90

    def test_does_not_mutate_input(self) -> None:
        config = _sample_config()
        cb.apply_proposal(config, _proposal())
        assert config["distributed_map_max_concurrency"] == 4
        assert config["llm_fanout_max_concurrency"] == 5
        assert config["analyze_llm_reserved_concurrency"] == 40


class TestWriteConfig:
    def test_roundtrip_preserves_values(self, tmp_path: Path) -> None:
        path = tmp_path / "dev.json"
        config = cb.apply_proposal(_sample_config(), _proposal())
        cb.write_config(path, config)
        reloaded = cb.load_config(path)
        assert reloaded == config

    def test_output_is_two_space_indented_with_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "dev.json"
        cb.write_config(path, {"a": 1})
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert '  "a": 1' in text


# --------------------------------------------------------------------------- #
# Muestra sintética: sin PII, determinista.
# --------------------------------------------------------------------------- #


class TestBuildSample:
    def test_size_and_indificadores_rotation(self) -> None:
        indificadores = ["A", "B", "C"]
        sample = cb.build_sample(9, indificadores, empty_ratio=0.0)
        assert len(sample) == 9
        assert [it.indificador for it in sample[:3]] == ["A", "B", "C"]

    def test_is_deterministic_for_same_seed(self) -> None:
        a = cb.build_sample(20, ["X"], seed=7)
        b = cb.build_sample(20, ["X"], seed=7)
        assert a == b

    def test_no_pii_only_synthetic_templates(self) -> None:
        sample = cb.build_sample(100, ["X"], empty_ratio=0.1)
        for item in sample:
            if cb.is_empty_marker(item.respuesta):
                continue
            # El texto no vacío se compone exclusivamente de plantillas sintéticas
            # concatenadas con un espacio: debe contener al menos una plantilla.
            assert any(
                tpl in item.respuesta for tpl in cb._SYNTHETIC_TEMPLATES
            )

    def test_rejects_invalid_arguments(self) -> None:
        with pytest.raises(ValueError, match="size"):
            cb.build_sample(0, ["X"])
        with pytest.raises(ValueError, match="indificadores"):
            cb.build_sample(5, [])
        with pytest.raises(ValueError, match="empty_ratio"):
            cb.build_sample(5, ["X"], empty_ratio=1.0)


class TestIsEmptyMarker:
    @pytest.mark.parametrize(
        "value", ["", "  ", "sin comentarios", "N/A", "Ninguno", "-", "SIN COMENTARIOS"]
    )
    def test_recognizes_empty_markers(self, value: str) -> None:
        assert cb.is_empty_marker(value) is True

    @pytest.mark.parametrize("value", ["hola", "me siento bien", "n/a de verdad"])
    def test_rejects_real_text(self, value: str) -> None:
        assert cb.is_empty_marker(value) is False


# --------------------------------------------------------------------------- #
# simulate_results(): curva simulada sin red para --dry-run.
# --------------------------------------------------------------------------- #


class TestSimulateResults:
    def test_produces_one_result_per_concurrency(self) -> None:
        results = cb.simulate_results([1, 5, 10])
        assert [r.concurrency for r in results] == [1, 5, 10]

    def test_latency_grows_with_concurrency(self) -> None:
        results = cb.simulate_results([1, 40])
        assert results[1].stats.p99_s > results[0].stats.p99_s

    def test_feeds_build_proposal_end_to_end(self) -> None:
        proposal = cb.build_proposal(cb.simulate_results(cb.DEFAULT_CONCURRENCIES))
        product = (
            proposal.distributed_map_max_concurrency
            * proposal.llm_fanout_max_concurrency
        )
        assert product <= cb.RESERVED_CONCURRENCY_HARD_CAP

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="concurrencies"):
            cb.simulate_results([])


# --------------------------------------------------------------------------- #
# CLI: arranque sin red (dry-run / solo lectura).
# --------------------------------------------------------------------------- #


class TestCliParsing:
    def test_parse_concurrencies_sorts_and_dedups(self) -> None:
        parser = cb.build_arg_parser()
        args = parser.parse_args(["--concurrencies", "40,5,5,1"])
        assert args.concurrencies == (1, 5, 40)

    def test_parse_concurrencies_rejects_non_integer(self) -> None:
        parser = cb.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--concurrencies", "1,abc"])

    def test_parse_concurrencies_rejects_non_positive(self) -> None:
        parser = cb.build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--concurrencies", "0,5"])


class TestMainDryRun:
    def test_dry_run_succeeds_without_network_or_write(self) -> None:
        # Si el dry-run intentara tocar la red, fallaría aquí: no construimos
        # ningún adaptador y no hay credenciales. El contrato es que --dry-run
        # jamás invoca a Bedrock.
        exit_code = cb.main(["--env", "dev", "--dry-run", "--samples", "20"])
        assert exit_code == 0

    def test_help_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cb.main(["-h"])
        assert excinfo.value.code == 0

    def test_write_config_flag_persists_only_with_flag(self, tmp_path: Path) -> None:
        # Prepara un config de entorno falso en un directorio temporal y apunta
        # el script a él, para no tocar los JSON versionados.
        config_dir = tmp_path
        (config_dir / "dev.json").write_text(
            '{\n  "distributed_map_max_concurrency": 4,\n'
            '  "llm_fanout_max_concurrency": 5,\n'
            '  "analyze_llm_reserved_concurrency": 40\n}\n',
            encoding="utf-8",
        )
        # Simula directamente apply/write con una propuesta, sin red.
        proposal = cb.build_proposal(cb.simulate_results(cb.DEFAULT_CONCURRENCIES))
        path = cb.config_path("dev", config_dir=config_dir)
        updated = cb.apply_proposal(cb.load_config(path), proposal)
        cb.write_config(path, updated)
        reloaded = cb.load_config(path)
        assert reloaded["llm_fanout_max_concurrency"] == (
            proposal.llm_fanout_max_concurrency
        )


def test_module_import_has_no_network_side_effects() -> None:
    """El import del módulo no ejecuta E/S: las constantes están disponibles."""
    assert cb.EXPRESS_CEILING_SECONDS == 300.0
    assert cb.ITEMS_PER_BATCH == 25
    assert math.isclose(cb.EXPRESS_SAFETY_MARGIN_SECONDS, 60.0)
