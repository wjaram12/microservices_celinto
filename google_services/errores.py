"""
Excepciones de dominio de la app de Google Workspace.

El cliente del Admin SDK lanza estas; router.py (FastAPI) las traduce a códigos
HTTP. Mantener los errores de dominio fuera de FastAPI permite probar la lógica
sin levantar el servidor.
"""


class ErrorDeValidacion(Exception):
    """Datos de entrada inválidos (p. ej. un correo fuera del dominio
    institucional) -> el router responde 400."""


class ErrorDeGoogle(Exception):
    """Fallo al hablar con el Admin SDK: Google caído, reintentos agotados,
    permisos insuficientes, cuota excedida -> el router responde 502."""


class ErrorDeConflicto(Exception):
    """Dos personas distintas reclaman la misma cuenta de Google -> el router
    responde 409. Ocurre con homónimos: el cerrojo por cédula no los serializa,
    porque se bloquean sobre llaves distintas."""


class ErrorDeConfiguracion(Exception):
    """El servicio no está bien configurado: falta el JSON del service account,
    culpa de quien llama -> el router responde 500."""
