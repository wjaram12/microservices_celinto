from typing import Literal

from pydantic import BaseModel, Field


class RespuestaValidacionPago(BaseModel):
    """Respuesta de validar-pago.

    IMPORTANTE para consumidores: `result` indica SOLO que el documento fue
    reconocido como comprobante de pago (depósito o transferencia). Para saber
    si además se leyó la información, usa `status`. Esta ruta no contrasta los
    datos contra ningún valor del sistema: solo clasifica y extrae.
    """
    result: bool = Field(
        description=("True si el documento es un comprobante de pago (DEPOSITO o "
                     "TRANSFERENCIA) clasificado con confianza suficiente. NO implica "
                     "que la extracción trajera datos; usa `status` para eso."))
    message: str = Field(
        description="Mensaje legible para humanos; no parsear en código (usa `status`).")
    status: Literal["no_reconocido", "extraccion_fallida", "extraido"] = Field(
        description=("Estado estructurado: 'no_reconocido' (no es un comprobante de pago), "
                     "'extraccion_fallida' (es un comprobante pero sin datos), 'extraido' "
                     "(es un comprobante y se extrajo la información)."))
    document_class: str = Field(
        description="Clase detectada: 'DEPOSITO', 'TRANSFERENCIA' u 'other'.")
    confidence: float
    datos: dict = {}
    confianzas: dict = Field(
        default={},
        description=("Índice de confianza 0..1 por cada campo extraído, con la misma "
                     "clave que en `datos`; los campos anidados usan notación con punto "
                     "(p.ej. 'monto.amount'). El valor es null si Extend no reporta "
                     "confianza para ese campo. Vacío si no hubo extracción."))
