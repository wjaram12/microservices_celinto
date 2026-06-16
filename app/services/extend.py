"""
Servicio ClienteExtend: todas las llamadas HTTP a la API de Extend
(https://docs.extend.ai). Solo transporte; qué procesador/config usar lo
deciden ServicioProcesadores y ServicioDocumentos.

Flujo de inferencia (su API no acepta el binario directo, solo un file_id):
    1. POST /files/upload  (multipart)  -> file_id
    2. POST /classify      (file_id)    -> clase + confianza
    3. POST /parse         (file_id)    -> texto/markdown (OCR)
    4. POST /extract       (file_id)    -> campos estructurados

El mismo file_id se reutiliza para clasificar, extraer y/o parsear sin
reenviar los bytes.
"""
import asyncio
import logging
from typing import Optional, Tuple

import httpx
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings
from app.services.errores import ErrorDeProveedor

logger = logging.getLogger(__name__)

EXTEND_BASE_URL = "https://api.extend.ai"
EXTEND_API_VERSION = "2026-02-09"

_RED_REINTENTABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

# Scores de confianza que Extend reporta por campo en output.metadata de /extract,
# en orden de preferencia: se usa el primero no nulo. Todos vienen en rango 0..1 y
# cualquiera puede faltar o ser null.
_SCORES_CONFIANZA = ("ocrConfidence", "reviewAgentScore", "logprobsConfidence")


class _FalloTransitorio(Exception):
    """
    Señal interna: un fallo de Extend que justifica reintentar (red caída, HTTP
    429 o 5xx). Nunca se propaga fuera del cliente; si se agotan los intentos,
    `_llamar` lo traduce a ErrorDeProveedor (HTTP 502 al consumidor).
    """

    def __init__(self, detalle: str, retry_after: Optional[float] = None):
        super().__init__(detalle)
        self.retry_after = retry_after


def _leer_retry_after(respuesta: httpx.Response) -> Optional[float]:
    """Segundos del header Retry-After (formato numérico). Si viene como fecha
    HTTP o no viene, devuelve None y se usa el backoff exponencial."""
    bruto = respuesta.headers.get("Retry-After")
    if not bruto:
        return None
    try:
        return max(0.0, float(bruto))
    except ValueError:
        return None


class ClienteExtend:
    """
    Cliente HTTP async hacia Extend, con el manejo de errores centralizado.

    Los reintentos (MAX_INTENTOS, ESPERA_INICIAL, ESPERA_MAX) están acotados a
    propósito: el consumidor llama de forma síncrona, así que el peor caso de
    latencia añadida es (MAX_INTENTOS - 1) esperas con backoff exponencial y
    jitter, con tope ESPERA_MAX por espera (que también acota el header
    Retry-After).
    """

    MAX_INTENTOS = 3
    ESPERA_INICIAL = 0.5
    ESPERA_MAX = 8.0
    _dormir = staticmethod(asyncio.sleep)

    def __init__(self):
        self._cliente: Optional[httpx.AsyncClient] = None

    def _obtener_cliente(self) -> httpx.AsyncClient:
        """Crea el cliente HTTP async solo cuando se necesita (no al importar)."""
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                base_url=EXTEND_BASE_URL,
                headers={
                    "Authorization": f"Bearer {settings.EXTEND_API_KEY}",
                    "x-extend-api-version": EXTEND_API_VERSION,
                },
                timeout=httpx.Timeout(300.0, connect=10.0),
            )
        return self._cliente

    async def _intento(self, metodo: str, ruta: str, **kwargs) -> dict:
        """
        Un intento de petición a Extend. Devuelve el JSON si todo fue bien.
        Distingue lo transitorio de lo definitivo:
          - red reintentable / HTTP 429 / HTTP 5xx -> _FalloTransitorio (se reintenta).
          - red no reintentable (p.ej. ReadTimeout) / HTTP 4xx -> ErrorDeProveedor
            (no se reintenta; reintentar repetiría el mismo fallo o agotaría el
            tiempo del consumidor).
        """
        cliente = self._obtener_cliente()
        try:
            respuesta = await cliente.request(metodo, ruta, **kwargs)
        except _RED_REINTENTABLE as e:
            raise _FalloTransitorio(f"fallo de red ({type(e).__name__})") from e
        except httpx.HTTPError as e:
            logger.error("Fallo de red llamando a Extend %s %s: %s", metodo, ruta, e)
            raise ErrorDeProveedor("No se pudo conectar con el servicio de Extend.") from e

        if respuesta.status_code == 429 or respuesta.status_code >= 500:
            logger.warning(
                "Extend %s %s -> HTTP %s (transitorio, se reintentará)",
                metodo, ruta, respuesta.status_code,
            )
            raise _FalloTransitorio(
                f"HTTP {respuesta.status_code}", retry_after=_leer_retry_after(respuesta)
            )
        if respuesta.status_code >= 400:
            logger.error(
                "Extend %s %s -> HTTP %s: %s",
                metodo, ruta, respuesta.status_code, respuesta.text[:1000],
            )
            raise ErrorDeProveedor(
                f"Extend respondió con un error (HTTP {respuesta.status_code})."
            )
        return respuesta.json()

    def _espera(self, retry_state) -> float:
        """Cuánto esperar antes del próximo intento: respeta Retry-After si el
        429/5xx lo trae (acotado a ESPERA_MAX); si no, backoff exponencial con jitter."""
        exc = retry_state.outcome.exception()
        if isinstance(exc, _FalloTransitorio) and exc.retry_after is not None:
            return min(exc.retry_after, self.ESPERA_MAX)
        return wait_exponential_jitter(
            initial=self.ESPERA_INICIAL, max=self.ESPERA_MAX
        )(retry_state)

    async def _llamar(self, metodo: str, ruta: str, **kwargs) -> dict:
        """
        Hace una petición a Extend con reintentos de fallos transitorios
        (red/429/5xx) y devuelve el JSON. Traduce cualquier fallo definitivo —o
        el agotamiento de reintentos— a ErrorDeProveedor. El detalle va al log;
        al consumidor nunca le llega el cuerpo crudo de Extend.
        """
        try:
            async for intento in AsyncRetrying(
                retry=retry_if_exception_type(_FalloTransitorio),
                stop=stop_after_attempt(self.MAX_INTENTOS),
                wait=self._espera,
                sleep=self._dormir,
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            ):
                with intento:
                    return await self._intento(metodo, ruta, **kwargs)
        except _FalloTransitorio as e:
            logger.error(
                "Extend %s %s agotó %s intento(s): %s",
                metodo, ruta, self.MAX_INTENTOS, e,
            )
            raise ErrorDeProveedor("No se pudo conectar con el servicio de Extend.") from e
        raise ErrorDeProveedor("No se pudo conectar con el servicio de Extend.")

    async def subir_archivo(self, contenido: bytes, mime_type: str, nombre: str) -> str:
        """Sube el archivo a Extend y devuelve su file_id (reutilizable)."""
        datos = await self._llamar(
            "POST", "/files/upload",
            files={"file": (nombre or "documento", contenido, mime_type)},
        )
        archivo = datos.get("file", datos)
        file_id = archivo.get("id")
        if not file_id:
            logger.error("Respuesta de subida sin id de archivo: %s", str(datos)[:500])
            raise ErrorDeProveedor("Extend no devolvió un identificador de archivo.")
        return file_id

    async def clasificar(self, file_id: str, fragmento: dict) -> Tuple[str, float]:
        """
        POST /classify con el fragmento de configuración (classifier publicado o
        classifications inline). Devuelve (clase_detectada, confianza).
        """
        cuerpo = {"file": {"id": file_id}, **fragmento}
        datos = await self._llamar("POST", "/classify", json=cuerpo)

        salida = datos.get("output")
        if datos.get("status") != "PROCESSED" or not salida:
            logger.error(
                "Clasificación no completada (status=%s, failureReason=%s)",
                datos.get("status"), datos.get("failureReason"),
            )
            raise ErrorDeProveedor("La clasificación del documento no se completó.")

        clase = str(salida.get("type", "DOCUMENTO_DESCONOCIDO")).upper()
        confianza = float(salida.get("confidence") or 0.0)
        return clase, confianza

    async def parsear(self, file_id: str, fragmento: dict) -> str:
        """POST /parse (OCR): devuelve todo el texto del documento (markdown)."""
        cuerpo = {"file": {"id": file_id}, **fragmento}
        datos = await self._llamar("POST", "/parse", json=cuerpo)
        if datos.get("status") != "PROCESSED":
            logger.error(
                "Parse no completado (status=%s, failureReason=%s)",
                datos.get("status"), datos.get("failureReason"),
            )
            raise ErrorDeProveedor("El OCR del documento no se completó.")
        chunks = (datos.get("output") or {}).get("chunks") or []
        return "\n".join(chunk.get("content", "") for chunk in chunks)

    @staticmethod
    def _confianzas_por_campo(metadata: Optional[dict]) -> dict:
        """
        Aplana el output.metadata de /extract a {campo: confianza 0..1}. Conserva
        la notación de Extend para campos anidados ('monto.amount', ...) para que
        cada confianza quede alineada con su campo de output.value. Por cada campo
        toma el primer score no nulo de _SCORES_CONFIANZA; None si Extend no
        reporta ninguno.
        """
        salida = {}
        for clave, meta in (metadata or {}).items():
            if not isinstance(meta, dict):
                continue
            confianza = None
            for score in _SCORES_CONFIANZA:
                valor = meta.get(score)
                if valor is not None:
                    confianza = float(valor)
                    break
            salida[clave] = confianza
        return salida

    async def extraer(self, file_id: str, fragmento: dict) -> Tuple[dict, dict]:
        """
        Extracción estructurada. El endpoint depende del fragmento:
          - Extractor PUBLICADO ({"processorId": "ex_...", ["version"]}): se ejecuta
            con POST /processor_runs (sync), el ÚNICO endpoint que respeta el
            esquema publicado del procesador. POST /extract es zero-shot e ignora
            cualquier referencia a procesador (autoextrae un esquema en inglés).
          - Esquema INLINE ({"config": {"schema": ...}}): POST /extract con ese
            schema, que sí se respeta.
        Devuelve (valores, confianzas):
          - valores: output.value (los campos extraídos).
          - confianzas: {campo: confianza 0..1} desde output.metadata, con la
            misma notación de Extend para anidados (p.ej. 'monto.amount'); la
            confianza es None cuando Extend no la reporta para ese campo.
        """
        if fragmento.get("processorId"):
            # processor_runs usa file.fileId (camelCase) y devuelve la corrida
            # envuelta en 'processorRun'; sync=true para respuesta inmediata.
            cuerpo = {"file": {"fileId": file_id}, "sync": True, **fragmento}
            datos = await self._llamar("POST", "/processor_runs", json=cuerpo)
            corrida = datos.get("processorRun") or {}
            if corrida.get("status") != "PROCESSED":
                logger.error(
                    "Extract (processor_run) no completado (status=%s, failureReason=%s)",
                    corrida.get("status"), corrida.get("failureReason"),
                )
                raise ErrorDeProveedor("La extracción de campos no se completó.")
            output = corrida.get("output") or {}
        else:
            cuerpo = {"file": {"id": file_id}, **fragmento}
            datos = await self._llamar("POST", "/extract", json=cuerpo)
            if datos.get("status") != "PROCESSED":
                logger.error(
                    "Extract no completado (status=%s, failureReason=%s)",
                    datos.get("status"), datos.get("failureReason"),
                )
                raise ErrorDeProveedor("La extracción de campos no se completó.")
            output = datos.get("output") or {}
        return output.get("value") or {}, self._confianzas_por_campo(output.get("metadata"))

    async def listar_procesadores(self, tipo_extend: str) -> list:
        """
        GET /processors?type=... siguiendo la paginación. Devuelve la lista
        cruda de procesadores publicados en Extend Studio. La lista puede venir
        bajo la clave 'processors' o 'data'.
        """
        salida: list = []
        token: Optional[str] = None
        for _ in range(50):
            params = {"type": tipo_extend, "maxPageSize": 100}
            if token:
                params["nextPageToken"] = token
            datos = await self._llamar("GET", "/processors", params=params)
            salida.extend(datos.get("processors") or datos.get("data") or [])
            token = datos.get("nextPageToken")
            if not token:
                break
        return salida

    async def obtener_version_procesador(self, procesador_id: str, version_id: str) -> dict:
        """GET /processors/{id}/versions/{versionId}: la versión con su config."""
        datos = await self._llamar(
            "GET", f"/processors/{procesador_id}/versions/{version_id}"
        )
        return datos.get("version") or datos

    async def actualizar_procesador(self, procesador_id: str, config: dict) -> dict:
        """
        POST /processors/{id}: actualiza la configuración del procesador en
        Extend. OJO: modifica su versión BORRADOR (draft); publicarla como
        versión nueva es un paso aparte en Extend Studio.
        """
        return await self._llamar(
            "POST", f"/processors/{procesador_id}", json={"config": config}
        )

    async def publicar_procesador(self, procesador_id: str,
                                  release_type: str = "minor",
                                  descripcion: Optional[str] = None) -> dict:
        """
        POST /processors/{id}/publish: publica el borrador actual como versión
        nueva (snapshot numerado según releaseType 'minor'/'major'). Las rutas
        que ejecutan 'latest' pasan a usarla de inmediato.
        """
        cuerpo: dict = {"releaseType": release_type}
        if descripcion:
            cuerpo["description"] = descripcion
        return await self._llamar(
            "POST", f"/processors/{procesador_id}/publish", json=cuerpo
        )


extend = ClienteExtend()
