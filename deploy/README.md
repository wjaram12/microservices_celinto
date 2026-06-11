# Despliegue con nginx + TLS

Hoy el servicio está expuesto en `http://IP:8000` **sin cifrar**: las API keys
viajan en texto plano. Esta configuración pone nginx delante con HTTPS y deja
gunicorn escuchando solo en localhost.

```
internet ──HTTPS:443──> nginx ──HTTP:127.0.0.1:8000──> gunicorn (4 workers ASGI)
```

## 1. Instalar nginx y certbot (en el servidor)

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

## 2. Servicio systemd de gunicorn

```bash
sudo cp deploy/clasificador.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clasificador
sudo systemctl status clasificador        # debe decir "active (running)"
```

Esto reemplaza el gunicorn lanzado a mano. Importante: el unit usa
`-k uvicorn.workers.UvicornWorker` (FastAPI es ASGI) y `--bind 127.0.0.1:8000`
(ya no se expone el 8000 a internet).

## 3. Configurar nginx

Necesitas un dominio apuntando a la IP del servidor (un registro A
`clasificador.tudominio.com -> 34.44.36.139`). Luego:

```bash
sudo cp deploy/nginx/clasificador.conf /etc/nginx/sites-available/clasificador
sudo sed -i 's/TU_DOMINIO/clasificador.tudominio.com/g' /etc/nginx/sites-available/clasificador
sudo ln -s /etc/nginx/sites-available/clasificador /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t                              # valida la sintaxis
```

## 4. Certificado TLS (Let's Encrypt, gratis y auto-renovable)

```bash
sudo certbot --nginx -d clasificador.tudominio.com
sudo systemctl reload nginx
```

certbot programa la renovación automática (`systemctl list-timers | grep certbot`).

### ¿Sin dominio? (solo IP) — certificado autofirmado

Los consumidores deberán confiar en el certificado explícitamente
(`verify=False` en requests, o distribuirles el .crt). Es mejor conseguir un
dominio, pero si no hay:

```bash
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/ssl/private/clasificador.key \
  -out /etc/ssl/certs/clasificador.crt \
  -subj "/CN=34.44.36.139" -addext "subjectAltName=IP:34.44.36.139"
```

Y en `clasificador.conf` cambia las dos líneas `ssl_certificate*` por esas rutas
y `server_name TU_DOMINIO;` por `server_name 34.44.36.139;`.

## 5. Firewall (GCP)

Abrir 80/443 y **cerrar el 8000** que hoy está expuesto:

```bash
gcloud compute firewall-rules create permitir-https --allow tcp:80,tcp:443
gcloud compute firewall-rules list | grep 8000     # localizar la regla vieja
gcloud compute firewall-rules delete <nombre-de-la-regla-del-8000>
```

(O desde la consola web: VPC network → Firewall.)

## 6. Verificar

```bash
curl -s https://clasificador.tudominio.com/        # health check por TLS
curl -s http://34.44.36.139:8000/ --max-time 5     # debe FALLAR (ya no expuesto)
```

Y desde Windows, el E2E apuntando al dominio:

```powershell
$env:API_URL = "https://clasificador.tudominio.com"
$env:API_KEY_ADMIN = "wsk_..."
python probar_servicio.py
```

## Notas

- Los consumidores (celinto, ucg) deben actualizar su URL base a la HTTPS.
- `client_max_body_size 12m` cubre los archivos de 10 MB del servicio; si se
  sube el límite en `app/services/documentos.py` (MAX_BYTES), subirlo aquí también.
- Los timeouts del proxy (330 s) están alineados con el timeout de Extend (300 s).
