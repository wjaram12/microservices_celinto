"""
Rutas HTTP de Google Workspace como APIRouter reutilizable.

Se expone como `api` para poder montarlo en DOS sitios:
  - en la app standalone (google_services/main.py), y

Capa HTTP delgada: traduce los errores de dominio del cliente a códigos HTTP. Cada
endpoint declara su propia dependencia de auth, así funciona igual montado donde
sea: el flujo de los sistemas consumidores (leer el directorio, procesar/crear
personas, vínculos, miembros de grupos) basta con una clave válida — es PARA los
clientes, no deben necesitar una clave admin del clasificador. El scope admin queda
para lo que no es de clientes: el CRUD crudo de usuarios (proxy del Admin SDK).

SIN CACHÉ: toda petición consulta el Admin SDK en vivo. El directorio es la única
fuente de verdad y una lista de grupos u OUs cacheada puede estar desactualizada
justo cuando se necesita decidir sobre una cuenta. Se paga la latencia (~0,6 s al
listar los grupos del dominio) a cambio de no servir nunca un dato viejo.
"""
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from commons.seguridad import requiere_admin, verificar_api_key

from . import auditoria, nomenclatura
from .cliente import obtener_directorio
from .config import settings
from .errores import (
    ErrorDeConfiguracion, ErrorDeConflicto, ErrorDeGoogle, ErrorDeValidacion,
)
from .jerarquia import principal
from .schemas import (
    RespuestaAuditar, RespuestaConfirmacion, RespuestaCorreoSugerido,
    RespuestaCrearPersona, RespuestaEliminacion, RespuestaEstadoVinculos,
    RespuestaGrupos, RespuestaListaUsuarios, RespuestaMiembro, RespuestaMiembros,
    RespuestaPersona, RespuestaProcesar, RespuestaUnidades, RespuestaUsuario,
    RespuestaVinculo, SolicitudActualizarUsuario, SolicitudAgregarMiembro,
    SolicitudAuditar, SolicitudCrearPersona, SolicitudCrearUsuario, SolicitudProcesar,
    SolicitudVincular, validar_dominio,
)
from .vinculos import vinculos

logger = logging.getLogger(__name__)

api = APIRouter(tags=["Google Workspace"])


def verificar_credenciales() -> None:
    """Prepara el servicio al arrancar: crea la tabla de vínculos si falta y comprueba
    (consulta al propio admin delegado). Así los problemas se ven en el log del
    arranque y no en la primera petición de un usuario.

    Best-effort: NO tumba el servidor si falla; los endpoints responderán 500 con
    el motivo. Llamar en el startup del servidor que monte estas rutas."""
    logger.info("Google Workspace -> dominio=%s admin_delegado=%s",
                settings.GOOGLE_DOMINIO, settings.GOOGLE_ADMIN_DELEGADO)
    try:
        vinculos.inicializar()
        logger.info("Tabla de vínculos lista.")
    except Exception:
        logger.exception("No se pudo preparar la tabla google_vinculos.")
    try:
        obtener_directorio().usuarios.obtener(settings.GOOGLE_ADMIN_DELEGADO)
        logger.info("Credenciales de Google verificadas (delegación activa).")
    except Exception:
        logger.exception(
            "No se pudo verificar el acceso al Admin SDK. Revisa el JSON del service "
            "account (GOOGLE_SA_FILE) y que los scopes estén autorizados en la consola "
            "de admin de Google para %s.", settings.GOOGLE_ADMIN_DELEGADO)


def _traducir(e: Exception) -> HTTPException:
    """Errores de dominio -> HTTP. Cualquier otra cosa es un fallo nuestro (500)."""
    if isinstance(e, ErrorDeValidacion):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, ErrorDeConflicto):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, ErrorDeConfiguracion):
        return HTTPException(status_code=500, detail=str(e))
    if isinstance(e, ErrorDeGoogle):
        return HTTPException(status_code=502, detail=str(e))
    logger.exception("Error inesperado en el servicio de Google Workspace.")
    return HTTPException(status_code=500, detail="Error interno del servicio de Google Workspace.")


# --------------------------------------------------------------------------- #
# Usuarios
# --------------------------------------------------------------------------- #

@api.get("/google-services/usuarios/", response_model=RespuestaListaUsuarios,
         dependencies=[Depends(verificar_api_key)])
def listar_usuarios(
    consulta: str = Query("", description="Filtro con la sintaxis del Admin SDK, "
                                          "p. ej. `email:juan*` u `orgUnitPath=/Docentes`."),
    max_resultados: int = Query(100, ge=1, le=500),
):
    """Lista usuarios del dominio, opcionalmente filtrados."""
    try:
        usuarios = obtener_directorio().usuarios.filtrar(max_resultados, consulta)
    except Exception as e:
        raise _traducir(e)

    return RespuestaListaUsuarios(
        result=bool(usuarios),
        message=f"Se hallaron {len(usuarios)} usuario(s).",
        status="encontrado" if usuarios else "no_encontrado",
        total=len(usuarios),
        usuarios=usuarios,
    )


@api.get("/google-services/usuarios/{clave_usuario}", response_model=RespuestaUsuario,
         dependencies=[Depends(verificar_api_key)])
def obtener_usuario(clave_usuario: str):
    """Datos de un usuario por su correo o su ID de Google. 404 si no existe."""
    try:
        usuario = obtener_directorio().usuarios.obtener_con_reintentos(clave_usuario)
    except Exception as e:
        raise _traducir(e)

    if usuario is None:
        raise HTTPException(status_code=404,
                            detail=f"El usuario '{clave_usuario}' no existe en Google Workspace.")

    return RespuestaUsuario(
        result=True,
        message=f"Usuario '{clave_usuario}' encontrado.",
        status="encontrado",
        usuario=usuario,
    )


@api.post("/google-services/usuarios/", response_model=RespuestaUsuario, status_code=201,
          dependencies=[Depends(requiere_admin)])
def crear_usuario(datos: SolicitudCrearUsuario):
    """
    Crea una cuenta en el dominio y la ubica en su unidad organizativa.

    Es una operación *read-before-write*: si la cuenta ya existe se responde 409 en
    vez de dejar que Google devuelva un error opaco. La comprobación usa reintentos,
    así que una caída de Google da 502 y nunca un falso "no existe".
    """
    try:
        correo = validar_dominio(datos.primaryEmail)
        directorio = obtener_directorio()

        if directorio.usuarios.obtener_con_reintentos(correo) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"La cuenta '{correo}' ya existe en Google Workspace.")

        cuerpo = datos.model_dump(exclude_none=True)
        cuerpo["primaryEmail"] = correo
        usuario = directorio.usuarios.crear(cuerpo)
    except HTTPException:
        raise
    except Exception as e:
        raise _traducir(e)

    return RespuestaUsuario(
        result=True,
        message=f"Cuenta '{correo}' creada en la unidad '{datos.orgUnitPath}'.",
        status="creado",
        usuario=usuario,
    )


@api.patch("/google-services/usuarios/{clave_usuario}", response_model=RespuestaUsuario,
           dependencies=[Depends(requiere_admin)])
def actualizar_usuario(clave_usuario: str, datos: SolicitudActualizarUsuario):
    """Actualiza los campos enviados de un usuario (los omitidos no se tocan).
    Sirve, entre otras cosas, para resetear la contraseña o suspender la cuenta."""
    campos = datos.model_dump(exclude_unset=True, exclude_none=True)
    if not campos:
        raise HTTPException(status_code=400, detail="No se envió ningún campo que actualizar.")

    try:
        usuario = obtener_directorio().usuarios.actualizar(clave_usuario, campos)
    except Exception as e:
        raise _traducir(e)

    # Los nombres de los campos no son secretos; sus valores (password) sí.
    return RespuestaUsuario(
        result=True,
        message=f"Usuario '{clave_usuario}' actualizado ({', '.join(sorted(campos))}).",
        status="actualizado",
        usuario=usuario,
    )


@api.delete("/google-services/usuarios/{clave_usuario}", response_model=RespuestaEliminacion,
            dependencies=[Depends(requiere_admin)])
def eliminar_usuario(clave_usuario: str):
    """Elimina una cuenta del dominio. Si no existía responde 200 con
    status='no_encontrado' (la operación es idempotente)."""
    try:
        borrado = obtener_directorio().usuarios.eliminar(clave_usuario)
    except Exception as e:
        raise _traducir(e)

    return RespuestaEliminacion(
        result=borrado,
        message=(f"Cuenta '{clave_usuario}' eliminada." if borrado
                 else f"La cuenta '{clave_usuario}' no existía en Google Workspace."),
        status="eliminado" if borrado else "no_encontrado",
        email=clave_usuario,
    )


# --------------------------------------------------------------------------- #
# Personas: el vínculo cédula <-> cuentas de Google
#
# Es la puerta por la que entran los tres sistemas consumidores. Hablan de CÉDULA,
# nunca de correo ni de nombre: el correo colisiona (dos personas distintas con la
# misma dirección `nombre.apellido`) y el nombre no identifica (231 nombres
# repetidos en el dominio). La cédula es la única llave que no miente.
# --------------------------------------------------------------------------- #

def _cedula_de(usuario: dict):
    for e in (usuario.get("externalIds") or []):
        if (e.get("customType") or e.get("type")) == "identificacion":
            return (e.get("value") or "").strip() or None
    return None


@api.get("/google-services/personas/{identificacion}", response_model=RespuestaPersona,
         dependencies=[Depends(verificar_api_key)])
def obtener_persona(
    identificacion: str,
    verificar: bool = Query(
        False, description="Contrasta el registro contra Google en vivo. Cuesta ~0,5 s "
                           "por cuenta; sin esto la respuesta sale de la tabla (~10 ms)."),
):
    """
    Cuentas de Google de una persona, buscadas por su cédula.

    Responde desde la tabla de vínculos, que es un índice de lo que hay en Google.
    Con `verificar=true` se comprueba cuenta por cuenta contra el directorio y se
    informa de cualquier divergencia (una cuenta borrada, un correo cambiado, la
    cédula quitada a mano). La tabla nunca gana: si discrepan, manda Google.

    Devuelve TODAS sus cuentas, no una: una misma persona puede ser docente y
    estudiante a la vez. La principal va primero.
    """
    cedula = (identificacion or "").strip()
    if not cedula:
        raise HTTPException(status_code=400, detail="La cédula es obligatoria.")

    try:
        registradas = vinculos.por_cedula(cedula)
    except Exception as e:
        raise _traducir(e)

    if not registradas:
        return RespuestaPersona(
            result=False,
            message=f"No hay ninguna cuenta registrada para la cédula '{cedula}'.",
            status="no_encontrada", identificacion=cedula)

    comprobacion = None
    if verificar:
        try:
            usuarios = obtener_directorio().usuarios
            diferencias = []
            for v in registradas:
                u = usuarios.obtener_con_reintentos(v["google_id"])
                if u is None:
                    diferencias.append({"google_id": v["google_id"],
                                        "problema": "la cuenta ya no existe en Google"})
                    continue
                if (u.get("primaryEmail") or "").lower() != v["email"]:
                    diferencias.append({"google_id": v["google_id"],
                                        "problema": "el correo cambió en Google",
                                        "tabla": v["email"], "google": u.get("primaryEmail")})
                if _cedula_de(u) != cedula:
                    diferencias.append({"google_id": v["google_id"],
                                        "problema": "la cédula en Google no coincide",
                                        "google": _cedula_de(u)})
            comprobacion = {"coherente": not diferencias, "diferencias": diferencias}
        except Exception as e:
            raise _traducir(e)

    return RespuestaPersona(
        result=True,
        message=f"{len(registradas)} cuenta(s) registrada(s) para '{cedula}'.",
        status="encontrada", identificacion=cedula,
        total=len(registradas), cuentas=registradas, verificacion=comprobacion)


# Qué debe hacer el sistema cliente con cada veredicto. Se devuelve junto al estado
# para que el consumidor no tenga que reimplementar esta tabla en tres sitios.
ACCIONES = {
    "vinculada": "Nada. La persona ya tiene su cuenta y su cédula registrada.",
    "existe_sin_cedula": "Vincular la cuenta con POST /personas/{cedula}/vinculos.",
    "corregir_formato": "Vincular de nuevo: se normalizará la cédula mal escrita.",
    "conflicto_cedula": "Revisión humana. NO sobrescribir: una de las dos cédulas es "
                        "errónea y hay que averiguar cuál.",
    "revisar_multicuenta": "Revisión humana. Confirmar en cuál de sus cuentas debe ir "
                           "la cédula.",
    "ambigua": "Revisión humana. Varias cuentas activas con la misma jerarquía.",
    "correo_ocupado": "Corregir el correo en el sistema de origen: pertenece a otra "
                      "persona.",
    "solo_cuentas_inactivas": "Decidir si se reactiva la cuenta archivada o se crea una "
                              "nueva.",
    "disponible": "Crear la cuenta con POST /personas/.",
    "cedula_invalida": "Cargar la cédula real en el sistema de origen antes de migrar.",
}


@api.post("/google-services/personas/auditar", response_model=RespuestaAuditar,
          dependencies=[Depends(verificar_api_key)])
def auditar_persona(datos: SolicitudAuditar):
    """
    Veredicto sobre una persona frente a Google. **Solo lee: no crea ni modifica nada.**

    Es la versión individual y en vivo del informe que produce el backfill masivo.
    Comparte con él las reglas de identidad y de jerarquía, así que un mismo caso
    recibe el mismo estado por las dos vías.

    Sirve para dos cosas: saber si hay que crear una cuenta antes de llamar a
    `POST /personas/`, y detectar que el correo guardado en el sistema de origen
    pertenece a otra persona (`estado=correo_ocupado`).

    Cuesta entre 1 y 4 llamadas a Google (~0,5 s cada una) según lo lejos que haya
    que bajar en la escalera: cédula, luego correo, luego nombre. Si solo necesitas
    saber si la persona ya tiene cuenta, usa `GET /personas/{cedula}`, que responde
    en 2 ms desde el índice.
    """
    try:
        r = auditoria.auditar(datos.nombres, datos.apellidos,
                              datos.identificacion, datos.correo or "")
    except Exception as e:
        raise _traducir(e)

    hallada = r["estado"] not in ("disponible", "cedula_invalida")
    return RespuestaAuditar(
        result=hallada, message=r["detalle"], estado=r["estado"],
        accion_sugerida=ACCIONES[r["estado"]],
        identificacion=r["identificacion"], metodo=r["metodo"],
        cuenta=r["cuenta"], otras_cuentas=r["otras_cuentas"],
        correo_ajeno=r["correo_ajeno"], detalle=r["detalle"])


# Estados en los que ningún automatismo es seguro: los decide una persona.
REVISION = ("conflicto_cedula", "revisar_multicuenta", "ambigua",
            "solo_cuentas_inactivas", "cedula_invalida")

# Estados en los que la cuenta existe y solo falta escribirle la cédula.
VINCULABLES = ("existe_sin_cedula", "corregir_formato")


def _validar_destino(directorio, org_unit: str, grupos: list) -> None:
    """La unidad y los grupos deben existir ANTES de tocar nada. Si no, Google responde
    404 a mitad del alta, el traductor lo convierte en 502, y el sistema cliente cree
    que Google falló cuando el error es una ruta mal escrita."""
    if not directorio.unidades.existe(org_unit):
        raise HTTPException(
            status_code=400,
            detail=f"La unidad organizativa '{org_unit}' no existe. Consulta las válidas "
                   "en GET /google-services/unidades/.")
    for grupo in grupos:
        if not directorio.grupos.existe(grupo):
            raise HTTPException(
                status_code=400,
                detail=f"El grupo '{grupo}' no existe. Consulta los válidos en "
                       "GET /google-services/grupos/.")


def _password_para(cedula: str) -> str:
    """Contraseña inicial según la política configurada. La cuenta siempre se crea con
    `changePasswordAtNextLogin`, así que solo protege hasta el primer acceso."""
    if settings.GOOGLE_PASSWORD_INICIAL == "aleatoria":
        return secrets.token_urlsafe(12)
    return cedula


def _cuentas_por_cedula_en_google(cedula: str) -> list:
    """Cuentas que llevan esta cédula, preguntando a GOOGLE (no a la tabla).

    La relectura va contra Google a propósito: la tabla es un índice y puede ir por
    detrás. Antes de crear una cuenta hay que mirar la fuente de verdad, o se crean
    duplicados cuando el índice está frío.
    """
    usuarios = obtener_directorio().usuarios
    hallados = usuarios.filtrar(max_resultados=10, consulta=f"externalId={cedula}")
    # `externalId=` ignora el customType: se confirma que el valor es realmente la
    # cédula de la persona y no otro identificador que coincide.
    return [u for u in hallados if _cedula_de(u) == cedula]


@api.post("/google-services/personas/procesar", response_model=RespuestaProcesar)
def procesar_persona(datos: SolicitudProcesar, quien: dict = Depends(verificar_api_key)):
    """
    Audita a una persona y **actúa** según el veredicto. Devuelve `migrado`.

    Es el proceso completo en una llamada. Reglas:

    | Veredicto | Acción |
    |---|---|
    | `vinculada` | Nada. Ya tiene cuenta y cédula. `migrado=true` |
    | `existe_sin_cedula` | Escribe la cédula en su cuenta. `migrado=true` |
    | `corregir_formato` | Normaliza la cédula mal escrita. `migrado=true` |
    | `conflicto_cedula` | **Nada.** `migrado=false`, revisión humana |
    | `revisar_multicuenta` | **Nada.** `migrado=false`, revisión humana |
    | `ambigua` | **Nada.** `migrado=false`, revisión humana |
    | `solo_cuentas_inactivas` | **Nada.** `migrado=false`, revisión humana |
    | `cedula_invalida` | **Nada.** `migrado=false`, revisión humana |
    | `correo_ocupado` | Busca una dirección libre y crea la cuenta |
    | `disponible` | Crea la cuenta |

    Solo crea si se envía `orgUnitPath`. Sin él, un `disponible` devuelve
    `accion='crear'` con el correo sugerido, y no se toca nada.

    **Sobre `correo_ocupado`:** ese veredicto solo dice que el correo enviado es de
    otra persona; no dice que la persona no tenga cuenta bajo otra dirección. Antes de
    crear nada se vuelve a auditar SIN el correo. Si entonces aparece su cuenta, se
    aplica la regla de ese otro veredicto. Sin esta comprobación se crearía la segunda
    cuenta de alguien que ya tiene una.
    """
    cedula = (datos.identificacion or "").strip()
    if not cedula:
        raise HTTPException(status_code=400, detail="La cédula es obligatoria.")

    propuesto = (datos.correo or "").strip().lower()

    try:
        directorio = obtener_directorio()
        if datos.orgUnitPath:
            _validar_destino(directorio, datos.orgUnitPath, datos.grupos)

        with vinculos.bloquear(cedula):
            v = auditoria.auditar(datos.nombres, datos.apellidos, cedula, propuesto)

            # `correo_ajeno` es independiente del veredicto: la auditoría lo señala
            # tanto si la persona tiene cuenta (bajo otra dirección) como si no. Por
            # eso NO basta con mirar estado=='correo_ocupado', que solo se emite en el
            # segundo caso. Si lo hiciéramos, crearíamos la segunda cuenta de alguien
            # que ya tiene una.
            correo_ajeno = v["correo_ajeno"] is not None
            ocupados = []
            if correo_ajeno:
                ocupados.append({"correo": propuesto,
                                 "pertenece_a": v["correo_ajeno"]["nombre"]})

            estado = v["estado"]
            cuenta = v["cuenta"] or {}

            # --- No se toca nada: decide un humano ---
            if estado in REVISION:
                return RespuestaProcesar(
                    result=False, message=v["detalle"], migrado=False, estado=estado,
                    accion="requiere_revision", requiere_revision=True,
                    identificacion=cedula, correo=cuenta.get("email", ""),
                    google_id=cuenta.get("google_id", ""), ou=cuenta.get("ou", ""),
                    correo_en_uso=correo_ajeno, correo_propuesto=datos.correo,
                    ocupados=ocupados, otras_cuentas=v["otras_cuentas"],
                    detalle=v["detalle"])

            # --- Ya estaba migrada ---
            if estado == "vinculada":
                vinculos.registrar(cedula, cuenta["google_id"], cuenta["email"],
                                   cuenta["ou"], quien["consumidor"], True, "sincronizacion")
                distinto = bool(propuesto) and propuesto != cuenta["email"]
                return RespuestaProcesar(
                    result=True, message="Ya estaba migrada: su cuenta lleva la cédula.",
                    migrado=True, estado=estado, accion="ninguna",
                    identificacion=cedula, correo=cuenta["email"],
                    google_id=cuenta["google_id"], ou=cuenta["ou"],
                    correo_en_uso=correo_ajeno or distinto,
                    actualizar_en_origen=distinto, correo_propuesto=datos.correo,
                    ocupados=ocupados, otras_cuentas=v["otras_cuentas"],
                    detalle=v["detalle"])

            # --- La cuenta existe: solo hay que escribirle la cédula ---
            if estado in VINCULABLES:
                # La BASE PRIMERO, y no Google. Es la única que puede arbitrar que una
                # cuenta pertenezca a una sola persona: el cerrojo va por cédula, así
                # que dos homónimos con cédulas distintas no se serializan entre sí.
                # Si otra cédula ya reclamó esta cuenta, esto lanza 409 y Google queda
                # intacto. Al revés, habríamos pisado la cédula del otro.
                vinculos.registrar(cedula, cuenta["google_id"], cuenta["email"],
                                   cuenta["ou"], quien["consumidor"], True, "creacion")
                directorio.usuarios.establecer_external_id(
                    cuenta["google_id"], cedula, "identificacion")
                distinto = bool(propuesto) and propuesto != cuenta["email"]
                return RespuestaProcesar(
                    result=True,
                    message=f"Cédula registrada en la cuenta '{cuenta['email']}'.",
                    migrado=True, estado=estado, accion="vinculada",
                    identificacion=cedula, correo=cuenta["email"],
                    google_id=cuenta["google_id"], ou=cuenta["ou"],
                    correo_en_uso=correo_ajeno or distinto,
                    actualizar_en_origen=distinto, correo_propuesto=datos.correo,
                    ocupados=ocupados, otras_cuentas=v["otras_cuentas"],
                    detalle=v["detalle"])

            # --- No tiene cuenta: hay que crearla ---
            # Si el correo enviado estaba libre se usa; si era de otro (o no vino),
            # el servicio propone la siguiente dirección de la nomenclatura.
            correo = ""
            if propuesto and not correo_ajeno:
                validar_dominio(propuesto)
                if directorio.usuarios.obtener(propuesto) is None:
                    correo = propuesto
            if not correo:
                sugerido = nomenclatura.sugerir(datos.nombres, datos.apellidos)
                correo = sugerido["correo"]
                ocupados.extend(sugerido["ocupados"])

            if not datos.orgUnitPath:
                return RespuestaProcesar(
                    result=False,
                    message=f"Hay que crear la cuenta. Dirección libre: {correo}. "
                            "Reenvía con `orgUnitPath` para crearla.",
                    migrado=False, estado=estado, accion="crear",
                    identificacion=cedula, correo=correo,
                    correo_en_uso=correo_ajeno, actualizar_en_origen=True,
                    correo_propuesto=datos.correo, ocupados=ocupados,
                    detalle=v["detalle"])

            password = _password_para(cedula)
            nueva = directorio.usuarios.crear({
                "primaryEmail": correo,
                "name": {"givenName": datos.nombres, "familyName": datos.apellidos},
                "password": password,
                "changePasswordAtNextLogin": True,
                "orgUnitPath": datos.orgUnitPath,
            })
            directorio.usuarios.establecer_external_id(nueva["id"], cedula, "identificacion")
            for grupo in datos.grupos:
                try:
                    directorio.grupos.agregar_miembro(grupo, correo)
                except Exception:
                    logger.exception("No se pudo añadir '%s' al grupo '%s'.", correo, grupo)
            vinculos.registrar(cedula, nueva["id"], correo, datos.orgUnitPath,
                               quien["consumidor"], True, "creacion")

    except HTTPException:
        raise
    except Exception as e:
        raise _traducir(e)

    mensaje = f"Cuenta '{correo}' creada en '{datos.orgUnitPath}'."
    if correo_ajeno:
        mensaje += (f" El correo '{datos.correo}' pertenece a otra persona; actualiza "
                    "tu registro.")
    return RespuestaProcesar(
        result=True, message=mensaje, migrado=True, estado=estado, accion="creada",
        identificacion=cedula, correo=correo, google_id=nueva["id"],
        ou=datos.orgUnitPath, correo_en_uso=correo_ajeno,
        actualizar_en_origen=bool(propuesto) and propuesto != correo,
        correo_propuesto=datos.correo, ocupados=ocupados, password_inicial=password,
        detalle=v["detalle"])


@api.post("/google-services/personas/", response_model=RespuestaCrearPersona,
          status_code=201)
def crear_persona(datos: SolicitudCrearPersona, respuesta: Response,
                  quien: dict = Depends(verificar_api_key)):
    """
    Crea la cuenta de Google de una persona. **Idempotente por cédula.**

    Si la persona ya tiene cuenta, NO crea otra: responde 200 con `status=ya_existia`.
    Por eso un reintento tras un timeout es seguro, y por eso los tres sistemas pueden
    pedir el alta de la misma persona sin coordinarse entre ellos.

    Toda la operación corre bajo un cerrojo por cédula en PostgreSQL. Sin él, dos
    sistemas que dan de alta a la misma persona a la vez comprueban «¿existe?» los dos
    a la vez, ven que no, y crean dos cuentas con direcciones distintas. No es
    hipotético: 4 993 personas figuran en dos de los sistemas y 143 en los tres.

    Sobre el correo: si `correo_propuesto` está libre se usa; si pertenece a otra
    persona se asigna la siguiente dirección de la nomenclatura y se marca
    `correo_en_uso=true`. En cualquier caso, `actualizar_en_origen` indica si el
    sistema cliente debe corregir el correo que tiene guardado.
    """
    cedula = (datos.identificacion or "").strip()
    if not cedula:
        raise HTTPException(status_code=400, detail="La cédula es obligatoria.")

    try:
        directorio = obtener_directorio()
        usuarios = directorio.usuarios

        _validar_destino(directorio, datos.orgUnitPath, datos.grupos)

        # El cerrojo dura toda la transacción: el segundo sistema espera aquí y, al
        # entrar, encontrará la cuenta ya creada por el primero.
        with vinculos.bloquear(cedula):
            existentes = _cuentas_por_cedula_en_google(cedula)
            if existentes:
                cuenta = principal(existentes)
                correo = (cuenta.get("primaryEmail") or "").lower()
                fila = vinculos.registrar(
                    identificacion=cedula, google_id=cuenta["id"], email=correo,
                    ou=cuenta.get("orgUnitPath") or "/", consumidor=quien["consumidor"],
                    principal=True, origen="sincronizacion")
                previo = (datos.correo_propuesto or "").strip().lower()
                respuesta.status_code = 200
                return RespuestaCrearPersona(
                    result=True,
                    message=f"La persona ya tenía cuenta: {correo}. No se creó ninguna.",
                    status="ya_existia", identificacion=cedula, correo=correo,
                    google_id=cuenta["id"], orgUnitPath=fila["ou"],
                    correo_en_uso=bool(previo) and previo != correo,
                    actualizar_en_origen=bool(previo) and previo != correo,
                    correo_propuesto=datos.correo_propuesto)

            # No existe: hay que elegir dirección. Se recalcula aquí dentro aunque el
            # cliente ya la hubiera consultado: pudo ocuparse entre las dos llamadas.
            ocupados, correo_en_uso = [], False
            propuesto = (datos.correo_propuesto or "").strip().lower()
            correo = ""
            if propuesto:
                validar_dominio(propuesto)
                duenio = usuarios.obtener(propuesto)
                if duenio is None:
                    correo = propuesto
                else:
                    correo_en_uso = True
                    ocupados.append({
                        "correo": propuesto,
                        "pertenece_a": (duenio.get("name") or {}).get("fullName") or ""})
            if not correo:
                sugerido = nomenclatura.sugerir(datos.nombres, datos.apellidos)
                correo = sugerido["correo"]
                ocupados.extend(sugerido["ocupados"])

            password = _password_para(cedula)
            cuenta = usuarios.crear({
                "primaryEmail": correo,
                "name": {"givenName": datos.nombres, "familyName": datos.apellidos},
                "password": password,
                "changePasswordAtNextLogin": True,
                "orgUnitPath": datos.orgUnitPath,
            })

            # La cédula, en Google, para que el vínculo viaje con la cuenta.
            usuarios.establecer_external_id(cuenta["id"], cedula, "identificacion")

            # Los grupos toleran el 404 de propagación (el usuario acaba de nacer).
            grupos_asignados = []
            for grupo in datos.grupos:
                try:
                    r = obtener_directorio().grupos.agregar_miembro(grupo, correo)
                    grupos_asignados.append({"grupo": grupo, "resultado": r["status"]})
                except Exception as e:
                    logger.exception("No se pudo añadir '%s' al grupo '%s'.", correo, grupo)
                    grupos_asignados.append({"grupo": grupo, "resultado": "error",
                                             "detalle": str(e)[:160]})

            vinculos.registrar(
                identificacion=cedula, google_id=cuenta["id"], email=correo,
                ou=datos.orgUnitPath, consumidor=quien["consumidor"],
                principal=True, origen="creacion")

    except HTTPException:
        raise
    except Exception as e:
        raise _traducir(e)

    mensaje = f"Cuenta '{correo}' creada en '{datos.orgUnitPath}'."
    if correo_en_uso:
        mensaje += (f" El correo '{datos.correo_propuesto}' que enviaste pertenece a otra "
                    "persona; actualiza tu registro.")
    return RespuestaCrearPersona(
        result=True, message=mensaje, status="creada", identificacion=cedula,
        correo=correo, google_id=cuenta["id"], orgUnitPath=datos.orgUnitPath,
        correo_en_uso=correo_en_uso,
        actualizar_en_origen=bool(propuesto) and propuesto != correo,
        correo_propuesto=datos.correo_propuesto, ocupados=ocupados,
        password_inicial=password, grupos_asignados=grupos_asignados)


@api.get("/google-services/personas/{identificacion}/confirmar",
         response_model=RespuestaConfirmacion, dependencies=[Depends(verificar_api_key)])
def confirmar_creacion(identificacion: str):
    """
    Comprueba que una cuenta recién creada ya está operativa en Google.

    Existe por una razón concreta: **Google devuelve lecturas obsoletas justo después
    de escribir**. Se verificó en producción — tras un `patch`, la lectura inmediata no
    mostraba el cambio y tres segundos más tarde sí.

    Por eso `status='propagando'` NO es un fallo: significa que la cuenta se creó pero
    Google todavía no la muestra por completo. Reintenta a los pocos segundos. Un
    sistema que trate el 404 inmediato como error concluirá que el alta falló cuando
    en realidad funcionó, y la creará otra vez.
    """
    cedula = (identificacion or "").strip()
    try:
        registradas = vinculos.por_cedula(cedula)
        if not registradas:
            return RespuestaConfirmacion(
                result=False, message=f"No hay ninguna cuenta registrada para '{cedula}'.",
                status="no_encontrada", identificacion=cedula)

        v = registradas[0]
        usuario = obtener_directorio().usuarios.obtener(v["google_id"])
        existe = usuario is not None
        con_cedula = existe and _cedula_de(usuario) == cedula
    except Exception as e:
        raise _traducir(e)

    listo = existe and con_cedula
    return RespuestaConfirmacion(
        result=listo,
        message=("La cuenta está operativa y su cédula registrada." if listo else
                 "Google todavía no refleja la cuenta por completo; reintenta en unos "
                 "segundos."),
        status="listo" if listo else "propagando",
        identificacion=cedula, correo=v["email"],
        existe_en_google=existe, cedula_registrada=con_cedula)


@api.post("/google-services/personas/{identificacion}/vinculos",
          response_model=RespuestaVinculo, status_code=201)
def vincular(identificacion: str, datos: SolicitudVincular,
             quien: dict = Depends(verificar_api_key)):
    """
    Registra que una cuenta de Google pertenece a una persona.

    Escribe en los DOS sitios: la cédula como `externalId` en la cuenta (Google es la
    fuente de verdad, el dato viaja con ella) y la fila en la tabla, que añade lo que
    Google no guarda: la fecha y el sistema que la registró.

    Idempotente: repetirla actualiza el correo y la unidad, pero conserva `creado_en`
    y el consumidor original — quien la registró primero es quien la creó.
    """
    cedula = (identificacion or "").strip()
    if not cedula:
        raise HTTPException(status_code=400, detail="La cédula es obligatoria.")

    try:
        usuarios = obtener_directorio().usuarios
        usuario = usuarios.obtener_con_reintentos(datos.google_id)
        if usuario is None:
            raise HTTPException(
                status_code=404,
                detail=f"La cuenta '{datos.google_id}' no existe en Google Workspace.")

        # La BASE primero: es la única que impide que dos personas reclamen la misma
        # cuenta (UNIQUE sobre google_id). Si otra cédula ya la tiene, esto lanza 409
        # y Google queda intacto. Al revés, habríamos pisado la cédula del otro y
        # luego descubierto el conflicto, con el dato ya corrompido.
        fila = vinculos.registrar(
            identificacion=cedula, google_id=datos.google_id,
            email=usuario.get("primaryEmail") or "", ou=usuario.get("orgUnitPath") or "/",
            consumidor=quien["consumidor"], principal=datos.principal, origen="manual")
        usuarios.establecer_external_id(datos.google_id, cedula, "identificacion")
    except HTTPException:
        raise
    except Exception as e:
        raise _traducir(e)

    return RespuestaVinculo(
        result=True,
        message=f"'{fila['email']}' vinculada a la cédula '{cedula}'.",
        status="vinculada", vinculo=fila)


@api.delete("/google-services/personas/{identificacion}/vinculos/{google_id}",
            response_model=RespuestaVinculo, dependencies=[Depends(verificar_api_key)])
def desvincular(identificacion: str, google_id: str):
    """Borra el vínculo de la TABLA. No toca Google: para quitar la cédula de la
    cuenta hay que hacerlo explícitamente, y así un borrado accidental del índice no
    destruye el dato bueno."""
    try:
        borrado = vinculos.olvidar(identificacion, google_id)
    except Exception as e:
        raise _traducir(e)
    return RespuestaVinculo(
        result=borrado,
        message=("Vínculo borrado del registro (la cédula sigue en Google)." if borrado
                 else "No había vínculo registrado con esos datos."),
        status="desvinculada" if borrado else "no_encontrada")


@api.get("/google-services/correos/sugerir", response_model=RespuestaCorreoSugerido,
         dependencies=[Depends(verificar_api_key)])
def sugerir_correo(
    nombres: str = Query(description="Nombres de pila, p. ej. 'JOSE NICOLAS'."),
    apellidos: str = Query(description="Apellidos, p. ej. 'CABALLERO FRANCO'."),
):
    """
    Primera dirección libre para una persona, según la nomenclatura del dominio.

    Prueba `nombre.apellido`, luego los peldaños de la escalera, comprobando cada uno
    en vivo contra Google. No crea nada.

    IMPORTANTE: llámalo solo DESPUÉS de comprobar con `GET /personas/{cedula}` que la
    persona no tiene ya una cuenta. Si no, a alguien que ya tiene `walter.jara@` se le
    propondrá `walterjavier.jara@`, porque su propia dirección figura como ocupada.

    La dirección puede dejar de estar libre entre esta llamada y la creación: el alta
    la vuelve a comprobar dentro de su cerrojo.
    """
    try:
        r = nomenclatura.sugerir(nombres, apellidos)
    except Exception as e:
        raise _traducir(e)

    mensaje = f"Dirección libre: {r['correo']}."
    if r["ocupados"]:
        mensaje += f" Se descartaron {len(r['ocupados'])} ya ocupada(s)."
    return RespuestaCorreoSugerido(
        result=True, message=mensaje, status="libre",
        correo=r["correo"], patron=r["patron"], intentos=r["intentos"],
        ocupados=r["ocupados"])


@api.get("/google-services/vinculos/estado", response_model=RespuestaEstadoVinculos,
         dependencies=[Depends(verificar_api_key)])
def estado_vinculos():
    """Cuántos vínculos hay, cuántas personas, y quién los registró.

    Clave válida (consumo) a propósito: los clientes lo usan como verificación
    try:
        r = vinculos.contar()
    except Exception as e:
        raise _traducir(e)
    return RespuestaEstadoVinculos(
        result=True,
        message=f"{r['vinculos']} vínculo(s) de {r['personas']} persona(s).",
        vinculos=r["vinculos"], personas=r["personas"],
        por_consumidor=r["por_consumidor"])


# --------------------------------------------------------------------------- #
# Unidades organizativas y grupos
# --------------------------------------------------------------------------- #

@api.get("/google-services/unidades/", response_model=RespuestaUnidades,
         dependencies=[Depends(verificar_api_key)])
def listar_unidades():
    """Árbol de unidades organizativas del dominio, ordenado por ruta. Consulta
    Google en vivo en cada petición."""
    try:
        unidades = obtener_directorio().unidades.listar_formateado()
    except Exception as e:
        raise _traducir(e)

    return RespuestaUnidades(
        result=bool(unidades),
        message=f"Se hallaron {len(unidades)} unidad(es) organizativa(s).",
        status="encontrado" if unidades else "no_encontrado",
        total=len(unidades),
        unidades=unidades,
    )


@api.get("/google-services/grupos/", response_model=RespuestaGrupos,
         dependencies=[Depends(verificar_api_key)])
def listar_grupos():
    """Grupos del dominio (correo y nombre), ordenados por correo. Consulta Google
    en vivo en cada petición; con muchos grupos tarda ~0,6 s por la paginación."""
    try:
        grupos = obtener_directorio().grupos.listar_formateado()
    except Exception as e:
        raise _traducir(e)

    return RespuestaGrupos(
        result=bool(grupos),
        message=f"Se hallaron {len(grupos)} grupo(s).",
        status="encontrado" if grupos else "no_encontrado",
        total=len(grupos),
        grupos=grupos,
    )


@api.get("/google-services/grupos/{email_grupo}/miembros", response_model=RespuestaMiembros,
         dependencies=[Depends(verificar_api_key)])
def listar_miembros(email_grupo: str):
    """Miembros de un grupo, con su rol. 404 si el grupo no existe (distinto de un
    grupo que existe pero está vacío, que devuelve una lista vacía)."""
    try:
        directorio = obtener_directorio()
        if not directorio.grupos.existe(email_grupo):
            raise HTTPException(status_code=404,
                                detail=f"El grupo '{email_grupo}' no existe.")
        miembros = directorio.grupos.listar_miembros(email_grupo)
    except HTTPException:
        raise
    except Exception as e:
        raise _traducir(e)

    return RespuestaMiembros(
        result=bool(miembros),
        message=f"El grupo '{email_grupo}' tiene {len(miembros)} miembro(s).",
        status="encontrado" if miembros else "vacio",
        grupo=email_grupo, total=len(miembros), miembros=miembros)


@api.delete("/google-services/grupos/{email_grupo}/miembros/{email_usuario}",
            response_model=RespuestaMiembro, dependencies=[Depends(verificar_api_key)])
def quitar_miembro(email_grupo: str, email_usuario: str):
    """Saca a un usuario de un grupo. Idempotente: si no era miembro responde 200 con
    status='no_era_miembro'."""
    try:
        quitado = obtener_directorio().grupos.quitar_miembro(email_grupo, email_usuario)
    except Exception as e:
        raise _traducir(e)

    return RespuestaMiembro(
        result=quitado,
        message=(f"'{email_usuario}' sacado del grupo '{email_grupo}'." if quitado
                 else f"'{email_usuario}' no era miembro de '{email_grupo}'."),
        status="quitado" if quitado else "no_era_miembro",
        grupo=email_grupo, email=email_usuario, rol="")


@api.post("/google-services/grupos/{email_grupo}/miembros", response_model=RespuestaMiembro,
          dependencies=[Depends(verificar_api_key)])
def agregar_miembro(email_grupo: str, datos: SolicitudAgregarMiembro):
    """Añade un usuario a un grupo. Idempotente: si ya era miembro responde 200 con
    status='ya_era_miembro'. Reintenta mientras Google propaga una cuenta recién creada."""
    try:
        resultado = obtener_directorio().grupos.agregar_miembro(
            email_grupo, datos.email, datos.rol)
    except Exception as e:
        raise _traducir(e)

    ya_era = resultado["status"] == "ya_era_miembro"
    return RespuestaMiembro(
        result=True,
        message=(f"'{datos.email}' ya pertenecía al grupo '{email_grupo}'." if ya_era
                 else f"'{datos.email}' añadido al grupo '{email_grupo}' como {datos.rol}."),
        status=resultado["status"],
        grupo=email_grupo,
        email=datos.email,
        rol=datos.rol,
    )
