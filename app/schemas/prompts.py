from typing import Optional
from pydantic import BaseModel, Field


class PromptCrear(BaseModel):
    # Identificador corto para buscar el prompt ("cedula", "pasaporte", ...).
    clave: str = Field(..., min_length=1, max_length=50)
    # Etiqueta que devuelve el clasificador ("CEDULA", "PASAPORTE", ...).
    # "other" (minúsculas) es la clase de descarte que exige Extend.
    tipo: str = Field(..., min_length=1, max_length=50)
    # El prompt: descripción que guía al modelo de Extend.
    descripcion: str = Field(..., min_length=10)
    activo: bool = True


class PromptActualizar(BaseModel):
    # Todos opcionales: solo se actualizan los campos enviados.
    tipo: Optional[str] = Field(None, min_length=1, max_length=50)
    descripcion: Optional[str] = Field(None, min_length=10)
    activo: Optional[bool] = None


class PromptRespuesta(BaseModel):
    id: int
    clave: str
    tipo: str
    descripcion: str
    activo: bool
    creado_en: str
    actualizado_en: str
