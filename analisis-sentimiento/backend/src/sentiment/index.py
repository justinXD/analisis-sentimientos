import json

from sentiment.adapters.bedrock_adapter import BedrockAdapter
from sentiment.adapters.comprehend_adapter import ComprehendAdapter
from sentiment.observability.logging import get_logger
from sentiment.prompts.indificadores import INDIFICADORES

logger = get_logger(service="legacy_sentiment")


def lambda_handler(event, context):
    try:
        comprehend = ComprehendAdapter()
        llm = BedrockAdapter()
        # Nunca se loguea `event`: contiene `Respuesta`, que es PII (criterio 15.1).
        logger.info("handler_invoked")
        # validamos el tipo de dato de event y su contenido
        if validate_event(event):
            # event tiene estructura valida
            text = event.get("Respuesta")
            # Los adaptadores migrados (tareas 6.2/6.3) exponen el puerto batch de
            # Comprehend: se analiza este único Ítem como un batch de 1 elemento y
            # se reconcilia por `Index` (aquí, el índice 0).
            language_code = comprehend.detect_languages([text])[0]
            # Solo el código de idioma detectado (metadato), no el texto (criterio 15.2).
            logger.info("language_detected", extra={"language_code": language_code})
            successes, _errors = comprehend.detect_sentiments([text], language_code)
            classic = successes.get(0)
            sentiment = (
                classic.sentimiento_general.value if classic is not None else "N/F"
            )
            # El adaptador migrado (tarea 6.3) implementa el puerto LlmAnalyzer:
            # una sola invocación por la API Converse y una salida TOTAL
            # (EmotionAnalysis válido o excepción tipada), nunca None. Esto
            # elimina la rama "no response from llm" del flujo legado.
            emotions = llm.analyze(event.get("Indificador"), text)
            # Metadatos del resultado, nunca el contenido (explicación es PII).
            logger.info(
                "llm_sentiment_received",
                extra={"sentimientos_count": len(emotions.sentimientos)},
            )
            result_dict = {
                "sentimientoGeneral": sentiment,
                "Indificador": event.get("Indificador"),
                "Respuesta": text,
                "sentimientos": list(emotions.sentimientos),
                "explicacion": emotions.explicacion,
            }
            return {
                'statusCode': 200,
                "headers": {
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Headers': '*'
                    },
                'body': json.dumps(result_dict, ensure_ascii=False)
            }
        else:
            error_dict = {
            "error_type": "Params error",
            "message": "Body is not valid or does not have the right params"
            }
            return {
                'statusCode': 500,
                "headers": {
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Headers': '*'
                    },
                'body': json.dumps(error_dict)
            }
    except Exception as exc:
        # El detalle del error va solo al log del servidor, nunca al cliente ni
        # con PII; al cliente se le devuelve un mensaje genérico (criterio 15.5).
        logger.exception(
            "handler_exception",
            extra={"error_type": exc.__class__.__name__},
        )
        error_dict = {
            "error_type": exc.__class__.__name__,
            "message": str(exc)
        }
        return {
            'statusCode': 500,
            "headers": {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': '*'
                },
            'body': json.dumps(error_dict, ensure_ascii=False)
        }

def validate_event(event):
    try:
        if isinstance(event, dict):
            # event es un diccionario
            if "Indificador" in event and "Respuesta" in event:
                # event tiene la informacion correcta
                # validamos que las propiedades de event sean validas
                indificador = event.get("Indificador", "")
                respuesta = event.get("Respuesta", "")
                if indificador in INDIFICADORES:
                    # indificador es valido, validamos ahora respuesa
                    if isinstance(respuesta, str):
                        if len(respuesta) > 0:
                            # respuesta tiene contenido
                            logger.info("event_validation_passed")
                            return True
                        else:
                            logger.info(
                                "event_validation_failed",
                                extra={"reason": "respuesta_empty"},
                            )
                            return False
                    else:
                        logger.info(
                            "event_validation_failed",
                            extra={"reason": "respuesta_not_str"},
                        )
                        return False
                else:
                    # Se registra el motivo, no el valor del indificador recibido.
                    logger.info("event_validation_failed", extra={"reason": "unknown_indificador"})
                    return False
            else:
                logger.info("event_validation_failed", extra={"reason": "missing_fields"})
                return False
        else:
            logger.info("event_validation_failed", extra={"reason": "event_not_dict"})
            return False

    except Exception as exc:
        logger.exception(
            "validate_event_exception",
            extra={"error_type": exc.__class__.__name__},
        )
        return False
