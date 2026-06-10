"""
Autenticación servicio-a-servicio por API keys.

Cada sistema consumidor (celinto-posgrados, ucg-posgrados, ucg-on) tiene su
propia clave. Se envía en la cabecera `X-API-Key` y se valida contra una
tabla en PostgreSQL donde la clave NUNCA se guarda en texto plano, solo su
hash SHA-256. Así, aunque alguien lea la base, no puede reconstruir las claves.

Las claves se gestionan con el script `gestionar_llaves.py` (bootstrap) o con
el CRUD admin de la API. El valor en texto plano se muestra UNA sola vez al
crearla; después es irrecuperable.
"""
import contextlib
import hashlib
import secrets
from typing import Optional

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor

from fastapi import Depends, Header, HTTPException, status

from app.core.config import settings

# Prefijo identificable para reconocer una clave del servicio de un vistazo
# (útil en logs de los consumidores y para detectar fugas con escáneres).
PREFIJO_LLAVE = "wsk_"  # whistle service key

# Scopes (permisos) que puede tener una clave:
#   - consumo: clasificar y hacer OCR (lo que usan los sistemas consumidores).
#   - admin:   además, gestionar los prompts del clasificador y las API keys.
SCOPES_VALIDOS = {"consumo", "admin"}

# DDL de la tabla en PostgreSQL. Se asegura una vez por proceso (ver _conectar).
_DDL_API_KEYS = """
    CREATE TABLE IF NOT EXISTS api_keys (
        id          SERIAL PRIMARY KEY,
        consumidor  TEXT NOT NULL,
        key_hash    TEXT NOT NULL UNIQUE,
        scope       TEXT NOT NULL DEFAULT 'consumo',
        activo      BOOLEAN NOT NULL DEFAULT TRUE,
        creado_en   TIMESTAMP NOT NULL DEFAULT now(),
        ultimo_uso  TIMESTAMP
    )
"""

# Bandera para crear la tabla una sola vez por proceso (no en cada petición).
_tabla_lista = False


def _asegurar_tabla(con) -> None:
    """
    Crea la tabla si falta, TOLERANDO la carrera entre procesos.

    `CREATE TABLE IF NOT EXISTS` no es del todo atómico: si varios workers
    arrancan a la vez, todos ven que la tabla no existe y la intentan crear;
    uno gana y los demás fallan con UniqueViolation sobre `pg_type`. En ese
    caso la tabla YA existe, así que el error se ignora (era justo el objetivo).
    """
    try:
        with con.cursor() as cur:
            cur.execute(_DDL_API_KEYS)
        con.commit()
    except (pg_errors.UniqueViolation, pg_errors.DuplicateTable, pg_errors.DuplicateObject):
        # Otro worker creó la tabla primero: no es un error real.
        con.rollback()


@contextlib.contextmanager
def _conectar():
    """
    Abre una conexión a PostgreSQL, hace commit al salir bien (o rollback si
    hay error) y la cierra siempre. La primera vez en el proceso garantiza
    que la tabla exista (idempotente y a prueba de carreras entre workers).

    Una conexión por operación: para el volumen de este servicio es suficiente
    y simple. Si en el futuro hay alta concurrencia, conviene un pool
    (psycopg2.pool) o PgBouncer delante.
    """
    global _tabla_lista
    con = psycopg2.connect(settings.DATABASE_URL)
    try:
        if not _tabla_lista:
            _asegurar_tabla(con)
            _tabla_lista = True
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _hash(llave: str) -> str:
    """Hash determinista de la clave. SHA-256 basta: la clave es de alta entropía."""
    return hashlib.sha256(llave.encode("utf-8")).hexdigest()


def _normalizar_fila(fila: Optional[dict]) -> Optional[dict]:
    """
    Convierte una fila de psycopg2 (RealDictRow) en un dict simple, con los
    timestamps como texto 'YYYY-MM-DD HH:MM:SS' (los esquemas los esperan str).
    """
    if fila is None:
        return None
    d = dict(fila)
    if "activo" in d:
        d["activo"] = bool(d["activo"])
    for campo in ("creado_en", "ultimo_uso"):
        valor = d.get(campo)
        if valor is not None and not isinstance(valor, str):
            d[campo] = valor.strftime("%Y-%m-%d %H:%M:%S")
    return d


def inicializar() -> None:
    """
    Garantiza que la tabla exista en PostgreSQL (idempotente). Se llama al
    arrancar el servidor y desde el CLI; la creación real la hace `_conectar`
    la primera vez. Sirve para fallar pronto si la base no es accesible.
    """
    with _conectar():
        pass


def crear_llave(consumidor: str, scope: str = "consumo") -> dict:
    """
    Genera una clave nueva para un consumidor y la registra (hasheada).

    `scope` controla los permisos: 'consumo' (por defecto) para clasificar/OCR,
    o 'admin' para además gestionar prompts y claves.

    Devuelve un dict con la clave EN TEXTO PLANO (`llave`): es la única vez
    que existe fuera del hash, hay que entregársela al consumidor en ese
    momento. El resto de campos son los de la fila guardada.
    """
    consumidor = consumidor.strip()
    if not consumidor:
        raise ValueError("El nombre del consumidor no puede estar vacío.")

    scope = (scope or "consumo").strip().lower()
    if scope not in SCOPES_VALIDOS:
        raise ValueError(
            f"Scope inválido '{scope}'. Debe ser uno de: "
            f"{', '.join(sorted(SCOPES_VALIDOS))}."
        )

    # token_urlsafe(32) -> ~256 bits de entropía: imposible de adivinar.
    llave = PREFIJO_LLAVE + secrets.token_urlsafe(32)
    with _conectar() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO api_keys (consumidor, key_hash, scope) VALUES (%s, %s, %s) "
                "RETURNING id, consumidor, scope, activo, creado_en, ultimo_uso",
                (consumidor, _hash(llave), scope),
            )
            fila = cur.fetchone()

    registro = _normalizar_fila(fila)
    registro["llave"] = llave  # solo en este retorno; nunca se vuelve a tener
    return registro


def listar_llaves() -> list:
    """Lista los metadatos de las claves (nunca la clave ni el hash)."""
    with _conectar() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, consumidor, scope, activo, creado_en, ultimo_uso "
                "FROM api_keys ORDER BY consumidor, id"
            )
            filas = cur.fetchall()
    return [_normalizar_fila(f) for f in filas]


def revocar_llave(identificador: str) -> int:
    """
    Desactiva (no borra) las claves de un consumidor o un id concreto.
    Se conserva la fila para que el rastro de auditoría siga siendo válido.
    Devuelve cuántas filas se desactivaron.
    """
    identificador = identificador.strip()
    with _conectar() as con:
        with con.cursor() as cur:
            if identificador.isdigit():
                cur.execute(
                    "UPDATE api_keys SET activo = FALSE WHERE id = %s AND activo = TRUE",
                    (int(identificador),),
                )
            else:
                cur.execute(
                    "UPDATE api_keys SET activo = FALSE WHERE consumidor = %s AND activo = TRUE",
                    (identificador,),
                )
            return cur.rowcount


def obtener_llave(id_llave: int) -> Optional[dict]:
    """Devuelve los metadatos de una clave por su id (nunca el hash). None si no existe."""
    with _conectar() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, consumidor, scope, activo, creado_en, ultimo_uso "
                "FROM api_keys WHERE id = %s",
                (id_llave,),
            )
            fila = cur.fetchone()
    return _normalizar_fila(fila)


def actualizar_llave(
    id_llave: int,
    consumidor: Optional[str] = None,
    scope: Optional[str] = None,
    activo: Optional[bool] = None,
) -> Optional[dict]:
    """
    Actualiza los metadatos de una clave (nombre, scope o estado activo).
    Solo cambia los campos enviados. NO permite cambiar el hash: rotar una
    clave es crear otra nueva y revocar esta. Devuelve la clave o None si no
    existe. Lanza ValueError si el scope o el consumidor son inválidos.
    """
    actual = obtener_llave(id_llave)
    if actual is None:
        return None

    nuevo_consumidor = actual["consumidor"]
    if consumidor is not None:
        nuevo_consumidor = consumidor.strip()
        if not nuevo_consumidor:
            raise ValueError("El nombre del consumidor no puede estar vacío.")

    nuevo_scope = actual["scope"]
    if scope is not None:
        nuevo_scope = scope.strip().lower()
        if nuevo_scope not in SCOPES_VALIDOS:
            raise ValueError(
                f"Scope inválido '{nuevo_scope}'. Debe ser uno de: "
                f"{', '.join(sorted(SCOPES_VALIDOS))}."
            )

    nuevo_activo = actual["activo"] if activo is None else bool(activo)

    with _conectar() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET consumidor = %s, scope = %s, activo = %s WHERE id = %s",
                (nuevo_consumidor, nuevo_scope, nuevo_activo, id_llave),
            )
    return obtener_llave(id_llave)


def _buscar_activa(llave: str) -> Optional[dict]:
    """Busca una clave activa por su hash y registra el último uso. None si no hay."""
    with _conectar() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, consumidor, scope, activo FROM api_keys WHERE key_hash = %s",
                (_hash(llave),),
            )
            fila = cur.fetchone()
            if fila is None or not fila["activo"]:
                return None
            # Registrar el último uso para auditoría.
            cur.execute(
                "UPDATE api_keys SET ultimo_uso = now() WHERE id = %s",
                (fila["id"],),
            )
            return {
                "id": fila["id"],
                "consumidor": fila["consumidor"],
                "scope": fila["scope"],
            }


def verificar_api_key(
    x_api_key: Optional[str] = Header(
        None,
        alias="X-API-Key",
        description="API key del sistema consumidor.",
    ),
) -> dict:
    """
    Dependencia de FastAPI: valida la cabecera `X-API-Key` y devuelve el
    consumidor autenticado ({"id", "consumidor", "scope"}). Lanza 401 si falta
    o no es válida. Se cuelga del router para proteger todos los endpoints de
    inferencia de una sola vez.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación X-API-Key.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    registro = _buscar_activa(x_api_key)
    if registro is None:
        # Mismo mensaje para clave inexistente, mal formada o revocada: no se
        # le da pistas a un atacante sobre por qué falló.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key inválida o revocada.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return registro


def requiere_admin(registro: dict = Depends(verificar_api_key)) -> dict:
    """
    Dependencia para los endpoints de administración (gestión de prompts y
    de API keys).

    Reutiliza `verificar_api_key` (así una clave ausente/ inválida sigue dando
    401) y además exige scope 'admin'. Una clave válida pero de consumo recibe
    **403** (autenticada, pero sin permiso): así las claves de los sistemas
    consumidores nunca pueden tocar la administración.
    """
    if registro.get("scope") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta operación requiere una API key de administrador.",
        )
    return registro
