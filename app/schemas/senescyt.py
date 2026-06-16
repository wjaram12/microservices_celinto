from typing import Literal, Optional

from pydantic import BaseModel, Field


class RespuestaRegistroSenescyt(BaseModel):
    """Respuesta de validar-registro-senescyt.

    IMPORTANTE para consumidores: `result` indica SOLO que el documento fue
    reconocido como registro SENESCYT (clasificación). NO implica que la
    extracción funcionara ni que la identidad coincida. Para decidir si el
    registro es aprovechable, combina tres señales:
      - `result`         -> es un registro SENESCYT.
      - `status`         -> 'extraido' si además se leyó la información.
      - `match_document` -> True si la identidad enviada coincide.
    """
    result: bool = Field(
        description=("True si el documento es un registro SENESCYT (clasificación con "
                     "confianza suficiente). NO implica extracción ni coincidencia de "
                     "identidad; usa `status` y `match_document` para eso."))
    message: str = Field(
        description="Mensaje legible para humanos; no parsear en código (usa `status`).")
    status: Literal["no_reconocido", "extraccion_fallida", "extraido"] = Field(
        description=("Estado estructurado: 'no_reconocido' (no es SENESCYT), "
                     "'extraccion_fallida' (es SENESCYT pero sin datos), 'extraido' "
                     "(es SENESCYT y se extrajo la información)."))
    match_document: Optional[bool] = Field(
        default=None,
        description=("True si la identidad proporcionada coincide, False si no coincide, "
                     "None si no se envió nada para comparar o no se pudo leer del documento."))
    document_class: str
    confidence: float
    datos: dict = {}
    confianzas: dict = Field(
        default={},
        description=("Índice de confianza 0..1 por cada campo extraído, con la misma "
                     "clave que en `datos`; los campos anidados usan notación con punto "
                     "(p.ej. 'monto.amount'). El valor es null si Extend no reporta "
                     "confianza para ese campo. Vacío si no hubo extracción."))
