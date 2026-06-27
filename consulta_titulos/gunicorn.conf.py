"""
Configuración de gunicorn para producción (Linux).

Arrancar (desde services/):
    gunicorn -c consulta_titulos/gunicorn.conf.py consulta_titulos.main:app

Notas:
- worker_class UvicornWorker: gunicorn gestiona los procesos y uvicorn corre la
  app ASGI dentro de cada uno. Cada worker es un proceso aparte con su propio
  pool de hilos, su propia conexión a Redis y su propio singleton de ddddocr
  (la caché es compartida vía Redis, así que la coherencia no depende del worker).
- timeout alto: una consulta EN VIVO al portal real puede tardar 30-50 s, y con
  reintentos de captcha más; el timeout por defecto de gunicorn (30 s) la mataría.
"""
import os

bind = os.getenv("BIND", "0.0.0.0:8091")

# Procesos. Ajustar a la CPU del servidor. Hilos por worker para atender varias
# consultas (bloqueantes, usan requests) en paralelo dentro de cada proceso.
workers = int(os.getenv("WEB_CONCURRENCY", "3"))
threads = int(os.getenv("THREADS", "8"))
worker_class = "uvicorn.workers.UvicornWorker"

# Una consulta en vivo + reintentos de captcha puede ser lenta: no matar el worker.
timeout = int(os.getenv("TIMEOUT", "180"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.getenv("LOG_LEVEL", "info")
