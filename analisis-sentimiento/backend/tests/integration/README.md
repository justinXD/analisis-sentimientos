# Pruebas de integración del pipeline (tarea 15.2)

Estas pruebas ejercitan el Sistema **end-to-end contra un stack realmente
desplegado** en el entorno `dev` (API Gateway, DynamoDB, S3, Step Functions y la
Lambda `reconcile_stuck`). **No** usan AWS simulado: validan el pipeline real.

## Comportamiento por defecto: se SALTAN de forma limpia

Están marcadas con el marker `integration` y se **saltan** (`skip`) cuando no hay
stack desplegado ni credenciales. En un checkout o en el `Pipeline_CI` normal
aparecen como `SKIPPED`, nunca como `FAILED`, y no afectan a la recolección ni al
resultado del resto de la suite:

```bash
cd backend && source .venv/bin/activate
python -m pytest -q      # las de integración salen SKIPPED
```

El salto se activa cuando falta cualquiera de las variables de entorno
obligatorias (ver `_harness.py`). El patrón es:

```python
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_harness.SHOULD_SKIP, reason=_harness.SKIP_REASON),
]
```

## Escenarios cubiertos

| # | Escenario | Criterios |
|---|---|---|
| 1 | Camino feliz (termina y se descarga) | 5.9, 12.11 |
| 2 | Fallo parcial (SKIPPED + DONE, contadores conservados) | 5.9, 10.14 |
| 3 | Lote vacío (`items: []` → 400) | 3.7 |
| 4 | Lote de 1.000 ítems (tope del diseño) | 5.3, 3.6 |
| 5 | Expiración por el sweeper (`EXPIRED`) | 10.14, 10.11/10.12 |
| 6 | Purga: sin versiones remanentes en S3 | 19.3, 19.4 |

## Cómo ejecutarlas contra un `dev` real

> **Seguridad:** aquí solo aparecen **nombres** de variables de entorno y
> *placeholders*. Nunca se escriben credenciales, endpoints, ARNs de cuenta ni
> direcciones de correo reales en el repositorio (steering de seguridad).

Con el venv del backend activo y credenciales AWS del entorno `dev` en la cadena
estándar del SDK (perfil, variables `AWS_*`, rol asumido, etc.):

```bash
cd backend && source .venv/bin/activate

# Región del despliegue (boto3 la necesita).
export AWS_REGION="<REGION>"                 # p. ej. us-east-1

# Gate de ejecución y datos del stack dev.
export INTEGRATION_STACK_ENV="dev"
export INTEGRATION_API_BASE_URL="<API_BASE_URL>"   # https://<api-id>.execute-api.<region>.amazonaws.com/<stage>
export INTEGRATION_ACCESS_TOKEN="<ACCESS_TOKEN>"   # Bearer con scopes write/read (y purge)
export INTEGRATION_TENANT_ID="<TENANT_ID>"         # claim custom:tenant_id del token

# Opcionales.
export INTEGRATION_PROJECT="sentiment"             # por defecto: sentiment
export INTEGRATION_ACCOUNT_ID="<ACCOUNT_ID>"       # si falta, se resuelve con STS
export INTEGRATION_RECONCILE_FUNCTION_NAME="<NOMBRE_LAMBDA_RECONCILE_STUCK>"  # solo escenario 5

python -m pytest -m integration -q
```

### Resolución de recursos (por convención)

Los nombres de recurso se derivan de `{project}-{env}` (ver
`infraestructura/app.py` y la sección 6.5 del diseño), sin hardcodear ARNs:

- Dynamo_Table: `{project}-{env}-analysis`
- Bucket_Analisis: `{project}-{env}-analysis-{account_id}`
- Maquina_De_Estados: `{project}-{env}-analysis`

La Lambda `reconcile_stuck` **no** fija un `function_name` explícito (lo genera
CloudFormation), por lo que su nombre no es derivable por convención: el
escenario 5 lo toma de `INTEGRATION_RECONCILE_FUNCTION_NAME` y se **salta** si no
está definido. Puedes obtenerlo del stack `{project}-{env}-workflow`
(p. ej. `aws cloudformation list-stack-resources`).

## Requisitos del token

- Scopes `sentiment/analyses.write` y `sentiment/analyses.read` para todos los
  escenarios; además `sentiment/analyses.purge` para el escenario 6 y para la
  limpieza automática (`purge_guard`) del resto.
- El claim `custom:tenant_id` del token debe coincidir con `INTEGRATION_TENANT_ID`.

## Limpieza

Cada prueba purga los Lotes que crea (fixture `purge_guard`), de modo que no deja
datos sintéticos ni objetos de S3 en `dev`, incluso si la aserción falla. El
escenario de purga limpia por sí mismo. Nada de PII: todos los textos enviados
son sintéticos.
