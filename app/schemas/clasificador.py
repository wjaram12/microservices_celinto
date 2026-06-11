from pydantic import BaseModel


class RespuestaClasificacion(BaseModel):
    """Respuesta de la clasificación de un documento."""
    result: bool
    message: str
    document_class: str
    confidence: float
