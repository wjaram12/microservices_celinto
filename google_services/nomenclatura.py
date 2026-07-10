"""
Cómo se construye la dirección de correo de una persona nueva.

Las reglas NO son inventadas: se dedujeron midiendo las 25 106 cuentas vivas del
dominio. El 80,5 % son `nombre.apellido`; el resto son los peldaños a los que se
recurre cuando esa dirección ya está ocupada:

    nombre.apellido              20 201   80,5 %   <- forma canónica
    nombre1nombre2.apellido         674    2,7 %
    inicial+apellido                476    1,9 %   <- forma antigua (aaragundi@)
    nombre.apellido2                246    1,0 %
    ...y 272 direcciones que acaban en dígito (alex.vargas2@)

La colisión no es rara: en el cruce con los sistemas aparecieron 313 personas cuya
dirección canónica ya pertenecía a OTRA persona (dos «José Caballero» distintos).
Por eso cada peldaño se comprueba EN VIVO contra Google antes de proponerlo: la
tabla de vínculos es un índice de las cuentas que conocemos, no de todas las que
existen (buzones de grupo, cuentas de sistema, alias).

La forma `inicial+apellido` se deja fuera de la escalera: es la nomenclatura vieja
que la universidad abandonó, y generar cuentas nuevas con ella perpetuaría la
confusión de las cuentas duplicadas que ya tiene el dominio.
"""
import logging
import re
import unicodedata

from .config import settings
from .errores import ErrorDeValidacion

logger = logging.getLogger(__name__)

# Cuántos sufijos numéricos se prueban antes de rendirse (…2, …3, …4).
MAX_SUFIJO = 9


def _normalizar(texto: str) -> list:
    """Palabras en minúscula, sin acentos ni signos. 'BAÑOS AYALA' -> ['banos','ayala']."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z\s]", " ", t)
    return [p for p in t.lower().split() if p]


def candidatos(nombres: str, apellidos: str) -> list:
    """
    Peldaños de la escalera, en orden de preferencia. Solo la parte local, sin dominio.

    Las partículas de los apellidos compuestos ('de la cruz') se pegan al apellido en
    vez de tratarse como palabras sueltas: si no, salen direcciones como `cruz.de@`,
    que es justo la basura que encontramos en la tabla de personas.
    """
    nom = _normalizar(nombres)
    ape = _normalizar(apellidos)
    if not nom or not ape:
        raise ErrorDeValidacion("Se requieren nombres y apellidos para proponer un correo.")

    # 'DE LA CRUZ ALCIVAR' -> ['delacruz', 'alcivar']
    particulas = {"de", "del", "la", "las", "los", "san", "santa", "da", "do", "di", "van", "von"}
    compuesto, agrupados = [], []
    for palabra in ape:
        if palabra in particulas:
            compuesto.append(palabra)
        else:
            agrupados.append("".join(compuesto + [palabra]))
            compuesto = []
    if compuesto:                      # el apellido acabó en partícula: se pega al último
        agrupados[-1] = agrupados[-1] + "".join(compuesto) if agrupados else "".join(compuesto)
    ape = agrupados or ape

    opciones = [f"{nom[0]}.{ape[0]}"]
    if len(nom) > 1:
        opciones.append(f"{''.join(nom[:2])}.{ape[0]}")
    if len(ape) > 1:
        opciones.append(f"{nom[0]}.{ape[1]}")
        opciones.append(f"{nom[0]}.{''.join(ape[:2])}")

    # Sufijos numéricos como último recurso, sobre la forma canónica.
    opciones += [f"{opciones[0]}{i}" for i in range(2, MAX_SUFIJO + 1)]

    vistos, unicos = set(), []
    for o in opciones:
        if o not in vistos:
            vistos.add(o)
            unicos.append(o)
    return unicos


def sugerir(nombres: str, apellidos: str, dominio: str = "") -> dict:
    """
    Primera dirección LIBRE para esta persona, comprobada en vivo contra Google.

    Devuelve {correo, patron, intentos, ocupados}. `ocupados` lista las direcciones
    que se descartaron y de quién son: el sistema que llama necesita saberlo para
    explicar por qué a Juan Pérez le tocó `juan.perez2@`.

    No crea nada. Entre esta llamada y la creación, otro proceso podría tomar la
    dirección: por eso la creación vuelve a comprobarla dentro del cerrojo.
    """
    from .cliente import obtener_directorio

    dominio = (dominio or settings.GOOGLE_DOMINIO).strip().lstrip("@")
    usuarios = obtener_directorio().usuarios

    ocupados = []
    for i, local in enumerate(candidatos(nombres, apellidos), 1):
        correo = f"{local}@{dominio}"
        existente = usuarios.obtener(correo)
        if existente is None:
            return {"correo": correo, "patron": local, "intentos": i, "ocupados": ocupados}
        ocupados.append({
            "correo": correo,
            "pertenece_a": (existente.get("name") or {}).get("fullName") or "",
        })

    raise ErrorDeValidacion(
        f"No se encontró una dirección libre para '{nombres} {apellidos}' tras "
        f"{len(ocupados)} intentos. Asígnala a mano.")
