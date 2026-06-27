"""
Configuración de la app de consulta de títulos SENESCYT.

Los campos comunes (DATABASE_URL para el sistema de API keys, REDIS_URL para la
caché) los aporta commons.config.ConfigComun; aquí solo se añade lo propio del
portal SENESCYT. Todas las apps leen el mismo services/.env.

Por defecto `SENESCYT_BASE_URL` apunta al servidor MOCK local
(consulta_titulos/mock). EN PRODUCCIÓN se sobreescribe con el portal real y
`VERIFY_SSL=false` (su certificado está roto). Ver .env.example.
"""
from commons.config import ConfigComun

# Rutas del portal JSF/PrimeFaces. Son fijas del formulario; el mock las replica
# para que el scraper corra sin modificaciones contra mock o portal real.
CONSULTA_PATH = "/consulta-titulos-web/faces/vista/consulta/consulta.xhtml"
CAPTCHA_PATH = "/consulta-titulos-web/Captcha.jpg"

# (timeout_conexion, timeout_lectura) en segundos. 60 s de lectura porque el
# portal real tarda 30-50 s en horas pico (contra el mock es instantáneo).
TIMEOUT = (10, 60)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Reintentos del captcha por OCR: si ddddocr falla, se pide una imagen nueva y se
# vuelve a intentar hasta este tope. Acota el peor caso de latencia de la petición.
MAX_INTENTOS_CAPTCHA = 6


class Settings(ConfigComun):
    """Config de la consulta de títulos = comunes (DB/Redis) + lo del portal."""

    # A dónde consultar: el mock local por defecto; el portal real en producción.
    SENESCYT_BASE_URL: str = "http://localhost:8090"

    # Verificación de certificado SSL. El portal real de SENESCYT tiene el SSL
    # roto -> en producción poner VERIFY_SSL=false.
    VERIFY_SSL: bool = True

    # Vigencia del caché de una consulta: 30 días, igual que el doc original.
    CACHE_TTL_DIAS: int = 30

    @property
    def CACHE_TTL_SEGUNDOS(self) -> int:
        return self.CACHE_TTL_DIAS * 24 * 60 * 60


settings = Settings()
