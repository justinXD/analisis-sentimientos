"""Construct de la Lambda Layer de dependencias de terceros (diseño 14.3).

Empaqueta en una **única** Lambda Layer las dependencias de terceros compartidas
por las nueve Lambdas del backend (``aws-lambda-powertools``, ``pydantic``,
``jsonschema``, ``python-ulid``), tal y como fija el criterio 17.16. ``boto3`` /
``botocore`` **no** se incluyen: ya vienen en el runtime de Lambda y empaquetarlos
añadiría ~50 MB (diseño 14.3).

Estrategia de empaquetado (diseño 14.3)
---------------------------------------
La capa se construye con ``pip install --platform manylinux2014_aarch64
--only-binary=:all:`` para obtener *wheels* precompiladas de ``arm64`` (criterio
17.12/17.16), **sin necesidad de Docker durante el ``cdk synth``**. Para lograrlo
se usa el *bundling local* de CDK:

- Si el host tiene ``pip`` disponible (el caso del Pipeline_CI y del desarrollo),
  :meth:`_LocalPipBundler.try_bundle` instala las dependencias en el directorio
  de salida del *asset* y CDK **no** invoca Docker.
- La imagen Docker declarada en ``BundlingOptions.image`` es solo la red de
  seguridad para entornos sin ``pip`` en el ``PATH``; en CI nunca se alcanza.

El *hash* del *asset* se deriva del contenido de ``backend/requirements.txt``, de
modo que la capa solo se reconstruye cuando cambian las dependencias pineadas
(builds reproducibles, Requirement 17.11).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerVolume, ILocalBundling
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

# Runtime objetivo de todas las Lambdas del backend (diseño 14.4.4).
_RUNTIME = lambda_.Runtime.PYTHON_3_12
# Arquitectura objetivo (criterio 17.12): Graviton2 / arm64.
_ARCHITECTURE = lambda_.Architecture.ARM_64
# `pip` necesita estos valores para resolver wheels de arm64 / CPython 3.12 en un
# host que puede ser x86 (p. ej. el runner de CI o un portátil Intel).
_PIP_PLATFORM = "manylinux2014_aarch64"
_PY_VERSION = "3.12"
_PY_ABI = "cp312"
# Convención de las Lambda Layers de Python: las dependencias deben vivir bajo
# `python/` para quedar en el `sys.path` del runtime.
_LAYER_SITE_DIR = "python"

# Raíz del paquete `backend/` (…/infraestructura/constructs/ -> …/backend).
_BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
_REQUIREMENTS_FILE = _BACKEND_DIR / "requirements.txt"

# Rutas dentro del contenedor Docker (solo si se usa el fallback a Docker).
_CONTAINER_INPUT = "/asset-input"
_CONTAINER_OUTPUT = "/asset-output"


def _pip_install_command(requirements_file: str, output_dir: str) -> list[str]:
    """Construye el comando ``pip install`` para empaquetar la capa.

    Fuerza *wheels* precompiladas de ``arm64`` / CPython 3.12 (``--only-binary``
    + ``--platform`` + ``--python-version`` + ``--abi``), de modo que el host de
    *build* no necesita ser ``arm64`` ni compilar nada. Las dependencias se
    instalan bajo ``{output_dir}/python`` (convención de las Lambda Layers).

    Args:
        requirements_file: Ruta al ``requirements.txt`` a instalar.
        output_dir: Directorio de salida del *asset* de CDK.

    Returns:
        El comando como lista de argumentos (sin shell, para evitar inyección).
    """

    target = str(Path(output_dir) / _LAYER_SITE_DIR)
    return [
        "pip",
        "install",
        "--requirement",
        requirements_file,
        "--target",
        target,
        "--only-binary=:all:",
        "--platform",
        _PIP_PLATFORM,
        "--python-version",
        _PY_VERSION,
        "--implementation",
        "cp",
        "--abi",
        _PY_ABI,
        "--no-cache-dir",
    ]


@jsii.implements(ILocalBundling)
class _LocalPipBundler:
    """*Bundler* local que instala las dependencias con ``pip`` del host.

    Implementa el protocolo :class:`aws_cdk.ILocalBundling`. CDK llama a
    :meth:`try_bundle` **antes** de recurrir a Docker; si devuelve ``True``, el
    empaquetado se hace en el host y Docker nunca se invoca (requisito del diseño
    14.3: sin Docker en el ``synth``).
    """

    def __init__(self, requirements_file: Path) -> None:
        self._requirements_file = requirements_file

    def try_bundle(self, output_dir: str, *_options: Any, **_kwargs: Any) -> bool:
        """Intenta empaquetar la capa en el host usando ``pip``.

        jsii invoca este método con la ruta de salida y las opciones de *bundling*
        (``image``, ``command``…) como argumento posicional; se absorben en
        ``*_options`` porque el *bundling* local no las necesita.

        Args:
            output_dir: Directorio donde CDK espera el contenido del *asset*.
            _options: Opciones de *bundling* de CDK pasadas por jsii; sin uso aquí.
            _kwargs: Opciones de *bundling* pasadas por nombre; sin uso aquí.

        Returns:
            ``True`` si ``pip`` está disponible y la instalación tuvo éxito, de
            modo que CDK use el resultado local; ``False`` para que CDK recurra a
            la imagen Docker declarada en :class:`BundlingOptions`.
        """

        if shutil.which("pip") is None:
            return False

        command = _pip_install_command(str(self._requirements_file), output_dir)
        try:
            subprocess.run(command, check=True)  # noqa: S603 - args fijos, sin shell
        except (subprocess.CalledProcessError, OSError):
            # Si el build local falla (p. ej. sin red), dejamos que CDK pruebe con
            # Docker en lugar de romper el synth de forma opaca.
            return False
        return True


def _requirements_asset_hash(requirements_file: Path) -> str:
    """Calcula un *hash* estable del *asset* a partir del ``requirements.txt``.

    Ata el *hash* del *asset* al contenido de las dependencias pineadas (y no al
    árbol completo de ``backend/``), de modo que la capa solo se reconstruya
    cuando cambien las dependencias, manteniendo ``cdk synth`` determinista.

    Args:
        requirements_file: Ruta al ``requirements.txt`` de runtime.

    Returns:
        El *digest* SHA-256 hexadecimal del contenido del archivo.
    """

    return hashlib.sha256(requirements_file.read_bytes()).hexdigest()


class DependenciesLayer(Construct):
    """Lambda Layer con las dependencias de terceros del backend (criterio 17.16).

    Expone la capa resultante como :attr:`layer`, tipada con la interfaz
    :class:`aws_cdk.aws_lambda.ILayerVersion` para que los stacks consumidores
    (``compute``, ``api``…) la referencien sin acoplarse a la implementación.

    Args:
        scope: Construct padre.
        construct_id: Identificador lógico del construct.
        layer_name: Nombre físico de la capa (``{proyecto}-{env}-deps``).

    Raises:
        FileNotFoundError: Si no existe ``backend/requirements.txt``, de modo que
            un repositorio mal checkouteado rompa ``cdk synth`` de inmediato.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        layer_name: str,
    ) -> None:
        super().__init__(scope, construct_id)

        if not _REQUIREMENTS_FILE.is_file():
            raise FileNotFoundError(
                "No se encontró el requirements.txt de runtime del backend en "
                f"{_REQUIREMENTS_FILE}. La Lambda Layer no puede empaquetarse."
            )

        bundling = BundlingOptions(
            image=_RUNTIME.bundling_image,
            local=_LocalPipBundler(_REQUIREMENTS_FILE),
            # Comando usado SOLO si el bundling local no está disponible (sin pip
            # en el PATH). En CI el bundler local gana y Docker nunca se invoca.
            command=[
                "bash",
                "-c",
                " ".join(
                    _pip_install_command(
                        f"{_CONTAINER_INPUT}/requirements.txt", _CONTAINER_OUTPUT
                    )
                ),
            ],
            volumes=[
                DockerVolume(
                    container_path=_CONTAINER_INPUT,
                    host_path=str(_BACKEND_DIR),
                )
            ],
        )

        self._layer = lambda_.LayerVersion(
            self,
            "Layer",
            layer_version_name=layer_name,
            description=(
                "Dependencias de terceros compartidas por las Lambdas del backend "
                "(aws-lambda-powertools, pydantic, jsonschema, python-ulid). "
                "boto3/botocore no se incluyen: vienen en el runtime de Lambda."
            ),
            compatible_runtimes=[_RUNTIME],
            compatible_architectures=[_ARCHITECTURE],
            code=lambda_.Code.from_asset(
                str(_BACKEND_DIR),
                asset_hash_type=AssetHashType.CUSTOM,
                asset_hash=_requirements_asset_hash(_REQUIREMENTS_FILE),
                bundling=bundling,
            ),
        )

    @property
    def layer(self) -> lambda_.ILayerVersion:
        """La Lambda Layer de dependencias, tipada como ``ILayerVersion``."""

        return self._layer
