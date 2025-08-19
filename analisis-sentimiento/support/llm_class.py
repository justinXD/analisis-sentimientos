import boto3
import json
import traceback

class LLMModel:
    def __init__(self, region_name='us-east-1'):
        self.client = boto3.client('bedrock-runtime', region_name=region_name)
        self._model_id = "amazon.nova-lite-v1:0"
        self._config = {"maxTokens": 5000, "topP": 0.9, "topK": 20, "temperature": 0.7}

    def invoke_llm(self, text_to_analyze: str):
        try:
            print()
            system_prompt = """
            "Analiza el siguiente texto y extrae todos los sentimientos expresados en él. Asegúrate de identificar una variedad de emociones, incluyendo pero no limitándose a enojo, frustración, tristeza, felicidad, apatía, rencor, sarcasmo, doble sentido, etc. Retorna los sentimientos encontrados en un objeto JSON con la siguiente estructura: {sentimientos: ["sentimiento1", "sentimiento2", "sentimiento3", ...]}."

            Ejemplo de entrada:
            "Estoy realmente feliz de que hayas ganado, pero también un poco enojado porque no me avisaste antes."
            Ejemplo de salida:
            {
            "sentimientos": ["felicidad", "enojo"]
            }

            Ejemplo de entrada:
            "No puedo creer que hayas hecho eso, ¡estoy furioso! Y ahora me siento tan decepcionado."
            Ejemplo de salida:
            {
            "sentimientos": ["enojo", "decepción"]
            }

            Ejemplo de entrada:
            "No sé por qué, pero hoy me siento un poco apático."
            Ejemplo de salida:
            {
            "sentimientos": ["apatía"]
            }

            Ejemplo de entrada:
            "¡Qué gracioso! No puedo creer que hayas hecho algo así, ¡es tan irónico!"
            Ejemplo de salida:
            {
            "sentimientos": ["sarcasmo", "ironía"]
            }

            Instrucciones adicionales para el LLM:

            Contextualización: Considera el contexto en el que se expresan los sentimientos. Por ejemplo, el sarcasmo puede ser difícil de detectar sin entender el contexto completo.
            Sensibilidad: Reconoce la intensidad de los sentimientos. Por ejemplo, "muy feliz" vs. "un poco feliz" pueden tener diferentes implicaciones.
            Palabras clave: Utiliza una base de datos o diccionario de palabras asociadas a cada sentimiento para mejorar la precisión.
            Combinación de sentimientos: Algunas oraciones pueden expresar múltiples sentimientos simultáneamente. Asegúrate de capturar todos los que estén presentes.
            Evitar duplicados: No incluyas el mismo sentimiento más de una vez en la lista, aunque se exprese de diferentes maneras.
            """
            message_list = [{"role": "user", "content": [{"text": text_to_analyze}]}]

            body = {
                "system": [{"text": system_prompt}],
                "inferenceConfig": self._config,
                "messages": message_list,
            }
            response = self.client.invoke_model(modelId=self._model_id, body=json.dumps(body))
            model_response = json.loads(response["body"].read())
            # Pretty print the response JSON.
            print("[Full Response]")
            print(json.dumps(model_response, indent=2))
            # Print the text content for easy readability.
            content_text = model_response["output"]["message"]["content"][0]["text"]
            print("\n[Response Content Text]")
            print(content_text)
            return content_text
        except Exception as e:
            print("Error at invoke_llm function, reason: ", str(e))
            print(traceback.format_exc())