"""
View adm_prompts: administración de las clasificaciones (prompts) del clasificador.

Solo API (scope admin):  GET/POST /api/v1/prompts/  ·  GET/PUT/DELETE /api/v1/prompts/{clave}

La lógica vive en el servicio ServicioPrompts. Son las clases que se envían a
Extend al clasificar en modo inline (clave, tipo, descripción). La página del
panel se retiró (2026-06-11): ya no se usa; los prompts se gestionan por API si
hace falta. La tabla `clasificaciones` sigue viva porque el modo inline del
clasificador la lee.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import errors as pg_errors

from app.core.seguridad import requiere_admin
from app.schemas.prompts import PromptActualizar, PromptCrear, PromptRespuesta
from app.services.prompts import prompts

api = APIRouter()


@api.get("/prompts/", response_model=List[PromptRespuesta], tags=["Prompts (admin)"])
def listar_prompts(solo_activos: bool = False, _admin: dict = Depends(requiere_admin)):
    return prompts.listar(solo_activos=solo_activos)


@api.get("/prompts/{clave}", response_model=PromptRespuesta, tags=["Prompts (admin)"])
def obtener_prompt(clave: str, _admin: dict = Depends(requiere_admin)):
    p = prompts.obtener(clave)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No existe un prompt con la clave '{clave}'.")
    return p


@api.post("/prompts/", response_model=PromptRespuesta, status_code=201, tags=["Prompts (admin)"])
def crear_prompt(datos: PromptCrear, _admin: dict = Depends(requiere_admin)):
    if not prompts.normalizar_clave(datos.clave):
        raise HTTPException(status_code=400, detail="La clave no puede estar vacía.")
    try:
        return prompts.crear(datos.clave, datos.tipo, datos.descripcion, datos.activo)
    except pg_errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe un prompt con la clave '{prompts.normalizar_clave(datos.clave)}'.",
        )


@api.put("/prompts/{clave}", response_model=PromptRespuesta, tags=["Prompts (admin)"])
def actualizar_prompt(clave: str, datos: PromptActualizar, _admin: dict = Depends(requiere_admin)):
    p = prompts.actualizar(clave, datos.tipo, datos.descripcion, datos.activo)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No existe un prompt con la clave '{clave}'.")
    return p


@api.delete("/prompts/{clave}", status_code=204, tags=["Prompts (admin)"])
def eliminar_prompt(clave: str, _admin: dict = Depends(requiere_admin)):
    if not prompts.eliminar(clave):
        raise HTTPException(status_code=404, detail=f"No existe un prompt con la clave '{clave}'.")
