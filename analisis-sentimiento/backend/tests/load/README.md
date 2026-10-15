# Pruebas de carga de concurrencia multi-tenant (tarea 15.4)

Estas pruebas lanzan **5 Lotes de 5 Tenants distintos de forma simultánea** contra
un stack **realmente desplegado** en `dev` (la API Gateway real, DynamoDB, S3 y
Step Functions). Verifican que **ningún ítem se pierde** y miden las **latencias
de `api_submit` y `api_status`** frente a sus objetivos iniciales.

Reutilizan el arnés de las pruebas de integración (tarea 15.2) en
`backend/tests/integration/_harness.py`: resolución de recursos por convención
`{project}-{env}-...`, cliente HTTP de la API, gate de salto y helpers
`make_items`/`poll_until_terminal`.

## Comportamiento por defecto: se SALTAN de forma limpia

Están marcadas con los markers `load` e `integration` y se **saltan** (`skip`)
cuando no hay stack desplegado, faltan credenciales o no se configuran los 5
Tenants. En un checkout o en el `Pipeline_CI` normal aparecen como `SKIPPED`,
nunca como `FAILED`, y no afectan a la recolección ni al resto de la suite:

```bash
cd backend && source .venv/bin/activate
python -m pytest -q        # la de carga sale SKIPPED
```

Las **funciones puras** de cálculo (percentiles, conservación de ítems) sí se
prueban siempre, sin red, en `test_load_metrics.py`.

## Qué verifica

| Verificación | Criterios |
|---|---|
| 5 Lotes de Tenants distintos simultáneos no pierden ítems (`done+failed+skipped == item_count`) | 5.8, 18.3 |
| Aislamiento entre Tenants: contadores/estado por `analysis_id`, sin `analysis_id` compartidos | 18.5 |
| `api_submit` p95 < 1 s (≤100 ítems), techo duro 29 s | 18.1 |
| `api_status` p95 < 300 ms | 18.9 |

Los objetivos de latencia son **iniciales y revisables** (diseño, sección 22.1).
Si la p95 medida los supera, la prueba **falla** con un mensaje claro. Para la
primera calibración existe un modo **report-only** que informa sin fallar.

## Cómo ejecutarlas contra un `dev` real

> **Seguridad:** aquí solo aparecen **nombres** de variables de entorno y
> *placeholders*. Nunca se escriben credenciales, tokens, endpoints ni ARNs
> reales en el repositorio (steering de seguridad).

Con el venv del backend activo y credenciales AWS del entorno `dev` en la cadena
estándar del SDK:

```bash
cd backend && source .venv/bin/activate

export AWS_REGION="<REGION>"
export INTEGRATION_STACK_ENV="dev"
export INTEGRATION_API_BASE_URL="<API_BASE_URL>"

# 5 Tenants distintos, cada uno con su token (scopes write/read; purge para la
# limpieza). Forma RECOMENDADA: pares tenant_id:token separados por comas.
export INTEGRATION_TENANT_TOKENS="t1:<TOKEN_1>,t2:<TOKEN_2>,t3:<TOKEN_3>,t4:<TOKEN_4>,t5:<TOKEN_5>"

# Opcional: primera calibración, informa sin fallar por los objetivos.
export INTEGRATION_LOAD_REPORT_ONLY="1"

python -m pytest -m load -q
```

### Variables de entorno

| Variable | Oblig. | Propósito |
|---|---|---|
| `AWS_REGION` / `AWS_DEFAULT_REGION` | sí | Región para boto3. |
| `INTEGRATION_STACK_ENV` | sí | Entorno desplegado (`dev`). |
| `INTEGRATION_API_BASE_URL` | sí | URL base de la API desplegada. |
| `INTEGRATION_TENANT_TOKENS` | sí* | Pares `tenant_id:token` (≥5), forma recomendada. |
| `INTEGRATION_TENANT_IDS` + `INTEGRATION_ACCESS_TOKEN` | sí* | Alternativa: lista de `tenant_id` (≥5) con un token compartido cuyo claim de Tenant sea parametrizable. |
| `INTEGRATION_PROJECT` | no | Prefijo del proyecto (por defecto `sentiment`). |
| `INTEGRATION_ACCOUNT_ID` | no | Id de cuenta; si falta, se resuelve con STS. |
| `INTEGRATION_LOAD_REPORT_ONLY` | no | `1`/`true`/`yes` → modo report-only (no falla por objetivos). |

(*) Se necesita **una** de las dos formas de identificar los 5 Tenants. Sin
ninguna, la prueba de carga se **salta**.

## Requisitos del token

- Scopes `sentiment/analyses.write` y `sentiment/analyses.read` por Tenant;
  además `sentiment/analyses.purge` para la limpieza automática (`purge_guard`).
- El claim `custom:tenant_id` de cada token debe ser **distinto** entre los 5
  Tenants (es lo que valida el aislamiento del criterio 18.5).

## Limpieza

Cada Lote creado se purga al final (fixture `purge_guard`), incluso si una
aserción falla, de modo que no quedan datos sintéticos ni objetos de S3 en `dev`.
Todos los textos enviados son sintéticos: nada de PII.
