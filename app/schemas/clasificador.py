from pydantic import BaseModel


class RespuestaClasificacion(BaseModel):
    # Resultado principal: ¿el documento es válido/aceptado?
    result: bool
    # Mensaje legible para mostrar al usuario.
    message: str
    # Clase detectada por el modelo (CEDULA, OTROS, ...).
    document_class: str
    # Confianza del modelo (0.0 a 1.0).
    confidence: float
