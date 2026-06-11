"""
Errores del dominio. Las views los traducen a códigos HTTP:

    ErrorDeArchivo    -> 400 (formato o tamaño del archivo)
    ErrorDeValidacion -> 400 (datos de entrada mal formados)
    ErrorDeProveedor  -> 502 (Extend no respondió o respondió con error)
"""


class ErrorDeArchivo(Exception):
    """Error recuperable (formato o tamaño) que la API traduce a un HTTP 400."""


class ErrorDeValidacion(Exception):
    """Datos de entrada mal formados (ej. cédula del sistema sin 10 dígitos) -> HTTP 400."""


class ErrorDeProveedor(Exception):
    """Extend no respondió o respondió con error: la API lo traduce a un HTTP 502."""
