"""
Servicio ServicioProcesadores: la configuración de los procesadores de Extend,
en PostgreSQL (tabla `procesadores`).

Cada fila dice CÓMO resuelve UNA ruta de la API una operación de Extend para una
clase de documento. Así cada ruta puede usar procesadores distintos. Se edita en
caliente desde /admin/procesadores, sin redeploy.

    ruta          ruta de la API que usa la fila:
                  'clasificar'        -> POST /api/v1/clasificar/
                  'validar-identidad' -> POST /api/v1/validaciones/validar-identidad/
                  'ocr'               -> POST /api/v1/ocr/
    operacion     'clasificar' | 'extraer' | 'parse'
    clase         clase de documento ('CEDULA', 'PASAPORTE', ...) para los
                  esquemas de extracción; '' cuando no aplica.
    modo          'id'     -> procesador YA publicado en Extend (cl_.../ex_...).
                  'inline' -> config en la propia petición: clasificar usa la
                             tabla `clasificaciones`; extraer usa el JSON Schema
                             de la columna `esquema`.
    procesador_id 'cl_...'/'ex_...' cuando modo='id'.
    version       versión del procesador publicado a fijar (modo='id'); '' = última.
    esquema       JSONB: JSON Schema (extraer inline) o {"target": "..."} (parse).
    umbral        confianza mínima 0..1 del clasificador (solo 'clasificar').
    activo        TRUE = se usa; FALSE = guardada pero ignorada.

El .env sigue guardando SOLO secretos (EXTEND_API_KEY, DATABASE_URL).

Los resolutores (cuerpo_*) leen de esta tabla en cada petición y devuelven el
fragmento de body que ServicioDocumentos inyecta en la llamada a Extend.
"""
from typing import Callable, Optional

from psycopg2 import errors as pg_errors
from psycopg2.extras import Json, RealDictCursor

from app.core.db import ServicioBD
from app.services.errores import ErrorDeValidacion
from app.services.extend import extend
from app.services.rutas import rutas

OPERACIONES_VALIDAS = {"clasificar", "extraer", "parse"}
MODOS_VALIDOS = {"id", "inline"}

# Confianza mínima (0..1) para dar por válida una clasificación cuando la fila
# de 'clasificar' no define una propia (columna `umbral`).
UMBRAL_DEFECTO = 0.85

# Mapeo de nuestras operaciones a los tipos de procesador de Extend. OJO: Extend
# no tiene tipo "OCR/parse"; el parseo se configura por target, sin procesador.
_TIPO_EXTEND = {"clasificar": "CLASSIFY", "extraer": "EXTRACT"}

# Clase de descarte por defecto: Extend exige SIEMPRE una clase 'other' al
# clasificar. Se añade cuando una lista de clasificaciones no la trae.
OTRO_POR_DEFECTO = {
    "id": "otros_descarte",
    "type": "other",
    "description": (
        "Cualquier otro documento que no corresponda a ninguno de los tipos "
        "definidos: facturas, capturas de pantalla, papeles sin relación, etc."
    ),
}


def _normalizar_clasificaciones(lista: list) -> list:
    """Normaliza las clasificaciones propias de una fila ('other' en minúsculas,
    como exige Extend) y garantiza la clase de descarte."""
    salida = []
    tiene_otro = False
    for c in lista:
        tipo = str(c.get("type", "")).strip()
        if tipo.lower() in ("other", "otros"):
            tipo = "other"
            tiene_otro = True
        salida.append({
            "id": c.get("id"),
            "type": tipo,
            "description": c.get("description", ""),
        })
    if not tiene_otro:
        salida.append(OTRO_POR_DEFECTO)
    return salida


# JSON Schemas de extracción con los que se siembra la tabla. Las descripciones
# guían al modelo de extracción.
def _campo(desc: str) -> dict:
    return {"type": ["string", "null"], "description": desc}


_ESQUEMA_CEDULA = {
    "type": "object",
    "properties": {
        "numero_cedula": _campo("Número de cédula de identidad ecuatoriana (10 dígitos)."),
        "apellidos": _campo("Apellidos del titular."),
        "nombres": _campo("Nombres del titular."),
        "nacionalidad": _campo("Nacionalidad del titular."),
        "sexo": _campo("Sexo del titular (M o F)."),
        "fecha_nacimiento": _campo("Fecha de nacimiento (formato del documento)."),
        "lugar_nacimiento": _campo("Lugar de nacimiento."),
    },
}

_ESQUEMA_PASAPORTE = {
    "type": "object",
    "properties": {
        "numero_pasaporte": _campo("Número de pasaporte."),
        "pais_emisor": _campo("País emisor (código o nombre)."),
        "apellidos": _campo("Apellidos del titular (surname)."),
        "nombres": _campo("Nombres del titular (given names)."),
        "nacionalidad": _campo("Nacionalidad del titular."),
        "sexo": _campo("Sexo del titular (M o F)."),
        "fecha_nacimiento": _campo("Fecha de nacimiento (date of birth)."),
        "fecha_expiracion": _campo("Fecha de expiración del pasaporte (date of expiry)."),
    },
}

# Filas con las que se siembra la tabla la primera vez, una por (ruta, operación,
# clase). Reproduce el comportamiento por defecto: /clasificar usa SOLO su
# clasificador; /validar-identidad usa SOLO clasificador + extractores (sin OCR:
# el número se compara contra la extracción estructurada); /ocr usa el parse.
#   (ruta, operacion, clase, modo, procesador_id, version, esquema, umbral)
SEMILLA = [
    ("clasificar", "clasificar", "", "inline", None, None, None, UMBRAL_DEFECTO),
    ("validar-identidad", "clasificar", "", "inline", None, None, None, UMBRAL_DEFECTO),
    ("validar-identidad", "extraer", "CEDULA", "inline", None, None, _ESQUEMA_CEDULA, None),
    ("validar-identidad", "extraer", "PASAPORTE", "inline", None, None, _ESQUEMA_PASAPORTE, None),
    ("ocr", "parse", "", "inline", None, None, {"target": "markdown"}, None),
]

_COLUMNAS = ("id, ruta, operacion, clase, modo, procesador_id, version, esquema, "
             "umbral, activo, creado_en, actualizado_en")


class ServicioProcesadores(ServicioBD):
    """CRUD, resolutores y sincronización con Extend Studio."""

    DDL = """
        CREATE TABLE IF NOT EXISTS procesadores (
            id             SERIAL PRIMARY KEY,
            ruta           TEXT NOT NULL DEFAULT '',
            operacion      TEXT NOT NULL,
            clase          TEXT NOT NULL DEFAULT '',
            modo           TEXT NOT NULL DEFAULT 'inline',
            procesador_id  TEXT,
            version        TEXT,
            esquema        JSONB,
            umbral         REAL,
            activo         BOOLEAN NOT NULL DEFAULT TRUE,
            creado_en      TIMESTAMP NOT NULL DEFAULT now(),
            actualizado_en TIMESTAMP NOT NULL DEFAULT now(),
            UNIQUE (ruta, operacion, clase)
        )
    """
    # Columnas añadidas con el tiempo (idempotente). NOTA: si la tabla es
    # anterior a la columna `ruta`, conserva su UNIQUE(operacion, clase) viejo;
    # conviene recrearla (DROP TABLE procesadores) para tomar el nuevo
    # UNIQUE(ruta, operacion, clase) y re-sembrar por ruta.
    ALTERS = (
        "ALTER TABLE procesadores ADD COLUMN IF NOT EXISTS umbral REAL",
        "ALTER TABLE procesadores ADD COLUMN IF NOT EXISTS version TEXT",
        "ALTER TABLE procesadores ADD COLUMN IF NOT EXISTS ruta TEXT NOT NULL DEFAULT ''",
    )

    # --- Normalización y validación ---

    @staticmethod
    def _normalizar(fila: Optional[dict]) -> Optional[dict]:
        if fila is None:
            return None
        d = dict(fila)
        d["activo"] = bool(d["activo"])
        if d.get("umbral") is not None:
            d["umbral"] = float(d["umbral"])
        for campo in ("creado_en", "actualizado_en"):
            valor = d.get(campo)
            if valor is not None and not isinstance(valor, str):
                d[campo] = valor.strftime("%Y-%m-%d %H:%M:%S")
        return d

    @staticmethod
    def normalizar_ruta(ruta: str) -> str:
        return (ruta or "").strip().lower()

    @staticmethod
    def normalizar_operacion(operacion: str) -> str:
        return (operacion or "").strip().lower()

    @staticmethod
    def normalizar_clase(clase: Optional[str]) -> str:
        """Las clases se guardan en MAYÚSCULAS; '' cuando la operación no usa clase."""
        return (clase or "").strip().upper()

    @staticmethod
    def normalizar_modo(modo: str) -> str:
        return (modo or "").strip().lower()

    @staticmethod
    def _validar(ruta: str, operacion: str, modo: str,
                 procesador_id: Optional[str], esquema) -> None:
        # La ruta debe existir y estar activa en el catálogo (CRUD /api/v1/rutas/):
        # el CRUD de procesadores es la unión ruta <-> procesador.
        registradas = rutas.claves_activas()
        if ruta not in registradas:
            raise ValueError(
                f"Ruta '{ruta}' no registrada o inactiva. Rutas disponibles: "
                f"{', '.join(sorted(registradas)) or '(ninguna)'}. "
                "Regístrala primero en /admin/rutas."
            )
        if operacion not in OPERACIONES_VALIDAS:
            raise ValueError(
                f"Operación inválida '{operacion}'. Debe ser una de: "
                f"{', '.join(sorted(OPERACIONES_VALIDAS))}."
            )
        if modo not in MODOS_VALIDOS:
            raise ValueError("Modo inválido. Debe ser 'id' o 'inline'.")
        if modo == "id" and not (procesador_id or "").strip():
            raise ValueError("En modo 'id' hay que indicar el procesador_id (cl_.../ex_...).")
        if modo == "inline" and operacion == "extraer" and not esquema:
            raise ValueError("En modo 'inline' una extracción necesita un esquema JSON.")
        if operacion == "clasificar" and esquema is not None:
            clasificaciones = esquema.get("classifications") if isinstance(esquema, dict) else None
            if not isinstance(clasificaciones, list) or not clasificaciones:
                raise ValueError(
                    "Para 'clasificar', el esquema debe tener una lista 'classifications' "
                    "(id, type, description), o ser nulo para usar los prompts globales."
                )

    @staticmethod
    def _validar_umbral(umbral) -> None:
        if umbral is not None and not (0.0 <= float(umbral) <= 1.0):
            raise ValueError("El umbral de confianza debe estar entre 0 y 1 (ej. 0.85).")

    # --- Inicialización ---

    def inicializar(self) -> None:
        """Crea la tabla (idempotente) y la siembra si está vacía."""
        try:
            with self._conectar() as con:
                with con.cursor() as cur:
                    # Migración (idempotente): validar-identidad ya no usa OCR; su
                    # fila 'parse' sembrada en versiones anteriores se retira.
                    cur.execute(
                        "DELETE FROM procesadores WHERE ruta = 'validar-identidad' AND operacion = 'parse'"
                    )
                    cur.execute("SELECT COUNT(*) FROM procesadores")
                    if cur.fetchone()[0] == 0:
                        cur.executemany(
                            "INSERT INTO procesadores (ruta, operacion, clase, modo, procesador_id, version, esquema, umbral) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            [(ru, op, cl, mo, pid, ver, Json(esq) if esq is not None else None, umb)
                             for (ru, op, cl, mo, pid, ver, esq, umb) in SEMILLA],
                        )
        except pg_errors.UniqueViolation:
            # Carrera entre workers al arrancar: otro proceso sembró primero.
            pass

    # --- CRUD ---

    def listar(self, solo_activos: bool = False) -> list:
        sql = f"SELECT {_COLUMNAS} FROM procesadores"
        if solo_activos:
            sql += " WHERE activo = TRUE"
        sql += " ORDER BY ruta, operacion, clase"
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql)
                return [self._normalizar(f) for f in cur.fetchall()]

    def obtener_por_id(self, id_proc: int) -> Optional[dict]:
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT {_COLUMNAS} FROM procesadores WHERE id = %s", (id_proc,))
                return self._normalizar(cur.fetchone())

    def crear(self, ruta: str, operacion: str, clase: str, modo: str,
              procesador_id: Optional[str] = None,
              version: Optional[str] = None,
              esquema: Optional[dict] = None,
              umbral: Optional[float] = None, activo: bool = True) -> dict:
        """Inserta una fila. Lanza ValueError si los datos no son válidos y
        psycopg2.errors.UniqueViolation si ya existe esa (ruta, operacion, clase)."""
        ruta = self.normalizar_ruta(ruta)
        operacion = self.normalizar_operacion(operacion)
        clase = self.normalizar_clase(clase)
        modo = self.normalizar_modo(modo)
        procesador_id = (procesador_id or "").strip() or None
        version = (version or "").strip() or None
        self._validar(ruta, operacion, modo, procesador_id, esquema)
        self._validar_umbral(umbral)
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO procesadores (ruta, operacion, clase, modo, procesador_id, version, esquema, umbral, activo) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNAS}",
                    (ruta, operacion, clase, modo, procesador_id, version,
                     Json(esquema) if esquema is not None else None, umbral, bool(activo)),
                )
                return self._normalizar(cur.fetchone())

    def actualizar(self, id_proc: int,
                   ruta: Optional[str] = None,
                   operacion: Optional[str] = None,
                   clase: Optional[str] = None,
                   modo: Optional[str] = None,
                   procesador_id: Optional[str] = None,
                   version: Optional[str] = None,
                   esquema: Optional[dict] = None,
                   umbral: Optional[float] = None,
                   activo: Optional[bool] = None,
                   tocar_procesador_id: bool = False,
                   tocar_version: bool = False,
                   tocar_esquema: bool = False,
                   tocar_umbral: bool = False) -> Optional[dict]:
        """
        Actualiza solo los campos enviados. `procesador_id`, `version`, `esquema`
        y `umbral` pueden ponerse a NULL explícitamente; por eso sus banderas
        `tocar_*` indican si el campo viene en la petición (aunque su valor sea
        None). Devuelve la fila o None si no existe. Lanza ValueError si el
        resultado no es válido.
        """
        actual = self.obtener_por_id(id_proc)
        if actual is None:
            return None

        n_ruta = self.normalizar_ruta(ruta) if ruta is not None else actual["ruta"]
        n_operacion = self.normalizar_operacion(operacion) if operacion is not None else actual["operacion"]
        n_clase = self.normalizar_clase(clase) if clase is not None else actual["clase"]
        n_modo = self.normalizar_modo(modo) if modo is not None else actual["modo"]
        n_procesador = actual["procesador_id"]
        if tocar_procesador_id:
            n_procesador = (procesador_id or "").strip() or None
        n_version = actual["version"]
        if tocar_version:
            n_version = (version or "").strip() or None
        n_esquema = actual["esquema"]
        if tocar_esquema:
            n_esquema = esquema
        n_umbral = actual["umbral"]
        if tocar_umbral:
            n_umbral = umbral
        n_activo = actual["activo"] if activo is None else bool(activo)

        self._validar(n_ruta, n_operacion, n_modo, n_procesador, n_esquema)
        self._validar_umbral(n_umbral)

        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE procesadores SET ruta = %s, operacion = %s, clase = %s, modo = %s, "
                    "procesador_id = %s, version = %s, esquema = %s, umbral = %s, activo = %s, "
                    "actualizado_en = now() WHERE id = %s",
                    (n_ruta, n_operacion, n_clase, n_modo, n_procesador, n_version,
                     Json(n_esquema) if n_esquema is not None else None, n_umbral, n_activo, id_proc),
                )
        return self.obtener_por_id(id_proc)

    def eliminar(self, id_proc: int) -> bool:
        """Borra una fila. Devuelve False si el id no existía."""
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute("DELETE FROM procesadores WHERE id = %s", (id_proc,))
                return cur.rowcount > 0

    # --- Resolutores: traducen la fila activa al fragmento de body de Extend ---

    def _obtener_activa(self, ruta: str, operacion: str, clase: str) -> Optional[dict]:
        """Fila activa para (ruta, operacion, clase), o None si no hay ninguna."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"SELECT {_COLUMNAS} FROM procesadores "
                    "WHERE ruta = %s AND operacion = %s AND clase = %s AND activo = TRUE",
                    (ruta, operacion, clase),
                )
                return self._normalizar(cur.fetchone())

    @staticmethod
    def _ref_procesador(fila: dict) -> dict:
        """
        Referencia a un procesador publicado: {id, [version]}. La versión solo
        se incluye si la fila la fija; sin versión el body queda idéntico al de
        antes (Extend usa la última versión publicada). El nombre exacto del
        campo de versión conviene confirmarlo contra la cuenta de Extend.
        """
        ref = {"id": fila["procesador_id"]}
        if fila.get("version"):
            ref["version"] = fila["version"]
        return ref

    def cuerpo_clasificacion(self, ruta: str, construir_inline: Callable[[], list]) -> dict:
        """
        Fragmento de body para /classify en la ruta dada, en orden de prioridad:
        1. modo 'id'  -> el clasificador publicado en Extend.
        2. modo 'inline' con clasificaciones propias en la fila (esquema =
           {"classifications": [{id, type, description}, ...]}) -> esas, con la
           clase de descarte 'other' garantizada. Permite clasificaciones
           distintas por ruta.
        3. sin esquema -> las clasificaciones globales de la tabla
           `clasificaciones` (callable, para no tocarla cuando no hace falta).
        """
        fila = self._obtener_activa(ruta, "clasificar", "")
        if fila and fila["modo"] == "id" and fila["procesador_id"]:
            return {"classifier": self._ref_procesador(fila)}
        esquema = (fila or {}).get("esquema")
        propias = esquema.get("classifications") if isinstance(esquema, dict) else None
        if propias:
            return {"config": {"classifications": _normalizar_clasificaciones(propias)}}
        return {"config": {"classifications": construir_inline()}}

    def umbral_clasificacion(self, ruta: str) -> float:
        """
        Confianza mínima (0..1) para dar por válida una clasificación en la
        ruta. La toma de la fila de 'clasificar'; si no tiene una configurada,
        usa UMBRAL_DEFECTO.
        """
        fila = self._obtener_activa(ruta, "clasificar", "")
        if fila and fila.get("umbral") is not None:
            return float(fila["umbral"])
        return UMBRAL_DEFECTO

    def cuerpo_extraccion(self, ruta: str, clase: str) -> Optional[dict]:
        """
        Fragmento de body para /extract en la ruta. Busca primero la fila
        específica de la clase y, si no hay, una fila global de extracción
        (clase ''). Usa el extractor publicado (modo 'id') o el JSON Schema
        inline. Devuelve None si no hay forma de extraer esa clase.
        """
        clase = self.normalizar_clase(clase)
        fila = (self._obtener_activa(ruta, "extraer", clase)
                or self._obtener_activa(ruta, "extraer", ""))
        if not fila:
            return None
        if fila["modo"] == "id" and fila["procesador_id"]:
            return {"processor": self._ref_procesador(fila)}
        if fila["modo"] == "inline" and fila["esquema"]:
            return {"config": {"schema": fila["esquema"]}}
        return None

    def soporta_extraccion(self, ruta: str, clase: str) -> bool:
        """¿Hay forma de extraer campos para esta clase en la ruta? (extractor o esquema)."""
        return self.cuerpo_extraccion(ruta, clase) is not None

    def cuerpo_parse(self, ruta: str) -> dict:
        """Fragmento de body para /parse (OCR) en la ruta. Usa el target configurado o markdown."""
        fila = self._obtener_activa(ruta, "parse", "")
        target = "markdown"
        if fila and isinstance(fila.get("esquema"), dict):
            target = fila["esquema"].get("target") or target
        return {"config": {"target": target}}

    # --- Sincronización con Extend Studio ---

    async def listar_de_extend(self, tipo: str) -> list:
        """
        Lista los procesadores publicados en Extend Studio para una operación.
        `tipo` es 'clasificar' (CLASSIFY) o 'extraer' (EXTRACT). Devuelve
        [{id, nombre, tipo, versiones:[{id, version}]}]. Sirve para elegir el
        procesador en /admin en vez de pegar el id a mano.
        """
        tipo_extend = _TIPO_EXTEND.get((tipo or "").strip().lower())
        if not tipo_extend:
            raise ErrorDeValidacion("El tipo debe ser 'clasificar' o 'extraer'.")

        crudos = await extend.listar_procesadores(tipo_extend)
        return [{
            "id": p.get("id"),
            "nombre": p.get("name"),
            "tipo": p.get("type"),
            "versiones": [{"id": v.get("id"), "version": str(v.get("version"))}
                          for v in (p.get("versions") or [])],
        } for p in crudos]

    async def esquema_de_extend(self, procesador_id: str, version_id: str) -> dict:
        """
        Devuelve la configuración importable de una versión de un procesador
        publicado en Extend, para volcarla al schema builder:
            EXTRACT  -> el JSON Schema (version.config.schema)
            CLASSIFY -> las clasificaciones ({"classifications": [...]})
        Devuelve {} si la versión no trae nada importable.
        """
        pid = (procesador_id or "").strip()
        vid = (version_id or "").strip()
        if not pid or not vid:
            raise ErrorDeValidacion("Hace falta procesador_id y version_id.")
        version = await extend.obtener_version_procesador(pid, vid)
        config = version.get("config") or {}
        if config.get("schema"):
            return config["schema"]
        if config.get("classifications"):
            return {"classifications": config["classifications"]}
        return {}

    async def actualizar_en_extend(self, id_proc: int, publicar: bool = False) -> Optional[dict]:
        """
        Empuja el esquema GUARDADO de la fila a su procesador publicado en Extend
        (POST /processors/{id}): el JSON Schema para extractores (EXTRACT) o las
        clasificaciones para clasificadores (CLASSIFY). Actualiza la versión
        BORRADOR del procesador; con `publicar=True` además publica el borrador
        como versión nueva (release minor) — las rutas en 'última publicada' la
        usan de inmediato. Devuelve None si la fila no existe (-> 404).
        """
        fila = self.obtener_por_id(id_proc)
        if fila is None:
            return None
        pid = fila.get("procesador_id")
        if not pid:
            raise ErrorDeValidacion(
                "La fila no tiene un procesador de Extend asociado (procesador_id); "
                "no hay nada que actualizar en Extend."
            )
        esquema = fila.get("esquema")
        if fila["operacion"] == "clasificar":
            propias = esquema.get("classifications") if isinstance(esquema, dict) else None
            if not propias:
                raise ErrorDeValidacion(
                    "La fila no tiene clasificaciones guardadas para enviar a Extend."
                )
            config = {"type": "CLASSIFY",
                      "classifications": _normalizar_clasificaciones(propias)}
        elif fila["operacion"] == "extraer":
            if not esquema:
                raise ErrorDeValidacion("La fila no tiene esquema guardado para enviar a Extend.")
            config = {"type": "EXTRACT", "schema": esquema}
        else:
            raise ErrorDeValidacion(
                "Solo los clasificadores y extractores existen como procesadores en Extend."
            )

        datos = await extend.actualizar_procesador(pid, config)
        borrador = ((datos.get("processor") or datos).get("draftVersion") or {})
        resultado = {
            "procesador_id": pid,
            "operacion": fila["operacion"],
            "version_borrador": borrador.get("id"),
            "version_publicada": None,
        }
        if publicar:
            pub = await extend.publicar_procesador(pid, "minor")
            version = pub.get("version") or pub.get("processorVersion") or pub
            if isinstance(version, dict):
                resultado["version_publicada"] = version.get("version")
        return resultado


# Instancia única del servicio.
procesadores = ServicioProcesadores()
