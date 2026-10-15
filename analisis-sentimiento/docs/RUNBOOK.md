# Runbook operativo: serverless-sentiment-pipeline

Guía de operación del pipeline para la guardia (on-call). Documenta cada alarma
del catálogo (las 4 críticas y las no críticas), qué significa, su causa probable,
cómo diagnosticarla y cómo mitigarla; además de las variables de entorno
operativas y cómo ejecutar las pruebas de integración y de carga contra `dev`.

La vista de arquitectura está en [`ARCHITECTURE.md`](./ARCHITECTURE.md) y el
contrato de la API en [`API.md`](./API.md). El catálogo de alarmas se implementa en
`infraestructura/stacks/observability_stack.py` (tareas 13.5 y 13.6).

> **Seguridad.** Este runbook usa solo **nombres** de recursos, métricas y
> variables de entorno, con *placeholders* (`<REGION>`, `<ACCOUNT_ID>`, …): nunca
> credenciales, endpoints, ARNs, direcciones de correo ni importes reales. No
> copies PII (`respuesta`, `explicacion`, correos) a un ticket ni a un chat; usa
> siempre el `correlation_id` o el `analysis_id` para correlacionar.

## Tabla de contenidos

- [1. Mapa operativo de un vistazo](#1-mapa-operativo-de-un-vistazo)
- [2. Notificación y panel](#2-notificación-y-panel)
- [3. Convención de nombres de recursos](#3-convención-de-nombres-de-recursos)
- [4. Alarmas críticas](#4-alarmas-críticas)
  - [4.1 DLQ_Solicitudes con mensajes](#41-dlq_solicitudes-con-mensajes)
  - [4.2 Ejecuciones de Step Functions fallidas](#42-ejecuciones-de-step-functions-fallidas)
  - [4.3 AccessDenied en una Lambda](#43-accessdenied-en-una-lambda)
  - [4.4 Lotes atascados (StuckAnalyses)](#44-lotes-atascados-stuckanalyses)
- [5. Alarmas no críticas (alta/media)](#5-alarmas-no-críticas-altamedia)
  - [5.1 Tasa de error de Lambda > 5 %](#51-tasa-de-error-de-lambda--5-)
  - [5.2 Throttling de Bedrock > 50 / 5 min](#52-throttling-de-bedrock--50--5-min)
  - [5.3 Antigüedad de la Cola_Solicitudes > 300 s](#53-antigüedad-de-la-cola_solicitudes--300-s)
  - [5.4 Bounce rate de SES > 5 %](#54-bounce-rate-de-ses--5-)
  - [5.5 Ítems fallidos (ItemsFailed)](#55-ítems-fallidos-itemsfailed)
  - [5.6 Lambda throttled](#56-lambda-throttled)
  - [5.7 Latencia p99 de api_submit > 5 s](#57-latencia-p99-de-api_submit--5-s)
  - [5.8 Presupuesto diario superado](#58-presupuesto-diario-superado)
- [6. El sweeper `reconcile_stuck`](#6-el-sweeper-reconcile_stuck)
- [7. Variables de entorno operativas](#7-variables-de-entorno-operativas)
- [8. Pruebas de integración y de carga contra dev](#8-pruebas-de-integración-y-de-carga-contra-dev)

## 1. Mapa operativo de un vistazo

| Alarma | Severidad | Señal / umbral | Sección |
|---|---|---|---|
| DLQ_Solicitudes con mensajes | Crítica | `ApproximateNumberOfMessagesVisible` (Max, 5 min) > 0 | [4.1](#41-dlq_solicitudes-con-mensajes) |
| Ejecuciones de Step Functions fallidas | Crítica | `ExecutionsFailed` (Sum, 5 min) ≥ 1 | [4.2](#42-ejecuciones-de-step-functions-fallidas) |
| AccessDenied en una Lambda | Crítica | `AccessDeniedErrors` (Sum, 5 min) ≥ 1 | [4.3](#43-accessdenied-en-una-lambda) |
| Lotes atascados | Crítica | `StuckAnalyses` (Sum, 5 min) ≥ 1 | [4.4](#44-lotes-atascados-stuckanalyses) |
| Tasa de error de Lambda | Alta | `Errors/Invocations` (5 min) > 5 % | [5.1](#51-tasa-de-error-de-lambda--5-) |
| Throttling de Bedrock | Alta | `BedrockThrottles` (Sum, 5 min) > 50 | [5.2](#52-throttling-de-bedrock--50--5-min) |
| Cola_Solicitudes antigua | Alta | `ApproximateAgeOfOldestMessage` (Max, 5 min) > 300 s | [5.3](#53-antigüedad-de-la-cola_solicitudes--300-s) |
| Bounce rate de SES | Alta | `Reputation.BounceRate` (Avg) > 5 % | [5.4](#54-bounce-rate-de-ses--5-) |
| Ítems fallidos | Media | `ItemsFailed` (Sum, 5 min) > 10 | [5.5](#55-ítems-fallidos-itemsfailed) |
| Lambda throttled | Media | `Throttles` (Sum, 5 min) ≥ 1 | [5.6](#56-lambda-throttled) |
| Latencia p99 de api_submit | Media | `Duration` p99 (5 min) > 5.000 ms | [5.7](#57-latencia-p99-de-api_submit--5-s) |
| Presupuesto diario | Alta | AWS Budget `COST`/`DAILY` ≥ 100 % | [5.8](#58-presupuesto-diario-superado) |

## 2. Notificación y panel

- **Topic de guardia**: todas las alarmas (críticas y no críticas) notifican al
  mismo topic SNS `{project}-{env}-oncall-critical`. Las alarmas críticas también
  notifican el regreso a OK, de modo que la guardia vea la apertura y el cierre del
  incidente. La suscripción (correo/canal) se conecta **fuera de banda**: no hay
  ningún endpoint de contacto en el código.
- **Panel**: el *dashboard* de CloudWatch `{project}-{env}-pipeline` agrupa las
  métricas por fase (ingesta, procesamiento, salud terminal). Es el primer sitio
  donde mirar al recibir una alarma para situar el incidente en su fase.
- **Trazas**: X-Ray está activo en las Lambdas y en la Maquina_De_Estados; una
  traza de extremo a extremo ayuda a localizar la dependencia que falla.
- **Logs**: estructurados (JSON), con `correlation_id` propagado de extremo a
  extremo. Nunca contienen PII (redacción declarativa en
  `observability/logging.py`). El canal de auditoría de purgas vive en el log group
  dedicado `/sentiment/{env}/audit` (retención 365 días).

## 3. Convención de nombres de recursos

Los nombres se derivan de `{project}-{env}` (p. ej. `sentiment-dev-...`):

| Recurso | Nombre |
|---|---|
| Dynamo_Table | `{project}-{env}-analysis` |
| Bucket_Analisis | `{project}-{env}-analysis-{account_id}` |
| Maquina_De_Estados | `{project}-{env}-analysis` (el nombre de ejecución **es** el `analysis_id`) |
| Bus_Eventos | `{project}-{env}-events` |
| DLQ_Notificaciones | `{project}-{env}-notifications-dlq` |
| Topic de guardia | `{project}-{env}-oncall-critical` |
| Topic de rebotes/quejas SES | `{project}-{env}-ses-bounces-complaints` |

La Cola_Solicitudes y su DLQ (stack `ingest`) y la Lambda `reconcile_stuck`
(stack `workflow`, nombre generado por CloudFormation) no se derivan por convención
directa; obtén sus nombres con
`aws cloudformation list-stack-resources --stack-name {project}-{env}-ingest` y
`... --stack-name {project}-{env}-workflow`.

## 4. Alarmas críticas

Las cuatro usan ventana de 5 minutos, un periodo de evaluación, umbral > 0
(disparan con ≥ 1) y `TreatMissingData = NOT_BREACHING` (sin muestras no es
incidente). Una crítica implica intervención.

### 4.1 DLQ_Solicitudes con mensajes

- **Construct**: `DlqRequestsMessages`. **Métrica**:
  `ApproximateNumberOfMessagesVisible` (Max, 5 min) de la DLQ de la
  Cola_Solicitudes (`ingest.requests_dlq`).
- **Qué significa**: el arranque de la orquestación de un Lote falló de forma
  persistente. SQS reintentó la entrega hasta `maxReceiveCount` (3 recepciones) y
  movió el mensaje a la DLQ. El Lote quedó en `PENDING` sin ejecución; el sweeper
  acabará expirándolo si no se interviene.
- **Causa probable**: EventBridge Pipe deshabilitado o sin permisos; la
  Maquina_De_Estados no desplegada o sin permiso de `StartExecution`; payload del
  mensaje corrupto; cuota/throttling de `StartExecution`.
- **Diagnóstico**:
  1. Mira el panel (fila *ingesta*) y la antigüedad del mensaje (ver [5.3](#53-antigüedad-de-la-cola_solicitudes--300-s)).
  2. Inspecciona la DLQ: `aws sqs receive-message --queue-url <DLQ_URL> --max-number-of-messages 1 --visibility-timeout 0` (no borres; el cuerpo lleva solo punteros, sin PII). Extrae `analysis_id` y `correlation_id`.
  3. Revisa el estado del Pipe (`requests_pipe`) en la consola de EventBridge Pipes: ¿está `RUNNING`? ¿tiene errores de ejecución?
  4. Busca `AccessDenied` en el log del Pipe/máquina; si aparece, salta a [4.3](#43-accessdenied-en-una-lambda).
- **Mitigación**:
  1. Si el Pipe estaba deshabilitado/atascado: reactívalo; verifica su rol IAM (consumo de la cola + `states:StartExecution` sobre la máquina concreta).
  2. Si el fallo era transitorio y ya está resuelto: haz *redrive* de la DLQ a la cola de origen (`aws sqs start-message-move-task` desde la DLQ) para reprocesar los Lotes.
  3. Si el mensaje está corrupto (no reprocesable): registra `analysis_id`/`correlation_id`, elimínalo de la DLQ y deja que el sweeper cierre el Lote como `EXPIRED` (sus Ítems quedan `FAILED`/`EXECUTION_ABANDONED`).
- **No reintentar a ciegas**: un *redrive* mientras la causa raíz sigue viva solo
  vuelve a llenar la DLQ. Corrige primero.

### 4.2 Ejecuciones de Step Functions fallidas

- **Construct**: `StepFunctionsExecutionsFailed`. **Métrica**: `ExecutionsFailed`
  (Sum, 5 min) de la Maquina_De_Estados.
- **Qué significa**: al menos una ejecución terminó en `FAILED`. El Lote cerró como
  `FAILED` y publicó su Evento_Finalizacion (el pipeline degrada de forma
  controlada; no es un cuelgue). Hay que entender *por qué*.
- **Causa probable**: fallo al leer `inputs/` o `item_count` incoherente
  (`INPUT_READ_FAILED`); agregación agotó sus reintentos (`AGGREGATION_FAILED`);
  error de plataforma de la máquina.
- **Diagnóstico**:
  1. En la consola de Step Functions, abre la ejecución fallida (su **nombre es el
     `analysis_id`**). Mira el estado que falló en el grafo (`MarkFailed`,
     `Aggregate`, `ProcessItems`/`ItemReader`).
  2. Revisa la causa del `Catch` y los logs de la Lambda implicada por su
     `correlation_id` (los logs no llevan datos de ejecución ni PII).
  3. Consulta `GET /v1/analyses/{analysis_id}` (o la Dynamo_Table) para ver
     contadores y el `error_code` del META.
- **Mitigación**:
  1. `INPUT_READ_FAILED`: comprueba que el objeto `inputs/{tenant}/.../{analysis_id}.json` exista y que `item_count` del META coincida con el array; si fue un fallo transitorio de S3, el Lote puede reenviarse por la API.
  2. `AGGREGATION_FAILED`: revisa el log de `aggregate_results` (permisos sobre la tabla/bucket, tamaño del Lote). Tras corregir, el sweeper no reabre un terminal; para reprocesar hay que reenviar el Lote.
  3. Si es un defecto de permisos: ver [4.3](#43-accessdenied-en-una-lambda).

### 4.3 AccessDenied en una Lambda

- **Construct**: `AccessDeniedInAnyLambda`. **Métrica**: `AccessDeniedErrors`
  (namespace `SentimentPipeline`, Sum, 5 min), derivada por *metric filter* del
  término `AccessDenied` en los log groups de las Lambdas de `compute`.
- **Qué significa**: una política IAM está mal cableada. Es un **defecto de
  despliegue**, no un error de datos: de ahí la severidad crítica. Normalmente
  aparece justo tras un despliegue.
- **Causa probable**: un cambio de IaC retiró o acotó de más un permiso; una CMK/
  recurso renombrado; una `key policy` o *resource policy* que ya no autoriza al
  rol.
- **Diagnóstico**:
  1. Busca `AccessDenied` en los logs de `analyze_comprehend`, `analyze_llm` y
     `aggregate_results` (y en `api`/`workflow` si aplica): el mensaje identifica la
     acción y el recurso denegados.
  2. Correlaciona con el último despliegue (CloudFormation/CDK) por tiempo.
- **Mitigación**:
  1. Corrige la Politica_IAM en IaC (acción + ARN concreto; respeta mínimo
     privilegio, nunca `Resource: "*"` en acciones de datos) y redespliega.
  2. Si bloquea producción, considera revertir al último despliegue bueno conocido.
  3. Verifica que `tests/test_iam_policies.py` siga en verde tras el cambio.

### 4.4 Lotes atascados (StuckAnalyses)

- **Construct**: `StuckAnalyses`. **Métrica**: `StuckAnalyses` (namespace
  `SentimentPipeline`, Sum, 5 min), que publica el sweeper `reconcile_stuck` en cada
  pasada (incluso con valor 0).
- **Qué significa**: el sweeper cerró como `EXPIRED` uno o más Lotes abandonados
  (no terminales, sin avance ≥ 15 min **y** sin ejecución en curso). Es la red de
  seguridad de "terminación garantizada" actuando: el dato no se pierde, pero algo
  impidió el procesamiento normal.
- **Causa probable**: suele acompañar a [4.1](#41-dlq_solicitudes-con-mensajes)
  (Lotes nunca arrancados) o a una degradación prolongada de Bedrock/Comprehend.
- **Diagnóstico**:
  1. Revisa en los logs de `reconcile_stuck` el `ReconcileReport`
     (`candidates_examined`/`expired`/`events_republished`/`skipped`) de la pasada.
  2. Para cada `analysis_id` expirado, consulta su META: ¿tenía ejecución
     (`DescribeExecution`)? ¿`error_code = EXECUTION_ABANDONED` en sus Ítems?
  3. Correlaciona con las alarmas de DLQ, antigüedad de cola y throttling de
     Bedrock del mismo periodo.
- **Mitigación**: ataca la causa raíz subyacente (DLQ, Pipe, Bedrock). Los Lotes ya
  expirados no se reabren; comunícalo al Tenant si procede y, en su caso, reenvía el
  Lote por la API. Ver [sección 6](#6-el-sweeper-reconcile_stuck).

## 5. Alarmas no críticas (alta/media)

Todas notifican al mismo topic de guardia y usan ventana de 5 minutos.

### 5.1 Tasa de error de Lambda > 5 %

- **Construct**: `LambdaErrorRate{Función}` (una por Lambda del camino crítico: las
  tres de `compute` más `api_submit`). **Expresión**: `Errors / Invocations` > 0,05.
- **Significado / causa**: la función se degrada (dependencia caída, bug reciente,
  entrada inesperada). "Sin datos" = sin invocaciones = no incidente.
- **Diagnóstico**: identifica la función del nombre de la alarma; revisa sus logs
  por `correlation_id` y la traza X-Ray. ¿Correlaciona con un despliegue o con una
  dependencia (Bedrock/Comprehend/DynamoDB/S3)?
- **Mitigación**: si es un despliegue reciente, revierte; si es una dependencia,
  escala la mitigación de esa dependencia; si es un bug, parchea y redespliega.

### 5.2 Throttling de Bedrock > 50 / 5 min

- **Construct**: `BedrockThrottles`. **Métrica**: `BedrockThrottles` (EMF, Sum,
  5 min) > 50.
- **Significado / causa**: la cuota del Modelo_Bedrock está comprometida y el
  *backoff* de la Maquina_De_Estados ya no la absorbe de forma transparente. Suele
  darse con Lotes grandes o varios Lotes concurrentes.
- **Diagnóstico**: panel (fila *procesamiento*): `BedrockThrottles` y tokens
  (`BedrockInputTokens`/`BedrockOutputTokens`). Revisa logs de `analyze_llm`.
- **Mitigación**:
  1. Baja temporalmente la concurrencia del Distributed_Map (`MAX_CONCURRENCY`) y
     del fan-out por Ítem para reducir la tasa de llamadas al modelo.
  2. Solicita aumento de cuota de Bedrock para el modelo/region si es recurrente.
  3. Revisa la calibración de concurrencia (ver `scripts/calibrate_bedrock.py`,
     tarea 15.3) frente a la latencia real del modelo.

### 5.3 Antigüedad de la Cola_Solicitudes > 300 s

- **Construct**: `RequestsOldestMessageAge`. **Métrica**:
  `ApproximateAgeOfOldestMessage` (Max, 5 min) de la Cola_Solicitudes > 300 s.
- **Significado / causa**: hay mensajes sin consumir; el EventBridge Pipe está
  deshabilitado o atascado. Avisa ~10 min **antes** de que el sweeper empiece a
  expirar Lotes, dando margen a reactivar el consumo sin perder trabajo.
- **Diagnóstico**: estado del Pipe `requests_pipe` (¿`RUNNING`?); profundidad de la
  cola; logs del Pipe por `AccessDenied`.
- **Mitigación**: reactiva/corrige el Pipe. Si ya está consumiendo, la antigüedad
  bajará sola; si no, salta a [4.1](#41-dlq_solicitudes-con-mensajes).

### 5.4 Bounce rate de SES > 5 %

- **Construct**: `SesBounceRate`. **Métrica**: `AWS/SES`
  `Reputation.BounceRate` (nivel de cuenta, Avg) > 5 %.
- **Significado / causa**: la reputación de envío cae; AWS puede llegar a pausar la
  cuenta. Normalmente por direcciones `notify_email` inválidas o buzones llenos. No
  afecta al estado del Lote (SES es mejor esfuerzo; la verdad vive en el META).
- **Diagnóstico**: suscríbete/consulta el topic `{project}-{env}-ses-bounces-complaints`
  (destino del *configuration set*) para ver qué direcciones rebotan.
- **Mitigación**:
  1. Suprime las direcciones que rebotan de forma dura (lista de supresión de SES).
  2. Comunica a los Tenants afectados que corrijan su `notify_email`.
  3. Recuerda: el cliente siempre puede consultar el estado por la API aunque el
     correo falle.

### 5.5 Ítems fallidos (ItemsFailed)

- **Construct**: `ItemsFailed`. **Métrica**: `ItemsFailed` (EMF, Sum, 5 min) > 10.
- **Nota de diseño (desviación documentada, criterio 14.11)**: el criterio liga el
  umbral al **5 % del `item_count` de cada Lote**, un porcentaje por Lote que
  CloudWatch no puede computar sobre una métrica agregada por ventana. Se aproxima
  con un **umbral absoluto por ventana** (> 10) que capta un repunte anómalo de
  fallos a nivel de Sistema. El cómputo porcentual exacto corresponde al backend.
- **Significado / causa**: repunte de Ítems que terminan en `FAILED`
  (`LLM_OUTPUT_INVALID`, `COMPREHEND_ITEM_FAILED`, `LLM_INPUT_REJECTED`, …).
- **Diagnóstico**: usa `GET /v1/analyses/{analysis_id}?include_failures=true` sobre
  los Lotes recientes para ver los `error_code` dominantes; revisa logs de
  `analyze_llm`/`analyze_comprehend`.
- **Mitigación**: según el `error_code` dominante — degradación del modelo, cambio
  en el prompt/esquema de salida, o entradas problemáticas. Un repunte de
  `LLM_OUTPUT_INVALID` sugiere revisar el contrato del prompt/esquema.

### 5.6 Lambda throttled

- **Construct**: `LambdaThrottled{Función}` (una por Lambda del camino crítico).
  **Métrica**: `Throttles` (Sum, 5 min) ≥ 1.
- **Significado / causa**: se agotó la **concurrencia reservada** de la función y
  una invocación fue rechazada. No es un error de datos: se corrige con
  concurrencia.
- **Mitigación**: sube la concurrencia reservada de la función implicada en IaC (y
  revisa el límite de concurrencia de la cuenta). Si es `analyze_llm`, coordina con
  la cuota de Bedrock para no limitar simplemente para throttlear aguas abajo.

### 5.7 Latencia p99 de api_submit > 5 s

- **Construct**: `ApiSubmitLatencyP99`. **Métrica**: `Duration` p99 (5 min) >
  5.000 ms.
- **Significado / causa**: la cola de latencia del envío se degrada (cold starts,
  Lotes de 1.000 Ítems con muchas escrituras a DynamoDB/S3, dependencia lenta).
- **Diagnóstico**: traza X-Ray de `api_submit`; ¿cold starts frecuentes?; ¿tamaño de
  los Lotes?; latencia de S3/DynamoDB.
- **Mitigación**: revisa la concurrencia reservada de `api_submit`; considera
  provisioned concurrency si los cold starts dominan; verifica que las escrituras
  por `BatchWriteItem` no estén reintentando en exceso.

### 5.8 Presupuesto diario superado

- **Construct**: `daily_budget` (AWS Budgets, `COST`/`DAILY`). **Umbral**: gasto
  **real** (`ACTUAL`) ≥ 100 % del límite diario. El importe del Repositorio es un
  **marcador de proyecto**; el límite real por entorno se ajusta fuera de banda.
- **Significado / causa**: el gasto diario alcanzó el presupuesto. Es la red de
  seguridad frente a un gasto desbocado, típicamente de Bedrock.
- **Diagnóstico**: Cost Explorer por servicio; correlaciona con el volumen de Lotes
  y los tokens de Bedrock del panel.
- **Mitigación**: identifica el origen del gasto; si es un abuso o un bucle,
  reduce concurrencia/pausa la ingesta; ajusta el presupuesto solo si el gasto es
  legítimo y esperado.

## 6. El sweeper `reconcile_stuck`

El sweeper (`sentiment.handlers.reconcile_stuck.lambda_handler`, servicio en
`services/reconcile_stuck.py`) es la garantía de terminación. Una regla de
EventBridge Scheduler lo invoca **cada 15 minutos**.

**Qué hace cada pasada**: barre los candidatos del índice disperso (Lotes no
terminales sin avance ≥ 900 s, o terminales sin Evento_Finalizacion publicado) y,
por cada uno:

- **No terminal + abandonado** (doble condición: `updated_at` antiguo ≥ 900 s **y**
  la ejecución de Step Functions **no** está `RUNNING` — `SUCCEEDED`/`FAILED`/
  `TIMED_OUT`/`ABORTED` o inexistente): cierra el META como `EXPIRED`, marca sus
  Ítems `PENDING` como `FAILED`/`EXECUTION_ABANDONED`, publica el Evento_Finalizacion
  y lo retira del índice. Cuenta como `StuckAnalyses`.
- **Terminal sin evento publicado**: solo **republica** el Evento_Finalizacion (sin
  tocar estado ni contadores) y lo marca como publicado. No cuenta como atascado.
- **No cumple la doble condición**: lo deja intacto (p. ej. ejecución aún
  `RUNNING`).

**Garantía clave**: mientras la ejecución corra, el sweeper **no** expira el Lote
(evita matar un Lote legítimo con Bedrock degradado durante horas). El techo de 3 h
de la máquina asegura que la ejecución termine y el sweeper cierre el Lote en una
pasada posterior.

**Invocación manual** (diagnóstico/forzar una pasada; es idempotente y seguro):

```bash
aws lambda invoke --function-name <NOMBRE_LAMBDA_RECONCILE_STUCK> \
  --payload '{}' /tmp/reconcile_out.json
cat /tmp/reconcile_out.json   # { candidates_examined, expired, events_republished, skipped }
```

> El payload del evento del Scheduler no se inspecciona; `{}` es suficiente. La
> métrica `StuckAnalyses` se publica siempre al terminar, incluso con 0.

## 7. Variables de entorno operativas

Nombres (nunca valores/secretos) inyectados por la Infraestructura a las Lambdas.
Los secretos (client secrets de Cognito, secreto HMAC de webhook) viven en
Secrets_Manager, no en variables de entorno.

| Variable | Lambdas que la leen | Propósito |
|---|---|---|
| `TABLE_NAME` | api_*, procesamiento, sweeper | Nombre de la Dynamo_Table (`{project}-{env}-analysis`). |
| `BUCKET_NAME` | api_submit, api_download, api_purge, procesamiento | Nombre del Bucket_Analisis. |
| `QUEUE_URL` | api_submit (SQS publisher) | URL de la Cola_Solicitudes. |
| `EVENT_BUS_NAME` | aggregate_results, sweeper | Nombre del Bus_Eventos para el Evento_Finalizacion. |
| `STATE_MACHINE_ARN` | sweeper (`reconcile_stuck`) | ARN de la Maquina_De_Estados; deriva el ARN de ejecución por `analysis_id`. |
| `MODEL_ID` | analyze_llm (Bedrock) | Identificador del Modelo_Bedrock. **Cambiable sin redeploy** vía Parameter_Store `/sentiment/<env>/MODEL_ID`. |
| `MAX_ITEMS` | ingesta/config | Tope de Ítems por Lote (defecto 1.000). |
| `TTL_DAYS` | repositorio | TTL de los registros (defecto 90). |
| `MAX_CONCURRENCY` | config / Distributed_Map | Concurrencia del fan-out. |
| `CONFIG_ENV` | config | Entorno (`dev`/`stage`/`prod`); prefijo de Parameter_Store. |
| `SES_FROM_ADDRESS` | notify_email | Remitente verificado de SES. |
| `SES_CONFIGURATION_SET` | notify_email | Configuration set de seguimiento de rebotes/quejas. |
| `DOWNLOAD_URL_BASE` | notify_email | Base del enlace de descarga del correo (apunta al endpoint autenticado, no a una URL prefirmada). |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | todos | Región (resuelta por el runtime). |

**Resolución de configuración**: variable de entorno primero; para los valores
cambiables sin redeploy (`MODEL_ID`), *fallback* a Parameter_Store
`/sentiment/<env>/<CLAVE>`. Un valor requerido ausente o en blanco falla con un
`ConfigError` accionable que nombra la clave (no un `KeyError` opaco), nunca en el
import del módulo.

## 8. Pruebas de integración y de carga contra dev

> **Seguridad**: solo *placeholders*. boto3 toma las credenciales de la cadena
> estándar del SDK; los textos enviados son sintéticos, sin PII.

### 8.1 Pruebas de integración (tarea 15.2)

Ejercitan el pipeline **end-to-end contra un stack desplegado** en `dev`. Se
**saltan** (`SKIPPED`) de forma limpia cuando faltan las variables obligatorias, de
modo que `python -m pytest -q` en un checkout normal no falla por ellas. Detalle
completo en `backend/tests/integration/README.md` y en la
[sección 11 de `ARCHITECTURE.md`](./ARCHITECTURE.md#11-pruebas-de-integración-contra-un-stack-desplegado-dev).

```bash
cd backend && source .venv/bin/activate
export AWS_REGION="<REGION>"
export INTEGRATION_STACK_ENV="dev"
export INTEGRATION_API_BASE_URL="<API_BASE_URL>"   # https://<api-id>.execute-api.<region>.amazonaws.com/<stage>
export INTEGRATION_ACCESS_TOKEN="<ACCESS_TOKEN>"   # Bearer con scopes write/read (y purge)
export INTEGRATION_TENANT_ID="<TENANT_ID>"         # claim custom:tenant_id del token
# opcionales:
export INTEGRATION_PROJECT="sentiment"
export INTEGRATION_ACCOUNT_ID="<ACCOUNT_ID>"       # si falta, se resuelve con STS
export INTEGRATION_RECONCILE_FUNCTION_NAME="<NOMBRE_LAMBDA_RECONCILE_STUCK>"  # solo escenario de expiración
python -m pytest -m integration -q
```

Cada prueba purga los Lotes que crea (fixture `purge_guard`), por lo que no deja
datos sintéticos en `dev` aunque falle una aserción.

### 8.2 Pruebas de carga (tarea 15.4)

Las pruebas de carga viven en `backend/tests/load/` y comparten el patrón de las de
integración: corren **solo** contra un `dev` desplegado y se **saltan** sin las
variables de entorno de activación; nunca se ejecutan en el `Pipeline_CI` normal.

Al ejecutarlas, úsalas para calibrar la concurrencia frente a la latencia real del
Modelo_Bedrock (coordinado con `scripts/calibrate_bedrock.py`, tarea 15.3) y vigila
el panel durante la corrida: `BedrockThrottles` ([5.2](#52-throttling-de-bedrock--50--5-min)),
la antigüedad de la Cola_Solicitudes ([5.3](#53-antigüedad-de-la-cola_solicitudes--300-s)),
los throttles de Lambda ([5.6](#56-lambda-throttled)) y el presupuesto diario
([5.8](#58-presupuesto-diario-superado)).

> Consulta `backend/tests/load/README.md` (lo aporta la tarea 15.4) para el comando
> exacto, las variables de activación y los perfiles de carga. Si ese README aún no
> existe en tu checkout, la tarea de carga todavía no se ha integrado; no inventes
> los parámetros.
