"""
FuenteSenescyt: la capa "de dónde salen los datos", aislada del núcleo de caché.

Hoy envuelve el SenescytScraper (que apunta al mock o al portal real según
config). El núcleo de caché (cache.py) solo conoce esta interfaz: `consultar()`.
Si mañana la fuente cambia (otra API, otro scraper), se reemplaza aquí sin tocar
el caché ni la app.

Normaliza la respuesta cruda del scraper a un dict estable y serializable a JSON
(para guardarlo en Redis):

    {
      "identificacion": str,
      "nombres": str,
      "persona": dict,          # datos del titulado tal como los expone SENESCYT
      "titulos": list[dict],    # títulos parseados (con su categoría)
      "pdf_disponible": bool,
      "intentos_captcha": int,  # nº de intentos de OCR que costó resolver el captcha
      "encontrado": bool,       # hubo datos para esa identidad
    }
"""
import logging

from .errores import ErrorDeFuente
from .scraper import SenescytScraper, SenescytScraperError

logger = logging.getLogger(__name__)


class FuenteSenescyt:
    """Obtiene los títulos de una persona desde el portal (o el mock)."""

    def consultar(self, identificacion: str, apellidos: str = "") -> dict:
        """
        Resuelve el captcha por OCR, consulta y trae el detalle si hay match único.
        Lanza ErrorDeFuente si el portal no responde o el captcha no se pudo
        resolver. Devuelve el dict normalizado descrito arriba; `encontrado=False`
        cuando la consulta fue exitosa pero no hay títulos para esa identidad.
        """
        scraper = SenescytScraper()
        try:
            crudo = scraper.consultar_y_obtener_detalle(
                identificacion=identificacion, apellidos=apellidos)
        except SenescytScraperError as e:
            logger.warning("Fuente SENESCYT falló para %s: %s", identificacion, e)
            raise ErrorDeFuente(str(e)) from e

        persona = crudo.get("persona") or {}
        titulos = crudo.get("titulos") or []
        nombres = (persona.get("Nombres")
                   or persona.get("Nombres Completos")
                   or self._nombres_de_personas(crudo, identificacion))

        return {
            "identificacion": identificacion,
            "nombres": nombres or "",
            "persona": persona,
            "titulos": titulos,
            "pdf_disponible": bool(crudo.get("pdf_disponible")),
            "intentos_captcha": int(crudo.get("intentos") or 0),
            "encontrado": bool(persona) or bool(titulos),
        }

    def obtener_pdf(self, identificacion: str, apellidos: str = ""):
        """
        Descarga el PDF oficial del detalle. Como el PDF de SENESCYT no tiene URL
        pública (se genera en un postback JSF dentro de la sesión), hay que cargar
        el detalle y, en la MISMA sesión, ejecutar el botón 'Imprimir Información'.

        Devuelve (pdf_bytes, content_type). Lanza ErrorDeFuente si el portal falla
        o si esa identidad no tiene PDF (sin títulos, o no hubo match único).
        """
        scraper = SenescytScraper()
        try:
            detalle = scraper.consultar_y_obtener_detalle(
                identificacion=identificacion, apellidos=apellidos)
        except SenescytScraperError as e:
            logger.warning("Fuente SENESCYT falló (pdf) para %s: %s", identificacion, e)
            raise ErrorDeFuente(str(e)) from e

        if not detalle.get("pdf_disponible"):
            raise ErrorDeFuente(
                "El portal no ofrece PDF para esta identidad (sin títulos registrados "
                "o no hubo un match único por cédula).")
        try:
            return scraper.descargar_informe_pdf()
        except SenescytScraperError as e:
            logger.warning("No se pudo descargar el PDF de %s: %s", identificacion, e)
            raise ErrorDeFuente(str(e)) from e

    @staticmethod
    def _nombres_de_personas(crudo: dict, identificacion: str) -> str:
        """Si no hubo detalle pero sí listado, intenta sacar el nombre de la fila
        que coincide con la cédula buscada (o de la primera fila)."""
        personas = crudo.get("personas") or []
        for p in personas:
            if (p.get("identificacion") or "").strip() == (identificacion or "").strip():
                return p.get("nombres") or ""
        return personas[0].get("nombres", "") if personas else ""


fuente = FuenteSenescyt()
