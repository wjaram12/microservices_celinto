"""
Compatibilidad: las dependencias de autenticación se movieron a commons.seguridad
(las comparten todas las apps). Se re-exportan para no romper los imports
existentes (`from app.core.seguridad import verificar_api_key, requiere_admin`).
"""
from commons.seguridad import requiere_admin, verificar_api_key  # noqa: F401
