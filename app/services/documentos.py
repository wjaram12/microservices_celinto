"""
Servicio ServicioDocumentos: la lógica de negocio de la inferencia.

Orquesta el flujo completo de cada ruta — preprocesar, subir a Extend,
clasificar, extraer campos y/o parsear (OCR) — usando:

    ClienteExtend         (services/extend.py)        las llamadas HTTP
    ServicioProcesadores  (services/procesadores.py)  qué procesador/config usa cada ruta
    ServicioPrompts       (services/prompts.py)       las clasificaciones inline

Orden de lectura: constantes -> preprocesamiento -> búsqueda de texto ->
helpers de cédula -> la clase con las operaciones de alto nivel.
"""
import re
import unicodedata
from typing import Optional, Tuple

from app.services.errores import ErrorDeArchivo, ErrorDeValidacion
from app.services.extend import extend
from app.services.procesadores import OTRO_POR_DEFECTO, procesadores
from app.services.prompts import prompts

LONGITUD_CEDULA = 10
CLASE_CEDULA = "CEDULA"
CLASE_PASAPORTE = "PASAPORTE"
CLASE_SENESCYT = "REGISTRO_SENESCYT"
TIPOS_IDENTIDAD = {CLASE_CEDULA, CLASE_PASAPORTE}
FORMATOS_ACEPTADOS = {"application/pdf", "image/jpeg", "image/png"}
MAX_BYTES = 10 * 1024 * 1024
PATRON_CEDULA = re.compile(r"\b\d{10}\b")

RUTA_CLASIFICAR = "clasificar"
RUTA_VALIDAR = "validar-identidad"
RUTA_OCR = "ocr"
RUTA_SENESCYT = "validar-registro-senescyt"

CLASES_RECHAZADAS = {"OTROS", "OTHER", "DESCONOCIDO", "DOCUMENTO_DESCONOCIDO"}

TIPOS_DESCARTE = {"other", "otros"}


def validar_formato(contenido: bytes, mime_type: str) -> None:
    if mime_type not in FORMATOS_ACEPTADOS:
        raise ErrorDeArchivo(
            f"Formato '{mime_type}' no admitido. Debe ser PDF, JPEG o PNG."
        )


def validar_tamano(contenido: bytes, mime_type: str) -> None:
    tamano = len(contenido)
    if tamano == 0:
        raise ErrorDeArchivo("El archivo está vacío.")
    if tamano > MAX_BYTES:
        limite_mb = MAX_BYTES / (1024 * 1024)
        raise ErrorDeArchivo(
            f"El archivo excede el tamaño máximo permitido ({limite_mb:.0f} MB)."
        )


PREPROCESADORES = [validar_formato, validar_tamano]


def preprocesar(contenido: bytes, mime_type: str) -> None:
    for paso in PREPROCESADORES:
        paso(contenido, mime_type)


def _normalizar_busqueda(texto: str) -> str:
    """Minúsculas y sin tildes, 1 carácter de salida por carácter de entrada
    (mantiene alineadas las posiciones con el texto original)."""
    salida = []
    for ch in texto:
        bajo = ch.lower()
        bajo = bajo[0] if bajo else ch
        descompuesto = unicodedata.normalize("NFKD", bajo)
        base = "".join(c for c in descompuesto if not unicodedata.combining(c))
        salida.append(base[0] if base else bajo)
    return "".join(salida)


def buscar_en_texto(texto: str, termino: str, margen: int = 40) -> dict:
    """Busca todas las apariciones de `termino` en `texto`, ignorando
    mayúsculas y tildes; devuelve contexto de cada coincidencia."""
    termino = (termino or "").strip()
    if not termino:
        return {"termino": termino, "encontrado": False, "cantidad": 0, "coincidencias": []}

    texto_norm = _normalizar_busqueda(texto)
    termino_norm = _normalizar_busqueda(termino)

    coincidencias = []
    inicio = 0
    while True:
        pos = texto_norm.find(termino_norm, inicio)
        if pos == -1:
            break
        desde = max(0, pos - margen)
        hasta = min(len(texto), pos + len(termino_norm) + margen)
        coincidencias.append({"posicion": pos, "contexto": " ".join(texto[desde:hasta].split())})
        inicio = pos + len(termino_norm)

    return {
        "termino": termino,
        "encontrado": bool(coincidencias),
        "cantidad": len(coincidencias),
        "coincidencias": coincidencias,
    }


def normalizar_cedula(valor) -> str:
    """
    Normaliza un número de identificación a solo dígitos: quita espacios,
    guiones, puntos, etc. Tolera que el extractor devuelva el valor como
    número (int/float) en vez de texto.
    """
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return re.sub(r"\D", "", str(valor))


def normalizar_identificacion(valor) -> str:
    """
    Normaliza una identificación a MAYÚSCULAS y solo letras/dígitos (los
    números de pasaporte son alfanuméricos). Tolera valores numéricos.
    """
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return re.sub(r"[^A-Z0-9]", "", str(valor).upper())


_CLAVES_NUMERO = (
    "numero_cedula", "numero_identificacion", "numero_documento",
    "cedula", "identificacion", "numero",
)
_CLAVES_PASAPORTE = (
    "numero_pasaporte", "pasaporte", "numero_documento",
    "numero_identificacion", "numero",
)


def valor_en_datos(datos: dict, claves: tuple):
    """Primer valor presente en los campos extraídos bajo alguna de las claves."""
    for clave in claves:
        if (datos or {}).get(clave) is not None:
            return datos[clave]
    return None


def numero_en_datos(datos: dict) -> str:
    """Número de cédula entre los campos extraídos, normalizado a solo dígitos
    ('' si no aparece bajo ninguna clave conocida)."""
    return normalizar_cedula(valor_en_datos(datos, _CLAVES_NUMERO))


# Claves bajo las que puede venir el número de registro de un título SENESCYT.
# Es el dato que DEFINE a un registro; sin él, no se da por válido.
_CLAVES_REGISTRO = (
    "numero_registro", "num_registro", "registro",
    "numero_registro_senescyt", "numero_acta",
)


def registro_senescyt_en_datos(datos: dict) -> str:
    """Número de registro SENESCYT entre los campos extraídos ('' si no aparece
    bajo ninguna clave conocida)."""
    valor = valor_en_datos(datos, _CLAVES_REGISTRO)
    return str(valor or "").strip()


def cedula_es_valida(numero: str) -> bool:
    """Verifica una cédula ecuatoriana con el dígito verificador (módulo 10)."""
    if len(numero) != LONGITUD_CEDULA or not numero.isdigit():
        return False
    provincia = int(numero[:2])
    if provincia not in range(1, 25) and provincia != 30:
        return False
    if int(numero[2]) > 5:
        return False
    coeficientes = (2, 1, 2, 1, 2, 1, 2, 1, 2)
    suma = 0
    for digito, coeficiente in zip(numero[:9], coeficientes):
        producto = int(digito) * coeficiente
        if producto > 9:
            producto -= 9
        suma += producto
    verificador_esperado = (10 - suma % 10) % 10
    return int(numero[9]) == verificador_esperado


class ServicioDocumentos:
    """Flujos completos de clasificación, OCR y validación de identidad."""

    @staticmethod
    def construir_clasificaciones() -> list:
        """
        Arma la lista de clasificaciones para /classify desde las clasificaciones
        activas en la base. Garantiza una clase de descarte 'other' (Extend la
        exige) y que los id sean únicos.
        """
        activos = prompts.listar_activas()
        descarte = [p for p in activos if p["tipo"].lower() in TIPOS_DESCARTE]
        positivas = [p for p in activos if p["tipo"].lower() not in TIPOS_DESCARTE]

        if not positivas:
            raise ErrorDeValidacion(
                "No hay clasificaciones activas en la base. Registra al menos una "
                "en /api/v1/prompts/ (o desde /admin)."
            )

        clasificaciones = [
            {"id": p["clave"], "type": p["tipo"], "description": p["descripcion"]}
            for p in positivas
        ]
        clasificaciones.extend(
            {"id": p["clave"], "type": "other", "description": p["descripcion"]}
            for p in descarte
        )
        if not descarte:
            clasificaciones.append(OTRO_POR_DEFECTO)
        return clasificaciones

    async def _clasificar_archivo(self, file_id: str, ruta: str) -> Tuple[str, float]:
        """Clasifica un archivo ya subido con el procesador configurado para la ruta."""
        fragmento = procesadores.cuerpo_clasificacion(ruta, self.construir_clasificaciones)
        return await extend.clasificar(file_id, fragmento)

    async def _extraer_datos(self, ruta: str, clase: str, file_id: str) -> dict:
        """
        Extracción estructurada según la clase. Devuelve {} para clases sin
        procesador ni esquema (no es un documento de identidad reconocido en
        esa ruta).
        """
        fragmento = procesadores.cuerpo_extraccion(ruta, clase)
        if fragmento is None:
            return {}
        return await extend.extraer(file_id, fragmento)

    async def _extraer_texto(self, file_id: str, ruta: str) -> str:
        """OCR con la configuración de parse de la ruta."""
        return await extend.parsear(file_id, procesadores.cuerpo_parse(ruta))

    async def clasificar(self, contenido: bytes, mime_type: str, nombre: str = "") -> dict:
        """Preprocesa, clasifica y decide si es válido según el umbral de confianza."""
        preprocesar(contenido, mime_type)
        file_id = await extend.subir_archivo(contenido, mime_type, nombre)
        clase, confianza = await self._clasificar_archivo(file_id, RUTA_CLASIFICAR)
        umbral = procesadores.umbral_clasificacion(RUTA_CLASIFICAR)
        es_valido = clase not in CLASES_RECHAZADAS and confianza >= umbral
        return {
            "clase_detectada": clase,
            "confianza": confianza,
            "es_valido": es_valido,
        }

    async def ocr(
        self,
        contenido: bytes,
        mime_type: str,
        texto_a_buscar: Optional[str] = None,
        nombre: str = "",
    ) -> dict:
        """Extrae el texto del documento (OCR) y, si se envía `texto_a_buscar`,
        indica si aparece y en qué contexto."""
        preprocesar(contenido, mime_type)
        file_id = await extend.subir_archivo(contenido, mime_type, nombre)
        texto = await self._extraer_texto(file_id, RUTA_OCR)

        busqueda = None
        if texto_a_buscar and texto_a_buscar.strip():
            busqueda = buscar_en_texto(texto, texto_a_buscar)

        return {"texto_completo": texto, "busqueda": busqueda}

    async def validar(
        self,
        contenido: bytes,
        mime_type: str,
        cedula_sistema: Optional[str] = None,
        nombre: str = "",
    ) -> dict:
        """
        Clasifica el documento, extrae sus campos (cédula o pasaporte) y, si se
        envía `cedula_sistema`, compara el número contra el extraído SEGÚN LA
        CLASE detectada: cédula (solo dígitos + verificador) o pasaporte
        (alfanumérico en mayúsculas).

        Esta ruta usa SOLO dos procesadores: el clasificador y el extractor.
        No usa OCR/parse: el número del documento sale de la extracción
        estructurada, no del texto. (El campo `ocr` de la respuesta quedó
        deprecado y siempre es null.)
        """
        id_sistema = None
        if cedula_sistema and cedula_sistema.strip():
            id_sistema = normalizar_identificacion(cedula_sistema)
            if id_sistema.isdigit():
                if len(id_sistema) != LONGITUD_CEDULA:
                    raise ErrorDeValidacion(
                        f"La cédula del sistema debe tener {LONGITUD_CEDULA} dígitos; "
                        f"se recibió '{cedula_sistema}' ({len(id_sistema)} dígitos). "
                        "¿Se perdió un cero a la izquierda?"
                    )
                if not cedula_es_valida(id_sistema):
                    raise ErrorDeValidacion(
                        f"La cédula del sistema '{cedula_sistema}' no es un número de "
                        "cédula ecuatoriana válido (falla el dígito verificador)."
                    )
            elif len(id_sistema) < 5:
                raise ErrorDeValidacion(
                    f"La identificación del sistema '{cedula_sistema}' es demasiado "
                    "corta para ser una cédula o un número de pasaporte."
                )

        preprocesar(contenido, mime_type)
        file_id = await extend.subir_archivo(contenido, mime_type, nombre)
        clase, confianza = await self._clasificar_archivo(file_id, RUTA_VALIDAR)

        umbral = procesadores.umbral_clasificacion(RUTA_VALIDAR)
        es_cedula = clase == CLASE_CEDULA and confianza >= umbral
        es_identidad = clase in TIPOS_IDENTIDAD and confianza >= umbral

        datos = {}
        if es_identidad:
            datos = await self._extraer_datos(RUTA_VALIDAR, clase, file_id)

        resultado = {
            "clase_detectada": clase,
            "confianza": confianza,
            "es_cedula": es_cedula,
            "es_identidad": es_identidad,
            "datos": datos,
            "identificacion_sistema": None,
            "identificacion_documento": None,
            "coincide": None,
            "ocr": None,
        }

        if id_sistema is not None:
            resultado["identificacion_sistema"] = cedula_sistema

            if es_identidad:
                if clase == CLASE_CEDULA:
                    id_documento = numero_en_datos(datos)
                    id_documento = id_documento if cedula_es_valida(id_documento) else None
                else:
                    id_documento = normalizar_identificacion(
                        valor_en_datos(datos, _CLAVES_PASAPORTE)
                    ) or None
                resultado["identificacion_documento"] = id_documento
                resultado["coincide"] = bool(id_documento) and id_sistema == id_documento
            else:
                resultado["coincide"] = False

        return resultado

    async def validar_registro_senescyt(
        self,
        contenido: bytes,
        mime_type: str,
        nombre: str = "",
    ) -> dict:
        """
        Valida que el documento sea un registro de título de la SENESCYT en DOS
        pasos: (1) el clasificador lo reconoce como REGISTRO_SENESCYT con
        confianza suficiente, y (2) la extracción trae el número de registro (el
        dato que define a un registro). Solo si ambos se cumplen es válido. Usa
        clasificador + extractor (sin OCR); el extractor se configura por ruta en
        la tabla `procesadores`.
        """
        preprocesar(contenido, mime_type)
        file_id = await extend.subir_archivo(contenido, mime_type, nombre)
        clase, confianza = await self._clasificar_archivo(file_id, RUTA_SENESCYT)
        umbral = procesadores.umbral_clasificacion(RUTA_SENESCYT)

        # Paso 1: el clasificador lo reconoce como registro SENESCYT.
        es_senescyt = clase == CLASE_SENESCYT and confianza >= umbral

        datos = {}
        if es_senescyt:
            datos = await self._extraer_datos(RUTA_SENESCYT, clase, file_id)

        # Paso 2: la extracción confirma el número de registro.
        tiene_registro = bool(registro_senescyt_en_datos(datos))
        es_valido = es_senescyt and tiene_registro

        return {
            "clase_detectada": clase,
            "confianza": confianza,
            "es_senescyt": es_senescyt,
            "es_valido": es_valido,
            "datos": datos,
        }


documentos = ServicioDocumentos()
