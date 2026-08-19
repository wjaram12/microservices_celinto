# Bug: 502 en el alta cuando la cuenta SÍ se creó (404 de propagación de Google)

**Fecha:** 2026-07-13 · **Reportado por:** SGA-Posgrados (módulo `/correos_google`) · **Severidad:** alta
**Afecta a:** `POST /personas/procesar` y `POST /personas/` (todo consumidor que cree cuentas nuevas)

---

## Síntoma

Al crear cuentas por lote desde el SGA (primera creación, veredicto previo `disponible`), el
microservicio responde:

```
HTTP 502 — {"detail": "El usuario '102935914630665599937' no existe en Google."}
```

Pero **la cuenta sí quedó creada en Google Workspace**. El número del mensaje es el `google_id`
de la cuenta recién creada. En un lote real de posgrados fallaron así **41 de 41 creaciones
nuevas** (4 hilos concurrentes); al reauditar después, todas las personas aparecían como
`existe_sin_cedula` — cuenta creada, cédula sin escribir.

## Causa raíz

Google Admin SDK tiene **lecturas obsoletas justo después de escribir** (read-after-write no
consistente): un usuario recién insertado puede devolver `404` en `users.get` durante algunos
segundos. Este proyecto ya lo tiene documentado en el propio código:

> `router.py:696` (docstring de `/personas/{id}/confirmar`): *"Google devuelve lecturas
> obsoletas justo después de escribir… Un sistema que trate el 404 inmediato como error
> concluirá que el alta falló cuando en realidad funcionó."*

La secuencia del alta pisa exactamente esa trampa:

| Paso | Código | Qué pasa |
|---|---|---|
| 1 | `router.py:537` (`procesar`) / `router.py:645` (alta) — `usuarios.crear(...)` | La cuenta se crea. Google devuelve el `id`. |
| 2 | `router.py:544` / `router.py:654` — `establecer_external_id(nueva["id"], cedula, "identificacion")` | Hace read-modify-write: **relee** al usuario (`cliente.py:245 → obtener_con_reintentos`). |
| 3 | `cliente.py:162-163` | `obtener_con_reintentos` trata el `404` como definitivo (`return None`). Correcto en general — fatal para una cuenta que nació hace milisegundos. |
| 4 | `cliente.py:247` | `usuario is None` → `raise ErrorDeGoogle("El usuario 'X' no existe en Google.")` |
| 5 | `router.py:555` — `_traducir(e)` | El endpoint responde **502**. |

Nótese la asimetría: el paso de **grupos** sí tolera este 404 (`router.py:656`: *"Los grupos
toleran el 404 de propagación (el usuario acaba de nacer)"*), pero el paso del externalId —
que va justo antes/después — no.

## Consecuencias del abort

1. `vinculos.registrar(...)` (`router.py:550` / `router.py:667`) **nunca se ejecuta** → la
   cuenta creada no queda en el índice de vínculos.
2. La respuesta `201 creada` con `password_inicial` **nunca sale** → el consumidor no recibe
   la contraseña. (Con la política por defecto `GOOGLE_PASSWORD_INICIAL=cedula` no se pierde
   nada irrecuperable; con `aleatoria` la contraseña se perdería para siempre.)
3. El consumidor clasifica el 502 como error reintentable. Al reintentar, `procesar` encuentra
   la cuenta, entra por la rama vinculable (`router.py:490`), escribe la cédula (ya propagada)
   y responde `migrado=true, accion=vinculada` — **pero con `password_inicial=null`**, así que
   el consumidor jamás sabrá la contraseña por la API.
4. Mientras no se corrija: **toda primera pasada de un lote de creaciones nuevas fallará en
   masa** y solo la segunda pasada "sana" — con el costo de (3).

## Fix propuesto (dos capas)

> **Actualización — solución adoptada.** Finalmente se implementó un **fix de raíz** que
> hace innecesarias estas dos capas: la cédula se escribe **dentro del `users.insert`**
> (campo `externalIds`), no en una segunda llamada. Una cuenta recién creada no tiene
> externalIds que preservar, así que el read-modify-write sobra en el alta; se elimina la
> ventana de propagación y el 502 sin añadir ningún campo (`cedula_pendiente` no se
> introdujo). `establecer_external_id` se conserva intacto para cuentas ya existentes
> (rama vinculable y `vincular`). Ver `router.py` (ramas de creación de `procesar` y
> `crear_persona`) y `cliente.py:entrada_external_id`. El análisis de causa raíz de abajo
> sigue siendo válido como referencia; la propuesta de dos capas queda **superada**.

### Capa 1 — tolerar el 404 de propagación cuando la cuenta acaba de nacer

`cliente.py` — `establecer_external_id` acepta un modo `recien_creada` que reintenta el 404
con backoff (igual criterio que ya se aplica a 429/5xx), porque en ese contexto el `google_id`
es garantizado-válido (lo acaba de devolver `users.insert`):

```python
def establecer_external_id(self, clave_usuario, valor, tipo, recien_creada=False):
    usuario = self.obtener_con_reintentos(clave_usuario)
    if usuario is None and recien_creada:
        # 404 de propagación: users.insert acaba de devolver este id, la cuenta
        # existe; Google aún sirve lecturas obsoletas (ver /confirmar).
        for espera in (2, 4, 8, 16):          # ~30 s en total
            time.sleep(espera)
            usuario = self.obtener_con_reintentos(clave_usuario)
            if usuario is not None:
                break
    if usuario is None:
        raise ErrorDeGoogle(f"El usuario '{clave_usuario}' no existe en Google.")
    ...  # resto igual (read-modify-write preservando los demás externalIds)
```

Llamadas a cambiar (pasar `recien_creada=True` **solo** tras `crear`):

- `router.py:544` — `procesar`, rama "no tiene cuenta".
- `router.py:654` — `POST /personas/` (alta directa).
- `router.py:498` (rama vinculable) y `router.py:766` **no** lo necesitan: la cuenta es
  antigua, un 404 ahí sí significa que no existe.

### Capa 2 — red de seguridad: la creación nunca responde 502

Si aun con los reintentos la cédula no se pudo escribir, la cuenta **existe** y la respuesta
debe decirlo (el mismo tratamiento tolerante que ya reciben los grupos). En ambos endpoints
de alta:

```python
cedula_pendiente = False
try:
    directorio.usuarios.establecer_external_id(
        nueva["id"], cedula, "identificacion", recien_creada=True)
except ErrorDeGoogle:
    logger.exception("Cuenta '%s' creada pero la cédula no se pudo escribir aún.", correo)
    cedula_pendiente = True

# grupos + vinculos.registrar + respuesta 201 'creada' se ejecutan SIEMPRE
```

- `vinculos.registrar(...)` se ejecuta igual (el vínculo ya se conoce: cédula + google_id).
- La respuesta sigue siendo `201 status='creada', migrado=true` **con `password_inicial`**, y
  opcionalmente un campo nuevo `cedula_pendiente: bool` en el schema (default `false`) para
  que el consumidor pueda auditarlo.
- La cédula pendiente se sana sola: cualquier `procesar` posterior de la misma persona entra
  por la rama vinculable y la escribe (`router.py:490-499`), y el backfill también la cubre.

## Criterios de aceptación

1. Lote de N cuentas **nuevas** creado con 4-8 hilos → 0 respuestas 502 por
   "usuario no existe"; todas responden `201 creada` con `password_inicial`.
2. Con un mock/fault-injection que devuelva 404 en `users.get` durante los primeros ~10 s
   tras `users.insert`: la respuesta sigue siendo `201 creada` (capa 1 lo absorbe) y el
   externalId queda escrito.
3. Con 404 permanente forzado (>30 s): respuesta `201 creada` + `cedula_pendiente=true`,
   vínculo registrado, y un `procesar` posterior deja `externalId` escrito y
   `accion='vinculada'`.
4. La rama vinculable (`router.py:490`) conserva su comportamiento actual: 404 real → error
   (no hay reintento de propagación ahí).
5. `GET /personas/{id}/confirmar` sigue reportando `propagando`/`listo` sin cambios.

## Referencias de código (estado actual)

- `router.py:537-556` — flujo de creación de `procesar` (crear → externalId → grupos → vínculo).
- `router.py:645-670` — flujo de creación del alta directa (grupos ya tolerantes en `:656-665`).
- `cliente.py:150-168` — `obtener_con_reintentos` (404 → `None`, por diseño).
- `cliente.py:235-261` — `establecer_external_id` (read-modify-write; el punto exacto del fallo
  está en `:245-247`).
- `router.py:690-703` — `/confirmar`, donde el fenómeno de propagación ya está documentado.
- `config.py:59` — `GOOGLE_PASSWORD_INICIAL: str = "cedula"` (por qué la contraseña no se
  pierde con la política por defecto).

## Evidencia del incidente (SGA-Posgrados, lote real)

- 41 detalles quedaron con `respuesta_api = {"detail": "El usuario '<google_id>' no existe en
  Google.", "status_code": 502}` tras un Crear masivo (4 hilos).
- Reauditadas después, las mismas personas devolvían `existe_sin_cedula` con la cuenta ya
  visible (ej.: cédula 0707033312 → `luis.oto@casagrande.edu.ec`), confirmando que la cuenta
  se creó y solo faltó el externalId.
- El reintento desde el SGA las llevó a `migrado=true, accion=vinculada` sin intervención.
