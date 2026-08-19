# Endpoints de la migración de correos electrónicos

Referencia de las rutas de `google_services` que intervienen en migrar las cuentas de
correo institucional: pasar de «cada sistema guarda un correo suelto» a «cada persona
tiene su cuenta de Google identificada por su cédula».

**La llave es la cédula, nunca el correo.** El correo colisiona (dos personas distintas
que darían `nombre.apellido@`) y el nombre no identifica (231 nombres repetidos en el
dominio). Todos los endpoints de migración se indexan por `identificacion`.

Base: `http://<host>:8092` standalone, o `http://<host>:8001/api/v1` montado en el
clasificador. Autenticación por cabecera `X-API-Key` (tabla `api_keys` de `commons`);
el scope `consumo` basta para todo lo de este documento.

Toda respuesta lleva la convención del repo: `result` (señal booleana), `message`
(texto humano, **no parsear**) y `status`/`estado` (valor estructurado para lógica).

---

## Las tres rutas que importan

| Ruta | Para qué | Escribe |
|---|---|---|
| `POST /google-services/personas/procesar` | **El camino recomendado.** Audita y actúa en una sola llamada. Devuelve `migrado`. | Sí (vincula o crea) |
| `POST /google-services/personas/auditar` | Diagnóstico en vivo. Dice qué pasa con una persona sin tocar nada. | No |
| `POST /google-services/personas/` | Alta explícita de la cuenta, idempotente por cédula. | Sí (crea) |

Si migras en lote y quieres una sola llamada por persona: usa `/procesar`.
Si necesitas decidir tú el orgUnitPath por cada veredicto, o mostrar el caso a un
operador antes de escribir: `auditar` → decidir → `personas/`.

---

## 1. `POST /google-services/personas/procesar`

Audita a la persona contra Google y **ejecuta** lo que corresponda al veredicto.
Es el proceso de migración completo en una llamada.

### Petición

```json
{
  "identificacion": "0912345678",
  "nombres": "JOSE NICOLAS",
  "apellidos": "CABALLERO FRANCO",
  "correo": "jose.caballero@casagrande.edu.ec",
  "orgUnitPath": "/Academico/Estudiantes",
  "grupos": ["estudiantes@casagrande.edu.ec"]
}
```

| Campo | Obligatorio | Nota |
|---|---|---|
| `identificacion` | sí | La llave. `400` si viene vacía. |
| `nombres`, `apellidos` | sí | Se usan para hallarla si el correo no coincide, y para calcular la dirección. |
| `correo` | no | El que el sistema cliente tiene guardado. Sirve para detectar que pertenece a otra persona. |
| `orgUnitPath` | no | **Solo se crea la cuenta si se envía.** Sin él, un veredicto `disponible` devuelve `accion="crear"` con el correo sugerido y no crea nada. |
| `grupos` | no | Se validan antes de tocar nada (`400` si alguno no existe). Un fallo al añadir a un grupo se registra en el log pero no aborta el alta. |

### Qué hace con cada veredicto

| Veredicto | Acción | `migrado` |
|---|---|---|
| `vinculada` | Nada: ya tiene cuenta con su cédula | `true` |
| `existe_sin_cedula` | Escribe la cédula en su cuenta | `true` |
| `corregir_formato` | Normaliza la cédula mal escrita | `true` |
| `disponible` | Crea la cuenta (si hay `orgUnitPath`) | `true` |
| `correo_ocupado` | Busca dirección libre y crea la cuenta | `true` |
| `conflicto_cedula` | **Nada** — revisión humana | `false` |
| `revisar_multicuenta` | **Nada** — revisión humana | `false` |
| `ambigua` | **Nada** — revisión humana | `false` |
| `solo_cuentas_inactivas` | **Nada** — revisión humana | `false` |
| `cedula_invalida` | **Nada** — revisión humana | `false` |

Ante `correo_ocupado` el servicio **vuelve a auditar sin el correo** antes de crear
nada: ese veredicto dice que la dirección enviada es de otro, no que la persona no
tenga cuenta bajo otra dirección. Sin esa segunda pasada se crearía la segunda cuenta
de alguien que ya tiene una.

### Respuesta

```json
{
  "result": true,
  "message": "Cuenta 'jose.caballero@casagrande.edu.ec' creada en '/Academico/Estudiantes'.",
  "migrado": true,
  "estado": "disponible",
  "accion": "creada",
  "requiere_revision": false,
  "identificacion": "0912345678",
  "correo": "jose.caballero@casagrande.edu.ec",
  "google_id": "1043...",
  "ou": "/Academico/Estudiantes",
  "correo_en_uso": false,
  "actualizar_en_origen": false,
  "correo_propuesto": "jose.caballero@casagrande.edu.ec",
  "ocupados": [],
  "password_inicial": "xY7k...",
  "otras_cuentas": [],
  "detalle": "..."
}
```

Los campos que el sistema cliente debe leer:

- **`migrado`** — lo que se guarda. `true` solo si la persona acabó con una cuenta
  cuya cédula está registrada en Google.
- **`accion`** — `ninguna` · `vinculada` · `creada` · `crear` · `requiere_revision`.
  `crear` significa «hay que crearla pero no mandaste `orgUnitPath`».
- **`requiere_revision`** — ningún automatismo es seguro; lo decide una persona.
- **`correo`** — la dirección definitiva (o la sugerida si `accion="crear"`).
- **`actualizar_en_origen`** — el correo definitivo difiere del que enviaste:
  **escríbelo en tu tabla.**
- **`correo_en_uso`** — el correo que enviaste pertenece a otra persona; tu registro
  está mal.
- **`password_inicial`** — solo cuando la cuenta se acaba de crear. Si la política es
  `aleatoria`, es la única vez que se muestra. La cuenta exige cambiarla en el primer
  acceso.

---

## 2. `POST /google-services/personas/auditar`

Veredicto sobre una persona frente a Google. **Solo lee: no crea ni modifica nada.**
Es la versión individual y en vivo del informe del backfill masivo — comparte las
mismas reglas de identidad y jerarquía, así que un caso recibe el mismo estado por
las dos vías.

```json
{
  "identificacion": "0912345678",
  "nombres": "JOSE NICOLAS",
  "apellidos": "CABALLERO FRANCO",
  "correo": "jose.caballero@casagrande.edu.ec"
}
```

Respuesta: `estado` (los diez de la tabla de abajo), `accion_sugerida` (texto con qué
hacer), `metodo` (cómo se la identificó: `cedula`, `correo` o `nombre`), `cuenta`
(la principal hallada), `otras_cuentas` y `correo_ajeno`.

| Estado | Qué hacer |
|---|---|
| `vinculada` | Nada. Ya tiene su cuenta y su cédula registrada. |
| `existe_sin_cedula` | Vincular con `POST /personas/{cedula}/vinculos`. |
| `corregir_formato` | Vincular de nuevo: se normalizará la cédula mal escrita. |
| `conflicto_cedula` | Revisión humana. **NO sobrescribir**: una de las dos cédulas es errónea. |
| `revisar_multicuenta` | Revisión humana. Confirmar en cuál de sus cuentas va la cédula. |
| `ambigua` | Revisión humana. Varias cuentas activas con la misma jerarquía. |
| `correo_ocupado` | Corregir el correo en el sistema de origen: es de otra persona. |
| `solo_cuentas_inactivas` | Decidir si se reactiva la archivada o se crea una nueva. |
| `disponible` | Crear la cuenta con `POST /personas/`. |
| `cedula_invalida` | Cargar la cédula real en el origen antes de migrar. |

`correo_ajeno` se informa **aunque la persona sí tenga cuenta** bajo otra dirección.
En ese caso el `estado` no será `correo_ocupado`, pero el sistema de origen igualmente
guarda un dato equivocado.

Cuesta entre 1 y 4 llamadas a Google (~0,5 s cada una) según lo lejos que haya que
bajar en la escalera: cédula → correo → nombre. Si solo necesitas saber si ya tiene
cuenta, usa `GET /personas/{cedula}` (índice local, ~10 ms).

---

## 3. `POST /google-services/personas/`

Alta de la cuenta. **Idempotente por cédula**: si la persona ya tiene cuenta no crea
otra, responde `200` con `status="ya_existia"` (en vez del `201` del alta real).

```json
{
  "identificacion": "0912345678",
  "nombres": "JOSE NICOLAS",
  "apellidos": "CABALLERO FRANCO",
  "orgUnitPath": "/Academico/Estudiantes",
  "grupos": ["estudiantes@casagrande.edu.ec"],
  "correo_propuesto": "jose.caballero@casagrande.edu.ec"
}
```

`orgUnitPath` es obligatorio aquí (a diferencia de `/procesar`). La unidad y los
grupos se validan **antes** de escribir: si no existen, `400` con el mensaje de dónde
consultar los válidos.

Sobre el correo: si `correo_propuesto` está libre se usa; si pertenece a otra persona
se asigna la siguiente dirección de la nomenclatura y se marca `correo_en_uso=true`.
Si se omite, se calcula desde el nombre.

La respuesta incluye `correo` (definitivo), `google_id`, `orgUnitPath`,
`correo_en_uso`, `actualizar_en_origen`, `ocupados` (direcciones descartadas y de
quién son), `password_inicial` y `grupos_asignados` (cada grupo con `agregado`,
`ya_era_miembro` o `error`).

Toda la operación corre bajo un **cerrojo por cédula en PostgreSQL**. Sin él, dos
sistemas que dan de alta a la misma persona a la vez comprueban «¿existe?» los dos a
la vez, ven que no, y crean dos cuentas con direcciones distintas. No es hipotético:
4 993 personas figuran en dos de los sistemas y 143 en los tres. Por el cerrojo + la
idempotencia, **un reintento tras un timeout es seguro**.

---

## Apoyo

### `GET /google-services/personas/{identificacion}`

Cuentas de una persona. Responde desde la tabla de vínculos (~10 ms), que es un índice
de lo que hay en Google. Devuelve **todas** sus cuentas — una persona puede ser docente
y estudiante a la vez — con la principal primero.

Con `?verificar=true` se contrasta cuenta por cuenta contra el directorio en vivo
(~0,5 s por cuenta) y se informa de cualquier divergencia: cuenta borrada, correo
cambiado, cédula quitada a mano. **La tabla nunca gana: si discrepan, manda Google.**

### `GET /google-services/personas/{identificacion}/confirmar`

Comprueba que una cuenta recién creada ya está operativa. Existe por una razón
concreta: **Google devuelve lecturas obsoletas justo después de escribir** — verificado
en producción, tras un `patch` la lectura inmediata no mostraba el cambio y tres
segundos después sí.

Por eso `status="propagando"` **no es un fallo**: la cuenta se creó pero Google todavía
no la muestra por completo. Reintenta a los pocos segundos. Un sistema que trate el
404 inmediato como error concluirá que el alta falló cuando funcionó, y la creará otra
vez. Estados: `listo` · `propagando` · `no_encontrada`.

### `POST /google-services/personas/{identificacion}/vinculos`

Registra que una cuenta existente pertenece a una persona. Cuerpo:
`{"google_id": "...", "principal": true}`.

Escribe en los **dos** sitios: la cédula como `externalId` en la cuenta de Google (la
fuente de verdad, el dato viaja con la cuenta) y la fila en la tabla, que añade lo que
Google no guarda: la fecha y el sistema que la registró. La base va primero — es la
única que puede arbitrar que una cuenta pertenezca a una sola persona (`UNIQUE` sobre
`google_id`): si otra cédula ya la reclamó, sale `409` y Google queda intacto.

Idempotente: repetirla actualiza correo y unidad, pero conserva `creado_en` y el
consumidor original.

### `DELETE /google-services/personas/{identificacion}/vinculos/{google_id}`

Borra el vínculo de **la tabla**. No toca Google: así un borrado accidental del índice
no destruye el dato bueno.

### `GET /google-services/correos/sugerir?nombres=&apellidos=`

Primera dirección libre según la nomenclatura del dominio. Prueba `nombre.apellido` y
luego los peldaños de la escalera, comprobando cada uno en vivo. No crea nada.

**Llámalo solo después** de comprobar con `GET /personas/{cedula}` que la persona no
tiene ya cuenta. Si no, a alguien que ya tiene `walter.jara@` se le propondrá
`walterjavier.jara@`, porque su propia dirección figura como ocupada. La dirección
puede ocuparse entre esta llamada y el alta: el alta la vuelve a comprobar dentro de
su cerrojo.

### `GET /google-services/vinculos/estado`

Cuántos vínculos y personas hay registrados, desglosado por consumidor. Para ver el
avance de la migración.

### `GET /google-services/vinculos/`

**Qué cuentas hay registradas, quién las creó y cuándo.** Paginado. Filtros:
`origen`, `consumidor`, `desde`, `hasta` (ambas inclusive), `q` (busca en cédula y
correo), `limite` (1–1000, por defecto 100) y `desplazamiento`. Devuelve `total` sin
paginar, para que el cliente sepa cuánto le queda.

Sale de la tabla, no de Google: no consume cuota del Admin SDK, pero **dice qué se
registró, no qué sigue vivo**. Una cuenta borrada a mano en la consola de admin sigue
apareciendo aquí; para comprobarla, `/personas/{cedula}/confirmar`.

**Visibilidad:** una clave de consumo solo ve los vínculos que registró ella misma —
el `consumidor` que envíe se ignora y manda el de su clave. La clave admin las ve
todas. El filtro realmente aplicado vuelve en `filtros`.

Para separar lo que creó la API: `?origen=creacion`. Ojo con la siembra —
`sincronizar_vinculos.py` escribe `origen='sincronizacion'` con
`consumidor='backfill'`, **no** `origen='backfill'`, así que el backfill se aísla por
consumidor, no por origen.

### `GET /google-services/vinculos/resumen`

La versión ampliada de `/estado`, para un panel o un informe. `?dias=` (1–365, por
defecto 30) acota el desglose diario. Devuelve:

- `por_origen` — cuentas y personas por `(origen, consumidor)`, con la primera y la
  última fecha de alta. Aquí se lee de un vistazo cuántas creó la API (`creacion`)
  frente a las que solo adoptó (`sincronizacion`) o sembró el backfill.
- `por_dia` — altas por día dentro de la ventana.
- `anomalias` — filas con defectos **de forma**, con hasta cinco ejemplos cada una:
  `correo_vacio`, `dominio_ajeno`, `sin_google_id`, `cedula_de_relleno`,
  `correo_repetido`. **Lista vacía = nada que revisar.**

Dos reglas de `anomalias` están afinadas contra los datos reales y no conviene
endurecerlas sin mirar: los **subdominios son válidos** (138 cuentas viven en
`@posgrados.casagrande.edu.ec`) y un **documento con letras no es un error** (30
pasaportes y cédulas extranjeras); lo que se marca es el valor de relleno que define
`identidad.cedula_invalida()`: vacío o un solo dígito repetido.

### `GET /google-services/vinculos/reporte.csv`

El mismo listado **sin paginar y como CSV descargable**. Acepta exactamente los
filtros de `GET /vinculos/` y respeta la misma visibilidad: una clave de consumo
exporta solo lo suyo. El archivo sale como `correos_AAAA-MM-DD.csv`.

Va en *streaming* y con cursor del lado del servidor, así que las 23 000 filas no se
materializan en memoria. La contrapartida: si la base falla a mitad, el archivo llega
**cortado** en vez de dar un 500, porque la cabecera ya se envió.

Lleva BOM a propósito — el destino real es Excel, que sin él parte los acentos de las
unidades organizativas (`/Académico/...`).

---

## En el panel de administración

`/admin/correos` (📧 Correos en la barra lateral) monta las tres rutas anteriores:
cifras de cabecera, filtros por origen/consumidor/fechas/texto, la tabla paginada y
el botón de descarga del CSV, que respeta los filtros que estén puestos en pantalla.

La página no tiene API propia: consume la de Google Workspace. Duplicarla habría
significado mantener las reglas de visibilidad en dos sitios.

Las mismas consultas, para lanzarlas a mano contra la base, están en
`google_services/consultas_correos.sql`.

### Destinos: `GET /unidades/` y `GET /grupos/`

Los valores válidos de `orgUnitPath` y `grupos`. Consúltalos antes de migrar en lote:
`/personas/` y `/procesar` los validan y responden `400` si no existen.

---

## Flujo recomendado

**Migración en lote (una llamada por persona):**

```
POST /personas/procesar  →  ¿migrado?
                             ├─ true  → guardar `correo` si `actualizar_en_origen`
                             └─ false → a la cola de revisión con `estado` y `detalle`
```

**Con decisión manual por caso:**

```
GET  /personas/{cedula}          ¿ya la tenemos? (índice, barato)
POST /personas/auditar           veredicto en vivo
  ├─ vinculada                → nada
  ├─ existe_sin_cedula        → POST /personas/{cedula}/vinculos
  ├─ disponible/correo_ocupado→ POST /personas/
  └─ resto                    → revisión humana
GET  /personas/{cedula}/confirmar   tras crear, hasta status="listo"
```

---

## Errores y límites

| Código | Significado |
|---|---|
| `400` | Datos inválidos: cédula vacía, correo de otro dominio, OU o grupo inexistente |
| `401` / `403` | API key ausente, inválida, o sin el scope necesario |
| `404` | La cuenta o el grupo no existe |
| `409` | Conflicto: otra cédula ya reclamó esa cuenta de Google |
| `429` | Límite de tasa. Esperar lo que diga `Retry-After` |
| `500` | Mala configuración del servicio (credenciales, scopes) |
| `502` | Google falló tras agotar los reintentos |

**`502` nunca significa «no existe».** Ante `500`/`503` de Google se reintenta con
espera exponencial (2 s, 4 s, 8 s); si se agotan, se lanza `502`. Un `None` interno
significa **solo** `404`. Esta distinción corrige un bug del monolito, donde agotar
los reintentos devolvía lo mismo que «no existe» y quien llamaba creaba la cuenta por
duplicado.

**Límite de tasa** (por API key, en Redis): 600/min en los endpoints que consumen
cuota del Admin SDK — todos los de migración salvo los que leen del índice — y
3 000/min en las lecturas locales (`GET /personas/{cedula}`,
`DELETE .../vinculos/{google_id}`, `GET /vinculos/estado`). La cuota del Admin SDK
(~2 400 req/min) la comparten los tres sistemas cliente y ya se agotó una vez en
producción. Si Redis cae, el servicio sigue sin límite (fail-open).

---

Ver también: [`GUIA_SISTEMAS.md`](GUIA_SISTEMAS.md) (flujo paso a paso con peticiones
literales), [`BASE_DE_DATOS.md`](BASE_DE_DATOS.md) (qué guarda Google, qué guarda
`google_vinculos`) y [`README.md`](README.md) (configuración y arranque).
