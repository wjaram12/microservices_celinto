"""
Servicio ServicioRutas: el catálogo de rutas (URLs) de la API, en PostgreSQL.

Cada fila describe una ruta de inferencia del servicio:

    clave        identificador lógico ('clasificar', 'validar-identidad', 'ocr').
                 Es lo que referencia la tabla `procesadores` (columna ruta):
                 el CRUD de procesadores une una ruta con sus procesadores.
    url          el endpoint real ('/api/v1/clasificar/', ...). Informativo.
    descripcion  qué hace la ruta.
    activo       TRUE = se pueden asociar procesadores nuevos a esta ruta.

IMPORTANTE: una ruta nueva en esta tabla NO crea el endpoint; los endpoints
viven en app/views/ y usan las constantes RUTA_* de ServicioDocumentos. Esta
tabla gobierna QUÉ claves de ruta acepta el CRUD de procesadores y documenta
el mapeo clave -> URL para el panel.
"""
from typing import Optional

from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor

from app.core.db import ServicioBD

SEMILLA = [
    ("clasificar", "/api/v1/clasificar/",
     "Clasifica un documento y devuelve clase, confianza y validez."),
    ("validar-identidad", "/api/v1/validaciones/validar-identidad/",
     "Valida un documento de identidad: clasifica, extrae campos y compara la cédula."),
    ("ocr", "/api/v1/ocr/",
     "Extrae el texto del documento (OCR) con búsqueda opcional de un término."),
    ("validar-registro-senescyt", "/api/v1/validaciones/validar-registro-senescyt/",
     "Valida un registro de título de la SENESCYT: clasifica y extrae su información."),
]


class ServicioRutas(ServicioBD):
    """CRUD del catálogo de rutas de la API."""

    DDL = """
        CREATE TABLE IF NOT EXISTS rutas (
            id             SERIAL PRIMARY KEY,
            clave          TEXT NOT NULL UNIQUE,
            url            TEXT NOT NULL,
            descripcion    TEXT NOT NULL DEFAULT '',
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
        """Claves en minúsculas, sin espacios; los espacios internos se vuelven guiones."""
        return "-".join((clave or "").strip().lower().split())

    def inicializar(self) -> None:
        """Crea la tabla (idempotente) y la siembra si está vacía. Tolera la
        carrera entre workers al arrancar."""
        try:
            with self._conectar() as con:
                with con.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM rutas")
                    if cur.fetchone()[0] == 0:
                        cur.executemany(
                            "INSERT INTO rutas (clave, url, descripcion) VALUES (%s, %s, %s)",
                            SEMILLA,
                        )
        except pg_errors.UniqueViolation:
            pass

    def listar(self, solo_activos: bool = False) -> list:
        sql = "SELECT id, clave, url, descripcion, activo, creado_en, actualizado_en FROM rutas"
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
                    "SELECT id, clave, url, descripcion, activo, creado_en, actualizado_en "
                    "FROM rutas WHERE clave = %s",
                    (self.normalizar_clave(clave),),
                )
                return self._normalizar(cur.fetchone())

    def claves_activas(self) -> set:
        """Claves de las rutas activas: lo que acepta el CRUD de procesadores."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT clave FROM rutas WHERE activo = TRUE")
                return {f["clave"] for f in cur.fetchall()}

    def crear(self, clave: str, url: str, descripcion: str = "", activo: bool = True) -> dict:
        """Inserta una ruta. Lanza psycopg2.errors.UniqueViolation si la clave existe."""
        clave = self.normalizar_clave(clave)
        if not clave:
            raise ValueError("La clave de la ruta no puede estar vacía.")
        url = (url or "").strip()
        if not url.startswith("/"):
            raise ValueError("La URL debe ser una ruta del servicio (empezar con '/').")
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO rutas (clave, url, descripcion, activo) VALUES (%s, %s, %s, %s)",
                    (clave, url, (descripcion or "").strip(), bool(activo)),
                )
        return self.obtener(clave)

    def actualizar(
        self,
        clave: str,
        url: Optional[str] = None,
        descripcion: Optional[str] = None,
        activo: Optional[bool] = None,
    ) -> Optional[dict]:
        """Actualiza solo los campos enviados (la clave es inmutable: es el
        identificador que referencian los procesadores). None si no existe."""
        actual = self.obtener(clave)
        if actual is None:
            return None
        if url is not None:
            url = url.strip()
            if not url.startswith("/"):
                raise ValueError("La URL debe ser una ruta del servicio (empezar con '/').")
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE rutas SET url = %s, descripcion = %s, activo = %s, "
                    "actualizado_en = now() WHERE clave = %s",
                    (
                        url if url is not None else actual["url"],
                        descripcion.strip() if descripcion is not None else actual["descripcion"],
                        bool(activo) if activo is not None else actual["activo"],
                        actual["clave"],
                    ),
                )
        return self.obtener(clave)

    def eliminar(self, clave: str) -> bool:
        """Borra una ruta. Devuelve False si la clave no existía. La view valida
        antes que no tenga procesadores asociados."""
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute("DELETE FROM rutas WHERE clave = %s", (self.normalizar_clave(clave),))
                return cur.rowcount > 0


rutas = ServicioRutas()
