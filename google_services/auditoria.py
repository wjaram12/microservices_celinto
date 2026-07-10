"""
Auditoría de UNA persona contra Google, en vivo. Solo lee: nunca escribe.

Es el equivalente individual del veredicto que el backfill emite para decenas de
miles de personas de golpe. Comparte con él las reglas de identidad
(google_services.identidad) y de jerarquía (google_services.jerarquia), así que un
mismo caso recibe el mismo estado por las dos vías.

La diferencia es la fuente: el backfill cruza contra un volcado del directorio (una
pasada, ~90 s, 27 686 cuentas); esto pregunta a Google en cada llamada, porque un
sistema que va a dar de alta a alguien necesita el dato de HOY, no el de esta mañana.

Cuesta entre 1 y 4 llamadas a Google (unos 0,5 s cada una) según lo lejos que haya
que bajar en la escalera de búsqueda:

    1. por cédula   (externalId, probando las variantes con y sin cero inicial)
    2. por correo   (la llave exacta que el sistema cliente ya tiene)
    3. por nombre   (último recurso; la API exige el nombre completo exacto)
"""
import logging

from .identidad import (
    canonizar_cedula, casa_nombre, cedula_invalida, clave_nombre, normalizar,
    parecido, variantes_cedula,
)
from .jerarquia import es_persona, principal, rango

logger = logging.getLogger(__name__)

TIPO_CEDULA = "identificacion"


def cedula_de(usuario: dict):
    """Cédula que la cuenta lleva escrita en Google, o None."""
    for e in (usuario.get("externalIds") or []):
        if (e.get("customType") or e.get("type")) == TIPO_CEDULA:
            return (e.get("value") or "").strip() or None
    return None


def _resumen(usuario: dict) -> dict:
    return {
        "google_id": usuario.get("id"),
        "email": (usuario.get("primaryEmail") or "").lower(),
        "nombre": (usuario.get("name") or {}).get("fullName") or "",
        "ou": usuario.get("orgUnitPath") or "/",
        "suspendida": bool(usuario.get("suspended")),
        "archivada": bool(usuario.get("archived")),
        "cedula_en_google": cedula_de(usuario),
    }


def _unicos(cuentas: list) -> list:
    vistos, salida = set(), []
    for u in cuentas:
        if u and u["id"] not in vistos:
            vistos.add(u["id"])
            salida.append(u)
    return salida


def _por_cedula(usuarios, cedula: str) -> list:
    """Cuentas que llevan esta cédula. `externalId=` ignora el customType, así que se
    confirma que el valor sea realmente la cédula y no otro identificador que coincide."""
    hallados = []
    for forma in variantes_cedula(cedula):
        hallados += usuarios.filtrar(max_resultados=10, consulta=f"externalId={forma}")
    canon = canonizar_cedula(cedula)
    return [u for u in _unicos(hallados)
            if canonizar_cedula(cedula_de(u) or "") == canon]


def _por_nombre(usuarios, nombres: str, apellidos: str) -> list:
    """Cuentas cuyo nombre completo coincide. La API de Google exige el nombre exacto
    (`name:'Walter Jara'` devuelve cero), así que se consulta el completo y, si falla,
    se acota por apellido y se compara aquí."""
    completo = f"{nombres} {apellidos}".strip()
    exactos = usuarios.filtrar(max_resultados=10, consulta=f"name:'{completo}'")
    if exactos:
        return exactos

    primer_apellido = normalizar(apellidos).split()
    if not primer_apellido:
        return []
    candidatos = usuarios.filtrar(max_resultados=50,
                                  consulta=f"familyName:{primer_apellido[0]}*")
    return [u for u in candidatos
            if clave_nombre((u.get("name") or {}).get("fullName") or "") == clave_nombre(completo)]


def auditar(nombres: str, apellidos: str, identificacion: str, correo: str = "") -> dict:
    """
    Veredicto sobre una persona. No escribe nada.

    Estados posibles (los mismos que emite el backfill):

        cedula_invalida        la cédula es un valor de relleno; no identifica a nadie
        vinculada              su cuenta ya lleva la cédula: no hay nada que hacer
        corregir_formato       la lleva mal escrita (sin el cero inicial)
        conflicto_cedula       su cuenta lleva una cédula DISTINTA
        existe_sin_cedula      se identificó su cuenta, pero le falta la cédula
        revisar_multicuenta    tiene varias cuentas y la principal no es la que la identificó
        ambigua               varias cuentas vivas con la misma jerarquía
        correo_ocupado         el correo enviado pertenece a otra persona
        solo_cuentas_inactivas sus únicas cuentas están archivadas o suspendidas
        disponible             no tiene ninguna cuenta: se puede crear
    """
    from .cliente import obtener_directorio

    cedula = (identificacion or "").strip()
    correo = (correo or "").strip().lower()
    completo = f"{nombres} {apellidos}".strip()

    base = {"identificacion": cedula, "metodo": "", "cuenta": None,
            "otras_cuentas": [], "correo_ajeno": None, "detalle": ""}

    if cedula_invalida(cedula):
        return {**base, "estado": "cedula_invalida",
                "detalle": f"'{cedula}' es un valor de relleno, no identifica a nadie."}

    usuarios = obtener_directorio().usuarios

    # --- Anclas fuertes: identifican a la persona sin lugar a dudas ---
    por_ced = _por_cedula(usuarios, cedula)
    cuenta_correo = usuarios.obtener(correo) if correo else None

    # El correo enviado existe, pero ¿es SUYO? Solo lo es si su cédula lo confirma o
    # si el nombre casa. Si no, es la cuenta de un tercero y NO puede entrar en el
    # conjunto de candidatos: mezclarla con las cuentas reales de la persona hace que
    # parezcan dos cuentas suyas empatadas, y el veredicto sale 'ambigua'.
    correo_ajeno = None
    if cuenta_correo:
        suyo = (any(u["id"] == cuenta_correo["id"] for u in por_ced)
                or casa_nombre(completo, (cuenta_correo.get("name") or {}).get("fullName") or ""))
        if not suyo:
            correo_ajeno = _resumen(cuenta_correo)
            cuenta_correo = None

    base["correo_ajeno"] = correo_ajeno
    anclas = _unicos([*por_ced, *([cuenta_correo] if cuenta_correo else [])])

    # --- Hermanas: las otras cuentas de la misma persona. Se buscan SIEMPRE, aunque
    # ya haya ancla: la principal puede ser otra (el correo es el de estudiante, pero
    # la persona también es docente).
    exactas = _por_nombre(usuarios, nombres, apellidos) if completo else []
    cuentas = _unicos([*anclas, *exactas])
    vivas = [u for u in cuentas if es_persona(u)]

    if not vivas:
        if cuentas:
            return {**base, "estado": "solo_cuentas_inactivas",
                    "otras_cuentas": [_resumen(u) for u in cuentas],
                    "detalle": "Sus únicas cuentas están archivadas o suspendidas."}
        # No tiene cuenta. Si además el correo que traía es de otro, hay que decirlo:
        # el sistema de origen guarda un dato que pertenece a un tercero.
        if correo_ajeno:
            return {**base, "estado": "correo_ocupado", "metodo": "correo",
                    "cuenta": correo_ajeno,
                    "detalle": f"El correo '{correo}' pertenece a "
                               f"'{correo_ajeno['nombre']}'. La persona no tiene cuenta."}
        return {**base, "estado": "disponible",
                "detalle": "No tiene ninguna cuenta en el dominio."}

    elegida = principal(vivas)
    empatadas = [u for u in vivas if rango(u) == rango(elegida)]
    otras = [_resumen(u) for u in vivas if u["id"] != elegida["id"]]
    ids_anclas = {u["id"] for u in anclas}
    ids_ced = {u["id"] for u in por_ced}

    if len(empatadas) > 1:
        return {**base, "estado": "ambigua",
                "cuenta": _resumen(elegida), "otras_cuentas": otras,
                "detalle": "Varias cuentas activas con la misma jerarquía; hay que "
                           "elegir a mano."}

    if not anclas:
        metodo = "nombre"
        similitud = parecido(completo, (elegida.get("name") or {}).get("fullName") or "")
        if similitud < 1.0:
            return {**base, "estado": "existe_sin_cedula", "metodo": "nombre",
                    "cuenta": _resumen(elegida), "otras_cuentas": otras,
                    "detalle": f"Identificada solo por el nombre (similitud {similitud:.2f})."}
    else:
        metodo = "cedula" if elegida["id"] in ids_ced else "correo"

    # La principal no es la que la identificó: la persona tiene otra cuenta de más
    # rango. Que el nombre coincida lo sugiere, pero nada lo PRUEBA.
    if anclas and elegida["id"] not in ids_anclas:
        ancla = anclas[0]
        return {**base, "estado": "revisar_multicuenta", "metodo": metodo,
                "cuenta": _resumen(elegida), "otras_cuentas": otras,
                "detalle": f"Identificada en '{ancla.get('primaryEmail')}', pero su cuenta "
                           f"principal sería '{elegida.get('primaryEmail')}'."}

    actual = cedula_de(elegida)
    resultado = {**base, "metodo": metodo, "cuenta": _resumen(elegida),
                 "otras_cuentas": otras}

    if actual == cedula:
        return {**resultado, "estado": "vinculada",
                "detalle": "Su cuenta ya lleva la cédula registrada."}
    if actual is None:
        return {**resultado, "estado": "existe_sin_cedula",
                "detalle": "Se identificó su cuenta, pero no lleva la cédula."}
    if actual in variantes_cedula(cedula):
        return {**resultado, "estado": "corregir_formato",
                "detalle": f"Lleva la cédula mal escrita: '{actual}'."}
    return {**resultado, "estado": "conflicto_cedula",
            "detalle": f"Su cuenta lleva la cédula '{actual}', distinta de '{cedula}'."}
