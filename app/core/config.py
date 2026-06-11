import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Configuración de la aplicación cargada desde el .env.

    En el .env van SOLO secretos/credenciales (EXTEND_API_KEY, DATABASE_URL); el
    resto de la configuración (URLs, versión de la API, ids de procesador,
    esquemas) vive en app/core/procesadores.py como variables globales y NO va
    al .env. Si una variable obligatoria falta en el .env, la app falla al
    iniciar.
    """

    EXTEND_API_KEY: str

    DATABASE_URL: str

    model_config = SettingsConfigDict(
        env_file=os.path.join(Path(__file__).resolve().parent.parent.parent, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
