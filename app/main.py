# app/main.py
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse

from app import seguridad
from app.api import api_router

# 1. INICIALIZAMOS LA APLICACIÓN DE FASTAPI
app = FastAPI(
    title="Core de Clasificación - Universidad",
    description="API de inferencia pura para consumir tu modelo de Document AI",
    version="1.1.0"
)

# 2. BASE DE DATOS DE API KEYS
# Crea la tabla de claves si no existe. Las claves se gestionan con el script
# gestionar_llaves.py (no hay endpoint HTTP de administración).
seguridad.inicializar()

# 3. CARGAMOS TODAS LAS RUTAS DE LA V1
# `dependencies=[Depends(verificar_api_key)]` protege TODOS los endpoints de
# inferencia de una sola vez: ninguno responde sin una X-API-Key válida.
app.include_router(
    api_router,
    prefix="/api/v1",
    dependencies=[Depends(seguridad.verificar_api_key)],
)

# 4. PÁGINA DE ADMINISTRACIÓN DE PROMPTS
# HTML estático SIN secretos: el candado real está en la API (los endpoints
# /prompts/clases/ exigen scope admin). La página pide la clave admin al
# usuario y la usa en la cabecera X-API-Key; sin clave válida no puede hacer
# nada. Por eso servir el HTML sin autenticación es seguro.
RUTA_ADMIN = Path(__file__).resolve().parent / "static" / "admin.html"


@app.get("/admin", include_in_schema=False)
def admin():
    return FileResponse(RUTA_ADMIN, media_type="text/html")


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