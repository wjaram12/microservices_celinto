"""
Dependencias de FastAPI para la autenticación por API key (cabecera X-API-Key).

La lógica de las claves (hash, CRUD, verificación) vive en el servicio
APIConsumidores (app/services/consumidores.py); aquí solo está la traducción
a HTTP: 401 si la clave falta o no vale, 403 si no tiene scope admin.
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.services.consumidores import consumidores


def verificar_api_key(
    x_api_key: Optional[str] = Header(
        None,
        alias="X-API-Key",
        description="API key del sistema consumidor.",
    ),
) -> dict:
    """
    Valida la cabecera `X-API-Key` y devuelve el consumidor autenticado
    ({"id", "consumidor", "scope"}). Lanza 401 si falta o no es válida. Se
    cuelga del router para proteger todos los endpoints de una sola vez.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación X-API-Key.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    registro = consumidores.verificar(x_api_key)
    if registro is None:
        # Mismo mensaje para clave inexistente, mal formada o revocada: no se
        # le da pistas a un atacante sobre por qué falló.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key inválida o revocada.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return registro


def requiere_admin(registro: dict = Depends(verificar_api_key)) -> dict:
    """
    Dependencia para las views de administración (prompts, procesadores y
    API keys).

    Reutiliza `verificar_api_key` (así una clave ausente/inválida sigue dando
    401) y además exige scope 'admin'. Una clave válida pero de consumo recibe
    **403** (autenticada, pero sin permiso): así las claves de los sistemas
    consumidores nunca pueden tocar la administración.
    """
    if registro.get("scope") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta operación requiere una API key de administrador.",
        )
    return registro
