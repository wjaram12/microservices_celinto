"""
Servicio APIConsumidores: las API keys de los sistemas consumidores.

Cada sistema consumidor (celinto-posgrados, ucg-posgrados, ucg-on) tiene su
propia clave. Se envía en la cabecera `X-API-Key` y se valida contra la tabla
`api_keys`, donde la clave NUNCA se guarda en texto plano, solo su hash SHA-256.
Así, aunque alguien lea la base, no puede reconstruir las claves.

Las claves se gestionan con el CLI `gestionar_llaves.py` (bootstrap) o con la
view adm_consumidores. El valor en texto plano se muestra UNA sola vez al
crearla; después es irrecuperable.

Vive en `commons` porque lo usan tanto el clasificador como la consulta de títulos
(mismo sistema de API keys).
"""
import hashlib
import secrets
from typing import Optional

from psycopg2.extras import RealDictCursor

from commons.db import ServicioBD

PREFIJO_LLAVE = "wsk_"

SCOPES_VALIDOS = {"consumo", "admin"}


class APIConsumidores(ServicioBD):
    """CRUD y verificación de las API keys de los consumidores."""

    DDL = """
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

    @staticmethod
    def _hash(llave: str) -> str:
        """Hash determinista de la clave. SHA-256 basta: la clave es de alta entropía."""
        return hashlib.sha256(llave.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalizar_fila(fila: Optional[dict]) -> Optional[dict]:
        """Fila de psycopg2 -> dict simple, timestamps como texto (los esquemas los esperan str)."""
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

    def crear(self, consumidor: str, scope: str = "consumo") -> dict:
        """
        Genera una clave nueva para un consumidor y la registra (hasheada).

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

        llave = PREFIJO_LLAVE + secrets.token_urlsafe(32)
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO api_keys (consumidor, key_hash, scope) VALUES (%s, %s, %s) "
                    "RETURNING id, consumidor, scope, activo, creado_en, ultimo_uso",
                    (consumidor, self._hash(llave), scope),
                )
                fila = cur.fetchone()

        registro = self._normalizar_fila(fila)
        registro["llave"] = llave
        return registro

    def listar(self) -> list:
        """Lista los metadatos de las claves (nunca la clave ni el hash)."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, consumidor, scope, activo, creado_en, ultimo_uso "
                    "FROM api_keys ORDER BY consumidor, id"
                )
                filas = cur.fetchall()
        return [self._normalizar_fila(f) for f in filas]

    def obtener(self, id_llave: int) -> Optional[dict]:
        """Metadatos de una clave por su id (nunca el hash). None si no existe."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, consumidor, scope, activo, creado_en, ultimo_uso "
                    "FROM api_keys WHERE id = %s",
                    (id_llave,),
                )
                fila = cur.fetchone()
        return self._normalizar_fila(fila)

    def actualizar(
        self,
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
        actual = self.obtener(id_llave)
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

        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE api_keys SET consumidor = %s, scope = %s, activo = %s WHERE id = %s",
                    (nuevo_consumidor, nuevo_scope, nuevo_activo, id_llave),
                )
        return self.obtener(id_llave)

    def revocar(self, identificador: str) -> int:
        """
        Desactiva (no borra) las claves de un consumidor o un id concreto.
        Se conserva la fila para que el rastro de auditoría siga siendo válido.
        Devuelve cuántas filas se desactivaron.
        """
        identificador = identificador.strip()
        with self._conectar() as con:
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

    def verificar(self, llave: str) -> Optional[dict]:
        """Busca una clave activa por su hash y registra el último uso. None si no hay."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, consumidor, scope, activo FROM api_keys WHERE key_hash = %s",
                    (self._hash(llave),),
                )
                fila = cur.fetchone()
                if fila is None or not fila["activo"]:
                    return None
                cur.execute(
                    "UPDATE api_keys SET ultimo_uso = now() WHERE id = %s",
                    (fila["id"],),
                )
                return {
                    "id": fila["id"],
                    "consumidor": fila["consumidor"],
                    "scope": fila["scope"],
                }


consumidores = APIConsumidores()
