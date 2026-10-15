"""Pruebas_De_Infraestructura del dominio de identidad (tarea 13.1).

Afirman sobre el template de CloudFormation sintetizado del ``AuthStack`` las
garantías de autenticación/autorización M2M del diseño y de los criterios 1.6-1.8,
1.9, 1.13, 15.11 y 17.20:

- el User Pool declara el atributo personalizado inmutable ``custom:tenant_id``
  (criterios 1.9 / 2.1);
- el Resource Server ``sentiment`` expone **exactamente** los tres *scopes*
  ``analyses.write`` / ``analyses.read`` / ``analyses.purge`` (criterios 1.6/1.7/19.1);
- hay dos app clients ``client_credentials`` con *client secret*, con una vigencia
  de Access_Token de 3.600 s (criterio 1.8), y el cliente de Purga lleva **solo**
  el *scope* ``analyses.purge`` (mínimo privilegio, criterio 15.11);
- cada *client secret* se almacena en Secrets Manager con un **valor generado**
  (no embebido, criterio 17.20) y con rotación automática a **90 días**
  (criterio 1.13);
- la Lambda de rotación no concede acciones de datos sobre ``Resource: "*"``
  (criterios 15.11 / 17.8).

Las assertions usan :class:`aws_cdk.assertions.Template` (``has_resource_properties``
+ ``Match``) para no acoplarse a los identificadores lógicos que CDK asigne. El
``AuthStack`` se sintetiza aquí de forma autónoma (no depende de ningún otro stack,
diseño 14.4.2), con los datos sintéticos de :data:`tests._harness.TEST_ENV_CONFIG`.
"""

from __future__ import annotations

import json
from functools import lru_cache

import aws_cdk as cdk
import pytest
from aws_cdk import assertions
from aws_cdk.assertions import Match

from config.environments import EnvConfig
from stacks.auth_stack import AuthStack

# Configuración de prueba con datos exclusivamente sintéticos (criterio 17.10).
# El ``AuthStack`` solo usa ``project`` y ``env_name`` para derivar el prefijo de
# los recursos; el resto de campos satisface la invariante del sweeper (criterio
# 10.12) para que ``EnvConfig`` se construya sin lanzar. Se define aquí, y no se
# importa del arnés compartido, para que estas pruebas no dependan de stacks de
# tareas aún en curso que el arnés pueda llegar a importar.
_AUTH_TEST_CONFIG = EnvConfig(
    project="sentiment",
    env_name="test",
    log_retention_days=30,
    cors_origins=("http://localhost:5173",),
    bedrock_model_id="<MODEL_ID_A_CALIBRAR>",
    ses_sender_email="no-reply-test@sentiment.example.com",
    distributed_map_max_concurrency=4,
    llm_fanout_max_concurrency=5,
    analyze_llm_reserved_concurrency=40,
    queue_visibility_timeout_seconds=180,
    queue_max_receive_count=3,
    stuck_threshold_seconds=900,
    data_ttl_days=90,
    waf_mode="COUNT",
)

# Vigencia máxima del Access_Token en el template de Cognito, en minutos
# (criterio 1.8: 3.600 s = 60 min). Cognito expresa `AccessTokenValidity` en la
# unidad declarada por `TokenValidityUnits`.
_ACCESS_TOKEN_VALIDITY_MINUTES = 60

# Sufijos de scope tal como aparecen en el template. CDK emite cada
# `AllowedOAuthScopes` como un `Fn::Join` que concatena el `Ref` al Resource
# Server con `"/analyses.<accion>"`, por lo que las assertions comparan el sufijo
# (la parte literal) en lugar del nombre completo resuelto.
_PURGE_SCOPE_SUFFIX = "/analyses.purge"
_WRITE_SCOPE_SUFFIX = "/analyses.write"
_READ_SCOPE_SUFFIX = "/analyses.read"


def _scope_suffix(scope: object) -> str:
    """Extrae el sufijo literal (``/analyses.<accion>``) de un scope del template.

    Un ``AllowedOAuthScopes`` es un ``{"Fn::Join": ["", [{"Ref": ...}, sufijo]]}``.
    Esta función devuelve el último elemento literal de ese ``Fn::Join``, que es el
    sufijo del scope independiente del identificador lógico del Resource Server.

    Args:
        scope: Un elemento de ``AllowedOAuthScopes`` del template.

    Returns:
        El sufijo literal del scope (p. ej. ``"/analyses.purge"``), o la cadena
        tal cual si ya fuera literal.
    """

    if isinstance(scope, dict) and "Fn::Join" in scope:
        parts = scope["Fn::Join"][1]
        return str(parts[-1])
    return str(scope)


def _client_scope_suffixes(client: dict[str, object]) -> list[str]:
    """Devuelve los sufijos de scope ordenados de un app client del template."""

    props = client["Properties"]  # type: ignore[index]
    scopes = props.get("AllowedOAuthScopes", [])  # type: ignore[union-attr]
    return sorted(_scope_suffix(s) for s in scopes)


@lru_cache(maxsize=1)
def _auth_template() -> assertions.Template:
    """Sintetiza el ``AuthStack`` aislado y devuelve su :class:`Template` (cacheado).

    El ``AuthStack`` no tiene dependencias (diseño 14.4.2), así que se sintetiza en
    su propia ``App`` sin cablear ningún otro stack. Esto además lo aísla de las
    tareas en curso (``notifications``/``compute``) que puedan romper el grafo
    completo de ``app.py``.
    """

    app = cdk.App()
    stack = AuthStack(app, "sentiment-test-auth", config=_AUTH_TEST_CONFIG)
    return assertions.Template.from_stack(stack)


class TestUserPool:
    """El User Pool declara el atributo inmutable ``custom:tenant_id`` (1.9/2.1)."""

    def test_single_user_pool_is_defined(self) -> None:
        _auth_template().resource_count_is("AWS::Cognito::UserPool", 1)

    def test_user_pool_declares_immutable_tenant_id_attribute(self) -> None:
        # El esquema del pool debe incluir `tenant_id` como String y NO mutable:
        # la pertenencia de un principal a un Tenant no se reasigna (criterio 2.1).
        _auth_template().has_resource_properties(
            "AWS::Cognito::UserPool",
            {
                "Schema": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Name": "tenant_id",
                                "AttributeDataType": "String",
                                "Mutable": False,
                            }
                        )
                    ]
                )
            },
        )

    def test_user_pool_disables_self_sign_up(self) -> None:
        # Los consumidores son Sistemas_M2M, no usuarios finales: sin auto-registro.
        _auth_template().has_resource_properties(
            "AWS::Cognito::UserPool",
            {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
        )


class TestResourceServerScopes:
    """El Resource Server expone exactamente los tres scopes del Sistema."""

    def test_resource_server_identifier_is_sentiment(self) -> None:
        _auth_template().has_resource_properties(
            "AWS::Cognito::UserPoolResourceServer",
            {"Identifier": "sentiment"},
        )

    def test_resource_server_declares_the_three_scopes(self) -> None:
        # Los tres scopes de autorización (criterios 1.6/1.7/19.1), ni más ni menos.
        template = _auth_template()
        servers = template.find_resources("AWS::Cognito::UserPoolResourceServer")
        assert len(servers) == 1, "debe haber exactamente un Resource Server"

        (server,) = servers.values()
        scopes = server["Properties"]["Scopes"]
        scope_names = sorted(s["ScopeName"] for s in scopes)
        assert scope_names == ["analyses.purge", "analyses.read", "analyses.write"], (
            f"scopes inesperados en el Resource Server: {scope_names}"
        )


class TestAppClients:
    """Dos app clients client_credentials; el de Purga con mínimo privilegio."""

    def test_two_app_clients_are_defined(self) -> None:
        # Un cliente general (write+read) y uno dedicado a la Purga (purge).
        _auth_template().resource_count_is("AWS::Cognito::UserPoolClient", 2)

    def test_clients_only_allow_client_credentials_flow(self) -> None:
        # Ningún flujo interactivo (code/implicit): solo client_credentials (D6).
        template = _auth_template()
        for client in template.find_resources(
            "AWS::Cognito::UserPoolClient"
        ).values():
            flows = client["Properties"]["AllowedOAuthFlows"]
            assert flows == ["client_credentials"], (
                f"un app client habilita flujos no M2M: {flows}"
            )

    def test_clients_generate_a_secret(self) -> None:
        # client_credentials exige un client secret (cliente confidencial).
        template = _auth_template()
        for client in template.find_resources(
            "AWS::Cognito::UserPoolClient"
        ).values():
            assert client["Properties"]["GenerateSecret"] is True

    def test_access_token_validity_is_at_most_one_hour(self) -> None:
        # Vigencia máxima del Access_Token: 3.600 s = 60 min (criterio 1.8).
        template = _auth_template()
        for client in template.find_resources(
            "AWS::Cognito::UserPoolClient"
        ).values():
            props = client["Properties"]
            assert props["AccessTokenValidity"] == _ACCESS_TOKEN_VALIDITY_MINUTES
            assert props["TokenValidityUnits"]["AccessToken"] == "minutes"

    def test_purge_client_has_only_the_purge_scope(self) -> None:
        # Mínimo privilegio (criterio 15.11): el cliente de Purga NO puede escribir
        # ni leer Lotes; solo purgar. Se identifica por ser el único cuyo conjunto
        # de scopes es exactamente {analyses.purge}.
        template = _auth_template()
        clients = template.find_resources("AWS::Cognito::UserPoolClient")

        purge_clients = [
            c
            for c in clients.values()
            if _client_scope_suffixes(c) == [_PURGE_SCOPE_SUFFIX]
        ]
        assert len(purge_clients) == 1, (
            "debe existir exactamente un app client con SOLO el scope "
            f"{_PURGE_SCOPE_SUFFIX!r}; encontrados: {len(purge_clients)}"
        )

    def test_general_client_has_write_and_read_but_not_purge(self) -> None:
        # El cliente general tiene write+read y NUNCA el scope de Purga.
        template = _auth_template()
        clients = template.find_resources("AWS::Cognito::UserPoolClient")

        general = [
            c
            for c in clients.values()
            if _client_scope_suffixes(c)
            == sorted([_WRITE_SCOPE_SUFFIX, _READ_SCOPE_SUFFIX])
        ]
        assert len(general) == 1, (
            "debe existir exactamente un app client con write+read (sin purge)"
        )
        assert _PURGE_SCOPE_SUFFIX not in _client_scope_suffixes(general[0])


class TestM2mSecrets:
    """Los client_secret se generan y rotan a 90 días (criterios 1.13 / 17.20)."""

    def test_two_secrets_are_defined(self) -> None:
        # Un secreto por app client M2M (general y purga).
        _auth_template().resource_count_is("AWS::SecretsManager::Secret", 2)

    def test_secrets_generate_their_value(self) -> None:
        # El valor NO se embebe: se declara `GenerateSecretString` (criterio 17.20).
        template = _auth_template()
        for secret in template.find_resources(
            "AWS::SecretsManager::Secret"
        ).values():
            props = secret["Properties"]
            assert "GenerateSecretString" in props, (
                "el secreto debe generar su valor, no traerlo embebido"
            )
            # Y nunca debe traer un `SecretString` literal con el valor.
            assert "SecretString" not in props

    def test_each_secret_rotates_every_90_days(self) -> None:
        # Rotación automática con intervalo máximo de 90 días (criterio 1.13).
        template = _auth_template()
        schedules = template.find_resources(
            "AWS::SecretsManager::RotationSchedule"
        )
        assert len(schedules) == 2, "debe haber una rotación por secreto M2M"
        for schedule in schedules.values():
            rules = schedule["Properties"]["RotationRules"]
            assert rules["ScheduleExpression"] == "rate(90 days)", (
                f"intervalo de rotación inesperado: {rules}"
            )


class TestTemplateHasNoEmbeddedSecret:
    """El template no embebe ningún valor de secreto (criterio 17.20)."""

    def test_template_has_no_obvious_secret_markers(self) -> None:
        # Red de seguridad barata: marcadores que nunca deben aparecer literales.
        template_json = assertions.Template.to_json(_auth_template())
        haystack = json.dumps(template_json).lower()
        for marker in ("aws_secret_access_key", "private_key", "-----begin"):
            assert marker not in haystack, (
                f"posible secreto embebido en el template: {marker!r}"
            )


class TestRotationLambdaLeastPrivilege:
    """La Lambda de rotación acota a ARN concretos sus acciones de datos (17.8).

    Igual que :mod:`tests.test_iam_policies` excluye X-Ray (cuyas acciones no
    admiten ARN de recurso), aquí se excluye ``secretsmanager:GetRandomPassword``:
    es una acción **sin recurso direccionable** que CDK añade por defecto al rol de
    toda Lambda de rotación y que esta implementación, además, no ejerce (genera el
    valor localmente). Toda **otra** acción de ``secretsmanager:`` y de
    ``cognito-idp:`` debe estar acotada a un ARN concreto (criterios 15.11 / 17.8).
    """

    # Acciones que no admiten ARN de recurso y, por tanto, no cuentan como comodín
    # sobre datos (análogo a la exclusión de X-Ray en test_iam_policies).
    _RESOURCELESS_ACTIONS = frozenset({"secretsmanager:GetRandomPassword"})

    @pytest.mark.parametrize(
        "action_prefix", ["secretsmanager:", "cognito-idp:"]
    )
    def test_no_wildcard_resource_on_scopable_data_actions(
        self, action_prefix: str
    ) -> None:
        template = _auth_template()
        offenders: list[str] = []
        for policy in template.find_resources("AWS::IAM::Policy").values():
            document = policy["Properties"]["PolicyDocument"]
            for statement in document.get("Statement", []):
                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                resources = statement.get("Resource", [])
                if isinstance(resources, str):
                    resources = [resources]
                scopable = [
                    a
                    for a in actions
                    if a.startswith(action_prefix)
                    and a not in self._RESOURCELESS_ACTIONS
                ]
                if scopable and any(r == "*" for r in resources):
                    offenders.append(f"{statement.get('Sid')}: {scopable}")

        assert not offenders, (
            f"acciones acotables {action_prefix!r} sobre Resource '*' "
            f"(criterio 17.8): {offenders}"
        )
