"""Parseo robusto y total de la salida del Modelo_Bedrock (tarea 4.2).

Este módulo convierte el **texto crudo** devuelto por el Modelo_Bedrock en un
resultado de dominio :class:`~sentiment.domain.models.EmotionAnalysis`, o bien
falla de forma explícita con :class:`~sentiment.domain.errors.LlmSchemaError`
(``error_code = LLM_OUTPUT_INVALID``). No existe ningún tercer desenlace: la
función :func:`parse_llm_output` es **total** (criterio 7.13).

Pertenece a la capa de dominio y es **puro**: no importa ``boto3`` ni ningún
adaptador, y es importable sin que el SDK de AWS esté instalado (criterio 17.2).
Su única dependencia de terceros es ``jsonschema``, que ya es dependencia de
runtime del paquete y se empaqueta en la Lambda Layer compartida.

Garantías que materializa (Requirement 7):

* **Extracción tolerante (criterio 7.4).** Dentro de los primeros
  :data:`MAX_SCAN_CHARS` caracteres del texto, se evalúan como candidatos, en
  orden creciente de la posición de su llave de apertura ``{``, todos los objetos
  JSON sintácticamente completos y correctos que contenga, ignorando los
  delimitadores de bloque de código (``` ```json ```), la prosa anterior o
  posterior y la envoltura en un array de un solo elemento. Se adopta como
  resultado el **primer** candidato que cumple el JSON_Schema ``llm_output``, sin
  intentar reparar ni reescribir el texto recibido.

* **Rechazo explícito (criterio 7.5).** Si ningún candidato dentro de la ventana
  cumple el esquema, se lanza :class:`LlmSchemaError` sin producir ningún
  resultado parcial.

* **Normalización de ``sentimientos`` (criterio 7.6).** Sobre el objeto adoptado
  se recorta cada elemento, se pasa a minúsculas, se descartan los que no son
  cadenas, los que quedan vacíos tras el recorte y los que exceden
  :data:`MAX_SENTIMENTO_LEN` caracteres, se eliminan los duplicados conservando
  la primera aparición y se retienen como máximo los primeros
  :data:`MAX_SENTIMIENTOS` valores distintos.

* **Regla de negocio lista vacía (criterio 7.7).** Si la lista normalizada queda
  vacía, la ``explicacion`` resultante es la cadena vacía.

Diseño del escáner: para que el parseo sea seguro ante entradas adversas —texto
muy largo, objetos concatenados, llaves dentro de literales de cadena— **no** se
usa una expresión regular codiciosa (que, con ``\\{.*\\}``, uniría el primer ``{``
con el último ``}`` y no distinguiría objetos concatenados, además de arriesgar
backtracking catastrófico). En su lugar, :func:`iter_candidate_objects` recorre
el texto carácter a carácter contando el anidamiento de llaves y respetando los
literales de cadena JSON y sus escapes, de modo que cada candidato es un
subtexto con llaves balanceadas que se intenta parsear de forma independiente.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Final

from sentiment.domain.errors import LlmSchemaError
from sentiment.domain.models import EmotionAnalysis
from sentiment.schemas import LLM_OUTPUT_SCHEMA_FILE, get_validator

__all__ = [
    "MAX_SCAN_CHARS",
    "MAX_SENTIMIENTOS",
    "MAX_SENTIMENTO_LEN",
    "iter_candidate_objects",
    "extract_json_object",
    "normalize_sentimientos",
    "parse_llm_output",
]

# Ventana de inspección: solo los primeros 100.000 caracteres del texto crudo se
# consideran al buscar candidatos (criterio 7.4). Acota el coste del escáner ante
# una salida anómalamente larga del modelo.
MAX_SCAN_CHARS: Final = 100_000

# Tope de emociones distintas que se retienen tras normalizar (criterio 7.6).
MAX_SENTIMIENTOS: Final = 10

# Longitud máxima de un elemento de ``sentimientos`` tras recortar; los más
# largos se descartan por ruido (criterio 7.6). Es más estricto que el
# ``maxLength: 60`` del esquema, que acota el objeto *antes* de normalizar.
MAX_SENTIMENTO_LEN: Final = 40


def iter_candidate_objects(raw: str) -> Iterator[dict[str, Any]]:
    """Produce los objetos JSON candidatos del texto, en orden de aparición.

    Recorre los primeros :data:`MAX_SCAN_CHARS` caracteres de ``raw`` y, por cada
    llave de apertura ``{`` encontrada en orden creciente de posición, intenta
    extraer el subtexto con llaves balanceadas que empieza en ella y parsearlo con
    el decodificador JSON estándar. Los subtextos que no parsean (objeto truncado,
    comillas simples, coma final, etc.) se omiten en silencio; el escáner avanza
    al siguiente ``{``. Solo se emiten los candidatos que parsean a un objeto
    JSON (``dict``).

    El recorrido respeta los literales de cadena JSON y sus secuencias de escape,
    de modo que una ``{`` o una ``}`` dentro de una cadena no altera el conteo de
    anidamiento. Esto permite distinguir correctamente objetos concatenados
    (``{...}{...}``) y objetos con llaves embebidas en sus valores de texto.

    Es un generador perezoso y **total**: no lanza ninguna excepción para ninguna
    entrada; a lo sumo no emite ningún candidato.

    Args:
        raw: el texto crudo devuelto por el Modelo_Bedrock.

    Yields:
        Cada objeto JSON (``dict``) que aparece como subtexto balanceado dentro de
        la ventana de inspección, en orden creciente de la posición de su ``{``.
    """
    window = raw[:MAX_SCAN_CHARS]
    length = len(window)
    start = 0
    while True:
        open_pos = window.find("{", start)
        if open_pos == -1:
            return
        end_pos = _find_balanced_end(window, open_pos, length)
        if end_pos is not None:
            fragment = window[open_pos : end_pos + 1]
            try:
                parsed = json.loads(fragment)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                yield parsed
        # Avanza al siguiente candidato posible. Buscar desde open_pos + 1
        # garantiza progreso y permite descubrir objetos anidados o concatenados
        # cuando el candidato exterior no parseó.
        start = open_pos + 1


def _find_balanced_end(text: str, open_pos: int, length: int) -> int | None:
    """Devuelve el índice de la ``}`` que cierra la ``{`` de ``open_pos``, o ``None``.

    Cuenta el anidamiento de llaves desde ``open_pos`` respetando los literales de
    cadena JSON (``"..."``) y sus escapes (``\\``), de modo que las llaves dentro
    de una cadena no afectan el balance. Si el texto se agota antes de cerrar
    todas las llaves abiertas (objeto truncado), devuelve ``None``.

    Args:
        text: la ventana de texto que se está escaneando.
        open_pos: índice de la llave de apertura ``{`` desde la que balancear.
        length: longitud de ``text`` (precalculada por el llamador).

    Returns:
        El índice de la llave de cierre correspondiente, o ``None`` si no existe
        dentro de ``text``.
    """
    depth = 0
    in_string = False
    escaped = False
    pos = open_pos
    while pos < length:
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    return None


def extract_json_object(raw: str) -> dict[str, Any]:
    """Extrae el primer objeto JSON parseable del texto crudo del modelo.

    Tolera los delimitadores de bloque de código, la prosa anterior o posterior y
    la envoltura del objeto en un array de un solo elemento (``[ {...} ]``). No
    aplica validación de esquema ni normalización: devuelve el primer ``dict`` que
    aparece como objeto balanceado, o el ``dict`` contenido en un array de un
    solo elemento si ese array precede a cualquier objeto suelto.

    Esta función es un auxiliar de inspección y de pruebas. El camino de
    producción es :func:`parse_llm_output`, que selecciona el primer candidato
    **conforme al esquema** (criterio 7.4), no simplemente el primero parseable.

    Args:
        raw: el texto crudo devuelto por el Modelo_Bedrock.

    Returns:
        El primer objeto JSON (``dict``) hallado en la ventana de inspección.

    Raises:
        LlmSchemaError: si el texto no contiene, dentro de la ventana, ningún
            objeto JSON sintácticamente completo (``error_code =
            LLM_OUTPUT_INVALID``).
    """
    for candidate in iter_candidate_objects(raw):
        return candidate
    # Caso de envoltura en array de un solo elemento: ``[ {...} ]``. El escáner
    # de objetos ya habría emitido el ``{...}`` interior, salvo que el array
    # contenga algo que no sea un objeto. Si llegamos aquí, no hubo ningún objeto.
    raise LlmSchemaError("no_json_object_found", raw_length=len(raw))


def normalize_sentimientos(value: object) -> tuple[str, ...]:
    """Normaliza la lista ``sentimientos`` de un objeto adoptado (criterio 7.6).

    Aplica, en este orden, las reglas del criterio 7.6 sobre el valor crudo del
    campo ``sentimientos``:

    1. Descarta los elementos que no son cadenas.
    2. Recorta los espacios inicial y final de cada elemento y lo pasa a
       minúsculas.
    3. Descarta los que quedan vacíos tras el recorte y los que exceden
       :data:`MAX_SENTIMENTO_LEN` caracteres.
    4. Elimina los duplicados conservando la primera aparición de cada valor.
    5. Retiene como máximo los primeros :data:`MAX_SENTIMIENTOS` valores
       distintos.

    Es una función total: para cualquier entrada —incluido un valor que no sea
    una lista— devuelve una tupla (posiblemente vacía) sin lanzar.

    Args:
        value: el valor crudo del campo ``sentimientos`` del objeto adoptado.
            Normalmente una ``list`` de ``str``, pero se tolera cualquier tipo.

    Returns:
        Una tupla de emociones normalizadas, deduplicada, en orden de primera
        aparición y de longitud a lo sumo :data:`MAX_SENTIMIENTOS`.
    """
    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for element in value:
        if not isinstance(element, str):
            continue
        normalized = element.strip().lower()
        if not normalized or len(normalized) > MAX_SENTIMENTO_LEN:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) == MAX_SENTIMIENTOS:
            break
    return tuple(result)


def parse_llm_output(
    raw: str,
    item_id: str,
    *,
    model_id: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> EmotionAnalysis:
    """Convierte el texto crudo del modelo en un ``EmotionAnalysis``, o falla.

    Es la función **total** del parseo (criterio 7.13): para cualquier ``raw``
    posible —incluidas la cadena vacía, la cadena de solo espacios, un texto sin
    JSON, un objeto truncado, objetos concatenados y objetos con comillas
    simples— termina en exactamente uno de dos desenlaces observables: devuelve un
    :class:`EmotionAnalysis` conforme al JSON_Schema ``llm_output`` ya
    normalizado, o lanza :class:`LlmSchemaError` (``error_code =
    LLM_OUTPUT_INVALID``). Nunca devuelve ``None`` ni propaga ninguna otra
    excepción por un fallo de parseo o de validación.

    Procedimiento (criterios 7.4 a 7.7):

    1. Recorre los candidatos de :func:`iter_candidate_objects` en orden de
       aparición y adopta el **primero** que valida contra el esquema
       ``llm_output`` (criterio 7.4). No repara ni reescribe el texto.
    2. Si ningún candidato valida, lanza :class:`LlmSchemaError` (criterio 7.5).
    3. Normaliza la lista ``sentimientos`` del objeto adoptado (criterio 7.6).
    4. Si la lista normalizada queda vacía, fija ``explicacion`` a la cadena vacía
       (criterio 7.7); en caso contrario conserva la ``explicacion`` del objeto.

    Los metadatos de la invocación (``model_id``, ``input_tokens``,
    ``output_tokens``) no se derivan del texto: los aporta el adaptador de Bedrock
    que llamó al modelo. Se reciben como argumentos de palabra clave con valores
    por defecto neutros para que esta función siga siendo pura y testeable sin
    infraestructura.

    Args:
        raw: el texto crudo devuelto por el Modelo_Bedrock.
        item_id: identificador del Ítem al que pertenece el resultado; se copia
            tal cual al :class:`EmotionAnalysis` producido.
        model_id: identificador del Modelo_Bedrock que generó ``raw`` (lo aporta
            el adaptador). Por defecto, cadena vacía.
        input_tokens: tokens de entrada informados por la invocación; 0 si no se
            informa (criterio 7.11). Por defecto, 0.
        output_tokens: tokens de salida informados por la invocación; 0 si no se
            informa. Por defecto, 0.

    Returns:
        Un :class:`EmotionAnalysis` con los ``sentimientos`` normalizados y la
        ``explicacion`` acorde a la regla de la lista vacía.

    Raises:
        LlmSchemaError: si el texto no contiene, dentro de la ventana de
            inspección, ningún objeto JSON que cumpla el JSON_Schema
            ``llm_output`` (``error_code = LLM_OUTPUT_INVALID``). Es la **única**
            excepción que esta función puede propagar.
    """
    validator = get_validator(LLM_OUTPUT_SCHEMA_FILE)

    adopted: dict[str, Any] | None = None
    for candidate in iter_candidate_objects(raw):
        if validator.is_valid(candidate):
            adopted = candidate
            break

    if adopted is None:
        raise LlmSchemaError("no_schema_valid_object", raw_length=len(raw))

    sentimientos = normalize_sentimientos(adopted.get("sentimientos"))

    # Criterio 7.7: una lista normalizada vacía fuerza una explicación vacía. En
    # otro caso se conserva la ``explicacion`` del objeto adoptado, que el esquema
    # garantiza que es una cadena.
    if not sentimientos:
        explicacion = ""
    else:
        raw_explicacion = adopted.get("explicacion", "")
        explicacion = raw_explicacion if isinstance(raw_explicacion, str) else ""

    return EmotionAnalysis(
        item_id=item_id,
        sentimientos=sentimientos,
        explicacion=explicacion,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
