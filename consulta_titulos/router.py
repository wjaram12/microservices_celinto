"""
Rutas HTTP de la consulta de títulos como APIRouter reutilizable.

Se expone como `api` para poder montarlo en DOS sitios:
  - en la app standalone (consulta_titulos/main.py), y
  - dentro del clasificador (app/main.py), unificando todo en un solo servidor.

Capa HTTP delgada: traduce el resultado del núcleo de caché a códigos HTTP. Cada
endpoint declara su propia dependencia de auth (consulta: clave válida; gestión de
caché: scope admin), así funciona igual montado donde sea.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from commons.seguridad import requiere_admin, verificar_api_key

from .cache import cache
from .config import settings
from .errores import ErrorDeFuente, ErrorDeValidacion
from .schemas import RespuestaConsultaTitulos, RespuestaPDF, SolicitudConsulta

logger = logging.getLogger(__name__)

api = APIRouter(tags=["Consulta de Títulos SENESCYT"])


def calentar_ocr() -> None:
    """Pre-carga ddddocr (el OCR del captcha) para que la PRIMERA petición no pague
    el ~1-2 s de instanciarlo y para fallar pronto si no se puede cargar. Best-effort:
    no tumba el arranque si falla. Llamar en el startup del servidor que monte estas
    rutas (la app standalone o el clasificador unificado)."""
    logger.info("Consulta de títulos -> SENESCYT_BASE_URL=%s (verify_ssl=%s)",
                settings.SENESCYT_BASE_URL, settings.VERIFY_SSL)
    if settings.SENESCYT_BASE_URL.startswith("http://localhost"):
        logger.warning("SENESCYT_BASE_URL apunta al MOCK local; configúralo al "
                       "portal real para producción.")
    try:
        from .scraper import SenescytScraper
        SenescytScraper._ocr()
        logger.info("OCR (ddddocr) precargado.")
    except Exception:
        logger.exception("No se pudo precargar ddddocr al arrancar.")


def _mensaje(resultado: dict) -> str:
    """Arma el mensaje legible a partir del estado estructurado."""
    estado = resultado["status"]
    via = "caché" if resultado["fuente"] == "cache" else "SENESCYT en vivo"
    if estado == "encontrado":
        return f"Se hallaron {resultado['total_titulos']} título(s) (vía {via})."
    if estado == "no_encontrado":
        return f"No hay títulos registrados para la identidad consultada (vía {via})."
    return ("No hay datos cacheados para esa identidad. Reintenta con modo 'auto' o "
            "'senescyt' para consultar en vivo.")


@api.post("/consulta-titulos/", response_model=RespuestaConsultaTitulos,
          dependencies=[Depends(verificar_api_key)])
def consultar_titulos(datos: SolicitudConsulta):
    """Consulta los títulos de una persona aplicando la política de caché del `modo`."""
    try:
        resultado = cache.consultar_titulo(
            identificacion=datos.identificacion,
            apellidos=datos.apellidos or "",
            modo=datos.modo,
            force_refresh=datos.force_refresh,
            incluir_pdf=datos.incluir_pdf,
        )
    except ErrorDeValidacion as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeFuente as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error inesperado en /consulta-titulos/")
        raise HTTPException(status_code=500, detail="Error interno al consultar los títulos.")

    return RespuestaConsultaTitulos(
        result=(resultado["status"] == "encontrado"),
        message=_mensaje(resultado),
        status=resultado["status"],
        fuente=resultado["fuente"],
        persona=resultado["persona"],
        titulos=resultado["titulos"],
        total_titulos=resultado["total_titulos"],
        pdf_disponible=resultado["pdf_disponible"],
        vigente=resultado["vigente"],
        ttl_segundos=resultado["ttl_segundos"],
        intentos_captcha=resultado["intentos_captcha"],
        pdf_base64=resultado.get("pdf_base64"),
        pdf_bytes=resultado.get("pdf_bytes"),
    )


@api.get("/consulta-titulos/{cedula}/pdf", response_model=RespuestaPDF,
         dependencies=[Depends(verificar_api_key)])
def descargar_pdf(cedula: str, force_refresh: bool = False):
    """
    Devuelve el PDF oficial del título en base64 (SENESCYT no expone una URL del
    PDF; se genera en una sesión JSF y aquí se entrega codificado). Cacheado 30
    días. `force_refresh=true` ignora la caché y vuelve a descargarlo del portal.
    """
    try:
        r = cache.obtener_pdf(cedula, force_refresh=force_refresh)
    except ErrorDeValidacion as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeFuente as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error inesperado en /consulta-titulos/{cedula}/pdf")
        raise HTTPException(status_code=500, detail="Error interno al obtener el PDF.")

    via = "caché" if r["fuente"] == "cache" else "SENESCYT en vivo"
    return RespuestaPDF(
        result=True,
        message=f"PDF obtenido ({r['bytes']} bytes, vía {via}).",
        cedula=cedula,
        content_type=r["content_type"],
        bytes=r["bytes"],
        pdf_base64=r["pdf_base64"],
        fuente=r["fuente"],
        vigente=r["vigente"],
        ttl_segundos=r["ttl_segundos"],
    )


@api.delete("/consulta-titulos/{cedula}", dependencies=[Depends(requiere_admin)])
def invalidar(cedula: str):
    """Borra de la caché la entrada de una cédula (la próxima consulta irá en vivo)."""
    borrada = cache.invalidar(cedula)
    return {"invalidada": borrada, "cedula": cedula}


@api.post("/consulta-titulos/cache/reiniciar", dependencies=[Depends(requiere_admin)])
def reiniciar_cache():
    """Vacía toda la caché de títulos (claves senescyt:*). No afecta la del clasificador."""
    return {"reiniciada": True, "claves_borradas": cache.reiniciar()}
