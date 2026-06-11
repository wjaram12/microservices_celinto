"""
Prueba del registro de procesadores en PostgreSQL (app/procesadores.py).

Verifica, in-process (sin servidor, sin BD real, sin EXTEND_API_KEY real), que:

  1. Modo INLINE (siembra por defecto): /classify lleva config.classifications,
     /extract lleva config.schema, /parse lleva config.target=markdown.
  2. Modo PUBLICADO (se cambian filas a modo 'id' por el CRUD): /classify lleva
     classifier.id y /extract lleva processor.id, sin tocar documentos.py.
  3. El CRUD HTTP /api/v1/procesadores/ (scope admin) crea, rechaza duplicados
     (409), edita y borra.

psycopg2 (BD) y httpx (Extend) están simulados; el cliente falso captura el body
de cada llamada para poder afirmar sobre él.

Uso:
    .venv/Scripts/python.exe probar_procesadores.py
"""
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

os.environ.setdefault("EXTEND_API_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


# ---------- PostgreSQL simulado en memoria ----------
import psycopg2
from psycopg2 import errors as pg_errors

TABLAS = {"api_keys": [], "clasificaciones": [], "procesadores": [], "rutas": []}
SEQ = {"api_keys": 0, "clasificaciones": 0, "procesadores": 0, "rutas": 0}
TS = "2026-06-11 10:00:00"


def _unwrap(v):
    """psycopg2.extras.Json envuelve el dict en .adapted; lo desenvolvemos."""
    return getattr(v, "adapted", v)


def _tabla_de(low):
    for t in TABLAS:
        if t in low:
            return t
    return None


class Cur:
    def __init__(self, dict_mode=False):
        self._r = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, p=None):
        low = " ".join(sql.split()).lower()
        p = p or ()
        if low.startswith("create table") or low.startswith("alter table"):
            return
        if low.startswith("select count"):
            self._cnt = len(TABLAS[_tabla_de(low)])
            return

        # ----- procesadores (genérico: parsea los nombres de columna del SQL) -----
        if low.startswith("insert into procesadores"):
            cols = [c.strip() for c in
                    re.search(r"insert into procesadores \(([^)]+)\)", low).group(1).split(",")]
            SEQ["procesadores"] += 1
            row = {"id": SEQ["procesadores"], "ruta": "", "operacion": None, "clase": "",
                   "modo": "inline", "procesador_id": None, "version": None,
                   "esquema": None, "umbral": None, "activo": True,
                   "creado_en": TS, "actualizado_en": TS}
            for c, val in zip(cols, p):
                row[c] = _unwrap(val)
            # UNIQUE (ruta, operacion, clase)
            if any(r["ruta"] == row["ruta"] and r["operacion"] == row["operacion"]
                   and r["clase"] == row["clase"] for r in TABLAS["procesadores"]):
                raise pg_errors.UniqueViolation("duplicate key (ruta, operacion, clase)")
            TABLAS["procesadores"].append(row)
            self._r = [dict(row)]
            return
        if low.startswith("select") and "from procesadores" in low and "where id" in low:
            self._r = [r for r in TABLAS["procesadores"] if r["id"] == p[0]]
            return
        if low.startswith("select") and "from procesadores" in low and "where ruta" in low:
            self._r = [r for r in TABLAS["procesadores"]
                       if r["ruta"] == p[0] and r["operacion"] == p[1]
                       and r["clase"] == p[2] and r["activo"]]
            return
        if low.startswith("select") and "from procesadores" in low:
            rows = TABLAS["procesadores"]
            if "where activo = true" in low:
                rows = [r for r in rows if r["activo"]]
            self._r = sorted(rows, key=lambda r: (r["ruta"], r["operacion"], r["clase"]))
            return
        if low.startswith("update procesadores"):
            cols = [pt.split("=")[0].strip() for pt in
                    re.search(r"set (.+?) where id = %s", low).group(1).split(",") if "%s" in pt]
            *vals, id_ = p
            row = next((r for r in TABLAS["procesadores"] if r["id"] == id_), None)
            if row:
                for c, v in zip(cols, vals):
                    row[c] = _unwrap(v)
                row["actualizado_en"] = TS
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if low.startswith("delete from procesadores where ruta"):
            # Migración: retira la fila parse obsoleta de validar-identidad.
            TABLAS["procesadores"][:] = [
                r for r in TABLAS["procesadores"]
                if not (r["ruta"] == "validar-identidad" and r["operacion"] == "parse")
            ]
            return
        if low.startswith("delete from procesadores"):
            antes = len(TABLAS["procesadores"])
            TABLAS["procesadores"][:] = [r for r in TABLAS["procesadores"] if r["id"] != p[0]]
            self.rowcount = antes - len(TABLAS["procesadores"])
            return

        # ----- rutas -----
        if low.startswith("insert into rutas"):
            cols = [c.strip() for c in
                    re.search(r"insert into rutas \(([^)]+)\)", low).group(1).split(",")]
            SEQ["rutas"] += 1
            row = {"id": SEQ["rutas"], "clave": "", "url": "", "descripcion": "",
                   "activo": True, "creado_en": TS, "actualizado_en": TS}
            for c, val in zip(cols, p):
                row[c] = _unwrap(val)
            if any(r["clave"] == row["clave"] for r in TABLAS["rutas"]):
                raise pg_errors.UniqueViolation("duplicate key (clave)")
            TABLAS["rutas"].append(row)
            return
        if low.startswith("select clave from rutas"):
            self._r = [{"clave": r["clave"]} for r in TABLAS["rutas"] if r["activo"]]
            return
        if low.startswith("select") and "from rutas" in low and "where clave" in low:
            self._r = [r for r in TABLAS["rutas"] if r["clave"] == p[0]]
            return
        if low.startswith("select") and "from rutas" in low:
            rows = TABLAS["rutas"]
            if "where activo = true" in low:
                rows = [r for r in rows if r["activo"]]
            self._r = sorted(rows, key=lambda r: r["clave"])
            return
        if low.startswith("update rutas"):
            *vals, clave_ = p
            row = next((r for r in TABLAS["rutas"] if r["clave"] == clave_), None)
            if row:
                row.update({"url": vals[0], "descripcion": vals[1], "activo": vals[2],
                            "actualizado_en": TS})
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if low.startswith("delete from rutas"):
            antes = len(TABLAS["rutas"])
            TABLAS["rutas"][:] = [r for r in TABLAS["rutas"] if r["clave"] != p[0]]
            self.rowcount = antes - len(TABLAS["rutas"])
            return

        # ----- clasificaciones -----
        if low.startswith("insert into clasificaciones"):
            SEQ["clasificaciones"] += 1
            TABLAS["clasificaciones"].append({
                "id": SEQ["clasificaciones"], "clave": p[0], "tipo": p[1], "descripcion": p[2],
                "activo": p[3] if len(p) > 3 else True, "creado_en": TS, "actualizado_en": TS})
            return
        if low.startswith("select") and "from clasificaciones" in low and "where clave" in low:
            self._r = [r for r in TABLAS["clasificaciones"] if r["clave"] == p[0]]
            return
        if low.startswith("select") and "from clasificaciones" in low:
            rows = TABLAS["clasificaciones"]
            if "where activo = true" in low:
                rows = [r for r in rows if r["activo"]]
            self._r = sorted(rows, key=lambda r: r["clave"])
            return

        # ----- api_keys -----
        if low.startswith("insert into api_keys"):
            SEQ["api_keys"] += 1
            row = {"id": SEQ["api_keys"], "consumidor": p[0], "key_hash": p[1], "scope": p[2],
                   "activo": True, "creado_en": TS, "ultimo_uso": None}
            TABLAS["api_keys"].append(row)
            self._r = [dict(row)]
            return
        if low.startswith("select") and "from api_keys" in low and "where key_hash" in low:
            self._r = [{k: r[k] for k in ("id", "consumidor", "scope", "activo")}
                       for r in TABLAS["api_keys"] if r["key_hash"] == p[0]]
            return
        if low.startswith("update api_keys set ultimo_uso"):
            return
        raise AssertionError("SQL no manejado: " + low[:90])

    def fetchone(self):
        if hasattr(self, "_cnt"):
            return [self._cnt]
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)


class Con:
    closed = 0

    def cursor(self, cursor_factory=None):
        return Cur(cursor_factory is not None)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


psycopg2.connect = lambda *a, **k: Con()


# El pool real de psycopg2 inspecciona detalles internos de la conexión
# (info.transaction_status, etc.) que el mock no tiene; lo reemplazamos por un
# pool trivial que entrega/recibe la conexión simulada. Así se ejercita el
# código de ServicioBD (getconn/putconn/commit/rollback) sin esas internas.
import psycopg2.pool  # noqa: E402


class FakePool:
    def __init__(self, *a, **k):
        pass

    def getconn(self):
        return Con()

    def putconn(self, con, close=False):
        pass


psycopg2.pool.ThreadedConnectionPool = FakePool


# ---------- Extend (httpx) simulado, con captura de bodies ----------
import httpx

CAPTURAS = {"/classify": [], "/extract": [], "/parse": [], "/files/upload": []}

# Respuestas configurables del mock (las fases las cambian para simular clases
# distintas). El número de cédula viene "sucio" a propósito (guiones, puntos y
# espacios): la comparación de validar-identidad debe normalizarlo.
MOCK_CLASIFICACION = {"type": "CEDULA", "confidence": 0.95}
MOCK_EXTRACCION = {"numero_cedula": " 171-003.4065 ", "apellidos": "PEREZ", "nombres": "JUAN"}


def reiniciar_capturas():
    for k in CAPTURAS:
        CAPTURAS[k] = []


class FakeResp:
    def __init__(self, data, code=200):
        self._d = data
        self.status_code = code
        self.text = json.dumps(data)

    def json(self):
        return self._d


class FakeClient:
    def __init__(self, **k):
        pass

    async def request(self, metodo, ruta, **k):
        CAPTURAS.setdefault(ruta, []).append(k.get("json"))
        if ruta.startswith("/processors/") and "/versions/" in ruta:
            # CLASSIFY (cl_...) trae classifications; EXTRACT (ex_...) trae schema.
            if "/processors/cl_" in ruta:
                return FakeResp({"success": True, "version": {"id": "ver_b", "config": {
                    "classifications": [
                        {"id": "cedula_pub", "type": "CEDULA", "description": "Cédula publicada en Extend."},
                        {"id": "otros_pub", "type": "other", "description": "Descarte."}]}}})
            return FakeResp({"success": True, "version": {"id": "ver_b", "config": {
                "schema": {"type": "object", "properties": {
                    "campo_importado": {"type": ["string", "null"], "description": "de Extend"}}}}}})
        if metodo == "POST" and ruta.endswith("/publish"):
            # Publish Processor Version: publica el borrador como versión nueva.
            return FakeResp({"success": True, "version": {"id": "ver_new", "version": "2.1"}})
        if metodo == "POST" and ruta.startswith("/processors/"):
            # Update Processor: actualiza la config (versión borrador).
            return FakeResp({"success": True, "processor": {
                "id": ruta.split("/")[2], "draftVersion": {"id": "ver_draft"}}})
        if ruta == "/processors":
            return FakeResp({"success": True, "nextPageToken": None, "processors": [
                {"id": "cl_sync1", "name": "Clasificador Cédulas", "type": "CLASSIFY",
                 "versions": [{"id": "ver_a", "version": 1}, {"id": "ver_b", "version": 2}]},
            ]})
        if ruta == "/files/upload":
            return FakeResp({"id": "file_abc"})
        if ruta == "/classify":
            return FakeResp({"status": "PROCESSED", "output": dict(MOCK_CLASIFICACION)})
        if ruta == "/parse":
            return FakeResp({"status": "PROCESSED",
                             "output": {"chunks": [{"content": "REPUBLICA 1710034065 GUAYAS"}]}})
        if ruta == "/extract":
            return FakeResp({"status": "PROCESSED", "output": {"value": dict(MOCK_EXTRACCION)}})
        return FakeResp({}, 404)


httpx.AsyncClient = FakeClient


# ---------- App (importar DESPUÉS de instalar los mocks) ----------
from fastapi.testclient import TestClient  # noqa: E402

from app.services.consumidores import consumidores  # noqa: E402
from app.services.procesadores import procesadores  # noqa: E402
from app.main import app  # noqa: E402

cliente = TestClient(app)
consumo = consumidores.crear("celinto", "consumo")["llave"]
admin = consumidores.crear("admin", "admin")["llave"]
H = {"X-API-Key": consumo}
A = {"X-API-Key": admin}
ARCHIVO = {"file": ("cedula.png", b"\x89PNG", "image/png")}
CEDULA = {"cedula_sistema": "1710034065"}  # cédula ecuatoriana válida


# ---------- Mini-arnés de aserciones ----------
fallos = []


def check(nombre, condicion, detalle=""):
    icono = "[OK]  " if condicion else "[FALLA]"
    print(f"  {icono} {nombre}" + (f"  ({detalle})" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


def id_de(ruta, operacion, clase=""):
    """Busca el id de la fila (ruta, operacion, clase) en la tabla procesadores."""
    fila = next(p for p in procesadores.listar()
                if p["ruta"] == ruta and p["operacion"] == operacion and p["clase"] == clase)
    return fila["id"]


def validar():
    return cliente.post("/api/v1/validaciones/validar-identidad/",
                        files=ARCHIVO, data=CEDULA, headers=H)


# ================== FASE 1: modo INLINE (siembra por defecto) ==================
print("=== Fase 1: modo INLINE (siembra por defecto en la tabla) ===")
reiniciar_capturas()
r = validar()
check("HTTP 200", r.status_code == 200, f"fue {r.status_code}: {r.text[:200]}")
b_clas = CAPTURAS["/classify"][-1]
check("clasificar usa config.classifications",
      "config" in b_clas and "classifications" in b_clas["config"] and "classifier" not in b_clas)
check("clasificaciones inline no vacías", bool(b_clas.get("config", {}).get("classifications")))
b_ext = CAPTURAS["/extract"][-1]
check("extraer usa config.schema",
      "config" in b_ext and "schema" in b_ext["config"] and "processor" not in b_ext)
# validar-identidad usa SOLO clasificador + extractor: nunca llama al OCR.
check("validar-identidad NO llama a /parse (sin OCR)", len(CAPTURAS["/parse"]) == 0)
check("el campo ocr de la respuesta queda null (deprecado)", r.json().get("ocr") is None)

# La comparación normaliza AMBOS lados: el extractor devolvió ' 171-003.4065 '
# y la cédula del sistema fue '1710034065' -> deben coincidir.
check("compara el número NORMALIZADO (guiones/espacios fuera) -> coincide",
      r.json().get("match_document") is True, r.text[:300])
from app.services.documentos import normalizar_cedula, numero_en_datos  # noqa: E402
check("normalizar_cedula tolera int", normalizar_cedula(1710034065) == "1710034065")
check("normalizar_cedula tolera float", normalizar_cedula(1710034065.0) == "1710034065")
check("numero_en_datos encuentra claves alternativas",
      numero_en_datos({"numero_identificacion": "171.0034065"}) == "1710034065")
# El parse (OCR) es de la ruta /ocr.
cliente.post("/api/v1/ocr/", files=ARCHIVO, headers=H)
b_par = CAPTURAS["/parse"][-1]
check("la ruta /ocr usa config.target = markdown", b_par.get("config", {}).get("target") == "markdown")


# ================== FASE 2: modo PUBLICADO (CRUD -> modo 'id') ==================
print("\n=== Fase 2: modo PUBLICADO (se editan filas a modo 'id') ===")
RV = "validar-identidad"  # ruta que usa validar() (clasificador + extractor + parse)
procesadores.actualizar(id_de(RV, "clasificar"), modo="id",
                        procesador_id="cl_demo123", tocar_procesador_id=True)
procesadores.actualizar(id_de(RV, "extraer", "CEDULA"), modo="id",
                        procesador_id="ex_demo789", tocar_procesador_id=True)
reiniciar_capturas()
r = validar()
check("HTTP 200", r.status_code == 200, f"fue {r.status_code}: {r.text[:200]}")
b_clas = CAPTURAS["/classify"][-1]
check("clasificar usa classifier.id = cl_demo123",
      b_clas.get("classifier", {}).get("id") == "cl_demo123" and "config" not in b_clas)
b_ext = CAPTURAS["/extract"][-1]
check("extraer usa processor.id = ex_demo789",
      b_ext.get("processor", {}).get("id") == "ex_demo789" and "config" not in b_ext)
cliente.post("/api/v1/ocr/", files=ARCHIVO, headers=H)
b_par = CAPTURAS["/parse"][-1]
check("la ruta /ocr sigue usando config.target = markdown", b_par.get("config", {}).get("target") == "markdown")

# Volver a inline para el resto.
procesadores.actualizar(id_de(RV, "clasificar"), modo="inline",
                        procesador_id=None, tocar_procesador_id=True)
procesadores.actualizar(id_de(RV, "extraer", "CEDULA"), modo="inline",
                        procesador_id=None, tocar_procesador_id=True)


# ================== FASE 3: clase sin esquema ==================
print("\n=== Fase 3: extracción de una clase no soportada (modo inline) ===")
check("soporta_extraccion(CEDULA) es True", procesadores.soporta_extraccion(RV, "CEDULA"))
check("soporta_extraccion(OTROS) es False", not procesadores.soporta_extraccion(RV, "OTROS"))
check("cuerpo_extraccion(OTROS) es None", procesadores.cuerpo_extraccion(RV, "OTROS") is None)


# ================== FASE 4: CRUD HTTP /api/v1/procesadores/ ==================
print("\n=== Fase 4: CRUD HTTP (scope admin) ===")
nuevo = {"ruta": RV, "operacion": "extraer", "clase": "LICENCIA", "modo": "inline",
         "esquema": {"type": "object", "properties": {"numero": {"type": "string"}}}}
r = cliente.post("/api/v1/procesadores/", json=nuevo, headers=A)
check("crear -> 201", r.status_code == 201, f"fue {r.status_code}: {r.text[:200]}")
id_lic = r.json().get("id")
check("crear duplicado -> 409",
      cliente.post("/api/v1/procesadores/", json=nuevo, headers=A).status_code == 409)
check("crear modo id sin procesador_id -> 400",
      cliente.post("/api/v1/procesadores/",
                   json={"ruta": RV, "operacion": "extraer", "clase": "RUC", "modo": "id"},
                   headers=A).status_code == 400)
check("consumo no puede listar procesadores -> 403",
      cliente.get("/api/v1/procesadores/", headers=H).status_code == 403)
r = cliente.put(f"/api/v1/procesadores/{id_lic}",
                json={"modo": "id", "procesador_id": "ex_lic"}, headers=A)
check("editar a modo id -> 200", r.status_code == 200 and r.json()["procesador_id"] == "ex_lic")
check("ahora LICENCIA es extraíble", procesadores.soporta_extraccion(RV, "LICENCIA"))
check("borrar -> 204", cliente.delete(f"/api/v1/procesadores/{id_lic}", headers=A).status_code == 204)
check("LICENCIA ya no existe -> 404",
      cliente.get(f"/api/v1/procesadores/{id_lic}", headers=A).status_code == 404)


# ================== FASE 5: umbral de confianza configurable ==================
print("\n=== Fase 5: umbral configurable por ruta (la ruta /clasificar; da 0.95) ===")
RC = "clasificar"  # ruta del endpoint /clasificar
check("umbral por defecto = 0.85", abs(procesadores.umbral_clasificacion(RC) - 0.85) < 1e-9)
r = cliente.post("/api/v1/clasificar/", files=ARCHIVO, headers=H)
check("con umbral 0.85, confianza 0.95 -> válido", r.json().get("result") is True, r.text[:200])

procesadores.actualizar(id_de(RC, "clasificar"), umbral=0.99, tocar_umbral=True)
check("umbral ahora = 0.99", abs(procesadores.umbral_clasificacion(RC) - 0.99) < 1e-9)
r = cliente.post("/api/v1/clasificar/", files=ARCHIVO, headers=H)
check("con umbral 0.99, confianza 0.95 -> NO válido", r.json().get("result") is False, r.text[:200])

check("umbral fuera de rango (1.5) -> rechazado",
      cliente.post("/api/v1/procesadores/",
                   json={"ruta": RC, "operacion": "clasificar", "clase": "x", "modo": "inline", "umbral": 1.5},
                   headers=A).status_code in (400, 422))
procesadores.actualizar(id_de(RC, "clasificar"), umbral=0.85, tocar_umbral=True)  # restaurar


# ================== FASE 6: sincronización con Extend + versión ==================
print("\n=== Fase 6: sincronización con Extend Studio + pin de versión ===")
r = cliente.get("/api/v1/procesadores/extend?tipo=clasificar", headers=A)
check("GET /procesadores/extend?tipo=clasificar -> 200", r.status_code == 200, r.text[:200])
items = r.json() if r.status_code == 200 else []
check("devuelve procesadores de Extend con sus versiones",
      bool(items) and items[0]["id"] == "cl_sync1" and len(items[0]["versiones"]) == 2)
check("tipo inválido -> 400",
      cliente.get("/api/v1/procesadores/extend?tipo=zzz", headers=A).status_code == 400)
check("consumo no puede sincronizar -> 403",
      cliente.get("/api/v1/procesadores/extend?tipo=clasificar", headers=H).status_code == 403)

# Importar el esquema de un extractor de Extend (GET /processors/{id}/versions/{vid}).
r = cliente.get("/api/v1/procesadores/extend/esquema?procesador_id=ex_sync1&version_id=ver_b", headers=A)
check("importar esquema -> 200", r.status_code == 200, r.text[:200])
esq = r.json() if r.status_code == 200 else {}
check("el esquema importado trae properties", "campo_importado" in (esq.get("properties") or {}))
check("importar esquema sin version_id -> 422",
      cliente.get("/api/v1/procesadores/extend/esquema?procesador_id=ex_sync1", headers=A).status_code == 422)

# Fijar procesador + versión (ruta validar-identidad) y comprobar el body de /classify.
procesadores.actualizar(id_de(RV, "clasificar"), modo="id", procesador_id="cl_sync1",
                        version="2", tocar_procesador_id=True, tocar_version=True)
reiniciar_capturas()
validar()
b = CAPTURAS["/classify"][-1]
check("classify lleva id + versión fijada",
      b.get("classifier") == {"id": "cl_sync1", "version": "2"}, json.dumps(b))

# Sin versión -> body sin 'version' (retrocompatible).
procesadores.actualizar(id_de(RV, "clasificar"), version=None, tocar_version=True)
reiniciar_capturas()
validar()
b = CAPTURAS["/classify"][-1]
check("sin versión, classify lleva solo el id", b.get("classifier") == {"id": "cl_sync1"}, json.dumps(b))

procesadores.actualizar(id_de(RV, "clasificar"), modo="inline", procesador_id=None,
                        version=None, tocar_procesador_id=True, tocar_version=True)  # restaurar


# ================== FASE 7: páginas del panel (plantillas por view) ==================
print("\n=== Fase 7: páginas del panel /admin (Jinja2, una por view) ===")
r = cliente.get("/admin", follow_redirects=False)
check("/admin redirige a /admin/procesadores",
      r.status_code in (302, 307) and r.headers.get("location") == "/admin/procesadores")
for pagina, marca in (("consumidores", "Nueva API key"),
                      ("procesadores", "Nuevo procesador"),
                      ("rutas", "Nueva ruta")):
    r = cliente.get(f"/admin/{pagina}")
    check(f"/admin/{pagina} -> 200 y renderiza su plantilla",
          r.status_code == 200 and marca in r.text, f"HTTP {r.status_code}")
check("la página de prompts ya no existe -> 404",
      cliente.get("/admin/prompts").status_code == 404)
check("la API de prompts sigue viva (la usa el modo inline)",
      cliente.get("/api/v1/prompts/", headers=A).status_code == 200)
r = cliente.get("/admin/procesadores")
check("el panel usa los tokens de Material Design 3", "--md-primary" in r.text)
check("el panel referencia jQuery local (sin CDN)", 'src="/static/js/jquery.min.js"' in r.text)
check("el panel tiene el loader global", 'id="cargador"' in r.text)
check("el panel usa tablas con modales (layout CRM)",
      'class="tabla"' in r.text and "modal-fondo" in r.text and "<aside>" in r.text)
check("anti-parpadeo del login al navegar (clase con-sesion antes de pintar)",
      "con-sesion" in r.text and "html.con-sesion #acceso" in r.text)
r = cliente.get("/static/js/jquery.min.js")
check("jQuery vendorizado se sirve desde /static", r.status_code == 200 and "jQuery v3" in r.text[:100])


# ================== FASE 8: CRUD de rutas (URLs) y su unión con procesadores ==================
print("\n=== Fase 8: CRUD de rutas y unión rutas <-> procesadores ===")
r = cliente.get("/api/v1/rutas/", headers=A)
check("listar rutas -> 200 con las rutas sembradas",
      r.status_code == 200 and
      {"clasificar", "validar-identidad", "ocr", "validar-registro-senescyt"} <= {x["clave"] for x in r.json()})
check("consumo no puede listar rutas -> 403",
      cliente.get("/api/v1/rutas/", headers=H).status_code == 403)
nueva = {"clave": "verificar-titulo", "url": "/api/v1/titulos/verificar/",
         "descripcion": "Verifica títulos académicos."}
check("crear ruta -> 201", cliente.post("/api/v1/rutas/", json=nueva, headers=A).status_code == 201)
check("crear ruta duplicada -> 409",
      cliente.post("/api/v1/rutas/", json=nueva, headers=A).status_code == 409)
check("url sin '/' inicial -> 400",
      cliente.post("/api/v1/rutas/", json={"clave": "x", "url": "sin-barra"},
                   headers=A).status_code == 400)

# La unión: el CRUD de procesadores valida la ruta contra el catálogo.
check("procesador con ruta no registrada -> 400",
      cliente.post("/api/v1/procesadores/",
                   json={"ruta": "inexistente", "operacion": "parse", "clase": "", "modo": "inline"},
                   headers=A).status_code == 400)
r = cliente.post("/api/v1/procesadores/",
                 json={"ruta": "verificar-titulo", "operacion": "parse", "clase": "", "modo": "inline"},
                 headers=A)
check("procesador asociado a la ruta nueva -> 201", r.status_code == 201, r.text[:200])
id_pv = r.json().get("id")
check("eliminar ruta con procesadores asociados -> 409",
      cliente.delete("/api/v1/rutas/verificar-titulo", headers=A).status_code == 409)
check("borrar el procesador asociado -> 204",
      cliente.delete(f"/api/v1/procesadores/{id_pv}", headers=A).status_code == 204)
check("ahora la ruta sí se elimina -> 204",
      cliente.delete("/api/v1/rutas/verificar-titulo", headers=A).status_code == 204)


# ================== FASE 9: clasificaciones propias por clasificador ==================
print("\n=== Fase 9: esquema (clasificaciones propias) en clasificadores ===")
id_rc = id_de(RC, "clasificar")
propias = {"classifications": [
    {"id": "matricula", "type": "MATRICULA", "description": "Matrícula vehicular ecuatoriana."},
]}
r = cliente.put(f"/api/v1/procesadores/{id_rc}", json={"esquema": propias}, headers=A)
check("guardar clasificaciones propias -> 200", r.status_code == 200, r.text[:200])
reiniciar_capturas()
cliente.post("/api/v1/clasificar/", files=ARCHIVO, headers=H)
cls = CAPTURAS["/classify"][-1].get("config", {}).get("classifications", [])
check("classify usa las clasificaciones propias de la fila",
      any(c.get("id") == "matricula" for c in cls))
check("se garantiza la clase de descarte 'other'",
      any(c.get("type") == "other" for c in cls))
check("las globales (cedula) NO se usan en esta ruta",
      not any(c.get("id") == "cedula" for c in cls))

# Sin esquema -> vuelve a los prompts globales de la tabla `clasificaciones`.
cliente.put(f"/api/v1/procesadores/{id_rc}", json={"esquema": None}, headers=A)
reiniciar_capturas()
cliente.post("/api/v1/clasificar/", files=ARCHIVO, headers=H)
cls = CAPTURAS["/classify"][-1].get("config", {}).get("classifications", [])
check("sin esquema vuelve a los prompts globales",
      any(c.get("id") == "cedula" for c in cls))

check("esquema inválido para clasificar -> 400",
      cliente.put(f"/api/v1/procesadores/{id_rc}", json={"esquema": {"x": 1}},
                  headers=A).status_code == 400)

# Importar las clasificaciones de un clasificador PUBLICADO en Extend (mismo
# endpoint que el de extracción: devuelve schema o classifications según tipo).
r = cliente.get("/api/v1/procesadores/extend/esquema?procesador_id=cl_sync1&version_id=ver_b", headers=A)
check("importar clasificaciones de un clasificador publicado -> 200",
      r.status_code == 200, r.text[:200])
clasifs = (r.json() or {}).get("classifications") or []
check("devuelve la lista classifications del clasificador",
      any(c.get("id") == "cedula_pub" for c in clasifs))

# Empujar el esquema guardado AL procesador publicado (actualiza su borrador).
cliente.put(f"/api/v1/procesadores/{id_rc}",
            json={"esquema": propias, "modo": "id", "procesador_id": "cl_sync1"}, headers=A)
r = cliente.post(f"/api/v1/procesadores/{id_rc}/extend", headers=A)
check("actualizar clasificador en Extend -> 200", r.status_code == 200, r.text[:200])
b = (CAPTURAS.get("/processors/cl_sync1") or [None])[-1] or {}
check("se envió config CLASSIFY con las clasificaciones propias",
      b.get("config", {}).get("type") == "CLASSIFY"
      and any(c.get("id") == "matricula" for c in b["config"].get("classifications", [])))
check("el push incluye la clase de descarte 'other'",
      any(c.get("type") == "other" for c in b.get("config", {}).get("classifications", [])))
check("el push sin publicar NO publica versión",
      r.json().get("version_publicada") is None)

# Push + autopublicación de la versión (release minor).
r = cliente.post(f"/api/v1/procesadores/{id_rc}/extend?publicar=true", headers=A)
check("push + autopublicar -> 200 con la versión nueva",
      r.status_code == 200 and r.json().get("version_publicada") == "2.1", r.text[:200])
pub = (CAPTURAS.get("/processors/cl_sync1/publish") or [None])[-1] or {}
check("la publicación pidió release minor", pub.get("releaseType") == "minor")

id_ext = id_de(RV, "extraer", "CEDULA")
cliente.put(f"/api/v1/procesadores/{id_ext}",
            json={"modo": "id", "procesador_id": "ex_sync9"}, headers=A)
r = cliente.post(f"/api/v1/procesadores/{id_ext}/extend", headers=A)
check("actualizar extractor en Extend -> 200", r.status_code == 200, r.text[:200])
b = (CAPTURAS.get("/processors/ex_sync9") or [None])[-1] or {}
check("se envió config EXTRACT con el JSON Schema",
      b.get("config", {}).get("type") == "EXTRACT"
      and "numero_cedula" in (b["config"].get("schema", {}).get("properties") or {}))

check("fila sin procesador_id -> 400",
      cliente.post(f"/api/v1/procesadores/{id_de('ocr', 'parse')}/extend", headers=A).status_code == 400)

# Restaurar el estado por defecto de las filas tocadas.
cliente.put(f"/api/v1/procesadores/{id_rc}",
            json={"esquema": None, "modo": "inline", "procesador_id": None}, headers=A)
cliente.put(f"/api/v1/procesadores/{id_ext}",
            json={"modo": "inline", "procesador_id": None}, headers=A)


# ================== FASE 10: comparación según la clase (cédula o PASAPORTE) ==================
print("\n=== Fase 10: validar-identidad compara según la clase detectada ===")
MOCK_CLASIFICACION.update({"type": "PASAPORTE", "confidence": 0.97})
MOCK_EXTRACCION.clear()
MOCK_EXTRACCION.update({"numero_pasaporte": " a123-4567 ", "apellidos": "PEREZ"})

r = cliente.post("/api/v1/validaciones/validar-identidad/", files=ARCHIVO,
                 data={"cedula_sistema": "A1234567"}, headers=H)
check("pasaporte + número del sistema -> 200", r.status_code == 200, r.text[:300])
d = r.json() if r.status_code == 200 else {}
check("pasaporte es identidad (result True)", d.get("result") is True)
check("compara alfanumérico normalizado -> coincide",
      d.get("match_document") is True, r.text[:300])

r = cliente.post("/api/v1/validaciones/validar-identidad/", files=ARCHIVO,
                 data={"cedula_sistema": "B9999999"}, headers=H)
check("pasaporte con número distinto -> NO coincide",
      r.status_code == 200 and r.json().get("match_document") is False)

check("identificación demasiado corta -> 400",
      cliente.post("/api/v1/validaciones/validar-identidad/", files=ARCHIVO,
                   data={"cedula_sistema": "AB1"}, headers=H).status_code == 400)
check("cédula numérica inválida sigue dando 400 (fail-fast)",
      cliente.post("/api/v1/validaciones/validar-identidad/", files=ARCHIVO,
                   data={"cedula_sistema": "1234567890"}, headers=H).status_code == 400)

# Restaurar el mock para no contaminar otras corridas.
MOCK_CLASIFICACION.update({"type": "CEDULA", "confidence": 0.95})
MOCK_EXTRACCION.clear()
MOCK_EXTRACCION.update({"numero_cedula": " 171-003.4065 ", "apellidos": "PEREZ", "nombres": "JUAN"})


# ================== FASE 11: cache de config (TTL + invalidación) ==================
print("\n=== Fase 11: cache de configuración (pool + TTL) ===")
from app.core.cache import CacheTTL  # noqa: E402
_n = {"v": 0}
def _cargar():
    _n["v"] += 1
    return _n["v"]
c = CacheTTL(ttl_segundos=60)
check("primera lectura carga del origen", c.obtener(_cargar) == 1)
check("segunda lectura usa el cache (no recarga)", c.obtener(_cargar) == 1 and _n["v"] == 1)
c.invalidar()
check("tras invalidar, recarga", c.obtener(_cargar) == 2 and _n["v"] == 2)

# Invalidación end-to-end: editar un procesador se refleja en el resolutor.
procesadores.actualizar(id_de(RC, "clasificar"), umbral=0.5, tocar_umbral=True)
check("escribir invalida el cache (el resolutor ve el cambio)",
      abs(procesadores.umbral_clasificacion(RC) - 0.5) < 1e-9)
procesadores.actualizar(id_de(RC, "clasificar"), umbral=0.85, tocar_umbral=True)
check("el pool reemplaza la conexión-por-operación",
      type(__import__("app.core.db", fromlist=["_obtener_pool"])._obtener_pool()).__name__ == "FakePool")


# ================== FASE 12: ruta validar-registro-senescyt ==================
print("\n=== Fase 12: ruta validar-registro-senescyt (extractor personalizado) ===")
rutas_cat = {x["clave"] for x in cliente.get("/api/v1/rutas/", headers=A).json()}
check("la ruta validar-registro-senescyt está en el catálogo",
      "validar-registro-senescyt" in rutas_cat)
procs_sen = [p for p in cliente.get("/api/v1/procesadores/", headers=A).json()
             if p["ruta"] == "validar-registro-senescyt"]
check("tiene su clasificador y su extractor",
      {(p["operacion"], p["clase"]) for p in procs_sen}
      == {("clasificar", ""), ("extraer", "REGISTRO_SENESCYT")})

MOCK_CLASIFICACION.update({"type": "REGISTRO_SENESCYT", "confidence": 0.96})
MOCK_EXTRACCION.clear()
MOCK_EXTRACCION.update({"numero_registro": "1234-2026-ABC", "titulo": "Magíster en Educación",
                        "institucion": "Universidad Casa Grande"})
reiniciar_capturas()
r = cliente.post("/api/v1/validaciones/validar-registro-senescyt/", files=ARCHIVO, headers=H)
check("validar-registro-senescyt -> 200", r.status_code == 200, r.text[:300])
d = r.json() if r.status_code == 200 else {}
check("reconoce el registro (result True)", d.get("result") is True)
check("devuelve los datos extraídos",
      (d.get("datos") or {}).get("numero_registro") == "1234-2026-ABC")
cls = CAPTURAS["/classify"][-1].get("config", {}).get("classifications", [])
check("clasifica con las clasificaciones propias de la ruta",
      any(c.get("type") == "REGISTRO_SENESCYT" for c in cls))
ext = CAPTURAS["/extract"][-1].get("config", {}).get("schema", {}).get("properties", {})
check("extrae con el esquema SENESCYT", "numero_registro" in ext)

# Paso 2: reconocido como SENESCYT pero SIN número de registro -> no válido.
MOCK_EXTRACCION.clear()
MOCK_EXTRACCION.update({"titulo": "Magíster", "institucion": "UCG"})
r = cliente.post("/api/v1/validaciones/validar-registro-senescyt/", files=ARCHIVO, headers=H)
check("reconocido pero sin número de registro -> result False",
      r.status_code == 200 and r.json().get("result") is False, r.text[:200])
check("el mensaje avisa que falta el número de registro",
      "número de registro" in (r.json().get("message") or ""))
MOCK_EXTRACCION.clear()
MOCK_EXTRACCION.update({"numero_registro": "1234-2026-ABC", "titulo": "Magíster en Educación",
                        "institucion": "Universidad Casa Grande"})

MOCK_CLASIFICACION.update({"type": "CEDULA", "confidence": 0.97})
r = cliente.post("/api/v1/validaciones/validar-registro-senescyt/", files=ARCHIVO, headers=H)
check("documento ajeno -> result False y datos vacíos",
      r.status_code == 200 and r.json().get("result") is False and r.json().get("datos") == {})

MOCK_CLASIFICACION.update({"type": "CEDULA", "confidence": 0.95})
MOCK_EXTRACCION.clear()
MOCK_EXTRACCION.update({"numero_cedula": " 171-003.4065 ", "apellidos": "PEREZ", "nombres": "JUAN"})


print("\n" + ("=" * 50))
if fallos:
    print(f"RESULTADO: {len(fallos)} asercion(es) fallaron -> {', '.join(fallos)}")
    raise SystemExit(1)
print("RESULTADO: todas las aserciones pasaron. La tabla procesadores resuelve "
      "ambos modos y el CRUD admin funciona.")
