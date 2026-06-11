from typing import Optional
from pydantic import BaseModel


class RespuestaValidacion(BaseModel):
    """Respuesta de la validación de identidad; el campo ocr está deprecado y siempre es null."""
    result: bool
    message: str
    match_document: Optional[bool] = None
    document_class: str
    confidence: float
    ocr: Optional[str] = None
    datos: dict = {}
