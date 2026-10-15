"""Pruebas_De_Infraestructura de las Lambdas del backend (tarea 11.4).

Afirman sobre el template del stack-arnés (que instancia
:class:`~constructs.backend_function.BackendFunction`) las garantías transversales
que ese construct debe imponer a las nueve Lambdas del sistema:

- arquitectura ``arm64`` / Graviton (criterios 17.12 / design 14.4.6),
- concurrencia reservada fijada cuando se solicita (criterios 5.7 / 18.10–18.12),
- *log group* explícito con retención de 30 días y cifrado con la CMK
  (criterios 15.6 y la retención de 30 días del diseño 14.3),
- trazado X-Ray activo.

``BackendFunction`` aún no está cableado en ningún stack sintetizable (lo
instancian ``compute`` en 12.2 y ``api`` en 13.2), por lo que estas assertions
usan el stack-arnés de :mod:`tests._harness`, que lo construye sobre la CMK y el
Layer reales del ``StorageStack``.
"""

from __future__ import annotations

from aws_cdk.assertions import Match

from tests._harness import HARNESS_RESERVED_CONCURRENCY, synth_harness_template


class TestBackendFunctionArchitecture:
    """La Lambda se sintetiza sobre ``arm64`` y Python 3.12 (criterio 17.12)."""

    def test_function_runs_on_arm64(self) -> None:
        template = synth_harness_template()

        template.has_resource_properties(
            "AWS::Lambda::Function",
            {"Architectures": ["arm64"]},
        )

    def test_function_uses_python_312_runtime(self) -> None:
        template = synth_harness_template()

        template.has_resource_properties(
            "AWS::Lambda::Function",
            {"Runtime": "python3.12"},
        )


class TestBackendFunctionConcurrency:
    """La concurrencia reservada solicitada llega al template (criterio 5.7)."""

    def test_reserved_concurrency_is_set(self) -> None:
        template = synth_harness_template()

        template.has_resource_properties(
            "AWS::Lambda::Function",
            {"ReservedConcurrentExecutions": HARNESS_RESERVED_CONCURRENCY},
        )


class TestBackendFunctionTracing:
    """El trazado X-Ray queda activo para la depuración distribuida."""

    def test_tracing_is_active(self) -> None:
        template = synth_harness_template()

        template.has_resource_properties(
            "AWS::Lambda::Function",
            {"TracingConfig": {"Mode": "Active"}},
        )


class TestBackendFunctionLogGroup:
    """El *log group* explícito retiene 30 días y se cifra con la CMK (15.6)."""

    def test_log_group_retains_30_days(self) -> None:
        # RetentionDays.ONE_MONTH mapea a 30 en el template de CloudFormation.
        template = synth_harness_template()

        template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"RetentionInDays": 30},
        )

    def test_log_group_is_encrypted_with_a_cmk(self) -> None:
        # El log group explícito trae `KmsKeyId`; el grupo implícito de Lambda no.
        # No se fija el ARN exacto para no acoplar con el id lógico de la CMK.
        template = synth_harness_template()

        template.has_resource_properties(
            "AWS::Logs::LogGroup",
            {"KmsKeyId": Match.any_value()},
        )
