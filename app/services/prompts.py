"""
Servicio ServicioPrompts: las clasificaciones (prompts) del clasificador de
Extend, en PostgreSQL.

Cada fila es una clase que se le envía a Extend al clasificar:

    clave        identificador corto para buscarla ("cedula", "pasaporte", ...)
    tipo         etiqueta que devuelve el clasificador ("CEDULA", "PASAPORTE").
                 La etiqueta "other" (minúsculas) es la clase de descarte que
                 Extend exige tener siempre.
    descripcion  el prompt: la descripción que guía al modelo.
    activo       TRUE = se incluye al clasificar, FALSE = guardada pero ignorada.

Lo administra la view adm_prompts (página /admin/prompts y endpoints
/api/v1/prompts/).
"""
from typing import Optional

from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor

from app.core.db import ServicioBD

# Clasificaciones con las que se siembra la tabla la primera vez.
SEMILLA = [
    ("cedula", "CEDULA",
     "Cédula de identidad ecuatoriana: documento de identificación personal "
     "emitido por el Registro Civil del Ecuador. Contiene número de cédula de "
     "10 dígitos, nombres y apellidos, foto, fecha de nacimiento y nacionalidad."),
    ("pasaporte", "PASAPORTE",
     "Pasaporte: documento de viaje con foto, datos del titular y una zona MRZ "
     "(dos líneas legibles por máquina) al pie con el número de pasaporte, "
     "nacionalidad y fechas."),
    ("otros", "other",
     "Cualquier otro documento que no sea una cédula ni un pasaporte: "
     "facturas, certificados, capturas de pantalla, etc."),
]


class ServicioPrompts(ServicioBD):
    """CRUD de las clasificaciones (prompts) del clasificador."""

    DDL = """
        CREATE TABLE IF NOT EXISTS clasificaciones (
            id             SERIAL PRIMARY KEY,
            clave          TEXT NOT NULL UNIQUE,
            tipo           TEXT NOT NULL,
            descripcion    TEXT NOT NULL,
            activo         BOOLEAN NOT NULL DEFAULT TRUE,
            creado_en      TIMESTAMP NOT NULL DEFAULT now(),
            actualizado_en TIMESTAMP NOT NULL DEFAULT now()
        )
    """

    @staticmethod
    def _normalizar(fila: Optional[dict]) -> Optional[dict]:
        if fila is None:
            return None
        d = dict(fila)
        d["activo"] = bool(d["activo"])
        for campo in ("creado_en", "actualizado_en"):
            valor = d.get(campo)
            if valor is not None and not isinstance(valor, str):
                d[campo] = valor.strftime("%Y-%m-%d %H:%M:%S")
        return d

    @staticmethod
    def normalizar_clave(clave: str) -> str:
        """Las claves se guardan y buscan en minúsculas y sin espacios alrededor."""
        return (clave or "").strip().lower()

    @staticmethod
    def normalizar_tipo(tipo: str) -> str:
        """Etiquetas en MAYÚSCULAS, salvo 'other' (la clase de descarte de Extend)."""
        tipo = (tipo or "").strip()
        return "other" if tipo.lower() == "other" else tipo.upper()

    def inicializar(self) -> None:
        """Crea la tabla (idempotente) y la siembra si está vacía."""
        try:
            with self._conectar() as con:
                with con.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM clasificaciones")
                    if cur.fetchone()[0] == 0:
                        cur.executemany(
                            "INSERT INTO clasificaciones (clave, tipo, descripcion) VALUES (%s, %s, %s)",
                            SEMILLA,
                        )
        except pg_errors.UniqueViolation:
            # Carrera entre workers al arrancar: otro proceso sembró primero.
            pass

    def listar(self, solo_activos: bool = False) -> list:
        sql = "SELECT id, clave, tipo, descripcion, activo, creado_en, actualizado_en FROM clasificaciones"
        if solo_activos:
            sql += " WHERE activo = TRUE"
        sql += " ORDER BY clave"
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql)
                return [self._normalizar(f) for f in cur.fetchall()]

    def obtener(self, clave: str) -> Optional[dict]:
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, clave, tipo, descripcion, activo, creado_en, actualizado_en "
                    "FROM clasificaciones WHERE clave = %s",
                    (self.normalizar_clave(clave),),
                )
                return self._normalizar(cur.fetchone())

    def crear(self, clave: str, tipo: str, descripcion: str, activo: bool = True) -> dict:
        """Inserta una clasificación nueva. Lanza psycopg2.errors.UniqueViolation si la clave existe."""
        clave = self.normalizar_clave(clave)
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO clasificaciones (clave, tipo, descripcion, activo) "
                    "VALUES (%s, %s, %s, %s)",
                    (clave, self.normalizar_tipo(tipo), descripcion.strip(), bool(activo)),
                )
        return self.obtener(clave)

    def actualizar(
        self,
        clave: str,
        tipo: Optional[str] = None,
        descripcion: Optional[str] = None,
        activo: Optional[bool] = None,
    ) -> Optional[dict]:
        """Actualiza solo los campos enviados. Devuelve la fila o None si no existe."""
        actual = self.obtener(clave)
        if actual is None:
            return None
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE clasificaciones SET tipo = %s, descripcion = %s, activo = %s, "
                    "actualizado_en = now() WHERE clave = %s",
                    (
                        self.normalizar_tipo(tipo) if tipo is not None else actual["tipo"],
                        descripcion.strip() if descripcion is not None else actual["descripcion"],
                        bool(activo) if activo is not None else actual["activo"],
                        actual["clave"],
                    ),
                )
        return self.obtener(clave)

    def eliminar(self, clave: str) -> bool:
        """Borra una clasificación. Devuelve False si la clave no existía."""
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM clasificaciones WHERE clave = %s",
                    (self.normalizar_clave(clave),),
                )
                return cur.rowcount > 0


# Instancia única del servicio.
prompts = ServicioPrompts()
