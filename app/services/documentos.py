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
import asyncio
import re
import unicodedata
from enum import StrEnum
from typing import Optional, Tuple

from app.services.errores import ErrorDeArchivo, ErrorDeValidacion
from app.services.extend import extend
from app.services.procesadores import OTRO_POR_DEFECTO, procesadores
from app.services.prompts import prompts


class ClaseDocumento(StrEnum):
    CEDULA = "CEDULA"
    PASAPORTE = "PASAPORTE"
    SENESCYT = "REGISTRO_SENESCYT"
    CARTA_COMPROMISO = "CARTA_COMPROMISO"
    APOSTILLA = "APOSTILLA"
    DEPOSITO = "DEPOSITO"
    TRANSFERENCIA = "TRANSFERENCIA"


class RutaAPI(StrEnum):
    VALIDAR = "validar-identidad"
    OCR = "ocr"
    SENESCYT = "validar-registro-senescyt"
    PAGO = "validar-pago"


class EstadoValidacion(StrEnum):
    NO_RECONOCIDO = "no_reconocido"
    EXTRACCION_FALLIDA = "extraccion_fallida"
    EXTRAIDO = "extraido"


LONGITUD_CEDULA = 10
PREFIJO_PASAPORTE = "VS-"
TIPOS_IDENTIDAD = frozenset({ClaseDocumento.CEDULA, ClaseDocumento.PASAPORTE})
TIPOS_SENESCYT = frozenset({ClaseDocumento.SENESCYT, ClaseDocumento.CARTA_COMPROMISO, ClaseDocumento.APOSTILLA})
TIPOS_PAGO = frozenset({ClaseDocumento.DEPOSITO, ClaseDocumento.TRANSFERENCIA})
FORMATOS_ACEPTADOS = {"application/pdf", "image/jpeg", "image/png"}
MAX_BYTES = 15 * 1024 * 1024
PATRON_CEDULA = re.compile(r"\b\d{10}\b")
_PATRON_NO_DIGITO = re.compile(r"\D")
_PATRON_NO_ALFANUM = re.compile(r"[^A-Z0-9]")
_PATRON_TOKEN_NOMBRE = re.compile(r"[a-z0-9]+")

TIPOS_DESCARTE = {"other", "otros"}


def estado_validacion(es_clase_esperada: bool, datos: dict) -> EstadoValidacion:
    """Estado estructurado común a validar-identidad y validar-registro-senescyt."""
    if not es_clase_esperada:
        return EstadoValidacion.NO_RECONOCIDO
    if not datos:
        return EstadoValidacion.EXTRACCION_FALLIDA
    return EstadoValidacion.EXTRAIDO


_LECTURAS_CONFIG = asyncio.Semaphore(8)


async def _config_en_hilo(func, *args):
    """
    Ejecuta un resolutor de configuración (I/O síncrono de Redis/PG) en un hilo
    para no bloquear el event loop.

    El semáforo _LECTURAS_CONFIG acota las lecturas concurrentes: si Redis cae,
    cada lectura degrada a PostgreSQL y el pool puede agotarse; con el tope por
    debajo del pool size, las peticiones hacen cola aquí en vez de fallar con 500.
    """
    async with _LECTURAS_CONFIG:
        return await asyncio.to_thread(func, *args)


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
    return _PATRON_NO_DIGITO.sub("", str(valor))


def normalizar_identificacion(valor) -> str:
    """
    Normaliza una identificación a MAYÚSCULAS y solo letras/dígitos (los
    números de pasaporte son alfanuméricos). Tolera valores numéricos.
    """
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return _PATRON_NO_ALFANUM.sub("", str(valor).upper())


def normalizar_identificacion_comparable(valor) -> str:
    """
    Como `normalizar_identificacion`, pero rellena con ceros a la izquierda las
    identificaciones puramente numéricas más cortas que una cédula (10 dígitos),
    SOLO si el resultado es una cédula ecuatoriana válida. Recupera el cero
    inicial que algunos extractores pierden al devolver la cédula como número
    (p. ej. 942112129 -> 0942112129) sin tocar otros números cortos (pasaportes
    numéricos extranjeros) ni los alfanuméricos.
    """
    norm = normalizar_identificacion(valor)
    if norm.isdigit() and len(norm) < LONGITUD_CEDULA:
        rellena = norm.zfill(LONGITUD_CEDULA)
        if cedula_es_valida(rellena):
            return rellena
    return norm


_CLAVES_NUMERO = (
    "numero_cedula", "numero_identificacion", "numero_documento",
    "cedula", "identificacion", "numero",
)
_CLAVES_PASAPORTE = (
    "numero_pasaporte", "pasaporte", "numero_documento",
    "numero_identificacion", "numero",
)
_CLAVES_NOMBRE = (
    "nombres_completos", "nombres_apellidos", "nombre_completo",
    "nombres_y_apellidos", "nombres",
)


def normalizar_nombre(valor) -> str:
    """
    Normaliza un nombre para compararlo sin importar el orden de los tokens:
    minúsculas, sin tildes y con las palabras ordenadas alfabéticamente. Así
    'MOLINA JARAMILLO CARLOS ANDRES' y 'Carlos Andrés Molina Jaramillo'
    resultan iguales.
    """
    tokens = _PATRON_TOKEN_NOMBRE.findall(_normalizar_busqueda(str(valor or "")))
    return " ".join(sorted(tokens))


def comparar_campo(valor_sistema, valor_documento, normalizador) -> Optional[bool]:
    """
    Compara un dato del sistema con el extraído del documento, ya normalizados.
    Devuelve None cuando no se puede comparar —el sistema no lo envió o el
    documento no lo trae—; en caso contrario True/False según coincidan. Así se
    distingue "no coincide" (ambos presentes y distintos) de "no se pudo leer".
    """
    if valor_sistema is None or not str(valor_sistema).strip():
        return None
    documento = normalizador(valor_documento)
    if not documento:
        return None
    return normalizador(valor_sistema) == documento


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


def anteponer_prefijo_pasaporte(datos: dict) -> dict:
    """
    Antepone 'VS-' al número de pasaporte en los campos extraídos (solo el
    valor que se devuelve en la respuesta; NO afecta la comparación). Marca el
    primer campo presente entre las claves de pasaporte —el mismo que usa la
    comparación— y es idempotente: no duplica el prefijo si ya lo tiene.
    """
    for clave in _CLAVES_PASAPORTE:
        valor = (datos or {}).get(clave)
        if valor is None:
            continue
        texto = str(valor).strip()
        if not texto:
            continue
        if not texto.upper().startswith(PREFIJO_PASAPORTE):
            datos[clave] = f"{PREFIJO_PASAPORTE}{texto}"
        return datos
    return datos


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


def validar_identificacion_sistema(valor: str) -> str:
    """
    Valida y normaliza (fail-fast) la identificación que envía el sistema:
    cédula (10 dígitos + dígito verificador) o pasaporte (alfanumérico, mínimo
    5 caracteres). Lanza ErrorDeValidacion con el motivo. La usan AMBAS rutas
    de validación para que el contrato de entrada sea el mismo.
    """
    id_sistema = normalizar_identificacion(valor)
    if id_sistema.isdigit():
        if len(id_sistema) != LONGITUD_CEDULA:
            raise ErrorDeValidacion(
                f"La cédula del sistema debe tener {LONGITUD_CEDULA} dígitos; "
                f"se recibió '{valor}' ({len(id_sistema)} dígitos). "
                "¿Se perdió un cero a la izquierda?"
            )
        if not cedula_es_valida(id_sistema):
            raise ErrorDeValidacion(
                f"La cédula del sistema '{valor}' no es un número de "
                "cédula ecuatoriana válido (falla el dígito verificador)."
            )
    elif len(id_sistema) < 5:
        raise ErrorDeValidacion(
            f"La identificación del sistema '{valor}' es demasiado "
            "corta para ser una cédula o un número de pasaporte."
        )
    return id_sistema


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

    async def _clasificar_archivo(self, file_id: str, ruta: str) -> Tuple[str, float, float]:
        """
        Clasifica un archivo ya subido con el procesador configurado para la
        ruta. Devuelve (clase, confianza, umbral). La config (fragmento + umbral)
        se lee una sola vez y fuera del event loop (la lectura de Redis/PG es
        I/O síncrono que de otro modo bloquearía a las demás corrutinas).
        """
        fragmento, umbral = await _config_en_hilo(
            procesadores.config_clasificacion, ruta, self.construir_clasificaciones)
        clase, confianza = await extend.clasificar(file_id, fragmento)
        return clase, confianza, umbral

    async def _extraer_datos(self, ruta: str, clase: str, file_id: str) -> dict:
        """
        Extracción estructurada según la clase. Devuelve {} para clases sin
        procesador ni esquema (no es un documento de identidad reconocido en
        esa ruta).
        """
        fragmento = await _config_en_hilo(procesadores.cuerpo_extraccion, ruta, clase)
        if fragmento is None:
            return {}
        return await extend.extraer(file_id, fragmento)

    async def _extraer_texto(self, file_id: str, ruta: str) -> str:
        """OCR con la configuración de parse de la ruta."""
        fragmento = await _config_en_hilo(procesadores.cuerpo_parse, ruta)
        return await extend.parsear(file_id, fragmento)

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
        texto = await self._extraer_texto(file_id, RutaAPI.OCR)

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
        CLASE detectada: cédula (solo dígitos + verificador, recuperando un cero
        inicial perdido) o pasaporte (alfanumérico en mayúsculas). `coincide` es
        True/False solo cuando ambos lados están presentes y None cuando no hay
        nada que comparar (no se envió la cédula, el documento no trae el número
        o no es un documento de identidad), igual que validar-registro-senescyt.

        Esta ruta usa SOLO dos procesadores: el clasificador y el extractor.
        No usa OCR/parse: el número del documento sale de la extracción
        estructurada, no del texto. (El campo `ocr` de la respuesta quedó
        deprecado y siempre es null.)

        Si la clase es PASAPORTE, el número de pasaporte se devuelve en `datos`
        con el prefijo 'VS-'; la comparación con `cedula_sistema` sigue usando
        el número crudo (sin prefijo).
        """
        id_sistema = None
        if cedula_sistema and cedula_sistema.strip():
            id_sistema = validar_identificacion_sistema(cedula_sistema)

        preprocesar(contenido, mime_type)
        file_id = await extend.subir_archivo(contenido, mime_type, nombre)
        clase, confianza, umbral = await self._clasificar_archivo(file_id, RutaAPI.VALIDAR)
        es_cedula = clase == ClaseDocumento.CEDULA and confianza >= umbral
        es_identidad = clase in TIPOS_IDENTIDAD and confianza >= umbral

        datos = {}
        if es_identidad:
            datos = await self._extraer_datos(RutaAPI.VALIDAR, clase, file_id)

        resultado = {
            "clase_detectada": clase,
            "confianza": confianza,
            "es_cedula": es_cedula,
            "es_identidad": es_identidad,
            "status": estado_validacion(es_identidad, datos),
            "datos": datos,
            "identificacion_sistema": None,
            "identificacion_documento": None,
            "coincide": None,
            "ocr": None,
        }

        if id_sistema is not None:
            resultado["identificacion_sistema"] = cedula_sistema

            if es_identidad:
                if clase == ClaseDocumento.CEDULA:
                    id_documento = numero_en_datos(datos)
                    if id_documento and len(id_documento) < LONGITUD_CEDULA:
                        id_documento = id_documento.zfill(LONGITUD_CEDULA)
                    id_documento = id_documento if cedula_es_valida(id_documento) else None
                else:
                    id_documento = normalizar_identificacion(
                        valor_en_datos(datos, _CLAVES_PASAPORTE)
                    ) or None
                resultado["identificacion_documento"] = id_documento
                resultado["coincide"] = (id_sistema == id_documento) if id_documento else None

        if es_identidad and clase == ClaseDocumento.PASAPORTE:
            resultado["datos"] = anteponer_prefijo_pasaporte(datos)

        return resultado

    async def validar_registro_senescyt(
        self,
        contenido: bytes,
        mime_type: str,
        numero_identificacion: Optional[str] = None,
        nombres: Optional[str] = None,
        nombre: str = "",
    ) -> dict:
        """
        Valida que el documento sea una de las clases aceptadas por la ruta
        (registro SENESCYT, carta de compromiso de subida de título o apostilla) y,
        si se envían, contrasta la identidad con la extraída. Como en
        validar-identidad, `existe_clase` (el `result`) refleja solo la
        clasificación: que el documento sea reconocido como una de esas clases con
        confianza suficiente, sin exigir que la extracción traiga datos. Aparte,
        compara los datos del sistema con los del documento:

          - identificación: se compara ignorando espacios y caracteres especiales.
          - nombres: se comparan en minúsculas, sin tildes y con los tokens
            ordenados, para tolerar diferencias en el orden de los nombres.

        Solo se valida cada campo que se haya enviado (los None se omiten).
        `match_document` es True si al menos uno de los enviados coincide, False si
        ninguno, y None si no se envió ninguno. Usa clasificador + extractor (sin
        OCR); el extractor se configura por ruta en la tabla `procesadores`.
        """
        if numero_identificacion and numero_identificacion.strip():
            validar_identificacion_sistema(numero_identificacion)

        preprocesar(contenido, mime_type)
        file_id = await extend.subir_archivo(contenido, mime_type, nombre)
        clase, confianza, umbral = await self._clasificar_archivo(file_id, RutaAPI.SENESCYT)

        existe_clase = clase in TIPOS_SENESCYT and confianza >= umbral

        datos = {}
        if existe_clase:
            datos = await self._extraer_datos(RutaAPI.SENESCYT, clase, file_id)

        coincide_identificacion = comparar_campo(
            numero_identificacion, valor_en_datos(datos, _CLAVES_NUMERO),
            normalizar_identificacion_comparable)
        coincide_nombres = comparar_campo(
            nombres, valor_en_datos(datos, _CLAVES_NOMBRE), normalizar_nombre)
        comparaciones = [c for c in (coincide_identificacion, coincide_nombres) if c is not None]
        match_document = any(comparaciones) if comparaciones else None

        return {
            "clase_detectada": clase,
            "confianza": confianza,
            "existe_clase": existe_clase,
            "status": estado_validacion(existe_clase, datos),
            "match_document": match_document,
            "coincide_identificacion": coincide_identificacion,
            "coincide_nombres": coincide_nombres,
            "datos": datos,
        }

    async def validar_pago(self, contenido: bytes, mime_type: str, nombre: str = "") -> dict:
        """
        Valida que el documento sea un comprobante de pago y extrae su
        información. Clasifica entre DEPOSITO y TRANSFERENCIA (vs descarte) y,
        según la clase detectada, lo procesa con el extractor de esa clase (el
        extractor se resuelve por (ruta, clase) en la tabla `procesadores`).

        A diferencia de validar-identidad y validar-registro-senescyt, esta ruta
        NO contrasta la información contra datos del sistema: `es_pago` (el
        `result`) refleja solo la clasificación y se devuelven los datos
        extraídos. Usa clasificador + extractor (sin OCR).
        """
        preprocesar(contenido, mime_type)
        file_id = await extend.subir_archivo(contenido, mime_type, nombre)
        clase, confianza, umbral = await self._clasificar_archivo(file_id, RutaAPI.PAGO)

        es_pago = clase in TIPOS_PAGO and confianza >= umbral

        datos = {}
        if es_pago:
            datos = await self._extraer_datos(RutaAPI.PAGO, clase, file_id)

        return {
            "clase_detectada": clase,
            "confianza": confianza,
            "es_pago": es_pago,
            "status": estado_validacion(es_pago, datos),
            "datos": datos,
        }


documentos = ServicioDocumentos()
