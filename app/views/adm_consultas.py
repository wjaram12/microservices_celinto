"""
View adm_consultas: consola SQL del panel de administración.

    API (scope admin):
        GET  /api/v1/consultas/   ping (valida la clave admin; no ejecuta SQL)
        POST /api/v1/consultas/   ejecuta una sentencia SQL
    Página: GET /admin/consultas  (plantilla templates/consultas/)

⚠ Ejecuta SQL ARBITRARIO contra la base del servicio. Solo accesible con API key
de scope admin. Tras una ESCRITURA reinicia la caché de Redis automáticamente,
para que los workers relean la configuración nueva (procesadores/prompts) sin
esperar ni reiniciar procesos.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2 import Error as PgError

from app.core.cache import cache
from app.core.plantillas import plantillas
from app.core.seguridad import requiere_admin
from app.schemas.consultas import ConsultaSQL
from app.services.consultas import consultas

logger = logging.getLogger(__name__)

api = APIRouter()
paginas = APIRouter()


@paginas.get("/admin/consultas", include_in_schema=False)
def pagina(request: Request):
    return plantillas.TemplateResponse(request, "consultas/index.html", {"pagina": "consultas"})


@api.get("/consultas/", tags=["Consultas SQL (admin)"])
def ping(_admin: dict = Depends(requiere_admin)):
    """Valida la clave admin (lo usa la página al iniciar sesión). No ejecuta SQL."""
    return {"ok": True}


@api.post("/consultas/", tags=["Consultas SQL (admin)"])
def ejecutar_consulta(datos: ConsultaSQL, _admin: dict = Depends(requiere_admin)):
    """
    Ejecuta una sentencia SQL y devuelve filas (consulta) o el rowcount (comando).
    Tras una escritura, reinicia la caché para que los workers vean la config nueva.
    """
    try:
        resultado = consultas.ejecutar(datos.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PgError as e:
        raise HTTPException(status_code=400, detail=str(e).strip())
    except Exception:
        logger.exception("Error inesperado ejecutando SQL en la consola admin")
        raise HTTPException(status_code=500, detail="Error interno ejecutando la consulta.")

    if resultado.get("tipo") == "comando":
        resultado["cache_reiniciada"] = cache.reiniciar()
    return resultado
