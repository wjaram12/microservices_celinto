# Google Workspace Directory

Microservicio que administra el directorio de Google Workspace del dominio
institucional: **usuarios**, **unidades organizativas (OUs)** y **grupos**.

Es el port del cliente del Admin SDK que vivía en `emailing/services/google_client.py`
del monolito Django `academico-sga-cg`, extraído como servicio autónomo. **No** se
portó la lógica de negocio que lo rodeaba (auditoría de correos, `RegistroSincronizacion`,
serializadores de `Persona`): eso sigue en el monolito, que ahora puede consumir este
servicio por HTTP en vez de hablar con Google directamente.

Este servicio no tiene tablas propias, no usa Redis y no conoce a las personas de la
universidad: se limita a ser una fachada REST autenticada sobre la Directory API.
**Toda consulta va a Google en vivo**, sin caché — el directorio es la única fuente de
verdad y nunca se sirve un dato potencialmente viejo.

## Cómo se autentica contra Google

Un service account **no puede** administrar el directorio por sí mismo. Usa
*delegación en todo el dominio* (domain-wide delegation): actúa siempre en nombre de
un usuario que sí es administrador (`GOOGLE_ADMIN_DELEGADO`).

Para dejarlo listo:

1. En **Google Cloud Console**, en el proyecto correspondiente, crear un service
   account y descargar su clave JSON. Anotar su **Client ID** (el numérico).
2. Habilitar la **Admin SDK API** en ese proyecto.
3. En la **consola de administración de Google Workspace** →
   *Seguridad* → *Control de acceso y datos* → *Controles de API* →
   *Delegación en todo el dominio* → **Añadir**, pegar el Client ID y autorizar
   exactamente estos cinco scopes:

   ```
   https://www.googleapis.com/auth/admin.directory.user
   https://www.googleapis.com/auth/admin.directory.group
   https://www.googleapis.com/auth/admin.directory.group.member
   https://www.googleapis.com/auth/apps.licensing
   https://www.googleapis.com/auth/admin.directory.orgunit
   ```

   Deben coincidir con `SCOPES` en `config.py`. Si aquí falta uno de los que el
   código pide, **todas** las llamadas fallan con `401 unauthorized_client`.
4. Colocar el JSON en `services/google_workspace_sa.json` (está en `.gitignore`;
   **nunca** se versiona) y configurar `GOOGLE_ADMIN_DELEGADO` en `services/.env`.

## Configuración

Variables en `services/.env` (ver `.env.example`):

| Variable | Por defecto | Qué es |
|---|---|---|
| `GOOGLE_SA_FILE` | `google_workspace_sa.json` | Ruta al JSON del service account, relativa a `services/`. No confundir con `credentials.json`, que es del Document AI del clasificador. |
| `GOOGLE_ADMIN_DELEGADO` | `ucgone.users@casagrande.edu.ec` | Administrador del dominio que el service account impersona. |
| `GOOGLE_DOMINIO` | `casagrande.edu.ec` | Dominio institucional; se exige a los correos que se crean. |
| `DATABASE_URL` | — | Común a todas las apps (`commons`); solo para validar las API keys. |

## Arrancar

Siempre **desde `services/`** (es la raíz del `sys.path`: así resuelven `commons` y
los paquetes hermanos).

```bash
pip install -r requirements.txt   # dependencias de todo el repo, un solo archivo

# Standalone
uvicorn google_services.main:app --port 8092
gunicorn -c google_services/gunicorn.conf.py google_services.main:app   # producción (Linux)

# Unificado dentro del clasificador: las rutas quedan bajo /api/v1/...
uvicorn app.main:app --port 8001
```

Al arrancar se verifican las credenciales contra Google (una consulta al propio admin
delegado) y el resultado sale en el log. Si falla, **el servidor arranca igual** y los
endpoints responden `500` explicando el motivo: un problema de credenciales nunca
tumba el clasificador.

## Para los sistemas consumidores

Si integras UCG One, Posgrados o SGA contra este servicio, empieza por estos dos:

- **[`GUIA_SISTEMAS.md`](GUIA_SISTEMAS.md)** — el flujo de alta y verificación de
  cuentas, paso a paso, con las peticiones y respuestas literales.
- **[`BASE_DE_DATOS.md`](BASE_DE_DATOS.md)** — qué guarda Google, qué guarda la tabla
  `google_vinculos`, y qué hay que corregir en la base de datos de cada sistema.

La regla que resume ambos: **la llave es la cédula, nunca el correo.**

## Endpoints

Autenticación por cabecera `X-API-Key` (sistema de `commons`, tabla `api_keys`).
El **flujo de los sistemas cliente** (leer el directorio, procesar/crear personas,
vínculos, miembros de grupos — todo lo de `GUIA_SISTEMAS.md`) requiere una clave
válida (scope `consumo` basta). El scope `admin` queda para lo que no es de
clientes: el **CRUD crudo de usuarios** (proxy del Admin SDK).

| Método | Ruta | Auth | Qué hace |
|---|---|---|---|
| `GET` | `/google-services/usuarios/` | clave | Lista usuarios (`?consulta=`, `?max_resultados=`) |
| `GET` | `/google-services/usuarios/{clave_usuario}` | clave | Un usuario por correo o ID. `404` si no existe |
| `POST` | `/google-services/usuarios/` | admin | Crea una cuenta. `409` si ya existe |
| `PATCH` | `/google-services/usuarios/{clave_usuario}` | admin | Actualiza campos (contraseña, OU, suspensión) |
| `DELETE` | `/google-services/usuarios/{clave_usuario}` | admin | Elimina la cuenta (idempotente) |
| `GET` | `/google-services/unidades/` | clave | Árbol de OUs |
| `GET` | `/google-services/grupos/` | clave | Grupos del dominio |
| `POST` | `/google-services/grupos/{email_grupo}/miembros` | clave | Añade un usuario al grupo (idempotente) |

Montado en el clasificador, todas llevan el prefijo `/api/v1`.

Toda respuesta sigue la convención del repo: `result` (señal booleana), `message`
(texto para humanos, no parsear) y `status` (estado estructurado para lógica).

Errores: `400` datos inválidos · `401`/`403` autenticación · `404` no existe ·
`409` ya existe · `500` mala configuración del servicio · `502` Google falló.

### Ejemplos

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8092/google-services/unidades/

curl -X POST http://localhost:8092/google-services/usuarios/ \
  -H "X-API-Key: $API_KEY_ADMIN" -H "Content-Type: application/json" \
  -d '{"primaryEmail":"juan.perez@casagrande.edu.ec",
       "name":{"givenName":"Juan","familyName":"Pérez"},
       "password":"...","orgUnitPath":"/Estudiantes"}'

curl -X POST http://localhost:8092/google-services/grupos/docentes@casagrande.edu.ec/miembros \
  -H "X-API-Key: $API_KEY_ADMIN" -H "Content-Type: application/json" \
  -d '{"email":"juan.perez@casagrande.edu.ec","rol":"MEMBER"}'
```

Los cuerpos de `POST`/`PATCH` de usuarios son un **proxy transparente** hacia
`users.insert`/`users.patch`: además de los campos declarados en `schemas.py` se
admite cualquier otro que acepte la Directory API (`phones`, `organizations`,
`recoveryEmail`, `suspended`…) y se reenvía tal cual.

## Notas de implementación

- **Sin caché**: cada petición consulta el Admin SDK. Listar los ~180 grupos del
  dominio cuesta unos 0,6 s porque la API pagina de 200 en 200; se acepta esa latencia
  a cambio de no servir jamás un dato desactualizado. Importa sobre todo en las
  consultas de un usuario concreto, de las que depende decidir si una cuenta ya existe
  antes de crearla. Si en el futuro hiciera falta cachear, el sitio natural sería el
  consumidor, no este servicio.

- **Reintentos** (`cliente.py`): ante `500`/`503` de Google se reintenta con espera
  exponencial (2 s, 4 s, 8 s). `agregar_miembro` reintenta además ante `404`, porque
  Google tarda unos segundos en propagar un usuario recién creado.

- **Corrección respecto al original**: en el monolito, agotar los reintentos por un
  `503` devolvía `None`, lo mismo que un `404` "el usuario no existe" — y quien
  llamaba concluía que la cuenta estaba libre y la creaba por duplicado. Aquí `None`
  significa **solo** `404`; si Google no responde se lanza `ErrorDeGoogle` → `502`.

- **Construcción perezosa**: importar `cliente.py` no toca disco ni red. El cliente
  del Admin SDK se construye en el primer uso, protegido por un lock (los workers de
  gunicorn corren varios hilos). Es lo que permite el montaje tolerante a fallos en
  `app/main.py`.

## Pruebas

```bash
# desde services/, con el servicio arriba
API_URL=http://localhost:8092 API_KEY_ADMIN=wsk_... python probar_google_services.py
```

Solo hace lecturas: no crea, modifica ni borra cuentas reales.
