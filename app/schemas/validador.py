from typing import Literal, Optional
from pydantic import BaseModel, Field


class RespuestaValidacion(BaseModel):
    """Respuesta de la validación de identidad; el campo `ocr` está deprecado y
    siempre es null.

    IMPORTANTE para consumidores: `result` indica SOLO que el documento fue
    reconocido como cédula o pasaporte (clasificación). NO implica que la
    identidad coincida. Combina con `status` (extracción) y `match_document`
    (coincidencia) para decidir.
    """
    result: bool = Field(
        description=("True si el documento es cédula o pasaporte (clasificación con "
                     "confianza suficiente). NO implica que la identidad coincida; usa "
                     "`status` y `match_document` para eso."))
    message: str = Field(
        description="Mensaje legible para humanos; no parsear en código (usa `status`).")
    status: Literal["no_reconocido", "extraccion_fallida", "extraido"] = Field(
        description=("Estado estructurado: 'no_reconocido' (no es identidad), "
                     "'extraccion_fallida' (es identidad pero sin datos), 'extraido'."))
    match_document: Optional[bool] = Field(
        default=None,
        description=("True si la identificación coincide, False si no, None si no se "
                     "envió cédula, el documento no trae el número o no es identidad."))
    document_class: str
    confidence: float
    ocr: Optional[str] = None
    datos: dict = {}
    confianzas: dict = Field(
        default={},
        description=("Índice de confianza 0..1 por cada campo extraído, con la misma "
                     "clave que en `datos`; los campos anidados usan notación con punto "
                     "(p.ej. 'monto.amount'). El valor es null si Extend no reporta "
                     "confianza para ese campo. Vacío si no hubo extracción."))
