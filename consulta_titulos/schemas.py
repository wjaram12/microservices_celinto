"""
Modelos de entrada/salida (Pydantic) del servicio de consulta de títulos.

Se mantiene la convención del repo: `result` (señal booleana principal), `message`
(texto para humanos, no parsear en código) y `status` (estado estructurado para
lógica de máquina).
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class SolicitudConsulta(BaseModel):
    """Cuerpo de POST /consulta-titulos/."""
    identificacion: str = Field(
        "", description="Cédula (10 dígitos) o pasaporte del titular a consultar.")
    apellidos: Optional[str] = Field(
        None, description="Apellidos del titular (opcional; útil si no se tiene cédula).")
    modo: Literal["auto", "local", "senescyt"] = Field(
        "auto",
        description=("auto = caché vigente o, si no, consulta en vivo; "
                     "local = solo lo cacheado; senescyt = siempre en vivo."))
    force_refresh: bool = Field(
        False, description="Ignora la caché vigente y vuelve a consultar la fuente.")


class RespuestaConsultaTitulos(BaseModel):
    """Respuesta de la consulta de títulos."""
    result: bool = Field(
        description="True si se hallaron títulos para la identidad consultada.")
    message: str = Field(
        description="Mensaje legible para humanos; no parsear en código (usa `status`).")
    status: Literal["encontrado", "no_encontrado", "no_en_cache"] = Field(
        description=("'encontrado' (hay títulos), 'no_encontrado' (la fuente respondió "
                     "pero sin títulos), 'no_en_cache' (modo local y no estaba cacheado)."))
    fuente: Literal["cache", "senescyt"] = Field(
        description="De dónde salió la respuesta: 'cache' (Redis) o 'senescyt' (en vivo).")
    persona: dict = Field(
        default={}, description="Datos del titulado tal como los expone SENESCYT.")
    titulos: List[dict] = Field(
        default=[], description="Títulos registrados, cada uno con su categoría y campos.")
    total_titulos: int = 0
    pdf_disponible: bool = Field(
        default=False, description="True si SENESCYT ofrece el PDF oficial del detalle.")
    vigente: bool = Field(
        default=False, description="True si la respuesta vino de caché vigente.")
    ttl_segundos: Optional[int] = Field(
        default=None, description="Vigencia restante de la caché en segundos (si vino de caché).")
    intentos_captcha: Optional[int] = Field(
        default=None, description="Intentos de OCR que costó resolver el captcha (consulta en vivo).")


class RespuestaPDF(BaseModel):
    """Respuesta de GET /consulta-titulos/{cedula}/pdf: el PDF oficial en base64.

    SENESCYT no publica una URL del PDF (se genera en un postback JSF con sesión),
    así que se entrega el binario codificado en base64.
    """
    result: bool = Field(description="True si se obtuvo el PDF.")
    message: str
    cedula: str
    content_type: str = "application/pdf"
    bytes: int = Field(description="Tamaño del PDF en bytes (antes de codificar).")
    pdf_base64: str = Field(description="Contenido del PDF codificado en base64.")
    fuente: Literal["cache", "senescyt"] = Field(
        description="'cache' (Redis) o 'senescyt' (descargado en vivo del portal).")
    vigente: bool = False
    ttl_segundos: Optional[int] = None
