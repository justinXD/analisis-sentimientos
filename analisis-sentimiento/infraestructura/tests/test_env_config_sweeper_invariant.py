"""Pruebas de la invariante umbral del sweeper vs ventana de la cola (tarea 1.7).

Verifican la invariante del criterio 10.12 implementada en
:meth:`config.environments.EnvConfig.__post_init__`:

    stuck_threshold_seconds
        >= queue_max_receive_count * queue_visibility_timeout_seconds
           + SWEEPER_MARGIN_SECONDS

Cubren el caso que pasa (umbral suficiente) y el que falla (umbral insuficiente),
el comportamiento exacto en el límite, y que los tres entornos reales
(``dev`` / ``stage`` / ``prod``) satisfacen la invariante al cargarse.
"""

from __future__ import annotations

import dataclasses

import pytest

from config.environments import (
    SWEEPER_MARGIN_SECONDS,
    VALID_ENVIRONMENTS,
    EnvConfig,
    load_env_config,
)

# Base válida reutilizable: los valores nominales del diseño
# (3 × 180 + 360 = 900). Cada prueba la ajusta con ``dataclasses.replace``.
_BASE_KWARGS: dict[str, object] = {
    "project": "sentiment",
    "env_name": "dev",
    "log_retention_days": 30,
    "cors_origins": ("http://localhost:5173",),
    "bedrock_model_id": "<MODEL_ID_A_CALIBRAR>",
    "ses_sender_email": "no-reply-test@sentiment.example.com",
    "distributed_map_max_concurrency": 4,
    "llm_fanout_max_concurrency": 5,
    "analyze_llm_reserved_concurrency": 40,
    "queue_visibility_timeout_seconds": 180,
    "queue_max_receive_count": 3,
    "stuck_threshold_seconds": 900,
    "data_ttl_days": 90,
    "waf_mode": "COUNT",
}


def _make(**overrides: object) -> EnvConfig:
    """Construye un :class:`EnvConfig` a partir de la base válida con overrides."""

    return EnvConfig(**{**_BASE_KWARGS, **overrides})


class TestSweeperInvariantPasses:
    """Casos en los que el umbral del sweeper es suficiente."""

    def test_nominal_values_are_accepted(self) -> None:
        config = _make()  # 900 >= 3 * 180 + 360 = 900

        assert config.stuck_threshold_seconds == 900

    def test_threshold_exactly_at_minimum_is_accepted(self) -> None:
        # El mínimo es inclusivo: >= (no >).
        minimum = 3 * 180 + SWEEPER_MARGIN_SECONDS

        config = _make(stuck_threshold_seconds=minimum)

        assert config.stuck_threshold_seconds == minimum

    def test_threshold_above_minimum_is_accepted(self) -> None:
        config = _make(stuck_threshold_seconds=5_000)

        assert config.stuck_threshold_seconds == 5_000


class TestSweeperInvariantFails:
    """Casos en los que el umbral del sweeper es insuficiente."""

    def test_threshold_one_second_below_minimum_is_rejected(self) -> None:
        minimum = 3 * 180 + SWEEPER_MARGIN_SECONDS

        with pytest.raises(ValueError) as excinfo:
            _make(stuck_threshold_seconds=minimum - 1)

        # El mensaje debe nombrar los valores en conflicto para que el fallo de
        # `cdk synth` sea autoexplicativo.
        message = str(excinfo.value)
        assert "10.12" in message
        assert str(minimum - 1) in message
        assert str(minimum) in message

    def test_threshold_well_below_minimum_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _make(stuck_threshold_seconds=100)

    def test_larger_retry_window_raises_the_minimum(self) -> None:
        # Si la ventana de reintentos crece (más recepciones o visibility más
        # largo), un umbral antes válido deja de serlo.
        with pytest.raises(ValueError):
            _make(
                queue_max_receive_count=5,
                queue_visibility_timeout_seconds=300,
                stuck_threshold_seconds=900,  # 5 * 300 + 360 = 1860 > 900
            )

    def test_error_names_the_offending_env(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _make(env_name="prod", stuck_threshold_seconds=10)

        assert "prod" in str(excinfo.value)


class TestFrozenInstanceStillValidates:
    """La invariante se evalúa también al reconstruir con ``replace``."""

    def test_replace_with_invalid_threshold_is_rejected(self) -> None:
        valid = _make()

        with pytest.raises(ValueError):
            dataclasses.replace(valid, stuck_threshold_seconds=0)


class TestRealEnvironmentsSatisfyInvariant:
    """Los tres entornos reales cargan sin violar la invariante (criterio 10.12)."""

    @pytest.mark.parametrize("env_name", VALID_ENVIRONMENTS)
    def test_env_config_loads_and_satisfies_invariant(self, env_name: str) -> None:
        config = load_env_config(env_name)

        minimum = (
            config.queue_max_receive_count * config.queue_visibility_timeout_seconds
            + SWEEPER_MARGIN_SECONDS
        )
        assert config.stuck_threshold_seconds >= minimum
