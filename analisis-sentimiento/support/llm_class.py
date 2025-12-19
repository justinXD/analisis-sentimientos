import boto3
import json
import traceback

LIST_OF_SENTIMENTS = """

Sentimientos positivos:

    Felicidad: Estado de ánimo positivo, alegría.
    Amor: Sentimiento profundo de afecto y conexión hacia otra persona o cosa.
    Alegría: Emoción de placer y satisfacción.
    Gratitud: Sentimiento de agradecimiento.
    Esperanza: Sentimiento de expectativa positiva hacia el futuro.
    Calma: Estado de tranquilidad y paz interior.
    Entusiasmo: Sentimiento de gran interés y emoción.
    Orgullo: Sentimiento de satisfacción por los logros propios o de otros.
    Confianza: Seguridad en uno mismo y en las propias capacidades.
    Afecto: Sentimiento de cariño y ternura.
    Admiración: Sentimiento de respeto y aprecio por alguien o algo.
    Euforia: Estado de intensa felicidad y alegría.
    Optimismo: Tendencia a ver las cosas de manera positiva.
    Satisfacción: Sentimiento de complacencia y contento. 

Sentimientos negativos:

    Tristeza: Estado de ánimo de abatimiento y melancolía.
    Ira: Emoción intensa de enfado y hostilidad.
    Miedo: Emoción de temor y angustia ante un peligro.
    Ansiedad: Estado de preocupación y nerviosismo.
    Culpa: Sentimiento de responsabilidad por haber cometido un error.
    Vergüenza: Sentimiento de incomodidad o humillación por algo que se ha hecho.
    Celos: Sentimiento de resentimiento y envidia hacia otra persona.
    Envidia: Sentimiento de resentimiento hacia otra persona por lo que tiene.
    Desesperación: Sentimiento de falta de esperanza y abatimiento.
    Soledad: Sentimiento de aislamiento y falta de compañía.
    Frustración: Sentimiento de impotencia y decepción.
    Desilusión: Pérdida de la ilusión o esperanza.
    Nostalgia: Sentimiento de añoranza por el pasado.
    Dolor: Sensación física o emocional desagradable. 

Sentimientos ambiguos:

    Sorpresa: Reacción inesperada ante algo nuevo o inesperado.
    Interés: Sentimiento de curiosidad y atención hacia algo.
    Aburrimiento: Sentimiento de tedio e falta de interés.
    Confusión: Estado de perplejidad o falta de claridad.
    Compasión: Sentimiento de tristeza y lástima por el sufrimiento de otros. 

Otros:

    Amargura: Sentimiento de resentimiento y resentimiento.
    Resentimiento: Sentimiento de rencor y hostilidad hacia otra persona.
    Melancolía: Estado de tristeza vaga y profunda.
    Aversión: Sentimiento de rechazo y desagrado.
    Antipatia: Sentimiento de desagrado y hostilidad hacia otra persona.
    Fascinación: Sentimiento de atracción y admiración intensa.
"""

class LLMModel:
    def __init__(self, region_name='us-east-1'):
        self.client = boto3.client('bedrock-runtime', region_name=region_name)
        self._model_id = "amazon.nova-lite-v1:0"
        self._config = {"maxTokens": 5000, "topP": 0.9, "topK": 20, "temperature": 0.2}

    def invoke_llm(self, text_to_analyze: str):
        try:
            print()
            system_prompt = """
            "Analiza el siguiente texto y extrae todos los sentimientos expresados en él. Asegúrate de identificar una variedad de emociones, incluyendo pero no limitándose a los sentimientos enlistados a continuación: {{LIST_OF_SENTIMENTS}}. 
            RETORNA SIEMPRE los sentimientos encontrados en un objeto JSON con la siguiente estructura: 
            {
                sentimientos: ["sentimiento1", "sentimiento2", "sentimiento3"],
                explicacion: "tu razonamiento y justificacion sobre los sentimientos que se expresan en el texto y que agregaste en el atributo sentimientos"
            }

            Ejemplo de entrada:
            "Estoy realmente feliz de que hayas ganado, pero también un poco enojado porque no me avisaste antes."
            Ejemplo de salida:
            {
                "sentimientos": ["felicidad", "enojo"],
                "explicacion": "El texto "Estoy realmente feliz de que hayas ganado, pero también un poco enojado porque no me avisaste antes." expresa un sentimiento ambiguo, positivo de manera general y negativo. La palabra "feliz" indica una sensación de alegría y satisfacción. Sin embargo, tambien se menciona que se esta "un poco enojado", dado que el otro interlocutor debia de notificar su participacion en algun evento y no lo hizo, lo que proboca la molestia. Por lo tanto, el sentimiento predominante es la felicidad, sumandole un poco de enojo."
            }

            Ejemplo de entrada:
            "Excelente lugar para entrenar, correr, caminar andar en bici, hacer picnics o pasear al perro."
            Ejemplo de salida:
            {
                "sentimientos": ["alegría"],
                "explicacion": "El texto "Excelente lugar para entrenar, correr, caminar andar en bici, hacer picnics o pasear al perro." expresa un sentimiento positivo general. La palabra "excelente" indica una sensación de alegría y satisfacción. Las actividades mencionadas (entrenar, correr, caminar, andar en bici, hacer picnics o pasear al perro) son generalmente asociadas con bienestar y felicidad. Por lo tanto, el sentimiento predominante es la alegría."
            }

            Ejemplo de entrada:
            "No puedo creer que hayas hecho eso, ¡estoy furioso! Y ahora me siento tan decepcionado."
            Ejemplo de salida:
            {
                "sentimientos": ["furia", "decepción"],
                "explicacion": "El texto refleja primeramente sorpresa por alguna acción hecha, pero enseguida una palabra clave: furioso. Seguido a esto tenemos que dada a esa acción, viene en seguida la siguiente palabra clave: decepción. Por lo tanto, los sentimientos predominantes son la furia y la decepción."
            }

            Ejemplo de entrada:
            "No sé por qué, pero hoy me siento un poco apático."
            Ejemplo de salida:
            {
                "sentimientos": ["apatía"],
                "explicacion": "El texto "No sé por qué, pero hoy me siento un poco apático." expresa un unico sentimiento que es nuestra palabra clave: apatía. No se nos da mayor razón ni contexto del por que se escribio esto ni hacia que acción, estimulación o persona se siente la apatia, pero podemos asociarlo también con tristeza. Por lo tanto, la emoción predominante es la apatía."
            }

            Ejemplo de entrada:
            "¡Qué gracioso! No puedo creer que hayas hecho algo así, ¡es tan irónico!"
            Ejemplo de salida:
            {
                "sentimientos": ["decepción"],
                "explicacion": "El texto no menciona que fue lo que se hizo, sin embargo, vemos que el uso de la palabra clave "irónico", nos da la pauta para entender que se habla con sarcasmo. No se esta feliz por lo que sea que se haya hecho, si no todo lo contrario. Por lo tanto, la emoción predominante es la decepción."
            }

            Instrucciones adicionales:

            Contextualización: CONSIDERA el contexto en el que se expresan los sentimientos. Por ejemplo, el sarcasmo puede ser difícil de detectar sin entender el contexto completo.
            SENSIBILIDAD: Reconoce la intensidad de los sentimientos. Por ejemplo, "muy feliz" vs. "un poco feliz" pueden tener diferentes implicaciones.
            Palabras clave: Utiliza una base de datos o diccionario de palabras asociadas a cada sentimiento para mejorar la precisión.
            Combinación de sentimientos: Algunas oraciones pueden expresar múltiples sentimientos simultáneamente. Asegúrate de capturar todos los que estén presentes.
            EVITAR duplicados: No incluyas el mismo sentimiento más de una vez en la lista, aunque se exprese de diferentes maneras.
            EVITA confundir los adjetivos y los verbos con los sentimientos
            """
            message_list = [{"role": "user", "content": [{"text": text_to_analyze}]}, {"role": "assistant", "content": [{"text": "```json"}]}]

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