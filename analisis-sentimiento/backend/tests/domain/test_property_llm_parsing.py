"""Property 12 — Totalidad del parseo de la salida del LLM (tarea 4.3).

**Validates: Requirements 7.4, 7.5, 7.13**

Enunciado (design.md, sección 19, Property 12):

    Toda cadena posible devuelta por el modelo produce o un ``EmotionAnalysis``
    válido o un ``LlmSchemaError`` (``LLM_OUTPUT_INVALID``). Nunca una excepción
    distinta.

Esta es la propiedad de **totalidad** de :func:`sentiment.domain.llm_parsing.parse_llm_output`.
El Modelo_Bedrock es no determinista y puede devolver cualquier texto: prosa,
basura, JSON truncado, bloques delimitados por fences, objetos concatenados,
cadenas gigantescas o Unicode arbitrario (criterio 7.13). El parseo debe
«domesticar» ese espacio de entradas colapsándolo en exactamente **dos**
desenlaces observables, de modo que la Maquina_De_Estados nunca reciba un
``None`` ni una excepción inesperada que escape de la clasificación
transitorio/permanente del Catalogo_De_Errores:

1. **Éxito** — devuelve un :class:`EmotionAnalysis` conforme al JSON_Schema
   ``llm_output`` y ya normalizado (criterios 7.4, 7.6, 7.7):
   ``sentimientos`` es una tupla deduplicada, en minúsculas, de longitud a lo
   sumo :data:`MAX_SENTIMIENTOS`; y una lista vacía fuerza ``explicacion`` vacía.

2. **Rechazo explícito** — lanza :class:`LlmSchemaError` con
   ``error_code == "LLM_OUTPUT_INVALID"`` (criterio 7.5).

Nunca hay un tercer desenlace: la función no devuelve ``None`` ni propaga
ninguna otra clase de excepción (criterio 7.13).

Diseño de las estrategias: para que la propiedad tenga poder de refutación, no
basta con ``st.text()`` (que casi nunca produce un JSON conforme por azar). Se
compone un espacio de entradas que mezcla, en proporciones deliberadas:

* texto arbitrario y Unicode (:func:`arbitrary_text`), incluidos sustitutos y
  caracteres de control, que ejercitan el camino de rechazo y el escáner de
  llaves ante basura;
* objetos ``llm_output`` **válidos** serializados (:func:`valid_json_strings`),
  que ejercitan el camino de éxito y las invariantes del resultado;
* objetos **casi válidos** (:func:`near_valid_json_strings`): conformes salvo
  por mutaciones puntuales (campo ausente, tipo equivocado, ``sentimientos`` con
  ruido que la normalización debe limpiar), que ejercitan la frontera entre
  éxito y rechazo;
* **envolturas** (:func:`wrapped`): fences ```` ```json ````, prosa alrededor,
  array de un elemento y objetos concatenados, que ejercitan la tolerancia del
  escáner (criterio 7.4);
* **corrupciones** (:func:`corrupted`): truncado, inyección de bytes, duplicado,
  que empujan cualquiera de los anteriores hacia el espacio de lo malformado.

El ``item_id`` que recibe :func:`parse_llm_output` no influye en el desenlace del
parseo (se copia tal cual al resultado), así que se fija a una constante para
concentrar la generación en el único argumento que determina el comportamiento:
``raw``.
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sentiment.domain.errors import LlmSchemaError
from sentiment.domain.llm_parsing import (
    MAX_SENTIMENTO_LEN,
    MAX_SENTIMIENTOS,
    parse_llm_output,
)
from sentiment.domain.models import EmotionAnalysis

# El resultado copia este identificador sin interpretarlo; es irrelevante para el
# desenlace del parseo, por lo que se fija y no se genera.
ITEM_ID = "01JDQZ8F3K7XWQ2M5N8P4R6T9V#00042"


# --- Estrategias base ------------------------------------------------------

# Texto arbitrario de todo el repertorio Unicode, incluidos caracteres de
# control y sustitutos. Es el caso adverso puro: cadenas que el escáner de llaves
# recorre buscando objetos balanceados y que, salvo coincidencia astronómica, no
# contienen ningún objeto conforme al esquema.
arbitrary_text = st.text(max_size=2_000)


def _sentimiento_token() -> st.SearchStrategy[str]:
    """Genera un posible elemento de ``sentimientos`` (válido o ruidoso).

    Mezcla valores que la normalización conserva con valores que descarta
    (vacíos, solo espacios, demasiado largos, con mayúsculas y espacios que
    requieren trim), para ejercitar :func:`normalize_sentimientos` extremo a
    extremo a través de :func:`parse_llm_output`.
    """
    return st.one_of(
        st.text(
            alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
            min_size=2,
            max_size=MAX_SENTIMENTO_LEN,
        ),
        # Variantes que la normalización debe recortar / pasar a minúsculas.
        st.sampled_from(
            [
                "  Alegría  ",
                "GRATITUD",
                "alegria",
                "ALEGRÍA",
                " calma ",
                "miedo",
            ]
        ),
        # Ruido que la normalización descarta: vacío, espacios, excesivamente largo.
        st.sampled_from(["", "   ", "x" * (MAX_SENTIMENTO_LEN + 5), "y" * 70]),
    )


def _valid_object() -> st.SearchStrategy[dict[str, object]]:
    """Objeto conforme al JSON_Schema ``llm_output``.

    Respeta la regla de negocio del esquema (lista vacía ⇒ explicación vacía)
    para que el objeto sea aceptado como candidato, de modo que estas entradas
    recorran el camino de éxito y disparen la verificación de invariantes del
    resultado.
    """
    sentimientos = st.lists(
        st.text(
            alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
            min_size=2,
            max_size=20,
        ),
        min_size=0,
        max_size=10,
        unique=True,
    )
    return sentimientos.flatmap(
        lambda sents: st.fixed_dictionaries(
            {
                "Indificador": st.text(min_size=1, max_size=40),
                "Respuesta": st.text(min_size=0, max_size=120),
                "sentimientos": st.just(sents),
                # Si la lista es vacía, la explicación DEBE ser vacía (regla del
                # esquema); si no, puede ser cualquier texto acotado.
                "explicacion": st.just("")
                if not sents
                else st.text(min_size=0, max_size=200),
            }
        )
    )


def _near_valid_object() -> st.SearchStrategy[dict[str, object]]:
    """Objeto *casi* conforme: conforme con mutaciones puntuales.

    Parte de un objeto con los cuatro campos y luego, de forma aleatoria:

    * omite un campo requerido (⇒ no conforme, debe rechazarse);
    * cambia el tipo de un campo (⇒ no conforme);
    * inyecta en ``sentimientos`` ruido que la normalización debe limpiar
      (duplicados, mayúsculas, espacios, elementos no-cadena, exceso de 10),
      conservando por lo demás la conformidad del esquema cuando es posible.

    Son los casos frontera entre éxito y rechazo: fuerzan a la propiedad a
    distinguir ambos desenlaces sin un tercero.
    """
    base: dict[str, object] = {
        "Indificador": "Satisfacción en el Puesto",
        "Respuesta": "texto de la respuesta",
        "sentimientos": ["alegria", "gratitud"],
        "explicacion": "una explicación cualquiera",
    }

    def _mutate(seed: int) -> dict[str, object]:
        obj = dict(base)
        kind = seed % 5
        if kind == 0:
            obj.pop("explicacion", None)  # falta un requerido
        elif kind == 1:
            obj["sentimientos"] = "no-es-lista"  # tipo equivocado
        elif kind == 2:
            # Ruido que la normalización limpia; el esquema puede rechazarlo por
            # elementos no-string, lo que lo vuelve un caso de rechazo legítimo.
            obj["sentimientos"] = ["Alegría", "alegría", "ALEGRÍA", "  calma  "]
        elif kind == 3:
            obj["Indificador"] = 12345  # tipo equivocado
        else:
            obj["extra"] = "campo no permitido"  # additionalProperties: false
        return obj

    return st.integers(min_value=0, max_value=10_000).map(_mutate)


# --- Estrategias de cadenas crudas -----------------------------------------

valid_json_strings = _valid_object().map(json.dumps)
near_valid_json_strings = _near_valid_object().map(lambda o: json.dumps(o, default=str))


@st.composite
def wrapped(draw: st.DrawFn) -> str:
    """Envuelve una cadena JSON como lo haría un LLM verboso (criterio 7.4).

    Aplica una de varias envolturas que el escáner debe tolerar: fences de
    Markdown, prosa anterior/posterior, array de un solo elemento u objetos
    concatenados. El objeto base es válido o casi válido con igual probabilidad.
    """
    payload = draw(st.one_of(valid_json_strings, near_valid_json_strings))
    style = draw(st.integers(min_value=0, max_value=4))
    if style == 0:
        return f"```json\n{payload}\n```"
    if style == 1:
        prefix = draw(st.text(max_size=80))
        suffix = draw(st.text(max_size=80))
        return f"{prefix}\n{payload}\n{suffix}"
    if style == 2:
        return f"[{payload}]"
    if style == 3:
        other = draw(st.one_of(valid_json_strings, near_valid_json_strings))
        return payload + other
    return payload


@st.composite
def corrupted(draw: st.DrawFn) -> str:
    """Corrompe una cadena (envuelta o cruda) para empujarla a lo malformado.

    Trunca, inyecta bytes de ruido o duplica fragmentos. Cubre el JSON truncado,
    las llaves desbalanceadas y la basura intercalada, que deben desembocar en
    :class:`LlmSchemaError` sin otra excepción.
    """
    base = draw(st.one_of(valid_json_strings, near_valid_json_strings, wrapped()))
    if not base:
        return base
    mode = draw(st.integers(min_value=0, max_value=2))
    if mode == 0:
        cut = draw(st.integers(min_value=0, max_value=len(base)))
        return base[:cut]  # truncado
    if mode == 1:
        pos = draw(st.integers(min_value=0, max_value=len(base)))
        noise = draw(st.text(max_size=40))
        return base[:pos] + noise + base[pos:]  # inyección
    return base + base  # duplicado (objetos concatenados / llaves extra)


# Espacio de entradas completo: pondera el texto arbitrario (camino de rechazo)
# junto con los objetos válidos, casi válidos, envueltos y corrompidos (fronteras
# entre éxito y rechazo), más una pequeña fracción de cadenas muy largas.
raw_strings = st.one_of(
    arbitrary_text,
    valid_json_strings,
    near_valid_json_strings,
    wrapped(),
    corrupted(),
    # Cadenas largas: ejercitan la ventana de inspección y el coste del escáner.
    st.text(min_size=5_000, max_size=15_000),
)


# --- Verificación de invariantes del resultado -----------------------------


def _assert_success_invariants(result: EmotionAnalysis) -> None:
    """Comprueba las invariantes de un desenlace de éxito (criterios 7.4, 7.6, 7.7).

    Son exactamente las que exige el enunciado de P12 más la forma del tipo:

    * el resultado es un :class:`EmotionAnalysis`;
    * ``sentimientos`` es una ``tuple`` de ``str``;
    * está deduplicada (sin repetidos);
    * está en minúsculas y recortada (normalización del criterio 7.6);
    * tiene a lo sumo :data:`MAX_SENTIMIENTOS` elementos;
    * si está vacía, ``explicacion`` es la cadena vacía (criterio 7.7).
    """
    assert isinstance(result, EmotionAnalysis)
    assert isinstance(result.sentimientos, tuple)
    assert all(isinstance(s, str) for s in result.sentimientos)
    # Deduplicado: longitud igual a la del conjunto.
    assert len(result.sentimientos) == len(set(result.sentimientos))
    # Normalización: minúsculas y sin espacios en los extremos.
    assert all(s == s.lower() for s in result.sentimientos)
    assert all(s == s.strip() for s in result.sentimientos)
    assert all(s != "" for s in result.sentimientos)
    # Tope de 10 (criterio 7.6).
    assert len(result.sentimientos) <= MAX_SENTIMIENTOS
    # Regla de negocio de la lista vacía (criterio 7.7).
    if not result.sentimientos:
        assert result.explicacion == ""
    # La explicación siempre es una cadena.
    assert isinstance(result.explicacion, str)


# --- (a) Totalidad: solo dos desenlaces, nunca otra excepción --------------


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(raw=raw_strings)
def test_parse_llm_output_is_total(raw: str) -> None:
    """Para cualquier ``raw`` el parseo termina en éxito o en ``LlmSchemaError``.

    Es el núcleo de P12 (criterio 7.13): ``parse_llm_output`` nunca devuelve
    ``None`` y nunca propaga una excepción distinta de :class:`LlmSchemaError`.
    Cualquier otra excepción que escape hace fallar la prueba, delatando una
    violación de totalidad. En el camino de éxito se verifican además las
    invariantes del resultado (criterios 7.4, 7.6, 7.7).
    """
    try:
        result = parse_llm_output(raw, ITEM_ID)
    except LlmSchemaError as exc:
        # Rechazo explícito: el único error admisible, con el código del catálogo.
        assert exc.error_code == "LLM_OUTPUT_INVALID"
        return

    # Éxito: nunca None, y con todas las invariantes del resultado.
    assert result is not None
    _assert_success_invariants(result)


# --- (b) Determinismo: misma entrada ⇒ mismo desenlace ---------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(raw=raw_strings)
def test_parse_llm_output_is_deterministic(raw: str) -> None:
    """La misma cadena produce siempre el mismo desenlace observable.

    El parseo es una función pura del texto (no usa reloj ni aleatoriedad), así
    que dos invocaciones con el mismo ``raw`` deben coincidir: ambas lanzan
    :class:`LlmSchemaError`, o ambas devuelven el mismo :class:`EmotionAnalysis`.
    El determinismo es lo que permite clasificar ``LLM_OUTPUT_INVALID`` como
    permanente (criterio 9.17): reintentar no cambiaría el resultado.
    """

    def _run() -> EmotionAnalysis | type[LlmSchemaError]:
        try:
            return parse_llm_output(raw, ITEM_ID)
        except LlmSchemaError:
            return LlmSchemaError

    first = _run()
    second = _run()

    if first is LlmSchemaError or second is LlmSchemaError:
        assert first is second
    else:
        assert isinstance(first, EmotionAnalysis)
        assert isinstance(second, EmotionAnalysis)
        assert first == second


# --- (c) El camino de éxito es alcanzable y sus invariantes se sostienen ----


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(obj=_valid_object())
def test_valid_objects_always_parse_successfully(obj: dict[str, object]) -> None:
    """Todo objeto conforme (envuelto como JSON suelto) parsea con éxito.

    Garantiza que el camino de éxito no es vacío: para cualquier objeto que
    cumple el JSON_Schema ``llm_output``, ``parse_llm_output`` devuelve un
    :class:`EmotionAnalysis` que satisface las invariantes de P12. Sin esto, una
    función que siempre lanzara pasaría trivialmente la prueba de totalidad.
    """
    result = parse_llm_output(json.dumps(obj), ITEM_ID)
    _assert_success_invariants(result)

    # Las emociones del resultado son la normalización de las del objeto: cada una
    # proviene (en minúsculas y recortada) de algún elemento del objeto original.
    raw_sentimientos = obj["sentimientos"]
    assert isinstance(raw_sentimientos, list)
    original = {s.strip().lower() for s in raw_sentimientos if isinstance(s, str)}
    assert set(result.sentimientos) <= original


# --- (d) Las envolturas tolerables no rompen la totalidad -------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(raw=wrapped())
def test_wrapped_payloads_are_total(raw: str) -> None:
    """Fences, prosa, arrays y concatenaciones siguen produciendo dos desenlaces.

    Aísla el espacio de las envolturas del criterio 7.4 para confirmar que la
    tolerancia del escáner no introduce excepciones inesperadas: cada entrada
    envuelta termina en éxito (con invariantes) o en :class:`LlmSchemaError`.
    """
    try:
        result = parse_llm_output(raw, ITEM_ID)
    except LlmSchemaError as exc:
        assert exc.error_code == "LLM_OUTPUT_INVALID"
        return
    _assert_success_invariants(result)
