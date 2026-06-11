"""
Prueba END-TO-END del servicio corriendo de verdad (uvicorn + PostgreSQL + Extend).

A diferencia de probar_procesadores.py (in-process, con mocks), este script
golpea el servicio real por HTTP: salud, autenticación, panel, CRUDs de
administración, sincronización con Extend y (opcional) inferencia con un
documento real.

Uso:
    1. Arranca el servicio:  uvicorn app.main:app --port 8001
    2. Define la clave admin (se crea con: python gestionar_llaves.py crear admin admin):
         PowerShell:  $env:API_KEY_ADMIN = "wsk_..."
         CMD:         set API_KEY_ADMIN=wsk_...
         bash:        export API_KEY_ADMIN=wsk_...
    3. Corre el script:
         python probar_servicio.py
       Con documento real (activa la fase de inferencia):
         python probar_servicio.py C:\\ruta\\cedula.jpg [cedula_sistema]

NUNCA pegues la clave dentro de este archivo: está versionado en git y la clave
terminaría publicada en el repositorio.

Variables de entorno:
    API_URL        base del servicio (default http://127.0.0.1:8001)
    API_KEY_ADMIN  clave con scope admin (obligatoria para las fases admin)

Sale con código 0 si todo pasa, 1 si algo falla (sirve para CI).
"""
import json
import mimetypes
import os
import sys

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

BASE = os.environ.get("API_URL", "http://127.0.0.1:8001").rstrip("/")
ADMIN = os.environ.get("API_KEY_ADMIN", "")
ARCHIVO = sys.argv[1] if len(sys.argv) > 1 else None
CEDULA = sys.argv[2].strip() if len(sys.argv) > 2 else None

A = {"X-API-Key": ADMIN}
fallos = []


def check(nombre, condicion, detalle=""):
    icono = "[OK]  " if condicion else "[FALLA]"
    print(f"  {icono} {nombre}" + (f"  ({detalle})" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


def pedir(metodo, ruta, **kwargs):
    """Petición al servicio con timeout; los errores de red cuentan como fallo."""
    kwargs.setdefault("timeout", 120)
    return requests.request(metodo, BASE + ruta, **kwargs)


# ================== FASE 1: salud y estáticos ==================
print(f"Servicio bajo prueba: {BASE}")
print("\n=== Fase 1: salud, panel y estáticos ===")
try:
    r = pedir("GET", "/")
except requests.exceptions.ConnectionError:
    print(f"[FALLA] No se pudo conectar a {BASE}. ¿Está corriendo uvicorn?")
    sys.exit(1)
check("GET / -> 200 online", r.status_code == 200 and r.json().get("status") == "online")
r = pedir("GET", "/admin", allow_redirects=False)
check("/admin redirige al panel", r.status_code in (302, 307))
r = pedir("GET", "/admin/procesadores")
check("/admin/procesadores renderiza (MD3 + CRM)",
      r.status_code == 200 and "--md-primary" in r.text and "<aside>" in r.text)
r = pedir("GET", "/static/js/jquery.min.js")
check("jQuery vendorizado se sirve", r.status_code == 200 and "jQuery v3" in r.text[:100])

# ================== FASE 2: autenticación ==================
print("\n=== Fase 2: autenticación ===")
check("sin X-API-Key -> 401", pedir("GET", "/api/v1/rutas/").status_code == 401)
check("clave inválida -> 401",
      pedir("GET", "/api/v1/rutas/", headers={"X-API-Key": "wsk_invalida"}).status_code == 401)

if not ADMIN:
    print("\n[FALLA] Falta API_KEY_ADMIN: exporta una clave admin para las fases siguientes.")
    print("        Genera una con: python gestionar_llaves.py crear administracion admin")
    sys.exit(1)

r = pedir("GET", "/api/v1/rutas/", headers=A)
check("la clave admin entra -> 200", r.status_code == 200,
      f"HTTP {r.status_code}: ¿la clave tiene scope admin?")
if r.status_code != 200:
    print("\nSin clave admin válida no se puede seguir.")
    sys.exit(1)

# Clave de consumo temporal para probar el 403 (se revoca al final).
llave_consumo_id = None
r = pedir("POST", "/api/v1/api-keys/", headers=A,
          json={"consumidor": "prueba-e2e-temporal", "scope": "consumo"})
check("crear clave de consumo temporal -> 201", r.status_code == 201, r.text[:200])
if r.status_code == 201:
    cuerpo = r.json()
    llave_consumo_id = cuerpo["id"]
    H = {"X-API-Key": cuerpo["llave"]}
    check("consumo no puede tocar administración -> 403",
          pedir("GET", "/api/v1/api-keys/", headers=H).status_code == 403)

# ================== FASE 3: rutas <-> procesadores (CRUD real) ==================
print("\n=== Fase 3: CRUD de rutas y procesadores (con limpieza) ===")
rutas = pedir("GET", "/api/v1/rutas/", headers=A).json()
claves = {x["clave"] for x in rutas}
check("el catálogo trae las rutas del servicio",
      {"clasificar", "validar-identidad", "ocr"} <= claves, str(claves))

procs = pedir("GET", "/api/v1/procesadores/", headers=A).json()
check("hay procesadores configurados", len(procs) >= 1, f"{len(procs)} filas")
for p in procs:
    pid = f"  -> {p['procesador_id']} (v{p['version'] or 'última'})" if p["modo"] == "id" else "  (inline)"
    print(f"        {p['ruta']:<18} {p['operacion']:<10} {p['clase'] or '—':<10} {p['modo']}{pid}")
check("validar-identidad NO tiene fila de OCR (migración aplicada)",
      not any(p["ruta"] == "validar-identidad" and p["operacion"] == "parse" for p in procs))

# Aviso de configuración sospechosa: un procesador_id asignado en modo 'inline'
# se IGNORA (el modo inline usa el esquema/prompts, no el procesador publicado).
sospechosas = [p for p in procs if p["modo"] == "inline" and p.get("procesador_id")]
for p in sospechosas:
    print(f"  [AVISO] fila #{p['id']} ({p['ruta']}/{p['operacion']}): tiene procesador "
          f"'{p['procesador_id']}' pero modo 'inline' -> el procesador publicado NO se usa. "
          "Cambia el modo a 'id' si quieres usarlo.")

id_proc_tmp = None
try:
    r = pedir("POST", "/api/v1/rutas/", headers=A,
              json={"clave": "prueba-e2e", "url": "/api/v1/prueba/",
                    "descripcion": "Ruta temporal del script de prueba E2E."})
    check("crear ruta temporal -> 201", r.status_code == 201, r.text[:200])

    r = pedir("POST", "/api/v1/procesadores/", headers=A,
              json={"ruta": "prueba-e2e", "operacion": "extraer", "clase": "PRUEBA",
                    "modo": "inline",
                    "esquema": {"type": "object", "properties": {
                        "campo": {"type": ["string", "null"], "description": "Campo de prueba."}}}})
    check("asociar procesador a la ruta -> 201", r.status_code == 201, r.text[:200])
    if r.status_code == 201:
        id_proc_tmp = r.json()["id"]

    check("borrar ruta con procesador asociado -> 409",
          pedir("DELETE", "/api/v1/rutas/prueba-e2e", headers=A).status_code == 409)
finally:
    # Limpieza SIEMPRE, aunque algo haya fallado a medias.
    if id_proc_tmp is not None:
        check("limpiar: borrar procesador temporal -> 204",
              pedir("DELETE", f"/api/v1/procesadores/{id_proc_tmp}", headers=A).status_code == 204)
    check("limpiar: borrar ruta temporal -> 204",
          pedir("DELETE", "/api/v1/rutas/prueba-e2e", headers=A).status_code == 204)

# ================== FASE 4: sincronización con Extend (REAL) ==================
print("\n=== Fase 4: sincronización con Extend Studio (API real) ===")
for tipo in ("clasificar", "extraer"):
    r = pedir("GET", f"/api/v1/procesadores/extend?tipo={tipo}", headers=A)
    ok = r.status_code == 200
    check(f"GET /procesadores/extend?tipo={tipo} -> 200", ok, f"HTTP {r.status_code}: {r.text[:200]}")
    if ok:
        lista = r.json()
        print(f"        {len(lista)} procesador(es) {tipo} publicados en Extend:")
        for p in lista[:5]:
            versiones = ", ".join(f"v{v['version']}" for v in p.get("versiones", [])) or "sin versiones"
            print(f"          - {p['id']}  {p.get('nombre') or ''}  ({versiones})")

# ================== FASE 5: inferencia con documento real (opcional) ==================
if ARCHIVO:
    print("\n=== Fase 5: inferencia con documento real ===")
    if not os.path.isfile(ARCHIVO):
        check("el archivo existe", False, ARCHIVO)
    else:
        mime, _ = mimetypes.guess_type(ARCHIVO)
        with open(ARCHIVO, "rb") as f:
            contenido = f.read()
        nombre = os.path.basename(ARCHIVO)

        r = pedir("POST", "/api/v1/clasificar/", headers=A,
                  files={"file": (nombre, contenido, mime)})
        check("clasificar -> 200", r.status_code == 200, r.text[:300])
        if r.status_code == 200:
            d = r.json()
            print(f"        clase={d['document_class']}  confianza={d['confidence']:.2f}  valido={d['result']}")

        r = pedir("POST", "/api/v1/ocr/", headers=A,
                  files={"file": (nombre, contenido, mime)})
        check("ocr -> 200", r.status_code == 200, r.text[:300])
        if r.status_code == 200:
            print(f"        {len(r.json().get('content') or '')} caracteres extraídos")

        datos_form = {"cedula_sistema": CEDULA} if CEDULA else {}
        r = pedir("POST", "/api/v1/validaciones/validar-identidad/", headers=A,
                  files={"file": (nombre, contenido, mime)}, data=datos_form)
        check("validar-identidad -> 200", r.status_code == 200, r.text[:300])
        if r.status_code == 200:
            d = r.json()
            check("validar-identidad no devuelve OCR (deprecado)", d.get("ocr") is None)
            print("        " + json.dumps(
                {k: d[k] for k in ("result", "message", "document_class", "confidence", "match_document")},
                ensure_ascii=False))
            print(f"        datos extraídos: {json.dumps(d.get('datos') or {}, ensure_ascii=False)[:300]}")
else:
    print("\n(Fase 5 de inferencia omitida: pasa la ruta de un documento como argumento para activarla.)")

# ================== Limpieza final ==================
if llave_consumo_id is not None:
    check("limpiar: revocar la clave de consumo temporal -> 204",
          pedir("DELETE", f"/api/v1/api-keys/{llave_consumo_id}", headers=A).status_code == 204)

print("\n" + "=" * 50)
if fallos:
    print(f"RESULTADO: {len(fallos)} prueba(s) fallaron -> {', '.join(fallos)}")
    sys.exit(1)
print("RESULTADO: el servicio real pasó todas las pruebas end-to-end.")
