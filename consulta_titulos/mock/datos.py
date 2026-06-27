"""
Datos recreados de SENESCYT para el servidor mock.

Recrea la "respuesta" del portal: por cada cédula conocida, los datos del titulado
(`persona`) y sus títulos (`titulos`, agrupados por categoría). El mock los renderiza
como HTML JSF/PrimeFaces para que el scraper real los parsee sin cambios.

- Cédulas en CATALOGO -> match único: se devuelve el DETALLE directo.
- Cédula desconocida -> "sin resultados" (datatable vacío).

Para probar el camino de LISTADO (varias personas por apellido), el mock usa el
campo `apellidos`: si se busca solo por apellidos, devuelve todas las personas cuyo
apellido coincide (ver mock/main.py).
"""

# Estructura por cédula:
#   "persona": dict clave->valor (las claves se vuelven las filas del panelGrid)
#   "titulos": lista de {"categoria": str, "headers": [...], "filas": [[...], ...]}
CATALOGO = {
    "0912345678": {
        "persona": {
            "Identificación": "0912345678",
            "Nombres": "JUAN CARLOS PEREZ MORALES",
            "Nacionalidad": "ECUATORIANA",
        },
        "titulos": [
            {
                "categoria": "TÍTULOS DE TERCER NIVEL",
                "headers": ["Título", "Institución", "Tipo", "Fecha de Registro",
                            "Área o Campo", "Número de Registro"],
                "filas": [
                    ["INGENIERO EN SISTEMAS COMPUTACIONALES",
                     "UNIVERSIDAD DE GUAYAQUIL", "TERCER NIVEL", "2015-03-12",
                     "TECNOLOGÍAS DE LA INFORMACIÓN", "1006-15-1234567"],
                ],
            },
            {
                "categoria": "TÍTULOS DE CUARTO NIVEL",
                "headers": ["Título", "Institución", "Tipo", "Fecha de Registro",
                            "Área o Campo", "Número de Registro"],
                "filas": [
                    ["MAGÍSTER EN ADMINISTRACIÓN DE EMPRESAS",
                     "ESCUELA SUPERIOR POLITÉCNICA DEL LITORAL", "CUARTO NIVEL",
                     "2019-07-25", "ADMINISTRACIÓN", "7028-19-7654321"],
                ],
            },
        ],
    },
    "1700000001": {
        "persona": {
            "Identificación": "1700000001",
            "Nombres": "MARIA FERNANDA GONZALEZ TORRES",
            "Nacionalidad": "ECUATORIANA",
        },
        "titulos": [
            {
                "categoria": "TÍTULOS DE TERCER NIVEL",
                "headers": ["Título", "Institución", "Tipo", "Fecha de Registro",
                            "Área o Campo", "Número de Registro"],
                "filas": [
                    ["LICENCIADA EN CIENCIAS DE LA EDUCACIÓN",
                     "PONTIFICIA UNIVERSIDAD CATÓLICA DEL ECUADOR", "TERCER NIVEL",
                     "2012-11-05", "EDUCACIÓN", "1005-12-1112223"],
                ],
            },
        ],
    },
    "0102030405": {
        "persona": {
            "Identificación": "0102030405",
            "Nombres": "LUIS ANDRES GONZALEZ RAMIREZ",
            "Nacionalidad": "ECUATORIANA",
        },
        "titulos": [
            {
                "categoria": "TÍTULOS DE TERCER NIVEL",
                "headers": ["Título", "Institución", "Tipo", "Fecha de Registro",
                            "Área o Campo", "Número de Registro"],
                "filas": [
                    ["MÉDICO", "UNIVERSIDAD CENTRAL DEL ECUADOR", "TERCER NIVEL",
                     "2010-02-18", "SALUD", "1001-10-9998887"],
                ],
            },
        ],
    },
}


def obtener_por_cedula(cedula: str):
    """Datos de una cédula, o None si no está en el catálogo."""
    return CATALOGO.get((cedula or "").strip())


def buscar_por_apellidos(apellidos: str):
    """Lista de (cedula, registro) cuyo nombre contiene los apellidos buscados.
    Sirve para ejercitar el camino de LISTADO (varias personas)."""
    termino = (apellidos or "").strip().upper()
    if not termino:
        return []
    palabras = termino.split()
    encontrados = []
    for cedula, reg in CATALOGO.items():
        nombre = (reg["persona"].get("Nombres") or "").upper()
        if all(p in nombre for p in palabras):
            encontrados.append((cedula, reg))
    return encontrados
