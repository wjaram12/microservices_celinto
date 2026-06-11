# app/main.py
"""
Punto de montaje de la aplicación (patrón MVC):

    app/views/      capa HTTP: una view por recurso, con todas sus rutas
    app/services/   lógica de negocio: una clase de servicio por recurso
    app/schemas/    modelos Pydantic de entrada/salida
    app/templates/  plantillas del panel /admin, una carpeta por view
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

# 1. INICIALIZAMOS LA APLICACIÓN DE FASTAPI
app = FastAPI(
    title="Core de Clasificación - Universidad",
    description="API de inferencia que clasifica, hace OCR y extrae datos con Extend (extend.ai)",
    version="2.0.0"
)

# 2. BASE DE DATOS (PostgreSQL)
# Crea/siembra las tablas si no existen: api_keys (claves), clasificaciones
# (prompts del clasificador) y procesadores (config de Extend por ruta).
consumidores.inicializar()
prompts.inicializar()
rutas.inicializar()        # catálogo de rutas: lo referencia procesadores
procesadores.inicializar()

# 3. CARGAMOS LAS RUTAS DE LA V1 (una view por recurso)
# `dependencies=[Depends(verificar_api_key)]` protege TODOS los endpoints de
# una sola vez: ninguno responde sin una X-API-Key válida.
for view in (documentos, adm_prompts, adm_rutas, adm_procesadores, adm_consumidores):
    app.include_router(
        view.api,
        prefix="/api/v1",
        dependencies=[Depends(verificar_api_key)],
    )

# 4. PÁGINAS DE ADMINISTRACIÓN (una por view, en app/templates/)
# HTML SIN secretos: el candado real está en la API (los endpoints de
# administración exigen scope admin). Cada página pide la clave admin al
# usuario y la usa en la cabecera X-API-Key; sin clave válida no puede hacer
# nada. Por eso servir el HTML sin autenticación es seguro.
# (La página de prompts se retiró; su API sigue viva en /api/v1/prompts/.)
for view in (adm_procesadores, adm_rutas, adm_consumidores):
    app.include_router(view.paginas)

# Recursos estáticos del panel (jQuery vendorizado: sin CDNs, funciona offline).
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")


@app.get("/admin", include_in_schema=False)
def admin():
    """La URL histórica del panel lleva a la primera página (procesadores)."""
    return RedirectResponse("/admin/procesadores")


# 5. ENDPOINT BASE DE CONTROL (Para validar que el servidor responde)
# Se deja SIN autenticación a propósito: sirve de health check y no expone
# ningún dato sensible.
@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "Servicio de Clasificación Documental Universitario Activo 🚀",
        "admin_prompts": "/admin",
    }
