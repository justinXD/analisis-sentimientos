import json
import traceback
from support.ComprehendClass import ComprehendClass

def handler(event, context):
    try:
        print(event)
        comprehend = ComprehendClass()
        text = "Excelente lugar para entrenar, correr, caminar andar en bici o pasear al perro."
        language = comprehend.detect_languages(text)
        language_code = language[0]["LanguageCode"]
        print(f"Language detected: {language}")
        sentiment = comprehend.detect_sentiment(text, language_code)
        print(f"Sentiment detected: {sentiment}")
        return {
            'statusCode': 200,
            'body': json.dumps('Hello from Lambda!')
        }
    except Exception as e:
        print(f"An error occurred at handler: {e}")
        print(traceback.format_exc())
        return {
            'statusCode': 500,
            'body': json.dumps('An error occurred while processing the request.')
        }
