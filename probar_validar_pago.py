"""
Prueba manual del endpoint validar-pago en PRODUCCIÓN.

Sube una imagen de comprobante y muestra la respuesta. Todos los valores están
quemados abajo (URL, API key, imagen por defecto). Para probar otra imagen,
pásala como argumento.

Uso:
    .venv/Scripts/python.exe probar_validar_pago.py
    .venv/Scripts/python.exe probar_validar_pago.py "C:/ruta/a/otro_comprobante.jpg"
"""
import json
import mimetypes
import sys

import requests

# --- Valores quemados ---------------------------------------------------------
BASE_URL = "http://34.44.36.139:8000"
ENDPOINT = "/api/v1/validaciones/validar-pago/"
API_KEY = "wsk_ASSGk11JTqa-Ogb3uzmzHMM7hUnbbPNhGLtI24NRxFI"
IMAGEN_POR_DEFECTO = r"C:\Users\HP\Downloads\dataset_comprobantes\63468.jpg"
# ------------------------------------------------------------------------------

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def main():
    ruta = sys.argv[1] if len(sys.argv) > 1 else IMAGEN_POR_DEFECTO
    mime = mimetypes.guess_type(ruta)[0] or "image/jpeg"
    url = BASE_URL + ENDPOINT

    print(f"POST  {url}")
    print(f"Imagen: {ruta}  ({mime})\n")

    try:
        with open(ruta, "rb") as f:
            resp = requests.post(
                url,
                headers={"X-API-Key": API_KEY},
                files={"file": (ruta.split("\\")[-1].split("/")[-1], f, mime)},
                timeout=300,
            )
    except FileNotFoundError:
        print(f"ERROR: no se encontró la imagen en {ruta}")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"ERROR de conexión con el servicio: {e}")
        sys.exit(1)

    print(f"HTTP {resp.status_code}\n")
    try:
        cuerpo = resp.json()
    except ValueError:
        print(resp.text[:2000])
        return

    print(json.dumps(cuerpo, indent=2, ensure_ascii=False))

    # Resumen legible
    if resp.status_code == 200:
        print("\n--- Resumen ---")
        print(f"  Clase detectada : {cuerpo.get('document_class')}  (confianza {cuerpo.get('confidence')})")
        print(f"  result          : {cuerpo.get('result')}")
        print(f"  status          : {cuerpo.get('status')}")
        datos = cuerpo.get("datos") or {}
        if datos:
            print(f"  campos extraídos: {len(datos)}")
        else:
            print("  campos extraídos: (ninguno)")
        if cuerpo.get("document_class") == "OTHER":
            print("\n  ⚠ Salió OTHER: el clasificador de validar-pago no está activo con las")
            print("    clases deposito/transferencia. Revisa que existan en /admin/procesadores")
            print("    las 3 filas (clasificar + extraer DEPOSITO + extraer TRANSFERENCIA),")
            print("    que la de clasificar tenga sus clasificaciones, y reinicia la caché")
            print("    en /admin/cache si las insertaste por SQL.")


if __name__ == "__main__":
    main()
