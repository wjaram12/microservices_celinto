from typing import List, Optional

from pydantic import BaseModel, Field


class ProcesadorCrear(BaseModel):
    """Datos de entrada para crear una fila de procesador que configura una ruta/operación de Extend."""
    ruta: str = Field(..., description="'validar-identidad' | 'validar-registro-senescyt' | 'ocr'")
    operacion: str = Field(..., description="'clasificar' | 'extraer' | 'parse'")
    clase: str = Field("", max_length=50)
    modo: str = Field(..., description="'id' | 'inline'")
    procesador_id: Optional[str] = Field(None, max_length=200)
    version: Optional[str] = Field(None, max_length=50)
    esquema: Optional[dict] = None
    umbral: Optional[float] = Field(None, ge=0, le=1)
    activo: bool = True


class ProcesadorActualizar(BaseModel):
    """Actualización parcial de un procesador; procesador_id, esquema y umbral admiten null explícito."""
    ruta: Optional[str] = None
    operacion: Optional[str] = None
    clase: Optional[str] = Field(None, max_length=50)
    modo: Optional[str] = None
    procesador_id: Optional[str] = Field(None, max_length=200)
    version: Optional[str] = Field(None, max_length=50)
    esquema: Optional[dict] = None
    umbral: Optional[float] = Field(None, ge=0, le=1)
    activo: Optional[bool] = None


class ProcesadorRespuesta(BaseModel):
    """Metadatos de un procesador configurado."""
    id: int
    ruta: str
    operacion: str
    clase: str
    modo: str
    procesador_id: Optional[str] = None
    version: Optional[str] = None
    esquema: Optional[dict] = None
    umbral: Optional[float] = None
    activo: bool
    creado_en: str
    actualizado_en: str


class PublicarConfigExtend(BaseModel):
    """Config editada en el modal que se empuja al procesador de Extend y se
    publica. `esquema` es el JSON Schema (extraer) o {'classifications': [...]}
    (clasificar). No se persiste en local: Extend es la fuente de verdad."""
    esquema: dict


class VersionExtend(BaseModel):
    """Una versión publicada de un procesador en Extend Studio."""
    id: Optional[str] = None
    version: Optional[str] = None


class ProcesadorExtend(BaseModel):
    """Un procesador publicado en Extend, para elegirlo en /admin."""
    id: str
    nombre: Optional[str] = None
    tipo: Optional[str] = None
    versiones: List[VersionExtend] = []
