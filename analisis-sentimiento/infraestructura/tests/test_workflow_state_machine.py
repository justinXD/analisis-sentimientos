"""Pruebas_De_Infraestructura de la Maquina_De_Estados (tarea 12.3, diseño 21.4).

Afirman sobre el template de CloudFormation sintetizado del ``WorkflowStack`` que
la máquina de estados padre Standard y su Distributed Map están configurados tal
como exigen los criterios del Requirement 5 (orquestación), 9 (resiliencia) y 10
(terminación garantizada).

Se verifica, entre otros, que:

- La máquina padre es de tipo ``STANDARD`` (Distributed Map solo existe en
  Standard; el Lote puede durar horas) con el timeout global de 10.800 s
  (criterios 5.12 y diseño 17.3).
- El estado ``ProcessItems`` es un ``Map`` DISTRIBUTED con hijos ``EXPRESS``,
  ``ItemReader`` de S3 (JSON), ``ItemBatcher`` de 25, ``MaxConcurrency`` 4,
  ``ToleratedFailurePercentage`` 100 y ``ResultWriter`` a ``map-results``
  (criterios 5.3, 5.4, 5.5, 5.10, 5.13).
- El ``LlmFanOut`` interno limita la concurrencia por Ítem a 5 (criterio 5.6).
- Los ``Retry`` declarativos de ``DeepAnalysis`` (6), ``ClassicSentiment`` (5) y
  ``Aggregate`` (3) usan backoff 2.0, jitter completo y **solo** condiciones
  transitorias (criterios 9.1, 9.2, 9.3, 9.4, 9.11), sin el ``Retry`` que
  ``LambdaInvoke`` inyecta por defecto.
- ``MarkFailed`` publica el Evento_Finalizacion en ``FAILED`` antes de terminar
  (criterios 10.17/10.18/10.20).

Las assertions operan sobre la definición ASL embebida en ``DefinitionString``,
que CDK sintetiza como un ``Fn::Join`` de literales y referencias. Las
referencias (ARN de recursos aún no vinculados) se sustituyen por un marcador
seguro para que la definición sea JSON parseable sin resolver tokens.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aws_cdk import assertions

from tests._harness import synth_workflow_template

# Marcador con el que se sustituye cada referencia (``Ref``/``Fn::GetAtt``...) del
# ``Fn::Join`` de la definición. No contiene comillas ni llaves, de modo que al
# embeberse dentro de un valor de cadena (p. ej. ``arn:<REF>:states:...``) no rompe
# el parseo JSON de la definición resultante.
_REF_PLACEHOLDER = "REF"

# Conjunto exacto de condiciones transitorias que la máquina reintenta (criterio
# 9.16). Ninguna condición permanente del criterio 9.17 debe aparecer en un
# ``Retry`` (criterio 9.4): esa es precisamente la propiedad que se verifica.
_TRANSIENT_ERRORS = {
    "TransientDependencyError",
    "ThrottledError",
    "States.Timeout",
    "Lambda.TooManyRequestsException",
    "Lambda.ServiceException",
    "Lambda.SdkClientException",
}

# Condiciones permanentes del criterio 9.17: nunca deben figurar en un ``Retry``.
_PERMANENT_ERRORS = {
    "LlmInputRejectedError",
    "LlmSchemaError",
    "TaskInputInvalidError",
    "ComprehendItemFailedError",
}


def _flatten_definition(definition_string: dict[str, Any]) -> dict[str, Any]:
    """Aplana el ``DefinitionString`` (un ``Fn::Join``) a la definición ASL.

    Sustituye cada fragmento no literal (una referencia a recurso sin resolver)
    por :data:`_REF_PLACEHOLDER` y concatena el resultado, que es un documento JSON
    parseable con la estructura real de la máquina de estados.

    Args:
        definition_string: El valor de ``DefinitionString`` del recurso
            ``AWS::StepFunctions::StateMachine``.

    Returns:
        La definición ASL como diccionario.
    """

    parts = definition_string["Fn::Join"][1]
    rendered = "".join(
        part if isinstance(part, str) else _REF_PLACEHOLDER for part in parts
    )
    return json.loads(rendered)


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    """El template sintetizado del ``WorkflowStack`` (cacheado a nivel de módulo)."""

    return synth_workflow_template()


@pytest.fixture(scope="module")
def state_machine(template: assertions.Template) -> dict[str, Any]:
    """Las ``Properties`` del único recurso ``StateMachine`` del stack."""

    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    assert len(machines) == 1, "se espera exactamente una Maquina_De_Estados"
    return next(iter(machines.values()))["Properties"]


@pytest.fixture(scope="module")
def definition(state_machine: dict[str, Any]) -> dict[str, Any]:
    """La definición ASL aplanada de la Maquina_De_Estados."""

    return _flatten_definition(state_machine["DefinitionString"])


@pytest.fixture(scope="module")
def process_items(definition: dict[str, Any]) -> dict[str, Any]:
    """El estado ``ProcessItems`` (el Distributed Map)."""

    return definition["States"]["ProcessItems"]


@pytest.fixture(scope="module")
def child_states(process_items: dict[str, Any]) -> dict[str, Any]:
    """Los estados del workflow hijo Express (dentro del Distributed Map)."""

    return process_items["ItemProcessor"]["States"]


class TestParentMachine:
    """El workflow padre es Standard con el timeout y los estados del diseño 17."""

    def test_is_standard_type(self, state_machine: dict[str, Any]) -> None:
        # Standard es obligatorio: Distributed Map solo existe en Standard y el
        # Lote puede durar horas (criterio 5.12, diseño 17.3).
        assert state_machine["StateMachineType"] == "STANDARD"

    def test_execution_timeout_is_three_hours(
        self, definition: dict[str, Any]
    ) -> None:
        # Criterio 5.12: toda ejecución termina en 10.800 s como máximo.
        assert definition["TimeoutSeconds"] == 10800

    def test_starts_at_mark_running(self, definition: dict[str, Any]) -> None:
        assert definition["StartAt"] == "MarkRunning"

    def test_has_the_four_parent_states(self, definition: dict[str, Any]) -> None:
        # Los cuatro estados nombrados del padre (diseño 17.1/17.2).
        states = definition["States"]
        for name in ("MarkRunning", "ProcessItems", "Aggregate", "MarkFailed"):
            assert name in states, f"falta el estado del padre {name!r}"


class TestMarkRunning:
    """``MarkRunning`` transiciona el META de PENDING a RUNNING (criterios 5.2/5.15)."""

    def test_conditional_on_pending(self, definition: dict[str, Any]) -> None:
        # Criterio 5.2/5.15: solo transiciona si el META sigue en PENDING.
        mr = definition["States"]["MarkRunning"]
        assert mr["Parameters"]["ConditionExpression"] == "#st = :pending"

    def test_already_terminal_short_circuits_to_succeed(
        self, definition: dict[str, Any]
    ) -> None:
        # Criterio 5.15: si el META ya es terminal (condición falla), la ejecución
        # termina sin procesar nada (Catch de ConditionalCheckFailed → Succeed).
        mr = definition["States"]["MarkRunning"]
        catch = {
            tuple(c["ErrorEquals"]): c["Next"] for c in mr["Catch"]
        }
        assert catch[("DynamoDB.ConditionalCheckFailedException",)] == "AlreadyTerminal"
        assert definition["States"]["AlreadyTerminal"]["Type"] == "Succeed"

    def test_other_failures_go_to_mark_failed(
        self, definition: dict[str, Any]
    ) -> None:
        mr = definition["States"]["MarkRunning"]
        catch = {tuple(c["ErrorEquals"]): c["Next"] for c in mr["Catch"]}
        assert catch[("States.ALL",)] == "MarkFailed"


class TestDistributedMap:
    """``ProcessItems`` es el Distributed Map con la configuración del diseño 17.2."""

    def test_is_distributed_map_with_express_children(
        self, process_items: dict[str, Any]
    ) -> None:
        assert process_items["Type"] == "Map"
        config = process_items["ItemProcessor"]["ProcessorConfig"]
        # DISTRIBUTED es lo que habilita ItemReader/ItemBatcher/ResultWriter; los
        # hijos EXPRESS son baratos y rápidos (diseño 17.3).
        assert config["Mode"] == "DISTRIBUTED"
        assert config["ExecutionType"] == "EXPRESS"

    def test_item_reader_reads_json_from_s3(
        self, process_items: dict[str, Any]
    ) -> None:
        # Criterio 5.3: lee el array de Ítems de inputs/ con ItemReader, hasta 1000.
        reader = process_items["ItemReader"]
        assert reader["Resource"].endswith("s3:getObject")
        assert reader["ReaderConfig"]["InputType"] == "JSON"
        assert reader["ReaderConfig"]["MaxItems"] == 1000
        # La clave es dinámica por ejecución (el Pipe de 12.4 la inyecta).
        assert reader["Parameters"]["Key.$"] == "$.input_s3_key"

    def test_item_batcher_groups_in_25(self, process_items: dict[str, Any]) -> None:
        # Criterio 5.4: Batches de 25 Ítems como máximo (= límite de Comprehend).
        assert process_items["ItemBatcher"]["MaxItemsPerBatch"] == 25

    def test_max_concurrency_is_four(self, process_items: dict[str, Any]) -> None:
        # Criterio 5.5: 4 ejecuciones hijas en vuelo como máximo (dial de Bedrock).
        assert process_items["MaxConcurrency"] == 4

    def test_tolerates_full_failure(self, process_items: dict[str, Any]) -> None:
        # Criterio 5.10: un Batch fallido no detiene los demás; el Lote siempre
        # llega a Aggregate.
        assert process_items["ToleratedFailurePercentage"] == 100

    def test_result_writer_targets_map_results(
        self, process_items: dict[str, Any]
    ) -> None:
        # Criterio 5.13: el resultado del Map se escribe en map-results/.
        writer = process_items["ResultWriter"]
        assert writer["Resource"].endswith("s3:putObject")
        assert writer["Parameters"]["Prefix"] == "map-results"

    def test_input_read_failure_goes_to_mark_failed(
        self, process_items: dict[str, Any]
    ) -> None:
        # Criterio 5.16: fallo de lectura de inputs/ → FAILED(INPUT_READ_FAILED),
        # sin iniciar ningún hijo.
        catch = {tuple(c["ErrorEquals"]): c["Next"] for c in process_items["Catch"]}
        assert catch[("States.ItemReaderFailed",)] == "MarkFailed"

    def test_other_map_failures_still_aggregate(
        self, process_items: dict[str, Any]
    ) -> None:
        # Criterio 5.9: cualquier otro fallo del Map lleva igualmente a Aggregate
        # (el Lote SIEMPRE se agrega para dar un Estado_Terminal honesto).
        catch = {tuple(c["ErrorEquals"]): c["Next"] for c in process_items["Catch"]}
        assert catch[("States.ALL",)] == "Aggregate"


class TestChildWorkflow:
    """El workflow hijo Express: ClassicSentiment → LlmFanOut → DeepAnalysis."""

    def test_starts_at_classic_sentiment(
        self, process_items: dict[str, Any]
    ) -> None:
        assert process_items["ItemProcessor"]["StartAt"] == "ClassicSentiment"

    def test_llm_fan_out_concurrency_is_five(
        self, child_states: dict[str, Any]
    ) -> None:
        # Criterio 5.6: hasta 5 Ítems de un Batch en vuelo dentro de cada hijo.
        fan_out = child_states["LlmFanOut"]
        assert fan_out["Type"] == "Map"
        assert fan_out["MaxConcurrency"] == 5

    def test_deep_analysis_item_failure_is_absorbed(
        self, child_states: dict[str, Any]
    ) -> None:
        # Criterio 9.6: el fallo de un Ítem no detiene el resto del Lote; el Catch
        # de DeepAnalysis desvía a RecordItemFailure.
        deep = child_states["LlmFanOut"]["ItemProcessor"]["States"]["DeepAnalysis"]
        catch = {tuple(c["ErrorEquals"]): c["Next"] for c in deep["Catch"]}
        assert catch[("States.ALL",)] == "RecordItemFailure"

    def test_classic_sentiment_batch_failure_is_absorbed(
        self, child_states: dict[str, Any]
    ) -> None:
        # Criterio 6.15: solo el fallo de la llamada completa a Comprehend desvía
        # a RecordBatchFailure; el resto del Lote continúa (criterio 9.6).
        catch = {
            tuple(c["ErrorEquals"]): c["Next"]
            for c in child_states["ClassicSentiment"]["Catch"]
        }
        assert catch[("States.ALL",)] == "RecordBatchFailure"


def _deep_analysis_retry(child_states: dict[str, Any]) -> list[dict[str, Any]]:
    return child_states["LlmFanOut"]["ItemProcessor"]["States"]["DeepAnalysis"][
        "Retry"
    ]


class TestRetryPolicies:
    """Los ``Retry`` son declarativos, con backoff 2.0, jitter FULL y solo transitorios."""

    def test_deep_analysis_retry(self, child_states: dict[str, Any]) -> None:
        # Criterio 9.2: Analyze_Llm reintenta 6 veces, base 2 s, backoff 2.0,
        # jitter completo, techo 60 s.
        retries = _deep_analysis_retry(child_states)
        # No debe haber un segundo bloque Retry (el por defecto de LambdaInvoke).
        assert len(retries) == 1
        retry = retries[0]
        assert retry["MaxAttempts"] == 6
        assert retry["IntervalSeconds"] == 2
        assert retry["BackoffRate"] == 2
        assert retry["MaxDelaySeconds"] == 60
        assert retry["JitterStrategy"] == "FULL"

    def test_classic_sentiment_retry(self, child_states: dict[str, Any]) -> None:
        # Criterio 9.3: Analyze_Comprehend reintenta 5 veces, base 2 s, backoff
        # 2.0, jitter completo, techo 30 s.
        retries = child_states["ClassicSentiment"]["Retry"]
        assert len(retries) == 1
        retry = retries[0]
        assert retry["MaxAttempts"] == 5
        assert retry["IntervalSeconds"] == 2
        assert retry["BackoffRate"] == 2
        assert retry["MaxDelaySeconds"] == 30
        assert retry["JitterStrategy"] == "FULL"

    def test_aggregate_retry(self, definition: dict[str, Any]) -> None:
        # Criterio 9.11: Aggregate reintenta 3 veces, base 5 s, backoff 2.0,
        # jitter completo, techo 30 s.
        retries = definition["States"]["Aggregate"]["Retry"]
        assert len(retries) == 1
        retry = retries[0]
        assert retry["MaxAttempts"] == 3
        assert retry["IntervalSeconds"] == 5
        assert retry["BackoffRate"] == 2
        assert retry["MaxDelaySeconds"] == 30
        assert retry["JitterStrategy"] == "FULL"

    @pytest.mark.parametrize(
        "retry",
        [
            pytest.param("deep", id="DeepAnalysis"),
            pytest.param("classic", id="ClassicSentiment"),
            pytest.param("aggregate", id="Aggregate"),
        ],
    )
    def test_retry_covers_only_transient_conditions(
        self,
        retry: str,
        child_states: dict[str, Any],
        definition: dict[str, Any],
    ) -> None:
        # Criterio 9.4: las condiciones permanentes NO se reintentan; solo las
        # transitorias del criterio 9.16 figuran en ErrorEquals.
        if retry == "deep":
            errors = set(_deep_analysis_retry(child_states)[0]["ErrorEquals"])
        elif retry == "classic":
            errors = set(child_states["ClassicSentiment"]["Retry"][0]["ErrorEquals"])
        else:
            errors = set(definition["States"]["Aggregate"]["Retry"][0]["ErrorEquals"])

        assert errors == _TRANSIENT_ERRORS
        assert not (errors & _PERMANENT_ERRORS), (
            "una condición permanente (criterio 9.17) no debe reintentarse "
            "(criterio 9.4)"
        )


class TestTerminalFailure:
    """``MarkFailed`` cierra en FAILED y publica el Evento_Finalizacion (10.17/10.18)."""

    def test_aggregate_catch_goes_to_mark_failed(
        self, definition: dict[str, Any]
    ) -> None:
        # Criterio 9.11: agotados los reintentos de Aggregate → MarkFailed.
        catch = {
            tuple(c["ErrorEquals"]): c["Next"]
            for c in definition["States"]["Aggregate"]["Catch"]
        }
        assert catch[("States.ALL",)] == "MarkFailed"

    def test_mark_failed_sets_failed_status(
        self, definition: dict[str, Any]
    ) -> None:
        mf = definition["States"]["MarkFailed"]
        values = mf["Parameters"]["ExpressionAttributeValues"]
        assert values[":failed"]["S"] == "FAILED"

    def test_mark_failed_publishes_event_before_terminating(
        self, definition: dict[str, Any]
    ) -> None:
        # Criterios 10.18/10.20: publica el Evento_Finalizacion (detail-type
        # analysis.completed, event_id determinista {analysis_id}:FAILED) antes
        # de terminar la ejecución en Fail.
        states = definition["States"]
        assert states["MarkFailed"]["Next"] == "PublishFailure"
        publish = states["PublishFailure"]
        entry = publish["Parameters"]["Entries"][0]
        assert entry["DetailType"] == "analysis.completed"
        assert entry["Detail"]["status"] == "FAILED"
        assert (
            entry["Detail"]["event_id.$"]
            == "States.Format('{}:{}', $.analysis_id, 'FAILED')"
        )
        # Tras publicar, la ejecución termina en un estado Fail (criterio 10.18).
        assert publish["Next"] == "AnalysisFailed"
        assert states["AnalysisFailed"]["Type"] == "Fail"


class TestStateMachineObservabilityAndSecurity:
    """La máquina cifra sus logs con la CMK y emite trazas (criterios 15.6/14.5)."""

    def test_logging_enabled_at_all_level(
        self, state_machine: dict[str, Any]
    ) -> None:
        logging = state_machine["LoggingConfiguration"]
        assert logging["Level"] == "ALL"
        # No se incluyen datos de ejecución: defensa en profundidad anti-PII
        # (criterios 5.11/15.3).
        assert logging["IncludeExecutionData"] is False

    def test_tracing_enabled(self, state_machine: dict[str, Any]) -> None:
        assert state_machine["TracingConfiguration"]["Enabled"] is True

    def test_log_group_is_cmk_encrypted(
        self, template: assertions.Template
    ) -> None:
        # Criterio 15.6: los log groups cifran en reposo con la CMK del proyecto.
        log_groups = template.find_resources("AWS::Logs::LogGroup")
        assert log_groups, "la Maquina_De_Estados debe tener un log group propio"
        assert any(
            "KmsKeyId" in lg.get("Properties", {}) for lg in log_groups.values()
        ), "el log group de la Maquina_De_Estados debe estar cifrado con la CMK"
