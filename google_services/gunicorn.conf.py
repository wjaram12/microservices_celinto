"""
Configuración de gunicorn para producción (Linux).

Arrancar (desde services/):
    gunicorn -c google_services/gunicorn.conf.py google_services.main:app

Notas:
- worker_class UvicornWorker: gunicorn gestiona los procesos y uvicorn corre la
  app ASGI dentro de cada uno. Cada worker es un proceso aparte con su propio
  cliente del Admin SDK. No hay estado compartido entre workers: el servicio no
  cachea, cada petición va a Google.
- timeout moderado: la Directory API responde en cientos de milisegundos. El peor
  caso es `agregar_miembro` con los tres reintentos de propagación (2+4 s de espera
  más las llamadas), que cabe de sobra en 60 s.
"""
import os

bind = os.getenv("BIND", "0.0.0.0:8092")

# Procesos. Ajustar a la CPU del servidor. Hilos por worker para atender varias
# peticiones (bloqueantes, usan googleapiclient) en paralelo dentro de cada proceso.
workers = int(os.getenv("WEB_CONCURRENCY", "3"))
threads = int(os.getenv("THREADS", "8"))
worker_class = "uvicorn.workers.UvicornWorker"

timeout = int(os.getenv("TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"   # stdout
errorlog = "-"    # stderr
loglevel = os.getenv("LOG_LEVEL", "info")
