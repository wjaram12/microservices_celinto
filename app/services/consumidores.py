"""
Compatibilidad: el servicio de API keys se movió a commons.consumidores (lo
comparten el clasificador y la consulta de títulos: mismo sistema de api_keys).
Se re-exporta para no romper los imports existentes
(`from app.services.consumidores import consumidores`).
"""
from commons.consumidores import (  # noqa: F401
    PREFIJO_LLAVE,
    SCOPES_VALIDOS,
    APIConsumidores,
    consumidores,
)
