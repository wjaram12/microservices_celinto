"""
Toda la lógica del servicio vive aquí, en orden de lectura:

    constantes  ->  error  ->  modelo de datos  ->  preprocesamiento
              ->  llamada a Document AI  ->  búsqueda de texto
              ->  extracción de cédula  ->  las operaciones que usa la API.

La idea es que puedas leer este archivo de arriba a abajo y entender todo
el servicio sin saltar entre carpetas. Las operaciones de alto nivel
(`clasificar`, `ocr`, `validar`) son lo que llaman los endpoints.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from google.cloud import documentai

from app.core.config import settings

# --- Constantes ---
UMBRAL_CONFIANZA = 0.85                 # confianza mínima para dar un documento por válido
LONGITUD_CEDULA = 10                    # la cédula ecuatoriana tiene 10 dígitos
CLASE_CEDULA = "CEDULA"                 # etiqueta que devuelve el modelo para una cédula
FORMATOS_ACEPTADOS = {"application/pdf", "image/jpeg", "image/png"}
MAX_BYTES = 10 * 1024 * 1024            # 10 MB
PATRON_CEDULA = re.compile(r"\b\d{10}\b")

# Clases que NUNCA cuentan como documento válido, aunque la confianza sea alta.
# Cuando agregues la clase OTROS al procesador, su etiqueta debe estar aquí:
# así un "OTROS al 99%" se rechaza en vez de pasar como válido.
CLASES_RECHAZADAS = {"OTROS", "DESCONOCIDO", "DOCUMENTO_DESCONOCIDO"}


class ErrorDeArchivo(Exception):
    """Error recuperable (formato o tamaño) que la API traduce a un HTTP 400."""


class ErrorDeValidacion(Exception):
    """Datos de entrada mal formados (ej. cédula del sistema sin 10 dígitos) -> HTTP 400."""


@dataclass
class DocumentoProcesado:
    """Lo que devuelve Document AI tras procesar un documento."""
    clase_detectada: str
    confianza: float
    texto_completo: str = ""
    # Cada entidad es un dict simple: {"tipo", "valor", "confianza"}
    entidades: list = field(default_factory=list)


# --- Preprocesamiento: una lista de funciones simples ---
# Cada función recibe (contenido, mime_type) y lanza ErrorDeArchivo si algo
# está mal. Para agregar un paso nuevo (deskew, conversión de formato, etc.)
# escribe otra función con esa misma firma y añádela a PREPROCESADORES.
# El orden importa: se ejecutan de arriba a abajo.

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
    """Ejecuta todos los preprocesadores en orden."""
    for paso in PREPROCESADORES:
        paso(contenido, mime_type)


# --- Llamada a Google Document AI ---
_cliente = None  # se crea una sola vez, la primera vez que se usa (lazy)


def obtener_cliente() -> documentai.DocumentProcessorServiceClient:
    """Crea el cliente de Google solo cuando se necesita (no al importar)."""
    global _cliente
    if _cliente is None:
        _cliente = documentai.DocumentProcessorServiceClient()
    return _cliente


def procesar_documento(
    contenido: bytes, mime_type: str, processor_id: Optional[str] = None
) -> DocumentoProcesado:
    """
    Manda el documento a Document AI y traduce la respuesta a DocumentoProcesado.

    `processor_id` permite usar un procesador distinto al clasificador por
    defecto (p. ej. un procesador de OCR dedicado para el endpoint /ocr/).
    Si se omite, se usa el clasificador (`GOOGLE_PROCESSOR_ID`).
    """
    cliente = obtener_cliente()
    resource_name = cliente.processor_path(
        settings.GOOGLE_PROJECT_ID,
        settings.GOOGLE_LOCATION,
        processor_id or settings.GOOGLE_PROCESSOR_ID,
    )
    raw_document = documentai.RawDocument(content=contenido, mime_type=mime_type)
    request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)

    document = cliente.process_document(request=request).document

    # El clasificador devuelve UNA entidad por cada clase candidata y el orden
    # NO está garantizado: hay que quedarse con la de mayor confianza, no con
    # la primera de la lista.
    if document.entities:
        principal = max(document.entities, key=lambda e: e.confidence)
        clase_detectada = principal.type_.upper()
        confianza = float(principal.confidence)
    else:
        clase_detectada = "DOCUMENTO_DESCONOCIDO"
        confianza = 0.0

    entidades = [
        {"tipo": e.type_.upper(), "valor": e.mention_text, "confianza": float(e.confidence)}
        for e in document.entities
    ]

    return DocumentoProcesado(
        clase_detectada=clase_detectada,
        confianza=confianza,
        texto_completo=document.text or "",
        entidades=entidades,
    )


# --- Búsqueda de texto en el OCR (genérica) ---

def _normalizar_busqueda(texto: str) -> str:
    """
    Pasa el texto a minúsculas y sin tildes para comparar de forma tolerante.

    Se normaliza carácter por carácter garantizando que cada carácter de
    entrada produce EXACTAMENTE uno de salida; así las posiciones del texto
    normalizado siguen alineadas con las del original y los fragmentos de
    contexto se recortan en el sitio correcto.
    """
    salida = []
    for ch in texto:
        bajo = ch.lower()
        bajo = bajo[0] if bajo else ch  # lower() rara vez expande a 2 caracteres
        descompuesto = unicodedata.normalize("NFKD", bajo)
        base = "".join(c for c in descompuesto if not unicodedata.combining(c))
        salida.append(base[0] if base else bajo)
    return "".join(salida)


def buscar_en_texto(texto: str, termino: str, margen: int = 40) -> dict:
    """
    Busca TODAS las apariciones de `termino` en `texto`, ignorando mayúsculas
    y tildes. Por cada una devuelve un fragmento de contexto (del texto
    original) con `margen` caracteres a cada lado.
    """
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
        # split()+join() colapsa saltos de línea y espacios repetidos del OCR.
        fragmento = " ".join(texto[desde:hasta].split())
        coincidencias.append({"posicion": pos, "contexto": fragmento})
        inicio = pos + len(termino_norm)

    return {
        "termino": termino,
        "encontrado": bool(coincidencias),
        "cantidad": len(coincidencias),
        "coincidencias": coincidencias,
    }


# --- Extracción de la cédula del documento ---

def normalizar_cedula(valor: Optional[str]) -> str:
    """Deja solo los dígitos: quita espacios, guiones, puntos, etc."""
    return re.sub(r"\D", "", valor or "")


def cedula_es_valida(numero: str) -> bool:
    """
    Verifica que un número de 10 dígitos sea una cédula ecuatoriana REAL,
    usando el dígito verificador oficial (algoritmo módulo 10).

    Esto descarta números que "parecen" cédula pero no lo son. Ejemplo, un
    celular (098..., 099...): "09" sí es provincia (Guayas), pero su tercer
    dígito (8 o 9) no corresponde a persona natural y se rechaza; y cualquier
    otro número aleatorio que pase esos filtros casi siempre cae en el
    dígito verificador.
    """
    if len(numero) != LONGITUD_CEDULA or not numero.isdigit():
        return False

    # Los 2 primeros dígitos son la provincia: 01 a 24
    # (30 se usa para ecuatorianos registrados en el exterior).
    provincia = int(numero[:2])
    if provincia not in range(1, 25) and provincia != 30:
        return False

    # El tercer dígito es 0-5 para personas naturales.
    if int(numero[2]) > 5:
        return False

    # Algoritmo módulo 10: se multiplica cada uno de los 9 primeros dígitos
    # por su coeficiente; si el producto pasa de 9, se le resta 9.
    coeficientes = (2, 1, 2, 1, 2, 1, 2, 1, 2)
    suma = 0
    for digito, coeficiente in zip(numero[:9], coeficientes):
        producto = int(digito) * coeficiente
        if producto > 9:
            producto -= 9
        suma += producto

    # El décimo dígito debe ser lo que falta para llegar a la decena siguiente.
    verificador_esperado = (10 - suma % 10) % 10
    return int(numero[9]) == verificador_esperado


def extraer_cedula(doc: DocumentoProcesado) -> Optional[str]:
    """
    Busca el número de cédula en el documento.

    Junta todos los candidatos de 10 dígitos (primero los de las entidades,
    que son más confiables; luego los del texto OCR completo) y devuelve el
    PRIMERO que pase la verificación del dígito verificador. Así un teléfono
    u otro número de 10 dígitos que aparezca antes en el texto no se cuela
    como si fuera la cédula.

    Limitación conocida: en el texto OCR solo se detectan los 10 dígitos
    SEGUIDOS (así viene impresa la cédula). Si el OCR los separa con espacios
    o guiones ("010203 0405"), esa vía no los encuentra; las entidades sí,
    porque su valor pasa por normalizar_cedula().
    """
    candidatos = [normalizar_cedula(entidad["valor"]) for entidad in doc.entidades]
    candidatos.extend(PATRON_CEDULA.findall(doc.texto_completo))

    for candidato in candidatos:
        if cedula_es_valida(candidato):
            return candidato
    return None


# --- Operaciones de alto nivel (lo que llaman los endpoints) ---

def clasificar(contenido: bytes, mime_type: str) -> dict:
    """Preprocesa, clasifica y decide si es válido según el umbral de confianza."""
    preprocesar(contenido, mime_type)
    doc = procesar_documento(contenido, mime_type)
    # Para ser válido deben cumplirse DOS cosas: confianza suficiente Y que la
    # clase no sea una de las rechazadas. Si solo miráramos la confianza, un
    # "OTROS al 99%" pasaría como documento válido.
    es_valido = (
        doc.clase_detectada not in CLASES_RECHAZADAS
        and doc.confianza >= UMBRAL_CONFIANZA
    )
    return {
        "clase_detectada": doc.clase_detectada,
        "confianza": doc.confianza,
        "es_valido": es_valido,
    }


def ocr(
    contenido: bytes,
    mime_type: str,
    texto_a_buscar: Optional[str] = None,
) -> dict:
    """
    Extrae el texto del documento (OCR) y, si se envía `texto_a_buscar`,
    indica si ese término aparece y en qué contexto.

    Usa el procesador de OCR dedicado (`GOOGLE_OCR_PROCESSOR_ID`) si está
    configurado; si no, cae al clasificador, que también devuelve texto
    aunque normalmente menos completo que un procesador de OCR puro.
    """
    preprocesar(contenido, mime_type)
    doc = procesar_documento(
        contenido, mime_type, settings.GOOGLE_OCR_PROCESSOR_ID or None
    )

    busqueda = None
    if texto_a_buscar and texto_a_buscar.strip():
        busqueda = buscar_en_texto(doc.texto_completo, texto_a_buscar)

    return {
        "texto_completo": doc.texto_completo,
        "entidades": doc.entidades,
        "busqueda": busqueda,
    }


def validar(
    contenido: bytes,
    mime_type: str,
    cedula_sistema: Optional[str] = None,
) -> dict:
    """
    Validación dinámica en un solo paso:

    - Si NO llega `cedula_sistema`: solo clasifica el documento y dice si es
      una cédula (no se extrae ningún número).
    - Si llega `cedula_sistema`: además activa el OCR para leer el número de
      la cédula en el documento y lo compara contra el del sistema.
    """
    # Primero se valida el dato local (si llegó): es gratis e instantáneo.
    # Hacerlo al final desperdiciaría una llamada a Document AI (lenta y
    # facturada) solo para terminar devolviendo un error 400.
    id_sistema = None
    if cedula_sistema and cedula_sistema.strip():
        id_sistema = normalizar_cedula(cedula_sistema)

        # Si el número del sistema no tiene 10 dígitos, es un error de datos
        # (típico: se guardó como entero y perdió el cero inicial). Avisar es
        # mejor que devolver un "no coincide" silencioso.
        if len(id_sistema) != LONGITUD_CEDULA:
            raise ErrorDeValidacion(
                f"La cédula del sistema debe tener {LONGITUD_CEDULA} dígitos; "
                f"se recibió '{cedula_sistema}' ({len(id_sistema)} dígitos). "
                "¿Se perdió un cero a la izquierda?"
            )

        # Y si no pasa el dígito verificador, el dato del sistema está corrupto:
        # nunca va a coincidir con nada y conviene avisarlo con la causa real.
        if not cedula_es_valida(id_sistema):
            raise ErrorDeValidacion(
                f"La cédula del sistema '{cedula_sistema}' no es un número de "
                "cédula ecuatoriana válido (falla el dígito verificador)."
            )

    preprocesar(contenido, mime_type)
    doc = procesar_documento(contenido, mime_type)

    es_cedula = doc.clase_detectada == CLASE_CEDULA and doc.confianza >= UMBRAL_CONFIANZA

    resultado = {
        "clase_detectada": doc.clase_detectada,
        "confianza": doc.confianza,
        "es_cedula": es_cedula,
        # Campos de identidad y OCR: solo se llenan en el modo con número.
        "identificacion_sistema": None,
        "identificacion_documento": None,
        "coincide": None,
        "ocr": None,
    }

    # El OCR de comparación se activa únicamente si se envió un número.
    if id_sistema is not None:
        resultado["identificacion_sistema"] = cedula_sistema  # tal como llegó
        # Se devuelve el texto OCR del documento (lo que "leyó" el modelo).
        resultado["ocr"] = doc.texto_completo

        # Solo tiene sentido comparar números si el documento ES una cédula.
        # Sin esta condición, un certificado de votación (que trae impreso el
        # número de cédula) daría coincide=True con es_cedula=False, y un
        # consumidor que solo mire `coincide` aceptaría el documento equivocado.
        if es_cedula:
            id_documento = extraer_cedula(doc)
            resultado["identificacion_documento"] = id_documento
            resultado["coincide"] = bool(id_documento) and id_sistema == id_documento
        else:
            resultado["coincide"] = False

    return resultado
