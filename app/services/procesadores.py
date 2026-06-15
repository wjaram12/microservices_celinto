"""
Servicio ServicioProcesadores: la configuración de los procesadores de Extend,
en PostgreSQL (tabla `procesadores`).

Cada fila dice CÓMO resuelve UNA ruta de la API una operación de Extend para una
clase de documento. Así cada ruta puede usar procesadores distintos. Se edita en
caliente desde /admin/procesadores, sin redeploy.

    ruta          ruta de la API que usa la fila:
                  'validar-identidad'         -> POST /api/v1/validaciones/validar-identidad/
                  'validar-registro-senescyt' -> POST /api/v1/validaciones/validar-registro-senescyt/
                  'ocr'                       -> POST /api/v1/ocr/
    operacion     'clasificar' | 'extraer' | 'parse'
    clase         clase de documento ('CEDULA', 'PASAPORTE', ...) para los
                  esquemas de extracción; '' cuando no aplica.
    modo          'id'     -> procesador YA publicado en Extend (cl_.../ex_...).
                  'inline' -> config en la propia petición: clasificar usa la
                             tabla `clasificaciones`; extraer usa el JSON Schema
                             de la columna `esquema`.
    procesador_id 'cl_...'/'ex_...' cuando modo='id'.
    version       versión del procesador publicado a fijar (modo='id'); '' = última.
    esquema       JSONB: JSON Schema (extraer inline) o {"target": "..."} (parse).
    umbral        confianza mínima 0..1 del clasificador (solo 'clasificar').
    activo        TRUE = se usa; FALSE = guardada pero ignorada.

El .env sigue guardando SOLO secretos (EXTEND_API_KEY, DATABASE_URL).

Los resolutores (cuerpo_*) leen de esta tabla en cada petición y devuelven el
fragmento de body que ServicioDocumentos inyecta en la llamada a Extend.
"""
from typing import Callable, Optional, Tuple

from psycopg2 import errors as pg_errors
from psycopg2.extras import Json, RealDictCursor

from app.core.cache import cache
from app.core.db import ServicioBD
from app.services.errores import ErrorDeValidacion
from app.services.extend import extend
from app.services.rutas import rutas

OPERACIONES_VALIDAS = {"clasificar", "extraer", "parse"}
MODOS_VALIDOS = {"id", "inline"}

UMBRAL_DEFECTO = 0.85

_TIPO_EXTEND = {"clasificar": "CLASSIFY", "extraer": "EXTRACT"}

CLAVE_CACHE = "procesadores:activas"

OTRO_POR_DEFECTO = {
    "id": "otros_descarte",
    "type": "other",
    "description": (
        "Cualquier otro documento que no corresponda a ninguno de los tipos "
        "definidos: facturas, capturas de pantalla, papeles sin relación, etc."
    ),
}


def _normalizar_clasificaciones(lista: list) -> list:
    """Normaliza las clasificaciones propias de una fila ('other' en minúsculas,
    como exige Extend) y garantiza la clase de descarte 'other' que Extend
    exige siempre."""
    salida = []
    tiene_otro = False
    for c in lista:
        tipo = str(c.get("type", "")).strip()
        if tipo.lower() in ("other", "otros"):
            tipo = "other"
            tiene_otro = True
        salida.append({
            "id": c.get("id"),
            "type": tipo,
            "description": c.get("description", ""),
        })
    if not tiene_otro:
        salida.append(OTRO_POR_DEFECTO)
    return salida


def _campo(desc: str) -> dict:
    return {"type": ["string", "null"], "description": desc}


_ESQUEMA_CEDULA = {
    "type": "object",
    "properties": {
        "numero_cedula": _campo("Número de cédula de identidad ecuatoriana (10 dígitos)."),
        "apellidos": _campo("Apellidos del titular."),
        "nombres": _campo("Nombres del titular."),
        "nacionalidad": _campo("Nacionalidad del titular."),
        "sexo": _campo("Sexo del titular (M o F)."),
        "fecha_nacimiento": _campo("Fecha de nacimiento (formato del documento)."),
        "lugar_nacimiento": _campo("Lugar de nacimiento."),
    },
}

_ESQUEMA_PASAPORTE = {
    "type": "object",
    "properties": {
        "numero_identificacion": _campo("Número de identificación del titular (número de pasaporte)."),
        "pais_emisor": _campo("País emisor (código o nombre)."),
        "apellidos": _campo("Apellidos del titular (surname)."),
        "nombres": _campo("Nombres del titular (given names)."),
        "nacionalidad": _campo("Nacionalidad del titular."),
        "sexo": _campo("Sexo del titular (M o F)."),
        "fecha_nacimiento": _campo("Fecha de nacimiento (date of birth)."),
        "fecha_expiracion": _campo("Fecha de expiración del pasaporte (date of expiry)."),
    },
}

_ESQUEMA_SENESCYT = {
    "type": "object",
    "properties": {
        "numero_registro": _campo("Número de registro del título en la SENESCYT."),
        "nombres_apellidos": _campo("Nombres y apellidos del titular."),
        "numero_identificacion": _campo("Número de identificación (cédula o pasaporte) del titular."),
        "titulo": _campo("Nombre del título obtenido."),
        "institucion": _campo("Institución de Educación Superior que otorgó el título."),
        "tipo": _campo("Tipo o nivel del título (tercer nivel, cuarto nivel, etc.)."),
        "area_conocimiento": _campo("Área o campo de conocimiento."),
        "fecha_registro": _campo("Fecha de registro del título."),
    },
}

_ESQUEMA_CARTA_COMPROMISO = {
    "type": "object",
    "properties": {
        "numero_identificacion": _campo("Número de identificación (cédula o pasaporte) del firmante."),
        "nombres_completos": _campo("Nombres y apellidos completos del firmante."),
        "titulo": _campo("Título o programa al que se compromete a subir el documento."),
        "institucion": _campo("Institución de Educación Superior relacionada, si aparece."),
        "fecha": _campo("Fecha de la carta de compromiso."),
    },
}

_ESQUEMA_APOSTILLA = {
    "type": "object",
    "properties": {
        "numero_identificacion": _campo("Número de identificación del titular del documento apostillado."),
        "nombres_completos": _campo("Nombres y apellidos completos del titular."),
        "numero_apostilla": _campo("Número de la apostilla."),
        "pais_emisor": _campo("País que emite la apostilla."),
        "autoridad": _campo("Autoridad que certifica la apostilla."),
        "fecha": _campo("Fecha de emisión de la apostilla."),
    },
}

_ESQUEMA_DEPOSITO = {
    "type": "object",
    "properties": {
        "banco": {
            "type": ["string", "null"],
            "description": (
                "Name of the financial institution that issued the deposit receipt.\n\n"
                "Extract the full official name exactly as printed in the document \n"
                "header or stamp.\n\n"
                "Examples: \"BANCO PICHINCHA\", \"BANCO DEL PACÍFICO\", \"BANCO GUAYAQUIL\",\n"
                "\"PRODUBANCO\", \"BANCO INTERNACIONAL\", \"COOPERATIVA JEP\", \n"
                "\"COOPERATIVA 29 DE OCTUBRE\"\n\n"
                "Return null if not visible or legible."
            ),
        },
        "numero_cuenta": {
            "type": ["string", "null"],
            "description": (
                "Destination bank account number where the deposit was credited.\n"
                "May appear labeled as \"Cuenta\", \"No. Cuenta\", \"Cuenta Destino\", \n"
                "or \"Cuenta Beneficiario\".\n\n"
                "Extract exactly as printed, preserving all digits.\n"
                "May be partially masked (e.g. XXXX1234) — extract as shown.\n\n"
                "Return null if not present or not legible."
            ),
        },
        "nombre_depositante": {
            "type": ["string", "null"],
            "description": (
                "Full name of the person or entity that made the deposit.\n"
                "May appear labeled as \"Depositante\", \"Nombre\", \"Cliente\", \n"
                "or \"Remitente\".\n\n"
                "Extract exactly as printed, including all words in the name.\n"
                "May appear in uppercase or mixed case.\n\n"
                "Return null if not present or not legible."
            ),
        },
        "monto": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": ["number", "null"],
                    "description": "The numeric value of the currency.",
                },
                "iso_4217_currency_code": {
                    "type": ["string", "null"],
                    "description": "The ISO 4217 currency code (e.g., USD, EUR, GBP).",
                },
            },
            "additionalProperties": False,
            "required": ["amount", "iso_4217_currency_code"],
            "extend:type": "currency",
            "description": (
                "Total amount deposited, expressed in US dollars (USD).\n"
                "May appear labeled as \"Valor\", \"Monto\", \"Total\", or \"Importe\".\n\n"
                "Extract only the numeric value with up to 2 decimal places.\n"
                "Do not include currency symbols ($ or USD) in the output.\n\n"
                "Examples: \"150.00\", \"1250.50\", \"75.00\"\n\n"
                "Return null if not present or not legible."
            ),
        },
        "fecha": {
            "type": ["string", "null"],
            "extend:type": "date",
            "description": (
                "Date on which the deposit was made.\n"
                "May appear labeled as \"Fecha\", \"Fecha de Depósito\", or \"Date\".\n\n"
                "Normalize to ISO format YYYY-MM-DD regardless of how it appears \n"
                "on the document.\n\n"
                "Accepted input formats:\n"
                "- DD/MM/YYYY  → e.g. 14/05/2025\n"
                "- DD-MM-YYYY  → e.g. 14-05-2025\n"
                "- DD MMM YYYY → e.g. 14 MAY 2025\n"
                "- YYYY-MM-DD  → e.g. 2025-05-14\n\n"
                "Return null if not present or not legible."
            ),
        },
        "hora": {
            "type": ["string", "null"],
            "description": (
                "Time at which the deposit transaction was processed.\n"
                "May appear labeled as \"Hora\", \"Hora de Transacción\", or \"Time\".\n\n"
                "Normalize to 24-hour format HH:MM:SS in the output.\n"
                "If seconds are not shown, default to HH:MM:00.\n\n"
                "Examples: \"14:32:00\", \"09:05:00\"\n\n"
                "Return null if not present or not legible."
            ),
        },
        "numero_referencia": {
            "type": ["string", "null"],
            "description": (
                "Unique transaction or reference code assigned by the bank \n"
                "to identify this deposit operation.\n"
                "May appear labeled as \"Referencia\", \"No. Transacción\", \n"
                "\"Código\", \"Secuencial\", or \"Voucher\".\n\n"
                "Extract exactly as printed, preserving all characters \n"
                "including leading zeros.\n\n"
                "Return null if not present or not legible."
            ),
        },
        "agencia": {
            "type": ["string", "null"],
            "description": (
                "Bank branch or agency where the deposit was physically made.\n"
                "May appear labeled as \"Agencia\", \"Sucursal\", \"Oficina\", or \"Branch\".\n\n"
                "Extract exactly as printed including city or location if present.\n\n"
                "Examples: \"Agencia Guayaquil Norte\", \"Sucursal Kennedy\", \n"
                "\"Oficina Matriz Quito\"\n\n"
                "Return null if not present or not legible."
            ),
        },
        "tipo_cuenta": {
            "type": ["string", "null"],
            "description": (
                "Type of bank account where the deposit was credited.\n"
                "May appear labeled as \"Tipo de Cuenta\", \"Tipo Cta\", \n"
                "\"Cuenta de Ahorros\", \"Cuenta Corriente\", or similar.\n\n"
                "Expected values:\n"
                "- \"AHORROS\" — savings account (most common in Ecuador)\n"
                "- \"CORRIENTE\" — checking account (used by companies/businesses)\n\n"
                "Normalize to one of these two values regardless of how \n"
                "it appears on the document.\n\n"
                "Common variations:\n"
                "- \"CTA. AHORROS\", \"C/A\", \"AHO\" → return \"AHORROS\"\n"
                "- \"CTA. CORRIENTE\", \"C/C\", \"CTE\" → return \"CORRIENTE\"\n\n"
                "If the account type is not explicitly stated but the \n"
                "account number format suggests it (some banks prefix \n"
                "account numbers with a digit indicating type), \n"
                "extract what is visible.\n\n"
                "Return null if not present or not legible."
            ),
        },
    },
    "required": [
        "banco",
        "numero_cuenta",
        "nombre_depositante",
        "monto",
        "fecha",
        "hora",
        "numero_referencia",
        "agencia",
        "tipo_cuenta",
    ],
    "additionalProperties": False,
}

_ESQUEMA_TRANSFERENCIA = {
    "type": "object",
    "properties": {
        "banco": {
            "type": ["string", "null"],
            "description": (
                "Name of the financial institution or payment platform that \n"
                "issued the transfer confirmation.\n\n"
                "Extract the full official name exactly as printed in the \n"
                "document header.\n\n"
                "Examples: \"BANCO PICHINCHA\", \"BANCO CENTRAL DEL ECUADOR\",\n"
                "\"PRODUBANCO\", \"BANCO GUAYAQUIL\", \"PAYPHONE\", \"COOPERATIVA JEP\"\n\n"
                "Return null if not visible or legible."
            ),
        },
        "cuenta_origen": {
            "type": ["string", "null"],
            "description": (
                "Bank account number from which the funds were debited.\n"
                "May appear labeled as \"Cuenta Origen\", \"Cuenta Débito\", \n"
                "\"Mi Cuenta\", or \"Cuenta Remitente\".\n\n"
                "Extract exactly as printed, preserving all digits.\n"
                "May be partially masked (e.g. XXXX5678) — extract as shown.\n\n"
                "Return null if not present or not legible."
            ),
        },
        "cuenta_destino": {
            "type": ["string", "null"],
            "description": (
                "Bank account number to which the funds were credited.\n"
                "May appear labeled as \"Cuenta Destino\", \"Cuenta Beneficiario\",\n"
                "\"Cuenta Crédito\", or \"Cuenta Receptora\".\n\n"
                "Extract exactly as printed, preserving all digits.\n"
                "May be partially masked — extract as shown.\n\n"
                "Return null if not present or not legible."
            ),
        },
        "nombre_beneficiario": {
            "type": ["string", "null"],
            "description": (
                "Full name of the person or entity that received the transfer.\n"
                "May appear labeled as \"Beneficiario\", \"Nombre Beneficiario\",\n"
                "\"Destinatario\", or \"Nombre Receptor\".\n\n"
                "Extract exactly as printed, including all words in the name.\n"
                "May appear in uppercase or mixed case.\n\n"
                "Return null if not present or not legible."
            ),
        },
        "monto": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": ["number", "null"],
                    "description": "The numeric value of the currency.",
                },
                "iso_4217_currency_code": {
                    "type": ["string", "null"],
                    "description": "The ISO 4217 currency code (e.g., USD, EUR, GBP).",
                },
            },
            "additionalProperties": False,
            "required": ["amount", "iso_4217_currency_code"],
            "extend:type": "currency",
            "description": (
                "Total amount transferred, expressed in US dollars (USD).\n"
                "May appear labeled as \"Valor\", \"Monto\", \"Total\", \n"
                "\"Importe\", or \"Valor Transferido\".\n\n"
                "Extract only the numeric value with up to 2 decimal places.\n"
                "Do not include currency symbols ($ or USD) in the output.\n\n"
                "Examples: \"500.00\", \"1800.75\", \"250.00\"\n\n"
                "Return null if not present or not legible."
            ),
        },
        "fecha": {
            "type": ["string", "null"],
            "extend:type": "date",
            "description": (
                "Date on which the transfer was processed.\n"
                "May appear labeled as \"Fecha\", \"Fecha de Transferencia\", \n"
                "\"Fecha de Transacción\", or \"Date\".\n\n"
                "Normalize to ISO format YYYY-MM-DD regardless of how it \n"
                "appears on the document.\n\n"
                "Accepted input formats:\n"
                "- DD/MM/YYYY  → e.g. 14/05/2025\n"
                "- DD-MM-YYYY  → e.g. 14-05-2025\n"
                "- DD MMM YYYY → e.g. 14 MAY 2025\n"
                "- YYYY-MM-DD  → e.g. 2025-05-14\n\n"
                "Return null if not present or not legible."
            ),
        },
        "hora": {
            "type": ["string", "null"],
            "description": (
                "Time at which the transfer was processed.\n"
                "May appear labeled as \"Hora\", \"Hora de Transacción\", or \"Time\".\n\n"
                "Normalize to 24-hour format HH:MM:SS in the output.\n"
                "If seconds are not shown, default to HH:MM:00.\n\n"
                "Examples: \"10:15:00\", \"16:45:00\"\n\n"
                "Return null if not present or not legible."
            ),
        },
        "codigo_autorizacion": {
            "type": ["string", "null"],
            "description": (
                "Unique authorization or transaction code assigned to this transfer.\n"
                "May appear labeled as \"Código de Autorización\", \"No. Transacción\",\n"
                "\"Referencia SPI\", \"Código SPI\", or \"Número de Operación\".\n\n"
                "For interbank transfers via BCE (Banco Central del Ecuador), \n"
                "this is the SPI reference code.\n\n"
                "Extract exactly as printed, preserving all characters \n"
                "including leading zeros.\n\n"
                "Return null if not present or not legible."
            ),
        },
        "estado": {
            "type": ["string", "null"],
            "description": (
                "Final status of the transfer transaction as printed on the document.\n"
                "May appear labeled as \"Estado\", \"Estado de Transacción\", or \"Status\".\n\n"
                "Expected values:\n"
                "- Successful: \"EXITOSA\", \"APROBADA\", \"COMPLETADA\", \"PROCESADA\", \"OK\"\n"
                "- Failed: \"FALLIDA\", \"RECHAZADA\", \"ERROR\", \"PENDIENTE\"\n\n"
                "Extract exactly as printed. Do not translate or normalize.\n"
                "If the document only shows a success confirmation without \n"
                "an explicit status field, return \"EXITOSA\".\n\n"
                "Return null if not present or not legible."
            ),
        },
        "tipo_cuenta": {
            "type": ["string", "null"],
            "description": (
                "Type of destination bank account that received the transfer.\n"
                "May appear labeled as \"Tipo Cuenta Destino\", \"Tipo de Cuenta \n"
                "Beneficiario\", or \"Tipo Cta. Destino\".\n\n"
                "Expected values:\n"
                "- \"AHORROS\" — savings account\n"
                "- \"CORRIENTE\" — checking account\n\n"
                "Normalize to one of these two values regardless of how \n"
                "it appears on the document.\n\n"
                "Common variations:\n"
                "- \"CTA. AHORROS\", \"C/A\", \"AHO\" → return \"AHORROS\"\n"
                "- \"CTA. CORRIENTE\", \"C/C\", \"CTE\" → return \"CORRIENTE\"\n\n"
                "Note: In Ecuador, individual persons typically hold savings \n"
                "accounts (AHORROS) while legal entities and companies \n"
                "typically hold checking accounts (CORRIENTE). Use this \n"
                "as a secondary inference only if the field is not explicitly \n"
                "printed.\n\n"
                "Return null if not present or not legible."
            ),
        },
    },
    "required": [
        "banco",
        "cuenta_origen",
        "cuenta_destino",
        "nombre_beneficiario",
        "monto",
        "fecha",
        "hora",
        "codigo_autorizacion",
        "estado",
        "tipo_cuenta",
    ],
    "additionalProperties": False,
}

_CLASIF_SENESCYT = {"classifications": [
    {"id": "registro_senescyt", "type": "REGISTRO_SENESCYT",
     "description": ("Registro de título de la SENESCYT (Ecuador): documento que "
                     "certifica el registro de un título académico, con número de "
                     "registro, titular, institución de educación superior y título.")},
    {"id": "carta_compromiso", "type": "CARTA_COMPROMISO",
     "description": ("Carta de compromiso de subida de título: documento en el que el "
                     "aspirante se compromete a entregar o registrar su título académico "
                     "ante la institución, con nombres del firmante y, normalmente, su "
                     "número de identificación. (placeholder — reemplazar por el prompt real)")},
    {"id": "apostilla", "type": "APOSTILLA",
     "description": ("Apostilla (Convención de La Haya): certificación que legaliza un "
                     "documento público para uso internacional, con número de apostilla, "
                     "país y autoridad emisora, y datos del titular del documento. "
                     "(placeholder — reemplazar por el prompt real)")},
    {"id": "otros", "type": "other",
     "description": ("Cualquier otro documento que no sea un registro de título de la "
                     "SENESCYT, una carta de compromiso de subida de título ni una apostilla.")},
]}

_CLASIF_PAGO = {"classifications": [
    {"id": "classification1", "type": "other",
     "description": ("Use the `other` document type when the provided document can not "
                     "clearly be classified into one of the described classifications.")},
    {"id": "classification_7nc", "type": "deposito",
     "description": (
         "Bank deposit receipt or voucher issued by any Ecuadorian financial \n"
         "institution. This document certifies that a cash deposit was made \n"
         "into a bank account.\n\n"
         "DISTINCTIVE FEATURES:\n"
         "- Printed or stamped header with the bank's name and logo\n"
         "  (Banco Pichincha, Banco Guayaquil, Banco Pacífico, Produbanco, \n"
         "  Banco Internacional, Cooperativas, etc.)\n"
         "- Document type label: \"DEPÓSITO\", \"COMPROBANTE DE DEPÓSITO\", \n"
         "  \"PAPELETA DE DEPÓSITO\" or similar\n"
         "- Contains: account number, depositor name, deposit amount, \n"
         "  date and time, branch or agency, transaction reference number\n"
         "- May include a bank teller stamp or validation seal\n"
         "- Can be presented as a physical scan or a printed digital receipt\n"
         "- Amount is displayed prominently, typically in USD\n\n"
         "EXCLUDE: Wire transfers, online transfers, payment confirmations \n"
         "without a deposit slip format.")},
    {"id": "classification_EWR", "type": "transferencia",
     "description": (
         "Electronic bank transfer confirmation or receipt issued by any \n"
         "Ecuadorian financial institution or payment platform. This document \n"
         "certifies that funds were moved electronically between accounts.\n\n"
         "DISTINCTIVE FEATURES:\n"
         "- Header with the bank or platform name and logo\n"
         "  (Banco Pichincha, Banco Guayaquil, BCE - Banco Central del Ecuador,\n"
         "  Produbanco, Cooperativas, PayPhone, etc.)\n"
         "- Document type label: \"TRANSFERENCIA\", \"COMPROBANTE DE TRANSFERENCIA\",\n"
         "  \"TRANSFERENCIA INTERBANCARIA\", \"CONFIRMACIÓN DE TRANSFERENCIA\" or similar\n"
         "- Contains: origin account, destination account, beneficiary name,\n"
         "  transfer amount in USD, date and time, transaction or authorization code\n"
         "- May show SPI (Sistema de Pagos Interbancarios) reference for \n"
         "  interbank transfers\n"
         "- Can be a printed PDF, screenshot, or digital receipt\n"
         "- Transaction status must show: \"EXITOSA\", \"APROBADA\", \"COMPLETADA\" \n"
         "  or equivalent confirmation\n\n"
         "EXCLUDE: Deposit slips, payment orders pending approval, \n"
         "failed or rejected transaction confirmations.")},
]}

SEMILLA = [
    ("validar-identidad", "clasificar", "", "inline", None, None, None, UMBRAL_DEFECTO),
    ("validar-identidad", "extraer", "CEDULA", "inline", None, None, _ESQUEMA_CEDULA, None),
    ("validar-identidad", "extraer", "PASAPORTE", "inline", None, None, _ESQUEMA_PASAPORTE, None),
    ("validar-registro-senescyt", "clasificar", "", "inline", None, None, _CLASIF_SENESCYT, UMBRAL_DEFECTO),
    ("validar-registro-senescyt", "extraer", "REGISTRO_SENESCYT", "inline", None, None, _ESQUEMA_SENESCYT, None),
    ("validar-registro-senescyt", "extraer", "CARTA_COMPROMISO", "inline", None, None, _ESQUEMA_CARTA_COMPROMISO, None),
    ("validar-registro-senescyt", "extraer", "APOSTILLA", "inline", None, None, _ESQUEMA_APOSTILLA, None),
    ("validar-pago", "clasificar", "", "inline", None, None, _CLASIF_PAGO, UMBRAL_DEFECTO),
    ("validar-pago", "extraer", "DEPOSITO", "inline", None, None, _ESQUEMA_DEPOSITO, None),
    ("validar-pago", "extraer", "TRANSFERENCIA", "inline", None, None, _ESQUEMA_TRANSFERENCIA, None),
    ("ocr", "parse", "", "inline", None, None, {"target": "markdown"}, None),
]

_COLUMNAS = (
    "p.id, r.clave AS ruta, p.ruta_id, p.operacion, p.clase, p.modo, "
    "p.procesador_id, p.version, p.esquema, p.umbral, p.activo, "
    "p.creado_en, p.actualizado_en"
)
_FROM = "FROM procesadores p JOIN rutas r ON r.id = p.ruta_id"


class ServicioProcesadores(ServicioBD):
    """CRUD, resolutores y sincronización con Extend Studio."""

    DDL = """
        CREATE TABLE IF NOT EXISTS procesadores (
            id             SERIAL PRIMARY KEY,
            ruta_id        INTEGER NOT NULL REFERENCES rutas(id) ON DELETE RESTRICT,
            operacion      TEXT NOT NULL,
            clase          TEXT NOT NULL DEFAULT '',
            modo           TEXT NOT NULL DEFAULT 'inline',
            procesador_id  TEXT,
            version        TEXT,
            esquema        JSONB,
            umbral         REAL,
            activo         BOOLEAN NOT NULL DEFAULT TRUE,
            creado_en      TIMESTAMP NOT NULL DEFAULT now(),
            actualizado_en TIMESTAMP NOT NULL DEFAULT now(),
            UNIQUE (ruta_id, operacion, clase)
        )
    """

    @staticmethod
    def _normalizar(fila: Optional[dict]) -> Optional[dict]:
        if fila is None:
            return None
        d = dict(fila)
        d["activo"] = bool(d["activo"])
        if d.get("umbral") is not None:
            d["umbral"] = float(d["umbral"])
        for campo in ("creado_en", "actualizado_en"):
            valor = d.get(campo)
            if valor is not None and not isinstance(valor, str):
                d[campo] = valor.strftime("%Y-%m-%d %H:%M:%S")
        return d

    @staticmethod
    def normalizar_ruta(ruta: str) -> str:
        return (ruta or "").strip().lower()

    @staticmethod
    def normalizar_operacion(operacion: str) -> str:
        return (operacion or "").strip().lower()

    @staticmethod
    def normalizar_clase(clase: Optional[str]) -> str:
        """Las clases se guardan en MAYÚSCULAS; '' cuando la operación no usa clase."""
        return (clase or "").strip().upper()

    @staticmethod
    def normalizar_modo(modo: str) -> str:
        return (modo or "").strip().lower()

    def _resolver_ruta_id(self, ruta: str) -> int:
        """Valida que la ruta exista y esté activa; devuelve su id para la FK."""
        fila = rutas.obtener(ruta)
        if fila is None or not fila["activo"]:
            registradas = rutas.claves_activas()
            raise ValueError(
                f"Ruta '{ruta}' no registrada o inactiva. Rutas disponibles: "
                f"{', '.join(sorted(registradas)) or '(ninguna)'}. "
                "Regístrala primero en /admin/rutas."
            )
        return fila["id"]

    @staticmethod
    def _validar(operacion: str, modo: str,
                 procesador_id: Optional[str], esquema) -> None:
        if operacion not in OPERACIONES_VALIDAS:
            raise ValueError(
                f"Operación inválida '{operacion}'. Debe ser una de: "
                f"{', '.join(sorted(OPERACIONES_VALIDAS))}."
            )
        if modo not in MODOS_VALIDOS:
            raise ValueError("Modo inválido. Debe ser 'id' o 'inline'.")
        if modo == "id" and not (procesador_id or "").strip():
            raise ValueError("En modo 'id' hay que indicar el procesador_id (cl_.../ex_...).")
        if modo == "inline" and operacion == "extraer" and not esquema:
            raise ValueError("En modo 'inline' una extracción necesita un esquema JSON.")
        if operacion == "clasificar" and esquema is not None:
            clasificaciones = esquema.get("classifications") if isinstance(esquema, dict) else None
            if not isinstance(clasificaciones, list) or not clasificaciones:
                raise ValueError(
                    "Para 'clasificar', el esquema debe tener una lista 'classifications' "
                    "(id, type, description), o ser nulo para usar los prompts globales."
                )

    @staticmethod
    def _validar_umbral(umbral) -> None:
        if umbral is not None and not (0.0 <= float(umbral) <= 1.0):
            raise ValueError("El umbral de confianza debe estar entre 0 y 1 (ej. 0.85).")

    def inicializar(self) -> None:
        """Crea la tabla (idempotente) y la siembra si está vacía. Tolera la
        carrera entre workers al arrancar (captura UniqueViolation)."""
        try:
            with self._conectar() as con:
                with con.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM procesadores")
                    if cur.fetchone()[0] == 0:
                        cur.executemany(
                            "INSERT INTO procesadores "
                            "(ruta_id, operacion, clase, modo, procesador_id, version, esquema, umbral) "
                            "VALUES ((SELECT id FROM rutas WHERE clave = %s), %s, %s, %s, %s, %s, %s, %s)",
                            [(ru, op, cl, mo, pid, ver, Json(esq) if esq is not None else None, umb)
                             for (ru, op, cl, mo, pid, ver, esq, umb) in SEMILLA],
                        )
        except pg_errors.UniqueViolation:
            pass
        cache.invalidar(CLAVE_CACHE)

    def listar(self, solo_activos: bool = False) -> list:
        sql = f"SELECT {_COLUMNAS} {_FROM}"
        if solo_activos:
            sql += " WHERE p.activo = TRUE"
        sql += " ORDER BY r.clave, p.operacion, p.clase"
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql)
                return [self._normalizar(f) for f in cur.fetchall()]

    def obtener_por_id(self, id_proc: int) -> Optional[dict]:
        with self._conectar() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT {_COLUMNAS} {_FROM} WHERE p.id = %s", (id_proc,))
                return self._normalizar(cur.fetchone())

    def crear(self, ruta: str, operacion: str, clase: str, modo: str,
              procesador_id: Optional[str] = None,
              version: Optional[str] = None,
              esquema: Optional[dict] = None,
              umbral: Optional[float] = None, activo: bool = True) -> dict:
        """Inserta una fila. Lanza ValueError si los datos no son válidos y
        psycopg2.errors.UniqueViolation si ya existe esa (ruta, operacion, clase)."""
        ruta = self.normalizar_ruta(ruta)
        operacion = self.normalizar_operacion(operacion)
        clase = self.normalizar_clase(clase)
        modo = self.normalizar_modo(modo)
        procesador_id = (procesador_id or "").strip() or None
        version = (version or "").strip() or None
        ruta_id = self._resolver_ruta_id(ruta)
        self._validar(operacion, modo, procesador_id, esquema)
        self._validar_umbral(umbral)
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO procesadores "
                    "(ruta_id, operacion, clase, modo, procesador_id, version, esquema, umbral, activo) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (ruta_id, operacion, clase, modo, procesador_id, version,
                     Json(esquema) if esquema is not None else None, umbral, bool(activo)),
                )
                nuevo_id = cur.fetchone()[0]
        cache.invalidar(CLAVE_CACHE)
        return self.obtener_por_id(nuevo_id)

    def actualizar(self, id_proc: int,
                   ruta: Optional[str] = None,
                   operacion: Optional[str] = None,
                   clase: Optional[str] = None,
                   modo: Optional[str] = None,
                   procesador_id: Optional[str] = None,
                   version: Optional[str] = None,
                   esquema: Optional[dict] = None,
                   umbral: Optional[float] = None,
                   activo: Optional[bool] = None,
                   tocar_procesador_id: bool = False,
                   tocar_version: bool = False,
                   tocar_esquema: bool = False,
                   tocar_umbral: bool = False) -> Optional[dict]:
        """
        Actualiza solo los campos enviados. `procesador_id`, `version`, `esquema`
        y `umbral` pueden ponerse a NULL explícitamente; por eso sus banderas
        `tocar_*` indican si el campo viene en la petición (aunque su valor sea
        None). Devuelve la fila o None si no existe. Lanza ValueError si el
        resultado no es válido.
        """
        actual = self.obtener_por_id(id_proc)
        if actual is None:
            return None

        if ruta is not None:
            n_ruta = self.normalizar_ruta(ruta)
            n_ruta_id = self._resolver_ruta_id(n_ruta)
        else:
            n_ruta = actual["ruta"]
            n_ruta_id = actual["ruta_id"]
        n_operacion = self.normalizar_operacion(operacion) if operacion is not None else actual["operacion"]
        n_clase = self.normalizar_clase(clase) if clase is not None else actual["clase"]
        n_modo = self.normalizar_modo(modo) if modo is not None else actual["modo"]
        n_procesador = actual["procesador_id"]
        if tocar_procesador_id:
            n_procesador = (procesador_id or "").strip() or None
        n_version = actual["version"]
        if tocar_version:
            n_version = (version or "").strip() or None
        n_esquema = actual["esquema"]
        if tocar_esquema:
            n_esquema = esquema
        n_umbral = actual["umbral"]
        if tocar_umbral:
            n_umbral = umbral
        n_activo = actual["activo"] if activo is None else bool(activo)

        self._validar(n_operacion, n_modo, n_procesador, n_esquema)
        self._validar_umbral(n_umbral)

        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE procesadores SET ruta_id = %s, operacion = %s, clase = %s, modo = %s, "
                    "procesador_id = %s, version = %s, esquema = %s, umbral = %s, activo = %s, "
                    "actualizado_en = now() WHERE id = %s",
                    (n_ruta_id, n_operacion, n_clase, n_modo, n_procesador, n_version,
                     Json(n_esquema) if n_esquema is not None else None, n_umbral, n_activo, id_proc),
                )
        cache.invalidar(CLAVE_CACHE)
        return self.obtener_por_id(id_proc)

    def eliminar(self, id_proc: int) -> bool:
        """Borra una fila. Devuelve False si el id no existía."""
        with self._conectar() as con:
            with con.cursor() as cur:
                cur.execute("DELETE FROM procesadores WHERE id = %s", (id_proc,))
                borrada = cur.rowcount > 0
        cache.invalidar(CLAVE_CACHE)
        return borrada

    def _filas_activas(self) -> list:
        """Todas las filas activas, cacheadas en Redis (centralizado). Es lo que
        consultan los resolutores en cada petición; evita golpear la BD cada vez."""
        return cache.obtener(CLAVE_CACHE, lambda: self.listar(solo_activos=True))

    def _obtener_activa(self, ruta: str, operacion: str, clase: str) -> Optional[dict]:
        """Fila activa para (ruta, operacion, clase) desde el cache, o None."""
        for fila in self._filas_activas():
            if fila["ruta"] == ruta and fila["operacion"] == operacion and fila["clase"] == clase:
                return fila
        return None

    @staticmethod
    def _ref_procesador(fila: dict) -> dict:
        """
        Referencia a un procesador publicado: {id, [version]}. La versión solo
        se incluye si la fila la fija; sin versión el body queda idéntico al de
        antes (Extend usa la última versión publicada). El nombre exacto del
        campo de versión conviene confirmarlo contra la cuenta de Extend.
        """
        ref = {"id": fila["procesador_id"]}
        if fila.get("version"):
            ref["version"] = fila["version"]
        return ref

    def _fragmento_clasificacion(self, fila: Optional[dict],
                                 construir_inline: Callable[[], list]) -> dict:
        """Fragmento de /classify a partir de la fila activa de 'clasificar' (o
        None). Ver `cuerpo_clasificacion` para el orden de prioridad."""
        if fila and fila["modo"] == "id" and fila["procesador_id"]:
            return {"classifier": self._ref_procesador(fila)}
        esquema = (fila or {}).get("esquema")
        propias = esquema.get("classifications") if isinstance(esquema, dict) else None
        if propias:
            return {"config": {"classifications": _normalizar_clasificaciones(propias)}}
        return {"config": {"classifications": construir_inline()}}

    @staticmethod
    def _umbral_de(fila: Optional[dict]) -> float:
        """Umbral de la fila de 'clasificar', o UMBRAL_DEFECTO si no lo fija."""
        if fila and fila.get("umbral") is not None:
            return float(fila["umbral"])
        return UMBRAL_DEFECTO

    def cuerpo_clasificacion(self, ruta: str, construir_inline: Callable[[], list]) -> dict:
        """
        Fragmento de body para /classify en la ruta dada, en orden de prioridad:
        1. modo 'id'  -> el clasificador publicado en Extend.
        2. modo 'inline' con clasificaciones propias en la fila (esquema =
           {"classifications": [{id, type, description}, ...]}) -> esas, con la
           clase de descarte 'other' garantizada. Permite clasificaciones
           distintas por ruta.
        3. sin esquema -> las clasificaciones globales de la tabla
           `clasificaciones` (callable, para no tocarla cuando no hace falta).
        """
        return self._fragmento_clasificacion(self._obtener_activa(ruta, "clasificar", ""),
                                              construir_inline)

    def umbral_clasificacion(self, ruta: str) -> float:
        """
        Confianza mínima (0..1) para dar por válida una clasificación en la
        ruta. La toma de la fila de 'clasificar'; si no tiene una configurada,
        usa UMBRAL_DEFECTO.
        """
        return self._umbral_de(self._obtener_activa(ruta, "clasificar", ""))

    def config_clasificacion(self, ruta: str,
                             construir_inline: Callable[[], list]) -> Tuple[dict, float]:
        """
        Fragmento de body para /classify y umbral de la ruta, leyendo la fila
        activa de 'clasificar' UNA sola vez. Evita la doble lectura de caché
        (fragmento + umbral) que hacía el flujo de inferencia por petición.
        """
        fila = self._obtener_activa(ruta, "clasificar", "")
        return self._fragmento_clasificacion(fila, construir_inline), self._umbral_de(fila)

    def cuerpo_extraccion(self, ruta: str, clase: str) -> Optional[dict]:
        """
        Fragmento de body para /extract en la ruta. Busca primero la fila
        específica de la clase y, si no hay, una fila global de extracción
        (clase ''). Usa el extractor publicado (modo 'id') o el JSON Schema
        inline. Devuelve None si no hay forma de extraer esa clase.
        """
        clase = self.normalizar_clase(clase)
        fila = (self._obtener_activa(ruta, "extraer", clase)
                or self._obtener_activa(ruta, "extraer", ""))
        if not fila:
            return None
        if fila["modo"] == "id" and fila["procesador_id"]:
            return {"processor": self._ref_procesador(fila)}
        if fila["modo"] == "inline" and fila["esquema"]:
            return {"config": {"schema": fila["esquema"]}}
        return None

    def soporta_extraccion(self, ruta: str, clase: str) -> bool:
        """¿Hay forma de extraer campos para esta clase en la ruta? (extractor o esquema)."""
        return self.cuerpo_extraccion(ruta, clase) is not None

    def cuerpo_parse(self, ruta: str) -> dict:
        """Fragmento de body para /parse (OCR) en la ruta. Usa el target configurado o markdown."""
        fila = self._obtener_activa(ruta, "parse", "")
        target = "markdown"
        if fila and isinstance(fila.get("esquema"), dict):
            target = fila["esquema"].get("target") or target
        return {"config": {"target": target}}

    async def listar_de_extend(self, tipo: str) -> list:
        """
        Lista los procesadores publicados en Extend Studio para una operación.
        `tipo` es 'clasificar' (CLASSIFY) o 'extraer' (EXTRACT). Devuelve
        [{id, nombre, tipo, versiones:[{id, version}]}]. Sirve para elegir el
        procesador en /admin en vez de pegar el id a mano.
        """
        tipo_extend = _TIPO_EXTEND.get((tipo or "").strip().lower())
        if not tipo_extend:
            raise ErrorDeValidacion("El tipo debe ser 'clasificar' o 'extraer'.")

        crudos = await extend.listar_procesadores(tipo_extend)
        return [{
            "id": p.get("id"),
            "nombre": p.get("name"),
            "tipo": p.get("type"),
            "versiones": [{"id": v.get("id"), "version": str(v.get("version"))}
                          for v in (p.get("versions") or [])],
        } for p in crudos]

    async def esquema_de_extend(self, procesador_id: str, version_id: str) -> dict:
        """
        Devuelve la configuración importable de una versión de un procesador
        publicado en Extend, para volcarla al schema builder:
            EXTRACT  -> el JSON Schema (version.config.schema)
            CLASSIFY -> las clasificaciones ({"classifications": [...]})
        Devuelve {} si la versión no trae nada importable.
        """
        pid = (procesador_id or "").strip()
        vid = (version_id or "").strip()
        if not pid or not vid:
            raise ErrorDeValidacion("Hace falta procesador_id y version_id.")
        version = await extend.obtener_version_procesador(pid, vid)
        config = version.get("config") or {}
        if config.get("schema"):
            return config["schema"]
        if config.get("classifications"):
            return {"classifications": config["classifications"]}
        return {}

    async def actualizar_en_extend(self, id_proc: int, publicar: bool = False) -> Optional[dict]:
        """
        Empuja el esquema GUARDADO de la fila a su procesador publicado en Extend
        (POST /processors/{id}): el JSON Schema para extractores (EXTRACT) o las
        clasificaciones para clasificadores (CLASSIFY). Actualiza la versión
        BORRADOR del procesador; con `publicar=True` además publica el borrador
        como versión nueva (release minor) — las rutas en 'última publicada' la
        usan de inmediato. Devuelve None si la fila no existe (-> 404).
        """
        fila = self.obtener_por_id(id_proc)
        if fila is None:
            return None
        pid = fila.get("procesador_id")
        if not pid:
            raise ErrorDeValidacion(
                "La fila no tiene un procesador de Extend asociado (procesador_id); "
                "no hay nada que actualizar en Extend."
            )
        esquema = fila.get("esquema")
        if fila["operacion"] == "clasificar":
            propias = esquema.get("classifications") if isinstance(esquema, dict) else None
            if not propias:
                raise ErrorDeValidacion(
                    "La fila no tiene clasificaciones guardadas para enviar a Extend."
                )
            config = {"type": "CLASSIFY",
                      "classifications": _normalizar_clasificaciones(propias)}
        elif fila["operacion"] == "extraer":
            if not esquema:
                raise ErrorDeValidacion("La fila no tiene esquema guardado para enviar a Extend.")
            config = {"type": "EXTRACT", "schema": esquema}
        else:
            raise ErrorDeValidacion(
                "Solo los clasificadores y extractores existen como procesadores en Extend."
            )

        datos = await extend.actualizar_procesador(pid, config)
        borrador = ((datos.get("processor") or datos).get("draftVersion") or {})
        resultado = {
            "procesador_id": pid,
            "operacion": fila["operacion"],
            "version_borrador": borrador.get("id"),
            "version_publicada": None,
        }
        if publicar:
            pub = await extend.publicar_procesador(pid, "minor")
            version = pub.get("version") or pub.get("processorVersion") or pub
            if isinstance(version, dict):
                resultado["version_publicada"] = version.get("version")
        return resultado


procesadores = ServicioProcesadores()
