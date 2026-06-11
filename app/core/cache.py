"""
Cache en memoria con expiración (TTL) por proceso.

Para la configuración que se LEE en cada petición pero CAMBIA rara vez (los
procesadores por ruta y los prompts del clasificador). Evita golpear PostgreSQL
en cada inferencia y, como la lectura pasa a ser en memoria, deja de bloquear el
event loop async con esas consultas. Los cambios desde /admin se reflejan al
invalidar el cache en cada escritura (mismo worker) o al caducar el TTL (resto
de workers).
"""
import threading
import time
from typing import Callable


class CacheTTL:
    """
    Guarda un único valor con caducidad. `obtener(cargar)` devuelve el valor
    cacheado si sigue vigente, o lo recarga llamando a `cargar()`. Hilo-seguro
    (los servicios se usan desde el event loop y desde el threadpool de FastAPI).
    """

    def __init__(self, ttl_segundos: float = 30.0):
        self._ttl = ttl_segundos
        self._lock = threading.Lock()
        self._valor = None
        self._caduca = 0.0
        self._valido = False

    def obtener(self, cargar: Callable):
        with self._lock:
            if self._valido and time.monotonic() < self._caduca:
                return self._valor
        valor = cargar()
        with self._lock:
            self._valor = valor
            self._caduca = time.monotonic() + self._ttl
            self._valido = True
        return valor

    def invalidar(self) -> None:
        with self._lock:
            self._valido = False
