# Guía de la función OCR

Cómo se usa el OCR del clasificador documental y —sobre todo— **qué devuelve
exactamente**, para que un consumidor (persona o agente) pueda ajustarlo a su
necesidad sin leer el código.

El OCR vive en **un solo endpoint**: `POST /api/v1/ocr/`. Todo lo demás de esta
guía explica esa llamada: su entrada, su salida, sus límites y dónde se cambia su
comportamiento.

---

## Qué es y qué no es

`/ocr/` **transcribe el documento a texto** y nada más. No clasifica, no extrae
campos, no valida identidades y no compara contra ningún dato del sistema.

| Si necesitas… | Usa |
|---|---|
| El texto completo del documento | `POST /api/v1/ocr/` |
| Saber qué clase de documento es + campos estructurados | `POST /api/v1/validaciones/validar-identidad/` · `.../validar-registro-senescyt/` · `.../validar-pago/` |

Las tres rutas de validación **no usan OCR**: sacan sus datos de la extracción
estructurada de Extend, no del texto (`app/services/documentos.py:417-424`). El
campo `ocr` de `validar-identidad` quedó deprecado y **siempre viene `null`**. Si
un consumidor quiere el texto además de los campos, tiene que llamar a `/ocr/`
aparte, con su propia subida del archivo.

---

## La llamada

```http
POST /api/v1/ocr/
X-API-Key: wsk_...
Content-Type: multipart/form-data
```

| Campo (multipart) | Tipo | Obligatorio | Qué hace |
|---|---|---|---|
| `file` | archivo | **sí** | El documento. PDF, JPEG o PNG. |
| `texto_a_buscar` | texto | no | Término a buscar dentro del documento. **Solo cambia `message`** (ver más abajo). |

Autenticación: cabecera `X-API-Key` con **cualquier clave válida** — basta scope
`consumo`, no hace falta `admin` (`app/main.py:92-98`). Sin clave o con clave
revocada → `401`.

```bash
curl -X POST "$API/api/v1/ocr/" \
  -H "X-API-Key: $CLAVE" \
  -F "file=@registro.pdf;type=application/pdf"
```

```python
r = requests.post(f"{API}/api/v1/ocr/",
                  headers={"X-API-Key": CLAVE},
                  files={"file": ("registro.pdf", contenido, "application/pdf")},
                  timeout=310)
texto = r.json()["content"]
```

> **El `Content-Type` de la parte del archivo importa.** El servicio valida el
> MIME que llega en el multipart, no la extensión ni los bytes
> (`app/services/documentos.py:89-93`). Un PDF enviado como
> `application/octet-stream` se rechaza con `400`. Los clientes HTTP que adivinan
> el MIME por extensión suelen acertar; los que mandan `octet-stream` por defecto,
> no. Decláralo explícitamente.

---

## La respuesta

**Exactamente tres campos.** No hay más (`app/schemas/ocr.py`):

```json
{
  "result": true,
  "message": "Texto extraído del documento (1832 caracteres).",
  "content": "# REPÚBLICA DEL ECUADOR\n\nSENESCYT\n\n| Registro | 1234-56-789 |\n..."
}
```

| Campo | Tipo | Qué significa |
|---|---|---|
| `result` | bool | `true` si se extrajo **algún** texto; `false` si el texto salió vacío. |
| `message` | string | Frase para humanos. **No la parsees**: su redacción cambia sin aviso. |
| `content` | string | El texto completo del documento. `""` cuando `result` es `false`. |

`result` es literalmente `bool(content)` (`app/views/documentos.py:80`): no es un
índice de calidad ni de confianza. Un documento borroso del que solo se leyó una
palabra devuelve `result: true`. **Quien necesite saber si la lectura sirve tiene
que juzgarlo sobre `content`** — longitud, presencia de las palabras clave que
espera, un patrón que valide.

### `texto_a_buscar` no devuelve la búsqueda

Este es el detalle que más sorprende. El servicio **sí** busca el término —
ignorando mayúsculas y tildes, y calculando posición y contexto de cada
aparición (`app/services/documentos.py:128-154`)— pero ese resultado **no viaja
en el JSON**: solo decide cuál de estas frases va en `message`.

| Situación | `message` |
|---|---|
| No se envió `texto_a_buscar` | `"Texto extraído del documento (N caracteres)."` |
| Se envió y aparece | `"Texto extraído; el término buscado aparece N vez/veces en el documento."` |
| Se envió y no aparece | `"Texto extraído; el término buscado no aparece en el documento."` |
| No se extrajo texto | `"No se pudo extraer texto del documento."` |

Consecuencia práctica: **si necesitas el dato de la búsqueda —cuántas veces,
dónde, con qué contexto— no uses `texto_a_buscar`.** Pide el `content` y busca en
él, en tu propio código, donde además controlas la normalización. Sacar el número
del `message` con una expresión regular es frágil y se romperá al primer cambio de
redacción.

La normalización que aplica el servicio, por si quieres replicarla: minúsculas,
sin tildes, un carácter de salida por carácter de entrada (así las posiciones
siguen alineadas con el texto original) y contexto de 40 caracteres a cada lado.

---

## Cómo leer `content`

Es **markdown**, tal y como lo devuelve el parser de Extend, con la estructura del
documento conservada: encabezados con `#`, tablas en formato markdown, listas.

Se construye uniendo con un salto de línea el contenido de cada *chunk* que
devuelve Extend (`app/services/extend.py:222-223`). Con la configuración por
defecto —un chunk por página— eso significa que **las páginas quedan pegadas con
un solo `\n`, sin ninguna marca de separación**. No hay forma de saber, leyendo el
texto, dónde acabó la página 1 y empezó la 2. Si tu caso lo necesita, hay que
cambiar la configuración del parser (siguiente sección) y tocar el cliente para
que emita los chunks por separado.

Lo que **no** hay en la respuesta, aunque Extend lo produzca:

- **Confianza del OCR.** Ni global ni por palabra. (Las rutas de validación sí
  devuelven `confianzas` por campo, pero eso viene de la extracción, no del OCR.)
- **Coordenadas.** Extend devuelve *blocks* con `boundingBox` y `polygon`; el
  cliente los descarta y se queda solo con el texto.
- **Número de páginas, tipo de bloque, tablas como estructura.** Todo llega
  aplanado a markdown.
- **Consumo de créditos** de la llamada a Extend.

Todo eso está disponible en la respuesta cruda de Extend: recuperarlo es ampliar
`ClienteExtend.parsear` y el esquema `RespuestaOCR`, no cambiar de proveedor.

---

## Ajustar el comportamiento

El OCR **no se configura por `.env` ni por parámetros de la petición**: se
configura con una fila en la tabla `procesadores`, editable en caliente desde
`/admin/procesadores` o por API. La fila del OCR es la única de la semilla que
nace activa (`app/services/procesadores.py:556`):

| ruta | operacion | clase | modo | esquema | activo |
|---|---|---|---|---|---|
| `ocr` | `parse` | *(vacía)* | `inline` | `{"target": "markdown"}` | `true` |

`esquema.target` es lo único que se envía a Extend como configuración
(`app/services/procesadores.py:899-905`). Valores admitidos por Extend:

| `target` | Resultado |
|---|---|
| `markdown` *(por defecto)* | Texto con la estructura en markdown. Lo que quieres casi siempre. |
| `spatial` | Texto que preserva la disposición espacial de la página. Útil en formularios y tablas muy posicionales. |

Cambiarlo (hace falta clave **admin**):

```http
PUT /api/v1/procesadores/{id}
X-API-Key: <clave admin>

{"esquema": {"target": "spatial"}}
```

El cambio invalida la caché y aplica a la siguiente petición: **sin redeploy y sin
reinicio**. Si la fila se desactiva o se borra, el OCR no se rompe — cae al
`markdown` por defecto que trae el código.

> A diferencia de las rutas de validación, el OCR **no usa un procesador publicado
> en Extend Studio**: no existe un "parser" que publicar allí, su configuración es
> legítimamente local. Por eso esta fila sí vive en la base, mientras que las de
> clasificar/extraer están neutralizadas porque Extend es su fuente de verdad.

---

## Errores

| Código | Cuándo | Qué hacer |
|---|---|---|
| `400` | Formato no admitido (MIME distinto de PDF/JPEG/PNG), archivo vacío, archivo > 15 MB, o no se pudo leer la subida | Corregir la petición. **No reintentar.** |
| `401` | Falta `X-API-Key`, o la clave es inválida o está revocada | Revisar la clave. |
| `502` | Extend falló, o devolvió una corrida sin `status: "PROCESSED"` | **Reintentar** con espera creciente. |
| `500` | Fallo no previsto del servicio | Avisar a TI. No reintentar en bucle. |

Un `502` ya viene **después** de que el cliente reintentara por su cuenta: ante
red caída, `429` o `5xx` de Extend hace hasta 3 intentos con backoff exponencial
con jitter (0,5 s a 8 s), respetando el `Retry-After` si Extend lo manda
(`app/services/extend.py:86-88, 143-151`). Que llegue un `502` significa que
Extend lleva un rato sin responder o que rechazó la petición de forma definitiva:
espera decenas de segundos antes de volver a intentar, no milisegundos.

Los `4xx` de Extend (clave mala, archivo corrupto) **no** se reintentan y salen
también como `502`, así que si un documento concreto falla siempre mientras los
demás pasan, no es un problema de disponibilidad: revisa el archivo. El cuerpo
crudo de Extend nunca se propaga al consumidor —queda en el log del servicio—,
así que el detalle real del fallo hay que buscarlo ahí.

---

## Límites y rendimiento

| Límite | Valor | Dónde |
|---|---|---|
| Tamaño máximo | **15 MB** | `app/services/documentos.py:55` |
| Formatos | `application/pdf`, `image/jpeg`, `image/png` | `app/services/documentos.py:54` |
| Timeout hacia Extend | 300 s de lectura, 10 s de conexión | `app/services/extend.py:103` |
| Límite de peticiones por clave | **ninguno** | — |

Sobre esto último: el clasificador **no** aplica límite de tasa (el limitador por
API key existe, pero solo en el servicio de Google Workspace). Quien manda es la
cuota de tu cuenta de Extend, y se agota con `429` que el cliente reintenta en
silencio. En lotes grandes, modera tú el paralelismo.

Cada llamada a `/ocr/` sube el archivo a Extend **otra vez**: el `file_id` se
reutiliza dentro de una misma petición, nunca entre peticiones. Dos llamadas por
el mismo documento son dos subidas y dos parses. **No hay caché de resultados de
OCR**: si vas a necesitar el texto más de una vez, guárdalo tú.

El coste es de segundos por documento, dominado por Extend. Un archivo grande y
multipágina puede acercarse al timeout de 300 s: pon el timeout de tu cliente HTTP
**por encima** de eso (310 s o más) o verás timeouts tuyos sobre peticiones que
habrían terminado bien.

---

## Bajo el capó

Dos llamadas a Extend por cada `/ocr/` (`app/services/extend.py:6-13`):

```
POST /files/upload   (multipart)   -> file_id
POST /parse          (file_id + {"config": {"target": "markdown"}})
```

De la respuesta de `/parse`, el cliente exige `status == "PROCESSED"` y se queda
con el contenido de los chunks; cualquier otro estado se convierte en `502` con el
`failureReason` registrado en el log (`app/services/extend.py:212-223`).

> **Si algún día `content` llega vacío pero Extend responde 200**, el sospechoso
> es la forma de la respuesta: el cliente lee los chunks bajo `output.chunks`, y
> la documentación pública de Extend describe la corrida de parse con los chunks
> en la raíz. Bastaría con que una versión de la API cambiara ese anidamiento para
> que el texto saliera vacío **sin ningún error**. Es el primer sitio donde mirar,
> y la razón por la que conviene fijar `x-extend-api-version` (hoy `2026-02-09`,
> en `app/services/extend.py:34`) y no dejarla flotar.

---

## Qué NO hacer

**No parsees `message`.** Es prosa para humanos y cambia. Todo lo accionable está
en `result` y `content`.

**No uses `texto_a_buscar` esperando recibir la búsqueda.** No viene en la
respuesta. Busca sobre `content`.

**No trates `result: true` como "el OCR salió bien".** Solo dice que el texto no
está vacío. La calidad la juzgas tú sobre `content`.

**No reintentes un `400`.** Es tu petición: el MIME, el tamaño o el archivo.

**No llames a `/ocr/` para obtener campos estructurados.** Para eso están las
rutas de validación, que usan extractores con esquema y devuelven confianza por
campo. Sacar la cédula del texto con una regex es peor: el OCR confunde dígitos y
no te dice cuánto se fía.

**No asumas que el texto conserva la paginación.** Las páginas van pegadas sin
marca.
