"""
View adm_cache: reinicio de la caché centralizada del servicio.

    API (scope admin): POST /api/v1/cache/reiniciar

Vacía la caché compartida en Redis. Útil cuando se quiere forzar a los workers a
releer la configuración fresca de la base sin esperar a una escritura ni reiniciar
los procesos. No tiene página propia: lo dispara el botón del sidebar del panel.
"""
import logging

from fastapi import APIRouter, Depends

from app.core.cache import cache
from app.core.seguridad import requiere_admin

logger = logging.getLogger(__name__)

api = APIRouter()


@api.post("/cache/reiniciar", tags=["Caché (admin)"])
def reiniciar_cache(_admin: dict = Depends(requiere_admin)):
    """
    Vacía la caché centralizada (Redis). Tras esto, todos los workers releen la
    configuración (procesadores y prompts) de la base en la siguiente petición.
    """
    borradas = cache.reiniciar()
    return {"reiniciada": True, "claves_borradas": borradas}
