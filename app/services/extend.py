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
import logging
from typing import Optional, Tuple

import httpx

from app.core.config import settings
from app.services.errores import ErrorDeProveedor

logger = logging.getLogger(__name__)

EXTEND_BASE_URL = "https://api.extend.ai"
EXTEND_API_VERSION = "2026-02-09"


class ClienteExtend:
    """Cliente HTTP async hacia Extend, con el manejo de errores centralizado."""

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

    async def _llamar(self, metodo: str, ruta: str, **kwargs) -> dict:
        """
        Hace una petición a Extend y devuelve el JSON, traduciendo cualquier
        fallo (red o HTTP >= 400) a ErrorDeProveedor. El detalle va al log; al
        consumidor nunca le llega el cuerpo crudo de Extend.
        """
        cliente = self._obtener_cliente()
        try:
            respuesta = await cliente.request(metodo, ruta, **kwargs)
        except httpx.HTTPError as e:
            logger.error("Fallo de red llamando a Extend %s %s: %s", metodo, ruta, e)
            raise ErrorDeProveedor("No se pudo conectar con el servicio de Extend.") from e

        if respuesta.status_code >= 400:
            logger.error(
                "Extend %s %s -> HTTP %s: %s",
                metodo, ruta, respuesta.status_code, respuesta.text[:1000],
            )
            raise ErrorDeProveedor(
                f"Extend respondió con un error (HTTP {respuesta.status_code})."
            )
        return respuesta.json()

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

    async def extraer(self, file_id: str, fragmento: dict) -> dict:
        """
        POST /extract con el fragmento de configuración (processor publicado o
        schema inline). Devuelve output.value (los campos extraídos).
        """
        cuerpo = {"file": {"id": file_id}, **fragmento}
        datos = await self._llamar("POST", "/extract", json=cuerpo)
        if datos.get("status") != "PROCESSED":
            logger.error(
                "Extract no completado (status=%s, failureReason=%s)",
                datos.get("status"), datos.get("failureReason"),
            )
            raise ErrorDeProveedor("La extracción de campos no se completó.")
        return (datos.get("output") or {}).get("value") or {}

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
