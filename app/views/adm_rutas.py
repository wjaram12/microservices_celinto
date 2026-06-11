"""
View adm_rutas: catálogo de rutas (URLs) de la API.

Todas sus rutas en un solo lugar:
    API  (scope admin):  GET/POST /api/v1/rutas/  ·  GET/PUT/DELETE /api/v1/rutas/{clave}
    Página:              GET /admin/rutas  (plantilla templates/rutas/)

La lógica vive en el servicio ServicioRutas. Este catálogo es la mitad "URL" de
la integración: el CRUD de procesadores une cada ruta registrada aquí con sus
procesadores de Extend. No se puede borrar una ruta que tenga procesadores
asociados (primero hay que reasignarlos o borrarlos).
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2 import errors as pg_errors

from app.core.plantillas import plantillas
from app.core.seguridad import requiere_admin
from app.schemas.rutas import RutaActualizar, RutaCrear, RutaRespuesta
from app.services.procesadores import procesadores
from app.services.rutas import rutas

api = APIRouter()
paginas = APIRouter()


# --- Página ---

@paginas.get("/admin/rutas", include_in_schema=False)
def pagina(request: Request):
    return plantillas.TemplateResponse(
        request, "rutas/index.html", {"pagina": "rutas"}
    )


# --- API (scope admin) ---

@api.get("/rutas/", response_model=List[RutaRespuesta], tags=["Rutas (admin)"])
def listar_rutas(solo_activos: bool = False, _admin: dict = Depends(requiere_admin)):
    return rutas.listar(solo_activos=solo_activos)


@api.get("/rutas/{clave}", response_model=RutaRespuesta, tags=["Rutas (admin)"])
def obtener_ruta(clave: str, _admin: dict = Depends(requiere_admin)):
    r = rutas.obtener(clave)
    if r is None:
        raise HTTPException(status_code=404, detail=f"No existe una ruta con la clave '{clave}'.")
    return r


@api.post("/rutas/", response_model=RutaRespuesta, status_code=201, tags=["Rutas (admin)"])
def crear_ruta(datos: RutaCrear, _admin: dict = Depends(requiere_admin)):
    try:
        return rutas.crear(datos.clave, datos.url, datos.descripcion, datos.activo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except pg_errors.UniqueViolation:
        # La clave es UNIQUE: 409 (conflicto), no 500.
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe una ruta con la clave '{rutas.normalizar_clave(datos.clave)}'.",
        )


@api.put("/rutas/{clave}", response_model=RutaRespuesta, tags=["Rutas (admin)"])
def actualizar_ruta(clave: str, datos: RutaActualizar, _admin: dict = Depends(requiere_admin)):
    try:
        r = rutas.actualizar(clave, datos.url, datos.descripcion, datos.activo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if r is None:
        raise HTTPException(status_code=404, detail=f"No existe una ruta con la clave '{clave}'.")
    return r


@api.delete("/rutas/{clave}", status_code=204, tags=["Rutas (admin)"])
def eliminar_ruta(clave: str, _admin: dict = Depends(requiere_admin)):
    clave_norm = rutas.normalizar_clave(clave)
    if rutas.obtener(clave_norm) is None:
        raise HTTPException(status_code=404, detail=f"No existe una ruta con la clave '{clave}'.")
    # Integridad de la unión: una ruta con procesadores asociados no se borra.
    asociados = [p for p in procesadores.listar() if p["ruta"] == clave_norm]
    if asociados:
        raise HTTPException(
            status_code=409,
            detail=(f"La ruta '{clave_norm}' tiene {len(asociados)} procesador(es) "
                    "asociado(s). Bórralos o reasígnalos primero en /admin/procesadores."),
        )
    rutas.eliminar(clave_norm)
