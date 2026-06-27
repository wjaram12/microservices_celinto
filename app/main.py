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
import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.seguridad import verificar_api_key
from app.services.consumidores import consumidores
from app.services.procesadores import procesadores
from app.services.prompts import prompts
from app.services.rutas import rutas
from app.views import (
    adm_cache, adm_consultas, adm_consumidores, adm_procesadores, adm_prompts,
    adm_rutas, documentos,
)

logger = logging.getLogger(__name__)

# La consulta de títulos SENESCYT corre EN EL MISMO servidor: se monta su router
# (sus endpoints ya declaran su propia auth: consulta = clave válida, gestión de
# caché = scope admin). Comparte commons (DB/Redis/api_keys) con el clasificador.
#
# Import TOLERANTE A FALLOS: sus dependencias son pesadas (ddddocr/onnxruntime/
# opencv). Si no están instaladas en el servidor, NO debe tumbar el clasificador:
# se loguea y se arranca sin esas rutas (instala consulta_titulos/requirements.txt
# para habilitarlas).
try:
    from consulta_titulos.router import api as consulta_titulos_api, calentar_ocr
    _consulta_titulos_ok = True
except Exception:
    logger.exception(
        "No se pudo cargar la consulta de títulos; el clasificador arranca SIN esas "
        "rutas. Instala consulta_titulos/requirements.txt para habilitarlas.")
    _consulta_titulos_ok = False

app = FastAPI(
    title="Core de Clasificación - Universidad",
    description="API de inferencia que clasifica, hace OCR y extrae datos con Extend (extend.ai)",
    version="2.0.0"
)

consumidores.inicializar()
prompts.inicializar()
rutas.inicializar()
procesadores.inicializar()


if _consulta_titulos_ok:
    @app.on_event("startup")
    def _calentar_ocr_senescyt():
        """Pre-carga el OCR (ddddocr) de la consulta de títulos al arrancar el worker."""
        calentar_ocr()


for view in (documentos, adm_prompts, adm_rutas, adm_procesadores, adm_consumidores,
             adm_cache, adm_consultas):
    app.include_router(
        view.api,
        prefix="/api/v1",
        dependencies=[Depends(verificar_api_key)],
    )

# Router de consulta de títulos (auth declarada por endpoint -> sin dep global aquí).
# Solo si sus dependencias cargaron (ver import tolerante a fallos arriba).
if _consulta_titulos_ok:
    app.include_router(consulta_titulos_api, prefix="/api/v1")

for view in (adm_procesadores, adm_rutas, adm_consumidores, adm_consultas):
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
