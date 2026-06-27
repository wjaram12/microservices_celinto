"""
Cliente Redis COMPARTIDO por las apps (creado perezosamente, seguro tras el fork
de gunicorn). Antes cada app creaba el suyo; ahora hay uno solo.

Solo expone la conexión: la POLÍTICA de caché (con TTL o por invalidación) la
define cada app sobre este cliente, porque son distintas.
"""
import threading

import redis

from commons.config import settings

_cliente = None
_cliente_lock = threading.Lock()


def obtener_cliente():
    """Cliente Redis del proceso. Timeouts cortos: si Redis no responde, degrada
    rápido en vez de colgar la petición."""
    global _cliente
    if _cliente is None:
        with _cliente_lock:
            if _cliente is None:
                _cliente = redis.Redis.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
    return _cliente
