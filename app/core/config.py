# app/core/config.py
import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # 1. DEFINICIÓN DE VARIABLES Y VALIDACIÓN DE TIPOS
    # Si alguna variable no se encuentra en el .env, FastAPI lanzará un error al iniciar.
    GOOGLE_PROJECT_ID: str
    GOOGLE_LOCATION: str = "us"  # Valor por defecto si no se especifica en el .env
    GOOGLE_PROCESSOR_ID: str     # procesador clasificador (obligatorio)

    # Procesador de OCR dedicado (opcional). Si se configura, el endpoint /ocr/
    # lo usa para extraer texto; si se omite, /ocr/ cae al clasificador, que
    # también devuelve texto aunque normalmente menos completo.
    GOOGLE_OCR_PROCESSOR_ID: Optional[str] = None

    # PostgreSQL: cadena de conexión donde se almacenan las API keys.
    # Formato: postgresql://usuario:contraseña@host:puerto/basededatos
    DATABASE_URL: str
    
    # 2. CONFIGURACIÓN DEL ARCHIVO .ENV
    # Buscamos el archivo .env en la raíz del proyecto (un nivel arriba de /app)
    model_config = SettingsConfigDict(
        env_file=os.path.join(Path(__file__).resolve().parent.parent.parent, ".env"),
        env_file_encoding="utf-8",
        extra="ignore"  # Ignora otras variables del .env que no usemos aquí
    )

# Instanciamos la clase para que pueda ser importada en el resto del proyecto
settings = Settings()

# 3. ASIGNACIÓN DE LA LLAVE DE GOOGLE CLOUD
# Buscamos el archivo credentials.json en la raíz del proyecto
BASE_DIR = Path(__file__).resolve().parent.parent.parent
RUTA_CREDENCIALES = os.path.join(BASE_DIR, "credentials.json")

# Le indicamos a las librerías oficiales de Google dónde está tu llave privada
if os.path.exists(RUTA_CREDENCIALES):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = RUTA_CREDENCIALES
else:
    print(f"⚠️ ¡Advertencia! No se encontró el archivo 'credentials.json' en la raíz: {RUTA_CREDENCIALES}")