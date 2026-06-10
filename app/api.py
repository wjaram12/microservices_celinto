"""
Capa HTTP: solo lee la petición, llama a la lógica de documentos.py y
traduce el resultado (o los errores) a códigos HTTP. Sin lógica de negocio.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException

from app import documentos, prompts, seguridad
from app.documentos import ErrorDeArchivo, ErrorDeValidacion
from app.prompts import ClaseNoEncontrada, ErrorDocumentAI
from app.schemas.api_keys import APIKeyActualizar, APIKeyCreada, APIKeyCrear, APIKeyRespuesta
from app.schemas.clasificador import RespuestaClasificacion
from app.schemas.ocr import RespuestaOCR
from app.schemas.prompts import ActualizarClase, ClasePrompt
from app.schemas.validador import RespuestaValidacion
from app.seguridad import requiere_admin

logger = logging.getLogger(__name__)

api_router = APIRouter()


async def leer_archivo(file: UploadFile) -> bytes:
    """
    Lee el archivo subido, rechazando ANTES los que exceden el tamaño máximo.

    El chequeo va antes de `file.read()` porque read() carga el archivo
    completo en memoria: sin este filtro, un archivo de 2 GB consumiría
    2 GB de RAM solo para luego ser rechazado por tamaño.
    """
    if file.size is not None and file.size > documentos.MAX_BYTES:
        limite_mb = documentos.MAX_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"El archivo excede el tamaño máximo permitido ({limite_mb:.0f} MB).",
        )
    try:
        return await file.read()
    except Exception:
        # Ej.: el cliente cortó la conexión a mitad de la subida.
        logger.exception("No se pudo leer el archivo subido")
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo subido.")


@api_router.post("/clasificar/", response_model=RespuestaClasificacion, tags=["Clasificadores"])
async def clasificar_documento(file: UploadFile = File(...)):
    """Recibe un archivo y devuelve su clasificación (clase, confianza, validez)."""
    contenido = await leer_archivo(file)

    try:
        resultado = documentos.clasificar(contenido, file.content_type)
    except ErrorDeArchivo as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        # El detalle completo va al log del servidor; al cliente solo le llega
        # un mensaje genérico para no exponer información interna (nombres de
        # recursos de Google, IDs del proyecto, etc.).
        logger.exception("Error procesando el documento en /clasificar/")
        raise HTTPException(status_code=500, detail="Error interno al procesar el documento.")

    # Cuando no es válido hay dos causas distintas y el mensaje debe decir
    # cuál fue: una clase rechazada puede venir con confianza altísima
    # (ej. "OTROS al 99%") y ahí "confianza insuficiente" sería mentira.
    if resultado["es_valido"]:
        mensaje = "Documento procesado y clasificado con éxito."
    elif resultado["clase_detectada"] in documentos.CLASES_RECHAZADAS:
        mensaje = "El documento no corresponde a ninguno de los tipos aceptados."
    else:
        mensaje = "La confianza del modelo es insuficiente para dar por válido este documento."

    return RespuestaClasificacion(
        result=resultado["es_valido"],
        message=mensaje,
        document_class=resultado["clase_detectada"],
        confidence=resultado["confianza"],
    )


@api_router.post("/ocr/", response_model=RespuestaOCR, tags=["OCR"])
async def extraer_texto(
    file: UploadFile = File(...),
    texto_a_buscar: Optional[str] = Form(
        None,
        description=(
            "Opcional. Texto a buscar dentro del documento. Si se envía, la "
            "respuesta indica si aparece, cuántas veces y en qué contexto "
            "(ignorando mayúsculas y tildes). Si se omite, solo se devuelve "
            "el texto completo extraído."
        ),
    ),
):
    """Extrae el texto de un documento por OCR y, opcionalmente, busca un término en él."""
    contenido = await leer_archivo(file)

    try:
        resultado = documentos.ocr(contenido, file.content_type, texto_a_buscar)
    except ErrorDeArchivo as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Error procesando el documento en /ocr/")
        raise HTTPException(status_code=500, detail="Error interno al procesar el documento.")

    contenido_ocr = resultado["texto_completo"]
    busqueda = resultado["busqueda"]
    hay_texto = bool(contenido_ocr)

    if not hay_texto:
        mensaje = "No se pudo extraer texto del documento."
    elif busqueda is None:
        mensaje = f"Texto extraído del documento ({len(contenido_ocr)} caracteres)."
    elif busqueda["encontrado"]:
        mensaje = (
            f"Texto extraído; el término buscado aparece "
            f"{busqueda['cantidad']} vez/veces en el documento."
        )
    else:
        mensaje = "Texto extraído; el término buscado no aparece en el documento."

    return RespuestaOCR(
        result=hay_texto,
        message=mensaje,
        content=contenido_ocr,
    )


@api_router.post("/validaciones/validar-identidad/", response_model=RespuestaValidacion, tags=["Validadores"])
async def validar_documento(
    file: UploadFile = File(...),
    cedula_sistema: Optional[str] = Form(
        None,
        description=(
            "Opcional. Si se envía, se activa el OCR para leer el número de la "
            "cédula en el documento y compararlo. Si se omite, solo se valida "
            "que el documento sea una cédula."
        ),
    ),
):
    contenido = await leer_archivo(file)

    try:
        resultado = documentos.validar(contenido, file.content_type, cedula_sistema)
    except (ErrorDeArchivo, ErrorDeValidacion) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Error procesando el documento en /validaciones/validar-identidad/")
        raise HTTPException(status_code=500, detail="Error interno al procesar el documento.")

    # El mensaje depende del modo en que se usó el servicio.
    if resultado["identificacion_sistema"] is None:
        # Modo simple: solo se validó que sea una cédula.
        mensaje = (
            "El documento fue reconocido como una cédula."
            if resultado["es_cedula"]
            else "El documento no fue reconocido como una cédula válida."
        )
    elif not resultado["es_cedula"]:
        mensaje = (
            "El documento no fue reconocido como una cédula; "
            "no se comparó el número de identificación."
        )
    elif resultado["identificacion_documento"] is None:
        mensaje = "No se pudo extraer un número de identificación del documento."
    elif resultado["coincide"]:
        mensaje = "La identificación del sistema coincide con la del documento."
    else:
        mensaje = "La identificación del sistema NO coincide con la del documento."

    return RespuestaValidacion(
        result=resultado["es_cedula"],
        message=mensaje,
        match_document=resultado["coincide"],
        document_class=resultado["clase_detectada"],
        confidence=resultado["confianza"],
        ocr=resultado["ocr"],
    )


# --- Gestión de prompts del clasificador (SOLO scope 'admin') ---
# Edita las descripciones de clase del procesador de Document AI, que en el
# clasificador foundation-model son el prompt. `requiere_admin` exige una API
# key de administrador: las claves de consumo reciben 403 aquí.

@api_router.get("/prompts/clases/", response_model=List[ClasePrompt], tags=["Prompts (admin)"])
def listar_clases(_admin: dict = Depends(requiere_admin)):
    """Lista las clases del clasificador con su etiqueta y su prompt (descripción)."""
    try:
        return prompts.listar_clases()
    except ErrorDocumentAI as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error listando clases en /prompts/clases/")
        raise HTTPException(status_code=500, detail="Error interno al leer los prompts.")


@api_router.put("/prompts/clases/{name}", response_model=ClasePrompt, tags=["Prompts (admin)"])
def actualizar_clase(name: str, datos: ActualizarClase, _admin: dict = Depends(requiere_admin)):
    """Edita el prompt (description) y/o la etiqueta (display_name) de una clase."""
    if datos.description is None and datos.display_name is None:
        raise HTTPException(
            status_code=400,
            detail="Envía al menos 'description' o 'display_name' para actualizar.",
        )
    try:
        return prompts.actualizar_clase(name, datos.description, datos.display_name)
    except ClaseNoEncontrada:
        raise HTTPException(status_code=404, detail=f"No existe la clase '{name}'.")
    except ErrorDocumentAI as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error actualizando la clase en /prompts/clases/")
        raise HTTPException(status_code=500, detail="Error interno al actualizar el prompt.")


# --- CRUD de API keys (SOLO scope 'admin') ---
# Operación sensible: crear claves (incluso admin) da acceso al servicio. Por
# eso exige scope 'admin'. El CLI `gestionar_llaves.py` se mantiene para crear
# la PRIMERA clave admin cuando aún no existe ninguna (bootstrap).

@api_router.get("/api-keys/", response_model=List[APIKeyRespuesta], tags=["API Keys (admin)"])
def listar_api_keys(_admin: dict = Depends(requiere_admin)):
    """Lista todas las API keys (metadatos; nunca la clave ni el hash)."""
    return seguridad.listar_llaves()


@api_router.get("/api-keys/{id_llave}", response_model=APIKeyRespuesta, tags=["API Keys (admin)"])
def obtener_api_key(id_llave: int, _admin: dict = Depends(requiere_admin)):
    llave = seguridad.obtener_llave(id_llave)
    if llave is None:
        raise HTTPException(status_code=404, detail=f"No existe una API key con id {id_llave}.")
    return llave


@api_router.post("/api-keys/", response_model=APIKeyCreada, status_code=201, tags=["API Keys (admin)"])
def crear_api_key(datos: APIKeyCrear, _admin: dict = Depends(requiere_admin)):
    """
    Crea una API key nueva. Devuelve la clave EN TEXTO PLANO (`llave`) una sola
    vez: hay que entregarla al consumidor en ese momento, no se vuelve a tener.
    """
    try:
        return seguridad.crear_llave(datos.consumidor, datos.scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_router.put("/api-keys/{id_llave}", response_model=APIKeyRespuesta, tags=["API Keys (admin)"])
def actualizar_api_key(id_llave: int, datos: APIKeyActualizar, _admin: dict = Depends(requiere_admin)):
    """Actualiza nombre, scope o estado (activo) de una clave. No cambia la clave en sí."""
    if datos.consumidor is None and datos.scope is None and datos.activo is None:
        raise HTTPException(
            status_code=400,
            detail="Envía al menos un campo para actualizar (consumidor, scope o activo).",
        )
    try:
        actualizada = seguridad.actualizar_llave(
            id_llave, datos.consumidor, datos.scope, datos.activo
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if actualizada is None:
        raise HTTPException(status_code=404, detail=f"No existe una API key con id {id_llave}.")
    return actualizada


@api_router.delete("/api-keys/{id_llave}", status_code=204, tags=["API Keys (admin)"])
def revocar_api_key(id_llave: int, _admin: dict = Depends(requiere_admin)):
    """
    Revoca una clave (la desactiva). NO la borra: se conserva la fila para que
    el rastro de auditoría siga siendo válido. Para reactivarla, usar PUT con
    activo=true.
    """
    if seguridad.obtener_llave(id_llave) is None:
        raise HTTPException(status_code=404, detail=f"No existe una API key con id {id_llave}.")
    seguridad.actualizar_llave(id_llave, activo=False)
