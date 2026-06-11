from typing import Optional

from pydantic import BaseModel, Field


class RutaCrear(BaseModel):
    # Identificador lógico que referencian los procesadores ('clasificar', ...).
    clave: str = Field(..., min_length=1, max_length=50)
    # El endpoint real del servicio ('/api/v1/clasificar/', ...). Informativo.
    url: str = Field(..., min_length=1, max_length=200)
    descripcion: str = ""
    activo: bool = True


class RutaActualizar(BaseModel):
    # Todos opcionales: solo se actualizan los campos enviados. La clave es
    # inmutable (es lo que referencian las filas de procesadores).
    url: Optional[str] = Field(None, min_length=1, max_length=200)
    descripcion: Optional[str] = None
    activo: Optional[bool] = None


class RutaRespuesta(BaseModel):
    id: int
    clave: str
    url: str
    descripcion: str
    activo: bool
    creado_en: str
    actualizado_en: str
