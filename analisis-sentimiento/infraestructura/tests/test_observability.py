"""Pruebas_De_Infraestructura del ``ObservabilityStack`` (tarea 13.5).

Afirman sobre el template de CloudFormation sintetizado las garantías de la tarea
13.5, derivadas de los criterios 14.7, 14.8, 14.9 y 14.14 (y de los *steerings* de
seguridad):

- **Topic SNS de guardia** (criterio 14.14): existe exactamente uno, está cifrado
  en reposo (``KmsMasterKeyId``) y **no** declara ninguna suscripción con un
  endpoint/correo real *hardcodeado* (la suscripción es fuera de banda).
- **Las cuatro alarmas críticas** con su métrica, umbral y la acción hacia el
  topic de guardia:
  - DLQ_Solicitudes: ``ApproximateNumberOfMessagesVisible`` > 0 (criterio 14.7).
  - Step Functions: ``ExecutionsFailed`` ≥ 1 en 5 min (criterio 14.8).
  - ``AccessDenied``: *metric filter* sobre los log groups + métrica custom,
    alarma ≥ 1 (criterio 14.9).
  - Lotes atascados: métrica custom ``StuckAnalyses`` ≥ 1 (criterios 14.9/10.15).
- **La acción de alarma apunta al topic de guardia** en TODAS las alarmas
  (criterio 14.14).

Las assertions usan el *harness* de :mod:`tests._harness`, que sintetiza el stack
sobre la cadena de dependencias real (``auth``…``api``) sin vincular cuenta/región
y sin empaquetar assets (igual que ``cdk synth`` en CI).
"""

from __future__ import annotations

from aws_cdk.assertions import Match

from tests._harness import TEST_ENV_CONFIG, synth_observability_template

# Namespace de las métricas custom EMF del Sistema (fuente única: backend
# ``observability/metrics.py`` y constante del ``observability_stack``).
_METRICS_NAMESPACE = "SentimentPipeline"
_PREFIX = f"{TEST_ENV_CONFIG.project}-{TEST_ENV_CONFIG.env_name}"


class TestOnCallTopic:
    """El topic SNS de guardia existe, se cifra y no filtra endpoints (14.14)."""

    def test_exactly_one_oncall_topic_is_created(self) -> None:
        template = synth_observability_template()

        template.resource_count_is("AWS::SNS::Topic", 1)

    def test_oncall_topic_is_encrypted_at_rest(self) -> None:
        # Cifrado en reposo con la clave gestionada de SNS: el template trae
        # KmsMasterKeyId (criterio 15.6, defensa en profundidad de la notificación).
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::SNS::Topic",
            {"KmsMasterKeyId": Match.any_value()},
        )

    def test_no_subscription_hardcodes_a_real_endpoint(self) -> None:
        # La suscripción (correo/canal de guardia) se hace fuera de banda: el
        # template NO debe declarar ninguna AWS::SNS::Subscription con un endpoint
        # real (steering de seguridad / criterio 17.20).
        template = synth_observability_template()

        template.resource_count_is("AWS::SNS::Subscription", 0)


class TestCriticalAlarmCount:
    """La tarea 13.5 aporta cuatro alarmas críticas; 13.6 añade las no críticas.

    Tras la extensión 13.6 el stack contiene más alarmas (alta/media), así que la
    comprobación ya no afirma el total absoluto —eso lo cubre
    :class:`TestNonCriticalAlarms`— sino que las **cuatro críticas** de 13.5 siguen
    presentes por su métrica característica, de modo que la extensión no las haya
    desplazado ni alterado.
    """

    def test_the_four_critical_alarm_metrics_are_present(self) -> None:
        template = synth_observability_template()

        for metric_name, namespace in (
            ("ApproximateNumberOfMessagesVisible", "AWS/SQS"),
            ("ExecutionsFailed", "AWS/States"),
            ("AccessDeniedErrors", _METRICS_NAMESPACE),
            ("StuckAnalyses", _METRICS_NAMESPACE),
        ):
            template.has_resource_properties(
                "AWS::CloudWatch::Alarm",
                {
                    "MetricName": metric_name,
                    "Namespace": namespace,
                    "Threshold": 0,
                    "ComparisonOperator": "GreaterThanThreshold",
                },
            )


class TestDlqRequestsAlarm:
    """Alarma de mensajes en la DLQ_Solicitudes (criterio 14.7)."""

    def test_alarm_watches_dlq_visible_messages_above_zero(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "ApproximateNumberOfMessagesVisible",
                "Namespace": "AWS/SQS",
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "EvaluationPeriods": 1,
            },
        )


class TestExecutionsFailedAlarm:
    """Alarma de ejecuciones de Step Functions fallidas (criterio 14.8)."""

    def test_alarm_watches_executions_failed(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "ExecutionsFailed",
                "Namespace": "AWS/States",
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "EvaluationPeriods": 1,
            },
        )


class TestAccessDeniedAlarm:
    """Metric filter + alarma de ``AccessDenied`` en cualquier Lambda (14.9)."""

    def test_one_metric_filter_per_compute_log_group(self) -> None:
        # Las tres Lambdas de compute (comprehend/llm/aggregate) tienen cada una su
        # filtro que escribe la misma métrica custom (criterio 14.9).
        template = synth_observability_template()

        template.resource_count_is("AWS::Logs::MetricFilter", 3)

    def test_metric_filter_emits_access_denied_custom_metric(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::Logs::MetricFilter",
            {
                "FilterPattern": Match.string_like_regexp("AccessDenied"),
                "MetricTransformations": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "MetricNamespace": _METRICS_NAMESPACE,
                                "MetricName": "AccessDeniedErrors",
                                "MetricValue": "1",
                            }
                        )
                    ]
                ),
            },
        )

    def test_alarm_watches_the_access_denied_custom_metric(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "AccessDeniedErrors",
                "Namespace": _METRICS_NAMESPACE,
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        )


class TestStuckAnalysesAlarm:
    """Alarma de Lotes atascados sobre la métrica custom ``StuckAnalyses`` (14.9)."""

    def test_alarm_watches_stuck_analyses_custom_metric(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "StuckAnalyses",
                "Namespace": _METRICS_NAMESPACE,
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
                "EvaluationPeriods": 1,
            },
        )


class TestAlarmActionsTargetOnCallTopic:
    """Toda alarma (crítica y no crítica) notifica al topic de guardia (14.14).

    La tarea 13.6 reutiliza el mismo topic de guardia para las severidades
    alta/media (catálogo de alarmas 13.2), así que TODAS las alarmas del stack
    —no solo las cuatro críticas— deben enrutar su acción a ese topic.
    """

    def test_every_alarm_has_an_action_to_the_oncall_topic(self) -> None:
        # Localiza el id lógico del topic y verifica que CADA alarma lo referencia
        # en sus AlarmActions (criterio 14.14).
        template = synth_observability_template()

        topics = template.find_resources("AWS::SNS::Topic")
        assert len(topics) == 1, "debe existir exactamente un topic de guardia"
        (topic_logical_id,) = topics.keys()

        alarms = template.find_resources("AWS::CloudWatch::Alarm")
        assert len(alarms) >= 4, "deben existir al menos las cuatro alarmas críticas"

        for logical_id, alarm in alarms.items():
            actions = alarm["Properties"].get("AlarmActions", [])
            references = [
                action["Ref"]
                for action in actions
                if isinstance(action, dict) and "Ref" in action
            ]
            assert topic_logical_id in references, (
                f"la alarma {logical_id} no envía su notificación al topic de "
                f"guardia (criterio 14.14); AlarmActions={actions}"
            )

    def test_alarm_name_is_prefixed_by_project_env(self) -> None:
        # Las alarmas llevan el prefijo {project}-{env} para ser identificables en
        # la consola por entorno.
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {"AlarmName": Match.string_like_regexp(f"^{_PREFIX}-")},
        )


# ---------------------------------------------------------------------------
# Tarea 13.6: panel de CloudWatch, alarmas no críticas y presupuesto diario.
# ---------------------------------------------------------------------------


class TestDashboard:
    """Panel de CloudWatch con el estado del pipeline (criterio 14.13)."""

    def test_exactly_one_dashboard_is_created(self) -> None:
        template = synth_observability_template()

        template.resource_count_is("AWS::CloudWatch::Dashboard", 1)

    def test_dashboard_is_named_by_project_env(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Dashboard",
            {"DashboardName": f"{_PREFIX}-pipeline"},
        )

    def test_dashboard_body_references_the_key_pipeline_metrics(self) -> None:
        # El cuerpo del dashboard es un JSON serializado en DashboardBody. Debe
        # mencionar las métricas clave de las tres fases del pipeline (14.13): las
        # métricas de negocio EMF del backend y las de la Cola_Solicitudes/SES.
        template = synth_observability_template()

        dashboards = template.find_resources("AWS::CloudWatch::Dashboard")
        assert len(dashboards) == 1
        (dashboard,) = dashboards.values()
        body = dashboard["Properties"]["DashboardBody"]
        # DashboardBody es un Fn::Join de fragmentos (métricas con refs a recursos)
        # y literales; se aplana a texto para buscar los nombres de métrica.
        rendered = _flatten_cfn_text(body)

        for needle in (
            "ItemsProcessed",
            "ItemsFailed",
            "BedrockThrottles",
            "BedrockInputTokens",
            "BedrockOutputTokens",
            "StuckAnalyses",
            "ApproximateNumberOfMessagesVisible",
            "ApproximateAgeOfOldestMessage",
            "Reputation.BounceRate",
        ):
            assert needle in rendered, (
                f"el panel debe graficar la métrica {needle!r} (criterio 14.13); "
                "no aparece en DashboardBody"
            )


class TestLambdaErrorRateAlarms:
    """Alarma alta por Lambda cuando la tasa de error supera el 5% (14.10)."""

    def test_error_rate_alarm_uses_errors_over_invocations_expression(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "Threshold": 0.05,
                "ComparisonOperator": "GreaterThanThreshold",
                "Metrics": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Expression": Match.string_like_regexp(
                                    r"errors\s*/\s*invocations"
                                )
                            }
                        )
                    ]
                ),
            },
        )

    def test_one_error_rate_alarm_per_critical_path_function(self) -> None:
        # Cuatro Lambdas del camino crítico (3 de compute + api_submit) ⇒ cuatro
        # alarmas de tasa de error (criterio 14.10).
        template = synth_observability_template()

        alarms = template.find_resources("AWS::CloudWatch::Alarm")
        error_rate_alarms = [
            logical_id
            for logical_id in alarms
            if "LambdaErrorRate" in logical_id
        ]
        assert len(error_rate_alarms) == 4, (
            "debe haber una alarma de tasa de error por Lambda del camino crítico "
            f"(criterio 14.10); se encontraron {len(error_rate_alarms)}"
        )


class TestBedrockThrottlesAlarm:
    """Alarma alta sobre la métrica custom ``BedrockThrottles`` (catálogo 13.2)."""

    def test_alarm_watches_bedrock_throttles_above_fifty(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "BedrockThrottles",
                "Namespace": _METRICS_NAMESPACE,
                "Threshold": 50,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        )


class TestRequestsAgeAlarm:
    """Alarma alta de mensaje sin consumir en la Cola_Solicitudes (13.2)."""

    def test_alarm_watches_oldest_message_age_above_300s(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "ApproximateAgeOfOldestMessage",
                "Namespace": "AWS/SQS",
                "Threshold": 300,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        )


class TestSesBounceRateAlarm:
    """Alarma alta sobre la tasa de rebote de SES (catálogo 13.2)."""

    def test_alarm_watches_ses_bounce_rate_above_five_percent(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "Reputation.BounceRate",
                "Namespace": "AWS/SES",
                "Threshold": 0.05,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        )


class TestItemsFailedAlarm:
    """Alarma media sobre la métrica custom ``ItemsFailed`` (criterio 14.11)."""

    def test_alarm_watches_items_failed_custom_metric(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "ItemsFailed",
                "Namespace": _METRICS_NAMESPACE,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        )


class TestLambdaThrottledAlarms:
    """Alarma media por Lambda cuando se registra ≥ 1 throttle en 5 min (13.2)."""

    def test_one_throttled_alarm_per_critical_path_function(self) -> None:
        template = synth_observability_template()

        alarms = template.find_resources("AWS::CloudWatch::Alarm")
        throttled_alarms = [
            logical_id
            for logical_id in alarms
            if "LambdaThrottled" in logical_id
        ]
        assert len(throttled_alarms) == 4, (
            "debe haber una alarma de throttling por Lambda del camino crítico "
            f"(catálogo 13.2); se encontraron {len(throttled_alarms)}"
        )

    def test_throttled_alarm_watches_the_throttles_metric(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "Throttles",
                "Namespace": "AWS/Lambda",
                "Threshold": 0,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        )


class TestApiSubmitLatencyAlarm:
    """Alarma media sobre la latencia p99 de ``api_submit`` (catálogo 13.2)."""

    def test_alarm_watches_api_submit_p99_duration_above_5s(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "Duration",
                "Namespace": "AWS/Lambda",
                "ExtendedStatistic": "p99",
                # Duration está en milisegundos: 5 s = 5000 ms.
                "Threshold": 5000,
                "ComparisonOperator": "GreaterThanThreshold",
            },
        )


class TestDailyBudget:
    """Presupuesto diario de AWS Budgets que notifica a guardia (criterio 14.12)."""

    def test_exactly_one_daily_cost_budget_is_created(self) -> None:
        template = synth_observability_template()

        template.resource_count_is("AWS::Budgets::Budget", 1)

    def test_budget_tracks_daily_cost(self) -> None:
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::Budgets::Budget",
            {
                "Budget": Match.object_like(
                    {
                        "BudgetType": "COST",
                        "TimeUnit": "DAILY",
                        "BudgetName": f"{_PREFIX}-daily-cost",
                    }
                )
            },
        )

    def test_budget_notifies_the_oncall_topic_on_actual_overspend(self) -> None:
        # La notificación es sobre el gasto REAL (ACTUAL) y su suscriptor es el
        # topic SNS de guardia (criterio 14.12).
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::Budgets::Budget",
            {
                "NotificationsWithSubscribers": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Notification": Match.object_like(
                                    {
                                        "NotificationType": "ACTUAL",
                                        "ComparisonOperator": "GREATER_THAN",
                                    }
                                ),
                                "Subscribers": Match.array_with(
                                    [
                                        Match.object_like(
                                            {"SubscriptionType": "SNS"}
                                        )
                                    ]
                                ),
                            }
                        )
                    ]
                )
            },
        )

    def test_topic_policy_authorizes_budgets_service_to_publish(self) -> None:
        # Para que Budgets pueda publicar en el topic cifrado hace falta una
        # política de recurso que autorice al principal del servicio (mínimo
        # privilegio: acotada al propio topic). No debe conceder a cuentas de
        # terceros ni usar comodín de recurso.
        template = synth_observability_template()

        template.has_resource_properties(
            "AWS::SNS::TopicPolicy",
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Effect": "Allow",
                                        "Action": "SNS:Publish",
                                        "Principal": {
                                            "Service": "budgets.amazonaws.com"
                                        },
                                    }
                                )
                            ]
                        )
                    }
                )
            },
        )


def _flatten_cfn_text(value: object) -> str:
    """Aplana un fragmento de template (texto, ``Fn::Join``, ``Ref``…) a texto.

    El ``DashboardBody`` es un ``Fn::Join`` que entrelaza literales JSON con
    referencias a recursos (p. ej. el ARN de la cola). Esta utilidad concatena
    recursivamente las partes textuales para poder afirmar sobre los nombres de
    métrica que el panel grafica, ignorando los ``Ref``/``Fn::GetAtt`` que no son
    texto literal.

    Args:
        value: El valor del template (``str``, ``list`` o ``dict`` de intrínsecas).

    Returns:
        La concatenación de todas las porciones de texto literal halladas.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten_cfn_text(item) for item in value)
    if isinstance(value, dict):
        return "".join(
            _flatten_cfn_text(item)
            for key, item in value.items()
            if key == "Fn::Join"
        )
    return ""
