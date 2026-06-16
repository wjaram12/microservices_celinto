"""
Prueba manual del endpoint validar-registro-senescyt en PRODUCCIÓN.

Sube un documento (registro SENESCYT, carta de compromiso de subida de título o
apostilla) y muestra la respuesta. Opcionalmente compara identidad si le pasas
numero_identificacion y/o nombres. Valores de conexión quemados abajo.

Uso:
    .venv/Scripts/python.exe probar_validar_pago.py
    .venv/Scripts/python.exe probar_validar_pago.py "C:/ruta/doc.jpg"
    .venv/Scripts/python.exe probar_validar_pago.py "C:/ruta/doc.jpg" 0942112129
    .venv/Scripts/python.exe probar_validar_pago.py "C:/ruta/doc.jpg" 0942112129 "Carlos Andres Molina Jaramillo"

  arg1 = imagen (opcional; usa IMAGEN_POR_DEFECTO si se omite)
  arg2 = numero_identificacion (opcional; se compara contra el documento)
  arg3 = nombres (opcional; ponlo entre comillas)
"""
import json
import mimetypes
import sys

import requests

# --- Valores quemados ---------------------------------------------------------
BASE_URL = "http://136.119.71.131:8000"
ENDPOINT = "/api/v1/validaciones/validar-registro-senescyt/"
API_KEY = "wsk_DCBAyZQjn2R5JX-2fI6lEWo3tZHz9tFOAo-JsU9l_ws"
IMAGEN_POR_DEFECTO = r"C:\Users\HP\Downloads\senescyt_0942112129.pdf"
# ------------------------------------------------------------------------------

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def main():
    ruta = sys.argv[1] if len(sys.argv) > 1 else IMAGEN_POR_DEFECTO
    numero_identificacion = sys.argv[2] if len(sys.argv) > 2 else None
    nombres = sys.argv[3] if len(sys.argv) > 3 else None

    mime = mimetypes.guess_type(ruta)[0] or "image/jpeg"
    url = BASE_URL + ENDPOINT

    # Solo se envían los campos de identidad provistos (los ausentes prueban el
    # camino "sin comparar": match_document = null).
    data = {}
    if numero_identificacion:
        data["numero_identificacion"] = numero_identificacion
    if nombres:
        data["nombres"] = nombres

    print(f"POST  {url}")
    print(f"Imagen: {ruta}  ({mime})")
    print(f"Identidad enviada: {data or '(ninguna)'}\n")

    try:
        with open(ruta, "rb") as f:
            resp = requests.post(
                url,
                headers={"X-API-Key": API_KEY},
                files={"file": (ruta.replace("\\", "/").split("/")[-1], f, mime)},
                data=data,
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
        print(f"  match_document  : {cuerpo.get('match_document')}  "
              "(True=coincide · False=no coincide · None=no se comparó)")
        datos = cuerpo.get("datos") or {}
        print(f"  campos extraídos: {len(datos) if datos else '(ninguno)'}")
        if cuerpo.get("document_class") == "OTHER":
            print("\n  ⚠ Salió OTHER: o el documento no es de los 3 tipos aceptados")
            print("    (registro SENESCYT / carta de compromiso / apostilla), o la config")
            print("    de esas clases no está sembrada/activa en producción, o la caché")
            print("    quedó vieja (reiníciala en /admin/cache).")


if __name__ == "__main__":
    main()
