# Cambios de contrato — rutas de validación (junio 2026)

Nota para los consumidores del servicio (celinto-posgrados, ucg-posgrados,
ucg-on pregrado). Afecta a:

- `POST /api/v1/validaciones/validar-identidad/`
- `POST /api/v1/validaciones/validar-registro-senescyt/`
- `POST /api/v1/clasificar/` (**ELIMINADA**)

## 🗑️ Ruta eliminada: `POST /api/v1/clasificar/`

La ruta de clasificación "suelta" (sin validaciones) se eliminó: no se va a
utilizar. Cualquier llamada devolverá **404**. La clasificación sigue
existiendo como paso interno de las dos rutas de validación, que devuelven la
clase en `document_class`.

Operativo (servidor): la fila `clasificar` ya no se siembra en las tablas
`rutas` y `procesadores`; en una base existente la fila vieja queda huérfana y
puede borrarse desde `/admin/procesadores` y `/admin/rutas`.

## ⚠️ Acción requerida ANTES de actualizar

**`match_document` ahora puede ser `null` en casos que antes devolvían `false`.**

| Caso | Antes | Ahora |
|------|:-----:|:-----:|
| No se envió dato para comparar | `null` | `null` (igual) |
| El documento no trae el dato (extractor no lo leyó) | `false` | **`null`** |
| El documento no es de la clase esperada | `false` | **`null`** |
| Ambos lados presentes y distintos | `false` | `false` (igual) |
| Ambos lados presentes e iguales | `true` | `true` (igual) |

Semántica: `true` = coincide, `false` = **se comparó y NO coincide**,
`null` = **no se pudo comparar** (no se envió el dato o no se pudo leer del
documento).

> Si su sistema rechaza solo cuando `match_document == false`, un `null`
> pasaría como aprobado. Revisen esa lógica: para aprobar identidad exijan
> `match_document == true` (no "distinto de false").

## `result` indica SOLO la clasificación

`result: true` significa que el documento **es de la clase esperada** (cédula/
pasaporte, o registro SENESCYT) con confianza suficiente. **No** implica que la
extracción funcionara ni que la identidad coincida.

En `validar-registro-senescyt` esto es un cambio: antes `result` también exigía
que la extracción trajera el número de registro. Para decidir si un registro es
aprovechable, combinar:

```
result == true  AND  status == "extraido"  AND  match_document == true
```

## Campo nuevo: `status` (en ambas rutas)

Estado estructurado, legible por máquina (no parsear `message`, que es solo
para humanos):

| `status` | Significado |
|----------|-------------|
| `"no_reconocido"` | La clase no es la esperada por la ruta |
| `"extraccion_fallida"` | Clase correcta, pero no se pudo extraer información |
| `"extraido"` | Clase correcta e información extraída |

## Parámetros nuevos en `validar-registro-senescyt` (opcionales)

- `numero_identificacion` — se compara con el extraído ignorando espacios y
  caracteres especiales (y recuperando un cero inicial perdido en cédulas).
- `nombres` — se comparan sin distinguir mayúsculas, tildes ni el ORDEN de los
  nombres (p. ej. "Carlos Andrés Molina Jaramillo" ≡ "MOLINA JARAMILLO CARLOS
  ANDRES"). Se exige el nombre completo: omitir un segundo nombre da `false`.
- `match_document` es `true` si **al menos uno** de los enviados coincide (OR).

## Validación de entrada unificada (400)

Ambas rutas validan la identificación enviada ANTES de procesar el documento
(error 400 con el motivo):

- Numérica → debe tener 10 dígitos y pasar el dígito verificador de cédula
  ecuatoriana ("¿se perdió un cero a la izquierda?").
- No numérica (pasaporte) → mínimo 5 caracteres alfanuméricos.

Esto es nuevo en `validar-registro-senescyt` (antes aceptaba cualquier valor).

## Sin cambios

- `validar-identidad`: `result`, `document_class`, `confidence`, `datos`,
  `ocr` (deprecado, siempre `null`) y el prefijo `VS-` del pasaporte en `datos`.
- Autenticación (X-API-Key) y códigos de error (400/502/500).

---

# Referencia de respuestas

## `POST /api/v1/validaciones/validar-identidad/`

Entrada: `file` (multipart, obligatorio) + `cedula_sistema` (form, opcional).

Campos de la respuesta 200: `result`, `message`, `status`, `match_document`,
`document_class`, `confidence`, `datos`, `ocr` (siempre `null`).

| # | Caso | `result` | `status` | `match_document` | `message` |
|---|------|:---:|:---:|:---:|---------|
| 1 | No es cédula ni pasaporte (clase ajena o confianza < umbral) | `false` | `no_reconocido` | `null` | "El documento no fue reconocido como cédula ni pasaporte." |
| 2 | Es identidad, pero la extracción no trajo nada | `true` | `extraccion_fallida` | `null` | "El documento es {CLASE}, pero no se pudo extraer la información; falló el extractor o el documento no tiene suficiente claridad." |
| 3 | Es identidad, sin `cedula_sistema` | `true` | `extraido` | `null` | "Documento reconocido como {CLASE}; datos extraídos." |
| 4 | Con `cedula_sistema`, pero el número del documento no se pudo leer o falla el verificador | `true` | `extraido` | `null` | "No se pudo extraer un número de identificación del documento." |
| 5 | Con `cedula_sistema` y coincide | `true` | `extraido` | `true` | "La identificación del sistema coincide con la del documento." |
| 6 | Con `cedula_sistema` y NO coincide | `true` | `extraido` | `false` | "La identificación del sistema NO coincide con la del documento." |

Nota: si la clase es PASAPORTE, `datos` devuelve el número con prefijo `VS-`
(la comparación interna usa el número crudo).

## `POST /api/v1/validaciones/validar-registro-senescyt/`

Entrada: `file` (multipart, obligatorio) + `numero_identificacion` y `nombres`
(form, opcionales).

Campos de la respuesta 200: `result`, `message`, `status`, `match_document`,
`document_class`, `confidence`, `datos`.

| # | Caso | `result` | `status` | `match_document` | `message` |
|---|------|:---:|:---:|:---:|---------|
| 1 | No es registro SENESCYT (clase ajena o confianza < umbral) | `false` | `no_reconocido` | `null` | "El documento no fue reconocido como un registro de título de la SENESCYT." |
| 2 | Es SENESCYT, pero la extracción no trajo nada | `true` | `extraccion_fallida` | `null` | "El documento es un registro SENESCYT, pero no se pudo extraer la información; falló el extractor o el documento no tiene suficiente claridad." |
| 3 | Es SENESCYT, sin parámetros de identidad | `true` | `extraido` | `null` | "Registro SENESCYT reconocido; información extraída." |
| 4 | Se enviaron parámetros, pero el documento no trae los campos para comparar | `true` | `extraido` | `null` | "Registro SENESCYT reconocido, pero no se pudo leer del documento la información para verificar la identidad." |
| 5 | Al menos uno de los enviados coincide (OR) | `true` | `extraido` | `true` | "Registro SENESCYT reconocido; la identidad coincide con la del documento." |
| 6 | Ninguno de los enviados coincide | `true` | `extraido` | `false` | "Es un registro SENESCYT, pero {el número de identificación / los nombres / ambos} no coincide(n) con los datos proporcionados." |

## Errores (ambas rutas)

| HTTP | Causa | Detalle |
|:---:|------|---------|
| 400 | Formato no admitido | "Formato '{mime}' no admitido. Debe ser PDF, JPEG o PNG." |
| 400 | Archivo vacío o > 10 MB | "El archivo está vacío." / "El archivo excede el tamaño máximo permitido (10 MB)." |
| 400 | Cédula enviada ≠ 10 dígitos | "La cédula del sistema debe tener 10 dígitos… ¿Se perdió un cero a la izquierda?" |
| 400 | Cédula enviada con dígito verificador inválido | "…no es un número de cédula ecuatoriana válido (falla el dígito verificador)." |
| 400 | Identificación no numérica con < 5 caracteres | "…es demasiado corta para ser una cédula o un número de pasaporte." |
| 401/403 | API key ausente, inválida o sin scope de consumo | — |
| 422 | Falta el campo `file` | Error de validación de FastAPI |
| 502 | Error del proveedor (Extend) | Mensaje del proveedor |
| 500 | Error interno inesperado | "Error interno al procesar el documento." |

**Regla de oro:** aprobar identidad solo con
`result == true && status == "extraido" && match_document == true`.
Nunca con "`match_document` distinto de `false`": `null` significa
"no se pudo comparar", no "aprobado".
