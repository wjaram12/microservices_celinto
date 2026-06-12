"""
View de inferencia: clasificación, OCR y validación de identidad.

Solo capa HTTP: lee la petición, llama al servicio ServicioDocumentos y traduce
el resultado (o los errores) a códigos HTTP. Sin lógica de negocio.
"""
import logging
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.schemas.clasificador import RespuestaClasificacion
from app.schemas.ocr import RespuestaOCR
from app.schemas.senescyt import RespuestaRegistroSenescyt
from app.schemas.validador import RespuestaValidacion
from app.services import documentos as srv
from app.services.documentos import documentos
from app.services.errores import ErrorDeArchivo, ErrorDeProveedor, ErrorDeValidacion

logger = logging.getLogger(__name__)

api = APIRouter()


async def leer_archivo(file: UploadFile) -> bytes:
    """
    Lee el archivo subido, rechazando ANTES los que exceden el tamaño máximo.

    El chequeo va antes de `file.read()` porque read() carga el archivo
    completo en memoria: sin este filtro, un archivo de 2 GB consumiría
    2 GB de RAM solo para luego ser rechazado por tamaño.
    """
    if file.size is not None and file.size > srv.MAX_BYTES:
        limite_mb = srv.MAX_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"El archivo excede el tamaño máximo permitido ({limite_mb:.0f} MB).",
        )
    try:
        return await file.read()
    except Exception:
        logger.exception("No se pudo leer el archivo subido")
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo subido.")


@api.post("/clasificar/", response_model=RespuestaClasificacion, tags=["Clasificadores"])
async def clasificar_documento(file: UploadFile = File(...)):
    """Recibe un archivo y devuelve su clasificación (clase, confianza, validez)."""
    contenido = await leer_archivo(file)

    try:
        resultado = await documentos.clasificar(contenido, file.content_type, file.filename or "")
    except ErrorDeArchivo as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeProveedor as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error procesando el documento en /clasificar/")
        raise HTTPException(status_code=500, detail="Error interno al procesar el documento.")

    if resultado["es_valido"]:
        mensaje = "Documento procesado y clasificado con éxito."
    elif resultado["clase_detectada"] in srv.CLASES_RECHAZADAS:
        mensaje = "El documento no corresponde a ninguno de los tipos aceptados."
    else:
        mensaje = "La confianza del modelo es insuficiente para dar por válido este documento."

    return RespuestaClasificacion(
        result=resultado["es_valido"],
        message=mensaje,
        document_class=resultado["clase_detectada"],
        confidence=resultado["confianza"],
    )


@api.post("/ocr/", response_model=RespuestaOCR, tags=["OCR"])
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
        resultado = await documentos.ocr(contenido, file.content_type, texto_a_buscar, file.filename or "")
    except ErrorDeArchivo as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeProveedor as e:
        raise HTTPException(status_code=502, detail=str(e))
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


@api.post("/validaciones/validar-identidad/", response_model=RespuestaValidacion, tags=["Validadores"])
async def validar_documento(
    file: UploadFile = File(...),
    cedula_sistema: Optional[str] = Form(
        None,
        description=(
            "Opcional. Número de identificación del sistema: cédula (10 dígitos) "
            "o pasaporte (alfanumérico). Si se envía, se compara contra el número "
            "extraído del documento según su clase (cédula o pasaporte). Si se "
            "omite, solo se valida el documento y se extraen sus datos."
        ),
    ),
):
    contenido = await leer_archivo(file)

    try:
        resultado = await documentos.validar(contenido, file.content_type, cedula_sistema, file.filename or "")
    except (ErrorDeArchivo, ErrorDeValidacion) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeProveedor as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error procesando el documento en /validaciones/validar-identidad/")
        raise HTTPException(status_code=500, detail="Error interno al procesar el documento.")

    clase = resultado["clase_detectada"]
    if not resultado["es_identidad"]:
        mensaje = "El documento no fue reconocido como cédula ni pasaporte."
    elif resultado["identificacion_sistema"] is None:
        mensaje = f"Documento reconocido como {clase}; datos extraídos."
    elif resultado["identificacion_documento"] is None:
        mensaje = "No se pudo extraer un número de identificación del documento."
    elif resultado["coincide"]:
        mensaje = "La identificación del sistema coincide con la del documento."
    else:
        mensaje = "La identificación del sistema NO coincide con la del documento."

    return RespuestaValidacion(
        result=resultado["es_identidad"],
        message=mensaje,
        match_document=resultado["coincide"],
        document_class=resultado["clase_detectada"],
        confidence=resultado["confianza"],
        ocr=resultado["ocr"],
        datos=resultado["datos"],
    )


@api.post("/validaciones/validar-registro-senescyt/", response_model=RespuestaRegistroSenescyt, tags=["Validadores"])
async def validar_registro_senescyt(
    file: UploadFile = File(...),
    numero_identificacion: Optional[str] = Form(
        None,
        description=(
            "Opcional. Número de identificación (cédula o pasaporte) del titular "
            "según el sistema. Si se envía, se compara con el extraído del documento "
            "ignorando espacios y caracteres especiales."
        ),
    ),
    nombres: Optional[str] = Form(
        None,
        description=(
            "Opcional. Nombres y apellidos del titular según el sistema. Si se "
            "envían, se comparan con los extraídos sin distinguir mayúsculas, tildes "
            "ni el orden de los nombres."
        ),
    ),
):
    """Valida que el documento sea un registro de título de la SENESCYT y, si se
    envían `numero_identificacion` y/o `nombres`, contrasta la identidad con la
    extraída, devolviendo la información del extractor personalizado."""
    contenido = await leer_archivo(file)

    try:
        resultado = await documentos.validar_registro_senescyt(
            contenido, file.content_type, numero_identificacion, nombres, file.filename or "")
    except (ErrorDeArchivo, ErrorDeValidacion) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ErrorDeProveedor as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Error procesando el documento en /validaciones/validar-registro-senescyt/")
        raise HTTPException(status_code=500, detail="Error interno al procesar el documento.")

    if not resultado["es_senescyt"]:
        mensaje = "El documento no fue reconocido como un registro de título de la SENESCYT."
    elif not resultado["datos"]:
        mensaje = ("El documento es un registro SENESCYT, pero no se pudo extraer la "
                   "información; falló el extractor o el documento no tiene suficiente "
                   "claridad.")
    elif resultado["match_document"] is None:
        mensaje = "Registro SENESCYT reconocido; información extraída."
    elif resultado["match_document"]:
        mensaje = "Registro SENESCYT reconocido; la identidad coincide con la del documento."
    else:
        difieren = []
        if resultado["coincide_identificacion"] is False:
            difieren.append("el número de identificación")
        if resultado["coincide_nombres"] is False:
            difieren.append("los nombres")
        mensaje = ("Es un registro SENESCYT, pero " + " y ".join(difieren) +
                   " no coincide(n) con los datos proporcionados.")

    return RespuestaRegistroSenescyt(
        result=resultado["es_senescyt"],
        message=mensaje,
        match_document=resultado["match_document"],
        document_class=resultado["clase_detectada"],
        confidence=resultado["confianza"],
        datos=resultado["datos"],
    )
