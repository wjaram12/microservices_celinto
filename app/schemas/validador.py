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
    # DEPRECADO: validar-identidad ya no usa OCR (el número se compara contra la
    # extracción estructurada). Siempre null; se conserva para no romper a los
    # consumidores. Para texto OCR está el endpoint /api/v1/ocr/.
    ocr: Optional[str] = None
    # Datos extraídos del documento según su tipo (cédula o pasaporte), como
    # diccionario. Vacío {} si el documento no es un tipo de identidad reconocido.
    datos: dict = {}
