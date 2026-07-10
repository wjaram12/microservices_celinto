"""
Cómo se compara la identidad de una persona: cédulas y nombres.

Vive aquí porque lo usan dos cosas que **deben coincidir siempre**: el backfill
masivo (que cruza decenas de miles de personas contra un volcado del directorio) y
el endpoint de auditoría individual (que pregunta a Google en vivo). Si divergieran,
el informe diría una cosa y la API otra sobre la misma persona.

Cada regla salió de un problema real de los datos, no de una suposición.
"""
import re
import unicodedata
from difflib import SequenceMatcher

# Similitud mínima para dar por buena una coincidencia difusa de nombres. Alto a
# propósito: vincular a la persona equivocada es un error silencioso y difícil de
# deshacer. Por debajo de esto, la fila va a revisión humana.
UMBRAL_NOMBRE = 0.93

# Partículas de los apellidos compuestos. Sin esto, 'DE LA CRUZ ALCIVAR' se rompe en
# palabras sueltas y produce direcciones como `cruz.de@`, que es la basura que había
# en la tabla de personas.
PARTICULAS = {"de", "del", "la", "las", "los", "san", "santa", "da", "do", "di",
              "van", "von"}


def normalizar(texto: str) -> str:
    """Mayúsculas, sin acentos, sin espacios repetidos. Para comparar nombres."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(t.upper().split())


def clave_nombre(texto: str) -> str:
    """Nombre normalizado con las palabras ORDENADAS.

    Hace que 'JARA MORAN WALTER JAVIER' y 'WALTER JAVIER JARA MORAN' colisionen, que
    es lo que hace falta al comparar tablas que ordenan nombre y apellido al revés.
    """
    return " ".join(sorted(normalizar(texto).split()))


def variantes_cedula(valor: str) -> list:
    """
    Formas en que una misma cédula puede estar escrita en Google.

    En el dominio hay 142 cédulas guardadas SIN el cero inicial ('925122673' en vez
    de '0925122673'), por haberse guardado como número entero. La búsqueda de Google
    es por cadena exacta, así que hay que probar las dos formas o se crean duplicados.
    """
    v = (valor or "").strip()
    if not v:
        return []
    formas = {v, v.lstrip("0")}
    if v.isdigit() and len(v) < 10:
        formas.add(v.zfill(10))
    return [f for f in formas if f]


def canonizar_cedula(cedula: str) -> str:
    """Forma canónica para comparar. Solo se rellena la de 9 dígitos: es el error
    conocido. Un documento de 6, 7 u 8 dígitos es extranjero y rellenarlo lo
    convertiría en otro número."""
    c = (cedula or "").strip().upper()
    if c.isdigit() and len(c) == 9:
        return c.zfill(10)
    return c


def cedula_invalida(cedula: str) -> bool:
    """Valores de relleno que jamás deben acabar escritos en Google.

    En las tablas reales aparecen '0000000000' y '00000000' en filas que ni siquiera
    son personas: 'DIRECCIÓN GENERAL ACADÉMICA', 'Place to pay', 'Nuvei', 'PICCA'.
    Escribirlos sería peor que dejar la cuenta sin cédula: parecerían un dato bueno.
    """
    c = (cedula or "").strip()
    if not c:
        return True
    return c.isdigit() and len(set(c)) == 1


def agrupar_apellidos(apellidos: str) -> list:
    """'DE LA CRUZ ALCIVAR' -> ['delacruz', 'alcivar']."""
    palabras = [p for p in re.sub(r"[^A-Za-z\s]", " ", normalizar(apellidos)).lower().split()]
    compuesto, agrupados = [], []
    for palabra in palabras:
        if palabra in PARTICULAS:
            compuesto.append(palabra)
        else:
            agrupados.append("".join(compuesto + [palabra]))
            compuesto = []
    if compuesto:                       # acabó en partícula: se pega al último
        if agrupados:
            agrupados[-1] += "".join(compuesto)
        else:
            agrupados = ["".join(compuesto)]
    return agrupados


def casa_nombre(nombre_a: str, nombre_b: str) -> bool:
    """
    ¿Los dos nombres son de la MISMA persona?

    No basta comparar las cadenas: en Google los nombres vienen truncados
    ('MAXIMO ANDRADE MURILLO' frente a 'MAXIMO JAVIER ANDRADE MURILLO'), y exigir un
    93 % de similitud marcaba a esas personas como intrusas en su propia cuenta —
    1 114 falsos positivos en el primer cruce, frente a 552 conflictos reales.

    Por eso se comparan CONJUNTOS DE PALABRAS: si el nombre más corto está contenido
    en el más largo, es la misma persona. Se exigen al menos dos palabras en común,
    porque un solo nombre de pila coincide demasiado a menudo entre desconocidos.
    """
    a_norm, b_norm = normalizar(nombre_a), normalizar(nombre_b)
    if not a_norm or not b_norm:
        return False
    if clave_nombre(a_norm) == clave_nombre(b_norm):
        return True

    a, b = set(a_norm.split()), set(b_norm.split())
    menor, mayor = (a, b) if len(a) <= len(b) else (b, a)
    if len(menor) >= 2 and menor <= mayor:
        return True

    return SequenceMatcher(None, a_norm, b_norm).ratio() >= UMBRAL_NOMBRE


def parecido(nombre_a: str, nombre_b: str) -> float:
    """Similitud 0-1 entre dos nombres normalizados. Para informar, no para decidir."""
    return SequenceMatcher(None, normalizar(nombre_a), normalizar(nombre_b)).ratio()
