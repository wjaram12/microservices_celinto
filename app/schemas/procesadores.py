from typing import List, Optional

from pydantic import BaseModel, Field


class ProcesadorCrear(BaseModel):
    # Ruta de la API que usa esta fila ('clasificar'|'validar-identidad'|'ocr').
    ruta: str = Field(..., description="'clasificar' | 'validar-identidad' | 'ocr'")
    # Operación de Extend que configura esta fila.
    operacion: str = Field(..., description="'clasificar' | 'extraer' | 'parse'")
    # Clase de documento ('CEDULA', 'PASAPORTE', ...) para los esquemas de
    # extracción; vacío cuando la operación no usa clase (clasificar / parse).
    clase: str = Field("", max_length=50)
    # 'id' = procesador publicado en Extend; 'inline' = config en esta fila / BD.
    modo: str = Field(..., description="'id' | 'inline'")
    # Id del procesador publicado (cl_.../ex_...), obligatorio si modo='id'.
    procesador_id: Optional[str] = Field(None, max_length=200)
    # Versión del procesador publicado a fijar (modo='id'); vacío = última publicada.
    version: Optional[str] = Field(None, max_length=50)
    # JSON Schema de extracción (modo='inline' en 'extraer') u opciones del parse.
    esquema: Optional[dict] = None
    # Umbral de confianza 0..1 (solo 'clasificar'): mínimo para dar por válida
    # una clasificación. Si se omite, se usa el valor por defecto (0.85).
    umbral: Optional[float] = Field(None, ge=0, le=1)
    activo: bool = True


class ProcesadorActualizar(BaseModel):
    # Todos opcionales: solo se actualizan los campos enviados. `procesador_id`,
    # `esquema` y `umbral` admiten null explícito (se distingue con
    # model_fields_set en la API).
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


# --- Sincronización con Extend Studio (GET /processors) ---

class VersionExtend(BaseModel):
    id: Optional[str] = None
    version: Optional[str] = None


class ProcesadorExtend(BaseModel):
    """Un procesador publicado en Extend, para elegirlo en /admin."""
    id: str
    nombre: Optional[str] = None
    tipo: Optional[str] = None
    versiones: List[VersionExtend] = []
