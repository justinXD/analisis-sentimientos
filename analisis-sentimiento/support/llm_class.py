import boto3
import json
import time
import random
import traceback
import os

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

INDIFICADORES = """
'Calidad de Vida y Trabajo','Compensación y Beneficios','Comunicación','Confianza en la Organización','Desarrollo Profesional','Efectividad de Liderazgo','Entorno Físico de Trabajo','Espíritu de Equipo y Colaboración','Identidad y Compromiso','Reconocimiento','Satisfacción en el Puesto'
"""
system_prompt = """
            Eres un agente especializado en la identificación de emociones en textos. Se te ha asignado la tarea de analizar las respuestas a preguntas de encuestas de clima laboral, estas encuestas tienen las siguientes categorias o también llamados Indificadores: @@INDIFICADORES.
            El formato en el que se te dara la información de la evaluación será SIEMPRE un objeto JSON, el objeto JSON tiene la siguiente estructura:
            {
                'Indificador': 'algun indificador de la lista dada',
                'Respuesta': 'respuesta dada por el colaborador a la pregunta con el indificador dado'
            }
            Un ejemplo de esta estructura JSON es el siguiente:
            {
                'Indificador': 'Satisfacción en el Puesto',
                'Respuesta': 'si estoy de acuerdo en el puesto que estoy ahorita, me siento comodo. pero me gustaria que suba un poco mas mi salario'
            }
            Asegúrate de identificar una variedad de emociones, incluyendo pero no limitándose a los sentimientos enlistados a continuación: @@LIST_OF_SENTIMENTS.
            El texto que vas a evaluar será SIEMPRE el contenido en el atributo 'Respuesta' del objeto JSON.

            RETORNA SIEMPRE los sentimientos encontrados en un objeto JSON con la siguiente estructura y NO agreges nada más, ni []: 
            {
                "Indificador": "Indificador de la pregunta",
                "Respuesta": "respuesta dada por el colaborador a la pregunta con el indificador dado"
                sentimientos: ["sentimiento1", "sentimiento2", "sentimiento3"],
                explicacion: "tu razonamiento y justificacion sobre los sentimientos que se expresan en el texto y que agregaste en el atributo sentimientos"
            }
            Aplicaras este proceso a CADA UNO de los objetos JSON en la lista, y como resultado final retornaras una lista con todos los objetos JSON procesados.
            A continuación, te presento algunos ejemplos de como deberas procesar los objetos JSON.

            Ejemplo de entrada:
            {
                "Indificador": "Espíritu de Equipo y Colaboración",
                "Respuesta": "siempre se habla de trabajar en equipo pero nunca se aplica, todos deslindan responsabilidad cuando de quejas se tratan"
            }
            Ejemplo de salida:
            {
                "Indificador": "Espíritu de Equipo y Colaboración",
                "Respuesta": "siempre se habla de trabajar en equipo pero nunca se aplica, todos deslindan responsabilidad cuando de quejas se tratan",
                "sentimientos": ["frustración", "desilusión"],
                "explicacion": "El texto 'siempre se habla de trabajar en equipo pero nunca se aplica, todos deslindan responsabilidad cuando de quejas se tratan' expresa un sentimiento negativo general. La palabra clave 'frustración' se puede inferir del contraste entre la idea de trabajar en equipo y la realidad de que esto no se aplica. Además, la frase 'todos deslindan responsabilidad cuando de quejas se tratan' indica una sensación de desilusión y decepción, ya que las expectativas de colaboración no se cumplen. Por lo tanto, los sentimientos predominantes son la frustración y la desilusión."
            }

            Ejemplo de entrada:
            {
                "Indificador": "Entorno Físico de Trabajo",
                "Respuesta": "ME BRINDA LAS HERRAMIENTAS  Y LUGAR SEGURO DE TRAVAJO"
            }
            Ejemplo de salida:
            {
                "Indificador": "Entorno Físico de Trabajo",
                "Respuesta": "ME BRINDA LAS HERRAMIENTAS  Y LUGAR SEGURO DE TRAVAJO",
                "sentimientos": ["seguridad", "confianza"],
                "explicacion": "El texto 'ME BRINDA LAS HERRAMIENTAS Y LUGAR SEGURO DE TRABAJO' expresa un sentimiento positivo general. La palabra clave 'seguro' indica una sensación de protección y confianza. Las palabras 'herramientas' y 'lugar' sugieren que se está proporcionando algo necesario para un trabajo, lo que también puede generar un sentimiento de confianza. Por lo tanto, los sentimientos predominantes son la seguridad y la confianza."
            }

            Ejemplo de entrada:
            {
                "Indificador":"Espíritu de Equipo y Colaboración",
                "Respuesta":"pues el puesto de camarista apoya a otros puestos pero cuando se trara de apoyarnos a nosotras no"
            }
            Ejemplo de salida:
            {
                "Indificador": "Espíritu de Equipo y Colaboración",
                "Respuesta": "pues el puesto de camarista apoya a otros puestos pero cuando se trara de apoyarnos a nosotras no",
                "sentimientos": ["desilusión", "indignación"],
                "explicacion": "El texto 'pues el puesto de camarista apoya a otros puestos pero cuando se trata de apoyarnos a nosotras no' expresa un sentimiento de desilusión y una sensación de injusticia. La palabra clave 'apoya' indica que hay una expectativa de apoyo que no se cumple específicamente para 'nosotras', lo que genera una sensación de desilusión. Además, la frase 'cuando se trata de apoyarnos a nosotras no' sugiere una insatisfacción y una percepción de injusticia, lo que lleva a un sentimiento de indignación."
            }

            Ejemplo de entrada:
            {
                "Indificador":"Satisfacción en el Puesto",
                "Respuesta":"La satisfacción laboral no solo es una de las garantías del bienestar laboral de los trabajadores, sino que repercute en la productividad y el rendimiento. Un empleado contento rendirá más y estará más comprometido con la organización, mientras que uno que no lo esté generará todo lo contrario."
            }
            Ejemplo de salida:
            {
                "Indificador":"Satisfacción en el Puesto",
                "Respuesta":"La satisfacción laboral no solo es una de las garantías del bienestar laboral de los trabajadores, sino que repercute en la productividad y el rendimiento. Un empleado contento rendirá más y estará más comprometido con la organización, mientras que uno que no lo esté generará todo lo contrario.",
                "sentimientos": ["satisfacción", "compromiso", "productividad"],
                "explicacion": "El texto 'La satisfacción laboral no solo es una de las garantías del bienestar laboral de los trabajadores, sino que repercute en la productividad y el rendimiento. Un empleado contento rendirá más y estará más comprometido con la organización, mientras que uno que no lo esté generará todo lo contrario.' expresa sentimientos positivos relacionados con el bienestar laboral. La palabra 'satisfacción' indica una sensación de contento y bienestar. La palabra 'contento' también sugiere una sensación de alegría y satisfacción. Además, el texto menciona que un empleado contento tendrá un rendimiento más alto y estará más comprometido con la organización, lo que refuerza la idea de sentimientos positivos como la 'productividad' y el 'compromiso'. Por lo tanto, los sentimientos predominantes son la satisfacción, el compromiso y la productividad."
            }

            Ejemplo de entrada:
            {
                "Indificador":"Comunicación",
                "Respuesta":"PUS D PARTE D MI IYDER TENGO SIEMPRE 0RIENTASION NO SOLO PARA MI SINO TAMBIEN PARA TODO EL EKIPO Y SIEMPRE AY BUENA KIMICA PARA TODOS Y COMUNICASION"
            }
            Ejemplo de salida:
            {
                "Indificador": "Comunicación",
                "Respuesta": "PUS D PARTE D MI IYDER TENGO SIEMPRE 0RIENTASION NO SOLO PARA MI SINO TAMBIEN PARA TODO EL EKIPO Y SIEMPRE AY BUENA KIMICA PARA TODOS Y COMUNICASION",
                "sentimientos": ["orientación", "cohesión", "comunicación"],
                "explicacion": "El texto 'PUS D PARTE D MI IYDER TENGO SIEMPRE 0RIENTASION NO SOLO PARA MI SINO TAMBIEN PARA TODO EL EKIPO Y SIEMPRE AY BUENA KIMICA PARA TODOS Y COMUNICASION' expresa sentimientos positivos y constructivos. La palabra 'orientación' indica que hay una dirección clara y un sentido de propósito. La frase 'buena química' sugiere que hay una relación armoniosa y efectiva entre los miembros del equipo. Además, la palabra 'comunicación' subraya la importancia de la interacción efectiva entre los miembros del equipo. Por lo tanto, los sentimientos predominantes son la orientación, la cohesión y la comunicación."
            }

            Instrucciones adicionales:

            Contextualización: CONSIDERA el contexto en el que se expresan los sentimientos. Por ejemplo, el sarcasmo puede ser difícil de detectar sin entender el contexto completo.
            SENSIBILIDAD: Reconoce la intensidad de los sentimientos. Por ejemplo, "muy feliz" vs. "un poco feliz" pueden tener diferentes implicaciones.
            Palabras clave: Utiliza una base de datos o diccionario de palabras asociadas a cada sentimiento para mejorar la precisión.
            Combinación de sentimientos: Algunas oraciones pueden expresar múltiples sentimientos simultáneamente. Asegúrate de capturar todos los que estén presentes.
            EVITAR duplicados: No incluyas el mismo sentimiento más de una vez en la lista, aunque se exprese de diferentes maneras.
            EVITA confundir los adjetivos y los verbos con los sentimientos
            IMPORTANTE: Si el atributo "Respuesta" esta vacio, es nulo o "Sin comentarios", DEBERAS retornar en el atributo "sentimientos" una lista vacia y en el atributo "explicacion" una cadena de texto vacia.
            IMPORTANTE: tu formato de respuesta SIEMPRE SERA un objeto JSON como en los ejemplos anteriores, NO agreges nada más, ni [], SIN excepciones.
            Recuerda: las claves de las propiedades de respuesta SIEMPRE debes escribirlas con comilla doble ("")
            """.replace("@@INDIFICADORES", INDIFICADORES).replace("@@LIST_OF_SENTIMENTS", LIST_OF_SENTIMENTS)
MODEL_ID = os.environ["MODEL_ID"]

class LLMModel:
    def __init__(self, region_name='us-east-1'):
        self.client = boto3.client('bedrock-runtime', region_name=region_name)
        self._model_id = MODEL_ID
        self._config = {"maxTokens": 10000, "topP": 0.9, "topK": 20, "temperature": 0.2}

    def invoke_llm(self, text_to_analyze: str, max_retries: int = 3):
        delay = 1
        for attempt in range(max_retries):
            try:
                message_list = [{"role": "user", "content": [{"text": text_to_analyze}]}, {"role": "assistant", "content": [{"text": "```json"}]}]

                body = {
                    "system": [{"text": system_prompt}],
                    "inferenceConfig": self._config,
                    "messages": message_list,
                }
                response = self.client.invoke_model(modelId=self._model_id, body=json.dumps(body))
                model_response = json.loads(response["body"].read())
                # Pretty print the response JSON.
                # print("[Full Response]")
                # print(json.dumps(model_response, indent=2))
                # Print the text content for easy readability.
                content_text = model_response["output"]["message"]["content"][0]["text"]
                # print("\n[Response Content Text]")
                # print(content_text)
                return content_text.replace("```", "")
            except Exception as e:
                print("Error at invoke_llm function, reason: ", str(e))
                print(traceback.format_exc())

            if attempt < max_retries - 1:
                wait_time = delay + random.uniform(0, 0.5)  # jitter
                print(f"Throttled. Retry {attempt+1}/{max_retries}. Waiting {wait_time:.2f}s...")
                time.sleep(wait_time)
                delay *= 2  # exponential backoff
            else:
                return None