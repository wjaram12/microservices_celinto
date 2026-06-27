"""
Paquete `commons`: infraestructura compartida por las apps del repo (el
clasificador en `app/` y la consulta de títulos en `consulta_titulos/`).

Reúne lo que antes estaba duplicado: configuración base (DB/Redis), pool de
PostgreSQL + ServicioBD, cliente Redis, y el sistema de API keys (tabla
`api_keys` + dependencias de FastAPI). Cada app importa de aquí en vez de
reimplementarlo.
"""
