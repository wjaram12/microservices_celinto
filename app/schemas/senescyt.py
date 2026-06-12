from typing import Optional

from pydantic import BaseModel


class RespuestaRegistroSenescyt(BaseModel):
    """Respuesta de validar-registro-senescyt: si el documento es un registro de
    título de la SENESCYT, `datos` trae la información extraída. `match_document`
    indica si la identidad proporcionada coincide con la del documento (None si
    no se envió ningún dato para comparar)."""
    result: bool
    message: str
    match_document: Optional[bool] = None
    document_class: str
    confidence: float
    datos: dict = {}
