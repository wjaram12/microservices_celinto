"""
Backfill de la cédula (externalId) en las cuentas de Google Workspace.

Vincula cada persona de la tabla local con SU cuenta principal de Google y escribe
allí la cédula como `externalIds` con `customType=identificacion`, para que después
se pueda resolver con `users.list(query="externalId=<cedula>")`.

Se ejecuta en TRES FASES separadas, y solo la última escribe:

    1. volcar    Lee las ~28 000 cuentas de Google en una pasada (~90 s) y las deja
                 en un archivo local. Sustituye a decenas de miles de consultas
                 individuales, que agotarían la cuota del Admin SDK.

    2. informar  Cruza el volcado con tu CSV de personas y produce un INFORME con
                 el veredicto de cada fila. NO toca Google. Esto es lo que se revisa
                 a mano antes de escribir nada.

    3. aplicar   Escribe en Google SOLO las filas del informe marcadas `insertar` o
                 `corregir`. Por defecto hace una simulación; escribe de verdad
                 únicamente con `--real`.

Uso (desde services/):

    python migrar_cedulas_google.py volcar
    python migrar_cedulas_google.py informar --personas pruebas/personas.csv
    python migrar_cedulas_google.py aplicar                 # simulación
    python migrar_cedulas_google.py aplicar --real          # escribe en Google

Los archivos intermedios van a `pruebas/`, que está en el .gitignore: contienen
datos personales (cédulas y nombres) y NO deben acabar en el repositorio.

Por qué el emparejamiento se hace aquí y no consultando a Google por nombre: la API
exige el nombre completo exacto (`name:'Walter Jara'` devuelve cero resultados) y
buscar por apellido devuelve ruido. En local se pueden normalizar acentos, reordenar
apellidos y usar difflib con un umbral.
"""
import argparse
import csv
import json
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from google_services.identidad import (  # noqa: E402  (tras el bloque de stdlib)
    UMBRAL_NOMBRE, casa_nombre as _casa_nombre_txt, cedula_invalida, clave_nombre,
    normalizar, variantes_cedula,
)
from google_services.jerarquia import (  # noqa: E402
    es_persona, ou_de as _ou, principal, rango,
)

RAIZ = Path(__file__).resolve().parent
PRUEBAS = RAIZ / "pruebas"
VOLCADO = PRUEBAS / "volcado_google.json"
INFORME = PRUEBAS / "informe_backfill.csv"
# Cuentas ya escritas. Permite reanudar tras un corte sin repetir ni saltar nada.
BITACORA = PRUEBAS / "aplicadas.log"

# Escrituras en paralelo. Cada una son dos llamadas a Google (leer + escribir), de
# ~0,3 s cada una: en serie, 24 000 cuentas son horas. Con 12 hilos baja a minutos y
# se queda lejos del límite de cuota del Admin SDK (unas 2 400 peticiones/minuto).
HILOS = 12

# customType con el que se guarda la cédula. Es el que ya usa el módulo `emailing`
# del monolito; hay 3 cuentas con 'cedula' que quedan como legado a revisar aparte.
TIPO_CEDULA = "identificacion"

# Las reglas de qué cuenta es de una persona viva y cuál es su cuenta principal
# viven en google_services.jerarquia: las comparte el registro de vínculos, y si
# divergieran, la tabla diría una cosa y Google otra.

# Las reglas de comparación de identidad (normalizar, clave_nombre, variantes de la
# cédula, cédulas de relleno, coincidencia de nombres) viven en
# google_services.identidad: las comparte el endpoint de auditoría individual. Si
# divergieran, el informe masivo y la API darían veredictos distintos del mismo caso.


# --------------------------------------------------------------------------- #
# Fase 1: volcar
# --------------------------------------------------------------------------- #

def fase_volcar() -> None:
    from google_services.cliente import obtener_directorio

    PRUEBAS.mkdir(exist_ok=True)
    print("Volcando el directorio de Google (una pasada, ~90 s)...")

    usuarios = []
    for u in obtener_directorio().usuarios.volcar():
        usuarios.append({
            "google_id": u.get("id"),
            "email": (u.get("primaryEmail") or "").lower(),
            "nombre": ((u.get("name") or {}).get("fullName") or ""),
            "ou": u.get("orgUnitPath") or "/",
            "suspended": bool(u.get("suspended")),
            "archived": bool(u.get("archived")),
            "lastLoginTime": u.get("lastLoginTime") or "",
            "externalIds": u.get("externalIds") or [],
        })
        if len(usuarios) % 5000 == 0:
            print(f"  {len(usuarios)} cuentas...")

    VOLCADO.write_text(json.dumps(usuarios, ensure_ascii=False), encoding="utf-8")
    vivas = sum(1 for u in usuarios if es_persona(u))
    print(f"\n{len(usuarios)} cuentas volcadas ({vivas} son personas vivas) -> {VOLCADO}")


def cargar_volcado() -> list:
    if not VOLCADO.is_file():
        sys.exit(f"No existe {VOLCADO}. Corre primero: python {Path(__file__).name} volcar")
    return json.loads(VOLCADO.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Fase 2: informar
# --------------------------------------------------------------------------- #

def indexar(usuarios: list) -> dict:
    """Índices para emparejar sin volver a llamar a Google."""
    por_email, por_ext, por_nombre, por_apellido = {}, defaultdict(list), defaultdict(list), defaultdict(list)
    for u in usuarios:
        por_email[u["email"]] = u
        for e in u["externalIds"]:
            v = (e.get("value") or "").strip()
            if v:
                # Se indexa por todas las variantes: así una cédula guardada sin el
                # cero inicial se encuentra igual buscando la forma canónica.
                for f in variantes_cedula(v):
                    por_ext[f].append(u)
        if u["nombre"]:
            por_nombre[clave_nombre(u["nombre"])].append(u)
            # Cubo por apellido para acotar la comparación difusa: comparar cada
            # persona contra las 28 000 cuentas sería inviable.
            for token in set(normalizar(u["nombre"]).split()):
                por_apellido[token].append(u)
    return {"email": por_email, "ext": por_ext, "nombre": por_nombre, "apellido": por_apellido}


def candidatos_exactos(persona: dict, idx: dict) -> list:
    """Cuentas cuyo nombre completo normalizado coincide exactamente."""
    completo = f"{persona['nombres']} {persona['apellidos']}"
    return idx["nombre"].get(clave_nombre(completo), [])


def candidatos_difusos(persona: dict, idx: dict) -> list:
    """Cuentas cuyo nombre se parece por encima del umbral. Solo como último recurso."""
    completo = f"{persona['nombres']} {persona['apellidos']}"
    objetivo = normalizar(completo)
    vistos, mejores = set(), []
    # Solo se compara contra cuentas que comparten al menos un apellido.
    for token in normalizar(persona["apellidos"]).split():
        for u in idx["apellido"].get(token, []):
            if u["google_id"] in vistos:
                continue
            vistos.add(u["google_id"])
            r = SequenceMatcher(None, objetivo, normalizar(u["nombre"])).ratio()
            if r >= UMBRAL_NOMBRE:
                mejores.append(u)
    return mejores


def cedula_en(usuario: dict):
    """Valor del externalId de tipo cédula que ya tiene la cuenta, o None."""
    for e in usuario["externalIds"]:
        if (e.get("customType") or e.get("type")) == TIPO_CEDULA:
            return (e.get("value") or "").strip()
    return None


def veredicto(persona: dict, idx: dict) -> dict:
    """Decide el estado de una persona frente al directorio. No escribe nada."""
    cedula = (persona["identificacion"] or "").strip()
    email = (persona["emailinst"] or "").strip().lower()

    # --- Anclas FUERTES: identifican a la persona sin lugar a dudas. ---
    # Por cédula (probando las variantes con y sin cero inicial).
    por_ced = _unicos([u for f in variantes_cedula(cedula) for u in idx["ext"].get(f, [])])
    # Por correo institucional: la llave exacta que ya está en tu tabla.
    cuenta_email = idx["email"].get(email) if email else None

    base = {**persona, "metodo": "", "estado": "", "google_id": "", "email_google": "",
            "ou": "", "detalle": "", "candidatos": 0}

    # El correo de la tabla existe en Google, pero ¿es SUYO? Solo si la cédula lo
    # confirma o el nombre casa. Si no, es la cuenta de un tercero: se excluye del
    # conjunto de candidatos. Mezclarla con las cuentas reales de la persona la haría
    # parecer una segunda cuenta suya, y el veredicto saldría 'ambigua'.
    correo_ajeno = None
    if cuenta_email:
        suyo = (any(u["google_id"] == cuenta_email["google_id"] for u in por_ced)
                or _casa_nombre(persona, cuenta_email))
        if not suyo:
            correo_ajeno = cuenta_email
            cuenta_email = None

    anclas = _unicos([*por_ced, *([cuenta_email] if cuenta_email else [])])

    # --- Hermanas: las OTRAS cuentas de la misma persona. ---
    # Hay que buscarlas SIEMPRE, aunque ya se tenga ancla: la cuenta principal puede
    # ser una distinta de la que ancló (p. ej. el correo de la tabla es el de
    # estudiante, pero la persona también es docente).
    exactas = candidatos_exactos(persona, idx)
    difusas = [] if (anclas or exactas) else candidatos_difusos(persona, idx)

    cuentas = _unicos([*anclas, *exactas, *difusas])
    vivas = [u for u in cuentas if es_persona(u)]
    base["candidatos"] = len(cuentas)

    if not vivas:
        if cuentas:
            return {**base, "estado": "solo_cuentas_inactivas",
                    "detalle": "; ".join(f"{u['email']} ({_ou(u)})" for u in cuentas)}
        # No tiene cuenta. Si además el correo de la tabla es de un tercero, hay que
        # decirlo: el sistema de origen guarda un dato que pertenece a otra persona.
        if correo_ajeno:
            return {**base, "estado": "correo_ocupado", "metodo": "email", "candidatos": 1,
                    "google_id": correo_ajeno["google_id"],
                    "email_google": correo_ajeno["email"], "ou": _ou(correo_ajeno),
                    "detalle": f"El correo pertenece a '{correo_ajeno['nombre']}'; "
                               "la persona no tiene cuenta."}
        return {**base, "estado": "disponible", "detalle": "Sin cuenta en Google"}

    elegida = principal(vivas)
    empatadas = [u for u in vivas if rango(u) == rango(elegida)]
    otras = [u["email"] for u in vivas if u["google_id"] != elegida["google_id"]]

    ids_anclas = {u["google_id"] for u in anclas}
    ids_ced = {u["google_id"] for u in por_ced}

    def _con(estado, detalle, metodo=""):
        d = detalle
        if otras:
            d = (d + " | " if d else "") + f"otras cuentas: {', '.join(otras)}"
        if correo_ajeno:
            # La persona sí tiene cuenta, pero el correo que guarda la tabla es de
            # otro. No cambia el veredicto, pero el origen tiene un dato equivocado.
            d = (d + " | " if d else "") + \
                f"OJO: el correo '{email}' es de '{correo_ajeno['nombre']}'"
        return {**base, "estado": estado, "metodo": metodo,
                "google_id": elegida["google_id"], "email_google": elegida["email"],
                "ou": _ou(elegida), "detalle": d}

    # Varias cuentas vivas con la MISMA precedencia: no hay forma de elegir.
    if len(empatadas) > 1:
        metodo = "cedula" if por_ced else ("email" if cuenta_email else "nombre")
        return _con("ambigua",
                    "; ".join(f"{u['email']} ({_ou(u)})" for u in empatadas), metodo)

    # Sin ancla fuerte: se llegó aquí solo por el nombre.
    if not anclas:
        if difusas:
            # Parecido, no idéntico. Escribir aquí es el error más caro posible.
            return _con("revisar_nombre_difuso",
                        f"solo coincidencia aproximada con '{elegida['nombre']}'", "nombre")
        estado_nombre = "nombre"
    else:
        estado_nombre = "cedula" if elegida["google_id"] in ids_ced else "email"

    # La cuenta principal NO es la que ancló el emparejamiento. Es el caso de la
    # persona con dos cuentas (docente + estudiante): el nombre dice que es la misma
    # persona, pero nada lo PRUEBA. Se deja a revisión humana en vez de escribir.
    if anclas and elegida["google_id"] not in ids_anclas:
        ancla = anclas[0]
        return _con("revisar_multicuenta",
                    f"anclada por {'cédula' if por_ced else 'correo'} en "
                    f"'{ancla['email']}' ({_ou(ancla)}), pero la principal sería "
                    f"'{elegida['email']}' ({_ou(elegida)})", estado_nombre)

    actual = cedula_en(elegida)

    if actual == cedula:
        return _con("vinculada", "", estado_nombre)
    if actual is None:
        return _con("insertar", "", estado_nombre)
    if actual in variantes_cedula(cedula):
        # Misma cédula, mal escrita (típicamente sin el cero inicial): es seguro
        # normalizarla.
        return _con("corregir_formato", f"valor actual '{actual}'", estado_nombre)
    # Cédula DISTINTA. No se toca: o la tabla o Google está equivocado, y
    # sobrescribir destruiría el dato bueno sin dejar rastro.
    return _con("conflicto_cedula",
                f"Google dice '{actual}', la tabla dice '{cedula}'", estado_nombre)


def _unicos(cuentas: list) -> list:
    vistos, salida = set(), []
    for u in cuentas:
        if u and u["google_id"] not in vistos:
            vistos.add(u["google_id"])
            salida.append(u)
    return salida


def _casa_nombre(persona: dict, usuario: dict) -> bool:
    """Adapta la comparación compartida al par (fila de la tabla, cuenta del volcado)."""
    return _casa_nombre_txt(f"{persona['nombres']} {persona['apellidos']}", usuario["nombre"])


COLUMNAS = ["persona_id", "identificacion", "nombres", "apellidos", "emailinst",
            "estado", "metodo", "google_id", "email_google", "ou", "candidatos", "detalle"]

ESCRIBIBLES = ("insertar", "corregir_formato")


def marcar_colisiones(filas: list) -> None:
    """
    Degrada las filas que escribirían en la MISMA cuenta de Google.

    Pasa cuando la tabla de personas tiene a la misma persona dos veces (dos
    persona_id, a veces con cédulas distintas). Sin esto, las dos filas se escriben
    en orden y gana la última: la cuenta acaba con la cédula de la fila equivocada,
    sin dejar rastro de que hubo un choque.

    Si las cédulas coinciden, la colisión es benigna: escribe una y las demás quedan
    como `duplicado_persona`. Si difieren, ninguna escribe.
    """
    por_cuenta = defaultdict(list)
    for f in filas:
        if f["estado"] in ESCRIBIBLES and f["google_id"]:
            por_cuenta[f["google_id"]].append(f)

    for grupo in por_cuenta.values():
        if len(grupo) < 2:
            continue
        cedulas = {f["identificacion"].strip() for f in grupo}
        ids = ", ".join(f["persona_id"] for f in grupo)
        if len(cedulas) > 1:
            for f in grupo:
                f["estado"] = "conflicto_duplicado"
                f["detalle"] = (f"varias personas ({ids}) apuntan a esta cuenta con "
                                f"cédulas distintas: {', '.join(sorted(cedulas))}")
        else:
            for f in grupo[1:]:
                f["estado"] = "duplicado_persona"
                f["detalle"] = f"misma persona y cédula que persona_id {grupo[0]['persona_id']}"


def fase_informar(ruta_personas: Path) -> None:
    if not ruta_personas.is_file():
        sys.exit(f"No existe el CSV de personas: {ruta_personas}")

    usuarios = cargar_volcado()
    idx = indexar(usuarios)
    print(f"Volcado: {len(usuarios)} cuentas. Emparejando...")

    filas = []
    with ruta_personas.open(encoding="utf-8-sig", newline="") as f:
        for persona in csv.DictReader(f):
            persona.setdefault("emailinst", "")
            if cedula_invalida(persona["identificacion"]):
                filas.append({**persona, "estado": "cedula_invalida", "metodo": "",
                              "google_id": "", "email_google": "", "ou": "",
                              "candidatos": 0,
                              "detalle": f"cédula de relleno '{persona['identificacion']}'"})
                continue
            filas.append(veredicto(persona, idx))

    # Dos personas que escribirían en la misma cuenta: se resuelve DESPUÉS de tener
    # todas las filas, porque es una propiedad del conjunto, no de una fila suelta.
    marcar_colisiones(filas)

    conteo = defaultdict(int)
    for f in filas:
        conteo[f["estado"]] += 1

    PRUEBAS.mkdir(exist_ok=True)
    with INFORME.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)

    print(f"\n{len(filas)} personas procesadas -> {INFORME}\n")
    etiquetas = {
        "vinculada": "ya tiene su cédula, nada que hacer",
        "insertar": "cuenta identificada, falta la cédula   -> SE ESCRIBIRÁ",
        "corregir_formato": "misma cédula sin el cero inicial       -> SE ESCRIBIRÁ",
        "conflicto_cedula": "Google tiene OTRA cédula, revisar a mano",
        "revisar_multicuenta": "la principal no es la cuenta que ancló, revisar",
        "revisar_nombre_difuso": "solo se parece el nombre, revisar a mano",
        "conflicto_duplicado": "2 personas -> 1 cuenta, cédulas distintas",
        "duplicado_persona": "fila repetida de la misma persona, se omite",
        "cedula_invalida": "cédula de relleno (p. ej. 0000000000)",
        "ambigua": "varias cuentas empatadas, revisar a mano",
        "correo_ocupado": "el correo es de otra persona, revisar",
        "solo_cuentas_inactivas": "solo cuentas archivadas/suspendidas",
        "disponible": "sin cuenta en Google, hay que crearla",
    }
    for estado, texto in etiquetas.items():
        print(f"  {estado:24} {conteo[estado]:6}   {texto}")

    sobrantes = set(conteo) - set(etiquetas)
    for estado in sorted(sobrantes):
        print(f"  {estado:24} {conteo[estado]:6}   (estado no catalogado)")

    print(f"\nLa fase 'aplicar' escribiría en {conteo['insertar'] + conteo['corregir_formato']} cuentas.")
    print("Revisa el informe ANTES de correrla.")


# --------------------------------------------------------------------------- #
# Fase 3: aplicar
# --------------------------------------------------------------------------- #

def fase_aplicar(real: bool, limite: int = 0) -> None:
    from google_services.cliente import obtener_directorio

    if not INFORME.is_file():
        sys.exit(f"No existe {INFORME}. Corre primero la fase 'informar'.")

    with INFORME.open(encoding="utf-8-sig", newline="") as f:
        # SOLO los estados inequívocos. `conflicto_cedula`, `ambigua` y
        # `correo_ocupado` exigen decisión humana y nunca se escriben en automático.
        pendientes = [r for r in csv.DictReader(f) if r["estado"] in ESCRIBIBLES]

    if not pendientes:
        print("No hay nada que escribir.")
        return

    # Defensa en profundidad: aunque `informar` ya degrada las colisiones, el informe
    # es un CSV que alguien pudo editar a mano. Antes de escribir, se vuelve a
    # comprobar que ninguna cuenta reciba dos cédulas y que ninguna sea de relleno.
    destinos = defaultdict(set)
    for r in pendientes:
        destinos[r["google_id"]].add(r["identificacion"].strip())
    choques = {g: c for g, c in destinos.items() if len(c) > 1}
    rellenos = [r for r in pendientes if cedula_invalida(r["identificacion"])]
    if choques or rellenos:
        print("ABORTADO. El informe no es seguro para escribir:")
        for g, c in list(choques.items())[:5]:
            print(f"  la cuenta {g} recibiría {len(c)} cédulas distintas: {sorted(c)}")
        for r in rellenos[:5]:
            print(f"  cédula de relleno '{r['identificacion']}' -> {r['email_google']}")
        sys.exit(1)

    # Reanudación: cada cuenta ya escrita queda en la bitácora. Un proceso de decenas
    # de miles de escrituras se corta (red, cuota, un Ctrl-C) y hay que poder seguir
    # sin repetir ni saltarse nada. La escritura es idempotente, pero repetirla cuesta
    # dos llamadas a Google por cuenta.
    hechas = set()
    if BITACORA.is_file():
        with BITACORA.open(encoding="utf-8") as f:
            hechas = {ln.split("\t")[0] for ln in f if ln.strip()}
        pendientes = [r for r in pendientes if r["google_id"] not in hechas]
        print(f"Bitácora: {len(hechas)} cuentas ya procesadas; quedan {len(pendientes)}.")

    if limite:
        pendientes = pendientes[:limite]

    if not pendientes:
        print("No queda nada por escribir.")
        return

    if not real:
        print(f"SIMULACIÓN: se escribiría la cédula en {len(pendientes)} cuentas.")
        for r in pendientes[:10]:
            print(f"  {r['email_google']:42} {r['identificacion']:12} ({r['estado']})")
        if len(pendientes) > 10:
            print(f"  ... y {len(pendientes) - 10} más")
        print("\nPara escribir de verdad:  --real")
        return

    print(f"ESCRIBIENDO en {len(pendientes)} cuentas de Google ({HILOS} hilos)...\n")
    conteo, errores = defaultdict(int), []
    candado = threading.Lock()
    inicio = time.monotonic()

    def escribir(r):
        # `obtener_directorio()` es por hilo a propósito: el cliente de googleapiclient
        # no es thread-safe. Pedirlo aquí dentro (y no una vez fuera) es lo que hace
        # que cada hilo use el suyo.
        usuarios = obtener_directorio().usuarios
        try:
            resultado = usuarios.establecer_external_id(
                r["google_id"], r["identificacion"].strip(), TIPO_CEDULA)
            return r, resultado, None
        except Exception as e:
            return r, "error", str(e)[:160]

    PRUEBAS.mkdir(exist_ok=True)
    with BITACORA.open("a", encoding="utf-8") as bit, \
            ThreadPoolExecutor(max_workers=HILOS) as pool:
        for i, (r, resultado, err) in enumerate(
                pool.map(escribir, pendientes), 1):
            with candado:
                conteo[resultado] += 1
                if err:
                    errores.append((r["email_google"], err))
                else:
                    # Solo se anota lo confirmado por Google, para que una reanudación
                    # reintente lo que falló.
                    bit.write(f"{r['google_id']}\t{r['identificacion']}\t{resultado}\n")
                    bit.flush()
            if i % 250 == 0 or i == len(pendientes):
                seg = time.monotonic() - inicio
                ritmo = i / seg if seg else 0
                falta = (len(pendientes) - i) / ritmo if ritmo else 0
                print(f"  {i}/{len(pendientes)}  ({ritmo:.1f}/s, faltan ~{falta/60:.0f} min)"
                      f"  errores={conteo['error']}")

    print("\nResultado:")
    for k in ("insertado", "corregido", "sin_cambios", "error"):
        if conteo[k]:
            print(f"  {k:12} {conteo[k]}")
    if errores:
        print(f"\nPrimeros errores (de {len(errores)}):")
        for email, msg in errores[:10]:
            print(f"  {email}: {msg}")
        print("\nLos fallidos NO están en la bitácora: vuelve a correr 'aplicar --real' "
              "para reintentarlos.")


# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="fase", required=True)
    sub.add_parser("volcar", help="Lee todas las cuentas de Google a un archivo local.")
    pi = sub.add_parser("informar", help="Cruza el volcado con el CSV de personas.")
    pi.add_argument("--personas", type=Path, required=True, help="CSV de personas.")
    pa = sub.add_parser("aplicar", help="Escribe la cédula en Google (simula por defecto).")
    pa.add_argument("--real", action="store_true", help="Escribe de verdad. Sin esto, simula.")
    pa.add_argument("--limite", type=int, default=0,
                    help="Procesa como mucho N cuentas. Para una prueba de humo.")

    args = p.parse_args()
    if args.fase == "volcar":
        fase_volcar()
    elif args.fase == "informar":
        fase_informar(args.personas)
    else:
        fase_aplicar(args.real, args.limite)


if __name__ == "__main__":
    main()
