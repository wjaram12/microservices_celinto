"""
Script de prueba para el endpoint de validación dinámica.

Sube un archivo local (cédula) al servicio:
  - Sin número: solo valida que el documento sea una cédula.
  - Con número: además activa el OCR para leer la cédula del documento
    y compararla contra la del sistema.

Uso:
    python probar_validacion.py <ruta_archivo> [cedula_sistema]

Ejemplos:
    python probar_validacion.py cedula_prueba.jpg                 # solo valida que sea cédula
    python probar_validacion.py cedula_prueba.jpg 0102030405      # valida y compara el número
    python probar_validacion.py "C:\\ruta\\con espacios\\cedula.pdf" 0102030405

Opcional:
    - cambiar la URL del servicio con la variable de entorno API_URL.
    - autenticarse con la variable de entorno API_KEY (cabecera X-API-Key).
      El servicio rechaza con HTTP 401 las peticiones sin una clave válida.
"""

import os
import sys
import mimetypes
import json

import requests

# URL del endpoint (configurable por variable de entorno)
API_URL = os.environ.get(
    "API_URL",
    "http://127.0.0.1:8001/api/v1/ocr/",
)

# API key del consumidor (se genera con: python gestionar_llaves.py crear <nombre>)
API_KEY = os.environ.get("API_KEY")
print(f"🔑 API_KEY: {'(no configurada)' if not API_KEY else 'CONFIGURADA'}")
# Formatos que acepta el servicio
MIME_ACEPTADOS = {"application/pdf", "image/jpeg", "image/png"}


def main():
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        sys.exit(1)

    ruta_archivo = sys.argv[1]
    # .strip() para tratar "   " igual que el servidor: como si no se hubiera enviado.
    cedula_sistema = sys.argv[2].strip() if len(sys.argv) == 3 else None
    if not cedula_sistema:
        cedula_sistema = None

    # 1. Validar que el archivo exista
    if not os.path.isfile(ruta_archivo):
        print(f"❌ No se encontró el archivo: {ruta_archivo}")
        sys.exit(1)

    # 2. Detectar el tipo MIME a partir de la extensión
    mime_type, _ = mimetypes.guess_type(ruta_archivo)
    if mime_type not in MIME_ACEPTADOS:
        print(f"❌ Formato '{mime_type}' no admitido. Debe ser PDF, JPEG o PNG.")
        sys.exit(1)

    print(f"📤 Enviando '{os.path.basename(ruta_archivo)}' ({mime_type})")
    if cedula_sistema:
        print(f"   Cédula del sistema: {cedula_sistema}  (modo OCR + comparación)")
    else:
        print("   Sin número: solo se validará que sea una cédula")
    print(f"   Hacia: {API_URL}\n")

    # 3. Enviar la petición multipart (archivo + campo de formulario opcional)
    # La clave va en la cabecera X-API-Key; sin ella el servicio responde 401.
    cabeceras = {"X-API-Key": API_KEY} if API_KEY else {}
    if not API_KEY:
        print("⚠️  Sin API_KEY: el servicio responderá 401. "
              "Genera una con 'python gestionar_llaves.py crear <nombre>' "
              "y expórtala en la variable de entorno API_KEY.\n")

    try:
        with open(ruta_archivo, "rb") as f:
            archivos = {"file": (os.path.basename(ruta_archivo), f, mime_type)}
            # El número solo se incluye si el usuario lo pasó.
            datos = {"cedula_sistema": cedula_sistema} if cedula_sistema else {}
            respuesta = requests.post(
                API_URL, files=archivos, data=datos, headers=cabeceras, timeout=60
            )
    except requests.exceptions.ConnectionError:
        print(f"❌ No se pudo conectar al servicio. ¿Está corriendo uvicorn en {API_URL}?")
        sys.exit(1)

    # 4. Mostrar el resultado
    print(f"⬅️  HTTP {respuesta.status_code}\n")
    try:
        cuerpo = respuesta.json()
        print(json.dumps(cuerpo, indent=2, ensure_ascii=False))
    except ValueError:
        print(respuesta.text)
        sys.exit(1)

    # 5. Veredicto legible
    # La respuesta ya trae un `message` claro; el icono lo decide el resultado:
    # en modo comparación manda `match_document`; en modo simple, `result`.
    if respuesta.status_code == 200:
        exito = cuerpo.get("match_document")
        if exito is None:
            exito = cuerpo.get("result")
        print(f"\n{'✅' if exito else '❌'} {cuerpo.get('message', '')}")


if __name__ == "__main__":
    main()
