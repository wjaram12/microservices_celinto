"""
Jerarquía de las unidades organizativas del dominio.

Vive aquí, y no en el script de migración, porque la usan dos cosas que deben
coincidir SIEMPRE: el backfill, que decide en qué cuenta escribe la cédula, y el
registro de vínculos, que decide cuál es la cuenta principal de una persona. Si
las reglas divergen, la tabla dice una cosa y Google otra.

Una persona puede tener varias cuentas vivas a la vez (la misma persona como
administrativa y como exalumna, o como docente y como estudiante). La cédula
identifica a la PERSONA, no a la cuenta, así que hace falta una regla para elegir
cuál es la principal. La regla se verificó contra los casos reales del dominio.
"""

# Nunca son cuentas de una persona viva: archivadas, suspendidas, de sistema, o
# buzones compartidos.
OU_EXCLUIDAS = ("/Archive", "/ADMINISTRATIVOS SUSPENDIDOS", "/system",
                "/Cuentas Grupales generales", "/PUERTO")

# Precedencia de la cuenta principal, de mayor a menor. Menor índice = más
# principal. Reproduce los casos reales: elige la administrativa sobre la de
# exalumno, y la de docente sobre la de estudiante.
PRECEDENCIA = ("/Administrativos", "/Academico/Docentes", "/Academico/Estudiantes")


def ou_de(usuario: dict) -> str:
    """Unidad organizativa. Acepta tanto el nombre crudo de la API (`orgUnitPath`)
    como el del volcado local (`ou`)."""
    return usuario.get("ou") or usuario.get("orgUnitPath") or "/"


def es_persona(usuario: dict) -> bool:
    """¿Es la cuenta de una persona viva?"""
    if usuario.get("suspended") or usuario.get("archived"):
        return False
    return not any(ou_de(usuario).startswith(x) for x in OU_EXCLUIDAS)


def rango(usuario: dict) -> int:
    """Posición en la precedencia; cuanto menor, más principal es la cuenta."""
    ou = ou_de(usuario)
    for i, prefijo in enumerate(PRECEDENCIA):
        if ou.startswith(prefijo):
            return i
    return len(PRECEDENCIA)


def principal(cuentas: list) -> dict:
    """Cuenta principal de una persona: la de mayor precedencia de unidad y, si
    empatan, la de último acceso más reciente (las marcas ISO ordenan como texto)."""
    mejor = min(rango(u) for u in cuentas)
    empatadas = [u for u in cuentas if rango(u) == mejor]
    return max(empatadas, key=lambda u: u.get("lastLoginTime") or "")
