"""
Gestión de las API keys de los sistemas consumidores.

No hay endpoint HTTP para esto a propósito (menos superficie de ataque): las
claves se administran desde la línea de comandos, con acceso al servidor.

Uso:
    python gestionar_llaves.py crear  <consumidor> [scope]
    python gestionar_llaves.py listar
    python gestionar_llaves.py revocar <consumidor|id>

`scope` (opcional, por defecto 'consumo'):
    consumo  -> clasificar y OCR (los sistemas consumidores).
    admin    -> además, gestionar los prompts del clasificador.

Ejemplos:
    python gestionar_llaves.py crear celinto-posgrados
    python gestionar_llaves.py crear ucg-posgrados
    python gestionar_llaves.py crear administracion admin
    python gestionar_llaves.py listar
    python gestionar_llaves.py revocar celinto-posgrados
    python gestionar_llaves.py revocar 3

IMPORTANTE: la clave en texto plano se muestra UNA sola vez, al crearla.
Después solo queda su hash; si se pierde, hay que generar otra.
"""
import sys

from app.services.consumidores import consumidores


def crear(consumidor: str, scope: str = "consumo") -> None:
    try:
        registro = consumidores.crear(consumidor, scope)
    except ValueError as e:
        print(f"{e}")
        sys.exit(1)

    print(
        f"API key creada para '{registro['consumidor']}' "
        f"(id {registro['id']}, scope {registro['scope']})."
    )
    print("\n Entrega esta clave al consumidor. NO se volverá a mostrar:\n")
    print(f"      {registro['llave']}\n")
    print(" El consumidor debe enviarla en cada petición en la cabecera:")
    print(f"      X-API-Key: {registro['llave']}")


def listar() -> None:
    llaves = consumidores.listar()
    if not llaves:
        print("No hay API keys registradas. Crea una con: python gestionar_llaves.py crear <consumidor>")
        return
    print(f"{'id':<4} {'consumidor':<24} {'scope':<9} {'estado':<10} {'creada':<20} {'último uso':<20}")
    print("-" * 90)
    for k in llaves:
        estado = "activo" if k["activo"] else "REVOCADO"
        print(
            f"{k['id']:<4} {k['consumidor']:<24} {k['scope']:<9} {estado:<10} "
            f"{k['creado_en']:<20} {(k['ultimo_uso'] or '—'):<20}"
        )


def revocar(identificador: str) -> None:
    n = consumidores.revocar(identificador)
    if n == 0:
        print(f"No se encontró ninguna clave activa para '{identificador}'.")
        sys.exit(1)
    print(f"{n} clave(s) revocada(s) para '{identificador}'.")


def main() -> None:
    consumidores.inicializar()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    comando = sys.argv[1].lower()

    if comando == "crear" and len(sys.argv) in (3, 4):
        scope = sys.argv[3] if len(sys.argv) == 4 else "consumo"
        crear(sys.argv[2], scope)
    elif comando == "listar" and len(sys.argv) == 2:
        listar()
    elif comando == "revocar" and len(sys.argv) == 3:
        revocar(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
