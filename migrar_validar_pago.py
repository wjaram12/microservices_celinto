"""
Migración one-off: siembra la ruta validar-pago en una BD ya poblada.

Las semillas automáticas (rutas.inicializar / procesadores.inicializar) solo
corren cuando la tabla está VACÍA, así que en una base existente (producción)
las filas de validar-pago NO se insertan al desplegar. Este script las crea de
forma idempotente, leyendo los configs del propio código, e invalida la caché.

EJECUTAR EN EL HOST DE PRODUCCIÓN (donde el .env apunta a la BD/Redis reales):

    .venv/bin/python migrar_validar_pago.py        # Linux
    .venv/Scripts/python.exe migrar_validar_pago.py # Windows

Es seguro correrlo varias veces: si una fila ya existe, la deja como está.
"""
from psycopg2 import errors as pg_errors

from app.services.procesadores import (
    UMBRAL_DEFECTO,
    _CLASIF_PAGO,
    _ESQUEMA_DEPOSITO,
    _ESQUEMA_TRANSFERENCIA,
    procesadores,
)
from app.services.rutas import rutas

RUTA = "validar-pago"
URL = "/api/v1/validaciones/validar-pago/"
DESC = "Valida un comprobante de pago: clasifica (depósito/transferencia) y extrae su información."

FILAS = [
    # (operacion, clase, esquema, umbral)
    ("clasificar", "", _CLASIF_PAGO, UMBRAL_DEFECTO),
    ("extraer", "DEPOSITO", _ESQUEMA_DEPOSITO, None),
    ("extraer", "TRANSFERENCIA", _ESQUEMA_TRANSFERENCIA, None),
]


def main():
    # 1) La ruta en el catálogo (procesadores.crear valida contra él).
    try:
        rutas.crear(RUTA, URL, DESC)
        print(f"[OK] ruta '{RUTA}' creada")
    except pg_errors.UniqueViolation:
        print(f"[=]  ruta '{RUTA}' ya existía")

    # 2) Los procesadores (clasificador + un extractor por clase).
    for operacion, clase, esquema, umbral in FILAS:
        etiqueta = f"{operacion}/{clase or '-'}"
        try:
            procesadores.crear(RUTA, operacion, clase, "inline",
                               esquema=esquema, umbral=umbral)
            print(f"[OK] procesador {etiqueta} creado")
        except pg_errors.UniqueViolation:
            print(f"[=]  procesador {etiqueta} ya existía")

    # procesadores.crear() ya invalida la caché de Redis en cada inserción; los
    # workers leen la config nueva en la siguiente petición.
    print("\nListo. Probar de nuevo POST /api/v1/validaciones/validar-pago/.")


if __name__ == "__main__":
    main()
