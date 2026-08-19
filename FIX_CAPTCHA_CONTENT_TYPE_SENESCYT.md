# FIX — Scraper de Consulta de Títulos (SENESCYT): captcha rechazado por falta de `Content-Type`

**Estado:** resuelto y verificado en vivo (julio 2026)
**Aplica a:** cualquier scraper HTTP del portal público de consulta de títulos de SENESCYT
**Portal afectado:** `https://titulos-edusuperior.minedec.gob.ec/consulta-titulos-web`

---

## 1. Síntoma

El scraper **conecta con el portal pero no devuelve resultados**. Según cómo esté escrito el
manejo de errores, se ve como alguna de estas variantes:

- Un error tipo *"el servicio de captcha no está disponible / HTTP 200"* (mensaje contradictorio:
  dice que no está disponible pero reporta un código de éxito).
- Una excepción en cada intento de resolver el captcha, sin llegar nunca a enviar la búsqueda.
- Respuesta vacía / "sin resultados" para cédulas que **sí** tienen títulos registrados.

Suele aparecer justo después de que el portal cambió de dominio y sumó un modal informativo,
lo que hace pensar que el modal es el culpable. **No lo es** (ver sección 6).

---

## 2. Causa raíz

El portal nuevo sirve la imagen del captcha así:

```
GET /consulta-titulos-web/Captcha.jpg
→ HTTP 200
→ cuerpo: JPEG válido (empieza con FF D8 FF)
→ SIN cabecera Content-Type          ← esto es lo que rompe
```

El portal viejo sí enviaba `Content-Type: image/jpeg`. Por eso casi todos los scrapers validan
el captcha con una condición del estilo:

```python
ct = resp.headers.get("Content-Type", "").lower()
if resp.status_code != 200 or "image" not in ct:
    raise Error("El servicio de captcha no está disponible ...")
```

Con el portal nuevo `ct` queda `""`, la condición se cumple **siempre**, y el scraper descarta
todos los captchas como "servicio caído". Como ese error suele re-lanzarse de inmediato (sin
consumir reintentos), **la búsqueda nunca llega a enviarse**. De ahí el "conecta pero no trae
resultados".

> Detalle de infraestructura: el portal está detrás de un balanceador de Oracle Cloud
> (cookies `X-Oracle-BMC-LBS-Route` y `JSESSIONID` con sufijo `.portal-titulos`). El header
> ausente viene de esa capa, así que **puede reaparecer o volver a desaparecer sin aviso**: la
> corrección debe tolerar ambos casos, no reemplazar una suposición rígida por otra.

---

## 3. La corrección

**Regla:** validar el captcha por los **bytes mágicos de la imagen**, y usar el `Content-Type`
solo como refuerzo (no como requisito).

Esto sigue detectando un captcha realmente caído: si el servicio falla, el portal devuelve HTML
o JSON de error, cuyos primeros bytes no coinciden con ninguna firma de imagen.

### 3.1 Python (`requests`)

```python
# El portal real sirve /Captcha.jpg con HTTP 200 pero SIN cabecera Content-Type
# (queda ""), así que validar solo por Content-Type rechaza captchas válidos.
# Validamos por los bytes mágicos de la imagen y usamos el Content-Type como
# refuerzo. Si el captcha estuviera caído, el portal devolvería un HTML/JSON de
# error y estos magic bytes no coincidirían.
ct = resp.headers.get("Content-Type", "").lower()
contenido = resp.content or b""
es_imagen = (
    "image" in ct
    or contenido[:3] == b"\xff\xd8\xff"           # JPEG
    or contenido[:8] == b"\x89PNG\r\n\x1a\n"      # PNG
    or contenido[:6] in (b"GIF87a", b"GIF89a")    # GIF
)
if resp.status_code != 200 or not es_imagen:
    raise SenescytScraperError(
        "El servicio de captcha de SENESCYT no está disponible en este momento "
        f"(HTTP {resp.status_code})."
    )

captcha_b64 = base64.b64encode(contenido).decode("ascii")
captcha_mime = ct if "image" in ct else "image/jpeg"   # fallback del MIME
```

### 3.2 Node / JavaScript (equivalente)

```js
const ct = (resp.headers.get("content-type") || "").toLowerCase();
const buf = Buffer.from(await resp.arrayBuffer());
const esImagen =
  ct.includes("image") ||
  (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) ||          // JPEG
  buf.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex")) || // PNG
  ["GIF87a", "GIF89a"].includes(buf.subarray(0, 6).toString("latin1")); // GIF

if (resp.status !== 200 || !esImagen) {
  throw new Error(`Captcha de SENESCYT no disponible (HTTP ${resp.status})`);
}
```

### 3.3 Firmas de imagen (referencia)

| Formato | Bytes iniciales (hex)      | Longitud |
|---------|----------------------------|----------|
| JPEG    | `FF D8 FF`                 | 3        |
| PNG     | `89 50 4E 47 0D 0A 1A 0A`  | 8        |
| GIF     | `47 49 46 38 37/39 61`     | 6        |

---

## 4. Dónde aplicar el cambio

Buscá en el código **todos** los puntos donde se descarga el captcha. Es habitual que haya más de
uno (uno en la carga inicial de sesión y otro al refrescar), y que solo uno tenga la validación:

```bash
# ripgrep
rg -n "Captcha.jpg|captcha" --glob '*.py' -l
rg -n 'Content-Type|content-type' <archivo_del_scraper>
```

En la implementación de referencia (`services/consulta_titulos/scraper.py`) los puntos son:

| Método               | Validaba Content-Type | Acción                                    |
|----------------------|-----------------------|-------------------------------------------|
| `iniciar_sesion()`   | No (solo `raise_for_status`) | Sin cambios; ya toleraba el header ausente |
| `refrescar_captcha()`| **Sí** → rompía        | **Aplicar el fix de la sección 3**         |

Si además el MIME se usa aguas abajo (por ejemplo para devolver el captcha en base64 a un
frontend), asegurate del fallback `"image/jpeg"` cuando el header viene vacío — un `data:` URI con
MIME vacío no renderiza en el navegador.

---

## 5. Actualizar el mock / entorno de pruebas

Este bug **no se detectó antes porque el mock era "más correcto" que el portal real**: enviaba
`Content-Type: image/png` cuando el portal real no envía nada. Para que el escenario quede
cubierto, el mock debe reproducir la peculiaridad.

FastAPI / Starlette:

```python
# El portal REAL sirve /Captcha.jpg con HTTP 200 pero SIN cabecera Content-Type.
# Reproducimos esa peculiaridad para que el mock ejercite la validación por
# bytes mágicos del scraper.
resp = Response(content=png)              # sin media_type
if "content-type" in resp.headers:
    del resp.headers["content-type"]      # MutableHeaders NO tiene .pop()
resp.set_cookie("JSESSIONID", sid, httponly=True)
return resp
```

> ⚠️ En Starlette, `resp.headers` es un `MutableHeaders` que **no** implementa `.pop()`.
> Usar `resp.headers.pop("content-type", None)` produce un `AttributeError` y un HTTP 500.
> Hay que usar `del`.

**Principio general:** cuando un mock diverge del servicio real "hacia lo correcto", esconde bugs.
El mock debe imitar las rarezas del real, no arreglarlas.

---

## 6. Cosas que NO son el problema (ya descartadas con evidencia en vivo)

| Sospecha                                   | Veredicto                                                                                             |
|--------------------------------------------|-------------------------------------------------------------------------------------------------------|
| El modal bloquea el scraper                | **Falso.** `modalInfoInicio` ("Cumplimiento de Sentencia") se abre con `PF('modalInfoInicio').show()` en `document-ready`: es JavaScript de navegador. Un scraper HTTP puro nunca lo ejecuta. **No hay nada que cerrar.** |
| El dominio en la configuración está mal    | Verificar, pero el correcto es `https://titulos-edusuperior.minedec.gob.ec` (la página oficial `senescyt.gob.ec/web/guest/consultas` hace 301 exactamente ahí). |
| Cambiaron los nombres de campos del form   | **Falso.** Siguen siendo `formPrincipal:identificacion`, `formPrincipal:apellidos`, `formPrincipal:captchaSellerInput`, `formPrincipal:boton-buscar` y `javax.faces.ViewState`. |
| El OCR no lee el captcha nuevo             | **Falso.** `ddddocr` crudo acertó 10/10. ⚠️ **No preprocesar la imagen** (escalar/umbralizar empeora la precisión). El captcha nuevo son 4 caracteres alfanuméricos en minúscula. |
| Hay que mandar `;jsessionid=` en la URL     | **No hace falta.** Las cookies de la misma `Session` bastan; el `JSESSIONID` no cambia entre la página y el captcha. |
| El SSL del portal está roto                | **Falso en el dominio nuevo.** Funciona con verificación de certificado activada (`VERIFY_SSL=true`). Si tu código lo tenía desactivado por el portal viejo, ya podés reactivarlo. |
| Cambió el markup de resultados             | **Falso.** El id del datatable ahora es `formPrincipal:j_idtNN:0:tablaAplicaciones` (con `j_idt` dinámico). Un selector por sufijo (`id` que **termina** en `tablaAplicaciones`) lo sigue matcheando. Si tu parser usa el id completo y fijo, ahí sí tenés que cambiarlo a coincidencia por sufijo. |

---

## 7. Verificación

1. **Contra el portal real** — corré el flujo completo con una cédula que tenga títulos
   registrados y confirmá que devuelve persona + títulos. Señales de éxito:
   - se resuelve en pocos intentos de captcha (normalmente 1);
   - aparece el botón `formPrincipal:btnInfoConsulta` (PDF disponible).

2. **Contra el mock actualizado** (sección 5) — debe pasar igual. Si el mock **no** reproduce el
   header ausente, esta prueba no vale como regresión.

3. **Prueba negativa de que el fix no debilita la detección de fallos:** apuntá la ruta del captcha
   a un endpoint que devuelva HTML o JSON y confirmá que sigue lanzando el error de
   "captcha no disponible" (los bytes mágicos no coinciden).

---

## 8. Checklist de aplicación en otro sistema

- [ ] Ubicar todas las descargas de la imagen del captcha en el código.
- [ ] Reemplazar la validación `"image" in Content-Type` por validación de bytes mágicos + `Content-Type` como refuerzo.
- [ ] Poner fallback de MIME a `image/jpeg` si el header viene vacío.
- [ ] Confirmar que la URL base apunta a `titulos-edusuperior.minedec.gob.ec`.
- [ ] Reactivar la verificación de SSL si estaba desactivada.
- [ ] Confirmar que el parser de resultados matchea el id del datatable **por sufijo**, no exacto.
- [ ] Actualizar el mock/fixture para servir el captcha **sin** `Content-Type`.
- [ ] Verificar de punta a punta contra el portal real y el mock.
- [ ] No tocar nada relacionado con el modal.

---

## 9. Lecciones transferibles

- **Un header ausente no es un header vacío... pero tu código los trata igual.** Validar tipo de
  contenido por metadatos que un intermediario (CDN, balanceador, proxy) puede alterar es frágil.
  Validá el contenido real cuando exista una firma barata de comprobar.
- **Mensajes de error que se contradicen son una pista fuerte.** "Servicio no disponible
  (HTTP 200)" señala que la condición del `if` está mal, no que el servicio esté caído.
- **El cambio más visible rara vez es la causa.** El modal era el cambio evidente; el culpable era
  un header que desapareció en silencio.
