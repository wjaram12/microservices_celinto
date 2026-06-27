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

```bash
pip install -r requirements.txt
```

> `ddddocr` arrastra `onnxruntime` (binario pesado). En Windows puede requerir el
> runtime de VC++. Instanciar el OCR cuesta ~1-2 s (se cachea como singleton).

## Cómo correr (desde la carpeta `services/`)

Se ejecuta como subpaquete (`consulta_titulos.*`) para poder importar `commons`.

1. **Redis** (opcional pero recomendado para ver la caché): si no hay Redis, el
   servicio sigue funcionando pero consulta la fuente cada vez (se ve en los logs).
2. **Servidor mock** (un solo worker):
   ```bash
   uvicorn consulta_titulos.mock.main:app --port 8090
   ```
3. **Servicio de consulta**:
   ```bash
   uvicorn consulta_titulos.main:app --port 8091
   ```

La configuración se lee del `.env` en la raíz `services/` (el mismo de las demás
apps): `DATABASE_URL` (api_keys), `REDIS_URL`, y para esta app `SENESCYT_BASE_URL`
y `VERIFY_SSL`.

## Uso

Todos los endpoints (salvo `/health`) exigen la cabecera `X-API-Key` con una clave
válida de la tabla `api_keys` (el mismo sistema del resto de servicios). La gestión
de caché requiere además scope `admin`.

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

# PDF oficial en base64 (SENESCYT no publica una URL del PDF; se entrega codificado)
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

El servicio (`consulta_titulos.main:app`) es **stateless** (el estado compartido
vive en Redis), así que escala con varios workers. El paquete `mock/` es **solo
para desarrollo** y NO se despliega.

1. **Configurar el entorno**: en el `.env` de `services/` (compartido) añadir:
   ```
   SENESCYT_BASE_URL=https://www.senescyt.gob.ec
   VERIFY_SSL=false
   REDIS_URL=redis://<host>:6379/0
   DATABASE_URL=postgresql://<user>:<pass>@<host>:5432/<db>   # tabla api_keys
   ```
   (`DATABASE_URL` ya existe para el clasificador; se reutiliza tal cual.)
2. **Instalar dependencias**: `pip install -r consulta_titulos/requirements.txt`.
3. **Arrancar desde `services/` (Linux, recomendado)**:
   ```bash
   gunicorn -c consulta_titulos/gunicorn.conf.py consulta_titulos.main:app
   ```
   En Windows (sin gunicorn):
   `uvicorn consulta_titulos.main:app --host 0.0.0.0 --port 8091 --workers 3`.

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
