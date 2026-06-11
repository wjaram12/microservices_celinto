"""
View adm_consumidores: administración de las API keys de los consumidores.

Todas sus rutas en un solo lugar:
    API  (scope admin):  GET/POST /api/v1/api-keys/  ·  GET/PUT/DELETE /api/v1/api-keys/{id}
    Página:              GET /admin/consumidores  (plantilla templates/consumidores/)

La lógica vive en el servicio APIConsumidores. Operación sensible: crear claves
(incluso admin) da acceso al servicio; por eso exige scope 'admin'. El CLI
`gestionar_llaves.py` se mantiene para crear la PRIMERA clave admin (bootstrap).
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.plantillas import plantillas
from app.core.seguridad import requiere_admin
from app.schemas.api_keys import APIKeyActualizar, APIKeyCreada, APIKeyCrear, APIKeyRespuesta
from app.services.consumidores import consumidores

api = APIRouter()
paginas = APIRouter()


@paginas.get("/admin/consumidores", include_in_schema=False)
def pagina(request: Request):
    """
    HTML SIN secretos: el candado real está en la API (los endpoints exigen
    scope admin). La página pide la clave al usuario y la usa en X-API-Key.
    """
    return plantillas.TemplateResponse(
        request, "consumidores/index.html", {"pagina": "consumidores"}
    )


@api.get("/api-keys/", response_model=List[APIKeyRespuesta], tags=["API Keys (admin)"])
def listar_api_keys(_admin: dict = Depends(requiere_admin)):
    """Lista todas las API keys (metadatos; nunca la clave ni el hash)."""
    return consumidores.listar()


@api.get("/api-keys/{id_llave}", response_model=APIKeyRespuesta, tags=["API Keys (admin)"])
def obtener_api_key(id_llave: int, _admin: dict = Depends(requiere_admin)):
    llave = consumidores.obtener(id_llave)
    if llave is None:
        raise HTTPException(status_code=404, detail=f"No existe una API key con id {id_llave}.")
    return llave


@api.post("/api-keys/", response_model=APIKeyCreada, status_code=201, tags=["API Keys (admin)"])
def crear_api_key(datos: APIKeyCrear, _admin: dict = Depends(requiere_admin)):
    """
    Crea una API key nueva. Devuelve la clave EN TEXTO PLANO (`llave`) una sola
    vez: hay que entregarla al consumidor en ese momento, no se vuelve a tener.
    """
    try:
        return consumidores.crear(datos.consumidor, datos.scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.put("/api-keys/{id_llave}", response_model=APIKeyRespuesta, tags=["API Keys (admin)"])
def actualizar_api_key(id_llave: int, datos: APIKeyActualizar, _admin: dict = Depends(requiere_admin)):
    """Actualiza nombre, scope o estado (activo) de una clave. No cambia la clave en sí."""
    if datos.consumidor is None and datos.scope is None and datos.activo is None:
        raise HTTPException(
            status_code=400,
            detail="Envía al menos un campo para actualizar (consumidor, scope o activo).",
        )
    try:
        actualizada = consumidores.actualizar(
            id_llave, datos.consumidor, datos.scope, datos.activo
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if actualizada is None:
        raise HTTPException(status_code=404, detail=f"No existe una API key con id {id_llave}.")
    return actualizada


@api.delete("/api-keys/{id_llave}", status_code=204, tags=["API Keys (admin)"])
def revocar_api_key(id_llave: int, _admin: dict = Depends(requiere_admin)):
    """
    Revoca una clave (la desactiva). NO la borra: se conserva la fila para que
    el rastro de auditoría siga siendo válido. Para reactivarla, usar PUT con
    activo=true.
    """
    if consumidores.obtener(id_llave) is None:
        raise HTTPException(status_code=404, detail=f"No existe una API key con id {id_llave}.")
    consumidores.actualizar(id_llave, activo=False)
