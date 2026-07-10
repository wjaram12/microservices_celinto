# Consulta de Títulos SENESCYT (servicio)

Servicio **solo API** (sin interfaz) que consulta los títulos registrados de una
persona en el portal público de SENESCYT (Ecuador), resolviendo el **captcha por
OCR** automáticamente (`ddddocr`) y **cacheando** el resultado por cédula en Redis
con **TTL de 30 días**.

Incluye un **servidor mock de alta fidelidad** que recrea el portal real
(imágenes de captcha reales + HTML JSF/PrimeFaces), de modo que todo el pipeline
—captcha → OCR → consulta → parseo → caché— se ejercita end-to-end sin depender
del portal real. Para consultar el portal real solo se cambia `SENESCYT_BASE_URL`.

## Estructura

Vive bajo `services/` y **reutiliza la infraestructura compartida** del paquete
`services/commons/` (config base DB/Redis, pool de PostgreSQL, cliente Redis y el
sistema de API keys), en vez de duplicarla. Solo aporta lo propio del dominio:

```
services/
  commons/         infraestructura compartida (config, db, redis_cache, consumidores, seguridad)
  consulta_titulos/
    config.py      hereda de commons (añade SENESCYT_BASE_URL, VERIFY_SSL, TTL)
    scraper.py     SenescytScraper portable (requests + bs4 + ddddocr + urllib3)
    fuente.py      FuenteSenescyt: envuelve el scraper y normaliza la respuesta
    cache.py       NÚCLEO DE CACHÉ: Redis SETEX 30 días, modos auto/local/senescyt
    schemas.py     modelos Pydantic de entrada/salida
    main.py        app FastAPI (auth vía commons.seguridad)
    mock/          servidor mock que imita el portal SENESCYT (solo desarrollo)
```

## Instalación

Las dependencias de todo el repo viven en un único `services/requirements.txt`:

```bash
pip install -r requirements.txt   # desde services/
```

> `ddddocr` arrastra `onnxruntime` (binario pesado). En Windows puede requerir el
> runtime de VC++. Instanciar el OCR cuesta ~1-2 s (se cachea como singleton).

## Cómo correr (desde la carpeta `services/`)

Se ejecuta como subpaquete (`consulta_titulos.*`) para poder importar `commons`.
Hay dos modos:

### A) Unificado en el clasificador (recomendado) — un solo servidor
El router de consulta de títulos se monta dentro de `app/main.py`, así el MISMO
proceso del clasificador sirve todo. No hay que levantar nada aparte:
```bash
uvicorn app.main:app --port 8000        # o el gunicorn del clasificador
```
Las rutas quedan bajo `/api/v1/`:
`/api/v1/consulta-titulos/`, `/api/v1/consulta-titulos/{cedula}/pdf`, etc.

### B) Standalone — servidor propio
```bash
uvicorn consulta_titulos.main:app --port 8091
```
Aquí las rutas van sin el prefijo `/api/v1` (`/consulta-titulos/`, `/health`, ...).

En ambos: **Redis** recomendado (sin él funciona pero consulta la fuente cada vez)
y, para probar sin el portal real, levantar el mock:
```bash
uvicorn consulta_titulos.mock.main:app --port 8090
```

La configuración se lee del `.env` en la raíz `services/` (el mismo de las demás
apps): `DATABASE_URL` (api_keys), `REDIS_URL`, y para esta app `SENESCYT_BASE_URL`
y `VERIFY_SSL`.

## Uso

Todos los endpoints exigen la cabecera `X-API-Key` con una clave válida de la tabla
`api_keys` (el mismo sistema del resto de servicios). La gestión de caché requiere
además scope `admin`.

> Los ejemplos usan el modo **standalone** (`:8091`, rutas sin prefijo). En el modo
> **unificado** la base es `http://<host>:8000/api/v1/consulta-titulos/` y el
> reinicio de caché es `POST /api/v1/consulta-titulos/cache/reiniciar`.

```bash
# Primera consulta (en vivo: resuelve captcha por OCR) -> fuente="senescyt"
curl -X POST http://localhost:8091/consulta-titulos/ \
  -H "X-API-Key: wsk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"identificacion":"0912345678"}'

# Segunda consulta (desde caché) -> fuente="cache", vigente=true
curl -X POST http://localhost:8091/consulta-titulos/ \
  -H "X-API-Key: wsk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"identificacion":"0912345678"}'

# Forzar refresco (ignora la caché)
curl -X POST http://localhost:8091/consulta-titulos/ \
  -H "X-API-Key: wsk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"identificacion":"0912345678","force_refresh":true}'

# Consulta + PDF en la MISMA respuesta (campo pdf_base64). Opt-in: por defecto
# incluir_pdf=false para ahorrar ancho de banda (~1 MB).
curl -X POST http://localhost:8091/consulta-titulos/ \
  -H "X-API-Key: wsk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"identificacion":"0912345678","incluir_pdf":true}'

# PDF solo (endpoint dedicado), también en base64
curl http://localhost:8091/consulta-titulos/0912345678/pdf \
  -H "X-API-Key: wsk_xxxxxxxx"

# Invalidar una cédula (títulos + PDF) -- requiere clave con scope admin
curl -X DELETE http://localhost:8091/consulta-titulos/0912345678 \
  -H "X-API-Key: wsk_admin_xxxx"

# Vaciar toda la caché -- requiere clave con scope admin
curl -X POST http://localhost:8091/cache/reiniciar \
  -H "X-API-Key: wsk_admin_xxxx"
```

### Cédulas de ejemplo en el mock
- `0912345678` — Juan Carlos Pérez Morales (2 títulos: tercer y cuarto nivel)
- `1700000001` — María Fernanda González Torres (1 título)
- `0102030405` — Luis Andrés González Ramírez (1 título)
- Buscar por apellido `GONZALEZ` (sin cédula) devuelve un **listado** de 2 personas.
- Cualquier otra cédula → `no_encontrado`.

## Despliegue en producción

**Recomendado: unificado en el clasificador** (un solo servidor/proceso). Como las
rutas se montan en `app/main.py`, basta con desplegar el clasificador como siempre;
la consulta de títulos viaja con él. El paquete `mock/` es **solo desarrollo**.

1. **Configurar el entorno**: en el `.env` de `services/` (compartido) añadir las 2
   variables nuevas (las demás ya existen para el clasificador):
   ```
   SENESCYT_BASE_URL=https://www.senescyt.gob.ec
   VERIFY_SSL=false
   # DATABASE_URL y REDIS_URL ya están (se reutilizan tal cual).
   ```
2. **Instalar dependencias** (añade ddddocr/onnxruntime/opencv al venv del server):
   `pip install -r requirements.txt` desde `services/`.
   En Linux, opencv (de ddddocr) necesita libs del sistema; si falta `libGL.so.1`:
   `sudo apt-get install -y libgl1 libglib2.0-0 libsm6 libxext6 libxrender1`
   (en Ubuntu viejos, `libgl1-mesa-glx` en vez de `libgl1`).
3. **Arrancar el clasificador** (ya sirve también la consulta de títulos):
   ```bash
   gunicorn -c gunicorn.conf.py app.main:app          # o como ya lo despliegues
   ```

> Alternativa **standalone** (servidor aparte, otro puerto):
> `gunicorn -c consulta_titulos/gunicorn.conf.py consulta_titulos.main:app`
> (Windows: `uvicorn consulta_titulos.main:app --host 0.0.0.0 --port 8091`).

### Notas de producción
- **Redis es necesario** para que la caché funcione (sin él el servicio sigue
  arriba pero consulta el portal en cada petición; se ve en los logs).
- **Latencia**: una consulta EN VIVO al portal real tarda 30-50 s y, con reintentos
  de captcha, más; por eso `gunicorn.conf.py` usa `timeout=180`. Las consultas
  cacheadas (30 días) responden al instante.
- **OCR**: ddddocr se precarga al arrancar cada worker (warm-up) y su acceso está
  protegido con lock (el modelo se comparte entre los hilos del worker).
- **Seguridad**: usa el MISMO sistema de API keys que el resto de servicios — la
  cabecera `X-API-Key` se valida contra la tabla `api_keys` de `DATABASE_URL`
  (las claves `wsk_...` de los consumidores existentes sirven aquí). Las consultas
  exigen una clave válida; la gestión de caché (DELETE/`reiniciar`) exige scope
  `admin`. `/health` queda abierto para los probes.

## Apuntar al portal real

En `.env` (o variables de entorno):

```
SENESCYT_BASE_URL=https://www.senescyt.gob.ec
VERIFY_SSL=false
```

No hay que tocar código: el scraper usa esas variables.
