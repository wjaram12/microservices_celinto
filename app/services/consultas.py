"""
Servicio ServicioConsultas: ejecuta SQL arbitrario contra la base del servicio.

Pensado SOLO para la consola de administración (/admin/consultas), protegida con
scope admin. Usa el mismo pool de conexiones que el resto de servicios; como
`_conectar` hace commit al terminar bien, los INSERT/UPDATE/DELETE persisten.

⚠ Es una herramienta poderosa: ejecuta cualquier sentencia con los privilegios del
usuario de la base. El único candado es el scope admin de la API key.
"""
from typing import Any

from app.core.db import ServicioBD

LIMITE_FILAS = 500


def _json_safe(valor: Any) -> Any:
    """Convierte un valor de psycopg2 a algo serializable a JSON para la respuesta.
    Los tipos nativos pasan tal cual; el resto (date/datetime/Decimal/uuid/…) a str
    (preserva el valor exacto); los binarios se resumen por tamaño."""
    if valor is None or isinstance(valor, (bool, int, float, str, list, dict)):
        return valor
    if isinstance(valor, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(valor))} bytes>"
    return str(valor)


class ServicioConsultas(ServicioBD):
    """Ejecuta SQL crudo. No tiene tabla propia (no corre DDL al conectar)."""

    def __init__(self):
        super().__init__()
        self._tabla_lista = True

    def ejecutar(self, sql: str) -> dict:
        """
        Ejecuta `sql` y devuelve:
          - consulta con resultados (SELECT / RETURNING):
              {"tipo":"consulta","columnas":[...],"filas":[[...]],
               "num_filas":N,"truncado":bool}
          - comando sin resultados (INSERT/UPDATE/DELETE/DDL):
              {"tipo":"comando","rowcount":N,"mensaje":"INSERT 0 1"}

        Lanza ValueError si el SQL está vacío y la excepción de psycopg2 si el SQL
        falla (la view la traduce a 400). `_conectar` hace commit al salir bien,
        por lo que las escrituras persisten.
        """
        sql = (sql or "").strip()
        if not sql:
            raise ValueError("La consulta SQL está vacía.")
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(sql)
                if cur.description is not None:
                    columnas = [d.name for d in cur.description]
                    filas = cur.fetchmany(LIMITE_FILAS + 1)
                    truncado = len(filas) > LIMITE_FILAS
                    filas = filas[:LIMITE_FILAS]
                    return {
                        "tipo": "consulta",
                        "columnas": columnas,
                        "filas": [[_json_safe(v) for v in fila] for fila in filas],
                        "num_filas": len(filas),
                        "truncado": truncado,
                    }
                return {
                    "tipo": "comando",
                    "rowcount": cur.rowcount,
                    "mensaje": cur.statusmessage,
                }


consultas = ServicioConsultas()
