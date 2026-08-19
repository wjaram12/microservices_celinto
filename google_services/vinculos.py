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

    @staticmethod
    def _filtros(origen: Optional[str], consumidor: Optional[str], desde, hasta,
                 texto: Optional[str]) -> tuple:
        """
        Traduce los filtros del listado a (fragmento WHERE, valores).

        Los comparte el listado paginado y la exportación, para que el CSV no pueda
        acabar conteniendo un conjunto distinto del que se ve en pantalla.

        `hasta` es INCLUSIVO: quien pide `hasta=2026-07-31` espera ver lo del 31, y
        `creado_en <= '2026-07-31'` dejaría fuera todo lo posterior a medianoche.

        Los fragmentos llevan marcador; los valores viajan SIEMPRE como parámetros,
        nunca concatenados.
        """
        condiciones, valores = [], []
        if origen:
            condiciones.append("origen = %s")
            valores.append(origen.strip())
        if consumidor:
            condiciones.append("consumidor = %s")
            valores.append(consumidor.strip())
        if desde is not None:
            condiciones.append("creado_en >= %s")
            valores.append(desde)
        if hasta is not None:
            condiciones.append("creado_en < %s + INTERVAL '1 day'")
            valores.append(hasta)
        if texto:
            # Una sola caja de búsqueda para las dos llaves con las que se pregunta
            # por una persona: su cédula o su correo.
            condiciones.append("(identificacion LIKE %s OR lower(email) LIKE %s)")
            patron = f"%{texto.strip().lower()}%"
            valores.extend([patron, patron])
        return ("WHERE " + " AND ".join(condiciones)) if condiciones else "", valores

    def listar(self, origen: Optional[str] = None, consumidor: Optional[str] = None,
               desde=None, hasta=None, texto: Optional[str] = None,
               limite: int = 100, desplazamiento: int = 0) -> dict:
        """
        Vínculos que cumplen los filtros, más el total SIN paginar.

        Devuelve las dos cifras porque un cliente que pagina necesita saber cuántas
        quedan; con solo la página no puede.
        """
        donde, valores = self._filtros(origen, consumidor, desde, hasta, texto)
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT count(*) AS n FROM google_vinculos {donde}", valores)
                total = cur.fetchone()["n"]
                cur.execute(
                    f"SELECT * FROM google_vinculos {donde} "
                    "ORDER BY creado_en DESC, id DESC LIMIT %s OFFSET %s",
                    valores + [limite, desplazamiento])
                filas = [self._fila(f) for f in cur.fetchall()]
        return {"total": total, "filas": filas}

    def exportar(self, origen: Optional[str] = None, consumidor: Optional[str] = None,
                 desde=None, hasta=None, texto: Optional[str] = None, lote: int = 1000):
        """
        Itera TODOS los vínculos que cumplen el filtro, sin paginar.

        Generador, y con cursor del lado del SERVIDOR (el que tiene `name`): así
        PostgreSQL manda los resultados por lotes en vez de materializar las 23 620
        filas en memoria del worker para luego serializarlas otra vez. Un reporte no
        debe poder tumbar el proceso por su tamaño.

        La conexión vive dentro del generador a propósito: quien lo consume (la
        respuesta en streaming) lo hace DESPUÉS de que el endpoint haya retornado, y
        si el `with` estuviera fuera la conexión ya estaría devuelta al pool.
        """
        donde, valores = self._filtros(origen, consumidor, desde, hasta, texto)
        with self._conectar() as con:
            with con.cursor(name="exportar_vinculos", cursor_factory=RealDictCursor) as cur:
                cur.itersize = lote
                cur.execute(
                    f"SELECT * FROM google_vinculos {donde} "
                    "ORDER BY creado_en DESC, id DESC", valores)
                for f in cur:
                    yield self._fila(f)

    def resumen(self, dias: int = 30, dominio: str = "") -> dict:
        """
        Cifras de la migración: por dónde entró cada cuenta, ritmo de altas y filas
        sospechosas.

        Las anomalías son de FORMA, no de existencia: esta tabla es un índice de
        Google y no sabe si la cuenta sigue viva. Para eso está `/confirmar`, que
        lee el directorio. Aquí solo se detecta lo que se puede ver desde la base:
        un correo vacío, de otro dominio, una cédula que no es una cédula.
        """
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT count(*) AS vinculos, "
                            "count(DISTINCT identificacion) AS personas FROM google_vinculos")
                base = dict(cur.fetchone())

                cur.execute(
                    """
                    SELECT origen, consumidor,
                           count(*)                       AS cuentas,
                           count(DISTINCT identificacion) AS personas,
                           min(creado_en)::date::text     AS primera,
                           max(creado_en)::date::text     AS ultima
                    FROM   google_vinculos
                    GROUP  BY origen, consumidor
                    ORDER  BY cuentas DESC
                    """)
                base["por_origen"] = [dict(f) for f in cur.fetchall()]

                cur.execute(
                    """
                    SELECT creado_en::date::text AS dia, origen, consumidor,
                           count(*) AS altas
                    FROM   google_vinculos
                    WHERE  creado_en >= now() - make_interval(days => %s)
                    GROUP  BY dia, origen, consumidor
                    ORDER  BY dia DESC, altas DESC
                    """, (dias,))
                base["por_dia"] = [dict(f) for f in cur.fetchall()]

                # Cada rama cuenta un defecto y enseña hasta cinco ejemplos. Se
                # consultan siempre todas y se descartan las de cero al final: así
                # el que llama ve una lista vacía cuando todo está bien, en vez de
                # tener que interpretar ceros.
                #
                # Las dos reglas afinadas contra los datos reales del dominio:
                #
                #   - El correo puede estar en un SUBDOMINIO. Hay 138 cuentas en
                #     @posgrados.casagrande.edu.ec y son legítimas; exigir el dominio
                #     exacto las marcaba todas como ajenas.
                #   - Un documento con letras NO es un error: es un pasaporte o una
                #     cédula extranjera (30 filas: 'AO677274', 'VS-BF232388'). Lo que
                #     de verdad no debe estar es un valor de RELLENO, que es lo que
                #     define identidad.cedula_invalida(): vacío, o un solo dígito
                #     repetido ('0000000000' en filas que ni son personas). El regex
                #     con retroceso `^(\\d)\\1*$` es ese `len(set(c)) == 1`.
                cur.execute(
                    r"""
                    SELECT 'correo_vacio' AS problema, count(*) AS filas,
                           (array_agg(identificacion))[1:5] AS ejemplos
                    FROM   google_vinculos WHERE btrim(coalesce(email, '')) = ''
                    UNION ALL
                    SELECT 'dominio_ajeno', count(*), (array_agg(email))[1:5]
                    FROM   google_vinculos
                    WHERE  lower(email) NOT LIKE %s AND lower(email) NOT LIKE %s
                    UNION ALL
                    SELECT 'sin_google_id', count(*), (array_agg(identificacion))[1:5]
                    FROM   google_vinculos WHERE btrim(coalesce(google_id, '')) = ''
                    UNION ALL
                    SELECT 'cedula_de_relleno', count(*), (array_agg(identificacion))[1:5]
                    FROM   google_vinculos
                    WHERE  btrim(coalesce(identificacion, '')) = ''
                       OR  identificacion ~ '^(\d)\1*$'
                    UNION ALL
                    SELECT 'correo_repetido', count(*), (array_agg(email))[1:5]
                    FROM   (SELECT lower(email) AS email FROM google_vinculos
                            GROUP BY 1 HAVING count(*) > 1) d
                    """,
                    (f"%@{(dominio or '').lower()}", f"%.{(dominio or '').lower()}"))
                base["anomalias"] = [dict(f) for f in cur.fetchall() if f["filas"]]
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
