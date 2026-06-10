from typing import Optional
from pydantic import BaseModel


class ClasePrompt(BaseModel):
    # Identificador interno de la clase en el esquema (no editable; se usa en
    # la URL del PUT para indicar qué clase se actualiza).
    name: str
    # Etiqueta legible de la clase: la que el clasificador devuelve como tipo.
    display_name: str
    # El prompt: descripción que guía al modelo fundacional al clasificar.
    description: str


class ActualizarClase(BaseModel):
    # Ambos opcionales: se actualiza solo lo que se envíe. Debe llegar al
    # menos uno (lo valida el endpoint).
    description: Optional[str] = None
    display_name: Optional[str] = None
