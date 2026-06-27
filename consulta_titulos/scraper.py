# -*- coding: UTF-8 -*-
"""
Scraper de la consulta pública de títulos de SENESCYT (Ecuador).

Núcleo portable: depende solo de requests, beautifulsoup4, ddddocr y urllib3.
La URL base y la verificación SSL salen de `config.settings`, así que el MISMO
código corre contra el servidor mock local o contra el portal real cambiando
solo `SENESCYT_BASE_URL` (y `VERIFY_SSL=false` para el real).

Flujo (JSF + captcha tipo imagen):
  1) iniciar_sesion()  -> abre sesión HTTP, obtiene ViewState + cookies + captcha
  2) consultar(...)    -> envía cédula/apellidos + captcha y parsea resultados
  3) ver_titulos(...)  -> ejecuta el command link "Ver Información" de una fila
  4) descargar_informe_pdf() -> POST del botón 'Imprimir Información'

Variantes con OCR (ddddocr): consultar_auto(), ver_titulos_auto() y el all-in-one
consultar_y_obtener_detalle().
"""
import base64
import logging
import re
import threading
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

from .config import (
    CAPTCHA_PATH,
    CONSULTA_PATH,
    MAX_INTENTOS_CAPTCHA,
    TIMEOUT,
    USER_AGENT,
    settings,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)


class SenescytScraperError(Exception):
    pass


class SenescytScraper:
    """
    Wrapper sobre requests.Session que mantiene cookies y ViewState entre llamadas.
    export_state()/from_state() permiten persistir la sesión entre requests web
    (no se usa en el flujo server-side de esta app, pero se conserva por portabilidad).
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        })
        self.view_state = None
        self._last_identificacion = ""
        self._last_apellidos = ""
        self._ver_info_cmd_id = ""

    # ---------- (de)serialización de sesión ----------
    def export_state(self):
        return {
            "cookies": requests.utils.dict_from_cookiejar(self.session.cookies),
            "view_state": self.view_state,
            "last_identificacion": self._last_identificacion,
            "last_apellidos": self._last_apellidos,
            "ver_info_cmd_id": self._ver_info_cmd_id,
        }

    @classmethod
    def from_state(cls, state):
        inst = cls()
        if state:
            cookies = state.get("cookies") or {}
            inst.session.cookies.update(cookies)
            inst.view_state = state.get("view_state")
            inst._last_identificacion = state.get("last_identificacion", "") or ""
            inst._last_apellidos = state.get("last_apellidos", "") or ""
            inst._ver_info_cmd_id = state.get("ver_info_cmd_id", "") or ""
        return inst

    # ---------- helpers HTTP ----------
    def _base(self):
        return settings.SENESCYT_BASE_URL

    def _get(self, path, **kwargs):
        return self.session.get(urljoin(self._base(), path),
                                verify=settings.VERIFY_SSL, timeout=TIMEOUT, **kwargs)

    def _post(self, path, data, **kwargs):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._base(),
            "Referer": urljoin(self._base(), CONSULTA_PATH),
        }
        return self.session.post(urljoin(self._base(), path), data=data, headers=headers,
                                 verify=settings.VERIFY_SSL, timeout=TIMEOUT, **kwargs)

    @staticmethod
    def _extraer_view_state(html):
        soup = BeautifulSoup(html, "html.parser")
        node = soup.find("input", {"name": "javax.faces.ViewState"})
        if not node or not node.get("value"):
            raise SenescytScraperError("No se pudo obtener javax.faces.ViewState del formulario.")
        return node["value"]

    @staticmethod
    def _extraer_ver_info_cmd_id(html):
        """Extrae el sufijo j_idtNN del link 'Ver Información' del listado.
        SENESCYT regenera ese id en cada despliegue (j_idt32, j_idt37, ...)."""
        m = re.search(r"formPrincipal:tablaTitulado:\d+:(j_idt\d+)", html or "")
        return m.group(1) if m else ""

    # ---------- API pública ----------
    def iniciar_sesion(self):
        """Carga la página, captura ViewState y descarga la imagen del captcha."""
        try:
            resp = self._get(CONSULTA_PATH)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SenescytScraperError(f"No se pudo abrir la consulta de SENESCYT: {e}") from e

        self.view_state = self._extraer_view_state(resp.text)

        try:
            cap = self._get(CAPTCHA_PATH)
            cap.raise_for_status()
        except requests.RequestException as e:
            raise SenescytScraperError(f"No se pudo descargar el captcha: {e}") from e

        return {
            "captcha_b64": base64.b64encode(cap.content).decode("ascii"),
            "captcha_mime": cap.headers.get("Content-Type", "image/jpeg"),
        }

    def refrescar_captcha(self):
        """Solicita una nueva imagen de captcha manteniendo la sesión."""
        try:
            cap = self._get(CAPTCHA_PATH)
        except requests.RequestException as e:
            raise SenescytScraperError(
                f"No se pudo conectar con SENESCYT para refrescar el captcha: {e}"
            ) from e

        ct = cap.headers.get("Content-Type", "").lower()
        if cap.status_code != 200 or "image" not in ct:
            raise SenescytScraperError(
                "El servicio de captcha de SENESCYT no está disponible en este "
                f"momento (HTTP {cap.status_code}). Es un problema temporal de "
                "SENESCYT. Intentá de nuevo en unos minutos."
            )

        return {
            "captcha_b64": base64.b64encode(cap.content).decode("ascii"),
            "captcha_mime": cap.headers.get("Content-Type", "image/jpeg"),
        }

    def consultar(self, captcha, identificacion="", apellidos=""):
        """Envía la búsqueda inicial. Devuelve listado de personas o detalle directo."""
        if not self.view_state:
            raise SenescytScraperError("La sesión no está inicializada. Llama iniciar_sesion() primero.")
        if not captcha:
            raise SenescytScraperError("El captcha es obligatorio.")
        if not identificacion and not apellidos:
            raise SenescytScraperError("Debe ingresar cédula o apellidos.")

        data = {
            "formPrincipal": "formPrincipal",
            "formPrincipal:apellidos": apellidos or "",
            "formPrincipal:identificacion": identificacion or "",
            "formPrincipal:captchaSellerInput": captcha,
            "formPrincipal:boton-buscar": "formPrincipal:boton-buscar",
            "javax.faces.ViewState": self.view_state,
        }

        try:
            resp = self._post(CONSULTA_PATH, data=data)
        except requests.RequestException as e:
            raise SenescytScraperError(f"Fallo al enviar la consulta: {e}") from e

        if resp.status_code >= 500:
            raise SenescytScraperError(
                f"El servicio de SENESCYT no está disponible (HTTP {resp.status_code}). "
                "Es un problema temporal de SENESCYT. Intentá de nuevo en unos minutos."
            )
        if resp.status_code >= 400:
            raise SenescytScraperError(f"SENESCYT respondió HTTP {resp.status_code} en la consulta.")

        try:
            self.view_state = self._extraer_view_state(resp.text)
        except SenescytScraperError:
            pass

        # PrimeFaces re-envía el form entero al ejecutar "Ver Información".
        self._last_identificacion = identificacion or ""
        self._last_apellidos = apellidos or ""

        listado = self._parse_listado_personas(resp.text)
        cmd_id = self._extraer_ver_info_cmd_id(resp.text)
        if cmd_id:
            self._ver_info_cmd_id = cmd_id
        return listado

    # ---------- modo automático con OCR ----------
    _ocr_singleton = None
    _ocr_lock = threading.Lock()

    @classmethod
    def _ocr(cls):
        """Carga ddddocr una sola vez (instanciar es caro: ~1-2s). La creación va
        bajo lock para que dos hilos no instancien el modelo a la vez."""
        if cls._ocr_singleton is None:
            with cls._ocr_lock:
                if cls._ocr_singleton is None:
                    try:
                        import ddddocr  # import diferido: librería opcional y pesada
                    except ImportError as e:
                        raise SenescytScraperError(
                            "ddddocr no está instalado. Ejecuta: pip install ddddocr"
                        ) from e
                    cls._ocr_singleton = ddddocr.DdddOcr(show_ad=False)
        return cls._ocr_singleton

    @classmethod
    def _resolver_captcha(cls, img_bytes) -> str:
        """Pasa la imagen por el OCR. Serializa el acceso al modelo compartido con
        un lock: el InferenceSession se reutiliza entre hilos (FastAPI corre los
        endpoints sync en un threadpool) y el lock evita carreras sobre él."""
        ocr = cls._ocr()
        with cls._ocr_lock:
            return (ocr.classification(img_bytes) or "").strip()

    def consultar_auto(self, identificacion="", apellidos="", max_intentos=MAX_INTENTOS_CAPTCHA):
        """Versión sin captcha humano: usa ddddocr y reintenta si SENESCYT rechaza."""
        if not identificacion and not apellidos:
            raise SenescytScraperError("Debe enviar cédula o apellidos.")

        if not self.view_state:
            self.iniciar_sesion()

        ultimo_mensaje = ""
        for intento in range(1, max_intentos + 1):
            try:
                cap = self.refrescar_captcha()
            except SenescytScraperError as e:
                if "no está disponible" in str(e).lower():
                    raise
                ultimo_mensaje = str(e)
                continue

            img_bytes = base64.b64decode(cap["captcha_b64"])
            texto = self._resolver_captcha(img_bytes)
            if not texto:
                ultimo_mensaje = "OCR vacío"
                continue
            try:
                resultado = self.consultar(captcha=texto,
                                           identificacion=identificacion,
                                           apellidos=apellidos)
            except SenescytScraperError as e:
                if "no está disponible" in str(e).lower():
                    raise
                ultimo_mensaje = str(e)
                continue
            if self._captcha_fallido(resultado.get("mensaje", "")):
                ultimo_mensaje = resultado["mensaje"]
                continue
            resultado["intentos"] = intento
            resultado["captcha_usado"] = texto
            return resultado

        raise SenescytScraperError(
            f"No se pudo resolver el captcha tras {max_intentos} intentos. "
            f"Último mensaje: {ultimo_mensaje}"
        )

    def consultar_y_obtener_detalle(self, identificacion="", apellidos="",
                                    max_intentos=MAX_INTENTOS_CAPTCHA, ri_objetivo=None):
        """All-in-one: resuelve captcha, consulta y carga el detalle si hay match único."""
        listado = self.consultar_auto(identificacion=identificacion,
                                      apellidos=apellidos,
                                      max_intentos=max_intentos)
        if listado.get("es_detalle"):
            return listado

        personas = listado.get("personas") or []
        if not personas:
            return {**listado, "persona": {}, "titulos": [], "pdf_disponible": False}

        if ri_objetivo is None and len(personas) == 1:
            ri_objetivo = personas[0]["ri"]
        if ri_objetivo is None:
            return {**listado, "persona": {}, "titulos": [], "pdf_disponible": False}

        detalle = self.ver_titulos(ri_objetivo)
        return {
            **listado,
            "ri_seleccionado": ri_objetivo,
            "persona": detalle["persona"],
            "titulos": detalle["titulos"],
            "pdf_disponible": detalle.get("pdf_disponible", False),
        }

    def ver_titulos(self, ri, captcha=""):
        """Ejecuta el command link 'Ver Información' del registro ri."""
        if not self.view_state:
            raise SenescytScraperError("La sesión no está inicializada.")
        try:
            ri = int(ri)
        except (TypeError, ValueError):
            raise SenescytScraperError("Índice de fila inválido.")

        cmd_suffix = self._ver_info_cmd_id or "j_idt32"
        cmd = f"formPrincipal:tablaTitulado:{ri}:{cmd_suffix}"
        data = {
            "formPrincipal": "formPrincipal",
            "formPrincipal:apellidos": getattr(self, "_last_apellidos", "") or "",
            "formPrincipal:identificacion": getattr(self, "_last_identificacion", "") or "",
            "formPrincipal:captchaSellerInput": captcha or "",
            cmd: cmd,
            "javax.faces.ViewState": self.view_state,
        }
        try:
            resp = self._post(CONSULTA_PATH, data=data)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SenescytScraperError(f"Fallo al cargar los títulos: {e}") from e

        try:
            self.view_state = self._extraer_view_state(resp.text)
        except SenescytScraperError:
            pass

        return self._parse_detalle_titulos(resp.text)

    def ver_titulos_auto(self, ri, max_intentos=MAX_INTENTOS_CAPTCHA):
        """Versión OCR de ver_titulos: resuelve captcha nuevo y reintenta."""
        if not self.view_state:
            raise SenescytScraperError("La sesión no está inicializada.")
        ultimo_mensaje = ""
        for intento in range(1, max_intentos + 1):
            try:
                cap = self.refrescar_captcha()
            except SenescytScraperError as e:
                if "no está disponible" in str(e).lower():
                    raise
                ultimo_mensaje = str(e)
                continue
            img_bytes = base64.b64decode(cap["captcha_b64"])
            texto = self._resolver_captcha(img_bytes)
            if not texto:
                ultimo_mensaje = "OCR vacío"
                continue
            try:
                detalle = self.ver_titulos(ri, captcha=texto)
            except SenescytScraperError as e:
                if "no está disponible" in str(e).lower():
                    raise
                ultimo_mensaje = str(e)
                continue
            msg = (detalle.get("mensaje") or "").lower()
            if "ingrese los caracteres" in msg or self._captcha_fallido(msg):
                ultimo_mensaje = detalle.get("mensaje", "")
                continue
            detalle["intentos"] = intento
            detalle["captcha_usado"] = texto
            return detalle
        raise SenescytScraperError(
            f"No se pudo resolver el captcha tras {max_intentos} intentos. "
            f"Último mensaje: {ultimo_mensaje}"
        )

    def descargar_pdf(self, url_relativa):
        """Descarga el PDF del certificado dado un href de los resultados."""
        if not url_relativa:
            raise SenescytScraperError("URL del certificado no provista.")
        url = urljoin(self._base() + CONSULTA_PATH, url_relativa)
        try:
            resp = self._get(url.replace(self._base(), ""))
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SenescytScraperError(f"Fallo al descargar el PDF: {e}") from e
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower():
            raise SenescytScraperError(
                f"La respuesta no es un PDF (Content-Type={content_type}). "
                "Es posible que la sesión haya expirado."
            )
        return resp.content, content_type

    def descargar_informe_pdf(self):
        """Descarga el PDF 'Imprimir Información' del detalle actual."""
        if not self.view_state:
            raise SenescytScraperError("La sesión no está inicializada.")
        cmd = "formPrincipal:btnInfoConsulta"
        data = {
            "formPrincipal": "formPrincipal",
            "formPrincipal:apellidos": getattr(self, "_last_apellidos", "") or "",
            "formPrincipal:identificacion": getattr(self, "_last_identificacion", "") or "",
            "formPrincipal:captchaSellerInput": "",
            cmd: cmd,
            "javax.faces.ViewState": self.view_state,
        }
        try:
            resp = self._post(CONSULTA_PATH, data=data)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SenescytScraperError(f"Fallo al descargar el PDF: {e}") from e

        if "text/html" in resp.headers.get("Content-Type", "").lower():
            try:
                self.view_state = self._extraer_view_state(resp.text)
            except SenescytScraperError:
                pass

        ct = resp.headers.get("Content-Type", "")
        if "pdf" not in ct.lower():
            raise SenescytScraperError(
                f"La respuesta no es un PDF (Content-Type={ct}). "
                "La sesión pudo haber expirado o el botón no estaba disponible."
            )
        return resp.content, ct

    # ---------- parsing ----------
    @staticmethod
    def _texto(td):
        return re.sub(r"\s+", " ", td.get_text(" ", strip=True)) if td else ""

    @classmethod
    def _mensaje_pagina(cls, soup):
        """Extrae el mensaje visible (summary + detail) que PrimeFaces renderiza."""
        partes = []
        for sel in (".ui-messages-error-detail", ".ui-messages-info-detail",
                    ".ui-messages-error-summary", ".ui-messages-info-summary"):
            for n in soup.select(sel):
                t = cls._texto(n)
                if t and t not in partes:
                    partes.append(t)
        return " - ".join(partes)

    @staticmethod
    def _captcha_fallido(mensaje):
        """True si el mensaje de SENESCYT indica que el captcha fue incorrecto."""
        if not mensaje:
            return False
        m = mensaje.lower()
        return ("caracter" in m and "incorrect" in m) or "captcha" in m

    @classmethod
    def _parse_listado_personas(cls, html):
        """Interpreta la búsqueda inicial: (a) listado, (b) detalle único, (c) vacío."""
        soup = BeautifulSoup(html, "html.parser")
        mensaje = cls._mensaje_pagina(soup)

        # (a) Listado
        personas = []
        dt = soup.find("div", id="formPrincipal:tablaTitulado")
        if dt:
            tbody = dt.find("tbody", id="formPrincipal:tablaTitulado_data")
            if tbody:
                for tr in tbody.find_all("tr"):
                    if "ui-datatable-empty-message" in (tr.get("class") or []):
                        continue
                    tds = tr.find_all("td")
                    if len(tds) < 2:
                        continue
                    ri = tr.get("data-ri")
                    try:
                        ri = int(ri) if ri is not None else None
                    except ValueError:
                        ri = None
                    personas.append({
                        "ri": ri,
                        "identificacion": cls._texto(tds[0]),
                        "nombres": cls._texto(tds[1]),
                    })

        # (b) Detalle directo (match único)
        es_detalle = False
        persona = {}
        titulos = []
        pdf_disponible = False
        if not personas and soup.find("button", id="formPrincipal:btnInfoConsulta"):
            detalle = cls._parse_detalle_titulos(html)
            persona = detalle["persona"]
            titulos = detalle["titulos"]
            pdf_disponible = detalle.get("pdf_disponible", False)
            es_detalle = bool(persona) or bool(titulos)
            if es_detalle:
                personas = [{
                    "ri": None,
                    "identificacion": persona.get("Identificación", ""),
                    "nombres": persona.get("Nombres", ""),
                }]

        return {
            "ok": bool(personas) or es_detalle,
            "mensaje": mensaje or ("Sin resultados." if not personas and not es_detalle else ""),
            "personas": personas,
            "es_detalle": es_detalle,
            "persona": persona,
            "titulos": titulos,
            "pdf_disponible": pdf_disponible,
        }

    @classmethod
    def _parse_detalle_titulos(cls, html):
        """Parsea la pantalla de detalle: datos del titulado + tabla(s) de títulos."""
        soup = BeautifulSoup(html, "html.parser")
        mensaje = cls._mensaje_pagina(soup)

        # Información personal: panelGrid con "Identifica" y SIN inputs/botones.
        datos_persona = {}
        info_grid = None
        for tbl in soup.find_all("table", class_=re.compile("ui-panelgrid")):
            if "Identifica" not in tbl.get_text():
                continue
            if tbl.find(["input", "button", "select", "textarea"]):
                continue
            info_grid = tbl
            break
        if info_grid:
            for tr in info_grid.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue
                clave = cls._texto(tds[0]).rstrip(":").strip()
                valor = cls._texto(tds[1]).strip()
                if clave:
                    datos_persona[clave] = valor

        # Tablas de títulos: id ...tablaAplicaciones, con <h4> de categoría.
        titulos = []
        for dt in soup.find_all("div", id=re.compile(r"tablaAplicaciones$")):
            categoria = ""
            anterior = dt.find_previous(["h4", "h3"])
            if anterior:
                categoria = cls._texto(anterior)

            thead = dt.find("thead")
            tbody = dt.find("tbody")
            if not tbody:
                continue
            headers = [cls._texto(th) for th in thead.find_all("th")] if thead else []
            for tr in tbody.find_all("tr"):
                if "ui-datatable-empty-message" in (tr.get("class") or []):
                    continue
                tds = tr.find_all("td")
                if not tds:
                    continue
                fila = {}
                if categoria:
                    fila["Categoría"] = categoria
                for i, td in enumerate(tds):
                    clave = headers[i] if i < len(headers) and headers[i] else f"col_{i}"
                    fila[clave] = cls._texto(td)
                titulos.append(fila)

        pdf_disponible = bool(soup.find("button", id="formPrincipal:btnInfoConsulta") or
                              soup.find(id="formPrincipal:gridBotonPdf"))

        return {
            "ok": bool(titulos) or bool(datos_persona),
            "mensaje": mensaje,
            "persona": datos_persona,
            "titulos": titulos,
            "pdf_disponible": pdf_disponible,
        }
