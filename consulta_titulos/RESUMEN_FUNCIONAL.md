# Consulta de Títulos SENESCYT — Resumen funcional

## Qué hace

Microservicio **solo API** (sin interfaz gráfica) que consulta los títulos
académicos registrados de una persona en el **portal público de SENESCYT
(Ecuador)**. Automatiza de punta a punta un trámite que normalmente exige
resolver un captcha a mano: el servicio **resuelve el captcha por OCR**
(`ddddocr`), consulta el portal, parsea los resultados y **cachea la respuesta
por cédula durante 30 días**, de modo que las consultas repetidas responden al
instante.

## Capacidades principales

- **Consulta por cédula o por apellidos.** Con la cédula (10 dígitos) o pasaporte
  devuelve el detalle directo; por apellidos devuelve un listado de coincidencias.
- **Resolución automática de captcha (OCR).** Sin intervención humana; reintenta
  si el portal rechaza el captcha.
- **Caché de 30 días en Redis.** La primera consulta va en vivo al portal
  (`fuente: "senescyt"`, tarda unos segundos); las siguientes salen de caché
  (`fuente: "cache"`, instantáneas). Se puede forzar refresco.
- **PDF oficial del título.** Descarga el certificado en PDF que emite SENESCYT y
  lo entrega en base64 (el portal no expone una URL pública del PDF). Opt-in para
  ahorrar ancho de banda (~1 MB).
- **Modos de búsqueda:** `auto` (caché y, si no hay, en vivo), `local` (solo
  caché) y `senescyt` (siempre en vivo).
- **Gestión de caché:** invalidar una cédula concreta o vaciar toda la caché
  (operaciones protegidas con clave admin).

## Datos que devuelve

Por cada persona consultada:

- **Datos del titulado:** identificación, nombres, género, nacionalidad.
- **Lista de títulos**, cada uno con: categoría (tercer/cuarto nivel), título,
  institución de educación superior, tipo (nacional/extranjero), número y fecha
  de registro, y área/campo de conocimiento.
- **PDF oficial** del registro (opcional, en base64).

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/api/v1/consulta-titulos/` | Consulta los títulos de una persona (opcionalmente con el PDF). |
| `GET`  | `/api/v1/consulta-titulos/{cedula}/pdf` | Devuelve solo el PDF oficial en base64. |
| `DELETE` | `/api/v1/consulta-titulos/{cedula}` | Borra de la caché esa cédula *(clave admin)*. |
| `POST` | `/api/v1/consulta-titulos/cache/reiniciar` | Vacía toda la caché *(clave admin)*. |

Todas las peticiones exigen la cabecera `X-API-Key` con una clave válida; sin ella
responde `401`. Las operaciones de gestión de caché exigen además scope `admin`.

## Arquitectura e integración

- Vive bajo `services/` y **reutiliza la infraestructura compartida** del paquete
  `services/commons/` (configuración base de DB/Redis, pool de PostgreSQL, cliente
  Redis y el sistema de API keys), en lugar de duplicarla.
- **Autenticación unificada:** usa el MISMO sistema de API keys (`wsk_...`) que el
  resto de servicios, validando contra la tabla `api_keys`.
- **Despliegue recomendado: unificado en el clasificador.** El router se monta
  dentro de `app/main.py`, así el mismo proceso sirve todo (rutas bajo
  `/api/v1/consulta-titulos/`). También puede correr standalone en su propio puerto.
- **Núcleo portable:** el scraper depende solo de `requests`, `beautifulsoup4`,
  `ddddocr` y `urllib3` (Python puro, sin acoplamiento a un framework).

### Componentes

| Archivo | Rol |
|---------|-----|
| `scraper.py` | Núcleo portable que habla con el portal JSF/PrimeFaces de SENESCYT. |
| `fuente.py` | Envuelve el scraper y normaliza la respuesta. |
| `cache.py` | Núcleo de caché: Redis `SETEX` 30 días, modos `auto`/`local`/`senescyt`. |
| `main.py` | App FastAPI con autenticación vía `commons.seguridad`. |
| `schemas.py` | Modelos Pydantic de entrada/salida. |
| `mock/` | Servidor mock de alta fidelidad que imita el portal (solo desarrollo). |

## Notas operativas

- **Servidor mock incluido:** recrea el portal real (imágenes de captcha + HTML
  JSF/PrimeFaces) para ejercitar todo el pipeline —captcha → OCR → consulta →
  parseo → caché— sin depender del portal real. Para producción solo se cambia
  `SENESCYT_BASE_URL`.
- **Latencia:** una consulta EN VIVO al portal real tarda 30–50 s (más con
  reintentos de captcha); las cacheadas responden al instante. El servicio usa
  `timeout=180` por esto.
- **Robustez:** distingue un fallo temporal del portal SENESCYT (para no reintentar
  en vano) de un error propio, y precarga el modelo de OCR al arrancar cada worker.
- **Redis recomendado:** sin él el servicio sigue operativo pero consulta el portal
  en cada petición.

## Códigos de estado HTTP

| Código | Significado |
|--------|-------------|
| `200` | OK (revisar `status` para saber si hubo títulos). |
| `400` | Petición inválida (p. ej. sin cédula ni apellidos). |
| `401` | Falta la cabecera `X-API-Key` o la clave no es válida. |
| `403` | Clave válida pero sin permiso (operaciones admin). |
| `502` | El portal de SENESCYT no respondió o no se pudo resolver el captcha. |
