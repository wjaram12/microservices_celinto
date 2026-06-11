"""Prueba integral de la migración a Extend (httpx + psycopg2 simulados)."""
import json
import psycopg2

# ---------- Postgres simulado en memoria ----------
TABLAS = {"api_keys": [], "clasificaciones": []}
SEQ = {"api_keys": 0, "clasificaciones": 0}

class Cur:
    def __init__(s, dict_mode=False): s._r = []; s.rowcount = -1
    def __enter__(s): return s
    def __exit__(s, *a): return False
    def execute(s, sql, p=None):
        low = " ".join(sql.split()).lower(); p = p or ()
        if low.startswith("create table"): return
        if "from clasificaciones" in low and low.startswith("select count"):
            s._r = [{"count": len(TABLAS["clasificaciones"])}]; s._cnt = len(TABLAS["clasificaciones"]); return
        # clasificaciones
        if low.startswith("insert into clasificaciones"):
            SEQ["clasificaciones"] += 1
            row = {"id": SEQ["clasificaciones"], "clave": p[0], "tipo": p[1], "descripcion": p[2],
                   "activo": p[3] if len(p) > 3 else True, "creado_en": "2026-06-10 10:00:00",
                   "actualizado_en": "2026-06-10 10:00:00"}
            TABLAS["clasificaciones"].append(row); return
        if low.startswith("select") and "from clasificaciones" in low and "where clave" in low:
            s._r = [r for r in TABLAS["clasificaciones"] if r["clave"] == p[0]]; return
        if low.startswith("select") and "from clasificaciones" in low:
            rows = TABLAS["clasificaciones"]
            if "where activo = true" in low: rows = [r for r in rows if r["activo"]]
            s._r = sorted(rows, key=lambda r: r["clave"]); return
        # api_keys
        if low.startswith("insert into api_keys"):
            SEQ["api_keys"] += 1
            row = {"id": SEQ["api_keys"], "consumidor": p[0], "key_hash": p[1], "scope": p[2],
                   "activo": True, "creado_en": "2026-06-10 10:00:00", "ultimo_uso": None}
            TABLAS["api_keys"].append(row); s._r = [dict(row)]; return
        if low.startswith("select") and "from api_keys" in low and "where key_hash" in low:
            s._r = [{k: r[k] for k in ("id", "consumidor", "scope", "activo")}
                    for r in TABLAS["api_keys"] if r["key_hash"] == p[0]]; return
        if low.startswith("update api_keys set ultimo_uso"): return
        raise AssertionError("SQL no manejado: " + low[:80])
    def fetchone(s):
        if hasattr(s, "_cnt"): return [s._cnt]
        return s._r[0] if s._r else None
    def fetchall(s): return list(s._r)
    def executemany(s, sql, seq):
        for p in seq: s.execute(sql, p)
class Con:
    def cursor(s, cursor_factory=None): return Cur(cursor_factory is not None)
    def commit(s): pass
    def rollback(s): pass
    def close(s): pass
psycopg2.connect = lambda *a, **k: Con()

# ---------- Extend (httpx) simulado ----------
import httpx
class FakeResp:
    def __init__(s, data, code=200): s._d = data; s.status_code = code; s.text = json.dumps(data)
    def json(s): return s._d
class FakeClient:
    def __init__(s, **k): pass
    async def request(s, metodo, ruta, **k):
        if ruta == "/files/upload": return FakeResp({"id": "file_abc"})
        if ruta == "/classify":
            return FakeResp({"status": "PROCESSED", "output": {"type": "CEDULA", "confidence": 0.95}})
        if ruta == "/parse":
            return FakeResp({"status": "PROCESSED", "output": {"chunks": [{"content": "REPUBLICA 1710034065 GUAYAS"}]}})
        if ruta == "/extract":
            return FakeResp({"status": "PROCESSED", "output": {"value": {
                "numero_cedula": "1710034065", "apellidos": "PEREZ", "nombres": "JUAN"}}})
        return FakeResp({}, 404)
httpx.AsyncClient = FakeClient

# ---------- App ----------
from app import seguridad
from app.main import app
from fastapi.testclient import TestClient

c = TestClient(app)
admin = seguridad.crear_llave("admin", "admin")["llave"]
consumo = seguridad.crear_llave("celinto", "consumo")["llave"]
A = {"X-API-Key": admin}; H = {"X-API-Key": consumo}
arch = {"file": ("x.png", b"\x89PNG", "image/png")}

print("=== prompts CRUD (admin) ===")
print("listar:", [p["clave"] for p in c.get("/api/v1/prompts/", headers=A).json()])
r = c.post("/api/v1/prompts/", json={"clave": "licencia", "tipo": "LICENCIA",
          "descripcion": "Licencia de conducir ecuatoriana con foto y categorías."}, headers=A)
print("crear:", r.status_code, r.json().get("clave"))
print("crear duplicado:", c.post("/api/v1/prompts/", json={"clave": "licencia", "tipo": "X",
          "descripcion": "duplicado de prueba"}, headers=A).status_code, "(409)")
print("consumo no puede listar prompts:", c.get("/api/v1/prompts/", headers=H).status_code, "(403)")

print("\n=== inferencia (Extend) ===")
print("CLASIFICAR:", json.dumps(c.post("/api/v1/clasificar/", files=arch, headers=H).json(), ensure_ascii=False))
print("OCR:", json.dumps(c.post("/api/v1/ocr/", files=arch, data={"texto_a_buscar": "guayas"}, headers=H).json(), ensure_ascii=False))
print("VALIDAR (con número):")
print(json.dumps(c.post("/api/v1/validaciones/validar-identidad/", files=arch,
      data={"cedula_sistema": "1710034065"}, headers=H).json(), indent=2, ensure_ascii=False))
print("\nsin API key:", c.post("/api/v1/clasificar/", files=arch).status_code, "(401)")
