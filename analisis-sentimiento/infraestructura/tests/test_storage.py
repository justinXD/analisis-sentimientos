"""Pruebas_De_Infraestructura del almacenamiento (criterio 17.9, tarea 11.4).

Afirman sobre el template de CloudFormation sintetizado del ``StorageStack`` que
el ``Bucket_Analisis`` se crea con las garantías de seguridad del diseño 12.3:

- cifrado en reposo con ``SSE-KMS`` (criterios 15.6 / 17.9),
- acceso público totalmente bloqueado (criterios 15.8 / 17.9),
- versionado de objetos habilitado (criterio 16.7),

y que la CMK del proyecto tiene habilitada la rotación automática (criterio 15.14).

Son assertions declarativas con :class:`aws_cdk.assertions.Template`
(``has_resource_properties`` + ``Match``), de modo que no dependen del
identificador lógico que CDK asigne al recurso.
"""

from __future__ import annotations

from aws_cdk import assertions
from aws_cdk.assertions import Match

from tests._harness import synth_storage_template


class TestBucketSecurity:
    """El Bucket_Analisis se sintetiza cifrado, privado y versionado (17.9/16.7)."""

    def test_bucket_blocks_all_public_access(self) -> None:
        # Las cuatro palancas de BlockPublicAccess deben estar activas (15.8).
        template = synth_storage_template()

        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                }
            },
        )

    def test_bucket_is_encrypted_with_kms(self) -> None:
        # SSE-KMS presente: la regla de cifrado por defecto usa `aws:kms`
        # (criterio 15.6 / 17.9). No se fija el ARN de la clave para no acoplar
        # con el identificador lógico de la CMK.
        template = synth_storage_template()

        template.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketEncryption": Match.object_like(
                    {
                        "ServerSideEncryptionConfiguration": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "ServerSideEncryptionByDefault": (
                                            Match.object_like(
                                                {"SSEAlgorithm": "aws:kms"}
                                            )
                                        )
                                    }
                                )
                            ]
                        )
                    }
                )
            },
        )

    def test_bucket_has_versioning_enabled(self) -> None:
        # Versionado habilitado: condición de la purga reversible (criterio 16.7).
        template = synth_storage_template()

        template.has_resource_properties(
            "AWS::S3::Bucket",
            {"VersioningConfiguration": {"Status": "Enabled"}},
        )

    def test_single_bucket_is_defined(self) -> None:
        # El stack define exactamente un bucket (el de análisis); si apareciera
        # otro sin estas garantías, las assertions anteriores podrían dar un falso
        # positivo al encontrar el bucket correcto.
        template = synth_storage_template()

        template.resource_count_is("AWS::S3::Bucket", 1)


class TestCmkRotation:
    """La CMK del proyecto rota automáticamente (criterio 15.14)."""

    def test_key_has_rotation_enabled(self) -> None:
        template = synth_storage_template()

        template.has_resource_properties(
            "AWS::KMS::Key",
            {"EnableKeyRotation": True},
        )


class TestNoPlaintextSecretsInTemplate:
    """El template no debe embeber ningún valor secreto (criterio 17.20)."""

    def test_template_has_no_obvious_secret_markers(self) -> None:
        # Marcadores de secreto evidentes que nunca deben aparecer embebidos en el
        # template sintetizado del almacenamiento. Es una red de seguridad barata,
        # no un escáner exhaustivo.
        template_json = assertions.Template.to_json(synth_storage_template())
        haystack = str(template_json).lower()

        for marker in ("client_secret", "aws_secret_access_key", "private_key"):
            assert marker not in haystack, (
                f"posible secreto embebido en el template: {marker!r}"
            )
