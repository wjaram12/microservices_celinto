"""
Configuración COMÚN a todas las apps: lo que comparten para hablar con la misma
infraestructura (PostgreSQL y Redis). Cada app extiende `ConfigComun` con sus
propios campos (p. ej. EXTEND_API_KEY en el clasificador, SENESCYT_BASE_URL en la
consulta de títulos).

Todas leen el MISMO `.env` en la raíz del repo (services/.env), donde van solo
secretos/credenciales.
"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# services/.env (commons/config.py -> commons/ -> services/)
_ENV_FILE = os.path.join(Path(__file__).resolve().parent.parent, ".env")


class ConfigComun(BaseSettings):
    """Campos compartidos. Las apps heredan de esta clase y añaden los suyos."""

    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"

    # Límite de tasa por API key (commons.rate_limit). Peticiones por minuto y POR
    # consumidor, contadas en Redis (globales entre workers y contextos de montaje).
    # RATE_LIMIT_ACTIVO=false lo apaga; 0 en una categoría = esa sin límite.
    RATE_LIMIT_ACTIVO: bool = True
    RATE_LIMIT_GOOGLE_POR_MINUTO: int = 600    # endpoints que consumen cuota del Admin SDK
    RATE_LIMIT_LECTURA_POR_MINUTO: int = 3000  # lecturas del índice local (PostgreSQL)

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Instancia para los módulos de commons (db, redis_cache). Las apps crean la suya
# (subclase) para sus campos propios; ambas leen el mismo .env.
settings = ConfigComun()
