"""
CLIENTE DEL ADMIN SDK — el corazón de esta app.

Habla con la Directory API de Google Workspace (usuarios, unidades organizativas y
grupos) autenticándose con un service account que impersona a un administrador del
dominio (domain-wide delegation): un service account no puede administrar el
directorio por sí mismo, siempre actúa "en nombre de" GOOGLE_ADMIN_DELEGADO.

Portado desde `emailing/services/google_client.py` del monolito Django
academico-sga-cg, con tres cambios:

1. Sin Django: la ruta del JSON sale de la config (settings.GOOGLE_SA_FILE,
   relativa a `services/`) en vez de settings.BASE_DIR.

2. Construcción PEREZOSA (obtener_directorio). El original construía el cliente en
   el __init__, o sea que importarlo tocaba disco y red. Aquí importar este módulo
   no hace nada: es lo que permite montar el router dentro del clasificador de
   forma tolerante a fallos (app/main.py) aunque falten las credenciales.

3. `obtener_con_reintentos` ya NO devuelve None cuando se agotan los reintentos.
   En el original, agotar los reintentos por un 503 devolvía None, exactamente lo
   mismo que devuelve un 404 "el usuario no existe" — y quien llamaba concluía que
   la cuenta no existía y la creaba igual. Aquí None significa SOLO 404; si Google
   no responde, se lanza ErrorDeGoogle.

a su valor natural (None, lista vacía o False).
"""
import logging
import threading
import time
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import API_NAME, API_VERSION, MAX_REINTENTOS, PAUSA, SCOPES, settings
from .errores import ErrorDeConfiguracion, ErrorDeGoogle

logger = logging.getLogger(__name__)

# services/ (google_services/cliente.py -> google_services/ -> services/)
_RAIZ = Path(__file__).resolve().parent.parent

# Errores transitorios: se reintentan. Un 400 (petición mal formada) o un 403 de
# permisos no mejoran reintentando; ojo, un 403 de CUOTA sí (ver _es_cuota).
REINTENTABLES = (429, 500, 503)


def _es_cuota(e: HttpError) -> bool:
    """¿Es un 403 de límite de cuota y no de permisos?

    El Admin SDK usa 403 para dos cosas muy distintas: "no tienes permiso" (nunca
    mejora) y "vas demasiado rápido" (mejora esperando). Distinguirlas mirando el
    motivo es imprescindible para procesos masivos: un backfill de decenas de miles
    de cuentas choca con la cuota y debe esperar, no abortar.
    """
    if getattr(e.resp, "status", None) != 403:
        return False
    detalle = str(e).lower()
    return any(m in detalle for m in
               ("ratelimitexceeded", "userratelimitexceeded", "quotaexceeded"))


def _reintentable(e: HttpError, incluir_404: bool = False) -> bool:
    estado = getattr(e.resp, "status", None)
    return estado in REINTENTABLES or _es_cuota(e) or (incluir_404 and estado == 404)


# La cuota del Admin SDK se mide POR MINUTO y por usuario delegado. Esperar 2 y 4
# segundos (el backoff de un 503) no sirve de nada: la ventana no se ha renovado y
# los tres intentos se agotan dentro del mismo minuto. Por eso la cuota tiene su
# propia escala, de decenas de segundos, y más intentos.
MAX_REINTENTOS_CUOTA = 6


def _tope(e: HttpError) -> int:
    return MAX_REINTENTOS_CUOTA if _es_cuota(e) else MAX_REINTENTOS


def _espera(e: HttpError, intento: int) -> float:
    if _es_cuota(e):
        return min(75.0, 10.0 * (2 ** intento))   # 10, 20, 40, 75, 75, ...
    return PAUSA * (2 ** intento)


def ruta_credenciales() -> Path:
    """Ruta absoluta al JSON del service account (las relativas cuelgan de services/)."""
    ruta = Path(settings.GOOGLE_SA_FILE)
    return ruta if ruta.is_absolute() else _RAIZ / ruta


def _estado(e: HttpError):
    """Código HTTP de un HttpError de googleapiclient, o None si no lo trae."""
    return getattr(e.resp, "status", None)


def _fallo(e: HttpError, accion: str) -> ErrorDeGoogle:
    """Traduce un HttpError a error de dominio, conservando el código de Google."""
    logger.exception("Fallo del Admin SDK al %s.", accion)
    return ErrorDeGoogle(f"Google respondió {_estado(e)} al {accion}: {e}")


class CredencialesAdminSDK:
    """Se encarga ÚNICAMENTE de construir y entregar la autenticación."""

    def __init__(self, archivo=None, admin_delegado: str = ""):
        self.archivo = Path(archivo) if archivo else ruta_credenciales()
        self.admin_delegado = admin_delegado or settings.GOOGLE_ADMIN_DELEGADO
        self.scopes = SCOPES

    def obtener(self):
        """Credenciales del service account, ya impersonando al admin delegado.

        Lanza ErrorDeConfiguracion si el archivo no existe o no es un service
        account válido: es un fallo de despliegue, no de quien llama.
        """
        if not self.archivo.is_file():
            raise ErrorDeConfiguracion(
                f"No se encontró el JSON del service account en '{self.archivo}'. "
                "Colócalo ahí o ajusta GOOGLE_SA_FILE en services/.env.")
        try:
            return (
                service_account.Credentials
                .from_service_account_file(str(self.archivo), scopes=self.scopes)
                .with_subject(self.admin_delegado)
            )
        except Exception as e:
            logger.exception("No se pudieron cargar las credenciales de Google.")
            raise ErrorDeConfiguracion(
                f"El JSON de '{self.archivo}' no es un service account válido: {e}") from e


class ServicioUsuarios:
    """Usuarios del dominio (Directory API `users`)."""

    def __init__(self, service):
        self._service = service

    def obtener(self, clave_usuario: str):
        """Datos del usuario por correo o ID. None si no existe (404)."""
        try:
            return self._service.users().get(userKey=clave_usuario).execute()
        except HttpError as e:
            if _estado(e) == 404:
                return None
            raise _fallo(e, f"consultar el usuario '{clave_usuario}'")

    def obtener_con_reintentos(self, clave_usuario: str):
        """
        Como `obtener`, pero reintentando si los servidores de Google fallan.

        Devuelve None SOLO si el usuario no existe (404). Si se agotan los
        reintentos lanza ErrorDeGoogle: un None ambiguo llevaría a quien llama a
        creer que la cuenta está libre y a crearla por duplicado.
        """
        intento = 0
        while True:
            try:
                return self._service.users().get(userKey=clave_usuario).execute()
            except HttpError as e:
                if _estado(e) == 404:
                    return None
                if _reintentable(e) and intento < _tope(e) - 1:
                    time.sleep(_espera(e, intento))
                    intento += 1
                    continue
                raise _fallo(e, f"consultar el usuario '{clave_usuario}'")

    def filtrar(self, max_resultados: int = 100, consulta: str = "") -> list:
        """Lista usuarios del dominio, opcionalmente filtrados por `consulta`
        (sintaxis de búsqueda del Admin SDK, p. ej. `email:juan*`)."""
        try:
            respuesta = self._service.users().list(
                customer="my_customer",
                maxResults=max_resultados,
                query=consulta or None,
            ).execute()
        except HttpError as e:
            raise _fallo(e, "listar usuarios")
        return respuesta.get("users", [])

    def crear(self, campos: dict) -> dict:
        """Crea un usuario. `campos` es el cuerpo tal cual lo espera la Directory API."""
        try:
            return self._service.users().insert(body=campos).execute()
        except HttpError as e:
            raise _fallo(e, f"crear el usuario '{campos.get('primaryEmail')}'")

    def actualizar(self, clave_usuario: str, campos: dict) -> dict:
        """Actualiza los campos indicados de un usuario (PATCH parcial).

        Reintenta ante cuota agotada y fallos transitorios: es la operación que usa
        el backfill masivo, y con decenas de miles de escrituras la cuota se agota
        con seguridad.
        """
        intento = 0
        while True:
            try:
                return self._service.users().patch(
                    userKey=clave_usuario, body=campos).execute()
            except HttpError as e:
                if _estado(e) == 404:
                    raise ErrorDeGoogle(f"El usuario '{clave_usuario}' no existe en Google.")
                if _reintentable(e) and intento < _tope(e) - 1:
                    time.sleep(_espera(e, intento))
                    intento += 1
                    continue
                raise _fallo(e, f"actualizar el usuario '{clave_usuario}'")

    def volcar(self, proyeccion: str = "full"):
        """
        Itera TODOS los usuarios del dominio, resolviendo la paginación.

        Es un generador: con ~28 000 cuentas, materializar la lista entera con
        `projection=full` ocupa bastante memoria y no hace falta. `full` trae los
        `externalIds`, que es lo que necesita el emparejamiento.

        Una sola pasada (~90 s) sustituye a decenas de miles de consultas
        individuales, que además chocarían con la cuota.
        """
        pagina = None
        while True:
            try:
                r = self._service.users().list(
                    customer="my_customer", maxResults=500, pageToken=pagina,
                    projection=proyeccion).execute()
            except HttpError as e:
                raise _fallo(e, "volcar el directorio")
            yield from r.get("users", [])
            pagina = r.get("nextPageToken")
            if not pagina:
                return

    def establecer_external_id(self, clave_usuario: str, valor: str, tipo: str) -> str:
        """
        Añade (o corrige) un externalId de `customType=tipo`, PRESERVANDO los demás.

        `users.patch` reemplaza el array `externalIds` COMPLETO: mandar solo la
        cédula borraría los ~679 externalIds de tipo 'organization' que ya existen
        en el dominio. Por eso esto es read-modify-write.

        Idempotente. Devuelve qué hizo: 'sin_cambios', 'insertado' o 'corregido'.
        """
        usuario = self.obtener_con_reintentos(clave_usuario)
        if usuario is None:
            raise ErrorDeGoogle(f"El usuario '{clave_usuario}' no existe en Google.")

        existentes = list(usuario.get("externalIds") or [])
        for e in existentes:
            if (e.get("customType") or e.get("type")) != tipo:
                continue
            if (e.get("value") or "").strip() == valor:
                return "sin_cambios"
            e["value"] = valor
            self.actualizar(clave_usuario, {"externalIds": existentes})
            return "corregido"

        existentes.append({"value": valor, "type": "custom", "customType": tipo})
        self.actualizar(clave_usuario, {"externalIds": existentes})
        return "insertado"

    def eliminar(self, clave_usuario: str) -> bool:
        """Elimina un usuario. False si no existía (404), True si se borró."""
        try:
            self._service.users().delete(userKey=clave_usuario).execute()
            return True
        except HttpError as e:
            if _estado(e) == 404:
                return False
            raise _fallo(e, f"eliminar el usuario '{clave_usuario}'")


class ServicioUnidades:
    """Unidades organizativas / OUs (Directory API `orgunits`)."""

    def __init__(self, service):
        self._service = service

    def listar(self, **kwargs) -> list:
        """Árbol completo de OUs del dominio, en bruto."""
        try:
            respuesta = self._service.orgunits().list(
                customerId="my_customer", type="all", **kwargs).execute()
        except HttpError as e:
            if _estado(e) == 404:
                return []
            raise _fallo(e, "listar las unidades organizativas")
        return respuesta.get("organizationUnits", [])

    def existe(self, ruta: str) -> bool:
        """¿Existe esa unidad organizativa? Una sola llamada, para validar antes de
        crear una cuenta: listar las 80 OUs para comprobar una sola es un despilfarro.
        La API quiere la ruta SIN la barra inicial."""
        if not ruta or ruta == "/":
            return True                      # la raíz siempre existe
        try:
            self._service.orgunits().get(
                customerId="my_customer", orgUnitPath=ruta.lstrip("/")).execute()
            return True
        except HttpError as e:
            if _estado(e) == 404:
                return False
            raise _fallo(e, f"comprobar la unidad organizativa '{ruta}'")

    def listar_formateado(self) -> list:
        """OUs normalizadas y ordenadas por ruta, con la raíz al frente (para
        poblar desplegables)."""
        unidades = [{"ruta": "/", "nombre": "Raíz (Root)", "descripcion": "", "padre": ""}]
        for ou in self.listar():
            unidades.append({
                "ruta": ou.get("orgUnitPath"),
                "nombre": ou.get("name"),
                "descripcion": ou.get("description", ""),
                "padre": ou.get("parentOrgUnitPath", "/"),
            })
        return sorted(unidades, key=lambda u: u["ruta"])


class ServicioGrupos:
    """Grupos y sus miembros (Directory API `groups` / `members`)."""

    def __init__(self, service):
        self._service = service

    def listar(self, **kwargs) -> list:
        """Todos los grupos del dominio, resolviendo la paginación (200 por página)."""
        completos = []
        pagina = None
        try:
            while True:
                respuesta = self._service.groups().list(
                    customer="my_customer", maxResults=200, pageToken=pagina, **kwargs
                ).execute()
                completos.extend(respuesta.get("groups", []))
                pagina = respuesta.get("nextPageToken")
                if not pagina:
                    return completos
        except HttpError as e:
            if _estado(e) == 404:
                return []
            raise _fallo(e, "listar los grupos")

    def listar_formateado(self) -> list:
        """Grupos normalizados (correo y nombre) y ordenados por correo."""
        grupos = [
            {"email": g.get("email"), "nombre": g.get("name", "Sin nombre")}
            for g in self.listar()
        ]
        return sorted(grupos, key=lambda g: g["email"] or "")

    def existe(self, email_grupo: str) -> bool:
        """¿Existe el grupo? Una llamada, para validar antes de intentar añadir a
        alguien (listar los 184 grupos costaría medio segundo)."""
        try:
            self._service.groups().get(groupKey=email_grupo).execute()
            return True
        except HttpError as e:
            if _estado(e) == 404:
                return False
            raise _fallo(e, f"comprobar el grupo '{email_grupo}'")

    def listar_miembros(self, email_grupo: str) -> list:
        """Miembros del grupo, resolviendo la paginación. [] si el grupo no existe."""
        miembros, pagina = [], None
        try:
            while True:
                r = self._service.members().list(
                    groupKey=email_grupo, maxResults=200, pageToken=pagina).execute()
                miembros.extend({
                    "email": m.get("email"), "rol": m.get("role"),
                    "tipo": m.get("type"), "estado": m.get("status"),
                } for m in r.get("members", []))
                pagina = r.get("nextPageToken")
                if not pagina:
                    return miembros
        except HttpError as e:
            if _estado(e) == 404:
                return []
            raise _fallo(e, f"listar los miembros de '{email_grupo}'")

    def quitar_miembro(self, email_grupo: str, email_usuario: str) -> bool:
        """
        Saca a un usuario del grupo. False si no era miembro (o el grupo no existe).

        Google no responde 404 cuando el miembro no está: responde **400 con el mensaje
        'Missing required field: memberKey'**, que sugiere una petición mal formada
        cuando en realidad la petición es correcta y el miembro no existe. Se distingue
        por el mensaje para no tragarse un 400 de verdad.
        """
        try:
            self._service.members().delete(
                groupKey=email_grupo, memberKey=email_usuario).execute()
            return True
        except HttpError as e:
            if _estado(e) == 404:
                return False
            if _estado(e) == 400 and "memberkey" in str(e).lower():
                return False
            raise _fallo(e, f"quitar '{email_usuario}' del grupo '{email_grupo}'")

    def agregar_miembro(self, email_grupo: str, email_usuario: str, rol: str = "MEMBER") -> dict:
        """
        Añade un usuario a un grupo, con reintentos.

        Dos casos que no son error: Google devuelve 409 si el usuario ya era
        miembro (idempotente -> `{"status": "already_member"}`), y puede devolver
        404 durante unos segundos tras crear la cuenta, mientras propaga el usuario
        entre sus servidores (por eso el 404 se reintenta aquí, a diferencia del
        resto de métodos).
        """
        cuerpo = {"email": email_usuario, "role": rol}
        intento = 0
        while True:
            try:
                self._service.members().insert(groupKey=email_grupo, body=cuerpo).execute()
                return {"status": "agregado"}
            except HttpError as e:
                if _estado(e) == 409:
                    return {"status": "ya_era_miembro"}
                if _reintentable(e, incluir_404=True) and intento < _tope(e) - 1:
                    time.sleep(_espera(e, intento))
                    intento += 1
                    continue
                raise _fallo(e, f"añadir '{email_usuario}' al grupo '{email_grupo}'")


class DirectorioGoogle:
    """Fachada: compone los tres servicios sobre una única conexión autenticada."""

    def __init__(self, credenciales=None):
        cred = credenciales or CredencialesAdminSDK().obtener()
        try:
            # cache_discovery=False: el caché en disco de googleapiclient no es
            # seguro entre los workers de gunicorn y ensucia los logs con avisos.
            servicio = build(API_NAME, API_VERSION, credentials=cred, cache_discovery=False)
        except Exception as e:
            logger.exception("No se pudo construir el cliente del Admin SDK.")
            raise ErrorDeConfiguracion(f"No se pudo conectar con el Admin SDK: {e}") from e

        self.usuarios = ServicioUsuarios(servicio)
        self.unidades = ServicioUnidades(servicio)
        self.grupos = ServicioGrupos(servicio)


_local = threading.local()


def obtener_directorio() -> DirectorioGoogle:
    """
    Directorio del HILO actual, construido perezosamente en su primer uso.

    Perezoso porque construirlo lee el JSON del service account y negocia un token
    con Google: hacerlo al importar rompería el arranque del clasificador cuando las
    credenciales no están.

    UNO POR HILO, no uno por proceso. El objeto que devuelve `googleapiclient.build`
    NO es thread-safe: por debajo lleva una conexión httplib2 con estado TLS, y
    compartirla entre hilos entrelaza los registros cifrados. Se manifiesta como
    `SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC` y timeouts aparentemente aleatorios,
    no como un error de concurrencia. Afecta tanto al backfill concurrente como a los
    workers de gunicorn con varios hilos.

    El coste es una construcción por hilo (una descarga del documento de descubrimiento
    y un token). Con una docena de hilos es despreciable frente a la corrupción.
    """
    directorio = getattr(_local, "directorio", None)
    if directorio is None:
        directorio = DirectorioGoogle()
        _local.directorio = directorio
    return directorio
