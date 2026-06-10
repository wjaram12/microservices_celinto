from pydantic import BaseModel


class RespuestaOCR(BaseModel):
    # Resultado principal: ¿se extrajo texto del documento?
    result: bool
    # Mensaje legible: si se buscó un término, indica si aparece y cuántas veces.
    message: str
    # Contenido del OCR: el texto completo extraído del documento.
    content: str
