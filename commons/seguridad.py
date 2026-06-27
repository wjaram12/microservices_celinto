"""
Dependencias de FastAPI para la autenticación por API key (cabecera X-API-Key).

La lógica de las claves (hash, CRUD, verificación) vive en commons.consumidores;
aquí solo está la traducción a HTTP: 401 si la clave falta o no vale, 403 si no
tiene scope admin. Lo comparten todas las apps.
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from commons.consumidores import consumidores


def verificar_api_key(
    x_api_key: Optional[str] = Header(
        None,
        alias="X-API-Key",
        description="API key del sistema consumidor.",
    ),
) -> dict:
    """
    Valida la cabecera `X-API-Key` y devuelve el consumidor autenticado
    ({"id", "consumidor", "scope"}). Lanza 401 si falta o no es válida.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la cabecera de autenticación X-API-Key.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    registro = consumidores.verificar(x_api_key)
    if registro is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key inválida o revocada.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return registro


def requiere_admin(registro: dict = Depends(verificar_api_key)) -> dict:
    """
    Exige scope 'admin'. Reutiliza `verificar_api_key` (una clave ausente/inválida
    sigue dando 401) y además exige scope 'admin'. Una clave válida pero de consumo
    recibe **403** (autenticada, pero sin permiso).
    """
    if registro.get("scope") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta operación requiere una API key de administrador.",
        )
    return registro
