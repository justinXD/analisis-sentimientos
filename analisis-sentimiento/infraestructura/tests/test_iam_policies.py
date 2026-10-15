"""Pruebas_De_Infraestructura de las políticas IAM (criterio 17.8, tarea 11.4).

Afirman sobre el template sintetizado que **ninguna** declaración IAM concede una
acción sobre **datos** con el recurso comodín ``Resource: "*"`` (criterios 17.8 /
15.11). Es la red de seguridad del mínimo privilegio: aunque un ``grant_*`` o una
política escrita a mano se equivocara, el test rompe el pipeline antes de desplegar
un permiso de datos sin acotar.

Alcance de "acción de datos"
----------------------------
Se consideran acciones de datos las de los servicios que manipulan datos de
negocio o PII: DynamoDB, S3, SQS, Bedrock, Comprehend, SSM y Secrets Manager
(:data:`DATA_ACTION_PREFIXES`). Deliberadamente **se excluye** X-Ray: sus acciones
(``xray:PutTraceSegments``…) no admiten ARN de recurso —la API no tiene recursos
direccionables—, por lo que su ``Resource: "*"`` es inevitable y no representa un
riesgo sobre datos (así lo documenta ``BackendFunction``). El test afirma, por
tanto, sobre las acciones de datos **específicamente**, no sobre todo ``"*"``.

Se recorren los templates de los stacks sintetizables en esta fase (``storage`` y
``data``) y el stack-arnés que instancia ``BackendFunction`` + ``TenantDataRole``,
cubriendo tanto las políticas de recurso como las políticas de rol/identidad.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from aws_cdk import assertions

from tests._harness import (
    synth_api_template,
    synth_data_template,
    synth_harness_template,
    synth_notifications_template,
    synth_observability_template,
    synth_storage_template,
    synth_workflow_template,
)

# Prefijos de acción que operan sobre datos de negocio o PII. Si una declaración
# incluye alguna de estas acciones, su `Resource` debe estar acotado a ARN
# concretos (criterios 17.8 / 15.11). X-Ray se excluye a propósito (ver docstring).
DATA_ACTION_PREFIXES: tuple[str, ...] = (
    "dynamodb:",
    "s3:",
    "sqs:",
    "bedrock:",
    "comprehend:",
    "ssm:",
    "secretsmanager:",
)

# Tipos de recurso del template que embeben declaraciones IAM a inspeccionar.
# Se cubren tanto las políticas gestionadas/inline independientes como las
# políticas inline embebidas en roles.
_POLICY_RESOURCE_TYPES: tuple[str, ...] = (
    "AWS::IAM::Policy",
    "AWS::IAM::ManagedPolicy",
    "AWS::IAM::Role",
)


def _as_list(value: object) -> list[object]:
    """Normaliza un campo de política que puede ser escalar o lista a lista.

    ``Action`` y ``Resource`` de una declaración IAM pueden venir como una cadena
    única o como una lista; esta función unifica ambos casos.

    Args:
        value: El valor del campo (``str``, ``list`` o ``None``).

    Returns:
        El valor como lista (vacía si era ``None``).
    """

    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _iter_policy_statements(
    template: assertions.Template,
) -> Iterator[tuple[str, dict[str, object]]]:
    """Itera las declaraciones IAM de todos los documentos de política del template.

    Recorre las políticas independientes (``AWS::IAM::Policy`` /
    ``AWS::IAM::ManagedPolicy``) y las políticas inline embebidas en los roles
    (``AWS::IAM::Role`` → ``Policies[].PolicyDocument``).

    Args:
        template: El template de CloudFormation sintetizado.

    Yields:
        Pares ``(resource_type, statement)`` por cada declaración encontrada.
    """

    for resource_type in _POLICY_RESOURCE_TYPES:
        for resource in template.find_resources(resource_type).values():
            properties = resource.get("Properties", {})
            if not isinstance(properties, dict):
                continue

            if resource_type == "AWS::IAM::Role":
                # Las políticas inline del rol viven bajo `Policies[].PolicyDocument`.
                for policy in _as_list(properties.get("Policies")):
                    if isinstance(policy, dict):
                        document = policy.get("PolicyDocument", {})
                        yield from _iter_document_statements(resource_type, document)
            else:
                document = properties.get("PolicyDocument", {})
                yield from _iter_document_statements(resource_type, document)


def _iter_document_statements(
    resource_type: str, document: object
) -> Iterator[tuple[str, dict[str, object]]]:
    """Itera las declaraciones de un ``PolicyDocument``.

    Args:
        resource_type: Tipo de recurso del que proviene el documento (para el
            mensaje de error).
        document: El ``PolicyDocument`` (puede no ser un dict si está sin resolver).

    Yields:
        Pares ``(resource_type, statement)`` por cada declaración del documento.
    """

    if not isinstance(document, dict):
        return
    for statement in _as_list(document.get("Statement")):
        if isinstance(statement, dict):
            yield resource_type, statement


def _statement_has_data_action(statement: dict[str, object]) -> bool:
    """Indica si una declaración incluye alguna acción de datos.

    Args:
        statement: La declaración IAM.

    Returns:
        ``True`` si alguna de sus acciones empieza por un prefijo de
        :data:`DATA_ACTION_PREFIXES`.
    """

    for action in _as_list(statement.get("Action")):
        if isinstance(action, str) and action.startswith(DATA_ACTION_PREFIXES):
            return True
    return False


def _resource_is_wildcard(statement: dict[str, object]) -> bool:
    """Indica si el ``Resource`` de una declaración es el comodín ``"*"``.

    Solo detecta el comodín total; un ARN con comodines internos acotados
    (p. ej. ``.../table/x*``) no cuenta como ``Resource: "*"``.

    Args:
        statement: La declaración IAM.

    Returns:
        ``True`` si algún elemento de ``Resource`` es exactamente ``"*"``.
    """

    return any(resource == "*" for resource in _as_list(statement.get("Resource")))


# Todos los templates sintetizables en esta fase que contienen políticas IAM.
_TEMPLATE_FACTORIES = {
    "storage": synth_storage_template,
    "data": synth_data_template,
    "harness": synth_harness_template,
    "notifications": synth_notifications_template,
    "workflow": synth_workflow_template,
    "observability": synth_observability_template,
    "api": synth_api_template,
}


class TestNoWildcardResourceOnDataActions:
    """Ninguna acción de datos se concede sobre ``Resource: "*"`` (criterio 17.8)."""

    @pytest.mark.parametrize("template_name", sorted(_TEMPLATE_FACTORIES))
    def test_no_data_action_uses_wildcard_resource(self, template_name: str) -> None:
        template = _TEMPLATE_FACTORIES[template_name]()

        offenders: list[str] = []
        for resource_type, statement in _iter_policy_statements(template):
            if _statement_has_data_action(statement) and _resource_is_wildcard(
                statement
            ):
                actions = _as_list(statement.get("Action"))
                sid = statement.get("Sid", "<sin Sid>")
                offenders.append(
                    f"[{template_name}/{resource_type}] Sid={sid!r} "
                    f"acciones={actions} usan Resource: '*'"
                )

        assert not offenders, (
            "Hay acciones de datos concedidas sobre Resource: '*' "
            "(criterio 17.8):\n" + "\n".join(offenders)
        )

    def test_harness_has_a_scoped_data_statement(self) -> None:
        # Guard-rail del propio test: el arnés debe contener al menos una
        # declaración de datos (la política de ejemplo del TenantDataRole), para
        # que `test_no_data_action_uses_wildcard_resource` no esté comprobando el
        # vacío sobre el arnés.
        template = synth_harness_template()

        data_statements = [
            statement
            for _, statement in _iter_policy_statements(template)
            if _statement_has_data_action(statement)
        ]

        assert data_statements, (
            "el stack-arnés no contiene ninguna declaración de datos; la "
            "verificación de ausencia de comodín no estaría ejercitándose"
        )
