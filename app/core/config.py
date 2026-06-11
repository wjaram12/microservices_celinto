# app/core/config.py
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 1. DEFINICIÓN DE VARIABLES Y VALIDACIÓN DE TIPOS
    # Si una variable obligatoria no está en el .env, la app falla al iniciar.
    #
    # Aquí van SOLO secretos/credenciales. El resto de la configuración (URLs,
    # versión de la API, ids de procesador, esquemas) vive en
    # app/core/procesadores.py como variables globales, no en el .env.

    # --- Extend ---
    # API key de Extend (dashboard -> Developers -> API Keys). Obligatoria.
    EXTEND_API_KEY: str

    # --- PostgreSQL: API keys y clasificaciones del clasificador ---
    # Formato: postgresql://usuario:contraseña@host:puerto/basededatos
    DATABASE_URL: str

    # 2. CONFIGURACIÓN DEL ARCHIVO .ENV
    # Buscamos el archivo .env en la raíz del proyecto (un nivel arriba de /app)
    model_config = SettingsConfigDict(
        env_file=os.path.join(Path(__file__).resolve().parent.parent.parent, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # Ignora otras variables del .env que no usemos aquí
    )


# Instanciamos la clase para que pueda ser importada en el resto del proyecto
settings = Settings()
