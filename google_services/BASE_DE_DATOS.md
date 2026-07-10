# El modelo de datos, para los sistemas clientes

Qué guarda el servicio, qué guarda Google, y **qué debe cambiar en la base de datos de
cada sistema cliente**.

Dirigido a los equipos de **UCG One**, **Posgrados** y **SGA**.

---

## Quién guarda qué

Hay dos almacenes y **no son intercambiables**. Confundirlos es el origen de casi
todos los problemas que encontramos.

### Google Workspace — la fuente de verdad

La cédula de cada persona vive **dentro de su propia cuenta**, como identificador
externo:

```json
"externalIds": [{ "value": "0954778106", "type": "custom", "customType": "identificacion" }]
```

Ya está escrita en **23 620 cuentas**. Es la verdad porque viaja con la cuenta:
sobrevive a un cambio de correo, se ve en la consola de administración, y cualquier
otra herramienta la puede leer sin pasar por nosotros.

Si la tabla y Google discrepan, **gana Google**. Siempre.

### PostgreSQL — el índice con trazabilidad

Tabla `google_vinculos` en la base del microservicio. Es un **índice derivado** de
Google, con dos cosas que Google no puede almacenar: **cuándo** se registró el vínculo
y **qué sistema** lo hizo.

Se puede borrar entera y reconstruir en dos minutos:

```bash
python sincronizar_vinculos.py      # recorre Google y rehace la tabla
```

Existe por dos razones medidas, no por gusto:

| | Latencia |
|---|---|
| Consultar Google en vivo | **516 ms** (y comparte una cuota de ~2 400 peticiones/min entre los tres sistemas) |
| Consultar la tabla | **2 ms** |

Y porque **Google no ofrece cerrojos**. Sin un cerrojo, dos sistemas que dan de alta
a la misma persona a la vez crean dos cuentas con direcciones distintas.

---

## La tabla `google_vinculos`

```sql
CREATE TABLE google_vinculos (
    id             SERIAL PRIMARY KEY,
    identificacion TEXT NOT NULL,          -- la cédula: la llave real
    google_id      TEXT NOT NULL,          -- id inmutable de la cuenta en Google
    email          TEXT NOT NULL,          -- correo actual (puede cambiar)
    ou             TEXT,                   -- unidad organizativa
    principal      BOOLEAN NOT NULL DEFAULT TRUE,
    consumidor     TEXT NOT NULL,          -- qué sistema la registró
    origen         TEXT NOT NULL DEFAULT 'creacion',
    creado_en      TIMESTAMP NOT NULL DEFAULT now(),
    actualizado_en TIMESTAMP,
    UNIQUE (identificacion, google_id)
);
```

Una fila **por cuenta**, no por persona: alguien puede ser docente y estudiante a la
vez y tener dos cuentas. Por eso la clave es el par `(identificacion, google_id)`.

### Los campos que importan

| Campo | Qué significa |
|---|---|
| `identificacion` | La cédula. **Es la llave**. Los tres sistemas hablan de esto. |
| `google_id` | Identificador inmutable de la cuenta. No cambia aunque cambie el correo. |
| `email` | El correo **actual**. Puede cambiar; no lo uses como llave. |
| `ou` | Unidad organizativa de la cuenta, p. ej. `/Academico/Docentes`. Es lo que distingue las dos cuentas de una misma persona. |
| `principal` | Cuál de sus cuentas es la principal. **Como mucho una por persona** (lo garantiza un índice único parcial). |
| `consumidor` | Qué sistema la registró: `ucgone`, `posgrados`, `sga`, `backfill`. Sale de la API key. |
| `origen` | `backfill` · `creacion` · `sincronizacion` · `manual`. |
| `creado_en` | Cuándo se registró. Un reintento **no** lo cambia: quien la registró primero es quien la creó. |
| `actualizado_en` | Última vez que cambió el correo o la unidad. |

### Índices

```sql
ix_google_vinculos_ced        btree (identificacion)      -- la búsqueda que hacen los 3 sistemas
ix_google_vinculos_mail       btree (lower(email))        -- al auditar una dirección
ux_google_vinculos_gid        UNIQUE (google_id)
ux_google_vinculos_principal  UNIQUE (identificacion) WHERE principal
```

Los dos últimos son **restricciones de integridad**, no optimizaciones.

`ux_google_vinculos_principal` impide que una persona tenga dos cuentas principales.
Al registrar una nueva como principal, la anterior se degrada en la misma transacción.

`ux_google_vinculos_gid` impide que **una cuenta pertenezca a dos personas**. Cierra
una carrera que el cerrojo por cédula no cubre: dos **homónimos** con cédulas
distintas se bloquean sobre llaves distintas, así que ambos pueden identificar la
misma cuenta por el nombre e intentar escribirle su cédula. El proceso masivo detecta
eso mirando el lote entero (`conflicto_duplicado`, 20 casos); una API que atiende de
una en una no puede. Con la restricción, la segunda petición recibe un **`409`** en
vez de corromper el dato.

Por eso el servicio **escribe primero en PostgreSQL y después en Google**: la base es
la única que puede arbitrar esa exclusividad. Al revés, ya habríamos pisado la cédula
del otro cuando descubriéramos el conflicto.

### El cerrojo

Antes de crear una cuenta, el servicio toma:

```sql
SELECT pg_advisory_xact_lock(hashtext('0954778106'));
```

No necesita que la fila exista (la persona todavía no está registrada) y se libera
solo al terminar la transacción, incluso si el proceso muere.

Probado: dos altas simultáneas de la misma cédula → una recibe `201`, la otra `200`,
**se crea una sola cuenta**. Sin el cerrojo salían dos, con direcciones distintas.

Importa porque **4 993 personas figuran en dos de los tres sistemas, y 143 en los tres.**

---

## Qué debe cambiar en TU base de datos

### 1. La cédula pasa a ser la llave hacia Google

Deja de usar el correo para preguntar por alguien. No hace falta que guardes el
`google_id`: la API acepta la cédula.

```python
# antes
cuenta = google.buscar_por_correo(persona.emailinst)   # frágil

# ahora
r = requests.get(f"{API}/google-services/personas/{persona.identificacion}", headers=H)
```

### 2. Tu columna de correo **no es fiable** y hay que corregirla

Los números, medidos sobre tus propios datos:

- **204 correos** guardados en los sistemas de origen **no existen en Google**. La
  dirección se construía mal: `jose.fernando.bonilla@` en la tabla frente a
  `josefernando.bonilla@` en Google, y `ese.de@` frente a `liubinsky.delaese@`.
- **333 correos institucionales** están asignados a **dos personas distintas**.
- **657 personas** (el 1,9 % de las 34 993) tienen registrado un correo que pertenece
  a otra persona. De esas, **531 no tienen cuenta** y **126 sí la tienen**, bajo otra
  dirección. Hay casos disparatados: la tabla guarda `mariadelcisne.wong@` como correo
  de `COSME TENEZACA`, cuya cuenta real es `cosme.tenezaca@`.

Por eso cada respuesta del alta trae dos banderas. **Úsalas para corregir tu tabla:**

```python
r = requests.post(f"{API}/google-services/personas/", json=payload, headers=H).json()

if r["actualizar_en_origen"]:            # el correo definitivo difiere del tuyo
    persona.emailinst = r["correo"]
    persona.save()

if r["correo_en_uso"]:                   # el tuyo era de OTRA persona
    log.warning("El correo %s pertenece a %s",
                r["correo_propuesto"], r["ocupados"][0]["pertenece_a"])
```

> Que el correo sea de otro **no significa que la persona no tenga cuenta**. Puede
> tenerla bajo otra dirección. El servicio distingue los dos casos: `correo_ocupado`
> es el primero; `correo_ajeno` acompaña al segundo. Confundirlos crea cuentas
> duplicadas.

### 3. Normaliza la cédula antes de enviarla

En las tablas de origen hay **30 cédulas de 9 dígitos**: se guardaron como número y
perdieron el cero inicial. `925122673` es en realidad `0925122673`.

Envíala **tal como la tengas**; el servicio prueba ambas formas al buscar. Pero
arréglala en tu base, porque es la llave.

También hay **cédulas de relleno** (`0000000000`, `00000000`) en filas que no son
personas: `DIRECCIÓN GENERAL ACADÉMICA`, `Place to pay`, `Nuvei`, `PICCA`. El servicio
las rechaza. **No las mandes.**

### 4. Una persona puede tener más de una cuenta

`GET /personas/{cedula}` devuelve una **lista**. Si tu modelo asume una cuenta por
persona, revísalo: en el dominio hay **231 nombres repetidos** que en su mayoría son
la misma persona con dos cuentas legítimas — administrativa y de exalumna, o docente y
estudiante.

La primera de la lista es la principal.

### 5. No dupliques la tabla `google_vinculos`

No la copies en tu sistema. Es un índice del servicio, y **puede reconstruirse desde
Google en cualquier momento**. Si necesitas el dato, pídelo a la API: cuesta 2 ms.

Guardar tu propia copia es exactamente el error que produjo los 204 correos
desincronizados que ahora hay que arreglar.

---

## Verificar que un vínculo es real

La tabla puede quedarse atrás: alguien borra una cuenta desde la consola de
administración, o le cambia el correo. Para contrastarla contra Google:

```http
GET /api/v1/google-services/personas/0954778106?verificar=true
```

```json
{
  "cuentas": [...],
  "verificacion": {
    "coherente": false,
    "diferencias": [
      {"google_id": "1079...", "problema": "el correo cambió en Google",
       "tabla": "correo.viejo@casagrande.edu.ec", "google": "correo.nuevo@casagrande.edu.ec"}
    ]
  }
}
```

Detecta tres divergencias: la cuenta ya no existe, el correo cambió, o la cédula en
Google no coincide. Cuesta ~500 ms por cuenta, así que **no lo uses en el camino
crítico**; úsalo en auditorías o cuando algo no cuadre.

---

## Estado actual

| | |
|---|---|
| Cuentas en el dominio | 27 686 |
| Con la cédula escrita en Google | **23 620** (85,3 %) |
| Cobertura sobre cuentas activas | **93,2 %** |
| Vínculos en `google_vinculos` | 23 620 |
| Tamaño de la tabla | ~10 MB |
| Pendientes de escribir | **180** |

Las 180 pendientes son de dos naturalezas:

- **59 bloqueadas.** No se pueden escribir automáticamente: 58 pertenecen a
  administradores de Google Workspace, a los que el service account delegado no puede
  modificar, y 1 (`hector.ramirez@`) tiene el nombre corrupto en Google —74 caracteres
  de `givenName`, con 48 espacios al final— y la API rechaza cualquier actualización
  sobre ella. Están en `pruebas/pendientes_manuales.csv`.

- **121 recién descubiertas.** El veredicto `correo_ocupado` las ocultaba: se
  reportaban como «tu correo es de otro» sin llegar a buscar si la persona tenía
  cuenta propia. Al corregir la auditoría aparecieron sus cuentas, todas halladas por
  nombre exacto. Se escriben con el proceso normal:

  ```bash
  python migrar_cedulas_google.py aplicar --real   # la bitácora salta las ya hechas
  ```
