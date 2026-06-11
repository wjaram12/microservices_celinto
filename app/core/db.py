"""
Base común de los servicios que persisten en PostgreSQL.

Cada servicio (APIConsumidores, ServicioPrompts, ServicioProcesadores) declara
su DDL y hereda de ServicioBD, que le da la conexión con commit/rollback y la
creación idempotente de su tabla — a prueba de carreras entre workers.

Las conexiones salen de un POOL compartido por todo el proceso
(psycopg2.pool.ThreadedConnectionPool): bajo concurrencia (varios sistemas
consumidores) se reutilizan en vez de abrir/cerrar una por operación.
"""
import contextlib
import threading

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2 import pool as pg_pool

from app.core.config import settings

# Tamaño del pool POR WORKER. POOL_MAX * nº de workers de gunicorn debe quedar
# por debajo de `max_connections` de PostgreSQL (default 100).
POOL_MIN = 1
POOL_MAX = 10

_pool = None
_pool_lock = threading.Lock()


def _obtener_pool():
    """
    Pool de conexiones compartido por todos los servicios del proceso, creado
    perezosamente en el primer uso. Crearlo de forma perezosa lo hace seguro tras
    el fork de gunicorn (cada worker abre su propio pool), siempre que NO se use
    la opción --preload.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = pg_pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, settings.DATABASE_URL)
    return _pool


class ServicioBD:
    DDL: str = ""
    ALTERS: tuple = ()

    def __init__(self):
        self._tabla_lista = False

    def _asegurar_tabla(self, con) -> None:
        """
        Crea la tabla si falta, TOLERANDO la carrera entre procesos.

        `CREATE TABLE IF NOT EXISTS` no es del todo atómico: si varios workers
        arrancan a la vez, todos ven que la tabla no existe y la intentan crear;
        uno gana y los demás fallan con UniqueViolation sobre `pg_type`. En ese
        caso la tabla YA existe, así que el error se ignora.
        """
        try:
            with con.cursor() as cur:
                cur.execute(self.DDL)
                for alter in self.ALTERS:
                    cur.execute(alter)
            con.commit()
        except (pg_errors.UniqueViolation, pg_errors.DuplicateTable, pg_errors.DuplicateObject):
            con.rollback()

    @contextlib.contextmanager
    def _conectar(self):
        """
        Toma una conexión del pool, hace commit al salir bien (o rollback si hay
        error) y SIEMPRE la devuelve al pool. La primera vez por servicio en el
        proceso garantiza que su tabla exista.

        Si la conexión falla a nivel de red/servidor (OperationalError /
        InterfaceError o el rollback no funciona), se descarta en vez de
        devolverla al pool, para no reutilizar una conexión rota tras, p.ej.,
        un reinicio de PostgreSQL.
        """
        pool = _obtener_pool()
        con = pool.getconn()
        rota = False
        try:
            if not self._tabla_lista:
                self._asegurar_tabla(con)
                self._tabla_lista = True
            yield con
            con.commit()
        except Exception as e:
            rota = isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)) or bool(con.closed)
            try:
                con.rollback()
            except Exception:
                rota = True
            raise
        finally:
            pool.putconn(con, close=rota)

    def inicializar(self) -> None:
        """
        Garantiza que la tabla exista (idempotente). Se llama al arrancar el
        servidor; sirve para fallar pronto si la base no es accesible. Los
        servicios con siembra la sobreescriben.
        """
        with self._conectar():
            pass
