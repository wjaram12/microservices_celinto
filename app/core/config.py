"""
Configuración del clasificador. Los campos comunes (DATABASE_URL, REDIS_URL)
viven en commons.config.ConfigComun; aquí solo se añade lo propio de esta app.

En el .env (services/.env) van SOLO secretos/credenciales (EXTEND_API_KEY,
DATABASE_URL); el resto de la configuración vive en código.
"""
from commons.config import ConfigComun


class Settings(ConfigComun):
    """Configuración del clasificador = comunes + EXTEND_API_KEY."""

    EXTEND_API_KEY: str


settings = Settings()
