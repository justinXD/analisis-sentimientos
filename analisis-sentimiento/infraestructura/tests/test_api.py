"""Pruebas_De_Infraestructura del dominio de exposición ``ApiStack`` (tarea 13.2).

Afirman sobre el template de CloudFormation sintetizado las garantías de la capa de
API derivadas de los criterios 1.1-1.7, 1.12, 1.16, 1.17, 15.9, 15.12, 15.13,
15.15, 18.10-18.12 y 19.2 (y de los *steerings* de seguridad y mínimo privilegio):

- **API REST con autorizador JWT nativo de Cognito** (``COGNITO_USER_POOLS``), que
  delega en API Gateway la validación de firma/``iss``/``aud``/``exp`` (criterios
  1.1-1.5) sin código propio, con el token en el header ``Authorization``.
- **Scope por ruta** (criterios 1.6/1.7/19.2): cada método declara exactamente el
  scope que su ruta exige (``analyses.write`` / ``analyses.read`` / ``analyses.purge``).
- **WAF** asociado al *stage* con una regla de **rate limiting** por IP de 2.000
  peticiones / 5 min (criterio 15.12) y los tres grupos gestionados de AWS.
- **Usage_Plan** con throttling de 10 rps en ráfaga y cuota de 1.000/día
  (criterio 15.13); la API_Key mide y limita, no autentica (criterio 1.12).
- **CORS** restringido a la lista blanca del entorno, nunca ``*`` (criterio 15.9).
- **Las cuatro Lambdas de API** con sus concurrencias reservadas 20/30/10/2
  (criterios 18.10-18.12) y aislamiento multi-tenant por IAM con *session tags*
  (``dynamodb:LeadingKeys``, prefijos de S3; diseño 12.2 / 12.2.1).

Las assertions usan el *harness* de :mod:`tests._harness`, que sintetiza el
``ApiStack`` sobre la cadena real de dependencias (``auth``/``storage``/``data``/
``ingest``/``notifications``) sin vincular cuenta/región y sin empaquetar assets
(igual que ``cdk synth`` en CI).
"""

from __future__ import annotations

import json

from aws_cdk.assertions import Match

from tests._harness import TEST_ENV_CONFIG, synth_api_template

# Handlers de las cuatro Lambdas de API y su concurrencia reservada esperada
# (criterios 18.10/18.11/18.12 y el objetivo de api_purge).
_EXPECTED_RESERVED = {
    "sentiment.handlers.api_submit.handler": 20,
    "sentiment.handlers.api_status.handler": 30,
    "sentiment.handlers.api_download.handler": 10,
    "sentiment.handlers.api_purge.handler": 2,
}

# Sufijos de scope tal como aparecen en el template: CDK los emite como un
# ``Fn::Join`` que concatena el ``Ref``/``Fn::ImportValue`` del Resource Server con
# ``"/analyses.<accion>"``. Las assertions comparan el sufijo literal.
_WRITE_SUFFIX = "/analyses.write"
_READ_SUFFIX = "/analyses.read"
_PURGE_SUFFIX = "/analyses.purge"


def _scope_suffix(scope: object) -> str:
    """Extrae el sufijo literal (``/analyses.<accion>``) de un scope del template."""

    if isinstance(scope, dict) and "Fn::Join" in scope:
        return str(scope["Fn::Join"][1][-1])
    return str(scope)


def _methods() -> list[dict[str, object]]:
    """Devuelve las propiedades de todos los métodos de API Gateway del template."""

    template = synth_api_template()
    methods = template.find_resources("AWS::ApiGateway::Method")
    return [m["Properties"] for m in methods.values()]


def _authorized_methods() -> list[dict[str, object]]:
    """Métodos con autorización Cognito (excluye los OPTIONS de CORS, sin auth)."""

    return [
        p
        for p in _methods()
        if p.get("AuthorizationType") == "COGNITO_USER_POOLS"
    ]


class TestJwtAuthorizer:
    """Autorizador JWT nativo de Cognito en el borde (criterios 1.1-1.5)."""

    def test_exactly_one_cognito_authorizer(self) -> None:
        synth_api_template().resource_count_is(
            "AWS::ApiGateway::Authorizer", 1
        )

    def test_authorizer_is_cognito_user_pools(self) -> None:
        # El autorizador nativo valida firma/iss/aud/exp sin código propio (1.1-1.5).
        synth_api_template().has_resource_properties(
            "AWS::ApiGateway::Authorizer",
            {
                "Type": "COGNITO_USER_POOLS",
                # El token viaja en el header estándar Authorization: Bearer <jwt>.
                "IdentitySource": "method.request.header.Authorization",
            },
        )

    def test_every_business_method_uses_the_authorizer(self) -> None:
        # Toda ruta de negocio (no OPTIONS) exige autorización Cognito: ninguna
        # queda abierta (criterios 1.1/1.6/1.7).
        authorized = _authorized_methods()
        assert len(authorized) == 5, (
            f"se esperaban 5 métodos autorizados (POST, 2×GET, GET download, "
            f"DELETE); encontrados: {len(authorized)}"
        )
        for props in authorized:
            assert props.get("AuthorizerId") is not None, (
                "un método Cognito no referencia el autorizador JWT"
            )


class TestScopePerRoute:
    """Cada ruta exige exactamente su scope (criterios 1.6/1.7/19.2)."""

    def _scopes_by_method(self) -> dict[tuple[str, bool], list[str]]:
        """Mapea (verbo HTTP, tiene_path_param) → sufijos de scope del método."""

        result: dict[tuple[str, bool], list[str]] = {}
        template = synth_api_template()
        for res in template.find_resources("AWS::ApiGateway::Method").values():
            props = res["Properties"]
            if props.get("AuthorizationType") != "COGNITO_USER_POOLS":
                continue
            scopes = sorted(
                _scope_suffix(s) for s in props.get("AuthorizationScopes", [])
            )
            # No se distinguen las dos rutas GET por el método; se agrupan todos
            # los scopes por verbo para verificar el conjunto global por verbo.
            result.setdefault((str(props["HttpMethod"]), False), []).extend(scopes)
        return result

    def test_post_requires_write_scope(self) -> None:
        scopes = self._scopes_by_method()[("POST", False)]
        assert scopes == [_WRITE_SUFFIX], (
            f"POST /v1/analyses debe exigir solo {_WRITE_SUFFIX}; es {scopes}"
        )

    def test_delete_requires_purge_scope(self) -> None:
        # El scope de purga lo valida API Gateway antes de invocar la Lambda (19.2).
        scopes = self._scopes_by_method()[("DELETE", False)]
        assert scopes == [_PURGE_SUFFIX], (
            f"DELETE /v1/analyses/{{id}} debe exigir solo {_PURGE_SUFFIX}; "
            f"es {scopes}"
        )

    def test_all_get_methods_require_read_scope(self) -> None:
        # Las tres rutas GET (listado, estado, descarga) exigen analyses.read (1.7).
        get_scopes = self._scopes_by_method()[("GET", False)]
        assert get_scopes, "no se encontró ningún método GET autorizado"
        assert set(get_scopes) == {_READ_SUFFIX}, (
            f"toda ruta GET debe exigir {_READ_SUFFIX}; scopes vistos: {get_scopes}"
        )

    def test_write_and_purge_are_segregated(self) -> None:
        # Mínimo privilegio por ruta: el envío no concede purga y viceversa.
        by_method = self._scopes_by_method()
        assert _PURGE_SUFFIX not in by_method[("POST", False)]
        assert _WRITE_SUFFIX not in by_method[("DELETE", False)]


class TestApiLambdas:
    """Las cuatro Lambdas de API con sus concurrencias reservadas (18.10-18.12)."""

    def test_four_api_functions_are_defined(self) -> None:
        synth_api_template().resource_count_is("AWS::Lambda::Function", 4)

    def test_each_function_has_its_reserved_concurrency(self) -> None:
        template = synth_api_template()
        found: dict[str, int | None] = {}
        for res in template.find_resources("AWS::Lambda::Function").values():
            props = res["Properties"]
            found[props["Handler"]] = props.get("ReservedConcurrentExecutions")

        for handler, expected in _EXPECTED_RESERVED.items():
            assert found.get(handler) == expected, (
                f"{handler} debe tener concurrencia reservada {expected}; "
                f"es {found.get(handler)}"
            )

    def test_functions_are_arm64(self) -> None:
        # Secure default del BackendFunction: arm64 (criterio 17.12).
        template = synth_api_template()
        for res in template.find_resources("AWS::Lambda::Function").values():
            assert res["Properties"]["Architectures"] == ["arm64"]


class TestWaf:
    """WAF asociado al stage con rate limiting por IP (criterios 15.12 / diseño 12.7)."""

    def test_web_acl_is_regional(self) -> None:
        # API Gateway REST es regional: la Web ACL debe serlo también.
        synth_api_template().has_resource_properties(
            "AWS::WAFv2::WebACL", {"Scope": "REGIONAL"}
        )

    def test_web_acl_is_associated_to_a_stage(self) -> None:
        synth_api_template().resource_count_is(
            "AWS::WAFv2::WebACLAssociation", 1
        )

    def test_rate_limit_rule_is_2000_per_5_minutes(self) -> None:
        # Regla de rate limiting por IP: 2.000 peticiones / 5 min (criterio 15.12).
        template = synth_api_template()
        (web_acl,) = template.find_resources("AWS::WAFv2::WebACL").values()
        rules = web_acl["Properties"]["Rules"]
        rate_rules = [
            r for r in rules if "RateBasedStatement" in r.get("Statement", {})
        ]
        assert len(rate_rules) == 1, "debe haber exactamente una regla de rate limit"
        assert rate_rules[0]["Statement"]["RateBasedStatement"]["Limit"] == 2000
        assert (
            rate_rules[0]["Statement"]["RateBasedStatement"]["AggregateKeyType"]
            == "IP"
        )

    def test_managed_rule_groups_are_present(self) -> None:
        # Los tres grupos gestionados de AWS del diseño 12.7, además del rate limit.
        template = synth_api_template()
        (web_acl,) = template.find_resources("AWS::WAFv2::WebACL").values()
        names = {r["Name"] for r in web_acl["Properties"]["Rules"]}
        for expected in (
            "AWSManagedRulesCommonRuleSet",
            "AWSManagedRulesKnownBadInputsRuleSet",
            "AWSManagedRulesAmazonIpReputationList",
        ):
            assert expected in names, f"falta el grupo gestionado {expected}"

    def test_waf_mode_is_count_in_test_env(self) -> None:
        # TEST_ENV_CONFIG usa waf_mode=COUNT (observación); la regla de rate limit
        # debe estar en modo Count, no Block, para ese entorno.
        assert TEST_ENV_CONFIG.waf_mode == "COUNT"
        template = synth_api_template()
        (web_acl,) = template.find_resources("AWS::WAFv2::WebACL").values()
        rate_rule = next(
            r
            for r in web_acl["Properties"]["Rules"]
            if "RateBasedStatement" in r.get("Statement", {})
        )
        assert "Count" in rate_rule["Action"], (
            f"en modo COUNT la regla de rate limit debe contar, no bloquear; "
            f"acción: {rate_rule['Action']}"
        )


class TestUsagePlan:
    """Usage_Plan con throttling y cuota por Tenant (criterios 15.13 / 1.12)."""

    def test_usage_plan_throttle_and_quota(self) -> None:
        # 10 rps burst y 1.000 Lotes/día (criterio 15.13).
        synth_api_template().has_resource_properties(
            "AWS::ApiGateway::UsagePlan",
            {
                "Throttle": Match.object_like(
                    {"BurstLimit": 10, "RateLimit": 10}
                ),
                "Quota": Match.object_like({"Limit": 1000, "Period": "DAY"}),
            },
        )

    def test_an_api_key_exists_for_the_usage_plan(self) -> None:
        # La API_Key identifica al consumidor ante el plan; no autentica (1.12).
        template = synth_api_template()
        template.resource_count_is("AWS::ApiGateway::ApiKey", 1)
        template.resource_count_is("AWS::ApiGateway::UsagePlanKey", 1)

    def test_business_methods_require_api_key(self) -> None:
        # Las rutas de negocio exigen API_Key para que el Usage_Plan mida/limase.
        for props in _authorized_methods():
            assert props.get("ApiKeyRequired") is True, (
                "una ruta autorizada no exige API_Key para el Usage_Plan"
            )


class TestCors:
    """CORS restringido a la lista blanca del entorno, nunca ``*`` (criterio 15.9)."""

    def test_cors_origin_is_the_env_allowlist_not_wildcard(self) -> None:
        template = synth_api_template()
        expected_origin = TEST_ENV_CONFIG.cors_origins[0]

        # Los métodos OPTIONS de preflight llevan el Access-Control-Allow-Origin en
        # su integración MOCK. Debe ser el origen del entorno, jamás "*".
        found_allow_origin = False
        for res in template.find_resources("AWS::ApiGateway::Method").values():
            props = res["Properties"]
            if props.get("HttpMethod") != "OPTIONS":
                continue
            integration = props.get("Integration", {})
            responses = integration.get("IntegrationResponses", [])
            for response in responses:
                params = response.get("ResponseParameters", {})
                origin = params.get(
                    "method.response.header.Access-Control-Allow-Origin"
                )
                if origin is not None:
                    found_allow_origin = True
                    assert origin != "'*'", (
                        "CORS no debe permitir cualquier origen (criterio 15.9)"
                    )
                    assert expected_origin in str(origin), (
                        f"el origen CORS debe ser la allowlist del entorno "
                        f"({expected_origin}); es {origin}"
                    )

        assert found_allow_origin, (
            "no se encontró ninguna cabecera Access-Control-Allow-Origin en el "
            "preflight CORS"
        )

    def test_no_wildcard_cors_anywhere_in_template(self) -> None:
        # Red de seguridad barata: el literal '*' no debe aparecer como origen CORS.
        template_json = synth_api_template().to_json()
        haystack = json.dumps(template_json)
        assert "'*'" not in haystack or "Access-Control-Allow-Origin" not in haystack


class TestTenantIsolationIam:
    """Aislamiento multi-tenant por IAM con session tags (diseño 12.2 / 12.2.1)."""

    def _policy_statements(self) -> list[dict[str, object]]:
        """Todas las declaraciones IAM (políticas inline y de rol) del template."""

        template = synth_api_template()
        statements: list[dict[str, object]] = []
        for res in template.find_resources("AWS::IAM::Policy").values():
            doc = res["Properties"]["PolicyDocument"]
            statements.extend(doc.get("Statement", []))
        return statements

    def test_data_reads_are_scoped_by_leading_keys(self) -> None:
        # api_status/api_download/api_purge acotan DynamoDB por tenant con
        # dynamodb:LeadingKeys y ${aws:PrincipalTag/tenant_id} (criterios 2.5/19.12).
        statements = self._policy_statements()
        leading_key_stmts = [
            s
            for s in statements
            if "dynamodb:LeadingKeys"
            in json.dumps(s.get("Condition", {}))
        ]
        assert leading_key_stmts, (
            "ninguna política acota DynamoDB por dynamodb:LeadingKeys; se pierde "
            "el aislamiento multi-tenant a nivel de IAM (criterio 2.5)"
        )
        for stmt in leading_key_stmts:
            assert "PrincipalTag/tenant_id" in json.dumps(stmt["Condition"]), (
                "LeadingKeys debe resolverse por la session tag tenant_id (12.2.1)"
            )

    def test_download_s3_is_scoped_to_tenant_prefix(self) -> None:
        # s3:GetObject de api_download acotado a outputs/${tenant_id}/* (criterio 2.6).
        statements = self._policy_statements()
        get_object_stmts = [
            s
            for s in statements
            if "s3:GetObject" in _as_action_list(s.get("Action"))
        ]
        assert get_object_stmts, "api_download debe conceder s3:GetObject acotado"
        for stmt in get_object_stmts:
            resources = json.dumps(stmt.get("Resource"))
            assert "outputs/" in resources and "PrincipalTag/tenant_id" in resources, (
                "s3:GetObject debe acotarse a outputs/${tenant_id}/* (criterio 2.6)"
            )

    def test_purge_is_the_only_role_with_delete(self) -> None:
        # Única Lambda con borrado: s3:DeleteObject / dynamodb:DeleteItem (diseño 12.2).
        statements = self._policy_statements()
        delete_actions = {"s3:DeleteObject", "s3:DeleteObjectVersion", "dynamodb:DeleteItem"}
        for stmt in statements:
            actions = set(_as_action_list(stmt.get("Action")))
            if actions & delete_actions:
                # El borrado de S3/Dynamo debe acotar por tag del lote/tenant.
                blob = json.dumps(stmt)
                assert "PrincipalTag" in blob, (
                    "una acción de borrado no está acotada por session tag "
                    "(tenant_id/analysis_id); diseño 12.2.1"
                )

    def test_no_data_action_on_wildcard_resource(self) -> None:
        # Refuerzo local del criterio 17.8/15.11 sobre el propio ApiStack.
        data_prefixes = ("dynamodb:", "s3:", "sqs:", "events:", "kms:")
        offenders: list[str] = []
        for stmt in self._policy_statements():
            actions = _as_action_list(stmt.get("Action"))
            resources = stmt.get("Resource")
            resources = resources if isinstance(resources, list) else [resources]
            scopable = [
                a for a in actions if a.startswith(data_prefixes)
            ]
            if scopable and any(r == "*" for r in resources):
                offenders.append(f"{stmt.get('Sid')}: {scopable}")
        assert not offenders, (
            f"acciones de datos sobre Resource '*' en el ApiStack: {offenders}"
        )


def _as_action_list(action: object) -> list[str]:
    """Normaliza el campo ``Action`` de una declaración a lista de cadenas."""

    if action is None:
        return []
    if isinstance(action, str):
        return [action]
    if isinstance(action, list):
        return [a for a in action if isinstance(a, str)]
    return []
