"""
Modelos de entrada/salida (Pydantic) del servicio de Google Workspace.

Se mantiene la convención del repo: `result` (señal booleana principal), `message`
(texto para humanos, no parsear en código) y `status` (estado estructurado para
lógica de máquina).

Las solicitudes que crean o actualizan usuarios son PROXIES TRANSPARENTES hacia la
Directory API: se declaran los campos imprescindibles y se admiten los demás tal
cual (`extra="allow"`), para no tener que tocar este archivo cada vez que se quiera
enviar un campo más de los muchos que acepta Google (phones, organizations,
suspended, recoveryEmail...).
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .errores import ErrorDeValidacion


def validar_dominio(email: str) -> str:
    """Exige que el correo pertenezca al dominio institucional y lo normaliza.

    Google rechazaría igualmente un correo de otro dominio, pero con un 400 opaco;
    esto falla antes, con un mensaje claro y sin gastar una llamada a la API.
    """
    correo = (email or "").strip().lower()
    if not correo:
        raise ErrorDeValidacion("El correo del usuario es obligatorio.")
    if not correo.endswith("@" + settings.GOOGLE_DOMINIO):
        raise ErrorDeValidacion(
            f"El correo '{correo}' no pertenece al dominio @{settings.GOOGLE_DOMINIO}.")
    return correo


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #

class NombreUsuario(BaseModel):
    """Nombre del usuario tal como lo espera la Directory API."""
    givenName: str = Field(description="Nombres.")
    familyName: str = Field(description="Apellidos.")


class SolicitudCrearUsuario(BaseModel):
    """Cuerpo de POST /google-services/usuarios/ (proxy de users.insert)."""
    model_config = ConfigDict(extra="allow")

    primaryEmail: str = Field(
        description="Correo institucional del usuario. Debe ser del dominio configurado.")
    name: NombreUsuario
    password: str = Field(description="Contraseña inicial. No se registra en los logs.")
    changePasswordAtNextLogin: bool = Field(
        True, description="Obliga a cambiar la contraseña en el primer inicio de sesión.")
    orgUnitPath: str = Field(
        "/", description="Ruta de la unidad organizativa destino, p. ej. '/Estudiantes'.")


class SolicitudActualizarUsuario(BaseModel):
    """Cuerpo de PATCH /google-services/usuarios/{clave_usuario} (proxy de users.patch).

    Sin campos obligatorios: solo se envían a Google los que vengan en la petición.
    """
    model_config = ConfigDict(extra="allow")

    password: Optional[str] = Field(None, description="Nueva contraseña.")
    changePasswordAtNextLogin: Optional[bool] = None
    orgUnitPath: Optional[str] = Field(None, description="Mover el usuario a otra OU.")
    suspended: Optional[bool] = Field(None, description="Suspender o reactivar la cuenta.")


class SolicitudAgregarMiembro(BaseModel):
    """Cuerpo de POST /google-services/grupos/{email_grupo}/miembros."""
    email: str = Field(description="Correo del usuario a añadir al grupo.")
    rol: Literal["MEMBER", "MANAGER", "OWNER"] = Field(
        "MEMBER", description="Rol del usuario dentro del grupo.")


# --------------------------------------------------------------------------- #
# Salida
# --------------------------------------------------------------------------- #

class RespuestaUsuario(BaseModel):
    """Respuesta de las operaciones sobre un usuario concreto."""
    result: bool = Field(description="True si la operación se completó.")
    message: str = Field(
        description="Mensaje legible para humanos; no parsear en código (usa `status`).")
    status: Literal["encontrado", "creado", "actualizado"]
    usuario: dict = Field(
        default={}, description="Recurso del usuario tal como lo devuelve la Directory API.")


class RespuestaListaUsuarios(BaseModel):
    result: bool
    message: str
    status: Literal["encontrado", "no_encontrado"] = Field(
        description="'no_encontrado' si el filtro no devolvió ningún usuario.")
    total: int = 0
    usuarios: List[dict] = []


class RespuestaEliminacion(BaseModel):
    result: bool = Field(description="True si el usuario se eliminó de Google.")
    message: str
    status: Literal["eliminado", "no_encontrado"]
    email: str


class RespuestaUnidades(BaseModel):
    result: bool
    message: str
    status: Literal["encontrado", "no_encontrado"]
    total: int = 0
    unidades: List[dict] = Field(
        default=[], description="Cada OU con `ruta`, `nombre`, `descripcion` y `padre`.")


class RespuestaGrupos(BaseModel):
    result: bool
    message: str
    status: Literal["encontrado", "no_encontrado"]
    total: int = 0
    grupos: List[dict] = Field(default=[], description="Cada grupo con `email` y `nombre`.")


class SolicitudAuditar(BaseModel):
    """Cuerpo de POST /google-services/personas/auditar. Solo lee; no cambia nada."""
    identificacion: str = Field(description="Cédula o documento de la persona.")
    nombres: str = Field("", description="Nombres de pila. Ayuda a hallarla si el "
                                         "correo no coincide.")
    apellidos: str = Field("", description="Apellidos.")
    correo: Optional[str] = Field(
        None, description="El correo que el sistema cliente tiene registrado. Se usa "
                          "para detectar si pertenece a otra persona.")


class RespuestaAuditar(BaseModel):
    """
    Veredicto sobre una persona frente a Google. Los mismos estados que emite el
    informe masivo del backfill, para que un caso reciba la misma respuesta por
    cualquiera de las dos vías.
    """
    result: bool = Field(description="True si la persona tiene alguna cuenta identificada.")
    message: str
    estado: Literal[
        "vinculada", "existe_sin_cedula", "corregir_formato", "conflicto_cedula",
        "revisar_multicuenta", "ambigua", "correo_ocupado", "solo_cuentas_inactivas",
        "disponible", "cedula_invalida",
    ] = Field(description="Ver la guía: cada estado dice qué hacer a continuación.")
    accion_sugerida: str = Field(
        description="Qué debería hacer el sistema cliente con este veredicto.")
    identificacion: str
    metodo: str = Field(
        "", description="Cómo se la identificó: 'cedula', 'correo' o 'nombre'. Vacío si "
                        "no se halló.")
    cuenta: Optional[dict] = Field(
        None, description="Cuenta principal hallada, con `email`, `google_id`, `ou` y "
                          "`cedula_en_google`.")
    otras_cuentas: List[dict] = Field(
        default=[], description="Sus demás cuentas. Una persona puede ser docente y "
                                "estudiante a la vez.")
    correo_ajeno: Optional[dict] = Field(
        default=None,
        description="Si el `correo` enviado pertenece a OTRA persona, aquí va de quién "
                    "es. Se informa aunque la persona sí tenga cuenta bajo otra "
                    "dirección — en ese caso el `estado` no será 'correo_ocupado', pero "
                    "el sistema de origen igualmente guarda un dato equivocado.")
    detalle: str = ""


class SolicitudProcesar(BaseModel):
    """Cuerpo de POST /google-services/personas/procesar."""
    identificacion: str = Field(description="Cédula. Es la llave de todo el proceso.")
    nombres: str = Field(description="Nombres de pila.")
    apellidos: str = Field(description="Apellidos.")
    correo: Optional[str] = Field(
        None, description="El correo que el sistema cliente tiene registrado.")
    orgUnitPath: Optional[str] = Field(
        None,
        description="Unidad organizativa destino. **Solo se crea la cuenta si se envía.** "
                    "Sin esto, un veredicto 'disponible' devuelve `accion='crear'` con el "
                    "correo sugerido, pero no crea nada.")
    grupos: List[str] = Field(default=[], description="Grupos a los que añadir la cuenta.")


class RespuestaProcesar(BaseModel):
    """
    Resultado del proceso completo. El campo que el sistema cliente debe guardar es
    `migrado`; el resto explica por qué.
    """
    result: bool
    message: str

    migrado: bool = Field(
        description="TRUE solo si la persona acabó con una cuenta cuya cédula está "
                    "registrada en Google. Es lo que el sistema cliente guarda.")
    estado: str = Field(description="El veredicto de la auditoría que motivó la acción.")
    accion: Literal["ninguna", "vinculada", "creada", "crear", "requiere_revision"] = Field(
        description="Qué hizo el servicio. 'crear' significa que HAY que crearla pero no "
                    "se envió `orgUnitPath`, así que no se creó nada.")
    requiere_revision: bool = Field(
        default=False,
        description="TRUE cuando ningún automatismo es seguro: conflicto de cédulas, "
                    "varias cuentas, o datos inválidos. Un humano debe decidir.")

    identificacion: str
    correo: str = Field("", description="Dirección definitiva, o la sugerida si accion='crear'.")
    google_id: str = ""
    ou: str = ""

    correo_en_uso: bool = Field(
        default=False, description="El correo que enviaste pertenece a otra persona.")
    actualizar_en_origen: bool = Field(
        default=False, description="El correo definitivo difiere del tuyo: guárdalo.")
    correo_propuesto: Optional[str] = None
    ocupados: List[dict] = []

    password_inicial: Optional[str] = Field(
        default=None, description="Solo si la cuenta se acaba de crear.")
    otras_cuentas: List[dict] = []
    detalle: str = ""


class SolicitudCrearPersona(BaseModel):
    """Cuerpo de POST /google-services/personas/ (alta de cuenta, idempotente)."""
    identificacion: str = Field(
        description="Cédula o documento. Es la LLAVE de la operación: si ya existe una "
                    "cuenta con esta cédula, no se crea otra.")
    nombres: str = Field(description="Nombres de pila, p. ej. 'JOSE NICOLAS'.")
    apellidos: str = Field(description="Apellidos, p. ej. 'CABALLERO FRANCO'.")
    orgUnitPath: str = Field(
        description="Unidad organizativa destino, p. ej. '/Academico/Estudiantes'.")
    grupos: List[str] = Field(
        default=[], description="Correos de los grupos a los que añadir la cuenta.")
    correo_propuesto: Optional[str] = Field(
        default=None,
        description="La dirección que el sistema cliente tiene registrada. Si está "
                    "libre, se usa. Si está ocupada por otra persona, se asigna la "
                    "siguiente de la nomenclatura y se avisa con `correo_en_uso`. "
                    "Si se omite, se calcula desde el nombre.")


class RespuestaCrearPersona(BaseModel):
    """
    Resultado del alta. Diseñada para que el sistema cliente pueda CORREGIR su propio
    registro: `correo_en_uso` y `actualizar_en_origen` le dicen si su dato estaba mal.
    """
    result: bool = Field(description="True si la persona tiene cuenta al terminar.")
    message: str
    status: Literal["creada", "ya_existia"] = Field(
        description="'ya_existia' cuando la persona ya tenía cuenta: la operación es "
                    "idempotente y no crea una segunda.")
    identificacion: str
    correo: str = Field(description="Dirección DEFINITIVA de la cuenta.")
    google_id: str
    orgUnitPath: str = ""

    correo_en_uso: bool = Field(
        default=False,
        description="True si el `correo_propuesto` pertenece a OTRA persona. El sistema "
                    "cliente tiene un dato erróneo y debe corregirlo.")
    actualizar_en_origen: bool = Field(
        default=False,
        description="True si `correo` difiere de lo que el cliente envió (o de lo que "
                    "tenía). Es la señal para escribir `correo` en la tabla del cliente.")
    correo_propuesto: Optional[str] = Field(
        default=None, description="Lo que el cliente envió, para que pueda compararlo.")
    ocupados: List[dict] = Field(
        default=[], description="Direcciones descartadas y a quién pertenecen.")

    password_inicial: Optional[str] = Field(
        default=None,
        description="Solo al CREAR. Si la política es 'aleatoria', es la única vez que "
                    "se muestra. La cuenta exige cambiarla en el primer acceso.")
    grupos_asignados: List[dict] = Field(
        default=[], description="Cada grupo con su resultado: 'agregado' o 'ya_era_miembro'.")


class RespuestaConfirmacion(BaseModel):
    """Resultado de comprobar que una cuenta recién creada ya está operativa."""
    result: bool
    message: str
    status: Literal["listo", "propagando", "no_encontrada"] = Field(
        description="'propagando' significa que Google todavía no muestra la cuenta o su "
                    "cédula. NO es un fallo: reintenta en unos segundos.")
    identificacion: str
    correo: str = ""
    existe_en_google: bool = False
    cedula_registrada: bool = False


class SolicitudVincular(BaseModel):
    """Cuerpo de POST /google-services/personas/{identificacion}/vinculos."""
    google_id: str = Field(description="ID de Google de la cuenta a vincular.")
    principal: bool = Field(
        True, description="Si es la cuenta principal de la persona (una por cédula).")


class RespuestaPersona(BaseModel):
    """Cuentas de Google de una persona, identificada por su cédula."""
    result: bool = Field(description="True si la persona tiene alguna cuenta registrada.")
    message: str
    status: Literal["encontrada", "no_encontrada"]
    identificacion: str
    total: int = 0
    cuentas: List[dict] = Field(
        default=[],
        description="Cada cuenta con `email`, `google_id`, `ou`, `principal`, "
                    "`consumidor` (quién la registró) y `creado_en`. La principal va primero.")
    verificacion: Optional[dict] = Field(
        default=None,
        description="Solo si se pidió `verificar=true`: contraste de la tabla contra "
                    "Google, con `coherente` y las diferencias halladas.")


class RespuestaVinculo(BaseModel):
    result: bool
    message: str
    status: Literal["vinculada", "desvinculada", "no_encontrada"]
    vinculo: dict = {}


class RespuestaCorreoSugerido(BaseModel):
    """Primera dirección libre para una persona, según la nomenclatura del dominio."""
    result: bool
    message: str
    status: Literal["libre"]
    correo: str = Field(description="Dirección propuesta, comprobada libre en Google.")
    patron: str = Field(description="Peldaño de la escalera que se usó (parte local).")
    intentos: int = Field(description="Cuántas direcciones se probaron antes de esta.")
    ocupados: List[dict] = Field(
        default=[],
        description="Direcciones descartadas y a quién pertenecen. Sirve para explicar "
                    "por qué a esta persona le tocó una variante.")


class RespuestaEstadoVinculos(BaseModel):
    result: bool
    message: str
    vinculos: int
    personas: int
    por_consumidor: List[dict] = []


class RespuestaMiembro(BaseModel):
    result: bool
    message: str
    status: Literal["agregado", "ya_era_miembro", "quitado", "no_era_miembro"] = Field(
        description="Las cuatro son respuestas normales: añadir y quitar son operaciones "
                    "idempotentes, así que repetirlas no es un error.")
    grupo: str
    email: str
    rol: str = ""


class RespuestaMiembros(BaseModel):
    """Miembros de un grupo."""
    result: bool
    message: str
    status: Literal["encontrado", "vacio"] = Field(
        description="'vacio' es un grupo que existe pero no tiene miembros. Un grupo "
                    "inexistente devuelve 404, no esto.")
    grupo: str
    total: int = 0
    miembros: List[dict] = Field(
        default=[], description="Cada miembro con `email`, `rol`, `tipo` y `estado`.")
