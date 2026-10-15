"""Pruebas_De_Infraestructura del Pipe y el sweeper del ``WorkflowStack`` (tarea 12.4).

Afirman sobre el template de CloudFormation sintetizado del ``WorkflowStack`` que
los dos recursos añadidos por la tarea 12.4 cumplen los criterios del Requirement
5 (orquestación / idempotencia de arranque) y 10 (terminación garantizada):

- **EventBridge Pipe** (``AWS::Pipes::Pipe``): su *source* es la Cola_Solicitudes,
  su *target* es la Maquina_De_Estados, agrupa de uno en uno (1 mensaje = 1 Lote =
  1 ``StartExecution``) y su ``InputTemplate`` fija el **nombre de ejecución** al
  ``analysis_id`` del mensaje (idempotencia de nivel 1, criterios 5.1/5.14/8.1),
  propagando solo identificadores y punteros, nunca ``respuesta`` (criterio 5.11).
- **Reconcile_Stuck** (``AWS::Lambda::Function``): la Lambda del sweeper con su
  handler real, y la regla de **EventBridge Scheduler** (``AWS::Scheduler::Schedule``)
  que la dispara cada 15 minutos (criterio 10.10).
- **Mínimo privilegio** (criterios 15.10/15.11): el rol del Pipe solo recibe el
  consumo de la cola concreta y ``states:StartExecution`` sobre la máquina
  concreta; el rol del sweeper, inspección de ejecuciones acotada a
  ``execution:{sm}:*`` y ``events:PutEvents`` sobre el bus concreto; ninguna acción
  de datos sobre ``Resource: "*"`` (esto último también lo cubre
  ``test_iam_policies.py`` de forma transversal).

El andamiaje de síntesis (cadena de dependencias real, assets sin empaquetar)
vive en :func:`tests._harness.synth_workflow_template`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aws_cdk import assertions

from tests._harness import synth_workflow_template

# Handler real del sweeper, empaquetado en ``backend/src`` (tarea 9.3). El módulo
# expone ``lambda_handler`` como punto de entrada.
_RECONCILE_HANDLER = "sentiment.handlers.reconcile_stuck.lambda_handler"

# Expresión de cadencia del barrido (criterio 10.10).
_EXPECTED_SCHEDULE = "rate(15 minutes)"


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    """El template sintetizado del ``WorkflowStack`` (cacheado a nivel de módulo)."""

    return synth_workflow_template()


# --- EventBridge Pipe ---------------------------------------------------------


@pytest.fixture(scope="module")
def pipe(template: assertions.Template) -> dict[str, Any]:
    """Las ``Properties`` del único recurso ``Pipe`` del stack."""

    pipes = template.find_resources("AWS::Pipes::Pipe")
    assert len(pipes) == 1, "se espera exactamente un EventBridge Pipe"
    return next(iter(pipes.values()))["Properties"]


class TestRequestPipe:
    """El Pipe conecta la Cola_Solicitudes con ``StartExecution`` (criterios 5.1/5.14/8.1)."""

    def test_source_is_the_requests_queue(self, pipe: dict[str, Any]) -> None:
        # El source debe apuntar al ARN de una cola SQS (la Cola_Solicitudes). CDK
        # lo renderiza como un `Fn::GetAtt` al recurso de la cola; basta con que
        # sea una referencia (no un literal) y que exista `SourceParameters` de SQS.
        assert "Source" in pipe
        assert "SqsQueueParameters" in pipe["SourceParameters"]

    def test_source_batch_size_is_one(self, pipe: dict[str, Any]) -> None:
        # Criterio 5.1: 1 mensaje = 1 Lote = 1 StartExecution. Agrupar rompería la
        # correspondencia y la idempotencia por nombre de ejecución.
        assert pipe["SourceParameters"]["SqsQueueParameters"]["BatchSize"] == 1

    def test_target_is_the_state_machine(self, pipe: dict[str, Any]) -> None:
        # El target es la Maquina_De_Estados y usa los parámetros de Step Functions.
        assert "Target" in pipe
        sfn_params = pipe["TargetParameters"]["StepFunctionStateMachineParameters"]
        # Standard no admite arranque síncrono desde Pipes (diseño 10.4).
        assert sfn_params["InvocationType"] == "FIRE_AND_FORGET"

    def test_execution_name_is_the_analysis_id(self, pipe: dict[str, Any]) -> None:
        # Núcleo de la tarea (criterios 5.1/5.14/8.1): el nombre de ejecución es el
        # analysis_id del mensaje, de modo que la 2ª entrega falle con
        # ExecutionAlreadyExists. Se expresa con la clave reservada `Name` del
        # InputTemplate, resuelta desde el cuerpo del mensaje.
        template_body = json.loads(pipe["TargetParameters"]["InputTemplate"])
        assert template_body["Name"] == "<$.body.analysis_id>"

    def test_input_carries_only_identifiers_and_pointers(
        self, pipe: dict[str, Any]
    ) -> None:
        # Criterio 5.11: la máquina propaga identificadores y claves de objeto,
        # nunca el texto de `respuesta`/`explicacion`.
        template_body = json.loads(pipe["TargetParameters"]["InputTemplate"])
        sfn_input = template_body["Input"]
        assert sfn_input == {
            "analysis_id": "<$.body.analysis_id>",
            "tenant_id": "<$.body.tenant_id>",
            "correlation_id": "<$.body.correlation_id>",
            "input_s3_key": "<$.body.input_s3_key>",
        }
        # Defensa explícita: ninguna clave del input transporta PII.
        assert "respuesta" not in sfn_input
        assert "explicacion" not in sfn_input

    def test_pipe_has_no_dead_letter_queue(self, pipe: dict[str, Any]) -> None:
        # Criterio 5.14: el descarte de duplicados (ExecutionAlreadyExists) es un
        # ÉXITO para Pipes, no un fallo; el Pipe NO define DLQ propia. CDK solo
        # emitiría `DeadLetterConfig` si se hubiera configurado una.
        source_sqs = pipe["SourceParameters"]["SqsQueueParameters"]
        assert "DeadLetterConfig" not in source_sqs


class TestPipeLeastPrivilege:
    """El rol del Pipe recibe solo consumo de la cola y StartExecution (15.10/15.11)."""

    def test_pipe_role_can_start_only_this_state_machine(
        self, template: assertions.Template
    ) -> None:
        # states:StartExecution debe concederse sobre un ARN concreto (una
        # referencia a la máquina), nunca sobre "*".
        statements = _all_statements(template)
        start = [
            st
            for st in statements
            if "states:StartExecution" in _as_list(st.get("Action"))
        ]
        assert start, "falta la concesión states:StartExecution para el Pipe"
        for st in start:
            assert "*" not in _as_list(st.get("Resource")), (
                "states:StartExecution no debe concederse sobre Resource: '*'"
            )

    def test_pipe_role_consumes_the_queue(
        self, template: assertions.Template
    ) -> None:
        # Pipes necesita Receive/Delete/GetQueueAttributes para sondear y borrar el
        # mensaje tras el arranque (criterio 5.14). grant_consume_messages las
        # concede sobre el ARN concreto de la cola.
        statements = _all_statements(template)
        actions = {
            action
            for st in statements
            for action in _as_list(st.get("Action"))
            if isinstance(action, str) and action.startswith("sqs:")
        }
        assert "sqs:ReceiveMessage" in actions
        assert "sqs:DeleteMessage" in actions
        assert "sqs:GetQueueAttributes" in actions


# --- Reconcile_Stuck (sweeper) ------------------------------------------------


@pytest.fixture(scope="module")
def reconcile_function(template: assertions.Template) -> dict[str, Any]:
    """Las ``Properties`` de la Lambda ``reconcile_stuck`` (por su handler)."""

    functions = template.find_resources("AWS::Lambda::Function")
    matching = [
        props["Properties"]
        for props in functions.values()
        if props["Properties"].get("Handler") == _RECONCILE_HANDLER
    ]
    assert len(matching) == 1, (
        "se espera exactamente una Lambda reconcile_stuck con el handler "
        f"{_RECONCILE_HANDLER!r}"
    )
    return matching[0]


class TestReconcileStuckFunction:
    """La Lambda del sweeper existe con su handler y su configuración (tarea 9.3/12.4)."""

    def test_handler_points_to_reconcile_stuck(
        self, reconcile_function: dict[str, Any]
    ) -> None:
        assert reconcile_function["Handler"] == _RECONCILE_HANDLER

    def test_is_arm64(self, reconcile_function: dict[str, Any]) -> None:
        # Secure default del BackendFunction (criterio 17.12): arm64 / Graviton.
        assert reconcile_function["Architectures"] == ["arm64"]

    def test_knows_the_state_machine_name(
        self, reconcile_function: dict[str, Any]
    ) -> None:
        # El sweeper necesita el nombre de la máquina para construir el ARN de las
        # ejecuciones que inspecciona (su nombre es el analysis_id, diseño 10.6).
        env = reconcile_function["Environment"]["Variables"]
        assert "STATE_MACHINE_NAME" in env


class TestReconcileStuckLeastPrivilege:
    """El rol del sweeper: inspección de ejecuciones y PutEvents, sin comodín (15.11)."""

    def test_describe_executions_scoped_to_this_machine(
        self, template: assertions.Template
    ) -> None:
        # La concesión propia del sweeper (SID DescribeSentimentExecutions) acota
        # DescribeExecution/ListExecutions al ARN de ejecución execution:{sm}:*,
        # nunca sobre "*" (criterio 15.11, diseño 10.6). Se identifica por su SID
        # para no confundirla con otras concesiones de states:* del stack (p. ej.
        # las que CDK añade sobre el ARN de la propia máquina).
        statements = _all_statements(template)
        sweeper_grant = [
            st
            for st in statements
            if st.get("Sid") == "DescribeSentimentExecutions"
        ]
        assert len(sweeper_grant) == 1, (
            "falta la concesión DescribeSentimentExecutions del sweeper"
        )
        st = sweeper_grant[0]
        # Acciones de inspección exactas (diseño 10.6).
        assert set(_as_list(st["Action"])) == {
            "states:DescribeExecution",
            "states:ListExecutions",
        }
        resources = _as_list(st.get("Resource"))
        assert "*" not in resources, (
            "states:DescribeExecution no debe concederse sobre Resource: '*'"
        )
        # El ARN de ejecución se renderiza como un Fn::Join con el sufijo ':*'.
        assert any(_arn_targets_execution(r) for r in resources), (
            "el permiso debe acotarse al ARN de ejecución execution:{sm}:*"
        )

    def test_no_states_grant_uses_wildcard_resource(
        self, template: assertions.Template
    ) -> None:
        # Red de seguridad transversal: ninguna concesión de ``states:*`` del stack
        # (ni la del sweeper ni las que CDK añade) usa Resource: "*".
        statements = _all_statements(template)
        for st in statements:
            actions = _as_list(st.get("Action"))
            if any(
                isinstance(a, str) and a.startswith("states:") for a in actions
            ):
                assert "*" not in _as_list(st.get("Resource")), (
                    f"una concesión states:* usa Resource '*': {actions}"
                )

    def test_can_publish_finalization_events(
        self, template: assertions.Template
    ) -> None:
        # events:PutEvents sobre el bus concreto para republicar el
        # Evento_Finalizacion (criterios 10.14/10.19).
        statements = _all_statements(template)
        put_events = [
            st
            for st in statements
            if "events:PutEvents" in _as_list(st.get("Action"))
        ]
        assert put_events, "falta events:PutEvents para el sweeper"
        for st in put_events:
            assert "*" not in _as_list(st.get("Resource"))


# --- EventBridge Scheduler ----------------------------------------------------


@pytest.fixture(scope="module")
def schedule(template: assertions.Template) -> dict[str, Any]:
    """Las ``Properties`` del único recurso ``Scheduler::Schedule`` del stack."""

    schedules = template.find_resources("AWS::Scheduler::Schedule")
    assert len(schedules) == 1, "se espera exactamente una regla de Scheduler"
    return next(iter(schedules.values()))["Properties"]


class TestReconcileSchedule:
    """La regla de EventBridge Scheduler dispara el sweeper cada 15 min (criterio 10.10)."""

    def test_runs_every_fifteen_minutes(self, schedule: dict[str, Any]) -> None:
        assert schedule["ScheduleExpression"] == _EXPECTED_SCHEDULE

    def test_flexible_window_is_off(self, schedule: dict[str, Any]) -> None:
        # OFF: el intervalo máximo entre barridos no supera los 15 min exactos que
        # exige el criterio 10.10 (sin adelanto/retraso dentro de una ventana).
        assert schedule["FlexibleTimeWindow"]["Mode"] == "OFF"

    def test_target_is_the_reconcile_lambda(self, schedule: dict[str, Any]) -> None:
        # El target apunta al ARN de una función Lambda y lleva un rol de invocación.
        target = schedule["Target"]
        assert "Arn" in target
        assert "RoleArn" in target


# --- Helpers ------------------------------------------------------------------


def _as_list(value: object) -> list[object]:
    """Normaliza un campo de política (escalar o lista) a lista."""

    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _all_statements(template: assertions.Template) -> list[dict[str, Any]]:
    """Recolecta todas las declaraciones IAM de policies y roles del template."""

    statements: list[dict[str, Any]] = []
    for resource_type in ("AWS::IAM::Policy", "AWS::IAM::Role"):
        for resource in template.find_resources(resource_type).values():
            properties = resource.get("Properties", {})
            if resource_type == "AWS::IAM::Role":
                for policy in _as_list(properties.get("Policies")):
                    if isinstance(policy, dict):
                        statements.extend(
                            _document_statements(policy.get("PolicyDocument"))
                        )
            else:
                statements.extend(
                    _document_statements(properties.get("PolicyDocument"))
                )
    return statements


def _document_statements(document: object) -> list[dict[str, Any]]:
    """Extrae las declaraciones de un ``PolicyDocument`` (vacío si no es dict)."""

    if not isinstance(document, dict):
        return []
    return [st for st in _as_list(document.get("Statement")) if isinstance(st, dict)]


def _arn_targets_execution(resource: object) -> bool:
    """Indica si un ``Resource`` apunta al ARN de ejecución ``execution:{sm}:*``.

    El ARN se renderiza como un ``Fn::Join`` cuyos fragmentos literales incluyen
    ``:execution:`` y terminan en ``:*``. Se inspeccionan los literales de cadena
    del join (las referencias a recursos no se resuelven en el template).
    """

    if isinstance(resource, dict) and "Fn::Join" in resource:
        parts = resource["Fn::Join"][1]
        literals = [p for p in parts if isinstance(p, str)]
        has_execution = any(":execution:" in p for p in literals)
        # El sufijo ``:*`` es el último fragmento literal del join (tras el
        # ``Fn::GetAtt`` al nombre de la máquina): acota a cualquier analysis_id.
        has_wildcard_suffix = any(p == ":*" or p.endswith(":*") for p in literals)
        return has_execution and has_wildcard_suffix
    if isinstance(resource, str):
        return ":execution:" in resource and resource.endswith(":*")
    return False
