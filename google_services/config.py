"""
Configuración de la app de Google Workspace.

El campo común que esta app usa (DATABASE_URL, para el sistema de API keys) lo
aporta commons.config.ConfigComun; aquí solo se añade lo propio del Admin SDK.
Todas las apps leen el mismo services/.env.

Esta app usa Redis SOLO para el limitador de tasa (commons.rate_limit, campos
RATE_LIMIT_* de ConfigComun): cada consulta va al Admin SDK en vivo, sin caché.

El único secreto es el JSON del service account: NO va en el .env, va en un archivo
aparte (gitignorado) cuya ruta se configura con GOOGLE_SA_FILE. Ver .env.example.
"""
from commons.config import ConfigComun

# Admin SDK Directory API. Fijos: identifican la API, no son configurables.
API_NAME = "admin"
API_VERSION = "directory_v1"

# Permisos que el service account necesita. Deben coincidir EXACTAMENTE con los
# si aquí se añade uno que allí no está, toda llamada falla con 401 unauthorized_client.
SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.group",
    "https://www.googleapis.com/auth/admin.directory.group.member",
    "https://www.googleapis.com/auth/apps.licensing",
    "https://www.googleapis.com/auth/admin.directory.orgunit",
]

# Reintentos ante caídas de los servidores de Google (500/503) y demoras de
# propagación (404 al añadir a un grupo un usuario recién creado).
# Espera exponencial: PAUSA * 2**intento -> 2 s, 4 s, 8 s.
MAX_REINTENTOS = 3
PAUSA = 2


class Settings(ConfigComun):
    """Config de Google Workspace = comunes (DB para las API keys) + lo del Admin SDK."""

    # Ruta al JSON del service account. Relativa se resuelve contra `services/`.
    # OJO: no es credentials.json (ese es el del Document AI del clasificador).
    GOOGLE_SA_FILE: str = "google_workspace_sa.json"

    # Admin del dominio que el service account impersona (domain-wide delegation).
    # Un service account no puede administrar el directorio por sí mismo: actúa
    # siempre "en nombre de" este usuario, que debe tener rol de administrador.
    GOOGLE_ADMIN_DELEGADO: str = "ucgone.users@casagrande.edu.ec"

    # Dominio institucional. Se exige que los usuarios que se crean pertenezcan a él.
    GOOGLE_DOMINIO: str = "casagrande.edu.ec"

    # Contraseña inicial de una cuenta nueva:
    #   "cedula"    la identificación de la persona (lo que hace el monolito hoy).
    #               OJO: la cédula es un dato público. Siempre se crea la cuenta con
    #               changePasswordAtNextLogin, así que la ventana se cierra en el
    #               primer acceso, pero mientras tanto cualquiera que la conozca entra.
    #   "aleatoria" contraseña fuerte, devuelta UNA sola vez en la respuesta del alta.
    #               El sistema que la pide se encarga de entregársela a la persona.
    GOOGLE_PASSWORD_INICIAL: str = "cedula"


settings = Settings()
