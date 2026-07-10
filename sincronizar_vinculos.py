"""
Reconstruye la tabla `google_vinculos` a partir de GOOGLE.

La tabla es un índice derivado: Google es la fuente de verdad. Esto lo hace
explícito — se puede tirar la tabla entera y rehacerla en un par de minutos
recorriendo el directorio. Lee las cuentas que llevan la cédula en `externalIds`
(customType=identificacion) y registra un vínculo por cada una.

Si una persona tiene varias cuentas (docente y estudiante, administrativa y
exalumna), se registran todas y se marca la principal con la regla de
google_services.jerarquia — la misma que usó el backfill para decidir dónde
escribir. Que ambas usen la misma regla no es un detalle: si divergieran, la tabla
diría que la cuenta principal es una y Google llevaría la cédula en otra.

Uso (desde services/):
    python sincronizar_vinculos.py [--consumidor backfill]

Es idempotente: correrlo dos veces no duplica nada, solo refresca correo, unidad y
`actualizado_en`.
"""
import argparse
import collections
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

TIPO_CEDULA = "identificacion"


def cedula_de(usuario: dict):
    """Cédula que la cuenta lleva en Google, o None."""
    for e in (usuario.get("externalIds") or []):
        if (e.get("customType") or e.get("type")) == TIPO_CEDULA:
            v = (e.get("value") or "").strip()
            return v or None
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--consumidor", default="backfill",
                   help="A quién se atribuyen los vínculos sincronizados.")
    args = p.parse_args()

    from google_services.cliente import obtener_directorio
    from google_services.jerarquia import es_persona, principal
    from google_services.vinculos import vinculos

    vinculos.inicializar()
    print("Recorriendo el directorio de Google...")

    por_cedula = collections.defaultdict(list)
    total = 0
    for u in obtener_directorio().usuarios.volcar():
        total += 1
        ced = cedula_de(u)
        if ced:
            por_cedula[ced].append(u)
        if total % 5000 == 0:
            print(f"  {total} cuentas...")

    filas = []
    varias = 0
    for ced, cuentas in por_cedula.items():
        # La principal se elige entre las cuentas VIVAS; si ninguna lo está (todas
        # archivadas), se cae a la primera para no perder el vínculo.
        vivas = [u for u in cuentas if es_persona(u)]
        elegida = principal(vivas or cuentas)
        if len(cuentas) > 1:
            varias += 1
        for u in cuentas:
            filas.append((
                ced,
                u["id"],
                (u.get("primaryEmail") or "").lower(),
                u.get("orgUnitPath") or "/",
                u["id"] == elegida["id"],
                args.consumidor,
                "sincronizacion",
            ))

    n = vinculos.registrar_muchos(filas)
    resumen = vinculos.contar()

    print()
    print(f"cuentas en el dominio      : {total:,}".replace(",", "."))
    print(f"con cédula en externalIds  : {len(filas):,}".replace(",", "."))
    print(f"personas distintas         : {len(por_cedula):,}".replace(",", "."))
    print(f"personas con varias cuentas: {varias}")
    print(f"vínculos escritos          : {n:,}".replace(",", "."))
    print()
    print(f"en la tabla: {resumen['vinculos']:,} vínculos, "
          f"{resumen['personas']:,} personas".replace(",", "."))
    for f in resumen["por_consumidor"]:
        print(f"   {f['consumidor']:16} {f['origen']:16} {f['n']:6}")


if __name__ == "__main__":
    main()
