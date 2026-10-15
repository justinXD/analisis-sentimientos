#!/usr/bin/env python3
"""Punto de entrada de la Infraestructura (AWS CDK en Python).

Carga la configuración del entorno seleccionado por contexto
(``cdk synth -c env=dev|stage|prod``; por defecto ``dev``), instancia los nueve
stacks de dominio en el orden de dependencias del diseño (sección 14.4.2) y pasa
los recursos compartidos por *props* tipadas.

La cuenta y la región se resuelven de las credenciales del despliegue
(``CDK_DEFAULT_ACCOUNT`` / ``CDK_DEFAULT_REGION``), no de los archivos de config
(diseño 14.4.1). Si no están presentes (p. ej. en un ``cdk synth`` sin
credenciales en CI), se usa un *environment* no vinculado y la síntesis sigue
siendo válida.

Orden de dependencias (cada stack se despliega después de aquellos de los que
depende):

    auth, storage            -> (sin dependencias)
    data                     -> storage
    notifications            -> storage, data
    ingest                   -> storage
    compute                  -> storage, data, ingest, notifications
    workflow                 -> storage, data, ingest, notifications, compute
    api                      -> auth, storage, data, ingest, notifications
    observability            -> workflow, api, compute (lee nombres/ARN)
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from config.environments import load_env_config
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.compute_stack import ComputeStack
from stacks.data_stack import DataStack
from stacks.ingest_stack import IngestStack
from stacks.notifications_stack import NotificationsStack
from stacks.observability_stack import ObservabilityStack
from stacks.storage_stack import StorageStack
from stacks.workflow_stack import WorkflowStack


def _resolve_env() -> cdk.Environment:
    """Resuelve la cuenta y región del despliegue desde las credenciales.

    Returns:
        Un :class:`aws_cdk.Environment` con ``account``/``region`` tomados de
        ``CDK_DEFAULT_ACCOUNT`` / ``CDK_DEFAULT_REGION`` cuando están presentes.
        En su ausencia (p. ej. ``cdk synth`` en CI sin credenciales), los campos
        quedan como ``None`` y CDK sintetiza con un *environment* no vinculado.
    """

    return cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    )


def main() -> None:
    """Construye la app de CDK con los nueve stacks encadenados y la sintetiza."""

    app = cdk.App()

    # Entorno seleccionable por contexto: `-c env=dev|stage|prod`. Por defecto dev.
    env_name = str(app.node.try_get_context("env") or "dev")
    config = load_env_config(env_name)

    cdk_env = _resolve_env()
    prefix = f"{config.project}-{config.env_name}"

    # --- Base: stacks con estado, sin dependencias ---
    auth = AuthStack(app, f"{prefix}-auth", config=config, env=cdk_env)
    storage = StorageStack(app, f"{prefix}-storage", config=config, env=cdk_env)

    # --- Datos ---
    data = DataStack(
        app, f"{prefix}-data", config=config, storage=storage, env=cdk_env
    )

    # --- Notificación ---
    notifications = NotificationsStack(
        app,
        f"{prefix}-notifications",
        config=config,
        storage=storage,
        data=data,
        env=cdk_env,
    )

    # --- Buffer de ingesta ---
    ingest = IngestStack(
        app, f"{prefix}-ingest", config=config, storage=storage, env=cdk_env
    )

    # --- Procesamiento ---
    compute = ComputeStack(
        app,
        f"{prefix}-compute",
        config=config,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
        env=cdk_env,
    )

    # --- Orquestación ---
    workflow = WorkflowStack(
        app,
        f"{prefix}-workflow",
        config=config,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
        compute=compute,
        env=cdk_env,
    )

    # --- Exposición ---
    api = ApiStack(
        app,
        f"{prefix}-api",
        config=config,
        auth=auth,
        storage=storage,
        data=data,
        ingest=ingest,
        notifications=notifications,
        env=cdk_env,
    )

    # --- Operación (último del orden) ---
    ObservabilityStack(
        app,
        f"{prefix}-observability",
        config=config,
        workflow=workflow,
        api=api,
        compute=compute,
        env=cdk_env,
    )

    app.synth()


if __name__ == "__main__":
    main()
