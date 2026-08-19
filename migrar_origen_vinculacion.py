"""
Separa, en los datos YA escritos, las cuentas creadas de las solo adoptadas.

Hasta el 2026-08-19, `/personas/procesar` registraba con `origen='creacion'` dos
cosas distintas:

  - la cuenta la creó la API                        -> creacion   (correcto)
  - la cuenta YA existía y solo se le puso la cédula -> creacion   (mal: es vinculacion)

Desde esa fecha el segundo caso se guarda como `vinculacion`, pero las filas
anteriores quedaron mezcladas y no se pueden separar mirando solo la tabla.

CÓMO LAS DISTINGUE
    Preguntando a Google cuándo nació cada cuenta. Si la cuenta es MUY anterior al
    vínculo, es que ya existía y solo la adoptamos; si nació al mismo tiempo que se
    registró el vínculo, la creamos nosotros.

    El directorio se lee de una sola pasada (~90 s, ~28 000 cuentas) en vez de una
    consulta por fila: miles de `users.get` chocarían con la cuota.

ZONAS HORARIAS
    `creado_en` se guarda naive en la zona de la sesión de PostgreSQL
    (America/Bogota, UTC-5) y Google devuelve UTC. Comparar en crudo daría 5 horas
    de desfase. La conversión la hace PostgreSQL, que tiene la base de zonas
    horarias de verdad; en Windows, `zoneinfo` puede no tenerla.

Uso (desde services/):
    python migrar_origen_vinculacion.py                    # informa, no toca nada
    python migrar_origen_vinculacion.py --margen-horas 6   # otro umbral
    python migrar_origen_vinculacion.py --aplicar          # reescribe el origen

Empieza SIEMPRE sin `--aplicar`: la simulación imprime la distribución de los
desfases, y con ella se ve si el corte separa limpiamente los dos grupos o si hay
casos en la frontera que conviene mirar a mano.
"""
import argparse
import sys
from collections import Counter

from psycopg2.extras import RealDictCursor, execute_values

from google_services.cliente import obtener_directorio
from google_services.vinculos import vinculos

# Por debajo de este desfase se considera que la cuenta nació con el vínculo, o
# sea que la creamos nosotros. Generoso a propósito: una creación real registra el
# vínculo en segundos, y una cuenta preexistente suele llevar meses. El hueco entre
# ambos casos es enorme, así que el umbral exacto no es delicado.
MARGEN_HORAS = 24

# Desfase (en segundos) -> etiqueta del histograma de la simulación.
TRAMOS = [
    (60, "menos de 1 min"),
    (3600, "1 min - 1 h"),
    (86400, "1 h - 1 día"),
    (7 * 86400, "1 - 7 días"),
    (30 * 86400, "7 - 30 días"),
    (365 * 86400, "1 - 12 meses"),
    (float("inf"), "más de un año"),
]

SQL_CANDIDATAS = """
    WITH g(google_id, creado_google) AS (VALUES %s)
    SELECT v.id,
           v.identificacion,
           v.email,
           v.creado_en,
           g.creado_google::timestamptz AS nacio,
           EXTRACT(EPOCH FROM (
               (v.creado_en AT TIME ZONE current_setting('TimeZone'))
               - g.creado_google::timestamptz
           )) AS desfase
    FROM   google_vinculos v
    JOIN   g ON g.google_id = v.google_id
    WHERE  v.origen = 'creacion'
    ORDER  BY desfase DESC
"""


def tramo(segundos: float) -> str:
    for tope, etiqueta in TRAMOS:
        if segundos < tope:
            return etiqueta
    return TRAMOS[-1][1]


def leer_argumentos():
    p = argparse.ArgumentParser(
        description="Reclasifica como 'vinculacion' las filas 'creacion' cuya cuenta "
                    "ya existía antes de que la API la tocara.")
    p.add_argument("--aplicar", action="store_true",
                   help="Escribe los cambios. Sin esto solo informa.")
    p.add_argument("--margen-horas", type=float, default=MARGEN_HORAS,
                   help=f"Desfase a partir del cual la cuenta se considera "
                        f"preexistente (por defecto {MARGEN_HORAS}).")
    p.add_argument("--ejemplos", type=int, default=10,
                   help="Cuántas filas de muestra imprimir (por defecto 10).")
    return p.parse_args()


def main() -> None:
    args = leer_argumentos()
    margen = args.margen_horas * 3600

    # 1. ¿Hay algo que migrar? Se pregunta antes de gastar 90 s en el volcado.
    with vinculos._conectar() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n FROM google_vinculos WHERE origen = 'creacion'")
            pendientes = cur.fetchone()["n"]

    if not pendientes:
        print("No hay filas con origen='creacion'. Nada que migrar.")
        return
    print(f"{pendientes} fila(s) con origen='creacion' por clasificar.")

    # 2. Volcado del directorio: una pasada, no una consulta por fila.
    print("Leyendo el directorio de Google (una pasada, ~90 s)…")
    try:
        nacimiento = {}
        for u in obtener_directorio().usuarios.volcar():
            if u.get("id") and u.get("creationTime"):
                nacimiento[u["id"]] = u["creationTime"]
    except Exception as e:
        sys.exit(f"No se pudo leer el directorio: {type(e).__name__}: {e}")
    print(f"{len(nacimiento)} cuenta(s) leídas de Google.")

    if not nacimiento:
        sys.exit("El directorio vino vacío; no se puede clasificar nada.")

    # 3. El cruce y la resta lo hace PostgreSQL, que sabe de zonas horarias.
    with vinculos._conectar() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            execute_values(cur, SQL_CANDIDATAS, list(nacimiento.items()),
                           template="(%s, %s)", page_size=1000)
            filas = cur.fetchall()

    huerfanas = pendientes - len(filas)
    preexistentes = [f for f in filas if f["desfase"] is not None and f["desfase"] > margen]
    creadas = [f for f in filas if f["desfase"] is not None and f["desfase"] <= margen]

    # 4. El informe. El histograma es lo que permite juzgar si el corte es limpio.
    print(f"\n{'desfase entre el nacimiento de la cuenta y el vínculo':<40} filas")
    print("-" * 56)
    reparto = Counter(tramo(f["desfase"]) for f in filas if f["desfase"] is not None)
    for _, etiqueta in TRAMOS:
        if reparto.get(etiqueta):
            print(f"  {etiqueta:<38} {reparto[etiqueta]:>6}")

    print(f"\nCon un margen de {args.margen_horas} h:")
    print(f"  {len(creadas):>6}  se quedan como 'creacion'   (la cuenta nació con el vínculo)")
    print(f"  {len(preexistentes):>6}  pasan a 'vinculacion'      (la cuenta ya existía)")
    if huerfanas:
        print(f"  {huerfanas:>6}  sin decidir: su cuenta ya no está en Google "
              f"(borrada). Se dejan como están.")

    if preexistentes and args.ejemplos:
        print(f"\nMuestra de las que pasarían a 'vinculacion' (mayor desfase primero):")
        print(f"  {'cédula':<14} {'correo':<40} {'nació':<22} {'antigüedad'}")
        for f in preexistentes[:args.ejemplos]:
            dias = f["desfase"] / 86400
            print(f"  {f['identificacion']:<14} {(f['email'] or '')[:39]:<40} "
                  f"{str(f['nacio'])[:19]:<22} {dias:,.0f} días antes")

    # Las que se quedan pero están cerca del corte: es donde puede haber error.
    frontera = [f for f in creadas if f["desfase"] is not None and f["desfase"] > 3600]
    if frontera:
        print(f"\n⚠️  {len(frontera)} fila(s) se quedan como 'creacion' pese a tener más de "
              f"1 h de desfase. Míralas antes de aplicar, o baja --margen-horas.")

    if not args.aplicar:
        print("\n(simulación: nada se ha escrito. Añade --aplicar para hacerlo.)")
        return

    if not preexistentes:
        print("\nNo hay nada que reescribir.")
        return

    # 5. La escritura. NO se toca `actualizado_en`: esto corrige una clasificación
    # nuestra, no refleja que el vínculo con Google haya cambiado.
    ids = [f["id"] for f in preexistentes]
    with vinculos._conectar() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE google_vinculos SET origen = 'vinculacion' WHERE id = ANY(%s)",
                (ids,))
            escritas = cur.rowcount
    print(f"\n{escritas} fila(s) reclasificadas como 'vinculacion'.")


if __name__ == "__main__":
    main()
