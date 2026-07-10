# Guía de integración para los sistemas consumidores

Cómo crear y verificar cuentas de correo institucional a través del servicio
`google_services`. Dirigida a **UCG One**, **Posgrados** y **SGA**.

---

## El principio: la llave es la cédula, nunca el correo

Todo en esta API se identifica por **cédula**. No por correo, no por nombre.

Esto no es una preferencia de diseño, es lo que dicen los datos del dominio:

- **333 correos institucionales** están asignados a **dos personas distintas** en las
  tablas de origen. `amanda.adrian@casagrande.edu.ec` figura como correo de Amanda
  Adrián *y* de Horacio García.
- **313 personas** tienen su dirección canónica `nombre.apellido@` ocupada por otra
  persona real. `jose.caballero@` existe, pero es de *José Alejandro*, no de *José
  Nicolás*.
- **231 nombres completos** están repetidos entre cuentas activas del dominio.
- **204 correos** guardados en los sistemas de origen **no existen en Google**.

Si tu sistema pregunta por correo o por nombre, tarde o temprano recibirá la cuenta
de otra persona. Si pregunta por cédula, no.

La cédula vive **en la propia cuenta de Google**, como identificador externo
(`externalIds`, `customType: identificacion`). Ya está escrita en **23 620 cuentas**.
Viaja con la cuenta: sobrevive a un cambio de correo y se ve en la consola de
administración.

---

## Dónde está el servicio

`google_services` **no corre en un servidor propio**: va montado dentro del
clasificador documental de la universidad, el mismo servidor que atiende la consulta
de títulos SENESCYT. Un solo host y un solo puerto para todos los servicios.

```
http://<host-del-clasificador>/api/v1/google-services/...
```

Todas las rutas de esta guía ya llevan el prefijo `/api/v1`: cópialas tal cual sobre
la URL base. El host y tu API key te los entrega TI; son **los mismos** que usarías
para cualquier otro servicio del clasificador.

Para saber si está arriba, `GET /` del servidor responde `{"status": "online", ...}`
sin autenticación.

> Existe además un modo *standalone* (el servicio solo, en su propio puerto y **sin**
> el prefijo `/api/v1`) que TI usa para pruebas y despliegues separados. Como sistema
> consumidor, integra siempre contra la URL unificada de arriba.

---

## Autenticación

Cabecera `X-API-Key` en toda petición. Cada sistema tiene su propia clave, y es la
**misma** que usa para el resto de servicios del clasificador (las claves se validan
contra una tabla `api_keys` compartida): no necesitas una clave aparte para Google.

**Todo lo que describe esta guía funciona con una clave de scope `consumo`** —
consultar, procesar, crear cuentas, vincular, gestionar miembros de grupos. El scope
`admin` solo se exige en operaciones que no son de los sistemas cliente (el CRUD
crudo de usuarios del Admin SDK y los diagnósticos internos), que no aparecen aquí.

Sin clave o con clave inválida → `401`.

Tu clave identifica a tu sistema: queda registrada como `consumidor` en cada cuenta
que crees. Es la trazabilidad de quién dio de alta a quién.

---

## El camino corto: una sola llamada

Si lo que quieres es «resuelve esta persona y dime si quedó migrada», usa:

```http
POST /api/v1/google-services/personas/procesar
X-API-Key: wsk_...
```

```json
{
  "identificacion": "0954778106",
  "nombres": "JOSE NICOLAS",
  "apellidos": "CABALLERO FRANCO",
  "correo": "jose.caballero@casagrande.edu.ec",
  "orgUnitPath": "/Academico/Estudiantes",
  "grupos": ["estudiantes@casagrande.edu.ec"]
}
```

Audita, decide y actúa. Devuelve **`migrado`**, que es lo que guardas en tu tabla:

| Veredicto | Qué hace el servicio | `migrado` |
|---|---|---|
| `vinculada` | Nada. Ya tiene cuenta y cédula | `true` |
| `existe_sin_cedula` | Escribe la cédula en su cuenta | `true` |
| `corregir_formato` | Normaliza la cédula mal escrita | `true` |
| `conflicto_cedula` | **Nada.** Su cuenta lleva otra cédula | `false` |
| `revisar_multicuenta` | **Nada.** Tiene varias cuentas | `false` |
| `ambigua` | **Nada.** Varias cuentas empatadas | `false` |
| `solo_cuentas_inactivas` | **Nada.** Solo tiene cuentas archivadas | `false` |
| `cedula_invalida` | **Nada.** La cédula es de relleno | `false` |
| `correo_ocupado` | Busca una dirección libre y **crea** la cuenta | `true` |
| `disponible` | **Crea** la cuenta | `true` |

Cuando `migrado` es `false`, viene `requiere_revision: true`: un humano tiene que
decidir. **Nunca se sobrescribe una cédula ni se elige una cuenta a ciegas.**

**Solo crea si envías `orgUnitPath`.** Sin él, un veredicto `disponible` devuelve
`accion: "crear"` con el correo sugerido y no toca nada — útil para previsualizar.

Y mira siempre `correo_en_uso` y `actualizar_en_origen`: te dicen si el correo que
guardas es de otra persona y cuál es el correcto.

> `correo_ocupado` significa que **la persona no tiene cuenta** *y* que el correo que
> enviaste es de otro. Si la persona sí tiene cuenta bajo otra dirección, el veredicto
> será el de esa cuenta y el correo ajeno se te informa aparte, en `correo_ajeno`. Sin
> esa distinción se crearían cuentas duplicadas.

Si prefieres controlar cada paso, sigue el flujo largo.

## El flujo de alta, paso a paso

### 1. ¿Ya tiene cuenta?

```http
GET /api/v1/google-services/personas/0954778106
X-API-Key: wsk_...
```

```json
{
  "result": true,
  "status": "encontrada",
  "identificacion": "0954778106",
  "total": 1,
  "cuentas": [{
    "email": "andy.moreira@casagrande.edu.ec",
    "google_id": "1079...",
    "ou": "/Academico/Estudiantes",
    "principal": true,
    "consumidor": "ucgone",
    "creado_en": "2026-07-09 16:12:59"
  }]
}
```

Responde en **~2 ms** (sale del índice en PostgreSQL, no de Google).

Si `status` es `encontrada`, **has terminado**. La persona ya tiene cuenta; usa ese
correo. Si es `no_encontrada`, sigue al paso 2.

> Una persona puede tener **varias cuentas** a la vez: la misma persona como docente
> y como estudiante. Por eso `cuentas` es una lista. La principal va primero.

### 1.b. Diagnóstico completo *(cuando algo no cuadra)*

`GET /personas/{cedula}` responde en 2 ms pero solo sabe lo que hay en el índice. Si
necesitas el veredicto completo —incluido si el correo que guardas pertenece a otra
persona—, pregunta en vivo:

```http
POST /api/v1/google-services/personas/auditar
```

```json
{
  "identificacion": "0925555555",
  "nombres": "JOEL ANDRES",
  "apellidos": "BANCHON ALVARADO",
  "correo": "ulises.prado@casagrande.edu.ec"
}
```

```json
{
  "estado": "existe_sin_cedula",
  "accion_sugerida": "Vincular la cuenta con POST /personas/{cedula}/vinculos.",
  "metodo": "nombre",
  "cuenta": {"email": "joel.banchon@casagrande.edu.ec", "google_id": "1101...",
             "ou": "/Academico/Estudiantes", "cedula_en_google": null},
  "correo_ajeno": {"email": "ulises.prado@casagrande.edu.ec",
                   "nombre": "ULISES ANTONIO PRADO NAREA"},
  "detalle": "Se identificó su cuenta, pero no lleva la cédula."
}
```

Fíjate en lo que dice esa respuesta: el correo que guardaba el sistema de origen es de
otra persona (`correo_ajeno`), **pero Joel sí tiene su propia cuenta**, hallada por
nombre. El veredicto es el de *su* cuenta, no el del correo equivocado. Hay **126
personas** así en los datos actuales.

**Solo lee. No crea ni modifica nada.** Es la versión individual del informe que
produjo el backfill, y comparte con él las mismas reglas: un caso recibe el mismo
veredicto por las dos vías.

Cuesta entre 1 y 4 llamadas a Google (~0,5 s cada una) según lo lejos que haya que
bajar en la escalera de búsqueda: **cédula → correo → nombre**. Úsalo para
diagnosticar, no en un bucle.

Cada respuesta trae `accion_sugerida`, para que no reimplementes esta tabla en tres
sistemas:

| `estado` | Qué significa | Qué hacer |
|---|---|---|
| `vinculada` | Su cuenta ya lleva la cédula | Nada |
| `existe_sin_cedula` | Se identificó la cuenta, falta la cédula | `POST /personas/{cedula}/vinculos` |
| `corregir_formato` | La lleva mal escrita (sin el cero inicial) | Vincular de nuevo |
| `conflicto_cedula` | Su cuenta lleva **otra** cédula | Revisión humana. **No sobrescribir** |
| `revisar_multicuenta` | Tiene varias cuentas y la principal no es la que la identificó | Revisión humana |
| `ambigua` | Varias cuentas activas con la misma jerarquía | Revisión humana |
| `correo_ocupado` | **No tiene cuenta** y el correo que enviaste es de otra persona | Crearle una con otra dirección |
| `solo_cuentas_inactivas` | Sus cuentas están archivadas o suspendidas | Decidir si reactivar |
| `disponible` | No tiene ninguna cuenta | `POST /personas/` |
| `cedula_invalida` | Cédula de relleno (`0000000000`) | Cargar la real |

### 2. ¿Qué dirección le corresponde? *(opcional)*

```http
GET /api/v1/google-services/correos/sugerir?nombres=JOSE%20NICOLAS&apellidos=CABALLERO%20FRANCO
```

```json
{
  "correo": "josenicolas.caballero@casagrande.edu.ec",
  "patron": "josenicolas.caballero",
  "intentos": 2,
  "ocupados": [{
    "correo": "jose.caballero@casagrande.edu.ec",
    "pertenece_a": "JOSE ALEJANDRO CABALLERO MEIER"
  }]
}
```

Aplica la nomenclatura real del dominio y comprueba cada peldaño en vivo:

| Peldaño | Ejemplo |
|---|---|
| `nombre.apellido` | `jose.caballero` |
| `nombre1nombre2.apellido` | `josenicolas.caballero` |
| `nombre.apellido2` | `jose.franco` |
| `nombre.apellido1apellido2` | `jose.caballerofranco` |
| sufijo numérico | `jose.caballero2` |

Los apellidos compuestos se tratan bien: *Roque Daniel de la Cruz Alcívar* →
`roque.delacruz@`, no `cruz.de@`.

> **Este paso solo vale si el paso 1 devolvió `no_encontrada`.** Si lo llamas para
> alguien que ya tiene cuenta, verás su propia dirección marcada como ocupada y te
> propondrá una variante. A Walter Jara le sugeriría `walterjavier.jara@`.

Puedes saltarte este paso: el alta calcula la dirección igualmente.

### 2.b. ¿A qué unidad y a qué grupos?

El alta exige un `orgUnitPath` y acepta una lista de `grupos`. Los valores válidos los
da el propio servicio, así que **no los codifiques en tu sistema**:

```http
GET /api/v1/google-services/unidades/     -> 80 unidades, con ruta, nombre y padre
GET /api/v1/google-services/grupos/       -> 184 grupos, con correo y nombre
```

Si mandas una ruta o un grupo que no existen, el alta responde **`400`** con el
nombre del endpoint donde consultarlos, y **no crea nada**. La validación ocurre
antes de tocar Google.

### 3. Crear

```http
POST /api/v1/google-services/personas/
X-API-Key: wsk_...
```

```json
{
  "identificacion": "0954778106",
  "nombres": "JOSE NICOLAS",
  "apellidos": "CABALLERO FRANCO",
  "orgUnitPath": "/Academico/Estudiantes",
  "grupos": ["estudiantes@casagrande.edu.ec"],
  "correo_propuesto": "jose.caballero@casagrande.edu.ec"
}
```

`correo_propuesto` es opcional: es **la dirección que tu sistema tiene guardada**. Si
está libre, se usa. Si pertenece a otra persona, se asigna la siguiente de la
nomenclatura y se te avisa.

**201 Created** — se creó la cuenta:

```json
{
  "status": "creada",
  "correo": "josenicolas.caballero@casagrande.edu.ec",
  "google_id": "1136...",
  "correo_en_uso": true,
  "actualizar_en_origen": true,
  "correo_propuesto": "jose.caballero@casagrande.edu.ec",
  "ocupados": [{"correo": "jose.caballero@casagrande.edu.ec",
                "pertenece_a": "JOSE ALEJANDRO CABALLERO MEIER"}],
  "password_inicial": "0954778106",
  "grupos_asignados": [{"grupo": "estudiantes@casagrande.edu.ec", "resultado": "agregado"}]
}
```

**200 OK** — la persona ya tenía cuenta y **no se creó otra**:

```json
{
  "status": "ya_existia",
  "correo": "andy.moreira@casagrande.edu.ec",
  "correo_en_uso": true,
  "actualizar_en_origen": true,
  "password_inicial": null
}
```

### 4. Confirmar

```http
GET /api/v1/google-services/personas/0954778106/confirmar
```

```json
{ "status": "listo", "existe_en_google": true, "cedula_registrada": true }
```

**`status: "propagando"` no es un error.** Google devuelve lecturas obsoletas justo
después de escribir: lo medimos en producción, la lectura inmediata tras crear no
mostraba el cambio y tres segundos después sí. Si tratas eso como un fallo,
concluirás que el alta no funcionó y crearás la cuenta otra vez.

Reintenta cada 2 segundos, hasta unos 30. Si sigue en `propagando`, escala.

---

## Los dos booleanos que debes mirar

Tu sistema guarda un correo institucional. Los datos dicen que muchas veces está mal.
Estos dos campos te dicen si tienes que corregirlo:

| Campo | Significa | Qué hacer |
|---|---|---|
| `correo_en_uso` | El `correo_propuesto` que enviaste **pertenece a otra persona** | Tu registro está mal. Corrígelo con `correo`. |
| `actualizar_en_origen` | El `correo` definitivo **difiere** del que enviaste | Guarda `correo` en tu tabla. |

Aparecen en las dos respuestas, `creada` y `ya_existia`. En el caso `ya_existia` son
igual de importantes: significan que tu tabla tiene una dirección que no es la real
de esa persona.

```python
r = requests.post(f"{API}/google-services/personas/", json=payload, headers=H).json()

if r["actualizar_en_origen"]:
    persona.emailinst = r["correo"]      # la verdad es la de Google
    persona.save()

if r["correo_en_uso"]:
    log.warning("El correo %s era de otra persona (%s)",
                r["correo_propuesto"], r["ocupados"][0]["pertenece_a"])
```

---

## Implementar el proceso en tu sistema

### 1. Lo que tu tabla de personas necesita guardar

No copies `google_vinculos`. Guarda solo el resultado del proceso:

```sql
ALTER TABLE personas
  ADD COLUMN google_id          TEXT,        -- id inmutable de la cuenta
  ADD COLUMN migrado            BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN migrado_en         TIMESTAMP,
  ADD COLUMN estado_google      TEXT,        -- el último veredicto recibido
  ADD COLUMN requiere_revision  BOOLEAN NOT NULL DEFAULT FALSE;
```

`emailinst` ya la tienes: el proceso la **corrige**. `migrado` es la bandera con la
que sabrás qué falta por hacer y podrás reanudar sin repetir.

### 2. El bucle

```python
PENDIENTES = Persona.objects.filter(migrado=False, requiere_revision=False)

for p in PENDIENTES.iterator():
    r = post(f"{API}/google-services/personas/procesar", headers=H, json={
        "identificacion": p.identificacion,
        "nombres": p.nombres,
        "apellidos": p.apellidos,
        "correo": p.emailinst or None,
        "orgUnitPath": ou_para(p),          # de GET /unidades/, no codificada a fuego
        "grupos": grupos_para(p),           # de GET /grupos/
    })

    if r.status_code == 502:                 # Google caído: reintentar más tarde
        continue                             # NO marcar nada; el bucle lo recogerá
    if r.status_code in (400, 409):          # datos nuestros, o dos personas una cuenta
        p.requiere_revision = True
        p.estado_google = r.json()["detail"]
        p.save(); continue

    d = r.json()

    if d["actualizar_en_origen"]:            # nuestro correo no era el suyo
        p.emailinst = d["correo"]

    p.estado_google = d["estado"]
    p.requiere_revision = d["requiere_revision"]
    p.migrado = d["migrado"]
    if d["migrado"]:
        p.google_id = d["google_id"]
        p.migrado_en = now()
    if d.get("password_inicial"):            # SOLO al crear; no se vuelve a mostrar
        entregar_credenciales(p, d["correo"], d["password_inicial"])
    p.save()
```

Tres cosas que ese bucle hace bien y conviene no romper:

**Reanudable.** Solo procesa `migrado=False`. Si el proceso muere a mitad, se reanuda
donde estaba. Y si vuelves a procesar a alguien ya migrado, el servicio responde
`vinculada` sin tocar nada: es idempotente.

**No marca nada ante un `502`.** Google caído no es un veredicto. Si marcaras
`requiere_revision`, esa persona quedaría fuera del bucle para siempre.

**Guarda `password_inicial` en el acto.** Solo viene cuando la cuenta se acaba de
crear, y no hay forma de recuperarla después.

### 3. Los que quedan fuera

`requiere_revision=True` significa que **ningún automatismo es seguro**: una cédula
que no coincide, varias cuentas, o datos inválidos. Sácalos a un informe para que
alguien decida.

En el cruce actual son **2 002 sobre 34 993 personas** (el 5,7 %), y la inmensa
mayoría —1 849— son `solo_cuentas_inactivas`: gente cuya única cuenta está archivada
o suspendida. Los demás: 93 `revisar_multicuenta`, 57 `ambigua`, 3 `cedula_invalida`.

No los reintentes en bucle: el veredicto no va a cambiar solo.

### 3.b. El `409`: dos personas, una cuenta

`procesar` puede devolver **`409 Conflict`**:

```
La cuenta '1101...' ya está vinculada a la cédula '0912345678'.
No se puede asignar también a '0987654321': revisa si son la misma persona.
```

Ocurre con **homónimos**. El servicio serializa las altas con un cerrojo por cédula,
pero dos personas con cédulas distintas se bloquean sobre llaves distintas: ambas
pueden identificar la misma cuenta por el nombre. Una restricción en la base impide
que la segunda pise a la primera.

Trátalo como `requiere_revision`. **No lo reintentes**: no se resuelve solo.

### 4. Ritmo y cuota

Cada llamada a `procesar` cuesta entre **1 y 4 consultas a Google** (~0,5 s cada una)
según lo lejos que baje en la escalera **cédula → correo → nombre**, más las de
creación si la hay. En la práctica, entre **1 y 3 segundos por persona**.

La cuota del Admin SDK es de unas **2 400 peticiones por minuto**, y **la comparten
los tres sistemas**. No es teórica: durante la migración masiva la agotamos y 159
cuentas fallaron.

- Procesa en **serie**, o con 4-8 hilos como mucho.
- Si recibes muchos `502`, baja el ritmo: el servicio ya reintenta internamente hasta
  6 veces con esperas de 10, 20, 40 y 75 segundos, así que un `502` significa que
  Google lleva **minutos** sin responder.
- Para volúmenes grandes (miles de altas), córrelo **fuera del horario de clases** y
  coordínalo con los otros dos sistemas.

Si solo necesitas saber si alguien tiene cuenta, no llames a `procesar`:
`GET /personas/{cedula}` responde en **2 ms** desde el índice y no consume cuota de
Google.

## Idempotencia y reintentos

**El alta es idempotente por cédula.** Si pierdes la respuesta por un timeout,
**repite exactamente el mismo `POST`**. La segunda vez responderá `200 ya_existia`
con la cuenta que se creó, no una duplicada.

No necesitas clave de idempotencia: la cédula ya lo es.

**Los tres sistemas pueden pedir el alta de la misma persona sin coordinarse.** El
servicio serializa las peticiones concurrentes con un cerrojo por cédula en
PostgreSQL: si UCG One y Posgrados dan de alta al mismo estudiante en el mismo
instante, uno recibe `201` y el otro `200`, y **se crea una sola cuenta**. Está
probado — sin ese cerrojo saldrían dos cuentas con direcciones distintas.

Esto importa: **4 993 personas figuran en dos de los tres sistemas, y 143 en los tres.**

---

## Contraseña inicial

Por defecto es **la cédula** de la persona (`GOOGLE_PASSWORD_INICIAL=cedula`), y la
cuenta se crea siempre con `changePasswordAtNextLogin`.

> La cédula ecuatoriana es un dato público. Hasta que la persona entre por primera
> vez, cualquiera que la conozca puede acceder a su cuenta. Si eso no es aceptable,
> cambia la política a `aleatoria`: el servicio devolverá una contraseña fuerte en
> `password_inicial`, **una sola vez**, y tu sistema deberá entregársela.

`password_inicial` solo viene cuando `status` es `creada`. En `ya_existia` es `null`.

---

## Códigos de respuesta

| Código | Cuándo | Qué hacer |
|---|---|---|
| `200` | La persona ya tenía cuenta | Usar `correo`. Mirar `actualizar_en_origen`. |
| `201` | Cuenta creada | Guardar `correo` y `password_inicial`. Confirmar. |
| `400` | Cédula vacía, correo fuera del dominio, unidad o grupo inexistentes | Corregir la petición. |
| `401` | Clave ausente, inválida o revocada | Revisar la API key. |
| `404` | La cuenta indicada no existe en Google | — |
| `409` | Esa cuenta ya pertenece a otra cédula (homónimos) | Revisión humana. **No reintentar** |
| `500` | El servicio no está bien configurado | Avisar a TI; no reintentar. |
| `502` | Google falló o agotó la cuota | **Reintentar** con espera creciente. |

Un `502` es transitorio. El servicio ya reintenta internamente (hasta 6 veces ante
cuota agotada, con esperas de 10, 20, 40 y 75 segundos), así que un `502` significa
que Google lleva minutos sin responder.

---

## Todos los endpoints

Todos exigen una clave válida (scope `consumo` basta):

| Método | Ruta | Para qué |
|---|---|---|
| `GET` | `/personas/{cedula}` | Cuentas de una persona. `?verificar=true` contrasta con Google |
| `POST` | `/personas/procesar` | **Audita, actúa y devuelve `migrado`.** El camino corto |
| `POST` | `/personas/auditar` | Veredicto en vivo. Solo lee |
| `POST` | `/personas/` | Alta idempotente |
| `GET` | `/personas/{cedula}/confirmar` | ¿Ya propagó la cuenta? |
| `GET` | `/correos/sugerir` | Primera dirección libre |
| `POST` | `/personas/{cedula}/vinculos` | Vincular a mano una cuenta existente |
| `DELETE` | `/personas/{cedula}/vinculos/{google_id}` | Quitar el vínculo del índice |
| `GET` | `/unidades/` | Árbol de unidades organizativas (valores válidos de `orgUnitPath`) |
| `GET` | `/grupos/` | Grupos del dominio (valores válidos de `grupos`) |
| `GET` | `/grupos/{grupo}/miembros` | Miembros de un grupo, con su rol |
| `POST` | `/grupos/{grupo}/miembros` | Añadir un miembro (idempotente) |
| `DELETE` | `/grupos/{grupo}/miembros/{correo}` | Quitar un miembro (idempotente) |

Todas cuelgan de `/api/v1/google-services/` en la URL unificada del clasificador
(ver [Dónde está el servicio](#dónde-está-el-servicio)).

---

## Qué NO hacer

**No consultes por correo para saber si alguien tiene cuenta.** El correo colisiona.
Usa `GET /personas/{cedula}`.

**No trates `propagando` como un error.** Google tarda segundos en reflejar lo que
acaba de escribir.

**No guardes el `google_id` como llave en tu sistema.** Guárdalo si quieres, pero la
llave es la cédula: es lo único que ambos lados entienden.

**No reintentes un `400` ni un `500`.** El primero es tu petición; el segundo, nuestra
configuración. Solo el `502` se reintenta.

**No asumas que tu correo guardado es el correcto.** Al cruzar las 34 993 personas de
los tres sistemas contra Google, **657 tenían registrado un correo que pertenece a
otra persona** (el 1,9 %): 531 no tenían cuenta y 126 sí la tenían, bajo otra
dirección. Por eso existen `correo_en_uso`, `actualizar_en_origen` y `correo_ajeno`.

**No confundas `correo_ocupado` con «el correo es de otro».** Son cosas distintas y
mezclarlas crea cuentas duplicadas:

- `estado: "correo_ocupado"` → la persona **no tiene cuenta** *y* el correo que
  enviaste es de otro. Hay que crearle una con otra dirección.
- `correo_ajeno: {...}` → el correo que enviaste es de otro, **pero la persona sí
  tiene cuenta** bajo otra dirección. El `estado` será el de esa cuenta.

En ambos casos `correo_en_uso` viene a `true` y tienes que corregir tu tabla.
