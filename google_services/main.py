"""
App STANDALONE de Google Workspace (solo API, sin interfaz).

Útil para correr el servicio por separado. Las rutas viven en router.py (`api`),
un solo servidor. La lógica vive en cliente.py.

Arrancar (desde services/):  uvicorn google_services.main:app --port 8092
"""
import logging

from fastapi import FastAPI

from .router import api, verificar_credenciales

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Google Workspace Directory",
    description=("Administra el directorio de Google Workspace (usuarios, unidades "
                 "organizativas y grupos) a través del Admin SDK, con un service "
                 "account que impersona a un administrador del dominio."),
    version="1.0.0",
)

app.include_router(api)


@app.on_event("startup")
def _startup():
    verificar_credenciales()


@app.get("/health", tags=["Infra"])
def health():
    return {"status": "ok"}
