# serverless-sentiment-pipeline

Pipeline serverless, asíncrono y multi-tenant para el análisis de sentimiento de
respuestas de encuestas de clima laboral sobre AWS. Un cliente sube un lote de
respuestas (un array JSON), recibe de inmediato un `analysis_id`, y el lote se
procesa en paralelo de forma tolerante a fallos. El cliente consulta el estado
por un endpoint `GET` y descarga el resultado consolidado mediante una URL
prefirmada de corta duración; opcionalmente recibe una notificación push
(evento de EventBridge, correo con SES o webhook firmado con HMAC) al alcanzar
un estado terminal.

Para el detalle de arquitectura, componentes y flujos, ver
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). La especificación completa
(requisitos y diseño) vive en `.kiro/specs/serverless-sentiment-pipeline/`.

## Tabla de contenidos

- [Qué hace](#qué-hace)
- [Arquitectura en una frase](#arquitectura-en-una-frase)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Requisitos previos](#requisitos-previos)
- [Puesta en marcha local](#puesta-en-marcha-local)
- [Pruebas y calidad](#pruebas-y-calidad)
- [Despliegue](#despliegue)
- [Variables de entorno y configuración](#variables-de-entorno-y-configuración)
- [Seguridad](#seguridad)
- [Limitaciones conocidas](#limitaciones-conocidas)

## Qué hace

1. **Ingesta** (`POST /analyses`): valida el lote completo contra un JSON Schema
   estricto (hasta 1.000 ítems, 5 KB por respuesta), lo persiste en S3 y
   DynamoDB, lo encola y responde `202 Accepted` con el `analysis_id`.
2. **Orquestación**: una ejecución de Step Functions por lote, con un estado
   Distributed Map que itera el array desde S3 y hace fan-out por ítem.
3. **Procesamiento**: Amazon Comprehend detecta idioma y sentimiento en lotes de
   25 documentos; Amazon Bedrock (Nova) hace la clasificación fina de emociones
   por ítem.
4. **Agregación**: se consolida el resultado en S3, se cierra el lote en
   DynamoDB (`COMPLETED` / `COMPLETED_WITH_ERRORS` / `FAILED`) y se publican las
   notificaciones.
5. **Consumo**: `GET /analyses/{analysis_id}` para el estado y
   `GET /analyses/{analysis_id}/download` para una URL prefirmada de 15 minutos.
6. **Supresión**: `DELETE /analyses/{analysis_id}` (scope de purga) elimina
   ítems, objetos de S3 y, por último, el registro META.

## Arquitectura en una frase

Serverless event-driven con capas hexagonales dentro de cada Lambda
(`handlers` → `services` → `adapters`), orquestación por lote con Step Functions
+ Distributed Map, y DynamoDB single-table como única fuente de verdad del
estado. El diseño completo, con los diagramas de componentes y de secuencia,
está en [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Estructura del repositorio

```
.
├── backend/              # Código de aplicación (Python 3.12)
│   └── src/sentiment/
│       ├── domain/       # Modelo de dominio puro, errores y puertos (sin AWS)
│       ├── services/     # Casos de uso (lógica de negocio, deps inyectadas)
│       ├── adapters/     # Implementaciones de los puertos (SDK de AWS)
│       ├── handlers/     # Adaptadores de transporte thin (API GW, SFN, SQS)
│       ├── prompts/      # system_prompt, catálogo e Indificadores versionados
│       ├── schemas/      # JSON Schemas de entrada y salida
│       └── observability/# Logging con redacción de PII, métricas, config
├── infraestructura/      # AWS CDK en Python (stacks por dominio)
├── docs/                 # ARCHITECTURE.md (+ API.md y RUNBOOK.md futuros)
└── .kiro/                # Especificación (requirements, design, tasks)
```

El código de aplicación (`backend/`) y la infraestructura como código
(`infraestructura/`) están deliberadamente separados. Cada Lambda se empaqueta
desde `backend/src/`.

## Requisitos previos

- **Python 3.12** (las Lambdas corren en `arm64`).
- **Node.js** para el AWS CDK CLI (`npm install -g aws-cdk`).
- **Credenciales de AWS** configuradas con permisos de despliegue para la cuenta
  y región objetivo. No se crean recursos manualmente por consola: todo recurso
  se gestiona por el despliegue de la Infraestructura.

## Puesta en marcha local

El núcleo de dominio es importable y testeable sin AWS. Para trabajar en el
backend:

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Para la infraestructura:

```bash
cd infraestructura
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Pruebas y calidad

Las comprobaciones de calidad son las que ejecuta el Pipeline_CI en cada pull
request. Desde `backend/`:

```bash
ruff check .          # lint; prohíbe `print` (regla T201)
mypy                  # verificación estática de tipos (--strict)
pytest                # pruebas unitarias, de contrato y de propiedades
```

Desde `infraestructura/`:

```bash
cdk synth             # sintetiza el template; falla el CI si falla aquí
pytest                # Pruebas_De_Infraestructura sobre el template
```

Las pruebas usan exclusivamente datos sintéticos y simulan AWS con `moto` para
las pruebas de contrato. No se emplean mocks para simular lógica de negocio.

## Despliegue

El despliegue se parametriza por entorno (`dev`, `stage`, `prod`) y no contiene
ningún valor secreto en el código ni en los archivos de configuración.

```bash
cd infraestructura

# Primera vez por cuenta/región:
cdk bootstrap

# Sintetizar y revisar el diff antes de aplicar:
cdk synth   --context env=dev
cdk diff    --context env=dev

# Desplegar todos los stacks del entorno:
cdk deploy --all --context env=dev
```

Sustituye `dev` por `stage` o `prod` según el entorno objetivo. El archivo
`cdk.context.json` se versiona (es necesario para la reproducibilidad de la
síntesis entre entornos); los artefactos de síntesis (`cdk.out/`) no.

## Variables de entorno y configuración

La configuración de las Lambdas se inyecta en tiempo de despliegue y se valida
en el arranque (fallo explícito si falta algo). Los secretos nunca se leen de
archivos versionados: provienen de AWS Secrets Manager y de SSM Parameter Store.
Nombres relevantes (valores reales nunca se versionan):

| Nombre | Origen | Propósito |
|---|---|---|
| `MODEL_ID` | SSM Parameter Store | Identificador del modelo de Bedrock a invocar. |
| `TABLE_NAME` | Variable de entorno (CDK) | Nombre de la tabla DynamoDB single-table. |
| `BUCKET_NAME` | Variable de entorno (CDK) | Nombre del Bucket_Analisis. |
| Secreto HMAC del webhook | Secrets Manager | Firma `HMAC-SHA256` de las entregas a webhooks. |
| Secreto M2M | Secrets Manager | Credenciales `client_credentials` de Cognito. |

Los secretos de desarrollo local se cargan desde un `.env` que **no** se
versiona (ver `.gitignore`). Nunca se comprometen credenciales reales al
control de versiones.

## Seguridad

- Autenticación OAuth2 `client_credentials` con Cognito y autorizador JWT en
  API Gateway (verifica firma, `iss`, `aud`/`client_id`, `exp` y scope por ruta).
- Aislamiento por tenant aplicado en la capa de datos (no por oscuridad del ID).
- Cifrado en reposo con CMK propia (DynamoDB y S3) y TLS obligatorio en tránsito.
- IAM de mínimo privilegio sobre ARN concretos, sin comodines sobre datos.
- Logging estructurado con redacción de PII; nunca se registran respuestas,
  explicaciones ni correos.

## Limitaciones conocidas

- La idempotencia de ingesta por encabezado `Idempotency-Key` está diferida: un
  reenvío de la misma solicitud crea un lote nuevo, acotado por la cuota de 5
  lotes en vuelo por tenant.
- Point-In-Time Recovery de DynamoDB conserva copias hasta 35 días, un riesgo
  residual frente a la purga (derecho al olvido).
- Los valores de concurrencia de Bedrock son iniciales y están sujetos a
  calibración con la latencia real del modelo.
