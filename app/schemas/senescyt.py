from pydantic import BaseModel


class RespuestaRegistroSenescyt(BaseModel):
    """Respuesta de validar-registro-senescyt: si el documento es un registro de
    título de la SENESCYT, `datos` trae la información extraída."""
    result: bool
    message: str
    document_class: str
    confidence: float
    datos: dict = {}
