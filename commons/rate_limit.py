"""
Límite de tasa por API key: ventana fija de 60 s con INCR+EXPIRE en Redis.

El contador vive en Redis (commons.redis_cache), no en el proceso: el límite es
GLOBAL entre los workers de gunicorn y entre los contextos de montaje (la app
unificada y las standalone cuentan contra la misma clave). Por eso no se usa
slowapi, que ata su limitador a cada instancia de app y cuenta por proceso.

La identidad es el `id` de la API key (tabla api_keys), nunca la IP: los sistemas
consumidores pueden salir por el mismo NAT. La dependencia compone
`verificar_api_key`, así que una clave ausente/inválida sigue dando 401 (y en
rutas admin el 403 va antes): nunca se cuenta una petición no autenticada.

Fail-open: si Redis no responde se loguea y se deja pasar — el mismo contrato que
la caché de consulta_titulos; el servicio nunca cae por Redis.
"""
import logging
import time

import redis
from fastapi import Depends, HTTPException, Response

from commons.config import settings
from commons.redis_cache import obtener_cliente
from commons.seguridad import verificar_api_key

logger = logging.getLogger(__name__)

VENTANA_SEGUNDOS = 60

# Categoría -> campo de ConfigComun con su límite por minuto. Para añadir una
# categoría nueva: una entrada aquí + su campo en ConfigComun.
_CAMPOS = {
    "google": "RATE_LIMIT_GOOGLE_POR_MINUTO",
    "lectura": "RATE_LIMIT_LECTURA_POR_MINUTO",
}


def limitar_tasa(categoria: str):
    """
    Factory: devuelve una dependencia FastAPI que autentica (vía
    `verificar_api_key`), cuenta la petición contra
    `ratelimit:{categoria}:{id_consumidor}:{ventana}` y responde 429 con
    `Retry-After` si el consumidor agotó su cupo del minuto.

    Devuelve el dict del consumidor, así puede sustituir a
    `Depends(verificar_api_key)` tanto en `dependencies=[...]` como en un
    parámetro; la caché de dependencias de FastAPI evita la doble verificación.
    """
    campo = _CAMPOS[categoria]  # KeyError al importar si la categoría no existe

    def dependencia(response: Response,
                    quien: dict = Depends(verificar_api_key)) -> dict:
        limite = getattr(settings, campo)
        if not settings.RATE_LIMIT_ACTIVO or limite <= 0:
            return quien

        ahora = int(time.time())
        clave = f"ratelimit:{categoria}:{quien['id']}:{ahora // VENTANA_SEGUNDOS}"
        try:
            # INCR ya es atómico; el pipeline solo ahorra un round-trip. El EXPIRE
            # repetido en cada petición es inocuo y evita claves huérfanas si la
            # primera petición de la ventana murió a medias.
            pipe = obtener_cliente().pipeline()
            pipe.incr(clave)
            pipe.expire(clave, VENTANA_SEGUNDOS * 2)
            usadas, _ = pipe.execute()
        except redis.RedisError:
            logger.warning(
                "Redis no disponible al limitar la tasa de '%s'; se deja pasar.",
                quien["consumidor"], exc_info=True)
            return quien

        response.headers["X-RateLimit-Limit"] = str(limite)
        response.headers["X-RateLimit-Remaining"] = str(max(limite - usadas, 0))

        # El exceso también cuenta (INCR antes de comparar): martillear en 429
        # no resetea el presupuesto del minuto.
        if usadas > limite:
            espera = VENTANA_SEGUNDOS - (ahora % VENTANA_SEGUNDOS)
            raise HTTPException(
                status_code=429,
                detail=(f"Se superó el límite de {limite} peticiones por minuto "
                        f"para esta API key. Reintenta en {espera} segundo(s)."),
                headers={"Retry-After": str(espera),
                         "X-RateLimit-Limit": str(limite),
                         "X-RateLimit-Remaining": "0"})
        return quien

    return dependencia
