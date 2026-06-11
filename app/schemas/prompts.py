from typing import Optional
from pydantic import BaseModel, Field


class PromptCrear(BaseModel):
    """Datos de entrada para crear un prompt de clasificación; el tipo 'other' es la clase de descarte que exige Extend."""
    clave: str = Field(..., min_length=1, max_length=50)
    tipo: str = Field(..., min_length=1, max_length=50)
    descripcion: str = Field(..., min_length=10)
    activo: bool = True


class PromptActualizar(BaseModel):
    """Actualización parcial de un prompt; solo se actualizan los campos enviados."""
    tipo: Optional[str] = Field(None, min_length=1, max_length=50)
    descripcion: Optional[str] = Field(None, min_length=10)
    activo: Optional[bool] = None


class PromptRespuesta(BaseModel):
    """Metadatos de un prompt de clasificación."""
    id: int
    clave: str
    tipo: str
    descripcion: str
    activo: bool
    creado_en: str
    actualizado_en: str
