from typing import Optional
from pydantic import BaseModel, Field


class APIKeyCrear(BaseModel):
    """Datos de entrada para crear una API key; scope 'consumo' o 'admin'."""
    consumidor: str = Field(..., min_length=1, max_length=60)
    scope: str = "consumo"


class APIKeyActualizar(BaseModel):
    """Actualización parcial de una API key; no permite cambiar la clave (rotar = crear y revocar)."""
    consumidor: Optional[str] = Field(None, min_length=1, max_length=60)
    scope: Optional[str] = None
    activo: Optional[bool] = None


class APIKeyRespuesta(BaseModel):
    """Metadatos de una API key; nunca incluye la clave en texto plano ni su hash."""
    id: int
    consumidor: str
    scope: str
    activo: bool
    creado_en: str
    ultimo_uso: Optional[str] = None


class APIKeyCreada(APIKeyRespuesta):
    """Respuesta al crear una API key; incluye la clave en texto plano una sola vez (nunca más)."""
    llave: str
