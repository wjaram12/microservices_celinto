"""
Gestión de los prompts del clasificador de Document AI.

En el clasificador de modelo fundacional (zero-shot), cada CLASE del esquema
del procesador es un `EntityType` con un `display_name` (la etiqueta que
devuelve el modelo) y una `description` (el PROMPT que guía la clasificación).
Editar esa descripción cambia el comportamiento del clasificador SIN reentrenar.

Aquí se leen y editan esas descripciones con la API de datasets de Document AI
(v1beta3: `get_dataset_schema` / `update_dataset_schema`). Es una superficie de
ADMINISTRACIÓN: los endpoints que la exponen exigen una API key con scope
'admin' (ver app/seguridad.py). El service account que use el servicio debe
tener permiso de edición del dataset para que esto funcione.
"""
import logging

from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import documentai_v1beta3 as docai

from app.core.config import settings

logger = logging.getLogger(__name__)


class ClaseNoEncontrada(Exception):
    """No existe una clase con ese identificador en el esquema -> HTTP 404."""


class ErrorDocumentAI(Exception):
    """Document AI no respondió o respondió con error -> HTTP 502."""


_cliente = None  # se crea una sola vez (lazy)


def obtener_cliente() -> docai.DocumentServiceClient:
    """
    Cliente de datasets de Document AI (lazy). Usa el endpoint REGIONAL: las
    APIs de dataset/esquema están ancladas a la región del procesador
    (p. ej. us-documentai.googleapis.com).
    """
    global _cliente
    if _cliente is None:
        opciones = ClientOptions(
            api_endpoint=f"{settings.GOOGLE_LOCATION}-documentai.googleapis.com"
        )
        _cliente = docai.DocumentServiceClient(client_options=opciones)
    return _cliente


def _ruta_schema() -> str:
    return obtener_cliente().dataset_schema_path(
        settings.GOOGLE_PROJECT_ID,
        settings.GOOGLE_LOCATION,
        settings.GOOGLE_PROCESSOR_ID,
    )


def _a_dict(entity_type) -> dict:
    return {
        "name": entity_type.name,
        "display_name": entity_type.display_name,
        "description": entity_type.description,
    }


def _leer_schema():
    """Lee el DatasetSchema del procesador, traduciendo fallos a ErrorDocumentAI."""
    try:
        return obtener_cliente().get_dataset_schema(name=_ruta_schema())
    except GoogleAPICallError as e:
        logger.error("Document AI get_dataset_schema falló: %s", e)
        raise ErrorDocumentAI("No se pudo leer el esquema del procesador.") from e


def listar_clases() -> list:
    """Devuelve las clases del clasificador con su etiqueta y su prompt."""
    schema = _leer_schema()
    return [_a_dict(et) for et in schema.document_schema.entity_types]


def actualizar_clase(name: str, descripcion=None, display_name=None) -> dict:
    """
    Edita el prompt (`description`) y/o la etiqueta (`display_name`) de una
    clase. Lee el esquema, modifica la clase indicada y lo vuelve a guardar.
    `name` es el identificador interno de la clase (campo `name` del EntityType).
    """
    schema = _leer_schema()

    objetivo = next(
        (et for et in schema.document_schema.entity_types if et.name == name), None
    )
    if objetivo is None:
        raise ClaseNoEncontrada(name)

    if descripcion is not None:
        objetivo.description = descripcion
    if display_name is not None:
        objetivo.display_name = display_name

    try:
        obtener_cliente().update_dataset_schema(dataset_schema=schema)
    except GoogleAPICallError as e:
        logger.error("Document AI update_dataset_schema falló: %s", e)
        raise ErrorDocumentAI("No se pudo actualizar el esquema del procesador.") from e

    return _a_dict(objetivo)
