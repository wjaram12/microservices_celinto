from pydantic import BaseModel, Field


class ConsultaSQL(BaseModel):
    """Sentencia SQL a ejecutar desde la consola de administración."""
    sql: str = Field(..., description="Sentencia SQL a ejecutar contra la base del servicio.")
