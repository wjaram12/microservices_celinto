"""
Descarga los comprobantes de un Excel (columnas: id, comprobante_url) a una
carpeta, para armar un dataset. Cada archivo se nombra con su `id` y conserva la
extensión real de la URL (.jpeg/.jpg/.png/.pdf).

Características:
  - Concurrente (varios hilos) para que las 1000 descargas no tarden una eternidad.
  - Reanudable: si un archivo ya existe (mismo id), lo salta. Puedes cortar y
    volver a correr sin re-descargar.
  - Reintentos por descarga y CSV con el resultado de cada fila.

Uso:
    python descargar_dataset.py
    python descargar_dataset.py <ruta_excel> <carpeta_salida> [hilos] [limite]

Por defecto:
    excel   = C:\\Users\\HP\\Downloads\\reporte_walter.jara_2026-06-11_823.xlsx
    salida  = C:\\Users\\HP\\Downloads\\dataset_comprobantes
    hilos   = 8
    limite  = 0 (todas las filas; pon un número para probar con pocas)
"""
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl
import requests

EXCEL = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\HP\Downloads\reporte_walter.jara_2026-06-11_823.xlsx"
SALIDA = sys.argv[2] if len(sys.argv) > 2 else r"C:\Users\HP\Downloads\dataset_comprobantes"
HILOS = int(sys.argv[3]) if len(sys.argv) > 3 else 8
LIMITE = int(sys.argv[4]) if len(sys.argv) > 4 else 0

EXTENSIONES_OK = {".jpeg", ".jpg", ".png", ".pdf"}
REINTENTOS = 3
TIMEOUT = 60
CABECERAS = {"User-Agent": "dataset-downloader/1.0"}


def leer_filas(ruta_excel):
    """Devuelve [(id, url)] desde el Excel, saltando el encabezado y filas vacías."""
    wb = openpyxl.load_workbook(ruta_excel, read_only=True, data_only=True)
    ws = wb.active
    filas = []
    for idv, url in ws.iter_rows(min_row=2, values_only=True):
        if idv is None or not url:
            continue
        filas.append((str(idv).strip(), str(url).strip()))
    wb.close()
    return filas


def nombre_archivo(idv, url):
    """Nombre destino: <id> + la extensión real de la URL (o .jpg por defecto)."""
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    if ext not in EXTENSIONES_OK:
        ext = ".jpg"
    return f"{idv}{ext}"


def descargar(idv, url):
    """Descarga una fila. Devuelve (id, url, archivo, estado, detalle)."""
    archivo = nombre_archivo(idv, url)
    destino = os.path.join(SALIDA, archivo)
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        return (idv, url, archivo, "saltado", "ya existía")

    ultimo_error = ""
    for intento in range(1, REINTENTOS + 1):
        try:
            r = requests.get(url, headers=CABECERAS, timeout=TIMEOUT, stream=True)
            if r.status_code != 200:
                ultimo_error = f"HTTP {r.status_code}"
                continue
            tmp = destino + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, destino)  # renombre atómico: solo queda si terminó bien
            return (idv, url, archivo, "ok", f"{os.path.getsize(destino)} bytes")
        except Exception as e:
            ultimo_error = f"{type(e).__name__}: {e}"
    return (idv, url, archivo, "error", ultimo_error)


def main():
    if not os.path.isfile(EXCEL):
        print(f"No se encontró el Excel: {EXCEL}")
        sys.exit(1)
    os.makedirs(SALIDA, exist_ok=True)

    filas = leer_filas(EXCEL)
    if LIMITE > 0:
        filas = filas[:LIMITE]
    print(f"Excel : {EXCEL}")
    print(f"Salida: {SALIDA}")
    print(f"{len(filas)} comprobantes a procesar con {HILOS} hilos.\n")

    resultados = []
    cuenta = {"ok": 0, "saltado": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=HILOS) as ex:
        futuros = {ex.submit(descargar, idv, url): idv for idv, url in filas}
        for n, fut in enumerate(as_completed(futuros), 1):
            res = fut.result()
            resultados.append(res)
            cuenta[res[3]] = cuenta.get(res[3], 0) + 1
            if res[3] == "error":
                print(f"  [ERROR] id={res[0]}  {res[4]}")
            if n % 50 == 0 or n == len(filas):
                print(f"  {n}/{len(filas)}  (ok={cuenta['ok']} saltados={cuenta['saltado']} errores={cuenta['error']})")

    csv_path = os.path.join(SALIDA, "_resultados.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "url", "archivo", "estado", "detalle"])
        w.writerows(resultados)

    print(f"\nListo. ok={cuenta['ok']}  saltados={cuenta['saltado']}  errores={cuenta['error']}")
    print(f"Detalle por fila en: {csv_path}")
    if cuenta["error"]:
        print("Hay errores: vuelve a correr el script para reintentar SOLO los que faltan "
              "(los ya descargados se saltan).")
        sys.exit(1)


if __name__ == "__main__":
    main()
