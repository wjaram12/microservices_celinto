"""
View adm_procesadores: administración de los procesadores de Extend por ruta.

Todas sus rutas en un solo lugar:
    API  (scope admin):
        GET/POST /api/v1/procesadores/            CRUD
        GET/PUT/DELETE /api/v1/procesadores/{id}
        GET /api/v1/procesadores/extend           sincronizar con Extend Studio
        GET /api/v1/procesadores/extend/esquema   importar el esquema de un extractor
    Página: GET /admin/procesadores  (plantilla templates/procesadores/)

La lógica vive en el servicio ServicioProcesadores. Es lo que permite cambiar
clasificadores, extractores, esquemas y umbrales en caliente, sin redeploy.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2 import errors as pg_errors

from app.core.plantillas import plantillas
from app.core.seguridad import requiere_admin
from app.schemas.procesadores import (
    ProcesadorActualizar, ProcesadorCrear, ProcesadorExtend,
    ProcesadorRespuesta, PublicarConfigExtend,
)
from app.services.errores import ErrorDeProveedor, ErrorDeValidacion
from app.services.procesadores import procesadores

logger = logging.getLogger(__name__)

api = APIRouter()
paginas = APIRouter()


@paginas.get("/admin/procesadores", include_in_schema=False)
def pagina(request: Request):
    return plantillas.TemplateResponse(
        request, "procesadores/index.html", {"pagina": "procesadores"}
    )


@api.get("/procesadores/", response_model=List[ProcesadorRespuesta], tags=["Procesadores (admin)"])
def listar_procesadores(solo_activos: bool = False, _admin: dict = Depends(requiere_admin)):
    return procesadores.listar(solo_activos=solo_activos)


@api.get("/procesadores/extend", response_model=List[ProcesadorExtend], tags=["Procesadores (admin)"])
async def procesadores_de_extend(tipo: str, _admin: dict = Depends(requiere_admin)):
    """
    Lista los procesadores publicados en Extend Studio (GET /processors) para
    elegirlos en /admin. `tipo` = 'clasificar' o 'extraer'.

    CRÍTICO: este endpoint está declarado a propósito ANTES de la ruta
    "/procesadores/{id_proc}" para que 'extend' no se interprete como un id; si
    se reordena, se rompe el ruteo.
    """
    try:
        return await procesadores.listar_de_extend(tipo)
    except ErrorDeValidacion as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeProveedor as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error listando procesadores de Extend")
        raise HTTPException(status_code=500, detail="Error interno al consultar Extend.")


@api.get("/procesadores/extend/esquema", tags=["Procesadores (admin)"])
async def esquema_de_extend(procesador_id: str, version_id: str, _admin: dict = Depends(requiere_admin)):
    """
    Devuelve el JSON Schema de extracción de una versión de un procesador de
    Extend, para importarlo al editor de esquema en /admin.
    """
    try:
        return await procesadores.esquema_de_extend(procesador_id, version_id)
    except ErrorDeValidacion as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeProveedor as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error obteniendo el esquema de Extend")
        raise HTTPException(status_code=500, detail="Error interno al consultar Extend.")


@api.post("/procesadores/{id_proc}/extend", tags=["Procesadores (admin)"])
async def publicar_config_en_extend(id_proc: int, datos: PublicarConfigExtend,
                                    _admin: dict = Depends(requiere_admin)):
    """
    Empuja la config editada en el modal al procesador de Extend asociado
    (clasificaciones para CLASSIFY, JSON Schema para EXTRACT) y publica una
    versión nueva (release minor) — la ruta en 'última publicada' la usa de
    inmediato. La config no se persiste en local: Extend es la fuente de verdad.
    """
    try:
        resultado = await procesadores.publicar_config_en_extend(id_proc, datos.esquema)
    except ErrorDeValidacion as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeProveedor as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error actualizando el procesador en Extend")
        raise HTTPException(status_code=500, detail="Error interno al actualizar en Extend.")
    if resultado is None:
        raise HTTPException(status_code=404, detail=f"No existe un procesador con id {id_proc}.")
    return resultado


@api.get("/procesadores/{id_proc}", response_model=ProcesadorRespuesta, tags=["Procesadores (admin)"])
def obtener_procesador(id_proc: int, _admin: dict = Depends(requiere_admin)):
    p = procesadores.obtener_por_id(id_proc)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No existe un procesador con id {id_proc}.")
    return p


@api.post("/procesadores/", response_model=ProcesadorRespuesta, status_code=201, tags=["Procesadores (admin)"])
def crear_procesador(datos: ProcesadorCrear, _admin: dict = Depends(requiere_admin)):
    try:
        return procesadores.crear(
            datos.ruta, datos.operacion, datos.clase, datos.modo,
            datos.procesador_id, datos.version, datos.esquema, datos.umbral, datos.activo,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except pg_errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail=(f"Ya existe un procesador para la ruta "
                    f"'{procesadores.normalizar_ruta(datos.ruta)}', operación "
                    f"'{procesadores.normalizar_operacion(datos.operacion)}' y clase "
                    f"'{procesadores.normalizar_clase(datos.clase)}'."),
        )


@api.put("/procesadores/{id_proc}", response_model=ProcesadorRespuesta, tags=["Procesadores (admin)"])
def actualizar_procesador(id_proc: int, datos: ProcesadorActualizar, _admin: dict = Depends(requiere_admin)):
    """
    Actualiza un procesador.

    procesador_id, version, esquema y umbral admiten null explícito, por eso se
    distingue "no enviado" de "enviado como null" con model_fields_set.
    """
    enviados = datos.model_fields_set
    try:
        p = procesadores.actualizar(
            id_proc,
            ruta=datos.ruta,
            operacion=datos.operacion,
            clase=datos.clase,
            modo=datos.modo,
            procesador_id=datos.procesador_id,
            version=datos.version,
            esquema=datos.esquema,
            umbral=datos.umbral,
            activo=datos.activo,
            tocar_procesador_id="procesador_id" in enviados,
            tocar_version="version" in enviados,
            tocar_esquema="esquema" in enviados,
            tocar_umbral="umbral" in enviados,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except pg_errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="Ya existe otro procesador con esa combinación de ruta, operación y clase.",
        )
    if p is None:
        raise HTTPException(status_code=404, detail=f"No existe un procesador con id {id_proc}.")
    return p


@api.delete("/procesadores/{id_proc}", status_code=204, tags=["Procesadores (admin)"])
def eliminar_procesador(id_proc: int, _admin: dict = Depends(requiere_admin)):
    if not procesadores.eliminar(id_proc):
        raise HTTPException(status_code=404, detail=f"No existe un procesador con id {id_proc}.")
