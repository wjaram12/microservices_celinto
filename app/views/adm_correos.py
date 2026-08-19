"""
View adm_correos: página del panel para los correos institucionales creados.

Solo aporta la PÁGINA:
    Página:  GET /admin/correos  (plantilla templates/correos/)

No define API propia a propósito. Los datos ya los sirve el router de Google
Workspace, que es su dueño y donde vive la tabla `google_vinculos`:

    GET /api/v1/google-services/vinculos/            listado paginado y filtrable
    GET /api/v1/google-services/vinculos/resumen     cifras y anomalías
    GET /api/v1/google-services/vinculos/reporte.csv el mismo listado como CSV

Duplicar aquí esos endpoints solo crearía una segunda copia de las reglas de
visibilidad (una clave de consumo ve únicamente lo suyo) que habría que mantener
en dos sitios.
"""
from fastapi import APIRouter, Request

from app.core.plantillas import plantillas

api = APIRouter()
paginas = APIRouter()


@paginas.get("/admin/correos", include_in_schema=False)
def pagina(request: Request):
    """
    HTML SIN secretos: el candado está en la API, que exige X-API-Key. La página
    pide la clave al usuario igual que el resto del panel.
    """
    return plantillas.TemplateResponse(request, "correos/index.html", {"pagina": "correos"})
