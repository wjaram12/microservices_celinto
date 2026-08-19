-- ===========================================================================
-- Consultas sobre google_vinculos: qué correos se crearon y por dónde entraron
-- ---------------------------------------------------------------------------
-- Tabla: google_vinculos  (ver google_services/BASE_DE_DATOS.md)
--
-- OJO CON EL ALCANCE DE ESTAS CONSULTAS
--   Esta tabla es un ÍNDICE de lo que hay en Google, no la fuente de verdad.
--   Dice quién registró el vínculo y cuándo; NO prueba que la cuenta siga viva
--   en el directorio. Para eso está GET /personas/{cedula}/confirmar (una cuenta)
--   o GET /personas/{cedula}?verificar=true (todas las suyas). La consulta 9
--   prepara justo esa lista.
--
-- QUÉ SIGNIFICA CADA `origen` (lo escribe google_services/router.py)
--   creacion       POST /personas/ creó una cuenta NUEVA en Google  <- "creado por la API"
--   sincronizacion POST /personas/ encontró la cuenta ya existente y solo la adoptó
--   manual         POST /personas/{cedula}/vinculos, vinculación explícita
--   backfill       declarado en ORIGENES pero NADIE lo escribe hoy (ver abajo)
--
-- CUIDADO CON LA SIEMBRA: sincronizar_vinculos.py:82 registra las filas del backfill
-- con origen='sincronizacion' y consumidor='backfill' (su valor por defecto), NO con
-- origen='backfill'. Es decir: para separar la siembra masiva de lo que hizo la API
-- hay que mirar `consumidor`, no `origen`. Comprobado sobre la base: las 23 620 filas
-- sembradas son ('sincronizacion', 'backfill').
--
-- `consumidor` sale de la API key: ucgone - posgrados - sga - backfill
-- Un reintento NO cambia `creado_en` ni `consumidor`: manda quien registró primero.
--
-- Las fechas y filtros marcados con  <<< EDITAR  son los que querrás tocar.
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. Foto general: cuántas cuentas entraron por cada vía
--    Es lo mismo que devuelve GET /google-services/vinculos/estado, con fechas.
-- ---------------------------------------------------------------------------
SELECT origen,
       consumidor,
       count(*)                        AS cuentas,
       count(DISTINCT identificacion)  AS personas,
       min(creado_en)::date            AS primera,
       max(creado_en)::date            AS ultima
FROM   google_vinculos
GROUP  BY origen, consumidor
ORDER  BY cuentas DESC;


-- ---------------------------------------------------------------------------
-- 2. LAS CUENTAS CREADAS POR LA API - el listado que normalmente se pide
-- ---------------------------------------------------------------------------
SELECT identificacion,
       email,
       ou,
       consumidor,
       principal,
       creado_en,
       actualizado_en
FROM   google_vinculos
WHERE  origen = 'creacion'
ORDER  BY creado_en DESC;


-- ---------------------------------------------------------------------------
-- 3. Creadas en una ventana concreta (para un corte semanal o un informe)
-- ---------------------------------------------------------------------------
SELECT identificacion, email, ou, consumidor, creado_en
FROM   google_vinculos
WHERE  origen = 'creacion'
  AND  creado_en >= DATE '2026-07-01'      -- <<< EDITAR: desde (inclusive)
  AND  creado_en <  DATE '2026-08-01'      -- <<< EDITAR: hasta (exclusive)
ORDER  BY creado_en;


-- ---------------------------------------------------------------------------
-- 4. Ritmo de altas: cuántas por día y por sistema
-- ---------------------------------------------------------------------------
SELECT creado_en::date AS dia,
       consumidor,
       count(*)        AS altas
FROM   google_vinculos
WHERE  origen = 'creacion'
GROUP  BY dia, consumidor
ORDER  BY dia DESC, altas DESC;


-- ---------------------------------------------------------------------------
-- 5. Altas por unidad organizativa - para ver si el destino fue el correcto
-- ---------------------------------------------------------------------------
SELECT coalesce(ou, '(sin unidad)') AS unidad,
       count(*)                     AS cuentas
FROM   google_vinculos
WHERE  origen = 'creacion'
GROUP  BY unidad
ORDER  BY cuentas DESC;


-- ---------------------------------------------------------------------------
-- 6. SANIDAD: filas que huelen mal aunque la API respondiera 201
--    Ninguna debería devolver nada. Comprobado sobre la base: hoy devuelve 0 filas.
--
--    DOS REGLAS AFINADAS CONTRA LOS DATOS REALES, no las endurezcas sin mirar:
--      - Los SUBDOMINIOS son legítimos: hay 138 cuentas en
--        @posgrados.casagrande.edu.ec. Exigir el dominio exacto las marca todas.
--      - Un documento con LETRAS no es un error: es un pasaporte o una cédula
--        extranjera (30 filas: 'AO677274', 'VS-BF232388'). Lo que no debe estar es
--        un valor de relleno, que es lo que define identidad.cedula_invalida():
--        vacío o un solo dígito repetido ('0000000000', en filas que ni son
--        personas). `^(\d)\1*$` es la versión SQL de ese `len(set(c)) == 1`.
-- ---------------------------------------------------------------------------
SELECT 'correo vacio'              AS problema, identificacion, email, consumidor, creado_en
FROM   google_vinculos WHERE btrim(coalesce(email, '')) = ''
UNION ALL
SELECT 'dominio ajeno',            identificacion, email, consumidor, creado_en
FROM   google_vinculos
WHERE  lower(email) NOT LIKE '%@casagrande.edu.ec'
  AND  lower(email) NOT LIKE '%.casagrande.edu.ec'
UNION ALL
SELECT 'sin google_id',            identificacion, email, consumidor, creado_en
FROM   google_vinculos WHERE btrim(coalesce(google_id, '')) = ''
UNION ALL
SELECT 'cedula de relleno',        identificacion, email, consumidor, creado_en
FROM   google_vinculos
WHERE  btrim(coalesce(identificacion, '')) = '' OR identificacion ~ '^(\d)\1*$'
UNION ALL
SELECT 'correo con mayusculas',    identificacion, email, consumidor, creado_en
FROM   google_vinculos WHERE email <> lower(email)
ORDER  BY problema, creado_en DESC;


-- ---------------------------------------------------------------------------
-- 7. Altas que cambiaron DESPUÉS de crearse
--    `actualizado_en` solo se escribe al re-registrar o al degradar la principal.
--    En una cuenta recién creada suele indicar un reintento del cliente o un
--    cambio de correo/unidad posterior. No es un error, pero conviene mirarlo.
-- ---------------------------------------------------------------------------
SELECT identificacion, email, ou, consumidor, creado_en, actualizado_en,
       actualizado_en - creado_en AS demora
FROM   google_vinculos
WHERE  origen = 'creacion'
  AND  actualizado_en IS NOT NULL
ORDER  BY actualizado_en DESC;


-- ---------------------------------------------------------------------------
-- 8. Personas con varias cuentas (docente + estudiante, etc.)
--    Legítimo por diseño, pero si una de las dos es `creacion` reciente puede ser
--    un alta duplicada que el cerrojo no cubrió (homónimos, cédula mal escrita).
-- ---------------------------------------------------------------------------
SELECT identificacion,
       count(*)                                     AS cuentas,
       string_agg(email || ' [' || origen || ']', ' | ' ORDER BY creado_en) AS detalle,
       bool_or(origen = 'creacion')                 AS alguna_creada_por_api
FROM   google_vinculos
GROUP  BY identificacion
HAVING count(*) > 1
ORDER  BY cuentas DESC, identificacion;


-- ---------------------------------------------------------------------------
-- 9. LISTA PARA VERIFICAR CONTRA GOOGLE
--    La tabla no sabe si la cuenta sigue viva. Esto saca las altas recientes con
--    la ruta lista para pasarla por el endpoint de confirmación, que SÍ lee Google.
--    Recuerda: status="propagando" no es un fallo, es que Google aún no la muestra.
-- ---------------------------------------------------------------------------
SELECT identificacion,
       email,
       creado_en,
       '/google-services/personas/' || identificacion || '/confirmar' AS endpoint
FROM   google_vinculos
WHERE  origen = 'creacion'
  AND  creado_en >= now() - INTERVAL '30 days'    -- <<< EDITAR: ventana a verificar
ORDER  BY creado_en DESC;


-- ---------------------------------------------------------------------------
-- 10. Integridad de las restricciones
--     Los índices únicos ux_google_vinculos_gid y ux_google_vinculos_principal
--     hacen imposibles estos casos. Se consultan para CONFIRMAR que siguen puestos:
--     si alguna devuelve filas, un índice se cayó en algún despliegue.
-- ---------------------------------------------------------------------------
SELECT 'google_id en dos personas' AS anomalia, google_id AS clave, count(*) AS filas
FROM   google_vinculos GROUP BY google_id HAVING count(*) > 1
UNION ALL
SELECT 'dos cuentas principales', identificacion, count(*)
FROM   google_vinculos WHERE principal GROUP BY identificacion HAVING count(*) > 1
UNION ALL
SELECT 'correo repetido', lower(email), count(*)
FROM   google_vinculos GROUP BY lower(email) HAVING count(*) > 1;


-- ---------------------------------------------------------------------------
-- 11. Índices y tamaño de la tabla (por si alguna de las anteriores va lenta)
-- ---------------------------------------------------------------------------
SELECT indexname, indexdef
FROM   pg_indexes
WHERE  tablename = 'google_vinculos'
ORDER  BY indexname;

SELECT pg_size_pretty(pg_total_relation_size('google_vinculos')) AS tamano_total,
       count(*)                                                  AS filas
FROM   google_vinculos;
