"""
Caché CENTRALIZADA en Redis, compartida por TODOS los workers de gunicorn.

Por qué centralizada y no en memoria: en producción corren varios workers
(procesos) de gunicorn. Una caché en memoria sería una copia POR worker, así que
al editar la configuración desde /admin solo el worker que atendió la escritura
invalidaría su copia; los otros seguirían sirviendo datos viejos hasta caducar.
Con Redis la caché es ÚNICA y compartida: invalidar en una escritura se ve al
instante en los 4 workers, por eso NO hace falta un TTL que "tape" la incoherencia.

Para qué sirve: configuración que se LEE en cada petición pero CAMBIA rara vez
(los procesadores por ruta y los prompts del clasificador). Evita golpear
PostgreSQL en cada inferencia.

Degradación: si Redis no responde (caído, reinicio), NO se tumba la inferencia.
`obtener` cae a leer del origen (PostgreSQL) directamente; el servicio sigue
arriba y solo pierde la caché mientras Redis vuelve.

Todas las claves se escriben bajo el prefijo PREFIJO para aislar el namespace del
servicio en un Redis compartido; `reiniciar()` solo borra claves bajo ese prefijo
sin tocar las de otros servicios.
"""
import json
import logging
from typing import Callable

import redis

from commons.redis_cache import obtener_cliente as _obtener_cliente

logger = logging.getLogger(__name__)

PREFIJO = "clasificador:cache:"


class Cache:
    """
    Caché clave-valor centralizada (Redis). Los valores se serializan a JSON, así
    que deben ser serializables (listas/dicts de tipos básicos: justo lo que
    devuelven los resolutores de configuración).
    """

    def obtener(self, clave: str, cargar: Callable):
        """
        Devuelve el valor cacheado para `clave`; si no está, lo calcula con
        `cargar()`, lo guarda y lo devuelve. Si Redis no responde, degrada a
        leer del origen (no rompe la inferencia).
        """
        clave_ns = PREFIJO + clave
        try:
            cliente = _obtener_cliente()
            crudo = cliente.get(clave_ns)
        except redis.RedisError:
            logger.warning("Redis no disponible al leer '%s'; se lee del origen.",
                           clave, exc_info=True)
            return cargar()

        if crudo is not None:
            try:
                return json.loads(crudo)
            except ValueError:
                logger.warning("Valor de caché corrupto en '%s'; se recarga.", clave)

        valor = cargar()
        try:
            cliente.set(clave_ns, json.dumps(valor))
        except (redis.RedisError, TypeError):
            logger.warning("Redis no disponible al guardar '%s'; se sigue sin cachear.",
                           clave, exc_info=True)
        return valor

    def invalidar(self, clave: str) -> None:
        """
        Borra la clave de la caché. Al ser Redis compartido, la invalidación se
        ve en todos los workers de inmediato. Tolera que Redis no esté disponible.
        """
        try:
            _obtener_cliente().delete(PREFIJO + clave)
        except redis.RedisError:
            logger.warning("Redis no disponible al invalidar '%s'.", clave, exc_info=True)

    def reiniciar(self) -> int:
        """
        Vacía TODA la caché del servicio (las claves bajo PREFIJO) y devuelve
        cuántas borró. Lo usa el botón 'Reiniciar' del panel: tras esto, los
        workers releen la configuración fresca de la base en la siguiente
        petición. Tolera que Redis no esté disponible (devuelve 0).
        """
        try:
            cliente = _obtener_cliente()
            claves = list(cliente.scan_iter(match=PREFIJO + "*"))
            if claves:
                cliente.delete(*claves)
            return len(claves)
        except redis.RedisError:
            logger.warning("Redis no disponible al reiniciar la caché.", exc_info=True)
            return 0


cache = Cache()
