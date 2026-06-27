"""
Compatibilidad: la base de datos común se movió a commons.db (la comparten el
clasificador y la consulta de títulos). Este módulo re-exporta ServicioBD y el
pool para no romper los imports existentes (`from app.core.db import ServicioBD`).
"""
from commons.db import POOL_MAX, POOL_MIN, ServicioBD, _obtener_pool  # noqa: F401
