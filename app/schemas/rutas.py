from typing import Optional

from pydantic import BaseModel, Field


class RutaCrear(BaseModel):
    """Datos de entrada para crear una ruta lógica que referencian los procesadores."""
    clave: str = Field(..., min_length=1, max_length=50)
    url: str = Field(..., min_length=1, max_length=200)
    descripcion: str = ""
    activo: bool = True


class RutaActualizar(BaseModel):
    """Actualización parcial de una ruta; la clave es inmutable."""
    url: Optional[str] = Field(None, min_length=1, max_length=200)
    descripcion: Optional[str] = None
    activo: Optional[bool] = None


class RutaRespuesta(BaseModel):
    """Metadatos de una ruta lógica."""
    id: int
    clave: str
    url: str
    descripcion: str
    activo: bool
    creado_en: str
    actualizado_en: str
