import json
import traceback
from support.comprehend_class import ComprehendClass
from support.nova_class import LLMModel
INDIFICADORES = ['Calidad de Vida y Trabajo','Compensación y Beneficios','Comunicación','Confianza en la Organización','Desarrollo Profesional','Efectividad de Liderazgo','Entorno Físico de Trabajo','Espíritu de Equipo y Colaboración','Identidad y Compromiso','Reconocimiento','Satisfacción en el Puesto']
SENTIMIENTOGENERAL = {
    "POSITIVE":"Positivo",
    "NEGATIVE": "Negativo",
    "NEUTRAL": "Neutral",
    "MIXED": "Mixto"
}

def lambda_handler(event, context):
    try:
        # TODO implement
        comprehend = ComprehendClass()
        llm = LLMModel()
        print("Hola mundo")
        print(event)
        # validamos el tipo de dato de event y su contenido
        if validate_event(event):
            # event tiene estructura valida
            text = event.get("Respuesta")
            language = comprehend.detect_languages(text)
            language_code = language[0]["LanguageCode"]
            print(f"Language detected: {language}")
            sentiment = comprehend.detect_sentiment(text, language_code)
            sentiment = SENTIMIENTOGENERAL.get(sentiment, "N/F") if language_code == "es" else sentiment
            dict_to_analyze = { "Indificador": event.get("Indificador"), "Respuesta": text }
            llm_sentiment = llm.invoke_llm(json.dumps(dict_to_analyze, ensure_ascii=False))
            if llm_sentiment is not None:
                print("llm_sentiment: ", llm_sentiment)
                print(type(llm_sentiment))
                llm_parsed = {}
                if isinstance(llm_sentiment, list):
                    # llm_sentiment = llm_sentiment[0]
                    print("llm_sentiment es instancia de list")
                    llm_parsed = json.loads(llm_sentiment)[0]
                elif isinstance(llm_sentiment, str):
                    print("llm_sentiment es instancia de str")
                    llm_parsed = json.loads(llm_sentiment)

                print("llm_parsed: ", llm_parsed)
                result_dict = { "sentimientoGeneral": sentiment, **llm_parsed }
                return {
                    'statusCode': 200,
                    "headers": {
                            'Access-Control-Allow-Origin': '*',
                            'Access-Control-Allow-Headers': '*'
                        },
                    'body': json.dumps(result_dict, ensure_ascii=False)
                }
            else:
               return {
                    'statusCode': 500,
                    "headers": {
                            'Access-Control-Allow-Origin': '*',
                            'Access-Control-Allow-Headers': '*'
                        },
                    'body': json.dumps({"error": "no response from llm"}, ensure_ascii=False)
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
    except Exception as e:
        print("Handler exception")
        print(traceback.format_exc())
        error_dict = {
            "error_type": e.__class__.__name__,
            "message": str(e)
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
                            print("todas las validaciones correctas")
                            return True
                        else:
                            print("respuesta no es un string valido")
                            return False
                    else:
                        print("respuesta no es instancia de str")
                        return False
                else:
                    print(f"indificador no valido, indificador no existe en lista de indificadores. Indificador: {indificador}")
                    return False
            else:
                print("event no contiene indificador ni respuesta")
                return False
        else:
            print("event no es una instancia de dict")
            return False

    except Exception as e:
        print("Error at validate_event function: ", str(e))
        print(traceback.format_exc())
        return False