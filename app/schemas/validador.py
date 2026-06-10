from typing import Optional
from pydantic import BaseModel


class RespuestaValidacion(BaseModel):
    # Resultado principal: ¿el documento es una cédula válida?
    result: bool
    # Mensaje legible para mostrar al usuario.
    message: str
    # Si se envió la cédula del sistema: ¿coincide con la del documento?
    # null si no se envió ningún número que comparar.
    match_document: Optional[bool] = None
    # Clase detectada por el modelo (CEDULA, OTROS, ...).
    document_class: str
    # Confianza del modelo (0.0 a 1.0).
    confidence: float
    # Texto extraído por OCR del documento. Solo se llena cuando se envió la
    # cédula del sistema (modo OCR); null en el modo simple.
    ocr: Optional[str] = None
