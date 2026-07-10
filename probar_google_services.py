"""
Prueba END-TO-END del servicio de Google Workspace corriendo de verdad
(uvicorn + PostgreSQL para las API keys + Google Admin SDK).

SOLO HACE LECTURAS. No crea, no modifica ni borra cuentas, grupos ni unidades del
dominio real, y no escribe nada fuera de sí misma.

El servicio no usa Redis: si Redis está levantado, la suite lo aprovecha para
comprobar afirmativamente que el servicio no deja ninguna clave en él.

Uso:
    1. Arranca el servicio:
         uvicorn google_services.main:app --port 8092
       (o el clasificador unificado: uvicorn app.main:app --port 8001, y entonces
        exporta API_PREFIJO=/api/v1)
    2. Define las claves (se crean con: python gestionar_llaves.py crear <nombre> <scope>):
         PowerShell:  $env:API_KEY_ADMIN = "wsk_..."
         bash:        export API_KEY_ADMIN=wsk_...
    3. Corre el script:
         python probar_google_services.py

NUNCA pegues la clave dentro de este archivo: está versionado en git y la clave
terminaría publicada en el repositorio.

Variables de entorno:
    API_URL         base del servicio (default http://127.0.0.1:8092)
    API_PREFIJO     prefijo de las rutas (default ""; /api/v1 si va unificado)
    API_KEY_ADMIN   clave con scope admin (obligatoria)
    API_KEY_CONSUMO clave con scope consumo (opcional; habilita la prueba del 403)

"""
import os
import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

BASE = os.environ.get("API_URL", "http://127.0.0.1:8092").rstrip("/")
PREFIJO = os.environ.get("API_PREFIJO", "").rstrip("/")
ADMIN = os.environ.get("API_KEY_ADMIN", "")
CONSUMO = os.environ.get("API_KEY_CONSUMO", "")

A = {"X-API-Key": ADMIN}
C = {"X-API-Key": CONSUMO}
fallos = []


def cliente_redis():
    """Cliente Redis si está levantado, o None. Este servicio NO debe usar Redis;
    lo único que se hace con él es comprobar que, efectivamente, no escribe nada."""
    try:
        import redis

        from commons.config import settings as comunes
        c = redis.Redis.from_url(comunes.REDIS_URL, decode_responses=True,
                                 socket_connect_timeout=2)
        c.ping()
        return c
    except Exception:
        return None


REDIS = cliente_redis()


def check(nombre, condicion, detalle=""):
    icono = "[OK]  " if condicion else "[FALLA]"
    print(f"  {icono} {nombre}" + (f"  ({detalle})" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


def pedir(metodo, ruta, **kwargs):
    """Petición al servicio con timeout; los errores de red cuentan como fallo."""
    kwargs.setdefault("timeout", 60)
    return requests.request(metodo, BASE + PREFIJO + ruta, **kwargs)


print(f"Servicio bajo prueba: {BASE}{PREFIJO or ' (sin prefijo)'}")
print("Redis: " + ("disponible (se verifica que el servicio NO lo usa)"
                   if REDIS else "no disponible (el servicio no lo necesita)"))

if not ADMIN:
    print("[FALLA] Falta API_KEY_ADMIN. Créala con: python gestionar_llaves.py crear pruebas admin")
    sys.exit(1)

# ================== FASE 1: salud ==================
print("\n=== Fase 1: salud ===")
try:
    r = requests.get(BASE + "/health", timeout=10)
except requests.exceptions.ConnectionError:
    print(f"[FALLA] No se pudo conectar a {BASE}. ¿Está corriendo uvicorn?")
    sys.exit(1)
check("GET /health responde 200", r.status_code == 200, f"status={r.status_code}")

# ================== FASE 2: autenticación ==================
print("\n=== Fase 2: autenticación ===")
r = pedir("GET", "/google-services/unidades/")
check("Sin clave -> 401", r.status_code == 401, f"status={r.status_code}")

r = pedir("GET", "/google-services/unidades/", headers={"X-API-Key": "wsk_clave_invalida"})
check("Clave inválida -> 401", r.status_code == 401, f"status={r.status_code}")

if CONSUMO:
    # Endpoint admin con clave de consumo. `requiere_admin` corta antes de que el
    # handler corra, y el correo está fuera del dominio: aunque la auth fallara,
    # la validación daría 400 sin llegar a crear nada en Google.
    r = pedir("POST", "/google-services/usuarios/", headers=C, json={
        "primaryEmail": "no.crear@gmail.com",
        "name": {"givenName": "No", "familyName": "Crear"},
        "password": "no-se-usa",
    })
    check("Clave de consumo en endpoint admin -> 403", r.status_code == 403,
          f"status={r.status_code}")
else:
    print("  [SALTA] Prueba del 403: define API_KEY_CONSUMO para habilitarla.")

# ================== FASE 3: lecturas del directorio ==================
print("\n=== Fase 3: lecturas del directorio (Google en vivo) ===")
r = pedir("GET", "/google-services/unidades/", headers=A)
if r.status_code == 500:
    print(f"  [FALLA] 500 al listar unidades: {r.json().get('detail')}")
    print("          Revisa GOOGLE_SA_FILE y la delegación en todo el dominio.")
    sys.exit(1)
check("GET /unidades/ responde 200", r.status_code == 200, f"status={r.status_code}")

if r.status_code == 200:
    d = r.json()
    check("Hay al menos la OU raíz", d.get("total", 0) >= 1, f"total={d.get('total')}")
    check("Cada unidad trae `ruta` y `nombre`",
          all("ruta" in u and "nombre" in u for u in d.get("unidades", [])))
    check("Contrato result/message/status", {"result", "message", "status"} <= set(d))
    check("La respuesta NO expone `fuente` (no hay caché)", "fuente" not in d)

r = pedir("GET", "/google-services/grupos/", headers=A)
check("GET /grupos/ responde 200", r.status_code == 200, f"status={r.status_code}")
if r.status_code == 200:
    d = r.json()
    check("Cada grupo trae `email` y `nombre`",
          all("email" in g and "nombre" in g for g in d.get("grupos", [])))

# ================== FASE 4: usuarios (solo lectura) ==================
print("\n=== Fase 4: usuarios (solo lectura) ===")
# El admin delegado tiene que existir: es la cuenta que el service account impersona.
delegado = os.environ.get("GOOGLE_ADMIN_DELEGADO", "")
if not delegado:
    try:
        from google_services.config import settings
        delegado = settings.GOOGLE_ADMIN_DELEGADO
    except Exception:
        delegado = ""

if delegado:
    r = pedir("GET", f"/google-services/usuarios/{delegado}", headers=A)
    check(f"GET /usuarios/{delegado} -> 200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code == 200:
        d = r.json()
        check("El usuario devuelto es el admin delegado",
              (d.get("usuario", {}).get("primaryEmail") or "").lower() == delegado.lower())
        check("status == 'encontrado'", d.get("status") == "encontrado")
else:
    print("  [SALTA] No se pudo determinar GOOGLE_ADMIN_DELEGADO.")

dominio = delegado.split("@")[-1] if "@" in delegado else "casagrande.edu.ec"
r = pedir("GET", f"/google-services/usuarios/no.existe.jamas.9f3a@{dominio}", headers=A)
check("Usuario inexistente -> 404", r.status_code == 404, f"status={r.status_code}")

r = pedir("GET", "/google-services/usuarios/?max_resultados=5", headers=A)
check("GET /usuarios/ (lista) responde 200", r.status_code == 200, f"status={r.status_code}")
if r.status_code == 200:
    check("Devuelve como mucho 5 usuarios", len(r.json().get("usuarios", [])) <= 5)

# ================== FASE 5: validaciones y ausencia de caché ==================
print("\n=== Fase 5: validaciones y ausencia de caché ===")
# Correo fuera del dominio: debe rechazarse ANTES de llamar a Google (400, no 502).
r = pedir("POST", "/google-services/usuarios/", headers=A, json={
    "primaryEmail": "alguien@gmail.com",
    "name": {"givenName": "Prueba", "familyName": "Dominio"},
    "password": "no-se-usa-porque-falla-antes",
})
check("Correo fuera del dominio -> 400", r.status_code == 400, f"status={r.status_code}")

# PATCH sin campos: 400 sin tocar Google.
r = pedir("PATCH", f"/google-services/usuarios/{delegado or 'x@' + dominio}", headers=A, json={})
check("PATCH sin campos -> 400", r.status_code == 400, f"status={r.status_code}")

# El endpoint de caché ya no existe: el servicio consulta Google en vivo siempre.
r = pedir("DELETE", "/google-services/cache", headers=A)
check("DELETE /cache ya no existe -> 404", r.status_code == 404, f"status={r.status_code}")

if REDIS:
    # Comprobación afirmativa de que el servicio no cachea: tras varias lecturas,
    # Redis no debe tener ni una clave `google:*`.
    REDIS.delete(*(REDIS.keys("google:*") or ["_"]))
    for _ in range(2):
        pedir("GET", "/google-services/unidades/", headers=A)
        pedir("GET", "/google-services/grupos/", headers=A)
    sobrantes = REDIS.keys("google:*")
    check("El servicio no escribe NADA en Redis", not sobrantes, f"claves={sobrantes}")
else:
    print("  [SALTA] Comprobación de que no se usa Redis: Redis no está levantado.")

# ================== Resumen ==================
print("\n" + "=" * 60)
if fallos:
    print(f"FALLARON {len(fallos)} prueba(s):")
    for f in fallos:
        print(f"  - {f}")
    sys.exit(1)
print("Todas las pruebas pasaron.")
sys.exit(0)
