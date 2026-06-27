"""
NÚCLEO DE CACHÉ — el corazón de esta app.

Cachea el resultado de la consulta de títulos POR CÉDULA en Redis, con TTL nativo
de 30 días (SETEX): consultar el portal SENESCYT es lento (resuelve un captcha por
OCR, a veces con reintentos, y el portal real tarda 30-50 s), así que una vez
obtenidos los títulos de una persona se sirven desde caché durante 30 días.

Por qué Redis con TTL nativo (y no un campo `valido_hasta` manual): la vigencia la
gestiona Redis con la expiración de la clave; no hay que comparar fechas ni limpiar
registros viejos. La clave caduca sola y la siguiente consulta vuelve a la fuente.

Por qué centralizada (Redis y no memoria): en producción corren varios workers; una
caché en memoria sería una copia por worker. Con Redis es única y compartida.

Degradación: si Redis no responde, el núcleo NO se cae — consulta la fuente
directamente (sin cachear) y registra el incidente. El servicio sigue arriba.

Modos (parámetro `modo` de consultar_titulo):
    auto      (default) caché vigente -> caché; si no, fuente en vivo y se guarda.
    local     SOLO caché; si no hay, status='no_en_cache' (no consulta la fuente).
    senescyt  siempre consulta la fuente en vivo; `force_refresh` ignora la caché.

Nota: a diferencia del doc original (Django, con BD interna de títulos), aquí no
hay BD propia de títulos, así que el modo 'local' significa "solo lo cacheado".
"""
import base64
import json
import logging

import redis

from commons.redis_cache import obtener_cliente as _obtener_cliente

from .config import settings
from .errores import ErrorDeFuente
from .fuente import fuente

logger = logging.getLogger(__name__)

PREFIJO = "senescyt:titulos:"
PREFIJO_PDF = "senescyt:pdf:"


def _normalizar_cedula(identificacion: str) -> str:
    """Clave de caché estable: sin espacios ni guiones, en mayúsculas (los
    pasaportes pueden ser alfanuméricos)."""
    return "".join((identificacion or "").split()).replace("-", "").upper()


class CacheTitulos:
    """Núcleo de caché de consultas de títulos (cache-aside sobre Redis + fuente)."""

    def _leer(self, cedula: str):
        """Devuelve el dict cacheado para la cédula o None (también None si Redis
        no responde -> el llamador decide ir a la fuente)."""
        clave = PREFIJO + cedula
        try:
            crudo = _obtener_cliente().get(clave)
        except redis.RedisError:
            logger.warning("Redis no disponible al leer '%s'; se irá a la fuente.",
                           cedula, exc_info=True)
            return None
        if crudo is None:
            return None
        try:
            return json.loads(crudo)
        except ValueError:
            logger.warning("Valor de caché corrupto en '%s'; se recarga.", cedula)
            return None

    def _guardar(self, cedula: str, valor: dict) -> None:
        """Guarda con expiración de CACHE_TTL_SEGUNDOS (30 días). Tolera que Redis
        no esté disponible: en ese caso simplemente no se cachea."""
        clave = PREFIJO + cedula
        try:
            _obtener_cliente().setex(clave, settings.CACHE_TTL_SEGUNDOS, json.dumps(valor))
        except (redis.RedisError, TypeError):
            logger.warning("Redis no disponible al guardar '%s'; se sigue sin cachear.",
                           cedula, exc_info=True)

    def _ttl(self, cedula: str):
        """Segundos que le quedan de vigencia a la clave; None si no aplica/Redis cae."""
        try:
            ttl = _obtener_cliente().ttl(PREFIJO + cedula)
        except redis.RedisError:
            return None
        return ttl if ttl is not None and ttl >= 0 else None

    def consultar_titulo(self, identificacion: str, apellidos: str = "",
                         modo: str = "auto", force_refresh: bool = False,
                         incluir_pdf: bool = False) -> dict:
        """
        Resuelve la consulta de títulos aplicando la política de caché del `modo`.

        Devuelve un dict con:
            status        'encontrado' | 'no_encontrado' | 'no_en_cache'
            fuente        'cache' | 'senescyt'
            persona, titulos, total_titulos, pdf_disponible
            vigente       True si vino de caché vigente
            ttl_segundos  vigencia restante (si vino de caché), o None
            intentos_captcha  nº de intentos de OCR (si se consultó en vivo)

        Lanza ErrorDeFuente si hay que ir a la fuente y esta falla.
        """
        cedula = _normalizar_cedula(identificacion)
        if not cedula and not (apellidos or "").strip():
            from errores import ErrorDeValidacion
            raise ErrorDeValidacion("Debe enviar una cédula/identificación o apellidos.")

        modo = (modo or "auto").strip().lower()

        # Lectura de caché (salvo que se fuerce refresco).
        cacheado = None if force_refresh else self._leer(cedula)
        if cacheado is not None and modo != "senescyt":
            return self._con_pdf(self._desde_cache(cacheado, cedula), cedula, apellidos, incluir_pdf)

        if modo == "local":
            # Solo caché: no se consulta la fuente.
            return {
                "status": "no_en_cache",
                "fuente": "cache",
                "persona": {},
                "titulos": [],
                "total_titulos": 0,
                "pdf_disponible": False,
                "vigente": False,
                "ttl_segundos": None,
                "intentos_captcha": None,
                "pdf_base64": None,
                "pdf_bytes": None,
            }

        # modo 'auto' sin caché, o modo 'senescyt': consultar la fuente en vivo.
        datos = fuente.consultar(cedula, apellidos)  # puede lanzar ErrorDeFuente

        # Se cachea siempre que la consulta a la fuente fue exitosa (incluso si no
        # hubo títulos: 'no_encontrado' vigente 30 días evita martillar el portal).
        valor = {
            "identificacion": datos["identificacion"],
            "nombres": datos["nombres"],
            "persona": datos["persona"],
            "titulos": datos["titulos"],
            "pdf_disponible": datos["pdf_disponible"],
            "encontrado": datos["encontrado"],
            "intentos_captcha": datos["intentos_captcha"],
        }
        self._guardar(cedula, valor)

        return self._con_pdf({
            "status": "encontrado" if datos["encontrado"] else "no_encontrado",
            "fuente": "senescyt",
            "persona": datos["persona"],
            "titulos": datos["titulos"],
            "total_titulos": len(datos["titulos"]),
            "pdf_disponible": datos["pdf_disponible"],
            "vigente": False,
            "ttl_segundos": None,
            "intentos_captcha": datos["intentos_captcha"],
        }, cedula, apellidos, incluir_pdf)

    def _con_pdf(self, resp: dict, cedula: str, apellidos: str, incluir_pdf: bool) -> dict:
        """Adjunta el PDF en base64 a la respuesta si se pidió `incluir_pdf` y hay PDF.
        Reutiliza obtener_pdf (cacheado 30 días). Si el PDF falla, no rompe la consulta:
        deja pdf_base64/pdf_bytes en None (la consulta ya fue exitosa)."""
        resp["pdf_base64"] = None
        resp["pdf_bytes"] = None
        if not incluir_pdf or resp.get("status") != "encontrado" or not resp.get("pdf_disponible"):
            return resp
        try:
            pdf = self.obtener_pdf(cedula, apellidos)
            resp["pdf_base64"] = pdf["pdf_base64"]
            resp["pdf_bytes"] = pdf["bytes"]
        except Exception:
            logger.warning("No se pudo incluir el PDF de '%s' en la consulta.", cedula, exc_info=True)
        return resp

    def _desde_cache(self, cacheado: dict, cedula: str) -> dict:
        titulos = cacheado.get("titulos") or []
        return {
            "status": "encontrado" if cacheado.get("encontrado") else "no_encontrado",
            "fuente": "cache",
            "persona": cacheado.get("persona") or {},
            "titulos": titulos,
            "total_titulos": len(titulos),
            "pdf_disponible": bool(cacheado.get("pdf_disponible")),
            "vigente": True,
            "ttl_segundos": self._ttl(cedula),
            "intentos_captcha": cacheado.get("intentos_captcha"),
        }

    def obtener_pdf(self, identificacion: str, apellidos: str = "",
                    force_refresh: bool = False) -> dict:
        """
        Devuelve el PDF oficial en base64, cacheado en Redis 30 días (clave aparte).
        SENESCYT no publica una URL del PDF, así que se obtiene del portal y se
        entrega como base64 (sin guardar nada en disco).

        Devuelve {pdf_base64, content_type, bytes, fuente, vigente, ttl_segundos}.
        Lanza ErrorDeFuente si el portal falla o no hay PDF para esa identidad.
        """
        cedula = _normalizar_cedula(identificacion)
        clave = PREFIJO_PDF + cedula

        if not force_refresh:
            try:
                crudo = _obtener_cliente().get(clave)
            except redis.RedisError:
                crudo = None
            if crudo:
                try:
                    datos = json.loads(crudo)
                    b64 = datos["pdf_base64"]
                    return {
                        "pdf_base64": b64,
                        "content_type": datos.get("content_type", "application/pdf"),
                        "bytes": (len(b64) * 3) // 4,
                        "fuente": "cache",
                        "vigente": True,
                        "ttl_segundos": self._ttl_pdf(cedula),
                    }
                except (ValueError, KeyError):
                    logger.warning("PDF cacheado corrupto en '%s'; se recarga.", cedula)

        pdf, ct = fuente.obtener_pdf(cedula, apellidos)  # puede lanzar ErrorDeFuente
        b64 = base64.b64encode(pdf).decode("ascii")
        try:
            _obtener_cliente().setex(
                clave, settings.CACHE_TTL_SEGUNDOS,
                json.dumps({"pdf_base64": b64, "content_type": ct}))
        except (redis.RedisError, TypeError):
            logger.warning("Redis no disponible al guardar el PDF '%s'.", cedula, exc_info=True)
        return {
            "pdf_base64": b64,
            "content_type": ct,
            "bytes": len(pdf),
            "fuente": "senescyt",
            "vigente": False,
            "ttl_segundos": None,
        }

    def _ttl_pdf(self, cedula: str):
        try:
            ttl = _obtener_cliente().ttl(PREFIJO_PDF + cedula)
        except redis.RedisError:
            return None
        return ttl if ttl is not None and ttl >= 0 else None

    def invalidar(self, identificacion: str) -> bool:
        """Borra de la caché la cédula (títulos y PDF). Devuelve True si borró algo."""
        cedula = _normalizar_cedula(identificacion)
        try:
            return bool(_obtener_cliente().delete(PREFIJO + cedula, PREFIJO_PDF + cedula))
        except redis.RedisError:
            logger.warning("Redis no disponible al invalidar '%s'.", identificacion, exc_info=True)
            return False

    def reiniciar(self) -> int:
        """Vacía toda la caché del servicio (títulos y PDFs) y devuelve cuántas
        claves borró. Tolera que Redis no esté disponible (devuelve 0)."""
        try:
            cliente = _obtener_cliente()
            claves = list(cliente.scan_iter(match=PREFIJO + "*"))
            claves += list(cliente.scan_iter(match=PREFIJO_PDF + "*"))
            if claves:
                cliente.delete(*claves)
            return len(claves)
        except redis.RedisError:
            logger.warning("Redis no disponible al reiniciar la caché.", exc_info=True)
            return 0


cache = CacheTitulos()
