"""
Registro de vínculos persona (cédula) <-> cuenta de Google.

Es el equivalente en PostgreSQL del `externalId` que se escribe en Google, con lo
que Google NO puede guardar: **cuándo** se registró el vínculo y **qué sistema** lo
registró. Ambos existen a propósito:

  - Google es la FUENTE DE VERDAD. El `externalId` viaja con la cuenta, sobrevive a
    un cambio de correo, se ve en la consola de administración y lo puede leer
    cualquier otra herramienta. Si la tabla y Google discrepan, gana Google.

  - Esta tabla es un ÍNDICE con trazabilidad. Responde en milisegundos (Google tarda
    ~500 ms y comparte una cuota de ~2 400 peticiones/minuto entre todos los
    sistemas) y añade fecha y consumidor. Se puede reconstruir entera desde Google.

Una persona puede tener VARIAS cuentas vivas a la vez (docente y estudiante, o
administrativa y exalumna): por eso la clave no es la cédula sino el par
(identificacion, google_id), y una de las filas se marca como `principal` con la
regla de google_services.jerarquia.

Además esta tabla aporta algo que Google no ofrece: un **cerrojo por cédula**. Con
tres sistemas dando altas, dos pueden comprobar «¿existe la cuenta?» a la vez, ver
que no, y crear dos cuentas con correos distintos para la misma persona. El cerrojo
de PostgreSQL serializa esa sección crítica.
"""
import contextlib
import logging
from typing import Optional

from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor, execute_values

from commons.db import ServicioBD

from .errores import ErrorDeConflicto

logger = logging.getLogger(__name__)

# De dónde salió el vínculo. Sirve para auditar y para reconstruir.
ORIGENES = {"backfill", "creacion", "sincronizacion", "manual"}


class ServicioVinculos(ServicioBD):
    """Vínculos entre la cédula de una persona y sus cuentas de Google."""

    DDL = """
        CREATE TABLE IF NOT EXISTS google_vinculos (
            id             SERIAL PRIMARY KEY,
            identificacion TEXT NOT NULL,
            google_id      TEXT NOT NULL,
            email          TEXT NOT NULL,
            ou             TEXT,
            principal      BOOLEAN NOT NULL DEFAULT TRUE,
            consumidor     TEXT NOT NULL,
            origen         TEXT NOT NULL DEFAULT 'creacion',
            creado_en      TIMESTAMP NOT NULL DEFAULT now(),
            actualizado_en TIMESTAMP,
            UNIQUE (identificacion, google_id)
        )
    """

    # Índices para las tres búsquedas reales: por persona (lo que piden los tres
    # sistemas), por cuenta (al sincronizar) y por correo (al auditar una dirección).
    #
    # El último es una RESTRICCIÓN, no un índice de rendimiento: una persona tiene
    # como mucho una cuenta principal. Sin esto, registrar la segunda cuenta de
    # alguien (docente además de estudiante) dejaría dos filas marcadas como
    # principal y `por_cedula` devolvería un orden arbitrario.
    # `ux_google_vinculos_gid` es la otra restricción, y cierra una carrera que el
    # cerrojo por cédula NO cubre: dos personas HOMÓNIMAS con cédulas distintas se
    # bloquean sobre llaves distintas, así que ambas pueden identificar la misma
    # cuenta por el nombre y escribirle su cédula, pisándose. El backfill lo detecta
    # mirando el lote entero (`conflicto_duplicado`); una API que atiende de una en
    # una no puede. Con esta restricción, la segunda falla en vez de corromper el dato.
    ALTERS = (
        "CREATE INDEX IF NOT EXISTS ix_google_vinculos_ced ON google_vinculos (identificacion)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_google_vinculos_gid ON google_vinculos (google_id)",
        "CREATE INDEX IF NOT EXISTS ix_google_vinculos_mail ON google_vinculos (lower(email))",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_google_vinculos_principal "
        "ON google_vinculos (identificacion) WHERE principal",
    )

    # ---------------------------------------------------------------- lectura

    @staticmethod
    def _fila(f: Optional[dict]) -> Optional[dict]:
        if f is None:
            return None
        d = dict(f)
        for campo in ("creado_en", "actualizado_en"):
            v = d.get(campo)
            if v is not None and not isinstance(v, str):
                d[campo] = v.strftime("%Y-%m-%d %H:%M:%S")
        return d

    def por_cedula(self, identificacion: str) -> list:
        """Todas las cuentas registradas de una persona. La principal va primero."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM google_vinculos WHERE identificacion = %s "
                    "ORDER BY principal DESC, email",
                    (identificacion.strip(),))
                return [self._fila(f) for f in cur.fetchall()]

    def por_email(self, email: str) -> Optional[dict]:
        """Vínculo de una dirección concreta. None si esa cuenta no está registrada."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM google_vinculos WHERE lower(email) = %s",
                            (email.strip().lower(),))
                return self._fila(cur.fetchone())

    def por_google_id(self, google_id: str) -> Optional[dict]:
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM google_vinculos WHERE google_id = %s",
                            (google_id.strip(),))
                return self._fila(cur.fetchone())

    def contar(self) -> dict:
        """Cifras para el endpoint de estado: cuántos vínculos, personas y quién los puso."""
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT count(*) AS vinculos, "
                            "count(DISTINCT identificacion) AS personas FROM google_vinculos")
                base = dict(cur.fetchone())
                cur.execute("SELECT consumidor, origen, count(*) AS n FROM google_vinculos "
                            "GROUP BY consumidor, origen ORDER BY n DESC")
                base["por_consumidor"] = [dict(f) for f in cur.fetchall()]
        return base

    # ---------------------------------------------------------------- escritura

    def registrar(self, identificacion: str, google_id: str, email: str, ou: str,
                  consumidor: str, principal: bool = True,
                  origen: str = "creacion") -> dict:
        """
        Registra (o actualiza) el vínculo de una cuenta con una persona.

        Idempotente sobre (identificacion, google_id): si el sistema reintenta, se
        actualizan correo, unidad y fecha, pero se CONSERVAN `creado_en` y el
        `consumidor` original. El primero que la registró es el que la creó, y eso
        es lo que interesa auditar.
        """
        if origen not in ORIGENES:
            raise ValueError(f"Origen inválido '{origen}'. Debe ser uno de: "
                             f"{', '.join(sorted(ORIGENES))}.")
        try:
            return self._registrar(identificacion, google_id, email, ou, consumidor,
                                   principal, origen)
        except pg_errors.UniqueViolation as e:
            # Una cuenta pertenece a UNA persona. Si otra cédula ya la reclamó, esto
            # es un homónimo o un error de datos: nunca se sobrescribe en silencio.
            if "ux_google_vinculos_gid" not in str(e):
                raise
            duenio = self.por_google_id(google_id)
            raise ErrorDeConflicto(
                f"La cuenta '{google_id}' ya está vinculada a la cédula "
                f"'{(duenio or {}).get('identificacion')}'. No se puede asignar también "
                f"a '{identificacion.strip()}': revisa si son la misma persona.") from e

    def _registrar(self, identificacion: str, google_id: str, email: str, ou: str,
                   consumidor: str, principal: bool, origen: str) -> dict:
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                if principal:
                    # Solo puede haber una principal por persona. Se degrada la
                    # anterior ANTES de insertar, o el índice único la rechazaría.
                    cur.execute(
                        "UPDATE google_vinculos SET principal = FALSE, actualizado_en = now() "
                        "WHERE identificacion = %s AND google_id <> %s AND principal",
                        (identificacion.strip(), google_id.strip()))
                cur.execute(
                    """
                    INSERT INTO google_vinculos
                        (identificacion, google_id, email, ou, principal, consumidor, origen)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (identificacion, google_id) DO UPDATE SET
                        email = EXCLUDED.email,
                        ou = EXCLUDED.ou,
                        principal = EXCLUDED.principal,
                        actualizado_en = now()
                    RETURNING *
                    """,
                    (identificacion.strip(), google_id.strip(), email.strip().lower(),
                     ou, principal, consumidor, origen))
                return self._fila(cur.fetchone())

    def registrar_muchos(self, filas: list) -> int:
        """Alta masiva (siembra y sincronización). Cada fila es la tupla que espera
        `registrar`. Una sola sentencia: sembrar 23 000 vínculos de uno en uno serían
        23 000 viajes a la base."""
        if not filas:
            return 0
        with self._conectar() as con:
            with con.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO google_vinculos
                        (identificacion, google_id, email, ou, principal, consumidor, origen)
                    VALUES %s
                    ON CONFLICT (identificacion, google_id) DO UPDATE SET
                        email = EXCLUDED.email,
                        ou = EXCLUDED.ou,
                        principal = EXCLUDED.principal,
                        actualizado_en = now()
                    """,
                    filas, page_size=1000)
                return len(filas)

    def olvidar(self, identificacion: str, google_id: str) -> bool:
        """Borra un vínculo (p. ej. la cuenta se eliminó en Google). True si borró."""
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute("DELETE FROM google_vinculos WHERE identificacion = %s "
                            "AND google_id = %s", (identificacion.strip(), google_id.strip()))
                return cur.rowcount > 0

    # ---------------------------------------------------------------- cerrojo

    @contextlib.contextmanager
    def bloquear(self, identificacion: str):
        """
        Cerrojo exclusivo sobre una cédula, mientras dura la transacción.

        Es la pieza que Google no puede dar. Sin esto, dos sistemas que dan de alta a
        la misma persona a la vez comprueban «¿existe?» simultáneamente, los dos ven
        que no, y crean dos cuentas con direcciones distintas. Con esto, el segundo
        espera y encuentra la cuenta ya creada.

        `pg_advisory_xact_lock` no necesita que exista la fila (la persona todavía no
        está registrada) y se libera solo al terminar la transacción, incluso si el
        proceso muere.
        """
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (identificacion.strip(),))
            yield con


vinculos = ServicioVinculos()
