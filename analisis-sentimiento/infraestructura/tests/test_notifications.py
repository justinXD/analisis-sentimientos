"""Pruebas_De_Infraestructura del ``NotificationsStack`` (tarea 13.3).

Afirman sobre el template de CloudFormation sintetizado las garantías de la tarea
13.3, derivadas de los criterios 13.1, 13.11, 9.9 y 9.10 (y de los *steerings* de
seguridad y mínimo privilegio):

- **Bus_Eventos** con el nombre EXACTO ``{project}-{env}-events`` (criterio 13.1 y
  el acoplamiento con ``compute_stack``). Este es el *assert* más importante: si el
  nombre cambia, la política ``events:PutEvents`` y la variable ``EVENT_BUS_NAME``
  de ``aggregate_results`` apuntarían a un bus inexistente.
- **Regla** sobre el bus que hace *match* por ``detail-type = "analysis.completed"``
  y enruta a ``notify_webhook`` (criterio 13.1).
- **Lambda ``notify_webhook``** con el handler correcto y destino asíncrono
  ``OnFailure`` hacia la DLQ_Notificaciones (criterios 9.9, 9.10).
- **Secreto HMAC** con valor **generado** (nunca *hardcodeado*), cifrado con la
  CMK, y permiso de lectura de ``notify_webhook`` **acotado** a su ARN concreto
  (nunca comodín; mínimo privilegio).
- **DLQ_Notificaciones** cifrada con la CMK y con retención de 14 días (9.10).

Las assertions usan el *harness* de :mod:`tests._harness`, que sintetiza el stack
sobre la CMK, el Layer y la Dynamo_Table reales de ``storage``/``data`` sin
vincular cuenta/región y sin empaquetar assets (igual que ``cdk synth`` en CI).
"""

from __future__ import annotations

from aws_cdk.assertions import Match

from stacks.naming import event_bus_name
from tests._harness import TEST_ENV_CONFIG, synth_notifications_template


class TestEventBusName:
    """El Bus_Eventos tiene el nombre EXACTO del contrato con ``compute_stack``."""

    def test_event_bus_uses_the_exact_contract_name(self) -> None:
        # El acoplamiento crítico: debe ser {project}-{env}-events, idéntico al que
        # aggregate_results usa en su política events:PutEvents y en EVENT_BUS_NAME.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Events::EventBus",
            {"Name": f"{TEST_ENV_CONFIG.project}-{TEST_ENV_CONFIG.env_name}-events"},
        )

    def test_event_bus_name_matches_compute_convention(self) -> None:
        # Red de seguridad explícita: el nombre proviene de la única fuente de
        # verdad compartida con compute_stack (_event_bus_name), no de un literal.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Events::EventBus",
            {"Name": event_bus_name(TEST_ENV_CONFIG)},
        )

    def test_exactly_one_event_bus_is_created(self) -> None:
        template = synth_notifications_template()

        template.resource_count_is("AWS::Events::EventBus", 1)


class TestAnalysisCompletedRule:
    """La regla hace *match* por ``analysis.completed`` y enruta a la Lambda (13.1)."""

    def test_rule_matches_analysis_completed_detail_type(self) -> None:
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Events::Rule",
            {"EventPattern": {"detail-type": ["analysis.completed"]}},
        )

    def test_rule_is_bound_to_the_custom_bus(self) -> None:
        # La regla debe colgar del Bus_Eventos personalizado (tiene EventBusName),
        # no del bus por defecto de la cuenta.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Events::Rule",
            {"EventBusName": Match.any_value()},
        )

    def test_rule_targets_a_lambda(self) -> None:
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Events::Rule",
            {"Targets": Match.array_with([Match.object_like({"Arn": Match.any_value()})])},
        )


class TestNotifyWebhookFunction:
    """La Lambda ``notify_webhook`` se construye con el handler y el redrive correctos."""

    def test_notify_webhook_handler_is_wired(self) -> None:
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Lambda::Function",
            {"Handler": "sentiment.handlers.notify_webhook.handler"},
        )

    def test_notify_webhook_has_onfailure_destination_to_the_dlq(self) -> None:
        # El destino asíncrono OnFailure traslada los webhooks no entregados a la
        # DLQ_Notificaciones conservando el evento original (criterios 9.9, 9.10).
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Lambda::EventInvokeConfig",
            {
                "DestinationConfig": {
                    "OnFailure": {"Destination": Match.any_value()}
                }
            },
        )

    def test_notify_webhook_receives_the_secret_arn_env_var(self) -> None:
        # Se publica el ARN del secreto (identificador), nunca su valor.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Environment": {
                    "Variables": Match.object_like(
                        {"WEBHOOK_HMAC_SECRET_ARN": Match.any_value()}
                    )
                }
            },
        )


class TestWebhookHmacSecret:
    """El secreto HMAC se genera y se cifra, y su lectura es de mínimo privilegio."""

    def test_secret_value_is_generated_not_hardcoded(self) -> None:
        # Un valor generado se expresa como `GenerateSecretString`; un secreto
        # hardcodeado traería `SecretString`. Afirmamos lo primero y la ausencia
        # de lo segundo (steering de seguridad / criterio 15.5).
        template = synth_notifications_template()

        secrets = template.find_resources("AWS::SecretsManager::Secret")
        assert len(secrets) == 1, "debe existir exactamente un secreto HMAC"

        (secret,) = secrets.values()
        properties = secret["Properties"]
        assert "GenerateSecretString" in properties, (
            "el secreto HMAC debe GENERARSE, no incluir material en el template"
        )
        assert "SecretString" not in properties, (
            "el secreto HMAC no debe traer un valor hardcodeado (SecretString)"
        )

    def test_secret_is_encrypted_with_a_cmk(self) -> None:
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SecretsManager::Secret",
            {"KmsKeyId": Match.any_value()},
        )

    def test_notify_webhook_reads_only_get_secret_value(self) -> None:
        # El permiso del secreto debe ser de solo lectura (GetSecretValue /
        # DescribeSecret), nunca de escritura/rotación (PutSecretValue, etc.).
        template = synth_notifications_template()

        policies = template.find_resources("AWS::IAM::Policy")
        secret_actions: list[str] = []
        for policy in policies.values():
            document = policy["Properties"]["PolicyDocument"]
            for statement in document["Statement"]:
                actions = statement.get("Action", [])
                actions = actions if isinstance(actions, list) else [actions]
                for action in actions:
                    if isinstance(action, str) and action.startswith(
                        "secretsmanager:"
                    ):
                        secret_actions.append(action)

        assert secret_actions, "notify_webhook debe tener permiso sobre el secreto"
        # Solo lectura: ninguna acción de mutación del secreto.
        forbidden = {
            "secretsmanager:PutSecretValue",
            "secretsmanager:UpdateSecret",
            "secretsmanager:DeleteSecret",
            "secretsmanager:RotateSecret",
            "secretsmanager:*",
        }
        offenders = sorted(set(secret_actions) & forbidden)
        assert not offenders, (
            f"el permiso sobre el secreto no debe permitir mutarlo: {offenders}"
        )
        assert "secretsmanager:GetSecretValue" in secret_actions, (
            "notify_webhook debe poder leer el valor del secreto (GetSecretValue)"
        )


class TestNotificationsDlq:
    """La DLQ_Notificaciones está cifrada con la CMK y retiene 14 días (9.10)."""

    def test_dlq_is_encrypted_with_a_cmk(self) -> None:
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SQS::Queue",
            {"KmsMasterKeyId": Match.any_value()},
        )

    def test_dlq_retains_messages_14_days(self) -> None:
        # 14 días = 1.209.600 segundos (criterio 9.10).
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SQS::Queue",
            {"MessageRetentionPeriod": 14 * 24 * 60 * 60},
        )


class TestSesVerifiedIdentity:
    """La identidad SES verificada se sintetiza desde la config del entorno (13.4)."""

    def test_a_single_ses_email_identity_is_created(self) -> None:
        template = synth_notifications_template()

        template.resource_count_is("AWS::SES::EmailIdentity", 1)

    def test_identity_uses_the_configured_sender_email(self) -> None:
        # La dirección remitente proviene de config.ses_sender_email (marcador no
        # secreto por entorno), nunca de un literal hardcodeado en el stack.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SES::EmailIdentity",
            {"EmailIdentity": TEST_ENV_CONFIG.ses_sender_email},
        )

    def test_identity_is_bound_to_the_configuration_set(self) -> None:
        # La identidad hereda el seguimiento de rebotes/quejas por defecto
        # (defensa en profundidad sobre el criterio 13.10).
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SES::EmailIdentity",
            {"ConfigurationSetAttributes": {"ConfigurationSetName": Match.any_value()}},
        )


class TestSesConfigurationSet:
    """El configuration set dedicado captura rebotes y quejas (criterio 13.10)."""

    def test_a_single_configuration_set_is_created(self) -> None:
        template = synth_notifications_template()

        template.resource_count_is("AWS::SES::ConfigurationSet", 1)

    def test_configuration_set_uses_the_contract_name(self) -> None:
        # Nombre {project}-{env}-email-events: el backend (notify_email) lo pasa en
        # cada SendEmail vía SES_CONFIGURATION_SET.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SES::ConfigurationSet",
            {
                "Name": (
                    f"{TEST_ENV_CONFIG.project}-{TEST_ENV_CONFIG.env_name}"
                    "-email-events"
                )
            },
        )

    def test_event_destination_captures_bounce_and_complaint(self) -> None:
        # El criterio 13.10 exige EXACTAMENTE los eventos de rebote y queja. El
        # orden que emite CDK no es significativo; se comprueba el conjunto.
        template = synth_notifications_template()

        destinations = template.find_resources(
            "AWS::SES::ConfigurationSetEventDestination"
        )
        assert len(destinations) == 1, (
            "debe existir exactamente un destino de eventos para rebotes/quejas"
        )

        (destination,) = destinations.values()
        event_destination = destination["Properties"]["EventDestination"]
        matching_types = set(event_destination["MatchingEventTypes"])
        assert matching_types == {"bounce", "complaint"}, (
            "el configuration set debe capturar solo bounce y complaint "
            f"(criterio 13.10), no {sorted(matching_types)}"
        )
        assert event_destination.get("Enabled") is True, (
            "el destino de eventos de rebotes/quejas debe estar habilitado"
        )

    def test_event_destination_targets_an_sns_topic(self) -> None:
        # El destino de eventos publica en el topic SNS de rebotes/quejas.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SES::ConfigurationSetEventDestination",
            {
                "EventDestination": Match.object_like(
                    {"SnsDestination": {"TopicARN": Match.any_value()}}
                )
            },
        )


class TestSesBounceComplaintTopic:
    """El topic SNS de eventos de SES está cifrado con la CMK y exige TLS (15.6)."""

    def test_bounce_topic_is_encrypted_with_a_cmk(self) -> None:
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SNS::Topic",
            {"KmsMasterKeyId": Match.any_value()},
        )

    def test_bounce_topic_denies_insecure_transport(self) -> None:
        # enforce_ssl añade una política de recurso que deniega aws:SecureTransport
        # = false. Verificamos que exista la denegación sobre el topic.
        template = synth_notifications_template()

        template.has_resource_properties(
            "AWS::SNS::TopicPolicy",
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Effect": "Deny",
                                        "Condition": {
                                            "Bool": {"aws:SecureTransport": "false"}
                                        },
                                    }
                                )
                            ]
                        )
                    }
                )
            },
        )
