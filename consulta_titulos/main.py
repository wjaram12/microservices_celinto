"""
App STANDALONE de consulta de títulos SENESCYT (solo API, sin interfaz).

Útil para correr el servicio por separado. Las rutas viven en router.py (`api`),
que también se monta dentro del clasificador (app/main.py) para unificar todo en
un solo servidor. La lógica vive en cache.py / fuente.py / scraper.py.

Arrancar (desde services/):  uvicorn consulta_titulos.main:app --port 8091
"""
import logging

from fastapi import FastAPI

from .router import api, calentar_ocr

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Consulta de Títulos SENESCYT",
    description=("Consulta los títulos registrados de una persona en SENESCYT, con "
                 "resolución automática de captcha (OCR) y caché de 30 días."),
    version="1.0.0",
)

app.include_router(api)


@app.on_event("startup")
def _startup():
    calentar_ocr()


@app.get("/health", tags=["Infra"])
def health():
    return {"status": "ok"}
