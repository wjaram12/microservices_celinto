# Consulta de Títulos SENESCYT — Guía portable

> Scraper del portal público de títulos de SENESCYT (Ecuador) con resolución
> automática de captcha por OCR y cache local. Pensado para copiar/adaptar a
> otro proyecto. El **núcleo (`SenescytScraper`) no depende de Django**: solo
> de `requests`, `beautifulsoup4` y `ddddocr`.

---

## 1. Arquitectura

| Capa | Archivo origen | Dependencias | Portabilidad |
|------|----------------|--------------|--------------|
| **Scraper** (núcleo) | `app/service/senescyt_scraper.py` | `requests`, `beautifulsoup4`, `ddddocr`, `urllib3` | **Totalmente portable** (Python puro) |
| **Cache / BD** | `app/service/senescyt_cache.py` | Django ORM + modelos | Acoplado a Django; reescribir si el otro stack no es Django |
| **Vista / endpoint** | `app/adm_consulta_titulos.py` | Django views + `request.session` | Adaptar a tu framework web |
| **Template** | `consulta_titulos/view.html` | Django templates + JS | Front a gusto |

```
Navegador ──POST action──▶ vista ──▶ senescyt_cache ──▶ SenescytScraper ──▶ portal SENESCYT
                                         │ (BD local + cache 30 días)
                                         ▼
                                  ConsultaTituloSenescyt
```

## 2. Dependencias

```bash
pip install requests beautifulsoup4 ddddocr urllib3
```

- `ddddocr` resuelve el captcha de imagen automáticamente (sin intervención humana).
  Es opcional: si solo quieres el flujo con captcha manual, no lo instales y usa
  `consultar()` en vez de `consultar_auto()`.
- Instanciar `ddddocr.DdddOcr()` cuesta ~1-2 s → se cachea como singleton.

## 3. Endpoints / parámetros del portal SENESCYT

Sitio JSF + PrimeFaces. **Los nombres de campo son fijos del formulario JSF.**

| Concepto | Valor |
|----------|-------|
| Base | `https://www.senescyt.gob.ec` |
| Consulta | `/consulta-titulos-web/faces/vista/consulta/consulta.xhtml` |
| Captcha (imagen) | `/consulta-titulos-web/Captcha.jpg` |
| SSL | roto → `verify=False` |
| Timeout | `(10, 60)` — en horas pico tarda 30-50 s |

Campos del POST de búsqueda:

```
formPrincipal                       = "formPrincipal"
formPrincipal:identificacion        = <cédula>
formPrincipal:apellidos             = <apellidos>
formPrincipal:captchaSellerInput    = <texto captcha>
formPrincipal:boton-buscar          = "formPrincipal:boton-buscar"
javax.faces.ViewState               = <extraído del HTML previo>
```

## 4. Flujo del scraper

1. `iniciar_sesion()` → GET de la consulta, extrae `javax.faces.ViewState`, baja el captcha en base64.
2. `consultar(captcha, identificacion, apellidos)` → POST. SENESCYT devuelve:
   - (a) **listado de personas** (tabla `formPrincipal:tablaTitulado`), o
   - (b) **detalle directo** si hay match único por cédula, o
   - (c) sin resultados / captcha errado.
3. `ver_titulos(ri, captcha)` → ejecuta el command link "Ver Información"
   `formPrincipal:tablaTitulado:{ri}:{j_idtNN}` para una persona del listado.
4. `descargar_informe_pdf()` → POST del botón `formPrincipal:btnInfoConsulta`, valida `Content-Type: application/pdf`.

Variantes automáticas con OCR: `consultar_auto()`, `ver_titulos_auto()`,
`consultar_y_obtener_detalle()` (all-in-one).

## 5. ⚠️ Detalles NO obvios (lo que cuesta descubrir)

- **El id JSF `j_idtNN` del link "Ver Información" se regenera entre despliegues
  de SENESCYT** (p. ej. `j_idt32` → `j_idt37`). NO lo hardcodees: se extrae con
  regex `formPrincipal:tablaTitulado:\d+:(j_idt\d+)` y se cachea en
  `_ver_info_cmd_id` (fallback `j_idt32`).
- **PrimeFaces re-envía el formulario completo** al hacer click en un command
  link → hay que reenviar `identificacion`/`apellidos` (`_last_*`).
- **Detección de captcha fallido** por el mensaje renderizado: contiene
  (`"caracter"` + `"incorrect"`) o `"captcha"`. Reintenta hasta 6 veces.
- **SENESCYT caído** (HTTP ≥500, o el captcha devuelve HTML en vez de imagen):
  corta de inmediato sin reintentar y da un mensaje claro de "problema temporal
  de SENESCYT, no del sistema".
- **Persistencia de sesión entre requests HTTP**: `export_state()` / `from_state()`
  serializan cookies + ViewState para guardarlos en la sesión del usuario
  (en Django: `request.session['senescyt_scraper_state']`).
- **Parsing** (BeautifulSoup):
  - Listado en `div#formPrincipal:tablaTitulado`.
  - Datos de persona = panelgrid que contiene "Identifica" y **NO** tiene inputs/botones.
  - Títulos en `div[id$=tablaAplicaciones]`; la categoría se toma del `<h4>` previo.

## 6. Modos de búsqueda (capa cache)

`senescyt_cache.consultar_titulo(identificacion, apellidos, ..., modo=)`:

| Modo | Comportamiento |
|------|----------------|
| `auto` (default) | Cascada: **BD local** → **cache** → **scraping en vivo** |
| `local` | Solo BD interna (`TituloPersona`); si no está, sugiere usar SENESCYT |
| `senescyt` | Solo scraping en vivo (cache vigente acelera; `force_refresh=True` lo ignora) |

TTL del cache = **30 días** (`CACHE_TTL_DIAS`).

## 7. Modelo de cache (Django) — adaptable

`ConsultaTituloSenescyt` (un registro por persona, `identificacion` único):

| Campo | Tipo | Nota |
|-------|------|------|
| `identificacion` | CharField unique, indexado | cédula |
| `nombres` | CharField | nombre completo |
| `persona` | JSONField (dict) | datos crudos de SENESCYT |
| `titulos` | JSONField (list) | lista de títulos parseados |
| `pdf` | FileField (`senescyt_pdfs/%Y/%m/`) | PDF oficial cacheado |
| `intentos_captcha` | IntegerField | nº de intentos OCR |
| `fecha_consulta` | DateTimeField (auto_now) | última consulta |
| `valido_hasta` | DateTimeField | vigencia del cache |
| `consultado_por` | FK Persona (SET_NULL) | auditoría |

Propiedades: `vigente` (`valido_hasta >= now`), `total_titulos`.

> En otro stack sin Django: reemplaza este modelo por una tabla equivalente o
> un cache key-value (Redis), guardando `persona`/`titulos` como JSON.

## 8. Contrato de la vista (acciones POST)

`adm_consulta_titulos.view` recibe `action` por POST (una sola vista, estilo
controlador). Respuestas en JSON (`result: true` = error):

| `action` | Hace | Devuelve |
|----------|------|----------|
| `iniciar` | abre sesión + captcha | `captcha_b64`, `captcha_mime` |
| `refrescar_captcha` | nueva imagen captcha | `captcha_b64`, `captcha_mime` |
| `consultar` / `refrescar` | busca (cache o vivo; `refrescar` fuerza) | `personas`, `es_detalle`, `persona`, `titulos`, `pdf_disponible`, `fuente`, ... |
| `ver_titulos` | detalle de una fila del listado | `persona`, `titulos`, `pdf_disponible` |
| `descargar_certificado` | baja el PDF | binario `application/pdf` |

## 9. Código fuente completo (núcleo portable)

> Copia este archivo tal cual a tu proyecto. No importa nada de Django.

```python
# senescyt_scraper.py
# -*- coding: UTF-8 -*-
"""
Scraper de la consulta pública de títulos de SENESCYT.
URL pública: https://www.senescyt.gob.ec/consulta-titulos-web/faces/vista/consulta/consulta.xhtml

Flujo (JSF + captcha tipo imagen):
  1) iniciar_sesion()  -> abre sesión HTTP, obtiene ViewState + cookies + captcha base64
  2) consultar(...)    -> envía cédula/apellidos + captcha y parsea resultados
  3) descargar_pdf(...)-> descarga un PDF de título usando el enlace devuelto
"""
import base64
import logging
import re
import urllib3
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.senescyt.gob.ec"
CONSULTA_PATH = "/consulta-titulos-web/faces/vista/consulta/consulta.xhtml"
CAPTCHA_PATH = "/consulta-titulos-web/Captcha.jpg"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# (timeout_conexion, timeout_lectura) en segundos. 60s porque SENESCYT
# a veces tarda 30-50s en responder en horarios pico.
TIMEOUT = (10, 60)


class SenescytScraperError(Exception):
    pass


class SenescytScraper:
    """
    Wrapper sobre requests.Session que mantiene cookies y ViewState entre llamadas.
    export_state()/from_state() permiten persistir la sesión entre requests web.
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
    def _get(self, path, **kwargs):
        return self.session.get(urljoin(BASE_URL, path), verify=False, timeout=TIMEOUT, **kwargs)

    def _post(self, path, data, **kwargs):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE_URL,
            "Referer": urljoin(BASE_URL, CONSULTA_PATH),
        }
        return self.session.post(urljoin(BASE_URL, path), data=data, headers=headers,
                                 verify=False, timeout=TIMEOUT, **kwargs)

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

    @classmethod
    def _ocr(cls):
        """Carga ddddocr una sola vez (instanciar es caro: ~1-2s)."""
        if cls._ocr_singleton is None:
            try:
                import ddddocr  # import diferido: librería opcional
            except ImportError as e:
                raise SenescytScraperError(
                    "ddddocr no está instalado. Ejecuta: pip install ddddocr"
                ) from e
            cls._ocr_singleton = ddddocr.DdddOcr(show_ad=False)
        return cls._ocr_singleton

    def consultar_auto(self, identificacion="", apellidos="", max_intentos=6):
        """Versión sin captcha humano: usa ddddocr y reintenta si SENESCYT rechaza."""
        if not identificacion and not apellidos:
            raise SenescytScraperError("Debe enviar cédula o apellidos.")

        ocr = self._ocr()
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
            texto = (ocr.classification(img_bytes) or "").strip()
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
                                    max_intentos=6, ri_objetivo=None):
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

    def ver_titulos_auto(self, ri, max_intentos=6):
        """Versión OCR de ver_titulos: resuelve captcha nuevo y reintenta."""
        if not self.view_state:
            raise SenescytScraperError("La sesión no está inicializada.")
        ocr = self._ocr()
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
            texto = (ocr.classification(img_bytes) or "").strip()
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
        url = urljoin(BASE_URL + CONSULTA_PATH, url_relativa)
        try:
            resp = self._get(url.replace(BASE_URL, ""))
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

        # Tablas de títulos: id formPrincipal:j_idt##:N:tablaAplicaciones, con <h4> de categoría.
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
```

## 10. Ejemplo de uso standalone (sin web)

```python
from senescyt_scraper import SenescytScraper, SenescytScraperError

scraper = SenescytScraper()
try:
    # Flujo all-in-one por cédula (resuelve captcha solo con OCR):
    detalle = scraper.consultar_y_obtener_detalle(identificacion="0912345678")
    print(detalle["persona"])
    for t in detalle["titulos"]:
        print(t.get("Categoría"), "->", t.get("Título"))

    if detalle.get("pdf_disponible"):
        pdf_bytes, ct = scraper.descargar_informe_pdf()
        with open("titulo.pdf", "wb") as fh:
            fh.write(pdf_bytes)
except SenescytScraperError as e:
    print("Error:", e)
```

## 11. Checklist para portar a otro proyecto

1. `pip install requests beautifulsoup4 ddddocr urllib3`.
2. Copiar `senescyt_scraper.py` (sección 9) — funciona tal cual.
3. Decidir cache: reusar `ConsultaTituloSenescyt` (Django) o una tabla/Redis equivalente con `persona`/`titulos` como JSON y `valido_hasta` (TTL 30 días).
4. Exponer un endpoint con las acciones de la sección 8; persistir `export_state()` en la sesión del usuario entre `iniciar`/`refrescar_captcha`/`ver_titulos`.
5. Para uso server-side puro (sin captcha manual), usar `consultar_y_obtener_detalle()` y olvidarse de la sesión.
6. Manejar `SenescytScraperError` y distinguir "SENESCYT caído" (mensaje contiene "no está disponible") para no reintentar en vano.

---

*Origen: proyecto SGA-Posgrados (UCG). Archivos fuente: `app/service/senescyt_scraper.py`, `app/service/senescyt_cache.py`, `app/adm_consulta_titulos.py`.*
