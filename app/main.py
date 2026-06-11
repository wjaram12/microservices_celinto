"""
Punto de montaje de la aplicación (patrón MVC):

    app/views/      capa HTTP: una view por recurso, con todas sus rutas
    app/services/   lógica de negocio: una clase de servicio por recurso
    app/schemas/    modelos Pydantic de entrada/salida
    app/templates/  plantillas del panel /admin, una carpeta por view

El HTML del panel /admin se sirve SIN autenticación a propósito: no contiene
secretos y el candado real está en los endpoints de administración, que exigen
scope admin; cada página pide la clave admin al usuario y la usa en la cabecera
X-API-Key, de modo que sin clave válida no puede hacer nada.
"""
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.seguridad import verificar_api_key
from app.services.consumidores import consumidores
from app.services.procesadores import procesadores
from app.services.prompts import prompts
from app.services.rutas import rutas
from app.views import adm_consumidores, adm_procesadores, adm_prompts, adm_rutas, documentos

app = FastAPI(
    title="Core de Clasificación - Universidad",
    description="API de inferencia que clasifica, hace OCR y extrae datos con Extend (extend.ai)",
    version="2.0.0"
)

consumidores.inicializar()
prompts.inicializar()
rutas.inicializar()
procesadores.inicializar()

for view in (documentos, adm_prompts, adm_rutas, adm_procesadores, adm_consumidores):
    app.include_router(
        view.api,
        prefix="/api/v1",
        dependencies=[Depends(verificar_api_key)],
    )

for view in (adm_procesadores, adm_rutas, adm_consumidores):
    app.include_router(view.paginas)

app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")


@app.get("/admin", include_in_schema=False)
def admin():
    """La URL histórica del panel lleva a la primera página (procesadores)."""
    return RedirectResponse("/admin/procesadores")


@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "Servicio de Clasificación Documental Universitario Activo 🚀",
        "admin_prompts": "/admin",
    }
