from pydantic import BaseModel


class RespuestaOCR(BaseModel):
    """Respuesta del OCR con el texto extraído del documento."""
    result: bool
    message: str
    content: str
