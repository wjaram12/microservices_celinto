from typing import Optional
from pydantic import BaseModel, Field


class APIKeyCrear(BaseModel):
    # Nombre del sistema consumidor (ej. "celinto-posgrados").
    consumidor: str = Field(..., min_length=1, max_length=60)
    # Permisos: 'consumo' (clasificar/OCR) o 'admin' (además gestionar prompts y claves).
    scope: str = "consumo"


class APIKeyActualizar(BaseModel):
    # Todos opcionales: se actualiza solo lo que se envíe. Debe llegar al menos
    # uno (lo valida el endpoint). No se puede cambiar la clave en sí (rotar =
    # crear otra y revocar esta).
    consumidor: Optional[str] = Field(None, min_length=1, max_length=60)
    scope: Optional[str] = None
    activo: Optional[bool] = None


class APIKeyRespuesta(BaseModel):
    # Metadatos de la clave. NUNCA incluye la clave en texto plano ni su hash.
    id: int
    consumidor: str
    scope: str
    activo: bool
    creado_en: str
    ultimo_uso: Optional[str] = None


class APIKeyCreada(APIKeyRespuesta):
    # La clave en TEXTO PLANO: solo se devuelve al crearla, nunca más.
    # Hay que entregársela al consumidor en ese momento.
    llave: str
