"""
Funde varias matrices de personas en una sola matriz maestra, sin duplicados.

Las tres fuentes se solapan: la misma persona aparece en dos sistemas con dos
`persona_id` distintos. Si se cruzan por separado contra Google, cada corrida
escribe en la MISMA cuenta sin que la otra lo sepa, y la segunda pisa a la primera.
Las guardas de migrar_cedulas_google.py solo ven su propio informe.

Por eso primero se unifican las personas y DESPUÉS se cruza una sola vez:

    python crear_matriz_maestra.py
    python migrar_cedulas_google.py informar --personas pruebas/matriz_maestra.csv
    python informe_gerencial_google.py

La entrada puede ser un CSV de personas o un informe de backfill: ambos llevan las
cinco columnas de origen, y del informe se ignora lo demás.

Cómo se elige la fila superviviente cuando una persona está en varias fuentes: gana
aquella cuyo `emailinst` EXISTE de verdad en Google (se comprueba contra el volcado).
Es un criterio medido, no una preferencia: entre las fuentes actuales, la proporción
de correos que existen va del 45 % al 82 %. Si ninguna acierta, gana la que al menos
traiga correo; y si empatan, el orden de FUENTES.

La cédula se canoniza rellenando con un cero las de 9 dígitos, que es el error
conocido ('925122673' es en realidad '0925122673'). Las de otras longitudes se dejan
como están: son pasaportes o documentos extranjeros, y rellenarlos los corrompería.
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

RAIZ = Path(__file__).resolve().parent
PRUEBAS = RAIZ / "pruebas"
VOLCADO = PRUEBAS / "volcado_google.json"
SALIDA = PRUEBAS / "matriz_maestra.csv"

# Orden de preferencia solo para desempatar. La calidad real se mide con el volcado.
#
# Se apunta al CSV de ORIGEN de cada sistema, no a su informe. `informe_backfill.csv`
# es la salida de este mismo flujo: usarlo aquí haría que la matriz maestra se
# alimentase de sí misma en la siguiente corrida.
#
# De `sga20176` no se conserva el CSV original, solo su informe; sirve igual, porque
# el informe arrastra intactas las cinco columnas de la persona.
FUENTES = [
    ("ucgone", Path("C:/whistle_corp/academico-sga-cg/plantilla.csv")),
    ("sga20176", PRUEBAS / "informe_backfill_20176.csv"),
    ("personas", PRUEBAS / "personas.csv"),
]

COLUMNAS = ["persona_id", "identificacion", "nombres", "apellidos", "emailinst",
            "fuentes", "ids_origen"]


def canonizar(cedula: str) -> str:
    """Cédula comparable. Solo se rellena la de 9 dígitos: es el error conocido de
    haberla guardado como número y perder el cero inicial. Un documento de 6, 7 u 8
    dígitos es extranjero y rellenarlo lo convertiría en otro número."""
    c = (cedula or "").strip().upper()
    if c.isdigit() and len(c) == 9:
        return c.zfill(10)
    return c


def de_relleno(cedula: str) -> bool:
    """'0000000000', '00000000'... No identifican a nadie: no se pueden usar para
    fusionar personas, o dos desconocidos distintos acabarían siendo el mismo."""
    c = (cedula or "").strip()
    return not c or (c.isdigit() and len(set(c)) == 1)


def emails_de_google() -> set:
    if not VOLCADO.is_file():
        print(f"AVISO: falta {VOLCADO}; la precedencia caerá al orden de FUENTES.")
        return set()
    return {u["email"] for u in json.loads(VOLCADO.read_text(encoding="utf-8"))}


def calidad(fila: dict, reales: set, rango_fuente: int) -> tuple:
    """Mayor es mejor. Se ordena por: correo que existe en Google, luego tener
    correo, y por último el orden de FUENTES (negado, para que 0 gane)."""
    correo = fila["emailinst"].strip().lower()
    return (correo in reales, bool(correo), -rango_fuente)


def main() -> None:
    reales = emails_de_google()

    # clave -> [(rango_fuente, nombre_fuente, fila), ...]
    grupos = defaultdict(list)
    leidas = 0
    for rango, (nombre, ruta) in enumerate(FUENTES):
        if not ruta.is_file():
            sys.exit(f"No existe {ruta}")
        with ruta.open(encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                leidas += 1
                ced = canonizar(fila["identificacion"])
                # Sin cédula utilizable no se puede fusionar: cada fila va sola.
                clave = ced if not de_relleno(ced) else f"__{nombre}:{fila['persona_id']}"
                grupos[clave].append((rango, nombre, fila))
        print(f"  {nombre:10} {ruta.name}")

    filas, fusionadas = [], 0
    for clave, miembros in grupos.items():
        if len(miembros) > 1:
            fusionadas += len(miembros) - 1
        rango, fuente, mejor = max(miembros, key=lambda m: calidad(m[2], reales, m[0]))
        fuentes = sorted({m[1] for m in miembros})
        ids = ";".join(f"{m[1]}:{m[2]['persona_id']}" for m in sorted(miembros, key=lambda m: m[0]))
        filas.append({
            # persona_id nuevo y estable: la cédula canónica identifica a la persona.
            "persona_id": f"{fuente}:{mejor['persona_id']}",
            "identificacion": canonizar(mejor["identificacion"]),
            "nombres": mejor["nombres"],
            "apellidos": mejor["apellidos"],
            "emailinst": mejor["emailinst"],
            "fuentes": "+".join(fuentes),
            "ids_origen": ids,
        })

    filas.sort(key=lambda f: f["identificacion"])
    PRUEBAS.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        w.writerows(filas)

    print()
    print(f"filas leídas      : {leidas:,}".replace(",", "."))
    print(f"personas únicas   : {len(filas):,}".replace(",", "."))
    print(f"duplicados fundidos: {fusionadas:,}".replace(",", "."))
    print()
    reparto = defaultdict(int)
    for f in filas:
        reparto[f["fuentes"]] += 1
    print("personas por combinación de fuentes:")
    for k, v in sorted(reparto.items(), key=lambda x: -x[1]):
        print(f"  {k:30} {v:6}")
    print()
    con_correo = sum(1 for f in filas if f["emailinst"].strip())
    ok = sum(1 for f in filas if f["emailinst"].strip().lower() in reales)
    print(f"con correo                    : {con_correo:,}".replace(",", "."))
    print(f"con correo que EXISTE en Google: {ok:,}".replace(",", "."))
    print(f"\n-> {SALIDA}")


if __name__ == "__main__":
    main()
