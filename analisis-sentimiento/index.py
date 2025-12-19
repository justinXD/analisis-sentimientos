import json
import traceback
from support.ComprehendClass import ComprehendClass
from support.llm_class import LLMModel

def handler(event, context):
    try:
        print(event)
        comprehend = ComprehendClass()
        llm = LLMModel()
        text = "PUS D PARTE D MI IYDER TENGO SIEMPRE 0RIENTASION NO SOLO PARA MI SINO TAMBIEN PARA TODO EL EKIPO Y SIEMPRE AY BUENA KIMICA PARA TODOS Y COMUNICASION"
        language = comprehend.detect_languages(text)
        language_code = language[0]["LanguageCode"]
        print(f"Language detected: {language}")
        sentiment = comprehend.detect_sentiment(text, language_code)
        llm_sentiment = llm.invoke_llm(text)
        print(f"LLM sentiment: {llm_sentiment}")
        print(f"Sentiment detected: {sentiment}")
        return {
            'statusCode': 200,
            "headers": {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': '*'
            },
            'body': json.dumps({"sentiment": sentiment})
        }
    except Exception as e:
        print(f"An error occurred at handler: {e}")
        print(traceback.format_exc())
        return {
            'statusCode': 500,
            'body': json.dumps('An error occurred while processing the request.')
        }
