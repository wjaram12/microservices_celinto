"""
Servidor MOCK del portal de consulta de títulos de SENESCYT (alta fidelidad).

Imita el portal JSF/PrimeFaces real para ejercitar el pipeline completo del
scraper (captcha por imagen -> OCR con ddddocr -> POST -> parseo HTML), sin
depender del portal real. Para ir al real basta apuntar SENESCYT_BASE_URL allá.

Endpoints (mismas rutas que el portal real, para que el scraper no cambie):
  GET  /consulta-titulos-web/faces/vista/consulta/consulta.xhtml  -> página + ViewState + cookie
  GET  /consulta-titulos-web/Captcha.jpg                          -> imagen de captcha (nueva)
  POST /consulta-titulos-web/faces/vista/consulta/consulta.xhtml  -> búsqueda / detalle / PDF

Estado por sesión (cookie JSESSIONID) en memoria: el texto del captcha vigente y
el mapa ri->cédula del último listado. Correr con UN solo worker.

Arrancar:  uvicorn mock.main:app --port 8090
"""
import re
import threading
import uuid

from fastapi import FastAPI, Request, Response

from . import captcha as captcha_mod
from . import datos as datos_mod
from . import plantillas

app = FastAPI(title="Mock SENESCYT - Consulta de Títulos")

CONSULTA_PATH = "/consulta-titulos-web/faces/vista/consulta/consulta.xhtml"
CAPTCHA_PATH = "/consulta-titulos-web/Captcha.jpg"
COOKIE = "JSESSIONID"

# PDF mínimo válido (el scraper solo valida el Content-Type application/pdf).
PDF_MINIMO = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

# sesiones: sid -> {"captcha": str, "listado": {ri: cedula}}
_sesiones = {}
_lock = threading.Lock()

_RE_VER_INFO = re.compile(r"^formPrincipal:tablaTitulado:(\d+):j_idt\d+$")


def _nuevo_view_state() -> str:
    return "e1:" + uuid.uuid4().hex


def _sesion(request: Request):
    """Devuelve (sid, estado) de la cookie; crea la entrada si no existe."""
    sid = request.cookies.get(COOKIE)
    with _lock:
        if not sid or sid not in _sesiones:
            sid = sid or uuid.uuid4().hex
            _sesiones.setdefault(sid, {"captcha": "", "listado": {}})
        return sid, _sesiones[sid]


def _html(contenido: str, sid: str) -> Response:
    resp = Response(content=contenido, media_type="text/html; charset=utf-8")
    resp.set_cookie(COOKIE, sid, httponly=True)
    return resp


@app.get(CONSULTA_PATH)
def get_consulta(request: Request):
    """Página inicial: setea cookie de sesión y entrega el form + ViewState."""
    sid, _ = _sesion(request)
    return _html(plantillas.pagina_inicial(_nuevo_view_state()), sid)


@app.get(CAPTCHA_PATH)
def get_captcha(request: Request):
    """Genera un captcha NUEVO y guarda su texto para esta sesión."""
    sid, estado = _sesion(request)
    png, texto = captcha_mod.generar()
    with _lock:
        estado["captcha"] = texto
    resp = Response(content=png, media_type="image/png")
    resp.set_cookie(COOKIE, sid, httponly=True)
    return resp


@app.post(CONSULTA_PATH)
async def post_consulta(request: Request):
    """Procesa la búsqueda (valida captcha), el 'Ver Información' o el PDF."""
    sid, estado = _sesion(request)
    form = await request.form()
    claves = list(form.keys())
    vs = _nuevo_view_state()

    # 1) Botón del PDF (no requiere captcha): devuelve el binario.
    if "formPrincipal:btnInfoConsulta" in claves:
        resp = Response(content=PDF_MINIMO, media_type="application/pdf")
        resp.set_cookie(COOKIE, sid, httponly=True)
        return resp

    # 2) Command link 'Ver Información' de una fila del listado (no requiere captcha).
    for k in claves:
        m = _RE_VER_INFO.match(k)
        if m:
            ri = int(m.group(1))
            cedula = (estado.get("listado") or {}).get(ri) or (estado.get("listado") or {}).get(str(ri))
            reg = datos_mod.obtener_por_cedula(cedula) if cedula else None
            if reg:
                return _html(plantillas.detalle(vs, reg["persona"], reg["titulos"]), sid)
            return _html(plantillas.sin_resultados(vs), sid)

    # 3) Búsqueda inicial (botón Buscar): validar captcha.
    captcha_in = (form.get("formPrincipal:captchaSellerInput") or "").strip()
    esperado = estado.get("captcha") or ""
    if not esperado or captcha_in.upper() != esperado.upper():
        return _html(
            plantillas.mensaje_error(vs, "Los caracteres ingresados son incorrectos."),
            sid)

    identificacion = (form.get("formPrincipal:identificacion") or "").strip()
    apellidos = (form.get("formPrincipal:apellidos") or "").strip()

    # 3a) Búsqueda por cédula -> match único: detalle directo.
    if identificacion:
        reg = datos_mod.obtener_por_cedula(identificacion)
        if reg:
            return _html(plantillas.detalle(vs, reg["persona"], reg["titulos"]), sid)
        return _html(plantillas.sin_resultados(vs), sid)

    # 3b) Búsqueda por apellidos -> 0/1/varios.
    if apellidos:
        encontrados = datos_mod.buscar_por_apellidos(apellidos)
        if not encontrados:
            return _html(plantillas.sin_resultados(vs), sid)
        if len(encontrados) == 1:
            _, reg = encontrados[0]
            return _html(plantillas.detalle(vs, reg["persona"], reg["titulos"]), sid)
        personas = []
        mapa = {}
        for ri, (cedula, reg) in enumerate(encontrados):
            personas.append((ri, cedula, reg["persona"].get("Nombres", "")))
            mapa[ri] = cedula
        with _lock:
            estado["listado"] = mapa
        return _html(plantillas.listado(vs, personas), sid)

    return _html(plantillas.sin_resultados(vs), sid)
