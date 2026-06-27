"""
Excepciones de dominio de la app de consulta de títulos.

La capa de fuente y el núcleo de caché lanzan estas; main.py (FastAPI) las traduce
a códigos HTTP. Mantener los errores de dominio fuera de FastAPI permite probar la
lógica sin levantar el servidor.
"""


class ErrorDeFuente(Exception):
    """Fallo al obtener los datos de la fuente (portal SENESCYT / mock):
    SENESCYT caído, captcha irresoluble, red, etc. -> la view responde 502."""


class ErrorDeValidacion(Exception):
    """Datos de entrada inválidos (p. ej. cédula vacía) -> la view responde 400."""
