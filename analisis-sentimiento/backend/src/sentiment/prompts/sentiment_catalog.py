"""Catálogo de sentimientos versionado del Sistema.

:data:`LIST_OF_SENTIMENTS` es el catálogo orientativo de emociones que se
incrusta en el ``system_prompt`` del Modelo_Bedrock para guiar (sin limitar) la
identificación de sentimientos del Análisis profundo. Se conserva verbatim
respecto del código legado, sin cambios semánticos (Requirement 17.13).

El catálogo es orientativo: el prompt indica explícitamente que las emociones
detectadas no se limitan a esta lista. Por eso no actúa como enum cerrado (a
diferencia de los Indificadores) y se mantiene como texto libre versionado.
"""

from __future__ import annotations

from typing import Final

# Catálogo de sentimientos verbatim del prompt legado. Se incrusta en el
# system_prompt mediante el marcador @@LIST_OF_SENTIMENTS.
LIST_OF_SENTIMENTS: Final[str] = """

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
